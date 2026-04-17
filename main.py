from __future__ import annotations

import asyncio
import base64
import json
import re
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

import ast
from astrbot.api import AstrBotConfig, llm_tool, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import ProviderRequest, LLMResponse
from astrbot.api.star import Context, Star
from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path
from websockets.asyncio.server import ServerConnection, serve


PLUGIN_NAME = "astrbot_plugin_phone_mcp"

# Characters that must be backslash-escaped for `adb shell input text`.
_ADB_TEXT_ESCAPE_CHARS = set("$`\"\\()&|;<>#!~{}[]'")

_MAX_SCREENSHOTS = 50

# Chinese / common app name → Android package name mapping.
# Used by the Launch/AppLaunch action handler so the model can say
# do(action="Launch", app="美团") without knowing the package name.
_COMMON_APP_PACKAGES: dict[str, str] = {
    "美团": "com.sankuai.meituan",
    "美团外卖": "com.sankuai.meituan.takeoutnew",
    "微信": "com.tencent.mm",
    "支付宝": "com.eg.android.AlipayGphone",
    "淘宝": "com.taobao.taobao",
    "抖音": "com.ss.android.ugc.aweme",
    "设置": "com.android.settings",
    "系统设置": "com.android.settings",
    "拼多多": "com.xunmeng.pinduoduo",
    "京东": "com.jingdong.app.mall",
    "QQ": "com.tencent.mobileqq",
    "qq": "com.tencent.mobileqq",
    "高德地图": "com.autonavi.minimap",
    "高德": "com.autonavi.minimap",
    "百度地图": "com.baidu.BaiduMap",
    "小红书": "com.xingin.xhs",
    "哔哩哔哩": "tv.danmaku.bili",
    "B站": "tv.danmaku.bili",
    "b站": "tv.danmaku.bili",
    "bilibili": "tv.danmaku.bili",
    "网易云音乐": "com.netease.cloudmusic",
    "网易云": "com.netease.cloudmusic",
    "饿了么": "me.ele",
    "闲鱼": "com.taobao.idlefish",
    "钉钉": "com.alibaba.android.rimet",
    "飞书": "com.ss.android.lark",
    "知乎": "com.zhihu.android",
    "豆瓣": "com.douban.frodo",
    "大众点评": "com.dianping.v1",
    "携程": "ctrip.android.view",
    "滴滴": "com.sdu.didi.psnger",
    "WPS": "cn.wps.moffice_eng",
    "相机": "com.android.camera",
    "Chrome": "com.android.chrome",
    "chrome": "com.android.chrome",
    "浏览器": "com.android.browser",
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
        deadline = asyncio.get_event_loop().time() + timeout
        async with self._condition:
            while True:
                latest = self.latest()
                if latest and latest.received_at_ms > after_received_ms:
                    return latest
                remaining = deadline - asyncio.get_event_loop().time()
                if remaining <= 0:
                    return None
                try:
                    await asyncio.wait_for(
                        self._condition.wait(), timeout=remaining
                    )
                except TimeoutError:
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

        prompt_file = Path(__file__).parent / "prompts" / "phone_agent_zh.md"
        if prompt_file.exists():
            self._system_prompt = prompt_file.read_text("utf-8")
        else:
            self._system_prompt = ""

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
            frame_text = json.dumps(payload, ensure_ascii=False, indent=2)
            await asyncio.to_thread(
                self.latest_frame_path.write_text, frame_text, "utf-8"
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

    def _search_nodes_scored(
        self, query: str, limit: int = 10
    ) -> list[dict[str, Any]]:
        """Search nodes with multi-tier scoring for VL model tap/locate.

        Scoring tiers:
        - Exact text/desc match: +20
        - Substring in text/desc: +10
        - Match in resource_id: +6
        - Match in semantic_hint: +6
        - Match in class_name: +3
        - Clickable bonus: +5
        - Visible bonus: +3
        - Shallow depth bonus: +2 * (1 / (depth+1))
        - Smaller area preferred (more precise target): +1 if area < median
        """
        latest = self._latest_frame()
        if latest is None:
            return []
        ui_state = latest.ui_state
        data = ui_state.get("data", {}) if isinstance(ui_state, dict) else {}
        elements = data.get("elements", []) if isinstance(data, dict) else []
        if not isinstance(elements, list) or not elements:
            return []

        lowered = query.strip().lower()
        if not lowered:
            return []

        scored: list[tuple[float, dict[str, Any]]] = []
        for element in elements:
            if not isinstance(element, dict):
                continue

            text = str(element.get("text", "")).lower()
            desc = str(element.get("desc", "")).lower()
            resource_id = str(element.get("resource_id", "")).lower()
            semantic_hint = str(element.get("semantic_hint", "")).lower()
            class_name = str(element.get("class_name", "")).lower()

            score = 0.0

            # Tier 1: exact match on text or desc
            if text == lowered or desc == lowered:
                score += 20.0
            # Tier 2: substring match on text or desc
            elif lowered in text or lowered in desc:
                score += 10.0
            # Tier 3: match in resource_id or semantic_hint
            elif lowered in resource_id or lowered in semantic_hint:
                score += 6.0
            # Tier 4: match in class_name
            elif lowered in class_name:
                score += 3.0
            else:
                continue  # No match at all

            # Bonuses
            is_clickable = element.get("is_clickable", False)
            if is_clickable:
                score += 5.0
            if element.get("is_visible_to_user", True):
                score += 3.0
            depth = element.get("depth", 0)
            if isinstance(depth, (int, float)) and depth >= 0:
                score += 2.0 / (depth + 1)

            # Prefer nodes with tap_point already computed
            if element.get("tap_point"):
                score += 1.0

            result = dict(element)
            result["_match_score"] = round(score, 2)
            scored.append((score, result))

        scored.sort(key=lambda pair: pair[0], reverse=True)
        return [item for _, item in scored[:limit]]

    def _get_current_package(self) -> str:
        """Get the package name from the latest frame."""
        latest = self._latest_frame()
        if latest is None:
            return ""
        ui_state = latest.ui_state
        data = ui_state.get("data", {}) if isinstance(ui_state, dict) else {}
        return data.get("package_name", "")

    def _extract_tap_point(self, element: dict[str, Any]) -> tuple[int, int] | None:
        """Extract tap coordinates from an element, computing from bounds if needed."""
        tap_point = element.get("tap_point")
        if isinstance(tap_point, list) and len(tap_point) == 2:
            return int(tap_point[0]), int(tap_point[1])

        # Fallback: compute from bounds string "[left,top][right,bottom]"
        bounds_str = str(element.get("bounds", ""))
        parts = bounds_str.replace("[", "").replace("]", ",").split(",")
        nums = [p.strip() for p in parts if p.strip()]
        if len(nums) >= 4:
            try:
                left, top, right, bottom = int(nums[0]), int(nums[1]), int(nums[2]), int(nums[3])
                return (left + right) // 2, (top + bottom) // 2
            except (ValueError, IndexError):
                pass
        return None

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

            # Compress high-res screenshots to prevent local LLM visual token OOM / 502 Timeout
            try:
                from PIL import Image
                import io
                image = Image.open(io.BytesIO(stdout))
                max_size = 512
                if max(image.width, image.height) > max_size:
                    # Resize keeping aspect ratio
                    ratio = max_size / max(image.width, image.height)
                    new_size = (int(image.width * ratio), int(image.height * ratio))
                    image = image.resize(new_size, Image.Resampling.LANCZOS)
                    out_buffer = io.BytesIO()
                    image.save(out_buffer, format="PNG")
                    stdout = out_buffer.getvalue()
                    result["size_bytes"] = len(stdout)
                    logger.info(f"Screencap compressed to {new_size[0]}x{new_size[1]} ({result['size_bytes']} bytes)")
            except Exception as e:
                logger.warning(f"Failed to compress screenshot, using original: {e}")

            if self._cfg_bool("persist_screenshots", True):
                file_name = f"{int(time.time() * 1000)}_{uuid4().hex[:8]}.png"
                file_path = self.screenshot_dir / file_name
                await asyncio.to_thread(file_path.write_bytes, stdout)
                result["file_path"] = str(file_path)
                # Rotate old screenshots to prevent unbounded disk usage

                await self._rotate_screenshots()
            return result
        except Exception as exc:
            logger.error("phone_mcp screencap failed: %s", exc)
            return {
                "command": args,
                "returncode": -1,
                "stderr": str(exc),
                "size_bytes": 0,
            }

    async def _rotate_screenshots(self) -> None:
        def _do_rotate() -> None:
            files = sorted(
                self.screenshot_dir.glob("*.png"),
                key=lambda p: p.stat().st_mtime,
            )
            excess = len(files) - _MAX_SCREENSHOTS
            for old_file in files[:excess]:
                old_file.unlink(missing_ok=True)

        try:
            await asyncio.to_thread(_do_rotate)
        except Exception as exc:
            logger.warning("phone_mcp screenshot rotation failed: %s", exc)

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

    def _parse_shell_command(self, command: str) -> list[str] | None:
        """Normalize and wrap a shell command string for adb exec."""
        normalized = command.strip()
        if not normalized:
            return None
        # Pass the entire shell command as a single argument so adb handles pipes `|`, `>`, etc. correctly.
        return [normalized]

    # Model names (case-insensitive substrings) that need auto-screenshot injection
    # because they tend to hallucinate screen content without visual grounding.
    _VISION_WEAK_MODEL_KEYWORDS = {"autoglm"}

    def _is_vision_weak_model(self, request: ProviderRequest) -> bool:
        """Check if the current model is known to hallucinate without visual input."""
        model_name = (request.model or "").lower()
        return any(kw in model_name for kw in self._VISION_WEAK_MODEL_KEYWORDS)

    @filter.on_llm_request()
    async def inject_phone_agent_prompt(self, event: AstrMessageEvent, request: ProviderRequest):
        """Inject customized phone automation strategies into to the LLM's system prompt."""
        if self._system_prompt:
            request.system_prompt += f"\n\n{self._system_prompt}\n"

        # Always inject lightweight frame metadata so the model has ground truth
        # about what app is currently in foreground (prevents hallucination).
        latest = self._latest_frame()
        if latest is not None:
            summary = self._frame_summary(latest)
            frame_info = (
                f"\n[PHONE STATE]\n"
                f"当前前台应用: {summary.get('package_name', 'unknown')}\n"
                f"Activity: {summary.get('activity_name', 'unknown')}\n"
                f"UI元素数量: {summary.get('element_count', 0)}\n"
                f"可点击元素: {summary.get('actionable_count', 0)}\n"
                f"含文本元素: {summary.get('text_count', 0)}\n"
            )
            request.system_prompt += frame_info

        # All image injection below is gated on model_supports_vision.
        # Text-only models (e.g. AutoGLM on llama.cpp without mmproj) will
        # crash with 500 if we send image_urls, so we must skip entirely.
        vision_enabled = self._cfg_bool("model_supports_vision", False)

        # Check if there's already a screenshot attached from a previous tool call
        has_screenshot = False
        if vision_enabled and request.contexts:
            # Look backwards in contexts to find the most recent screenshot
            for ctx in reversed(request.contexts):
                if ctx.get("role") == "tool" and "file_path" in str(ctx.get("content", "")):
                    try:
                        data = json.loads(ctx["content"])
                        if "file_path" in data:
                            # Attach the image to the current provider request
                            request.image_urls.append(f"file:///{data['file_path']}")
                            request.system_prompt += "\n\n[SYSTEM NOTIFICATION]\nThe requested screenshot image is attached to this turn. Please look at it to perceive the UI and continue your task.\n"
                            has_screenshot = True
                            break # Only attach the most recent screenshot
                    except Exception:
                        pass

        # Auto-capture screenshot for vision-weak models (e.g. AutoGLM) that
        # tend to hallucinate screen content when no visual input is provided.
        # Only works when model_supports_vision is true.
        if vision_enabled and not has_screenshot and self._is_vision_weak_model(request):
            try:
                result = await self._capture_screencap(None)
                if result.get("file_path"):
                    request.image_urls.append(f"file:///{result['file_path']}")
                    request.system_prompt += (
                        "\n\n[SYSTEM NOTIFICATION]\n"
                        "当前手机屏幕截图已自动附上。请仔细观察截图内容，"
                        "基于你真实看到的画面决定下一步操作。不要想象或猜测屏幕内容。\n"
                    )
                    logger.info("phone_mcp auto-screenshot injected for vision-weak model: %s", request.model)
            except Exception as e:
                logger.warning("phone_mcp auto-screenshot failed: %s", e)

    def _parse_do_command(self, cmd_str: str) -> dict[str, Any]:
        """Safely parse do(action="...", ...) string into a kwargs dictionary."""
        try:
            dummy_code = f"dummy({cmd_str})"
            tree = ast.parse(dummy_code)
            call_node = tree.body[0].value
            kwargs = {}
            for kw in call_node.keywords:
                if isinstance(kw.value, ast.Constant):
                    kwargs[kw.arg] = kw.value.value
                elif getattr(ast, "List", None) and getattr(kw.value, "elts", None):
                    kwargs[kw.arg] = [elt.value for elt in kw.value.elts if isinstance(elt, ast.Constant)]
            return kwargs
        except Exception as e:
            logger.warning("phone_mcp ast parser error for '%s': %s", cmd_str, e)
            return {}

    @filter.on_llm_response()
    async def intercept_action_response(self, event: AstrMessageEvent, resp: LLMResponse):
        """Intercept do(...) and inject into AstBot's ToolRunner."""
        text = resp.completion_text
        if not text:
            return

        # Make <answer> optional and use non-greedy match for do(...)
        match = re.search(r"do\((.*?)\)", text, re.DOTALL | re.IGNORECASE)
        if not match:
            # Debug: log when model output contains no do() command at all
            preview = text[:200].replace('\n', ' ')
            logger.warning("phone_mcp no do() command found in model output: %s...", preview)
            return

        cmd_str = match.group(1).strip()
        # Replace full-width quotes that models often hallucinate
        cmd_str = cmd_str.replace('“', '"').replace('”', '"').replace('‘', "'").replace('’', "'")
        
        kwargs = self._parse_do_command(cmd_str)
        action = kwargs.pop("action", None)
        if not action:
            logger.warning("phone_mcp intercepted action missing 'action' inside do().")
            return
            
        tool_name = ""
        tool_args = {}

        if action == "Tap":
            tool_name = "adb_tap"
            pt = kwargs.get("element", [0, 0])
            tool_args = {"x": pt[0], "y": pt[1]}
        elif action == "Swipe":
            tool_name = "adb_swipe"
            from_pt = kwargs.get("from_pt", [0, 0])
            to_pt = kwargs.get("to_pt", [0, 0])
            tool_args = {"x1": from_pt[0], "y1": from_pt[1], "x2": to_pt[0], "y2": to_pt[1]}
        elif action == "Input":
            tool_name = "adb_input_text"
            tool_args = {"text": str(kwargs.get("text", ""))}
        elif action == "Key":
            tool_name = "adb_keyevent"
            tool_args = {"keycode": str(kwargs.get("code", ""))}
        elif action == "VisionTap":
            tool_name = "phone_vision_tap"
            tool_args = {"query": str(kwargs.get("query", ""))}
        elif action == "VisionLocate":
            tool_name = "phone_vision_locate"
            tool_args = {"query": str(kwargs.get("query", ""))}
        elif action == "FindNodes":
            tool_name = "phone_find_nodes"
            tool_args = {"query": str(kwargs.get("query", ""))}
        elif action == "Perceive":
            tool_name = "phone_vision_describe"
        elif action in ["AppLaunch", "Launch", "Open", "OpenApp"]:
            tool_name = "adb_shell"
            if "command" in kwargs:
                tool_args = {"command": str(kwargs.get("command", ""))}
            else:
                app_name = str(kwargs.get("app", kwargs.get("name", "")))
                package = _COMMON_APP_PACKAGES.get(app_name, "")
                if not package:
                    # Fuzzy: try substring match
                    for cn_name, pkg in _COMMON_APP_PACKAGES.items():
                        if cn_name in app_name or app_name in cn_name:
                            package = pkg
                            break
                if not package:
                    # If it looks like a package name already (has dots), use directly
                    if "." in app_name:
                        package = app_name
                    else:
                        logger.warning("phone_mcp unknown app name '%s', trying as package", app_name)
                        package = app_name
                if package == "com.android.settings":
                    tool_args = {"command": "am start -a android.settings.SETTINGS"}
                else:
                    tool_args = {"command": f"monkey -p {package} -c android.intent.category.LAUNCHER 1"}
                logger.info("phone_mcp Launch resolved: '%s' -> package='%s'", app_name, package)
        elif action == "Wait":
            tool_name = "phone_wait_next_frame"
            duration = kwargs.get("duration", None)
            if duration is not None:
                tool_args = {"timeout_sec": min(float(duration), 15.0)}
        elif action == "Finish":
            status = kwargs.get("status", "unknown")
            reason = kwargs.get("reason", "")
            logger.info(f"Task finished by agent. Status: {status}, Reason: {reason}")
            return
        else:
            logger.warning("phone_mcp intercepted unknown action: %s", action)
            return

        logger.info("phone_mcp ACT Parser intercepted: %s -> %s %s", action, tool_name, tool_args)
        resp.tools_call_name.append(tool_name)
        resp.tools_call_args.append(tool_args)
        resp.tools_call_ids.append(f"call_ast_{uuid4().hex[:8]}")

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
        self, event: AstrMessageEvent, summary_only: bool = False, vision_mode: bool = False
    ) -> str:
        """Read the latest phone UI frame as structured data.

        For vision-assisted apps, consider using phone_vision_describe instead —
        it attaches a screenshot for visual understanding and returns a compact summary
        without the full element tree.

        Args:
            summary_only(boolean): When true, return frame summary instead of full JSON.
            vision_mode(boolean): When true, return compact summary without elements array (UI tree kept locally for coordinate lookup via phone_vision_tap).
        """
        latest = self._latest_frame()
        if latest is None:
            return json.dumps({"error": "no_frame"}, ensure_ascii=False)
        if vision_mode or summary_only:
            payload: Any = self._frame_summary(latest)
            if vision_mode:
                payload["hint"] = "UI tree kept locally. Use do(action=\"VisionTap\", query=\"...\") to tap elements by text, or do(action=\"Perceive\") to see the screen."
            return json.dumps(payload, ensure_ascii=False)
        return json.dumps(latest.payload, ensure_ascii=False)

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

    @llm_tool(name="phone_vision_tap")
    async def phone_vision_tap(
        self,
        event: AstrMessageEvent,
        query: str,
        serial: str = "",
    ) -> str:
        """VL model action: search the local UI tree for an element matching
        the query text, then tap it. Use this after visually understanding the
        screen via a screenshot.

        The query is matched against element text, description, resource ID, and
        semantic hints using multi-tier scoring. The best matching clickable
        element is tapped automatically.

        Args:
            query(string): Target element text, label, or description (e.g. "美团外卖", "搜索", "购物车").
            serial(string): Optional adb serial override.
        """
        matches = []
        max_scrolls = 3
        for i in range(max_scrolls):
            matches = self._search_nodes_scored(query, limit=5)
            if matches:
                break
            
            if i < max_scrolls - 1:
                # Auto-swipe: swipe from bottom to top (scroll down)
                latest = self._latest_frame()
                after_ms = latest.received_at_ms if latest else 0
                await self._run_adb(
                    self._adb_base_args(serial) + ["shell", "input", "swipe", "500", "1500", "500", "500"]
                )
                # Wait for new UI frame from websocket instead of blind sleep
                await self.frame_store.wait_next_frame(after_ms, timeout=4.0)

        if not matches:
            return json.dumps(
                {
                    "result": "no_match",
                    "query": query,
                    "hint": "No element found matching the query. Try FindNodes with a broader keyword, or use Tap with estimated coordinates.",
                },
                ensure_ascii=False,
            )

        # Pick the best match
        best = matches[0]
        tap = self._extract_tap_point(best)
        if tap is None:
            return json.dumps(
                {
                    "result": "no_coordinates",
                    "query": query,
                    "matched_text": best.get("text", ""),
                    "matched_id": best.get("id", ""),
                    "hint": "Found a matching element but could not extract tap coordinates. Use Tap with estimated coordinates.",
                },
                ensure_ascii=False,
            )

        x, y = tap
        tap_result = await self._run_adb(
            self._adb_base_args(serial) + ["shell", "input", "tap", str(x), str(y)]
        )

        return json.dumps(
            {
                "result": "tapped",
                "query": query,
                "matched_text": best.get("text", ""),
                "matched_id": best.get("id", ""),
                "match_score": best.get("_match_score", 0),
                "tap_point": [x, y],
                "adb": tap_result,
                "alternatives": [
                    {
                        "text": m.get("text", ""),
                        "id": m.get("id", ""),
                        "score": m.get("_match_score", 0),
                        "tap_point": self._extract_tap_point(m),
                    }
                    for m in matches[1:3]
                ],
            },
            ensure_ascii=False,
        )

    @llm_tool(name="phone_vision_locate")
    async def phone_vision_locate(
        self, event: AstrMessageEvent, query: str, limit: int = 5, serial: str = ""
    ) -> str:
        """Search the local UI tree for elements matching the query and return
        their coordinates without tapping. Use this to verify element positions
        before acting.

        Args:
            query(string): Target element text, label, or description.
            limit(number): Max number of candidates to return.
            serial(string): Optional adb serial override.
        """
        matches = []
        max_scrolls = 3
        for i in range(max_scrolls):
            matches = self._search_nodes_scored(query, limit=max(1, min(limit, 10)))
            if matches:
                break
                
            if i < max_scrolls - 1:
                # Auto-swipe: swipe from bottom to top (scroll down)
                latest = self._latest_frame()
                after_ms = latest.received_at_ms if latest else 0
                await self._run_adb(
                    self._adb_base_args(serial) + ["shell", "input", "swipe", "500", "1500", "500", "500"]
                )
                await self.frame_store.wait_next_frame(after_ms, timeout=4.0)

        if not matches:
            return json.dumps({
                    "result": "no_match", 
                    "query": query, 
                    "candidates": [],
                    "hint": "No element found matching the query. Use Tap with estimated coordinates.",
                },
                ensure_ascii=False,
            )

        candidates = []
        for m in matches:
            tap = self._extract_tap_point(m)
            candidates.append(
                {
                    "text": m.get("text", ""),
                    "id": m.get("id", ""),
                    "bounds": m.get("bounds", ""),
                    "tap_point": list(tap) if tap else None,
                    "match_score": m.get("_match_score", 0),
                    "is_clickable": m.get("is_clickable", False),
                    "semantic_hint": m.get("semantic_hint", ""),
                }
            )

        return json.dumps(
            {"result": "found", "query": query, "candidates": candidates},
            ensure_ascii=False,
        )

    @llm_tool(name="phone_vision_describe")
    async def phone_vision_describe(
        self, event: AstrMessageEvent, serial: str = ""
    ) -> str:
        """VL model perception: capture a screenshot (auto-attached to your
        visual context) and return a compact frame summary. Use this as your
        primary "eyes" for vision-assisted apps instead of reading the full UI tree.

        After calling this, look at the attached screenshot to understand the screen,
        then use do(action="VisionTap", query="...") to act on what you see.

        Args:
            serial(string): Optional adb serial override.
        """
        # Capture screenshot (will be auto-attached by inject_phone_agent_prompt)
        screenshot_result = await self._capture_screencap(serial)

        # Build compact summary from latest frame
        latest = self._latest_frame()
        summary: dict[str, Any] = {}
        if latest is not None:
            frame_summary = self._frame_summary(latest)
            summary = {
                "package_name": frame_summary.get("package_name", ""),
                "activity_name": frame_summary.get("activity_name", ""),
                "element_count": frame_summary.get("element_count", 0),
                "actionable_count": frame_summary.get("actionable_count", 0),
                "text_count": frame_summary.get("text_count", 0),
            }
            # Check for overlay
            ui_state = latest.ui_state
            data = ui_state.get("data", {}) if isinstance(ui_state, dict) else {}
            if data.get("overlay_detected"):
                summary["overlay_detected"] = True
                summary["overlay_close_node_id"] = data.get("overlay_close_node_id", "")
        else:
            summary = {"warning": "no_frame_available"}

        return json.dumps(
            {
                "screenshot": screenshot_result,
                "frame_summary": summary,
                "hint": "Screenshot attached. Look at the image to understand the screen. Use do(action=\"VisionTap\", query=\"...\") to tap elements by their visible text or description.",
            },
            ensure_ascii=False,
        )

    @llm_tool(name="phone_capture_screenshot")
    async def phone_capture_screenshot(
        self, event: AstrMessageEvent, serial: str = ""
    ) -> str:
        """Capture a full screenshot from the Android device. If you have visual capabilities, call this tool to 'see' the screen directly in your next response hook.

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

        Uses ADB Keyboard broadcast (base64) for reliable Unicode input
        when available, falls back to 'adb shell input text' otherwise.

        Args:
            text(string): Text to input.
            serial(string): Optional adb serial override.
        """
        # Try ADB Keyboard broadcast first — handles Chinese, emoji, etc.
        b64 = base64.b64encode(text.encode("utf-8")).decode("ascii")
        result = await self._run_adb(
            self._adb_base_args(serial)
            + ["shell", "am", "broadcast", "-a", "ADB_INPUT_B64", "--es", "msg", b64]
        )
        stdout = result.get("stdout", "")
        if "result=0" in stdout or "result=-1" in stdout:
            # ADB Keyboard not installed or not active — fall back
            escaped = "".join(
                "%s" if c == " " else f"\\{c}" if c in _ADB_TEXT_ESCAPE_CHARS else c
                for c in text
            )
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
        safe_parts = self._parse_shell_command(command)
        if safe_parts is None:
            return json.dumps({"error": "empty_adb_shell_command"}, ensure_ascii=False)
        result = await self._run_adb(
            self._adb_base_args(serial) + ["shell", *safe_parts]
        )
        return json.dumps(result, ensure_ascii=False)
