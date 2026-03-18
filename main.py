from __future__ import annotations

import asyncio
import json
import re
import shlex
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from astrbot.api import AstrBotConfig, llm_tool, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star
from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path
from websockets.asyncio.server import ServerConnection, serve


PLUGIN_NAME = "astrbot_plugin_phone_mcp"
SAFE_ADB_SHELL_PATTERN = re.compile(r"^[A-Za-z0-9_./:=,@%+\-\s]+$")
SAFE_ADB_SHELL_COMMANDS = {
    "getprop",
    "wm",
    "settings",
    "dumpsys",
    "pm",
    "cmd",
    "am",
    "input",
    "logcat",
    "screencap",
}


@dataclass
class StoredFrame:
    connection_id: str
    received_at_ms: int
    payload: dict[str, Any]

    @property
    def frame_id(self) -> str:
        return self.payload.get("payload", {}).get("frame_meta", {}).get("frame_id", "")

    @property
    def ui_state(self) -> dict[str, Any]:
        payload = self.payload.get("payload", {})
        if isinstance(payload, dict):
            ui_state = payload.get("ui_state")
            if isinstance(ui_state, dict):
                return ui_state
        return {}


class FrameStore:
    def __init__(self, max_frames: int) -> None:
        self.max_frames = max(1, max_frames)
        self.frames: deque[StoredFrame] = deque(maxlen=self.max_frames)
        self._condition = asyncio.Condition()

    async def add_frame(self, frame: StoredFrame) -> None:
        async with self._condition:
            self.frames.append(frame)
            self._condition.notify_all()

    def latest(self) -> StoredFrame | None:
        return self.frames[-1] if self.frames else None

    def recent(self, limit: int) -> list[StoredFrame]:
        if limit <= 0:
            return []
        return list(self.frames)[-limit:]

    async def wait_next_frame(
        self, after_received_ms: int, timeout: float
    ) -> StoredFrame | None:
        async with self._condition:
            existing = self.latest()
            if existing and existing.received_at_ms > after_received_ms:
                return existing

            try:
                await asyncio.wait_for(self._condition.wait(), timeout=timeout)
            except TimeoutError:
                return None

            latest = self.latest()
            if latest and latest.received_at_ms > after_received_ms:
                return latest
            return None


