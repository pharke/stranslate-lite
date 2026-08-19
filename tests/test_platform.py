"""快捷键规范解析测试。"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from stranslate_lite.platform.base import AdapterError, parse_hotkey_spec  # noqa: E402


def test_basic():
    h = parse_hotkey_spec("Alt + Q")
    assert h.modifiers == {"alt"}
    assert h.key == "q"


def test_aliases():
    assert parse_hotkey_spec("option+w").modifiers == {"alt"}
    assert parse_hotkey_spec("cmd+shift+f1").modifiers == {"cmd", "shift"}
    assert parse_hotkey_spec("command+win+left").modifiers == {"cmd"}


def test_named_keys():
    assert parse_hotkey_spec("ctrl+enter").key == "enter"
    assert parse_hotkey_spec("ctrl+esc").key == "esc"


def test_invalid():
    for spec in ("q", "alt+", "+q", "alt+xyz", "ctrl+alt", "alt+↗"):
        with pytest.raises(AdapterError):
            parse_hotkey_spec(spec)


def test_display():
    h = parse_hotkey_spec("shift+alt+q")
    assert h.display() == "alt+shift+q"
