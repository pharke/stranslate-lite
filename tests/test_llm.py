"""LLM 客户端测试：URL 拼接、请求构造、SSE 解析、重试、取消。"""

import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mock_server import MockOpenAIServer  # noqa: E402

from stranslate_lite.config import ApiConfig  # noqa: E402
from stranslate_lite.llm import (  # noqa: E402
    CancelEvent,
    LlmClient,
    LlmError,
    build_chat_url,
    build_request_body,
)


# ---------------------------------------------------------------------------
# URL 拼接（移植 UrlHelper.BuildFinalUrl 的 OpenAI 规则）
# ---------------------------------------------------------------------------

def test_url_join_cases():
    assert build_chat_url("https://api.openai.com/") == "https://api.openai.com/v1/chat/completions"
    assert build_chat_url("https://api.openai.com/v1") == "https://api.openai.com/v1/chat/completions"
    # 自定义路径原样保留
    assert build_chat_url("https://api.openai.com/v1") == "https://api.openai.com/v1/chat/completions"
    assert build_chat_url("https://proxy.example.com/api/v1/chat/completions") == \
        "https://proxy.example.com/api/v1/chat/completions"
    assert build_chat_url("https://proxy.example.com/custom") == "https://proxy.example.com/custom"
    # # 强制模式
    assert build_chat_url("https://proxy.example.com/my/endpoint#") == "https://proxy.example.com/my/endpoint"


def test_request_body():
    body = build_request_body("m", [{"role": "user", "content": "hi"}], 0.7, {"top_p": 0.9})
    assert body["model"] == "m"
    assert body["stream"] is True
    assert body["temperature"] == pytest.approx(0.7)
    assert body["top_p"] == 0.9


def test_temperature_clamped():
    body = build_request_body("m", [], 5.0, {})
    assert body["temperature"] == 2.0


# ---------------------------------------------------------------------------
# 流式解析
# ---------------------------------------------------------------------------

def _client(server_url: str, **kw) -> LlmClient:
    api = ApiConfig(base_url=server_url, api_key="k", model="m", **kw)
    return LlmClient(api)


def test_sse_stream(monkeypatch):
    srv = MockOpenAIServer("sse")
    try:
        client = _client(srv.url)
        parts = []
        result = client.chat_stream([{"role": "user", "content": "hi"}], on_delta=parts.append)
        assert result == "你好，世界！"
        assert parts == ["你好，", "世界！"]
        assert srv.last_request["stream"] is True
        assert srv.last_request["messages"][0]["content"] == "hi"
    finally:
        srv.stop()


def test_think_filtered():
    srv = MockOpenAIServer("sse_think")
    try:
        client = _client(srv.url)
        result = client.chat([{"role": "user", "content": "hi"}])
        assert result == "最终回答"  # <think> 块与流首空白均被过滤
    finally:
        srv.stop()


def test_reasoning_content_skipped():
    srv = MockOpenAIServer("sse_reasoning")
    try:
        client = _client(srv.url)
        result = client.chat([{"role": "user", "content": "hi"}])
        assert result == "回答内容"
    finally:
        srv.stop()


def test_non_stream_json_fallback():
    srv = MockOpenAIServer("json")
    try:
        client = _client(srv.url)
        result = client.chat([{"role": "user", "content": "hi"}])
        assert result == "非流式回答"
    finally:
        srv.stop()


def test_sse_error_event():
    srv = MockOpenAIServer("sse_error")
    try:
        client = _client(srv.url)
        with pytest.raises(LlmError, match="invalid api key"):
            client.chat([{"role": "user", "content": "hi"}])
    finally:
        srv.stop()


def test_http_500_retry_then_success():
    srv = MockOpenAIServer("http_500_then_sse")
    try:
        client = _client(srv.url, max_retries=3, retry_delay_ms=10)
        result = client.chat([{"role": "user", "content": "hi"}])
        assert result == "你好，世界！"
    finally:
        srv.stop()


def test_http_4xx_no_retry():
    srv = MockOpenAIServer("http_500_then_sse")
    try:
        # 4xx 不重试：切到 404 行为验证
        srv.httpd.behavior = "not_found_404"
        client = _client(srv.url, max_retries=3, retry_delay_ms=10)
        with pytest.raises(LlmError) as ei:
            client.chat([{"role": "user", "content": "hi"}])
        assert ei.value.status == 404
        assert ei.value.retryable is False
    finally:
        srv.stop()


def test_empty_response_error():
    srv = MockOpenAIServer("sse")
    try:
        srv.httpd.behavior = "empty_sse"
        client = _client(srv.url)
        with pytest.raises(LlmError, match="空响应"):
            client.chat([{"role": "user", "content": "hi"}])
    finally:
        srv.stop()


def test_cancel_interrupts_stream():
    srv = MockOpenAIServer("slow_sse")
    try:
        client = _client(srv.url, timeout_seconds=10)
        cancel = CancelEvent()
        errors = []

        def run():
            try:
                client.chat([{"role": "user", "content": "hi"}], cancel=cancel)
            except LlmError as e:
                errors.append(e)

        t = threading.Thread(target=run, daemon=True)
        t.start()
        time.sleep(0.4)  # 收到第一块后取消
        cancel.cancel()
        t.join(timeout=5)
        assert not t.is_alive(), "取消后任务应立即结束"
        assert errors and errors[0].kind == "cancelled"
    finally:
        srv.stop()
