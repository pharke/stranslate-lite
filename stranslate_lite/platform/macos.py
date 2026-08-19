"""macOS 平台适配器。

技术映射（对照 STranslate Windows 实现）：
- 全局热键：Carbon RegisterEventHotKey（系统级注册，吞掉组合键，无需任何权限）
  —— 等价 Win32 RegisterHotKey / NHotkey。用 ctypes 直接调用 Carbon，避免依赖
  pyobjc 对 C 结构体的元数据支持。
- 模拟复制：Quartz CGEventPost 发送 Cmd+C —— 等价 SendInput 模拟 Ctrl+C。
  需要「辅助功能」权限（macOS 平台约束，PopClip/Bob 等同类工具一致）。
  发送前先释放卡住的修饰键（对应原版 SendCtrlCV 的 KeyUp 清理）。
- 剪贴板：NSPasteboard.changeCount 轮询 —— 等价 GetClipboardSequenceNumber。
- 悬浮窗：非激活 NSPanel（NSNonactivatingPanelMask + Floating 层级，不抢焦点），
  内容为可滚动可复制的 NSTextView，流式更新。
- 菜单栏常驻入口：NSStatusItem（打开配置目录 / 检查权限 / 退出）。

线程模型：LLM 请求在后台线程执行；所有 AppKit 对象的创建与修改都通过
PyObjCTools.AppHelper.callAfter 投递到主线程执行。
"""

from __future__ import annotations

import ctypes
import ctypes.util
import logging
import struct
from typing import Callable, Dict, List, Optional, Tuple

from .base import AdapterError, PlatformAdapter, parse_hotkey_spec

logger = logging.getLogger(__name__)

try:
    import AppKit
    import Quartz
    from ApplicationServices import AXIsProcessTrusted
    from Foundation import NSPointInRect, NSObject
    from PyObjCTools.AppHelper import callAfter
except ImportError as e:  # pragma: no cover - 仅在 macOS 上执行
    raise ImportError(
        "缺少 pyobjc 依赖。请执行：pip install 'stranslate-lite'（在 macOS 上会自动安装 pyobjc-framework-Cocoa/Quartz/ApplicationServices）"
    ) from e

# ---------------------------------------------------------------------------
# macOS 虚拟键码（Events.h kVK_*）
# ---------------------------------------------------------------------------
KVK: Dict[str, int] = {
    "a": 0x00, "s": 0x01, "d": 0x02, "f": 0x03, "h": 0x04, "g": 0x05, "z": 0x06, "x": 0x07,
    "c": 0x08, "v": 0x09, "b": 0x0B, "q": 0x0C, "w": 0x0D, "e": 0x0E, "r": 0x0F, "y": 0x10,
    "t": 0x11, "1": 0x12, "2": 0x13, "3": 0x14, "4": 0x15, "6": 0x16, "5": 0x17, "=": 0x18,
    "9": 0x19, "7": 0x1A, "-": 0x1B, "8": 0x1C, "0": 0x1D, "]": 0x1E, "o": 0x1F, "u": 0x20,
    "[": 0x21, "i": 0x22, "p": 0x23, "return": 0x24, "l": 0x25, "j": 0x26, "'": 0x27, "k": 0x28,
    ";": 0x29, "\\": 0x2A, ",": 0x2B, "/": 0x2C, "n": 0x2D, "m": 0x2E, ".": 0x2F, "tab": 0x30,
    "space": 0x31, "`": 0x32, "delete": 0x33, "escape": 0x35, "f1": 0x7A, "f2": 0x78, "f3": 0x63,
    "f4": 0x76, "f5": 0x60, "f6": 0x61, "f7": 0x62, "f8": 0x64, "f9": 0x65, "f10": 0x6D,
    "f11": 0x67, "f12": 0x6F, "f13": 0x69, "f14": 0x6B, "f15": 0x71, "f16": 0x6A, "home": 0x73,
    "end": 0x77, "pageup": 0x74, "pagedown": 0x79, "left": 0x7B, "right": 0x7C, "down": 0x7D, "up": 0x7E,
}

