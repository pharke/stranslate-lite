"""提示词渲染与文本后处理测试。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from stranslate_lite.config import Message, Prompt  # noqa: E402
from stranslate_lite.prompts import (  # noqa: E402
    line_break_handler,
    postprocess_captured,
    render_messages,
    separator_handler,
)


def test_render_placeholders():
    p = Prompt(
        name="t",
        messages=[
            Message("system", "translate $source → $target"),
            Message("user", "$content"),
        ],
    )
    msgs = render_messages(p, "hello", "English", "简体中文")
    assert msgs[0]["content"] == "translate English → 简体中文"
    assert msgs[1]["content"] == "hello"


def test_content_placeholder_not_replaced_again():
    """content 内出现 $source 字面量时不应被二次替换（与 STranslate 替换顺序一致）。"""
    p = Prompt(name="t", messages=[Message("user", "$content")])
    msgs = render_messages(p, "价格是 $source 10", "English", "中文")
    assert msgs[0]["content"] == "价格是 $source 10"


def test_line_break_modes():
    assert line_break_handler("a\r\nb", "keep") == "a\r\nb"
    assert line_break_handler("a\r\nb", "remove") == "ab"
    assert line_break_handler("a\r\nb", "space") == "a b"


def test_separator_modes():
    assert separator_handler("my_var-name", "none") == "my_var-name"
    assert separator_handler("my_var-name", "underscore") == "my var-name"
    assert separator_handler("my_var-name", "hyphen") == "my_var name"
    assert separator_handler("my_var-name", "both") == "my var name"


def test_postprocess_pipeline():
    text = postprocess_captured("  hello_world\nnext-line  ", "space", "underscore", 100)
    assert text == "hello world next-line"


def test_postprocess_truncation():
    assert postprocess_captured("abcdef", "keep", "none", 3) == "abc"
