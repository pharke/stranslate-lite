"""划词取词：模拟复制 + 剪贴板变更轮询。

逻辑镜像 STranslate `ClipboardHelper.GetSelectedTextImplAsync`：
1. 记录原剪贴板文本与变更序号；
2. 发送「复制」组合键；
3. 轮询序号直至变化或超时（默认 500ms，可配 50~5000ms）；
4. 序号变化后再等 30ms 确保内容完整写入；
5. 序号变化 / 内容变化 / 原本为空 → 返回当前文本（Trim）；否则视为取词失败返回 None。
"""

from __future__ import annotations

import time
from typing import Optional

from .config import CaptureConfig
from .platform.base import PlatformAdapter
from .prompts import postprocess_captured


class CaptureError(Exception):
    pass


def capture_selected(adapter: PlatformAdapter, cfg: CaptureConfig) -> Optional[str]:
    original = adapter.clipboard_text()
    original_rev = adapter.clipboard_revision()

    adapter.copy_selection()

    deadline = time.monotonic() + cfg.timeout_ms / 1000.0
    has_changed = False
    while time.monotonic() < deadline:
        if adapter.clipboard_revision() != original_rev:
            has_changed = True
            time.sleep(0.03)  # 序号变化后稍等，确保内容完全更新
            break
        time.sleep(0.01)

    current = adapter.clipboard_text()

    if has_changed or current != original or not original:
        return current.strip() if current else None
    return None  # 没有检测到变化


def capture_and_postprocess(adapter: PlatformAdapter, cfg: CaptureConfig) -> str:
    """取词并做统一后处理；失败抛 CaptureError。"""
    text = capture_selected(adapter, cfg)
    if not text:
        raise CaptureError(
            "未取到选中文本：请确认已授予“辅助功能”权限、选中了可复制的文本后重试。"
        )
    return postprocess_captured(text, cfg.line_break, cfg.separators, cfg.max_chars)
