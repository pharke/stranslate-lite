"""提示词模板渲染与取词文本后处理。

占位符与原版 STranslate 保持一致：$source、$target、$content。
替换顺序与 STranslate 相同：先 source/target，后 content，
避免 content 内部出现的占位符被二次替换。
"""

from __future__ import annotations

import re
from typing import Dict, List

from .config import Prompt


def render_messages(prompt: Prompt, content: str, source: str, target: str) -> List[Dict[str, str]]:
    """渲染提示词为 OpenAI 兼容的 messages 列表。"""
    messages: List[Dict[str, str]] = []
    for m in prompt.messages:
        text = (
            m.content.replace("$source", source)
            .replace("$target", target)
            .replace("$content", content)
        )
        messages.append({"role": m.role, "content": text})
    return messages


# --------------------------------------------------------------------------
# 取词文本后处理（对应 STranslate Utilities.CapturedTextHandler）
# --------------------------------------------------------------------------

_WORD_INTERNAL_UNDERSCORE = re.compile(r"(?<=[\w])_(?=[\w])")
_WORD_INTERNAL_HYPHEN = re.compile(r"(?<=[\w])-(?=[\w])")


def line_break_handler(text: str, mode: str) -> str:
    if mode == "remove":
        return re.sub(r"[\r\n]+", "", text)
    if mode == "space":
        return re.sub(r"[\r\n]+", " ", text)
    return text  # keep


def separator_handler(text: str, mode: str) -> str:
    if mode in ("underscore", "both"):
        text = _WORD_INTERNAL_UNDERSCORE.sub(" ", text)
    if mode in ("hyphen", "both"):
        text = _WORD_INTERNAL_HYPHEN.sub(" ", text)
    return text


def postprocess_captured(text: str, line_break: str = "keep", separators: str = "none", max_chars: int = 8000) -> str:
    """取词文本统一后处理：Trim → 换行处理 → 分隔符处理 → 长度截断。"""
    text = text.strip()
    if not text:
        return text
    text = line_break_handler(text, line_break)
    text = separator_handler(text, separators)
    if len(text) > max_chars:
        text = text[:max_chars]
    return text
