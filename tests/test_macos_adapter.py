"""macOS 适配器冒烟测试（仅 darwin 执行）。

覆盖曾在真机上暴露的问题：
- Carbon.framework 加载与原型设置（64 位 Carbon 无 NewEventHandlerUPP 符号）；
- InstallEventHandler + RegisterEventHotKey 的 ctypes 调用（结构体按值传递）；
- KVK 键名别名（enter/esc 归一化后必须可查）。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="仅 macOS 执行")

from stranslate_lite.platform.macos import KVK, MacOSAdapter  # noqa: E402


def test_adapter_constructs_and_loads_carbon():
    """适配器能加载 Carbon 并完成原型设置（历史 bug：dlsym NewEventHandlerUPP）。"""
    a = MacOSAdapter()
    assert a.name == "macos"
    assert a._target


def test_kvk_has_enter_and_esc_aliases():
    """parse_hotkey_spec 归一化为 enter/esc，KVK 必须能查到。"""
    assert KVK["enter"] == KVK["return"] == 0x24
    assert KVK["esc"] == KVK["escape"] == 0x35


def test_register_hotkey_roundtrip():
    """注册一个罕见组合键成功（OSStatus==0），并确认事件处理器已安装。

    测试进程退出时系统自动释放热键；若与其他应用冲突会给出明确错误。
    """
    a = MacOSAdapter()
    fired = []

    a.register_hotkey("ctrl+alt+shift+f16", lambda: fired.append(True))
    assert len(a._hotkeys) == 1
    assert a._handler_upp is not None

    # 二次注册同一组合应失败（模拟冲突提示路径）
    with pytest.raises(Exception, match="占用|失败"):
        a.register_hotkey("ctrl+alt+shift+f16", lambda: None)


def test_unsupported_key_raises():
    a = MacOSAdapter()
    from stranslate_lite.platform.base import AdapterError

    with pytest.raises(AdapterError):
        a.register_hotkey("alt+insert", lambda: None)  # insert 不在 macOS KVK
