# STranslate「划词 + LLM」链路审阅 · MVP 提炼与跨平台可行性论证

> 审阅对象：https://github.com/STranslate/STranslate（shallow clone 于 `repo/`，含 `src/docs/` 官方模块文档）
> 关注场景：全局快捷键触发 → 读取划词选中文本 → 按自定义提示词调用 LLM API → 解析响应中的回答 → 展示/回填。

## 1. 需求还原（用户自述）

- Windows 高频使用，核心用法：`Alt+Q` 触发「划词翻译」，选中文本 → 调用自配置 LLM API（flash 模型为主）→ 展示回答。
- 另有绑定「特殊提示词」的快捷键做代码审阅/解释。
- 痛点：仅支持 Windows（WPF）；功能面过宽，真正需要的只是「快捷键 + 划词 + LLM + 提示词 + 解析回答」。

## 2. 关键链路审阅（源码 + 官方文档交叉验证）

### 2.1 触发与取词（Windows 实现）

| 环节 | 实现 | 关键代码 |
| --- | --- | --- |
| 全局热键注册 | NHotkey（Win32 `RegisterHotKey`），冲突检测、全屏跳过、禁用开关 | `Core/HotkeySettings.cs`（`HandleGlobalLogic` 映射中心）、`Helpers/HotkeyMapper.cs::SetHotkey` |
| 划词命令 | 热键 → `MainWindowViewModel.CrosswordTranslateAsync()` | `ViewModels/MainWindowViewModel.cs:1819` |
| 取词 | `ClipboardHelper.GetSelectedTextAsync(timeout)`：记录原剪贴板文本 + `GetClipboardSequenceNumber()` → `SendInput` 模拟 Ctrl+C（先释放所有卡住的修饰键）→ 10ms 轮询序列号变化（默认 500ms，可配 50–5000ms）→ 变化后再等 30ms 读文本并 `Trim()` | `Helpers/ClipboardHelper.cs:46-104` |
| 取词失败回退 | 无文本 → `HandleCrosswordFetchFailed()`：回退输入翻译或仅显示窗口 + Snackbar | `MainWindowViewModel.cs:2844` |
| 文本后处理 | 换行处理（保留/去空格/合并）+ 可选把标识符内 `_`/`-` 替换为空格（利于代码翻译） | `Core/Utilities.cs::CapturedTextHandler`、`MainWindowViewModel.cs::HandleCapturedText` |
| 并发/防抖 | `DebounceExecutor` 防抖、`TranslationOperation` 快照 + 操作序号拒绝旧结果覆盖 | `docs/flow-main-translation.md` |

### 2.2 LLM 调用链（与 UI 无关的核心，可直接移植）

以 `Plugins/STranslate.Plugin.Translate.OpenAI` 为范本（`BigModel` 同构）：

1. **提示词模型**：`Prompt{Name, IsEnabled, Items: PromptItem{role, content}[]}`，一组 prompt 中仅一个 `IsEnabled`；持久化到服务 Settings（JSON）。
   - 占位符替换：`$source`、`$target`、`$content`（逐项 `Replace`）。见 `BigModel/Main.cs:139-148`、`OpenAI/Main.cs:140-153`。
   - 默认自带三组：翻译 / 润色 / 总结（system 约束「只返回译文，不解释」）。
2. **语言映射**：`LangEnum → "Simplified Chinese"` 等自然语言字符串；Auto = "Requires you to identify automatically"。
3. **请求构造**（`OpenAIProtocol.CreateRequest`）：
   - chat/completions：`{model, messages, temperature(clamp), stream: true}`；responses 模式用 `input` 字段。
   - 支持 `AdditionalParametersJson` 附加参数（不能覆盖内置字段）。
4. **URL 拼接**（`UrlHelper.BuildFinalUrl`）：base URL + `/v1/chat/completions`；路径是 `/`、`/v1` 时自动补全；URL 以 `#` 结尾表示「强制使用该地址」。→ 兼容 OpenAI/DeepSeek/各类中转。
5. **认证**：`Authorization: Bearer <key>`。
6. **流式接收**（`HttpService.StreamPostAsyncEnumerable`）：`ResponseHeadersRead` → 逐行 `ReadLineAsync`，跳过空行，逐行回调。零缓冲、低首字延迟。
7. **SSE 解析**（`OpenAIProtocol.ParseStreamLine`）：剥 `data:` 前缀 → 跳过 `[DONE]` → JSON 解析 → `choices[0].delta.content`（responses 模式为 `delta`，配合 `type == "response.output_text.delta"`）→ 提取 `error.message` 作为错误。
8. **推理模型适配**：跳过 `<think>…</think>` 块与流首的前导空白。
9. **结果模型**：`TranslateResult{Text, IsSuccess, Duration}`；流式期间逐帧更新 `Text`。
10. **已知缺口**：`MaxRetries/RetryDelayMilliseconds` 字段存在但执行路径未消费（潜在可改进点）。