# Carbon 修饰键掩码（Events.h）
CMD_KEY = 1 << 8       # 256
SHIFT_KEY = 1 << 9     # 512
OPTION_KEY = 1 << 11   # 2048
CONTROL_KEY = 1 << 12  # 4096

_MOD_TO_CARBON = {
    "cmd": CMD_KEY,
    "shift": SHIFT_KEY,
    "alt": OPTION_KEY,
    "ctrl": CONTROL_KEY,
}

# 需要预先「释放」的修饰键（对应原版 SendCtrlCV 的清理逻辑）
_STUCK_MODIFIER_VKS = (0x37, 0x3A, 0x3B, 0x38, 0x3C, 0x3D, 0x3E)  # cmd/opt/ctrl/shift/rshift/ropt/rctrl

# ---------------------------------------------------------------------------
# Carbon C 接口（ctypes，避免 pyobjc 结构体元数据风险）
# ---------------------------------------------------------------------------


class _EventHotKeyID(ctypes.Structure):
    _fields_ = [("signature", ctypes.c_uint32), ("id", ctypes.c_uint32)]


class _EventTypeSpec(ctypes.Structure):
    _fields_ = [("eventClass", ctypes.c_uint32), ("eventKind", ctypes.c_uint32)]


_EventHandlerUPP = ctypes.CFUNCTYPE(ctypes.c_int32, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)

_kEventClassKeyboard = struct.unpack(">I", b"keyb")[0]
_kEventHotKeyPressed = 5
_kEventParamDirectObject = struct.unpack(">I", b"----")[0]
_typeEventHotKeyID = struct.unpack(">I", b"hkID")[0]
_SIGNATURE = struct.unpack(">I", b"SLIT")[0]


def _load_carbon():
    paths = ["/System/Library/Frameworks/Carbon.framework/Carbon"]
    for p in paths:
        try:
            return ctypes.CDLL(p)
        except OSError:
            continue
    raise AdapterError("无法加载 Carbon.framework，全局热键不可用")


