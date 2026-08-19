"""无界面适配器：用于 Linux 开发/测试与 CI。

- 热键注册到内存映射，可编程触发（测试用）。
- 剪贴板为空实现。
- 结果面板输出到 stdout。
"""

from __future__ import annotations

import sys
import threading
from typing import Callable, Dict, Optional

from .base import AdapterError, PlatformAdapter, parse_hotkey_spec


class HeadlessAdapter(PlatformAdapter):
    name = "headless"

    def __init__(self) -> None:
        self._hotkeys: Dict[str, Callable[[], None]] = {}
        self._stop = threading.Event()
        self._panels: Dict[str, str] = {}
        self._lock = threading.Lock()

    # ----- 生命周期 -----
    def run(self) -> None:
        self._stop.wait()

    def stop(self) -> None:
        self._stop.set()

    # ----- 热键 -----
    def register_hotkey(self, spec: str, callback: Callable[[], None]) -> None:
        parse_hotkey_spec(spec)  # 校验格式
        if spec in self._hotkeys:
            raise AdapterError(f"快捷键重复：{spec}")
        self._hotkeys[spec] = callback

    def fire(self, spec: str) -> None:
        """测试辅助：模拟触发。"""
        cb = self._hotkeys.get(spec)
        if cb is None:
            raise AdapterError(f"快捷键未注册：{spec}")
        cb()

    # ----- 取词 -----
    def copy_selection(self) -> None:
        pass

    def clipboard_text(self) -> Optional[str]:
        return None

    def clipboard_revision(self) -> int:
        return 0

    # ----- 结果展示 -----
    def show_result(self, key: str, text: str) -> None:
        with self._lock:
            first = key not in self._panels
            self._panels[key] = text
        if first:
            sys.stdout.write("\n──────── stranslate 结果 ────────\n")
        sys.stdout.write(text)
        sys.stdout.flush()

    def update_result(self, key: str, text: str) -> None:
        with self._lock:
            prev = self._panels.get(key, "")
            self._panels[key] = text
        if text.startswith(prev):
            sys.stdout.write(text[len(prev):])
        else:
            sys.stdout.write(text)
        sys.stdout.flush()

    def close_result(self, key: str) -> None:
        with self._lock:
            existed = key in self._panels
            self._panels.pop(key, None)
        if existed:
            sys.stdout.write("\n──────────────────────────────────\n")
            sys.stdout.flush()
