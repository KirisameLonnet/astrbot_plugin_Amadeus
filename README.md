# astrbot_plugin_phone_mcp

`astrbot_plugin_phone_mcp` 是面向 AstrBot Agent 的手机控制桥接插件。

它负责：

- 接收 `AmadeusClient` 通过 WebSocket 推送的结构化页面帧
- 将页面帧作为可按需取阅的数据提供给 Agent，而不是直接塞进 prompt
- 暴露 ADB 工具，让 Agent 能执行点击、滑动、输入、按键和截图

## 关联仓库

- Android 客户端：`AmadeusClient`
  - 仓库地址：`https://github.com/KirisameLonnet/AmadeusClient`
- 当前插件：`astrbot_plugin_Amadeus`
  - 仓库地址：`https://github.com/KirisameLonnet/astrbot_plugin_Amadeus`

两个仓库配合关系如下：

1. `AmadeusClient` 运行在安卓手机上，负责无障碍树、截图、OCR、结构化页面帧生成与 WebSocket 推送。
2. `astrbot_plugin_Amadeus` 运行在 AstrBot 侧，负责接收帧、提供 Agent 可调用工具，以及通过 ADB 回写手机动作。

## 当前能力

当前版本已实现：

1. 启动本地 WebSocket 服务端，接收手机端 `ui_frame_full`。
2. 保存最近若干帧到内存，并可选择持久化最新帧到插件数据目录。
3. 提供页面帧读取工具：
   - `phone_ws_status`
   - `phone_get_latest_frame`
   - `phone_get_recent_frames`
   - `phone_request_frame`
   - `phone_wait_next_frame`
   - `phone_find_nodes`
4. 提供截图工具：
   - `phone_capture_screenshot`
5. 提供 ADB 控制工具：
   - `adb_list_devices`
   - `adb_tap`
   - `adb_swipe`
   - `adb_input_text`
   - `adb_keyevent`
   - `adb_shell`（默认关闭）

## 设计原则

本插件最重要的原则是：

- 手机页面帧是“可取阅数据”
- Agent 需要时再调用工具读取
- 不把整帧 JSON 强行塞进 prompt

这样做的好处是：

1. 减少 prompt 冗余和上下文污染。
2. 让 Agent 可以按需读取“最新帧”“最近几帧”“搜索到的节点”。
3. 后续更容易接入 MCP / Tool Loop / Multi-Agent 流程。

## 依赖管理

本插件同时兼容两种依赖管理方式：

- `pip`：使用 `requirements.txt`
- `uv`：使用 `pyproject.toml` / `uv sync`

当前两边依赖已保持一致：

- `websockets>=16.0`

## 安装方式

### pip

```bash
pip install -r requirements.txt
```

### uv

```bash
uv sync
```

## 后续方向

1. 增加基于 `node_id` 的高层动作工具，例如 `tap_node`。
2. 让 Agent 能更稳定地请求手机强制抓帧与截图。
3. 继续与 `AmadeusClient` 对齐页面帧协议与动作回执协议。