class Main(Star):
    def __init__(self, context: Context, config: AstrBotConfig | None = None) -> None:
        super().__init__(context, config)
        self.config = config or AstrBotConfig()
        self.data_dir = Path(get_astrbot_plugin_data_path()) / PLUGIN_NAME
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.latest_frame_path = self.data_dir / "latest_frame.json"
        self.screenshot_dir = self.data_dir / "screenshots"
        self.screenshot_dir.mkdir(parents=True, exist_ok=True)

        self.frame_store = FrameStore(self._cfg_int("max_frames", 30))
        self.connections: dict[str, dict[str, Any]] = {}
        self._server = None
        self._server_task: asyncio.Task[None] | None = None
        self._server_ready = asyncio.Event()

    async def initialize(self) -> None:
        self._server_task = asyncio.create_task(self._run_ws_server())

    async def terminate(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
        if self._server_task is not None:
            self._server_task.cancel()
            try:
                await self._server_task
            except asyncio.CancelledError:
                pass

    def _cfg_int(self, key: str, default: int) -> int:
        value = self.config.get(key, default)
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    def _cfg_bool(self, key: str, default: bool) -> bool:
        value = self.config.get(key, default)
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return bool(value)

    def _cfg_str(self, key: str, default: str = "") -> str:
        value = self.config.get(key, default)
        return str(value).strip() if value is not None else default

    async def _run_ws_server(self) -> None:
        host = self._cfg_str("ws_host", "0.0.0.0")
        port = self._cfg_int("ws_port", 6910)

        async def handler(ws: ServerConnection) -> None:
            await self._handle_ws_connection(ws)

        try:
            async with serve(handler, host, port, max_size=4 * 1024 * 1024) as server:
                self._server = server
                self._server_ready.set()
                logger.info("phone_mcp ws server listening at ws://%s:%s", host, port)
                await asyncio.Future()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("phone_mcp ws server error: %s", exc)
            self._server_ready.set()

    async def _handle_ws_connection(self, ws: ServerConnection) -> None:
        remote = ws.remote_address
        connection_id = f"{remote[0]}:{remote[1]}" if remote else "unknown"
        self.connections[connection_id] = {
            "remote": remote,
            "connected_at_ms": int(time.time() * 1000),
            "last_frame_id": "",
            "last_received_at_ms": 0,
            "last_package_name": "",
        }
        logger.info("phone_mcp client connected: %s", connection_id)
        try:
            async for message in ws:
                if isinstance(message, bytes):
                    message = message.decode("utf-8", errors="ignore")
                await self._handle_ws_message(connection_id, message)
        except Exception as exc:
            logger.warning("phone_mcp client %s error: %s", connection_id, exc)
        finally:
            self.connections.pop(connection_id, None)
            logger.info("phone_mcp client disconnected: %s", connection_id)

    async def _handle_ws_message(self, connection_id: str, message: str) -> None:
        try:
            payload = json.loads(message)
        except json.JSONDecodeError as exc:
            logger.warning("phone_mcp invalid json from %s: %s", connection_id, exc)
            return

        if not isinstance(payload, dict):
            logger.warning("phone_mcp ignored non-dict payload from %s", connection_id)
            return

        frame = StoredFrame(
            connection_id=connection_id,
            received_at_ms=int(time.time() * 1000),
            payload=payload,
        )
        await self.frame_store.add_frame(frame)

        ui_state = frame.ui_state
        package_name = ""
        if isinstance(ui_state, dict):
            package_name = ui_state.get("data", {}).get("package_name", "")

        state = self.connections.get(connection_id)
        if state is not None:
            state["last_frame_id"] = frame.frame_id
            state["last_received_at_ms"] = frame.received_at_ms
            state["last_package_name"] = package_name

        if self._cfg_bool("persist_latest_frame", True):
            self.latest_frame_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

    def _latest_frame(self) -> StoredFrame | None:
        return self.frame_store.latest()

    def _build_status(self) -> dict[str, Any]:
        latest = self._latest_frame()
        return {
            "server": {
                "host": self._cfg_str("ws_host", "0.0.0.0"),
                "port": self._cfg_int("ws_port", 6910),
                "ready": self._server_ready.is_set(),
            },
            "connections": list(self.connections.values()),
            "latest_frame": {
                "frame_id": latest.frame_id if latest else "",
                "received_at_ms": latest.received_at_ms if latest else 0,
                "connection_id": latest.connection_id if latest else "",
                "package_name": latest.ui_state.get("data", {}).get("package_name", "")
                if latest
                else "",
            },
            "stored_frames": len(self.frame_store.frames),
        }

    def _frame_summary(self, frame: StoredFrame) -> dict[str, Any]:
        ui_state = frame.ui_state
        data = ui_state.get("data", {}) if isinstance(ui_state, dict) else {}
        elements = data.get("elements", []) if isinstance(data, dict) else []
        actionable = 0
        texts = 0
        semantic_hints = 0
        for element in elements if isinstance(elements, list) else []:
            if not isinstance(element, dict):
                continue
            if element.get("tap_point") is not None:
                actionable += 1
            if element.get("text"):
                texts += 1
            if element.get("semantic_hint"):
                semantic_hints += 1
        return {
            "frame_id": frame.frame_id,
            "received_at_ms": frame.received_at_ms,
            "connection_id": frame.connection_id,
            "package_name": data.get("package_name", ""),
            "activity_name": data.get("activity_name", ""),
            "event_type": data.get("event_type", ""),
            "element_count": len(elements) if isinstance(elements, list) else 0,
            "actionable_count": actionable,
            "text_count": texts,
            "semantic_hint_count": semantic_hints,
        }

    def _search_nodes(self, query: str, limit: int) -> list[dict[str, Any]]:
        latest = self._latest_frame()
        if latest is None:
            return []
        ui_state = latest.ui_state
        data = ui_state.get("data", {}) if isinstance(ui_state, dict) else {}
        elements = data.get("elements", []) if isinstance(data, dict) else []
        lowered = query.strip().lower()
        matches: list[dict[str, Any]] = []
        for element in elements if isinstance(elements, list) else []:
            if not isinstance(element, dict):
                continue
            haystack = " ".join(
                [
                    str(element.get("text", "")),
                    str(element.get("resource_id", "")),
                    str(element.get("class_name", "")),
                    str(element.get("semantic_hint", "")),
                ]
            ).lower()
            if lowered and lowered not in haystack:
                continue
            matches.append(element)
            if len(matches) >= limit:
                break
        return matches

    def _adb_base_args(self, serial: str | None) -> list[str]:
        args = ["adb"]
        chosen_serial = (serial or self._cfg_str("default_adb_serial", "")).strip()
        if chosen_serial:
            args.extend(["-s", chosen_serial])
        return args

    async def _run_adb(self, args: list[str]) -> dict[str, Any]:
        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await proc.communicate()
            return {
                "command": args,
                "returncode": proc.returncode,
                "stdout": stdout.decode("utf-8", errors="ignore").strip(),
                "stderr": stderr.decode("utf-8", errors="ignore").strip(),
            }
        except Exception as exc:
            logger.error("phone_mcp adb exec failed: %s", exc)
            return {
                "command": args,
                "returncode": -1,
                "stdout": "",
                "stderr": str(exc),
            }

    async def _capture_screencap(self, serial: str | None) -> dict[str, Any]:
        args = self._adb_base_args(serial) + ["exec-out", "screencap", "-p"]
        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await proc.communicate()
            result = {
                "command": args,
                "returncode": proc.returncode,
                "stderr": stderr.decode("utf-8", errors="ignore").strip(),
                "size_bytes": len(stdout),
            }
            if proc.returncode != 0 or not stdout:
                return result

            if self._cfg_bool("persist_screenshots", True):
                file_name = f"{int(time.time() * 1000)}_{uuid4().hex[:8]}.png"
                file_path = self.screenshot_dir / file_name
                file_path.write_bytes(stdout)
                result["file_path"] = str(file_path)
            return result
        except Exception as exc:
            logger.error("phone_mcp screencap failed: %s", exc)
            return {
                "command": args,
                "returncode": -1,
                "stderr": str(exc),
                "size_bytes": 0,
            }

    async def _request_phone_frame(self, serial: str | None) -> dict[str, Any]:
        args = self._adb_base_args(serial) + [
            "shell",
            "am",
            "broadcast",
            "-a",
            "com.astramadeus.client.ACTION_REQUEST_SNAPSHOT",
            "-p",
            "com.astramadeus.client",
        ]
        return await self._run_adb(args)

    def _parse_safe_shell_command(self, command: str) -> list[str] | None:
        normalized = command.strip()
        if not normalized or not SAFE_ADB_SHELL_PATTERN.fullmatch(normalized):
            return None
        try:
            parts = shlex.split(normalized)
        except ValueError:
            return None
        if not parts or parts[0] not in SAFE_ADB_SHELL_COMMANDS:
            return None
        return parts

    @filter.command("phone_status")
    async def phone_status(self, event: AstrMessageEvent):
        """Show phone bridge status."""
        yield event.plain_result(
            json.dumps(self._build_status(), ensure_ascii=False, indent=2)
        )

    @llm_tool(name="phone_ws_status")
    async def phone_ws_status(self, event: AstrMessageEvent) -> str:
        """Get websocket bridge status and latest frame metadata."""
        return json.dumps(self._build_status(), ensure_ascii=False)

    @llm_tool(name="phone_get_latest_frame")
    async def phone_get_latest_frame(
        self, event: AstrMessageEvent, summary_only: bool = False
    ) -> str:
        """Read the latest phone UI frame as structured data.

        Args:
            summary_only(boolean): When true, return frame summary instead of full JSON.
        """
        latest = self._latest_frame()
        if latest is None:
            return json.dumps({"error": "no_frame"}, ensure_ascii=False)
        payload: Any = self._frame_summary(latest) if summary_only else latest.payload
        return json.dumps(payload, ensure_ascii=False)

    @llm_tool(name="phone_get_recent_frames")
    async def phone_get_recent_frames(
        self, event: AstrMessageEvent, limit: int = 3
    ) -> str:
        """List recent frame summaries for the phone bridge.

        Args:
            limit(number): Max number of recent frames to return.
        """
        frames = self.frame_store.recent(max(1, min(limit, 10)))
        return json.dumps(
            [self._frame_summary(frame) for frame in frames], ensure_ascii=False
        )

    @llm_tool(name="phone_request_frame")
    async def phone_request_frame(
        self,
        event: AstrMessageEvent,
        wait_for_frame: bool = True,
        timeout_sec: float = 8.0,
        serial: str = "",
    ) -> str:
        """Force the phone client to capture and push a new frame.

        Args:
            wait_for_frame(boolean): When true, wait for a newer frame after requesting.
            timeout_sec(number): Max wait time for the new frame.
            serial(string): Optional adb serial override.
        """
        latest = self._latest_frame()
        after_ms = latest.received_at_ms if latest else 0
        adb_result = await self._request_phone_frame(serial)
        payload: dict[str, Any] = {"adb": adb_result}
        if wait_for_frame and adb_result.get("returncode") == 0:
            frame = await self.frame_store.wait_next_frame(
                after_ms,
                timeout=max(0.1, min(timeout_sec, 30.0)),
            )
            payload["frame"] = frame.payload if frame is not None else {"timeout": True}
        return json.dumps(payload, ensure_ascii=False)

    @llm_tool(name="phone_wait_next_frame")
    async def phone_wait_next_frame(
        self, event: AstrMessageEvent, timeout_sec: float = 8.0
    ) -> str:
        """Wait for the next UI frame from the phone.

        Args:
            timeout_sec(number): How long to wait in seconds.
        """
        latest = self._latest_frame()
        after_ms = latest.received_at_ms if latest else 0
        frame = await self.frame_store.wait_next_frame(
            after_ms, timeout=max(0.1, min(timeout_sec, 30.0))
        )
        if frame is None:
            return json.dumps({"timeout": True}, ensure_ascii=False)
        return json.dumps(frame.payload, ensure_ascii=False)

    @llm_tool(name="phone_find_nodes")
    async def phone_find_nodes(
        self, event: AstrMessageEvent, query: str, limit: int = 10
    ) -> str:
        """Search nodes inside the latest frame without stuffing the full frame into prompt.

        Args:
            query(string): Keyword to search in text, resource id, class name or semantic hint.
            limit(number): Max number of nodes to return.
        """
        return json.dumps(
            self._search_nodes(query, max(1, min(limit, 20))), ensure_ascii=False
        )

    @llm_tool(name="phone_capture_screenshot")
    async def phone_capture_screenshot(
        self, event: AstrMessageEvent, serial: str = ""
    ) -> str:
        """Capture a full screenshot from the connected Android device via adb.

        Args:
            serial(string): Optional adb serial override.
        """
        result = await self._capture_screencap(serial)
        return json.dumps(result, ensure_ascii=False)

    @llm_tool(name="adb_list_devices")
    async def adb_list_devices(self, event: AstrMessageEvent) -> str:
        """List connected adb devices."""
        result = await self._run_adb(["adb", "devices"])
        return json.dumps(result, ensure_ascii=False)

    @llm_tool(name="adb_tap")
    async def adb_tap(
        self, event: AstrMessageEvent, x: int, y: int, serial: str = ""
    ) -> str:
        """Tap on the connected Android device via adb.

        Args:
            x(number): Tap x coordinate in pixels.
            y(number): Tap y coordinate in pixels.
            serial(string): Optional adb serial override.
        """
        result = await self._run_adb(
            self._adb_base_args(serial) + ["shell", "input", "tap", str(x), str(y)]
        )
        return json.dumps(result, ensure_ascii=False)

    @llm_tool(name="adb_swipe")
    async def adb_swipe(
        self,
        event: AstrMessageEvent,
        x1: int,
        y1: int,
        x2: int,
        y2: int,
        duration_ms: int = 300,
        serial: str = "",
    ) -> str:
        """Swipe on the connected Android device via adb.

        Args:
            x1(number): Start x.
            y1(number): Start y.
            x2(number): End x.
            y2(number): End y.
            duration_ms(number): Swipe duration in milliseconds.
            serial(string): Optional adb serial override.
        """
        result = await self._run_adb(
            self._adb_base_args(serial)
            + [
                "shell",
                "input",
                "swipe",
                str(x1),
                str(y1),
                str(x2),
                str(y2),
                str(duration_ms),
            ],
        )
        return json.dumps(result, ensure_ascii=False)

    @llm_tool(name="adb_input_text")
    async def adb_input_text(
        self, event: AstrMessageEvent, text: str, serial: str = ""
    ) -> str:
        """Input text on the connected Android device via adb.

        Args:
            text(string): Text to input.
            serial(string): Optional adb serial override.
        """
        escaped = text.replace(" ", "%s")
        result = await self._run_adb(
            self._adb_base_args(serial) + ["shell", "input", "text", escaped]
        )
        return json.dumps(result, ensure_ascii=False)

    @llm_tool(name="adb_keyevent")
    async def adb_keyevent(
        self, event: AstrMessageEvent, keycode: str, serial: str = ""
    ) -> str:
        """Send an adb keyevent to the device.

        Args:
            keycode(string): Android keycode name or number, e.g. KEYCODE_BACK.
            serial(string): Optional adb serial override.
        """
        result = await self._run_adb(
            self._adb_base_args(serial) + ["shell", "input", "keyevent", keycode]
        )
        return json.dumps(result, ensure_ascii=False)

    @llm_tool(name="adb_shell")
    async def adb_shell(
        self, event: AstrMessageEvent, command: str, serial: str = ""
    ) -> str:
        """Run an adb shell command when explicitly enabled in plugin config.

        Args:
            command(string): Raw shell command.
            serial(string): Optional adb serial override.
        """
        if not self._cfg_bool("allow_adb_shell", False):
            return json.dumps({"error": "adb_shell_disabled"}, ensure_ascii=False)
        safe_parts = self._parse_safe_shell_command(command)
        if safe_parts is None:
            return json.dumps({"error": "unsafe_adb_shell_command"}, ensure_ascii=False)
        result = await self._run_adb(
            self._adb_base_args(serial) + ["shell", *safe_parts]
        )
        return json.dumps(result, ensure_ascii=False)
