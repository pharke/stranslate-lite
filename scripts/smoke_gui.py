"""macOS GUI 冒烟脚本（手动验证用，不进测试套件）。

在主线程真实运行 NSApplication 事件循环，验证：
1. 菜单栏状态项（_MenuActions 构造与属性挂载）
2. 悬浮面板创建/流式更新/关闭（_ResultPanel 全部 AppKit 调用）
3. Esc 监视器安装
4. 后台线程 → 主线程投递（_ui_after / callAfter 路径）
5. 面板几何定位（鼠标附近、屏幕边界内）
6. 点击面板外关闭（resign key 通知 → 面板销毁，对应 STranslate HideWhenDeactivated）
7. 无更新自动关闭计时（[ui].auto_close_seconds）

所有阶段定时器在 app.run() 之前一次性排入（真实应用中面板更新由
后台任务线程经 callAfter 投递，与本脚本 bg 线程路径一致）。
"""

import sys
import threading
import traceback

sys.path.insert(0, ".")

import AppKit  # noqa: E402
from Foundation import NSDefaultRunLoopMode, NSObject, NSRunLoop, NSTimer  # noqa: E402

from stranslate_lite.platform.macos import MacOSAdapter  # noqa: E402

results = []
adapter = MacOSAdapter()


def step(name, fn):
    try:
        fn()
        results.append(f"OK   {name}")
    except Exception as e:  # noqa: BLE001
        results.append(f"FAIL {name}: {e!r}\n{traceback.format_exc()}")
    print(results[-1], flush=True)


def check(name, cond, detail=""):
    results.append(f"{'OK  ' if cond else 'FAIL'} {name}{(' | ' + str(detail)) if detail else ''}")
    print(results[-1], flush=True)


class _Phases(NSObject):
    def phaseSetup_(self, timer):
        print(f"[main] isMainThread={AppKit.NSThread.isMainThread()}", flush=True)
        try:
            step("menu_bar", adapter._setup_menu_bar)
            step("esc_monitor", adapter._install_esc_monitor)
            # 主线程路径：_ui_after 直接执行
            step("show_result_main", lambda: adapter.show_result("job-1", "⏳ 调用中…"))
            step("update_result_main", lambda: adapter.update_result("job-1", "流式回答（主线程路径）"))
            check("panel_created", "job-1" in adapter._panels, f"panels={list(adapter._panels)}")
            # 后台线程路径：经 callAfter 投递（真实应用的任务线程模式）
            self._done = threading.Event()

            def bg():
                try:
                    adapter.update_result("job-1", "流式回答（后台线程路径）")
                    adapter.show_result("job-2", "第二个面板")
                except Exception as e:  # noqa: BLE001
                    results.append(f"FAIL bg: {e!r}")
                finally:
                    self._done.set()

            threading.Thread(target=bg, daemon=True).start()
        except Exception as e:  # noqa: BLE001
            results.append(f"FAIL phaseSetup: {e!r}\n{traceback.format_exc()}")
            print(results[-1], flush=True)

    def phaseVerify_(self, timer):
        try:
            p1 = adapter._panels.get("job-1")
            text1 = p1.text_view.string() if p1 is not None else ""
            check("bg_update_applied", "后台线程路径" in text1, text1)
            check("bg_show_applied", "job-2" in adapter._panels, f"panels={list(adapter._panels)}")

            frame = adapter._panels["job-2"].ns_panel.frame()
            screen = AppKit.NSScreen.mainScreen().visibleFrame()
            inside = (
                frame.origin.x >= screen.origin.x - 1
                and frame.origin.y >= screen.origin.y - 1
                and frame.origin.x + frame.size.width <= screen.origin.x + screen.size.width + 1
                and frame.origin.y + frame.size.height <= screen.origin.y + screen.size.height + 1
            )
            check("geometry_inside_screen", inside, f"origin={frame.origin.x:.0f},{frame.origin.y:.0f} size={frame.size.width:.0f}x{frame.size.height:.0f}")

            step("close_result_main", lambda: adapter.close_result("job-2"))
            check("panel_closed", "job-2" not in adapter._panels)
        except Exception as e:  # noqa: BLE001
            results.append(f"FAIL phaseVerify: {e!r}\n{traceback.format_exc()}")
            print(results[-1], flush=True)

    def phaseResign_(self, timer):
        """点击面板外 → resign key 通知 → 面板销毁。"""
        try:
            adapter.show_result("job-3", "失焦关闭测试")
            panel = adapter._panels.get("job-3")
            check("resign_panel_shown", panel is not None)
            if panel is not None:
                AppKit.NSNotificationCenter.defaultCenter().postNotificationName_object_(
                    AppKit.NSWindowDidResignKeyNotification, panel.ns_panel
                )
            check("resign_closed_panel", "job-3" not in adapter._panels, f"panels={list(adapter._panels)}")
        except Exception as e:  # noqa: BLE001
            results.append(f"FAIL phaseResign: {e!r}\n{traceback.format_exc()}")
            print(results[-1], flush=True)

    def phaseAutoClose_(self, timer):
        """无更新 1 秒自动关闭：show 后 1 秒应消失。"""
        try:
            adapter.set_auto_close_seconds(1.0)
            adapter.show_result("job-4", "自动关闭测试")
            check("autoclose_panel_shown", "job-4" in adapter._panels, f"panels={list(adapter._panels)}")
        except Exception as e:  # noqa: BLE001
            results.append(f"FAIL phaseAutoClose: {e!r}\n{traceback.format_exc()}")
            print(results[-1], flush=True)

    def phaseAutoCloseVerify_(self, timer):
        try:
            check("autoclose_panel_closed", "job-4" not in adapter._panels, f"panels={list(adapter._panels)}")
        except Exception as e:  # noqa: BLE001
            results.append(f"FAIL phaseAutoCloseVerify: {e!r}\n{traceback.format_exc()}")
            print(results[-1], flush=True)
        fails = [r for r in results if r.startswith("FAIL")]
        print("SMOKE DONE", "FAILURES=%d" % len(fails), flush=True)
        AppKit.NSApp.terminate_(None)


def main():
    app = AppKit.NSApplication.sharedApplication()
    app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyAccessory)
    p = _Phases.alloc().init()
    loop = NSRunLoop.currentRunLoop()
    for delay, sel in (
        (0.2, "phaseSetup:"),
        (1.6, "phaseVerify:"),
        (2.2, "phaseResign:"),
        (2.8, "phaseAutoClose:"),
        (4.4, "phaseAutoCloseVerify:"),
    ):
        t = NSTimer.timerWithTimeInterval_target_selector_userInfo_repeats_(delay, p, sel, None, False)
        loop.addTimer_forMode_(t, NSDefaultRunLoopMode)
    app.run()


if __name__ == "__main__":
    main()
