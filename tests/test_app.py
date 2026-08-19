"""应用编排测试：热键触发 → 取词 → 渲染 → LLM → 面板更新（用假客户端与假适配器）。"""

import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

from stranslate_lite.app import App  # noqa: E402
from stranslate_lite.config import ApiConfig, CaptureConfig, Config, Hotkey, Message, Prompt  # noqa: E402
from stranslate_lite.llm import LlmError  # noqa: E402
from test_capture import FakeAdapter  # noqa: E402


def _config(prompts=None, hotkeys=None) -> Config:
    return Config(
        api=ApiConfig(base_url="http://x/", api_key="k", model="m"),
        prompts=prompts or {
            "翻译": Prompt(name="翻译", messages=[Message("user", "$content")]),
            "审阅": Prompt(name="审阅", messages=[Message("system", "review $source"), Message("user", "$content")]),
        },
        hotkeys=hotkeys or [Hotkey(key="alt+q", prompt="翻译")],
        capture=CaptureConfig(timeout_ms=500),
    )


def test_hotkey_triggers_full_pipeline(monkeypatch):
    import stranslate_lite.app as app_module

    adapter = FakeAdapter()
    captured: dict = {}
    deltas: dict = {}

    class FakeClient:
        def __init__(self, api):
            pass

        def chat_stream(self, messages, on_delta, cancel=None, temperature=None):
            captured["messages"] = messages
            on_delta("你好")
            on_delta("世界")

    monkeypatch.setattr(app_module, "LlmClient", FakeClient)

    app = App(_config(), adapter)
    # 模拟「选中文本后按热键」：复制钩子写入新剪贴板
    adapter.text = "旧内容"
    adapter.rev = 1
    adapter.copy_hook = lambda: (setattr(adapter, "text", "hello"), setattr(adapter, "rev", 2))

    app.on_hotkey(app.config.hotkeys[0])
    deadline = time.time() + 2
    while time.time() < deadline and "job-1" not in adapter.panels:
        time.sleep(0.02)

    assert captured["messages"] == [{"role": "user", "content": "hello"}]
    assert adapter.panels["job-1"] == "你好世界"


def test_single_flight_cancels_previous(monkeypatch):
    import stranslate_lite.app as app_module

    adapter = FakeAdapter()
    start_event = threading.Event()
    cancelled: dict = {}

    class FakeClient:
        def __init__(self, api):
            pass

        def chat_stream(self, messages, on_delta, cancel=None, temperature=None):
            start_event.set()
            cancel.wait(timeout=5)
            if cancel.is_set():
                cancelled["yes"] = True
                raise app_module.CancelledError()

    monkeypatch.setattr(app_module, "LlmClient", FakeClient)

    app = App(_config(), adapter)
    adapter.text = "旧内容"
    adapter.rev = 1
    adapter.copy_hook = lambda: (setattr(adapter, "text", "hello"), setattr(adapter, "rev", 2))
    app.on_hotkey(app.config.hotkeys[0])
    assert start_event.wait(2)
    # 第二个触发应取消第一个任务
    app.on_hotkey(app.config.hotkeys[0])
    deadline = time.time() + 2
    while time.time() < deadline and cancelled.get("yes") is None:
        time.sleep(0.02)
    assert cancelled.get("yes") is True


def test_llm_error_shown_in_panel(monkeypatch):
    import stranslate_lite.app as app_module

    adapter = FakeAdapter()

    class FakeClient:
        def __init__(self, api):
            pass

        def chat_stream(self, messages, on_delta, cancel=None, temperature=None):
            raise LlmError("api", "API 返回错误（HTTP 401）：invalid key")

    monkeypatch.setattr(app_module, "LlmClient", FakeClient)

    app = App(_config(), adapter)
    adapter.text = ""
    adapter.rev = 1
    adapter.copy_hook = lambda: (setattr(adapter, "text", "hello"), setattr(adapter, "rev", 2))
    app.on_hotkey(app.config.hotkeys[0])
    deadline = time.time() + 2
    while time.time() < deadline and "job-1" not in adapter.panels:
        time.sleep(0.02)
    assert "调用失败" in adapter.panels.get("job-1", "")


def test_capture_failure_message(monkeypatch):
    import stranslate_lite.app as app_module

    adapter = FakeAdapter()
    adapter.text = "旧内容"  # 复制后不变 → 取词失败

    app = App(_config(), adapter)
    app.on_hotkey(app.config.hotkeys[0])
    deadline = time.time() + 2
    while time.time() < deadline and "job-1" not in adapter.panels:
        time.sleep(0.02)
    assert "取词失败" in adapter.panels.get("job-1", "")
