"""平台适配抽象与快捷键规范解析。"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Callable, List, Optional, Set, Tuple


class AdapterError(Exception):
    """平台适配层错误。"""


@dataclass(frozen=True)
class ParsedHotkey:
    modifiers: Set[str]  # {"ctrl","alt","shift","cmd"}（平台无关名称）
    key: str             # 归一化键名：单字符小写或命名键（f1/left/space/return/...）

    def display(self) -> str:
        order = ["ctrl", "alt", "shift", "cmd"]
        parts = [m for m in order if m in self.modifiers] + [self.key]
        return "+".join(parts)


_MOD_ALIASES = {
    "ctrl": "ctrl", "control": "ctrl",
    "alt": "alt", "option": "alt",
    "shift": "shift",
    "cmd": "cmd", "command": "cmd", "win": "cmd", "meta": "cmd", "super": "cmd",
}

_NAMED_KEYS = {
    "f1", "f2", "f3", "f4", "f5", "f6", "f7", "f8", "f9", "f10", "f11", "f12", "f13", "f14", "f15", "f16",
    "left", "right", "up", "down", "space", "tab", "return", "enter", "escape", "esc",
    "delete", "backspace", "home", "end", "pageup", "pagedown", "insert", "printscreen",
}

_SINGLE_CHARS = set("abcdefghijklmnopqrstuvwxyz0123456789-=[]\\;',./`")


def parse_hotkey_spec(spec: str) -> ParsedHotkey:
    """解析 "alt+q" 之类字符串为平台无关结构。"""
    parts = [p.strip().lower() for p in (spec or "").split("+") if p.strip()]
    if len(parts) < 2:
        raise AdapterError(f"快捷键“{spec}”格式无效，应为“修饰键+按键”，例如 alt+q")
    mods: Set[str] = set()
    key: Optional[str] = None
    for p in parts[:-1]:
        if p not in _MOD_ALIASES:
            raise AdapterError(f"快捷键“{spec}”含未知修饰键“{p}”（可用：ctrl/alt(option)/shift/cmd）")
        mods.add(_MOD_ALIASES[p])
    last = parts[-1]
    if last in _NAMED_KEYS:
        key = "enter" if last == "enter" else ("esc" if last == "esc" else last)
    elif len(last) == 1 and last in _SINGLE_CHARS:
        key = last
    else:
        raise AdapterError(f"快捷键“{spec}”的按键“{last}”不受支持（可用字母/数字/F1-F12/方向键等）")
    return ParsedHotkey(modifiers=mods, key=key)


class PlatformAdapter(ABC):
    """平台适配接口。所有方法线程安全：后台线程可调用显示方法。"""

    name: str = "abstract"

    # ----- 生命周期 -----
    @abstractmethod
    def run(self) -> None:
        """阻塞运行事件循环（macOS: NSApplication run）。"""

    @abstractmethod
    def stop(self) -> None:
        """请求退出事件循环。"""

    # ----- 热键 -----
    @abstractmethod
    def register_hotkey(self, spec: str, callback: Callable[[], None]) -> None:
        """注册全局热键；回调可能在任意线程执行。"""

    # ----- 取词 -----
    @abstractmethod
    def copy_selection(self) -> None:
        """向系统发送「复制」组合键（macOS: Cmd+C）。"""

    @abstractmethod
    def clipboard_text(self) -> Optional[str]:
        """读取剪贴板文本。"""

    @abstractmethod
    def clipboard_revision(self) -> int:
        """剪贴板变更序号（macOS: changeCount）。"""

    # ----- 结果展示（key 标识一次任务，支持流式更新） -----
    @abstractmethod
    def show_result(self, key: str, text: str) -> None:
        """显示/替换一个结果面板。"""

    @abstractmethod
    def update_result(self, key: str, text: str) -> None:
        """流式更新面板文本。"""

    @abstractmethod
    def close_result(self, key: str) -> None:
        """关闭面板。"""

    # ----- 杂项 -----
    def mouse_position(self) -> Optional[Tuple[int, int]]:
        return None

    def permission_issues(self) -> List[str]:
        """返回需要用户处理的事项（人类可读）。"""
        return []
