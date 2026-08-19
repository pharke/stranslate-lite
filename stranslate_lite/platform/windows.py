"""Windows 平台适配器（试验性，保留 Windows 上的使用习惯）。

- 全局热键：user32 RegisterHotKey + WM_HOTKEY 消息循环 —— 等价 STranslate 所用 NHotkey。
- 模拟复制：SendInput 发送 Ctrl+C（先释放卡住的修饰键，对应原版 SendCtrlCV）。
- 剪贴板：GetClipboardSequenceNumber + CF_UNICODETEXT。
- 悬浮窗：Tkinter（标准库）置顶无边框窗口；Tkinter 缺失时降级为控制台输出。

线程模型：Win32 消息循环在后台线程；Tkinter 事件在主线程；结果更新经队列投递。
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import queue
import threading
import time
from typing import Callable, Dict, Optional, Tuple

from .base import AdapterError, PlatformAdapter, parse_hotkey_spec

try:
    import tkinter as tk
    _HAS_TK = True
except ImportError:  # pragma: no cover
    tk = None  # type: ignore
    _HAS_TK = False

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

# ---------------------------------------------------------------------------
# Win32 常量与类型
# ---------------------------------------------------------------------------
WM_HOTKEY = 0x0312
WM_QUIT = 0x0012
MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
CF_UNICODETEXT = 13
INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002

_MOD_TO_WIN = {"alt": MOD_ALT, "ctrl": MOD_CONTROL, "shift": MOD_SHIFT, "cmd": MOD_WIN}

VK: Dict[str, int] = {
    **{chr(ord("a") + i): 0x41 + i for i in range(26)},
    **{str(i): 0x30 + i for i in range(10)},
    **{f"f{i}": 0x70 + (i - 1) for i in range(1, 13)},
    "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28,
    "space": 0x20, "tab": 0x09, "return": 0x0D, "enter": 0x0D, "escape": 0x1B, "esc": 0x1B,
    "delete": 0x2E,
    "home": 0x24, "end": 0x23, "pageup": 0x21, "pagedown": 0x22, "insert": 0x2D,
    "=": 0xBB, "-": 0xBD, "[": 0xDB, "]": 0xDD, "\\": 0xDC, ";": 0xBA,
    "'": 0xDE, ",": 0xBC, ".": 0xBE, "/": 0xBF, "`": 0xC0,
}
# 注：VK_DELETE(0x2E) 与 VK_OEM_PERIOD(0xBE) 分别对应 delete 与句点键

_STUCK_MODIFIER_VKS = (0x11, 0x12, 0x5B, 0x5C, 0x10, 0xA0, 0xA1)  # ctrl/alt/win/shift/lshift/rshift


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wt.WORD), ("wScan", wt.WORD), ("dwFlags", wt.DWORD),
        ("time", wt.DWORD), ("dwExtraInfo", wt.ULONG_PTR),
    ]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("ki", _KEYBDINPUT)]


class _INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", wt.DWORD), ("u", _INPUTUNION)]


class _MSG(ctypes.Structure):
    _fields_ = [
        ("hwnd", wt.HWND), ("message", wt.UINT), ("wParam", wt.WPARAM),
        ("lParam", wt.LPARAM), ("time", wt.DWORD), ("pt", wt.POINT),
    ]


# ---------------------------------------------------------------------------
# 适配器
# ---------------------------------------------------------------------------

class WindowsAdapter(PlatformAdapter):
    name = "windows"

    def __init__(self) -> None:
        self._hotkeys: Dict[int, Tuple[str, Callable[[], None]]] = {}
        self._next_id = 1
        self._msg_thread: Optional[threading.Thread] = None
        self._msg_thread_id: Optional[int] = None
        self._q: "queue.Queue[Tuple[str, str, str]]" = queue.Queue()
        self._panels: Dict[str, object] = {}
        self._root = None

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    def run(self) -> None:
        if _HAS_TK:
            self._root = tk.Tk()
            self._root.withdraw()
            self._root.after(50, self._drain_queue)
        self._msg_thread = threading.Thread(target=self._msg_loop, daemon=True, name="win32-msg-loop")
        self._msg_thread.start()
        if _HAS_TK:
            self._root.mainloop()
        else:
            while self._msg_thread.is_alive():
                self._drain_queue()
                time.sleep(0.05)

    def stop(self) -> None:
        if self._msg_thread_id is not None:
            user32.PostThreadMessageW(self._msg_thread_id, WM_QUIT, 0, 0)
        if self._root is not None:
            self._root.after(0, self._root.quit)

    def _msg_loop(self) -> None:
        self._msg_thread_id = kernel32.GetCurrentThreadId()
        msg = _MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            if msg.message == WM_HOTKEY:
                self._dispatch(msg.wParam)

    def _dispatch(self, hotkey_id: int) -> None:
        item = self._hotkeys.get(int(hotkey_id))
        if item is None:
            return
        try:
            item[1]()
        except Exception:
            import logging
            logging.getLogger(__name__).exception("热键回调异常")

    # ------------------------------------------------------------------
    # 热键
    # ------------------------------------------------------------------
    def register_hotkey(self, spec: str, callback: Callable[[], None]) -> None:
        parsed = parse_hotkey_spec(spec)
        vk = VK.get(parsed.key)
        if vk is None:
            raise AdapterError(f"按键“{parsed.key}”在 Windows 上不受支持")
        mods = 0
        for m in parsed.modifiers:
            mods |= _MOD_TO_WIN[m]
        hotkey_id = self._next_id
        self._next_id += 1
        if not user32.RegisterHotKey(None, hotkey_id, mods, vk):
            raise AdapterError(f"注册快捷键“{spec}”失败：可能已被其他应用占用")
        self._hotkeys[hotkey_id] = (spec, callback)

    # ------------------------------------------------------------------
    # 取词
    # ------------------------------------------------------------------
    def _send_key(self, vk: int, up: bool) -> None:
        inp = _INPUT(type=INPUT_KEYBOARD, ki=_KEYBDINPUT(wVk=vk, dwFlags=KEYEVENTF_KEYUP if up else 0))
        user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(_INPUT))

    def copy_selection(self) -> None:
        for vk in _STUCK_MODIFIER_VKS:  # 释放卡键（对应原版 SendCtrlCV）
            self._send_key(vk, True)
        self._send_key(0x11, False)  # Ctrl down
        self._send_key(ord("C"), False)
        self._send_key(ord("C"), True)
        self._send_key(0x11, True)

    def clipboard_text(self) -> Optional[str]:
        if not user32.OpenClipboard(None):
            return None
        try:
            handle = user32.GetClipboardData(CF_UNICODETEXT)
            if not handle:
                return None
            ptr = kernel32.GlobalLock(handle)
            if not ptr:
                return None
            try:
                return ctypes.wstring_at(ptr)
            finally:
                kernel32.GlobalUnlock(handle)
        finally:
            user32.CloseClipboard()

    def clipboard_revision(self) -> int:
        return int(user32.GetClipboardSequenceNumber())

    # ------------------------------------------------------------------
    # 结果展示
    # ------------------------------------------------------------------
    def show_result(self, key: str, text: str) -> None:
        self._q.put(("show", key, text))

    def update_result(self, key: str, text: str) -> None:
        self._q.put(("update", key, text))

    def close_result(self, key: str) -> None:
        self._q.put(("close", key, ""))

    def _drain_queue(self) -> None:
        try:
            while True:
                op, key, text = self._q.get_nowait()
                if op == "close":
                    panel = self._panels.pop(key, None)
                    if panel is not None:
                        getattr(panel, "close")()
                elif _HAS_TK:
                    self._ensure_panel(key)
                    if op == "show":
                        self._panels[key].set_text(text, reposition=True)
                    else:
                        self._panels[key].set_text(text, reposition=False)
                else:
                    print(f"[stranslate] {text}", flush=True)
        except queue.Empty:
            pass
        if self._root is not None:
            self._root.after(50, self._drain_queue)

    def _ensure_panel(self, key: str) -> None:
        if key in self._panels:
            return
        self._panels[key] = _TkPanel(self._root)


class _TkPanel:
    WIDTH, HEIGHT = 560, 240

    def __init__(self, root: "tk.Tk") -> None:
        self.top = tk.Toplevel(root)
        self.top.overrideredirect(True)
        self.top.attributes("-topmost", True)
        self.text = tk.Text(
            self.top, wrap="word", font=("Segoe UI", 11), padx=10, pady=10,
            background="#ffffff", relief="flat", highlightthickness=1,
            highlightbackground="#c0c0c0",
        )
        self.text.pack(fill="both", expand=True)
        self.top.bind("<Escape>", lambda e: self.top.destroy())

    def set_text(self, text: str, reposition: bool) -> None:
        self.text.delete("1.0", "end")
        self.text.insert("1.0", text)
        self.top.deiconify()
        self.top.geometry(f"{self.WIDTH}x{self.HEIGHT}+{self._x()}+{self._y()}" if reposition else "")
        self.top.lift()
        self.text.see("end")

    def close(self) -> None:
        try:
            self.top.destroy()
        except Exception:
            pass

    def _cursor(self) -> Tuple[int, int]:
        pt = wt.POINT()
        user32.GetCursorPos(ctypes.byref(pt))
        return pt.x, pt.y

    def _x(self) -> int:
        sw = user32.GetSystemMetrics(0)
        return min(max(self._cursor()[0] + 12, 0), max(sw - self.WIDTH - 8, 0))

    def _y(self) -> int:
        sh = user32.GetSystemMetrics(1)
        return min(max(self._cursor()[1] + 16, 0), max(sh - self.HEIGHT - 8, 0))