### 2.3 展示与回填

- 划词翻译：结果显示在置顶主窗口；`ReplaceTranslateAsync`（替换翻译）另走「取词 → 翻译 → `InputHelper.PrintText` 回写选区」路径，失败时恢复光标状态（`MainWindowViewModel.cs:1898`）。
- 自动复制、历史缓存（SQLite）、词典、TTS、OCR 等均为外围功能，与本需求无关。

## 3. Windows 专属点与 macOS 对应物

| STranslate（Windows） | macOS 等价实现 | 权限/风险 |
| --- | --- | --- |
| NHotkey `RegisterHotKey` | Carbon `RegisterEventHotKey`（pyobjc / Swift） | 无需权限；需跑在 runloop 里 |
| SendInput 模拟 Ctrl+C | `CGEventCreateKeyboardEvent` + `CGEventPost`（Cmd+C） | 需「辅助功能」授权（TCC），一次性系统设置 |
| 剪贴板 + 序列号轮询 | `NSPasteboard.changeCount` 轮询（同构思路） | 无需权限 |
| `SetForegroundWindow`/置顶窗口 | `NSPanel`（floating 层级）+ `NSApp.activateIgnoringOtherApps` | 无需权限 |
| WPF 主窗口 | 轻量悬浮窗 / 菜单栏 app + 通知 | 实现量可控 |
| SendInput 回写替换 | CGEvent 粘贴 | 同上，TCC |

## 4. MVP 范围

**保留（核心闭环）**
1. 全局快捷键（多个，可配置，默认 `Alt+Q`）。
2. 取词：模拟复制 + 剪贴板变更轮询 + 超时回退；文本后处理（Trim、换行处理、可选 `_/-` → 空格）。
3. LLM 客户端：OpenAI 兼容 chat/completions，流式 SSE 解析（含 `[DONE]`、错误提取、`<think>` 过滤）、URL 自动补全（含 `#` 强制模式）、温度、超时、重试。
4. 提示词系统：多组 prompt（角色列表），每组独立绑定一个快捷键；`$content`（`$source`/`$target` 简化保留）。
5. 结果展示：悬浮窗（置顶、鼠标附近）+ 自动复制可选；「替换回选区」作为后续增强。
6. 配置：单文件（TOML/JSON），多热键 × 多提示词共享一份 API 配置。

**明确排除（MVP 不做）**
OCR/截图、TTS、词典、生词本、历史缓存、插件市场、GUI 设置页（先配置文件）、Windows 打包（保留架构抽象，Windows 适配器可作为加分项）。

## 5. 可行性论证与风险

- **可行性**：链条中除「热键注册 / 模拟按键 / 剪贴板 / 窗口置前」四个 OS 原语外，全部是平台无关的纯逻辑（HTTP + JSON + 字符串模板）。四个原语在 macOS 均有成熟等价 API。核心模块（配置、模板、SSE 解析、URL 拼接）可在任意平台（含本开发环境 Linux）做完整单元测试，风险面被压缩到「平台适配器」薄层。
- **权限说明**：macOS 上模拟 `Cmd+C` 必须授予「辅助功能」权限（与 PopClip/Bob 等同类工具一致）；首次运行引导到「系统设置 → 隐私与安全性 → 辅助功能」。这是平台约束，非实现缺陷。
- **稳定性策略**（对应 STranslate 中的实现细节）：
  - 模拟复制前释放卡键（参考 `SendCtrlCV` 的 KeyUp 清理）；复制后「序列号变化 + 延迟 30ms」防半截读取；超时回退到旧剪贴板内容判定。
  - 并发防护：单任务互斥（新请求取消/忽略旧结果）。
  - 错误可见性：取词失败、HTTP 错误、SSE 错误分类提示。
  - 长文本截断、非 JSON 流行的容错（参考 `ParseStreamLine` 的 catch-return-default）。

## 6. 技术选型（已确认）

- **Python 3 + pyobjc**（用户已确认），结果展示为**鼠标附近悬浮窗**（用户已确认）。
- 架构：`core`（纯 Python、平台无关、可测）＋ `platform` 适配层（macOS 完整实现 + Windows 实验性 + headless 测试用）＋ 薄 CLI。
- 实现落点：`stranslate-lite/`（详见其 README；核心链路 44 项测试全部通过，macOS 适配层需按 README 清单真机验收）。
