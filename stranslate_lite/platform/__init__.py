"""平台抽象层入口：按运行平台选择适配器。"""

from __future__ import annotations

import sys

from .base import AdapterError, ParsedHotkey, PlatformAdapter, parse_hotkey_spec

__all__ = ["AdapterError", "ParsedHotkey", "PlatformAdapter", "parse_hotkey_spec", "get_adapter"]


def get_adapter() -> PlatformAdapter:
    if sys.platform == "darwin":
        from .macos import MacOSAdapter

        return MacOSAdapter()
    if sys.platform == "win32":
        from .windows import WindowsAdapter

        return WindowsAdapter()
    from .headless import HeadlessAdapter

    return HeadlessAdapter()
