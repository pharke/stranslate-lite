"""取词逻辑测试：用可控假适配器模拟剪贴板行为。"""

import sys
import time
from pathlib import Path
from typing import Callable, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from stranslate_lite.capture import CaptureError, capture_and_postprocess, capture_selected  # noqa: E402
from stranslate_lite.config import CaptureConfig  # noqa: E402
from stranslate_lite.platform.base import PlatformAdapter  # noqa: E402


class FakeAdapter(PlatformAdapter):
    """剪贴板行为可编程的假适配器。"""

    def __init__(self) -> None:
        self.text: Optional[str] = None
        self.rev = 0
        self.copy_calls = 0
        self.copy_hook: Optional[Callable[[], None]] = None
        self.panels = {}

    def run(self) -> None:
        pass

    def stop(self) -> None:
        pass

    def register_hotkey(self, spec, callback) -> None:
        pass

    def copy_selection(self) -> None:
        self.copy_calls += 1
        if self.copy_hook:
            self.copy_hook()

    def clipboard_text(self) -> Optional[str]:
        return self.text

    def clipboard_revision(self) -> int:
        return self.rev

    def show_result(self, key: str, text: str) -> None:
        self.panels[key] = text

    def update_result(self, key: str, text: str) -> None:
        self.panels[key] = text

    def close_result(self, key: str) -> None:
        self.panels.pop(key, None)


def _set_clipboard(adapter: FakeAdapter, text: str) -> None:
    adapter.text = text
    adapter.rev += 1


def test_capture_selection_changed():
    a = FakeAdapter()
    _set_clipboard(a, "旧内容")
    a.copy_hook = lambda: _set_clipboard(a, "  选中文本  ")
    text = capture_selected(a, CaptureConfig(timeout_ms=500))
    assert text == "选中文本"
    assert a.copy_calls == 1


def test_capture_unchanged_returns_none():
    """剪贴板未变化（无选中文本）→ 返回 None（对齐 STranslate）。"""
    a = FakeAdapter()
    _set_clipboard(a, "旧内容")
    text = capture_selected(a, CaptureConfig(timeout_ms=100))
    assert text is None


def test_capture_empty_original_and_unchanged():
    """原本剪贴板为空且无变化：返回 None（上层视为取词失败）。"""
    a = FakeAdapter()
    a.text = ""
    assert capture_selected(a, CaptureConfig(timeout_ms=100)) is None


def test_capture_and_postprocess_failure():
    a = FakeAdapter()
    a.text = "旧内容"
    try:
        capture_and_postprocess(a, CaptureConfig(timeout_ms=100))
        assert False, "应抛 CaptureError"
    except CaptureError as e:
        assert "未取到" in str(e)
