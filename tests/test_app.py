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


@pytest.fixture
def app(monkeypatch, tmp_path):
    """构造 App，并让热重载始终返回内存中的配置（测试环境无配置文件）。

    STRANSLATE_LITE_CONFIG 指向 tmp：缓存库 cache.db 也落在 tmp，
    不会污染用户真实配置目录。
    """
    import stranslate_lite.app as app_module

    cfg = _config()
    adapter = FakeAdapter()
    monkeypatch.setenv("STRANSLATE_LITE_CONFIG", str(tmp_path / "config.toml"))
    monkeypatch.setattr(app_module, "load_config", lambda: cfg)
    return App(cfg, adapter), adapter


def _wait_for_panel(adapter, key, timeout=2.0):
    deadline = time.time() + timeout
    while time.time() < deadline and key not in adapter.panels:
        time.sleep(0.02)
    return adapter.panels.get(key)


def test_hotkey_triggers_full_pipeline(app, monkeypatch):
    import stranslate_lite.app as app_module

    application, adapter = app
    captured: dict = {}

    class FakeClient:
        def __init__(self, api):
            pass

        def chat_stream(self, messages, on_delta, cancel=None, temperature=None):
            captured["messages"] = messages
            on_delta("你好")
            on_delta("世界")

    monkeypatch.setattr(app_module, "LlmClient", FakeClient)

    # 模拟「选中文本后按热键」：复制钩子写入新剪贴板
    adapter.text = "旧内容"
    adapter.rev = 1
    adapter.copy_hook = lambda: (setattr(adapter, "text", "hello"), setattr(adapter, "rev", 2))

    application.on_hotkey("alt+q")
    assert _wait_for_panel(adapter, "job-1") == "你好世界"
    assert captured["messages"] == [{"role": "user", "content": "hello"}]


def test_single_flight_cancels_previous(app, monkeypatch):
    import stranslate_lite.app as app_module

    application, adapter = app
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

    adapter.text = "旧内容"
    adapter.rev = 1
    adapter.copy_hook = lambda: (setattr(adapter, "text", "hello"), setattr(adapter, "rev", 2))
    application.on_hotkey("alt+q")
    assert start_event.wait(2)
    # 第二个触发应取消第一个任务
    application.on_hotkey("alt+q")
    deadline = time.time() + 2
    while time.time() < deadline and cancelled.get("yes") is None:
        time.sleep(0.02)
    assert cancelled.get("yes") is True


def test_llm_error_shown_in_panel(app, monkeypatch):
    import stranslate_lite.app as app_module

    application, adapter = app

    class FakeClient:
        def __init__(self, api):
            pass

        def chat_stream(self, messages, on_delta, cancel=None, temperature=None):
            raise LlmError("api", "API 返回错误（HTTP 401）：invalid key")

    monkeypatch.setattr(app_module, "LlmClient", FakeClient)

    adapter.text = ""
    adapter.rev = 1
    adapter.copy_hook = lambda: (setattr(adapter, "text", "hello"), setattr(adapter, "rev", 2))
    application.on_hotkey("alt+q")
    panel = _wait_for_panel(adapter, "job-1")
    assert panel is not None and "调用失败" in panel


def test_capture_failure_message(app, monkeypatch):
    import stranslate_lite.app as app_module

    application, adapter = app
    adapter.text = "旧内容"  # 复制后不变 → 取词失败

    application.on_hotkey("alt+q")
    panel = _wait_for_panel(adapter, "job-1")
    assert panel is not None and "取词失败" in panel


def test_unknown_hotkey_key_ignored(app):
    import stranslate_lite.app as app_module

    application, adapter = app
    application.on_hotkey("alt+unregistered")
    time.sleep(0.1)
    assert not adapter.panels  # 未注册的触发被忽略，不产生面板


def test_new_trigger_closes_previous_panel(app, monkeypatch):
    """单窗口语义（对齐 STranslate SingletonWindowOpener）：新触发关闭旧面板。"""
    import stranslate_lite.app as app_module

    application, adapter = app

    class FakeClient:
        def __init__(self, api):
            pass

        def chat_stream(self, messages, on_delta, cancel=None, temperature=None):
            on_delta("回答")

    monkeypatch.setattr(app_module, "LlmClient", FakeClient)
    adapter.text = "旧内容"
    adapter.rev = 1
    adapter.copy_hook = lambda: (setattr(adapter, "text", "hello"), setattr(adapter, "rev", adapter.rev + 1))

    application.on_hotkey("alt+q")
    assert _wait_for_panel(adapter, "job-1") == "回答"
    # 第二次触发：旧面板（job-1）应被关闭，只保留新面板（job-2）
    application.on_hotkey("alt+q")
    assert _wait_for_panel(adapter, "job-2") == "回答"
    assert "job-1" not in adapter.panels
    assert set(adapter.panels) == {"job-2"}


def test_auto_close_seconds_forwarded_to_adapter(app, monkeypatch):
    """[ui].auto_close_seconds 应在每次触发时下发到适配器（配合配置热重载）。"""
    import stranslate_lite.app as app_module

    application, adapter = app
    received = []
    adapter.set_auto_close_seconds = lambda s: received.append(s)

    application.on_hotkey("alt+q")
    assert received == [15.0]  # 默认配置值


def test_cache_hit_skips_llm(app, monkeypatch):
    """相同内容的第二次触发命中缓存，不再调用 API（对齐 STranslate 历史缓存）。"""
    import stranslate_lite.app as app_module

    application, adapter = app
    calls = {"n": 0}

    class FakeClient:
        def __init__(self, api):
            pass

        def chat_stream(self, messages, on_delta, cancel=None, temperature=None):
            calls["n"] += 1
            on_delta("新鲜回答")
            return "新鲜回答"

    monkeypatch.setattr(app_module, "LlmClient", FakeClient)
    adapter.text = "旧内容"
    adapter.rev = 1
    adapter.copy_hook = lambda: (setattr(adapter, "text", "hello"), setattr(adapter, "rev", adapter.rev + 1))

    application.on_hotkey("alt+q")
    assert _wait_for_panel(adapter, "job-1") == "新鲜回答"
    assert calls["n"] == 1

    # 第二次触发：同一内容 → 命中缓存，不调 API
    application.on_hotkey("alt+q")
    assert _wait_for_panel(adapter, "job-2") == "新鲜回答"
    assert calls["n"] == 1


def test_cache_miss_after_capture_change(app, monkeypatch):
    """不同取词内容 → 缓存未命中，正常调用 API。"""
    import stranslate_lite.app as app_module

    application, adapter = app
    calls = {"n": 0}

    class FakeClient:
        def __init__(self, api):
            pass

        def chat_stream(self, messages, on_delta, cancel=None, temperature=None):
            calls["n"] += 1
            on_delta("回答")
            return "回答"

    monkeypatch.setattr(app_module, "LlmClient", FakeClient)
    adapter.text = "旧内容"
    adapter.rev = 1
    captured_text = ["hello", "world"]

    def copy_hook():
        setattr(adapter, "text", captured_text[0])
        setattr(adapter, "rev", adapter.rev + 1)

    adapter.copy_hook = copy_hook
    application.on_hotkey("alt+q")
    _wait_for_panel(adapter, "job-1")

    def copy_hook2():
        setattr(adapter, "text", captured_text[1])
        setattr(adapter, "rev", adapter.rev + 1)

    adapter.copy_hook = copy_hook2
    application.on_hotkey("alt+q")
    _wait_for_panel(adapter, "job-2")
    assert calls["n"] == 2
