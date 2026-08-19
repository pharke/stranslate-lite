# stranslate-lite

从 [STranslate](https://github.com/STranslate/STranslate) 提炼出的最小可用工具：**全局快捷键触发划词 → 按自定义提示词调用 LLM（OpenAI 兼容接口）→ 鼠标附近悬浮窗流式展示回答**。

macOS 优先（解决 STranslate 不支持 Mac 的问题），核心逻辑完全跨平台；Windows 提供实验性适配器（保留原平台习惯）。

## 与 STranslate 的对应关系

| STranslate（Windows 实现） | stranslate-lite |
| --- | --- |
| NHotkey `RegisterHotKey`（Alt+D 划词翻译） | macOS Carbon `RegisterEventHotKey`（系统级、吞掉组合键、无需权限） |
| `ClipboardHelper`：SendInput 模拟 Ctrl+C + `GetClipboardSequenceNumber` 轮询 | Quartz `CGEventPost` Cmd+C + `NSPasteboard.changeCount` 轮询（同构逻辑：记录原文 → 模拟复制 → 轮询 → 延迟 30ms → 变化判定 → 超时回退） |
| `SendCtrlCV` 先释放卡住的修饰键 | `_release_stuck_modifiers`（等价实现） |
| `CapturedTextHandler`（换行处理、`_`/`-` 转空格） | `prompts.postprocess_captured`（同规则） |
| Prompt 模板 `$source`/`$target`/`$content`，多组提示词一组启用 | 同占位符；每组提示词可绑定独立快捷键 |
| `UrlHelper.BuildFinalUrl`（`/`、`/v1` 自动补全、`#` 强制完整地址） | `llm.build_chat_url`（同规则） |
| `OpenAIProtocol`：流式 SSE 解析、`data:` 前缀、`[DONE]`、错误提取、`<think>` 过滤、非流式回退 | `llm.LlmClient`（同规则 + 增强） |
| `Settings.MaxRetries`（原版声明但未消费） | 真实重试：网络错误 / 5xx / 429 / 408 按 `max_retries` × `retry_delay_ms` |
| 主窗口置顶显示 + 替换翻译 | 非激活置顶 `NSPanel` 悬浮窗（不抢焦点、Esc 关闭、文本可选中复制） |
| —（无） | 后台任务可取消：新热键触发即取消上一次调用（socket 级中断） |
| —（无） | 配置热重载：提示词 / API / 取词设置保存即生效（下次触发自动读取） |

## 快速开始（macOS）

```bash
# 1. 准备 Python 3.9+（系统自带，或 brew install python@3.12 / 推荐 uv）
python3 -m venv ~/.venvs/stranslate-lite && source ~/.venvs/stranslate-lite/bin/activate

# 2. 安装（macOS 上会自动安装 pyobjc 桥接依赖）
cd stranslate-lite
pip install .

# 3. 生成配置并编辑（api_key/model/base_url/提示词）
stranslate-lite config --init
# 路径：~/Library/Application Support/stranslate-lite/config.toml

# 4. 校验配置并测试 API 连通性
stranslate-lite check --ping

# 5. 启动（前台运行；常驻建议用下方 launchd 自启）
stranslate-lite run
```

### 辅助功能权限（必须，一次）

模拟 `Cmd+C` 取词受 macOS TCC 保护：

1. 首次运行 `stranslate-lite run` 会提示；或打开 系统设置 → 隐私与安全性 → 辅助功能；
2. 勾选运行本程序的**终端/iTerm/打包后的 App**；
3. **重启本程序**后生效（菜单栏图标「译」→ 检查辅助功能权限可随时跳转）。

> 热键本身（Carbon RegisterEventHotKey）无需任何权限；只有「模拟复制取词」需要辅助功能授权。这与 PopClip、Bob 等同类工具的权限要求一致。

## 配置

TOML 单文件。提示词、API、取词（`[capture]`）设置**保存即生效**：每次触发快捷键时自动重读；快捷键绑定列表本身（`[[hotkeys]]` 的增删改）在启动时注册，**改动后需重启**。完整示例由 `stranslate-lite config --init` 生成：

```toml
[api]
base_url = "https://api.openai.com/v1"   # 兼容 OpenAI/DeepSeek/各类中转；以 # 结尾 = 强制完整地址
api_key = "${OPENAI_API_KEY}"            # 明文或环境变量引用
model = "gpt-4o-mini"                    # 例如 gemini-2.0-flash 对应的服务商模型名
temperature = 0.7
timeout_seconds = 60
max_retries = 3
retry_delay_ms = 1000
source_lang = "Requires you to identify automatically"
target_lang = "Simplified Chinese"
# [api.extra_body]                        # 附加请求体参数（不能覆盖 model/messages/stream）
# top_p = 0.9

[capture]
timeout_ms = 500        # 模拟复制后的剪贴板轮询上限（50~5000）
line_break = "keep"     # keep | remove | space
separators = "none"     # none | underscore | hyphen | both（代码翻译时建议 both）
max_chars = 8000

[prompts."翻译"]        # 提示词名含非 ASCII 时必须加引号（TOML 规范）
name = "翻译"
[[prompts."翻译".messages]]
role = "system"
content = "You are a professional translation engine. Only return the translated text."
[[prompts."翻译".messages]]
role = "user"
content = "Please translate into $target:\n\n$content"

[[hotkeys]]
key = "alt+q"           # 修饰键：ctrl / alt(option) / shift / cmd；按键：字母/数字/F1-F12/方向键等
prompt = "翻译"
# source_lang / target_lang 可选，覆盖全局默认

[[hotkeys]]
key = "alt+w"
prompt = "代码审阅"
```

提示词占位符与 STranslate 一致：`$content`（选中文本）、`$source`、`$target`。

## 命令

```bash
stranslate-lite run                  # 启动后台工具（热键 + 悬浮窗 + 菜单栏图标；Ctrl+C 可退出）
stranslate-lite translate <文本>      # 一次性调用，结果打印 stdout
    --prompt <名称> | --hotkey <序号> | --stdin | --no-stream
stranslate-lite check [--ping|--hotkeys]  # 配置校验 + 权限检查；--ping 测试 API；--hotkeys 检测快捷键占用
stranslate-lite config --init|--path
```

## 开机自启（launchd）

编辑 `~/Library/LaunchAgents/com.user.stranslate-lite.plist`：

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.user.stranslate-lite</string>
  <key>ProgramArguments</key>
  <array>
    <string>/Users/YOU/.venvs/stranslate-lite/bin/stranslate-lite</string>
    <string>run</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
</dict></plist>
```

```bash
launchctl load ~/Library/LaunchAgents/com.user.stranslate-lite.plist
```

> 注意：launchd 环境下授予「辅助功能」权限的对象是**运行该 venv 的 python 可执行文件**（或打包后的 .app），而不是终端。

## 打包 .app（可选）

```bash
pip install pyinstaller
pyinstaller --noconfirm --windowed --name "stranslate-lite" \
  --osx-bundle-identifier "com.user.stranslate-lite" \
  stranslate_lite/cli.py
# dist/stranslate-lite.app → 拖入「应用程序」；辅助功能授权勾选该 .app
```

## 开发与测试

核心模块（配置/提示词/LLM/SSE/编排/取词逻辑）100% 平台无关，可在任意系统测试：

```bash
pip install -e '.[dev]'
pytest            # 52 项：SSE 解析、重试、取消、取词判定、配置热重载、CLI 端到端（模拟服务端）等
```

架构：`stranslate_lite/` 下 `config|prompts|llm|capture|app` 为平台无关核心；`platform/` 为适配层（`macos.py` 完整实现、`windows.py` 实验性、`headless.py` 测试用）。

macOS 上的 GUI 冒烟脚本（需图形会话，验证菜单栏/悬浮窗/线程投递/面板定位）：

```bash
python scripts/smoke_gui.py   # 输出 SMOKE DONE FAILURES=0 即通过
```

## macOS 真机验证状态

已在 **macOS 26.4.1（arm64）+ Python 3.12 + pyobjc 12.2.2** 上完成真机验证，并修复了 WSL 开发环境无法暴露的问题：

| 验证项 | 结果 |
| --- | --- |
| `check` 配置校验、`check --ping` 连通 | ✓ |
| `check --hotkeys`（Carbon RegisterEventHotKey 注册） | ✓ 两个快捷键 OSStatus=0 |
| 菜单栏「译」状态项与菜单 | ✓（GUI 冒烟） |
| 悬浮面板创建/流式更新/关闭/屏幕内定位 | ✓（GUI 冒烟） |
| 后台线程 → 主线程投递（callAfter）与主线程直接执行 | ✓（GUI 冒烟） |
| `run` 启动、Ctrl+C 优雅退出 | ✓（exit code 0） |
| 修复：64 位 Carbon 无 `NewEventHandlerUPP` 符号（原实现 macOS 上直接崩溃） | ✓ |
| 修复：主线程 callAfter 不被运行循环处理（面板不更新/无法退出） | ✓ |
| 修复：Esc 监视器漏传 NSKeyDownMask（Esc 无法关窗） | ✓ |
| 修复：`enter`/`esc` 键名在 macOS/Windows 键码表缺失 | ✓ |
| 修复：pyobjc 12 下 NSObject 子类构造方式（菜单栏动作） | ✓ |

仍需人工确认（需要真实键鼠与授权，无法自动化）：授予「辅助功能」权限后按 `Alt+Q` 取词 → 悬浮窗流式展示；无选中文本时的取词失败提示；悬浮窗不抢焦点；Esc 关窗；菜单栏各项交互。

> 若热键注册失败（`run` 启动报错或 `check --hotkeys` 显示 ✗），通常是被其他应用占用，换一个组合键即可。

## 已知限制

- **Windows 适配器**为实验性加分项（未真机验证），Windows 上仍可继续使用原版 STranslate。
- 尚未实现（后续路线）：结果自动复制、替换回选区、多 API 配置、悬浮窗深色模式、Tray 交互选提示词。
- 快捷键绑定（`[[hotkeys]]`）的增删改需要重启生效（提示词/API/取词配置热重载）。
