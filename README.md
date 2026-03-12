# astrbot_plugin_phone_mcp

AstrBot plugin that receives Android UI frames over WebSocket and exposes phone data plus ADB control tools to agents.

## What it does

- Starts a WebSocket server for `AmadeusClient`
- Stores recent `ui_frame_full` payloads as structured data for tools to read on demand
- Exposes ADB tools for tap, swipe, input, keyevent, and optional shell access

## Dependency management

This plugin supports both dependency installation styles:

- `pip`: install from `requirements.txt`
- `uv`: install from `pyproject.toml` / `uv sync`

Both files are kept aligned and currently require:

- `websockets>=16.0`

## Why this structure

The phone UI frame is not injected directly into the prompt. Agents should fetch the latest frame or search nodes through tools when needed, which keeps prompt context cleaner and more stable.

## Main tools

- `phone_ws_status`
- `phone_get_latest_frame`
- `phone_get_recent_frames`
- `phone_request_frame`
- `phone_wait_next_frame`
- `phone_find_nodes`
- `phone_capture_screenshot`
- `adb_list_devices`
- `adb_tap`
- `adb_swipe`
- `adb_input_text`
- `adb_keyevent`
- `adb_shell` (disabled by default)
