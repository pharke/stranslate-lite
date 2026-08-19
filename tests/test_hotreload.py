"""配置热重载测试：触发热键时重新读取配置文件，「保存即生效」。"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

from stranslate_lite.app import App  # noqa: E402
from stranslate_lite.config import load_config  # noqa: E402
from test_capture import FakeAdapter  # noqa: E402


def _write_config(path: Path, user_content: str = "$content", hotkey: str = "alt+q") -> None:
    path.write_text(
        f"""
[api]
base_url = "http://x/"
api_key = "k"
model = "m"

[prompts."翻译"]
name = "翻译"
[[prompts."翻译".messages]]
role = "user"
content = "{user_content}"

[[hotkeys]]
key = "{hotkey}"
prompt = "翻译"
""".strip()
        + "\n",
        encoding="utf-8",
    )


def _wait_for_panel(adapter, key, timeout=2.0):
    deadline = time.time() + timeout
    while time.time() < deadline and key not in adapter.panels:
        time.sleep(0.02)
    return adapter.panels.get(key)


@pytest.fixture
def client(monkeypatch):
    """假 LLM 客户端：记录每次请求的 messages。"""
    import stranslate_lite.app as app_module

    captured = []

    class FakeClient:
        def __init__(self, api):
            self.api = api

        def chat_stream(self, messages, on_delta, cancel=None, temperature=None):
            captured.append({"messages": messages, "api": self.api})
            on_delta("回答")

    monkeypatch.setattr(app_module, "LlmClient", FakeClient)
    return captured


def _setup_capture(adapter, selected: str = "hello") -> None:
    """模拟「有选中文本」：复制钩子把新文本写入剪贴板（版本号自增）。"""
    adapter.text = "旧内容"
    adapter.rev = 1
    adapter.copy_hook = lambda: (
        setattr(adapter, "text", selected),
        setattr(adapter, "rev", adapter.rev + 1),
    )


def test_config_reloaded_on_each_trigger(tmp_path, monkeypatch, client):
    cfg_path = tmp_path / "config.toml"
    _write_config(cfg_path, user_content="旧提示词 $content")
    monkeypatch.setenv("STRANSLATE_LITE_CONFIG", str(cfg_path))

    app = App(load_config(), FakeAdapter())
    _setup_capture(app.adapter)
    app.on_hotkey("alt+q")
    _wait_for_panel(app.adapter, "job-1")
    assert client[0]["messages"] == [{"role": "user", "content": "旧提示词 hello"}]

    # 修改提示词与 API 配置后保存 → 下次触发即生效（无需重启）
    cfg_path.write_text(
        cfg_path.read_text(encoding="utf-8")
        .replace("旧提示词 $content", "新提示词 $content")
        .replace('model = "m"', 'model = "m2"'),
        encoding="utf-8",
    )
    app.on_hotkey("alt+q")
    _wait_for_panel(app.adapter, "job-2")
    assert client[1]["messages"] == [{"role": "user", "content": "新提示词 hello"}]
    assert client[1]["api"].model == "m2"


def test_invalid_config_shows_error_panel(tmp_path, monkeypatch, client):
    cfg_path = tmp_path / "config.toml"
    _write_config(cfg_path)
    monkeypatch.setenv("STRANSLATE_LITE_CONFIG", str(cfg_path))

    app = App(load_config(), FakeAdapter())

    # 配置被改坏 → 面板提示重载失败，不崩溃
    cfg_path.write_text("这不是合法的 TOML [[[", encoding="utf-8")
    app.on_hotkey("alt+q")
    panel = _wait_for_panel(app.adapter, "job-1")
    assert panel is not None and "配置重载失败" in panel


def test_removed_hotkey_binding_is_ignored(tmp_path, monkeypatch, client):
    cfg_path = tmp_path / "config.toml"
    _write_config(cfg_path)
    monkeypatch.setenv("STRANSLATE_LITE_CONFIG", str(cfg_path))

    app = App(load_config(), FakeAdapter())

    # 热键绑定变更需重启；触发已不在配置中的快捷键 → 忽略
    _write_config(cfg_path, hotkey="alt+w")
    app.on_hotkey("alt+q")
    time.sleep(0.15)
    assert not app.adapter.panels
    assert client == []