class MacOSAdapter(PlatformAdapter):
    name = "macos"

    def __init__(self) -> None:
        self._carbon = _load_carbon()
        self._setup_carbon_prototypes()
        self._target = self._carbon.GetEventDispatcherTarget() or self._carbon.GetApplicationEventTarget()
        if not self._target:
            raise AdapterError("无法获取 Carbon 事件目标")

        self._hotkeys: Dict[int, Tuple[str, Callable[[], None]]] = {}
        self._hotkey_refs: Dict[int, int] = {}
        self._next_id = 1
        self._handler_upp: Optional[int] = None
        self._handler_ref = ctypes.c_void_p()
        self._esc_monitor: Optional[object] = None
        self._panels: Dict[str, "_ResultPanel"] = {}
        self._menu_actions: Optional[AppKit.NSObject] = None
        self._status_item: Optional[AppKit.NSStatusItem] = None
        self._callback = _EventHandlerUPP(self._handle_event)  # 防 GC

    # ------------------------------------------------------------------
    # Carbon 原型
    # ------------------------------------------------------------------
    def _setup_carbon_prototypes(self) -> None:
        c = self._carbon
        c.RegisterEventHotKey.argtypes = [
            ctypes.c_uint32, ctypes.c_uint32, _EventHotKeyID, ctypes.c_void_p, ctypes.c_uint32,
            ctypes.POINTER(ctypes.c_void_p),
        ]
        c.RegisterEventHotKey.restype = ctypes.c_int32
        c.UnregisterEventHotKey.argtypes = [ctypes.c_void_p]
        c.UnregisterEventHotKey.restype = ctypes.c_int32
        c.GetEventDispatcherTarget.argtypes = []
        c.GetEventDispatcherTarget.restype = ctypes.c_void_p
        c.GetApplicationEventTarget.argtypes = []
        c.GetApplicationEventTarget.restype = ctypes.c_void_p
        c.NewEventHandlerUPP.argtypes = [_EventHandlerUPP]
        c.NewEventHandlerUPP.restype = ctypes.c_void_p
        c.DisposeEventHandlerUPP.argtypes = [ctypes.c_void_p]
        c.InstallEventHandler.argtypes = [
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(_EventTypeSpec),
            ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p),
        ]
        c.InstallEventHandler.restype = ctypes.c_int32
        c.RemoveEventHandler.argtypes = [ctypes.c_void_p]
        c.RemoveEventHandler.restype = ctypes.c_int32
        c.GetEventParameter.argtypes = [
            ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint32),
            ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint32), ctypes.c_void_p,
        ]
        c.GetEventParameter.restype = ctypes.c_int32

    def _handle_event(self, next_handler: int, the_event: int, user_data: int) -> int:
        """Carbon 热键事件回调（主线程执行）。"""
        try:
            hk_id = _EventHotKeyID()
            status = self._carbon.GetEventParameter(
                the_event, _kEventParamDirectObject, _typeEventHotKeyID,
                None, ctypes.sizeof(_EventHotKeyID), None,
                ctypes.cast(ctypes.byref(hk_id), ctypes.c_void_p),
            )
            if status == 0:
                self._dispatch_hotkey(hk_id.id)
        except Exception:  # 回调内必须吞异常
            logger.exception("热键事件处理异常")
        return 0  # noErr

    def _dispatch_hotkey(self, hotkey_id: int) -> None:
        item = self._hotkeys.get(hotkey_id)
        if item is None:
            return
        _, callback = item
        try:
            callback()
        except Exception:
            logger.exception("热键回调异常")

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    def run(self) -> None:
        app = AppKit.NSApplication.sharedApplication()
        app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyAccessory)
        self._setup_menu_bar()
        self._install_esc_monitor()
        logger.info("进入 macOS 事件循环")
        app.run()

    def stop(self) -> None:
        def _stop():
            AppKit.NSApp.terminate_(None)
        callAfter(_stop)

    def _install_event_handler(self) -> None:
        if self._handler_upp is not None:
            return
        upp = self._carbon.NewEventHandlerUPP(self._callback)
        spec = (_EventTypeSpec * 1)(_EventTypeSpec(_kEventClassKeyboard, _kEventHotKeyPressed))
        status = self._carbon.InstallEventHandler(
            self._target, upp, 1, spec, None, ctypes.byref(self._handler_ref)
        )
        if status != 0:
            raise AdapterError(f"安装 Carbon 事件处理器失败（OSStatus={status}）")
        self._handler_upp = upp

    # ------------------------------------------------------------------
    # 热键
    # ------------------------------------------------------------------
    def register_hotkey(self, spec: str, callback: Callable[[], None]) -> None:
        parsed = parse_hotkey_spec(spec)
        kv = KVK.get(parsed.key)
        if kv is None:
            raise AdapterError(f"按键“{parsed.key}”在 macOS 上不受支持")
        mods = 0
        for m in parsed.modifiers:
            mods |= _MOD_TO_CARBON[m]
        if not mods:
            raise AdapterError(f"快捷键“{spec}”至少需要一个修饰键")

        self._install_event_handler()
        hotkey_id = self._next_id
        self._next_id += 1
        ref = ctypes.c_void_p()
        status = self._carbon.RegisterEventHotKey(
            kv, mods, _EventHotKeyID(_SIGNATURE, hotkey_id), self._target, 0, ctypes.byref(ref)
        )
        if status != 0:
            raise AdapterError(
                f"注册快捷键“{spec}”失败（OSStatus={status}）：可能已被其他应用占用"
            )
        self._hotkeys[hotkey_id] = (spec, callback)
        self._hotkey_refs[hotkey_id] = ref.value or 0

    # ------------------------------------------------------------------
    # 取词
    # ------------------------------------------------------------------
    def copy_selection(self) -> None:
        self._release_stuck_modifiers()
        self._post_combo(KVK["c"], Quartz.kCGEventFlagMaskCommand)

    def _release_stuck_modifiers(self) -> None:
        """发送复制前释放卡住的修饰键（对齐原版 SendCtrlCV）。"""
        src = Quartz.CGEventSourceCreate(Quartz.kCGEventSourceStateCombinedSessionState)
        for kv in _STUCK_MODIFIER_VKS:
            up = Quartz.CGEventCreateKeyboardEvent(src, kv, False)
            Quartz.CGEventPost(Quartz.kCGHIDEventTap, up)

    def _post_combo(self, kv: int, flags: int) -> None:
        src = Quartz.CGEventSourceCreate(Quartz.kCGEventSourceStateCombinedSessionState)
        down = Quartz.CGEventCreateKeyboardEvent(src, kv, True)
        up = Quartz.CGEventCreateKeyboardEvent(src, kv, False)
        Quartz.CGEventSetFlags(down, flags)
        Quartz.CGEventSetFlags(up, flags)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, down)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, up)

    def clipboard_text(self) -> Optional[str]:
        pb = AppKit.NSPasteboard.generalPasteboard()
        for ptype in (AppKit.NSPasteboardTypeString, "NSStringPboardType"):
            text = pb.stringForType_(ptype)
            if text is not None:
                return str(text)
        return None

    def clipboard_revision(self) -> int:
        return int(AppKit.NSPasteboard.generalPasteboard().changeCount())

    # ------------------------------------------------------------------
    # 结果展示（后台线程安全：内部投递主线程）
    # ------------------------------------------------------------------
    def show_result(self, key: str, text: str) -> None:
        callAfter(self._show_on_main, key, text)

    def update_result(self, key: str, text: str) -> None:
        callAfter(self._update_on_main, key, text)

    def close_result(self, key: str) -> None:
        callAfter(self._close_on_main, key)

    def _show_on_main(self, key: str, text: str) -> None:
        panel = self._panels.get(key)
        if panel is None:
            panel = _ResultPanel()
            self._panels[key] = panel
        panel.set_text(text)
        panel.show()

    def _update_on_main(self, key: str, text: str) -> None:
        panel = self._panels.get(key)
        if panel is None:
            self._show_on_main(key, text)
            return
        panel.set_text(text)

    def _close_on_main(self, key: str) -> None:
        panel = self._panels.pop(key, None)
        if panel is not None:
            panel.close()

    def _install_esc_monitor(self) -> None:
        def _monitor(event):
            for key, panel in list(self._panels.items()):
                if event.window() == panel.ns_panel and event.keyCode() == 53:  # Esc
                    del self._panels[key]
                    panel.close()
                    return None  # 消费事件
            return event

        self._esc_handler = _monitor  # 防 GC
        self._esc_monitor = AppKit.NSEvent.addLocalMonitorForEventsMatchingMask_(AppKit.NSKeyDownMask)
        # 注：addLocalMonitor 返回的对象与 handler 都必须保持引用

    # ------------------------------------------------------------------
    # 菜单栏
    # ------------------------------------------------------------------
    def _setup_menu_bar(self) -> None:
        self._menu_actions = _MenuActions(self)

        bar = AppKit.NSStatusBar.systemStatusBar()
        item = bar.statusItemWithLength_(AppKit.NSVariableStatusItemLength)
        item.button().setTitle_("译")

        menu = AppKit.NSMenu.alloc().init()

        def add(title: str, action: str, key_equiv: str) -> None:
            mi = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(title, action, key_equiv)
            mi.setTarget_(self._menu_actions)
            menu.addItem_(mi)

        add("打开配置文件所在文件夹", "openConfigFolderAction:", "")
        add("检查辅助功能权限", "openPrivacyAction:", "")
        menu.addItem_(AppKit.NSMenuItem.separatorItem())
        add("退出", "quitAction:", "q")

        item.setMenu_(menu)
        self._status_item = item

    # ------------------------------------------------------------------
    # 杂项
    # ------------------------------------------------------------------
    def permission_issues(self) -> List[str]:
        issues: List[str] = []
        try:
            trusted = bool(AXIsProcessTrusted())
        except Exception:
            trusted = False
        if not trusted:
            issues.append(
                "未授予“辅助功能”权限：模拟 Cmd+C 取词将被系统拦截。"
                "请在 系统设置 → 隐私与安全性 → 辅助功能 中勾选运行本程序的终端/应用（授权后需重启本程序）。"
            )
        return issues


