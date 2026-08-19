"""配置模型与加载。

配置文件为 TOML，路径优先级：
1. 环境变量 STRANSLATE_LITE_CONFIG 指定的文件
2. 平台默认位置：
   - macOS:   ~/Library/Application Support/stranslate-lite/config.toml
   - Linux:   $XDG_CONFIG_HOME/stranslate-lite/config.toml（默认 ~/.config/stranslate-lite/config.toml）
   - Windows: %APPDATA%\\stranslate-lite\\config.toml

api_key 支持 "${ENV_VAR}" 语法引用环境变量，避免明文入库。
"""

from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib  # type: ignore

APP_DIR_NAME = "stranslate-lite"
CONFIG_FILE_NAME = "config.toml"

_DEFAULT_HOTKEY = "alt+q"


class ConfigError(ValueError):
    """配置错误（含路径与字段信息）。"""


# --------------------------------------------------------------------------
# 数据模型
# --------------------------------------------------------------------------

@dataclass
class ApiConfig:
    base_url: str = "https://api.openai.com/v1"
    api_key: str = ""
    model: str = "gpt-4o-mini"
    temperature: float = 0.7
    timeout_seconds: float = 60.0
    max_retries: int = 3
    retry_delay_ms: int = 1000
    extra_body: Dict[str, Any] = field(default_factory=dict)
    source_lang: str = "Requires you to identify automatically"
    target_lang: str = "Simplified Chinese"

    def resolve_api_key(self) -> str:
        """解析 ${ENV_VAR} 引用。"""
        if not self.api_key:
            return ""
        m = re.fullmatch(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", self.api_key.strip())
        if m:
            return os.environ.get(m.group(1), "")
        return self.api_key


@dataclass
class Message:
    role: str
    content: str


@dataclass
class Prompt:
    name: str
    messages: List[Message]
    source_lang: Optional[str] = None
    target_lang: Optional[str] = None


@dataclass
class Hotkey:
    key: str
    prompt: str
    source_lang: Optional[str] = None
    target_lang: Optional[str] = None


@dataclass
class CaptureConfig:
    timeout_ms: int = 500
    line_break: str = "keep"  # keep | remove | space
    separators: str = "none"  # none | underscore | hyphen | both
    max_chars: int = 8000


@dataclass
class UiConfig:
    auto_close_seconds: float = 15.0  # 结果面板无更新 N 秒后自动关闭；0 = 永不自动关闭


@dataclass
class Config:
    api: ApiConfig = field(default_factory=ApiConfig)
    prompts: Dict[str, Prompt] = field(default_factory=dict)
    hotkeys: List[Hotkey] = field(default_factory=list)
    capture: CaptureConfig = field(default_factory=CaptureConfig)
    ui: UiConfig = field(default_factory=UiConfig)

    def default_hotkey(self) -> Optional[Hotkey]:
        """CLI/无参调用时的默认触发器：第一个热键。"""
        return self.hotkeys[0] if self.hotkeys else None

    def prompt(self, name: str) -> Prompt:
        if name not in self.prompts:
            raise ConfigError(f"提示词“{name}”不存在，可用：{', '.join(sorted(self.prompts))}")
        return self.prompts[name]


# --------------------------------------------------------------------------
# 路径
# --------------------------------------------------------------------------

def config_path() -> Path:
    override = os.environ.get("STRANSLATE_LITE_CONFIG")
    if override:
        return Path(override).expanduser()
    if sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    elif sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", Path.home()))
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / APP_DIR_NAME / CONFIG_FILE_NAME


# --------------------------------------------------------------------------
# 解析与校验
# --------------------------------------------------------------------------

_ROLES = {"system", "user", "assistant"}
_LINE_BREAKS = {"keep", "remove", "space"}
_SEPARATORS = {"none", "underscore", "hyphen", "both"}
_KNOWN_TOP_KEYS = {"api", "capture", "prompts", "hotkeys", "ui"}


def _err(path: str, msg: str) -> ConfigError:
    return ConfigError(f"配置错误 [{path}]：{msg}")


def _table(obj: Any, path: str) -> Dict[str, Any]:
    if not isinstance(obj, dict):
        raise _err(path, "应为 TOML 表")
    return obj


def _str(obj: Any, path: str) -> str:
    if not isinstance(obj, str) or not obj.strip():
        raise _err(path, "应为非空字符串")
    return obj.strip()


def _opt_str(obj: Any, path: str) -> Optional[str]:
    if obj is None or obj == "":
        return None
    return _str(obj, path)


def _float(obj: Any, path: str, lo: float, hi: float) -> float:
    if not isinstance(obj, (int, float)) or isinstance(obj, bool):
        raise _err(path, "应为数字")
    v = float(obj)
    if not (lo <= v <= hi):
        raise _err(path, f"应在 [{lo}, {hi}] 范围内")
    return v


def _int(obj: Any, path: str, lo: int, hi: int) -> int:
    if not isinstance(obj, int) or isinstance(obj, bool):
        raise _err(path, "应为整数")
    if not (lo <= obj <= hi):
        raise _err(path, f"应在 [{lo}, {hi}] 范围内")
    return obj


def _parse_api(t: Any) -> ApiConfig:
    t = _table(t, "api")
    for k in t:
        if k not in ApiConfig.__dataclass_fields__:
            raise _err(f"api.{k}", "未知字段")
    api = ApiConfig()
    if "base_url" in t:
        api.base_url = _str(t["base_url"], "api.base_url")
    if "api_key" in t:
        api.api_key = _str(t["api_key"], "api.api_key")
    if "model" in t:
        api.model = _str(t["model"], "api.model")
    if "temperature" in t:
        api.temperature = _float(t["temperature"], "api.temperature", 0.0, 2.0)
    if "timeout_seconds" in t:
        api.timeout_seconds = _float(t["timeout_seconds"], "api.timeout_seconds", 1.0, 600.0)
    if "max_retries" in t:
        api.max_retries = _int(t["max_retries"], "api.max_retries", 0, 10)
    if "retry_delay_ms" in t:
        api.retry_delay_ms = _int(t["retry_delay_ms"], "api.retry_delay_ms", 0, 60000)
    if "extra_body" in t:
        tb = _table(t["extra_body"], "api.extra_body")
        if any(k in tb for k in ("model", "messages", "stream")):
            raise _err("api.extra_body", "不能覆盖内置字段 model/messages/stream")
        api.extra_body = dict(tb)
    if "source_lang" in t:
        api.source_lang = _str(t["source_lang"], "api.source_lang")
    if "target_lang" in t:
        api.target_lang = _str(t["target_lang"], "api.target_lang")
    return api


def _parse_prompt(t: Any, path: str, default_name: str = "") -> Prompt:
    t = _table(t, path)
    name = _str(t.get("name") or default_name, f"{path}.name")
    if "messages" not in t or not isinstance(t["messages"], list) or not t["messages"]:
        raise _err(f"{path}.messages", "至少需要一条消息")
    msgs: List[Message] = []
    for i, m in enumerate(t["messages"]):
        m = _table(m, f"{path}.messages[{i}]")
        role = _str(m.get("role"), f"{path}.messages[{i}].role").lower()
        if role not in _ROLES:
            raise _err(f"{path}.messages[{i}].role", f"角色应为 {'/'.join(sorted(_ROLES))} 之一")
        msgs.append(Message(role=role, content=_str(m.get("content"), f"{path}.messages[{i}].content")))
    return Prompt(
        name=name,
        messages=msgs,
        source_lang=_opt_str(t.get("source_lang"), f"{path}.source_lang"),
        target_lang=_opt_str(t.get("target_lang"), f"{path}.target_lang"),
    )


def _parse_prompts(t: Any) -> Dict[str, Prompt]:
    t = _table(t, "prompts")
    prompts: Dict[str, Prompt] = {}
    for name, body in t.items():
        if not isinstance(body, dict):
            raise _err(f"prompts.{name}", "应为 TOML 表")
        p = _parse_prompt(body, f"prompts.{name}", default_name=name)
        if p.name in prompts:
            raise _err("prompts", f"提示词名称重复：{p.name}")
        prompts[p.name] = p
    if not prompts:
        raise _err("prompts", "至少需要一个提示词")
    return prompts


def _parse_capture(t: Any) -> CaptureConfig:
    t = _table(t, "capture")
    c = CaptureConfig()
    if "timeout_ms" in t:
        c.timeout_ms = _int(t["timeout_ms"], "capture.timeout_ms", 50, 5000)
    if "line_break" in t:
        c.line_break = _str(t["line_break"], "capture.line_break").lower()
        if c.line_break not in _LINE_BREAKS:
            raise _err("capture.line_break", f"应为 {'/'.join(sorted(_LINE_BREAKS))} 之一")
    if "separators" in t:
        c.separators = _str(t["separators"], "capture.separators").lower()
        if c.separators not in _SEPARATORS:
            raise _err("capture.separators", f"应为 {'/'.join(sorted(_SEPARATORS))} 之一")
    if "max_chars" in t:
        c.max_chars = _int(t["max_chars"], "capture.max_chars", 100, 100000)
    return c


def _parse_ui(t: Any) -> UiConfig:
    t = _table(t, "ui")
    for k in t:
        if k not in UiConfig.__dataclass_fields__:
            raise _err(f"ui.{k}", "未知字段")
    u = UiConfig()
    if "auto_close_seconds" in t:
        u.auto_close_seconds = _float(t["auto_close_seconds"], "ui.auto_close_seconds", 0.0, 600.0)
    return u


def _parse_hotkeys(t: Any, prompts: Dict[str, Prompt]) -> List[Hotkey]:
    if t is None:
        return []
    if not isinstance(t, list):
        raise _err("hotkeys", "应为 TOML 数组 [[hotkeys]]")
    keys: List[Hotkey] = []
    for i, item in enumerate(t):
        item = _table(item, f"hotkeys[{i}]")
        key = _str(item.get("key"), f"hotkeys[{i}].key")
        prompt = _str(item.get("prompt"), f"hotkeys[{i}].prompt")
        if prompt not in prompts:
            raise _err(f"hotkeys[{i}].prompt", f"提示词“{prompt}”不存在")
        if any(h.key == key for h in keys):
            raise _err(f"hotkeys[{i}].key", f"快捷键重复：{key}")
        keys.append(
            Hotkey(
                key=key,
                prompt=prompt,
                source_lang=_opt_str(item.get("source_lang"), f"hotkeys[{i}].source_lang"),
                target_lang=_opt_str(item.get("target_lang"), f"hotkeys[{i}].target_lang"),
            )
        )
    if not keys:
        raise _err("hotkeys", "至少需要配置一个快捷键（例如 alt+q）")
    return keys


def parse_config(data: Dict[str, Any]) -> Config:
    if not isinstance(data, dict):
        raise ConfigError("配置根节点应为 TOML 表")
    for k in data:
        if k not in _KNOWN_TOP_KEYS:
            raise _err(k, "未知顶层字段")
    api = _parse_api(data.get("api", {}))
    prompts = _parse_prompts(data.get("prompts", {}))
    capture = _parse_capture(data.get("capture", {}))
    ui = _parse_ui(data.get("ui", {}))
    hotkeys = _parse_hotkeys(data.get("hotkeys"), prompts)
    return Config(api=api, prompts=prompts, hotkeys=hotkeys, capture=capture, ui=ui)


def load_config(path: Optional[Path] = None) -> Config:
    path = path or config_path()
    if not path.exists():
        raise ConfigError(f"配置文件不存在：{path}\n请先运行 `stranslate-lite config --init` 生成示例配置。")
    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except ConfigError:
        raise
    except Exception as e:
        raise ConfigError(f"配置文件解析失败：{path}\n{e}") from e
    return parse_config(data)


def write_example(path: Path, hotkey: str = _DEFAULT_HOTKEY) -> None:
    """生成示例配置（已存在则不覆盖）。"""
    if path.exists():
        raise ConfigError(f"配置文件已存在：{path}（如需重置请先删除或改名）")
    path.parent.mkdir(parents=True, exist_ok=True)
    text = _example_toml(hotkey)
    path.write_text(text, encoding="utf-8")


_EXAMPLE_HEADER = """# stranslate-lite 示例配置
# 修改后保存即可，下次触发快捷键时自动生效（无需重启）。"""


def _example_toml(hotkey: str) -> str:
    return _EXAMPLE_HEADER + f"""
[api]
base_url = "https://api.openai.com/v1"      # 支持 OpenAI/DeepSeek 等兼容接口；以 # 结尾表示强制使用该完整地址
api_key = "${{OPENAI_API_KEY}}"               # 可直接填 key，或引用环境变量
model = "gpt-4o-mini"
temperature = 0.7
timeout_seconds = 60
max_retries = 3
retry_delay_ms = 1000
source_lang = "Requires you to identify automatically"
target_lang = "Simplified Chinese"
# [api.extra_body]                            # 附加请求体参数（不能覆盖 model/messages/stream）
# enable_thinking = false                     # 例如：阿里云百炼 qwen3 系列关闭思考模式，显著降低首字延迟
# top_p = 0.9

[capture]
timeout_ms = 500            # 模拟复制后等待剪贴板变化的最长时间（50~5000）
line_break = "keep"         # keep | remove | space：取词文本的换行处理
separators = "none"         # none | underscore | hyphen | both：标识符内 _/- 转空格（利于代码翻译）
max_chars = 8000

[ui]
auto_close_seconds = 15     # 结果面板无更新后自动关闭秒数（0 = 永不自动关闭；点击面板外随时关闭）

[prompts."翻译"]
name = "翻译"
[[prompts."翻译".messages]]
role = "system"
content = "You are a professional, authentic translation engine. You only return the translated text, without any explanations."
[[prompts."翻译".messages]]
role = "user"
content = "Please translate into $target (avoid explaining the original text):\\n\\n$content"

[prompts."代码审阅"]
name = "代码审阅"
[[prompts."代码审阅".messages]]
role = "system"
content = "You are an experienced senior engineer. Explain the selected code concisely: what it does, key edge cases, and possible bugs. Answer in Simplified Chinese."
[[prompts."代码审阅".messages]]
role = "user"
content = "$content"

[[hotkeys]]
key = "{hotkey}"
prompt = "翻译"

[[hotkeys]]
key = "alt+w"
prompt = "代码审阅"
"""
