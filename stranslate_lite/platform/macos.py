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

线程模型：LLM 请求在后台线程执行；所有 AppKit 对象的创建与修改都只发生在
主线程——后台线程经 callAfter 投递，主线程路径直接执行（见 _ui_after）。
"""

from __future__ import annotations

import ctypes
import logging
import struct
import threading
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
    "[": 0x21, "i": 0x22, "p": 0x23, "return": 0x24, "enter": 0x24, "l": 0x25, "j": 0x26,
    "'": 0x27, "k": 0x28, ";": 0x29, "\\": 0x2A, ",": 0x2B, "/": 0x2C, "n": 0x2D, "m": 0x2E,
    ".": 0x2F, "tab": 0x30, "space": 0x31, "`": 0x32, "delete": 0x33, "escape": 0x35,
    "esc": 0x35, "f1": 0x7A, "f2": 0x78, "f3": 0x63,
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
# 实测 macOS 26 投递的 EventHotKeyID 参数类型为 'hkid'（小写）；历史文档
# 常写作 'hkID'。优先用实测值，读不到再回退旧写法。
_typeEventHotKeyID = struct.unpack(">I", b"hkid")[0]
_typeEventHotKeyID_LEGACY = struct.unpack(">I", b"hkID")[0]
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
        # 结果面板 UI 节流：后台线程的流式增量先合并，再按主循环节拍投递
        self._ui_lock = threading.Lock()
        self._latest: Dict[str, str] = {}
        self._scheduled: set = set()
        # 已关闭的任务序号（key 形如 "job-N"）。粘性集合：面板关闭后
        # 迟到的 flush/show 不得把它“复活”；只保留最近 512 个，防无限增长。
        self._closed: set = set()
        # 面板自动关闭（对齐 STranslate HideWhenDeactivated + 兜底定时）：
        # - 点击面板外（resign key）立即关闭；
        # - 无更新 auto_close_seconds 秒后自动关闭（0 = 永不，流式期间重置）。
        self._auto_close_seconds: float = 0.0
        self._auto_close_timers: Dict[str, AppKit.NSTimer] = {}
        self._timer_actions: Optional[AppKit.NSObject] = None
        self._panel_observer: Optional[AppKit.NSObject] = None
        self._panel_key_by_window: Dict[object, str] = {}

    @staticmethod
    def _key_num(key: str) -> int:
        try:
            return int(key.rsplit("-", 1)[1])
        except (IndexError, ValueError):  # 兼容非 job-N 形式的 key
            return 0

    def _mark_closed(self, key: str) -> None:
        num = self._key_num(key)
        self._closed.add(num)
        if len(self._closed) > 512:  # 窗口剪枝
            self._closed = {n for n in self._closed if n > num - 256}

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
        # 注：64 位 Carbon 中 UPP（通用过程指针）已退化为普通函数指针，
        # NewEventHandlerUPP/DisposeEventHandlerUPP 只是头文件里的宏，
        # 在 Carbon.framework 中没有对应 dlsym 符号——直接把 CFUNCTYPE 回调
        # 指针传给 InstallEventHandler 即可，不能按符号加载。
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
            status = -1
            # macOS 26 实测参数类型为 'hkid'；旧系统可能是 'hkID'，依次尝试
            for typ in (_typeEventHotKeyID, _typeEventHotKeyID_LEGACY):
                status = self._carbon.GetEventParameter(
                    the_event, _kEventParamDirectObject, typ,
                    None, ctypes.sizeof(_EventHotKeyID), None,
                    ctypes.cast(ctypes.byref(hk_id), ctypes.c_void_p),
                )
                if status == 0:
                    break
            if status == 0:
                self._dispatch_hotkey(hk_id.id)
            else:
                logger.debug("热键事件参数读取失败（OSStatus=%s），忽略", status)
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
        self._install_sigint_handler()
        logger.info("进入 macOS 事件循环")
        app.run()

    def _install_sigint_handler(self) -> None:
        """让 Ctrl+C（SIGINT）能打断空闲的 NSApplication 运行循环。

        运行循环空闲时阻塞在 C 层，Python 默认的 SIGINT 不会触发
        KeyboardInterrupt；改用 MachSignals 经 mach port 投递到主线程
        （等价 pyobjc runEventLoop 的 installInterrupt 机制），使
        Ctrl+C 在终端前台运行时立即可靠退出。
        """
        try:
            import signal as _signal
            from PyObjCTools import MachSignals
            from PyObjCTools.AppHelper import machInterrupt

            MachSignals.signal(_signal.SIGINT, machInterrupt)
        except Exception:  # pragma: no cover - 防御性兜底
            logger.exception("安装 SIGINT 处理器失败（Ctrl+C 退出将不可用，请用菜单栏退出）")

    def stop(self) -> None:
        self._ui_after(lambda: AppKit.NSApp.terminate_(None))

    def _install_event_handler(self) -> None:
        if self._handler_upp is not None:
            return
        # 64 位 Carbon：UPP 即普通函数指针，直接投递 CFUNCTYPE 指针
        upp = ctypes.cast(self._callback, ctypes.c_void_p)
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
    def _ui_after(self, fn: Callable, *args) -> None:
        """把 fn 投递到主线程执行。

        后台线程经 AppHelper.callAfter 投递；主线程直接执行。
        实测 pyobjc 12.2 / macOS 26：主线程上调用 performSelectorOnMainThread
        （callAfter 的底层实现）不会被 NSApplication 运行循环处理——主线程
        直接执行既规避该问题，也少一次排队。所有 AppKit 对象的创建与修改
        因此仍然只发生在主线程。
        """
        if AppKit.NSThread.isMainThread():
            fn(*args)
        else:
            callAfter(fn, *args)

    def show_result(self, key: str, text: str) -> None:
        with self._ui_lock:
            self._latest[key] = text
        self._ui_after(self._show_on_main, key, text)

    def update_result(self, key: str, text: str) -> None:
        """流式更新（后台线程可调）：合并到最近一次未投递的最新文本，避免
        每个 token 都往主线程投递一次阻塞 UI。"""
        with self._ui_lock:
            self._latest[key] = text
            if key in self._scheduled or self._key_num(key) in self._closed:
                return
            self._scheduled.add(key)

        def _flush() -> None:
            with self._ui_lock:
                self._scheduled.discard(key)
                latest = self._latest.get(key)
            if latest is not None and self._key_num(key) not in self._closed:
                self._update_on_main(key, latest)

        self._ui_after(_flush)

    def close_result(self, key: str) -> None:
        with self._ui_lock:
            self._mark_closed(key)
            self._latest.pop(key, None)
            self._scheduled.discard(key)  # 已在队列中的 flush 会被 _closed 挡下
        self._ui_after(self._close_on_main, key)

    def set_auto_close_seconds(self, seconds: float) -> None:
        """结果面板无更新 N 秒后自动关闭（0 = 永不）。"""
        self._auto_close_seconds = float(seconds)

    def _show_on_main(self, key: str, text: str) -> None:
        if self._key_num(key) in self._closed:
            return  # 面板已被关闭（迟到的 callAfter）
        panel = self._panels.get(key)
        if panel is None:
            panel = _ResultPanel()
            self._panels[key] = panel
            if self._panel_observer is None:
                observer = _PanelObserver.alloc().init()
                observer._adapter = self  # type: ignore[attr-defined]
                self._panel_observer = observer
            # 点击面板外（resign key）→ 关闭（对应 STranslate HideWhenDeactivated）
            AppKit.NSNotificationCenter.defaultCenter().addObserver_selector_name_object_(
                self._panel_observer, "panelResignedKey:", AppKit.NSWindowDidResignKeyNotification,
                panel.ns_panel,
            )
            self._panel_key_by_window[panel.ns_panel] = key
        panel.set_text(text)
        panel.show()
        self._reset_auto_close(key)

    def _update_on_main(self, key: str, text: str) -> None:
        panel = self._panels.get(key)
        if panel is None:
            self._show_on_main(key, text)
            return
        panel.set_text(text)
        self._reset_auto_close(key)  # 流式期间每次更新重置自动关闭计时

    def _close_on_main(self, key: str) -> None:
        panel = self._panels.pop(key, None)
        if panel is not None:
            self._destroy_panel(key, panel)

    def _destroy_panel(self, key: str, panel: "_ResultPanel") -> None:
        self._cancel_auto_close(key)
        self._panel_key_by_window.pop(panel.ns_panel, None)
        if self._panel_observer is not None:
            AppKit.NSNotificationCenter.defaultCenter().removeObserver_name_object_(
                self._panel_observer, AppKit.NSWindowDidResignKeyNotification, panel.ns_panel
            )
        panel.close()

    # ------------------------------------------------------------------
    # 面板自动关闭（主线程）
    # ------------------------------------------------------------------
    def _reset_auto_close(self, key: str) -> None:
        if self._auto_close_seconds <= 0:
            return
        old = self._auto_close_timers.pop(key, None)
        if old is not None:
            old.invalidate()
        if self._timer_actions is None:
            actions = _TimerActions.alloc().init()
            actions._adapter = self  # type: ignore[attr-defined]
            self._timer_actions = actions
        timer = AppKit.NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            self._auto_close_seconds, self._timer_actions, "autoClose:", key, False
        )
        self._auto_close_timers[key] = timer

    def _cancel_auto_close(self, key: str) -> None:
        timer = self._auto_close_timers.pop(key, None)
        if timer is not None:
            timer.invalidate()

    def _install_esc_monitor(self) -> None:
        def _monitor(event):
            for key, panel in list(self._panels.items()):
                if event.window() == panel.ns_panel and event.keyCode() == 53:  # Esc
                    # 标记为已关闭：key 每次任务唯一，粘性集合可挡下
                    # 已在队列中的 flush / 迟到的 show，防止面板被“复活”
                    with self._ui_lock:
                        self._mark_closed(key)
                    del self._panels[key]
                    self._destroy_panel(key, panel)
                    return None  # 消费事件
            return event

        self._esc_handler = _monitor  # 防 GC
        # 注意必须显式传 mask：pyobjc 不会自动补 NSKeyDownMask，漏传时监视器匹配不到任何按键
        self._esc_monitor = AppKit.NSEvent.addLocalMonitorForEventsMatchingMask_handler_(
            AppKit.NSKeyDownMask, _monitor
        )
        # 注：addLocalMonitor 返回的对象与 handler 都必须保持引用

    # ------------------------------------------------------------------
    # 菜单栏
    # ------------------------------------------------------------------
    def _setup_menu_bar(self) -> None:
        # 注：pyobjc 12 的通用构造路径（_genericNewClass）直接走 alloc().init()，
        # 不会调用 Python 侧的 __init__，因此这里用属性挂载 adapter 而非构造参数。
        actions = _MenuActions.alloc().init()
        actions._adapter = self  # type: ignore[attr-defined]
        self._menu_actions = actions

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


class _TimerActions(NSObject):
    """NSTimer 目标：结果面板无更新自动关闭。adapter 经实例属性挂载。"""

    def autoClose_(self, timer) -> None:  # noqa: N802
        adapter = self._adapter  # type: ignore[attr-defined]
        key = timer.userInfo()
        adapter._auto_close_timers.pop(key, None)
        adapter.close_result(key)


class _PanelObserver(NSObject):
    """观察结果面板失去 key 状态（用户点击面板外）→ 关闭面板。

    对应 STranslate 主窗口的 HideWhenDeactivated（默认 true）行为：
    失焦即隐藏，无需手动关闭。
    """

    def panelResignedKey_(self, notification) -> None:  # noqa: N802
        adapter = self._adapter  # type: ignore[attr-defined]
        panel = notification.object()
        key = adapter._panel_key_by_window.get(panel)
        if key is not None:
            adapter.close_result(key)


class _MenuActions(NSObject):
    """菜单栏动作目标。

    不定义 __init__：pyobjc 12 的通用构造路径（_genericNewClass）直接调用
    alloc().init()，Python 侧 __init__ 不会被触发；adapter 由 _setup_menu_bar
    通过实例属性挂载。
    """

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