class _ResultPanel:
    """非激活置顶面板：NSScrollView + NSTextView，可滚动、可选中复制。"""

    WIDTH, HEIGHT = 560.0, 240.0

    def __init__(self) -> None:
        mask = (
            AppKit.NSTitledWindowMask | AppKit.NSClosableWindowMask
            | AppKit.NSResizableWindowMask | AppKit.NSNonactivatingPanelMask
        )
        self.ns_panel = AppKit.NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            ((0.0, 0.0), (self.WIDTH, self.HEIGHT)), mask, AppKit.NSBackingStoreBuffered, False
        )
        self.ns_panel.setLevel_(AppKit.NSFloatingWindowLevel)
        self.ns_panel.setBecomesKeyOnlyIfNeeded_(True)
        self.ns_panel.setHidesOnDeactivate_(False)
        self.ns_panel.setReleasedWhenClosed_(False)

        scroll = AppKit.NSScrollView.alloc().initWithFrame_(((0.0, 0.0), (self.WIDTH, self.HEIGHT)))
        scroll.setBorderType_(AppKit.NSNoBorder)
        scroll.setHasVerticalScroller_(True)
        scroll.setHasHorizontalScroller_(False)
        scroll.setAutohidesScrollers_(True)
        scroll.setAutoresizingMask_(AppKit.NSViewWidthSizable | AppKit.NSViewHeightSizable)

        content_size = scroll.contentSize()
        self.text_view = AppKit.NSTextView.alloc().initWithFrame_(
            ((0.0, 0.0), (content_size.width, content_size.height))
        )
        self.text_view.setMinSize_((0.0, content_size.height))
        self.text_view.setMaxSize_((1.0e7, 1.0e7))
        self.text_view.setVerticallyResizable_(True)
        self.text_view.setHorizontallyResizable_(False)
        self.text_view.setAutoresizingMask_(AppKit.NSViewWidthSizable)
        self.text_view.setTextContainerInset_((10.0, 10.0))
        self.text_view.setEditable_(False)
        self.text_view.setSelectable_(True)
        self.text_view.setRichText_(False)
        self.text_view.setFont_(AppKit.NSFont.systemFontOfSize_(13.0))
        self.text_view.setBackgroundColor_(AppKit.NSColor.windowBackgroundColor())
        self.text_view.textContainer().setContainerSize_((content_size.width, 1.0e7))
        self.text_view.textContainer().setWidthTracksTextView_(True)
        scroll.setDocumentView_(self.text_view)
        self.ns_panel.setContentView_(scroll)

    def set_text(self, text: str) -> None:
        self.text_view.setString_(text)
        length = len(text)
        self.text_view.scrollRangeToVisible_((length, 0))

    def show(self) -> None:
        x, y = self._position()
        self.ns_panel.setFrameOrigin_((x, y))
        self.ns_panel.orderFrontRegardless()

    def _position(self) -> Tuple[float, float]:
        loc = AppKit.NSEvent.mouseLocation()  # 全局坐标（左下原点）
        target_vis = None
        for screen in AppKit.NSScreen.screens():
            if NSPointInRect(loc, screen.frame()):
                target_vis = screen.visibleFrame()
                break
        if target_vis is None:
            target_vis = AppKit.NSScreen.mainScreen().visibleFrame()
        x = loc.x + 12.0
        y = loc.y - 16.0 - self.HEIGHT  # 面板出现在鼠标上方
        x = min(max(x, target_vis.origin.x + 8.0), target_vis.origin.x + target_vis.size.width - self.WIDTH - 8.0)
        y = min(max(y, target_vis.origin.y + 8.0), target_vis.origin.y + target_vis.size.height - self.HEIGHT - 8.0)
        return x, y

    def close(self) -> None:
        self.ns_panel.close()


class _MenuActions(NSObject):
    def __init__(self, adapter: MacOSAdapter) -> None:
        super().__init__()
        self._adapter = adapter

    def openConfigFolderAction_(self, sender) -> None:  # noqa: N802
        import os
        from ..config import config_path
        path = str(config_path())
        if path and os.path.exists(path):
            AppKit.NSWorkspace.sharedWorkspace().activateFileViewerSelectingPaths_([path])

    def openPrivacyAction_(self, sender) -> None:  # noqa: N802
        url = AppKit.NSURL.URLWithString_("x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility")
        if url is not None:
            AppKit.NSWorkspace.sharedWorkspace().openURL_(url)

    def quitAction_(self, sender) -> None:  # noqa: N802
        AppKit.NSApp.terminate_(None)
