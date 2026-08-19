"""OpenAI 兼容 LLM 客户端。

移植自 STranslate 的 OpenAI 插件链路并增强：
- UrlHelper.BuildFinalUrl：base_url 自动补全 /v1/chat/completions，支持 "#" 强制完整地址。
- OpenAIProtocol：请求构造（model/messages/temperature/stream + 附加参数合并保护）、
  SSE 逐行解析（data: 前缀、[DONE]、choices[0].delta.content、错误提取）、
  <think> 推理块过滤、流首空白跳过、非流式 JSON 回退。
- 重试：网络错误/5xx/429/408 按 max_retries + retry_delay_ms 重试（原版仅声明未消费）。
- 取消：CancelEvent + 关闭底层 socket，使阻塞中的读取立即中断。
"""

from __future__ import annotations

import json
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Dict, List, Optional

from . import __version__
from .config import ApiConfig

DEFAULT_CHAT_PATH = "/v1/chat/completions"


class LlmError(Exception):
    """LLM 调用错误。kind: network|api|cancelled|empty。"""

    def __init__(self, kind: str, message: str, retryable: bool = False, status: Optional[int] = None):
        super().__init__(message)
        self.kind = kind
        self.retryable = retryable
        self.status = status


class CancelledError(LlmError):
    def __init__(self):
        super().__init__("cancelled", "已取消")


class CancelEvent:
    """跨线程取消：置位事件 + 关闭活动 socket，让阻塞读取立即中断。"""

    def __init__(self) -> None:
        self._event = threading.Event()
        self._sock: Optional[socket.socket] = None
        self._lock = threading.Lock()

    def is_set(self) -> bool:
        return self._event.is_set()

    def wait(self, timeout: Optional[float] = None) -> bool:
        return self._event.wait(timeout)

    def cancel(self) -> None:
        self._event.set()
        with self._lock:
            sock = self._sock
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass

    def bind_socket(self, sock: Optional[socket.socket]) -> None:
        with self._lock:
            self._sock = sock


def build_chat_url(base_url: str, path: str = DEFAULT_CHAT_PATH) -> str:
    """拼接最终请求地址（移植 UrlHelper.BuildFinalUrl 的 OpenAI 规则）。"""
    base = (base_url or "").strip()
    if not base:
        raise LlmError("network", "API 地址为空，请检查配置 [api].base_url")
    if base.endswith("#"):
        # 以 # 结尾：强制使用该完整地址
        return base[:-1].rstrip("/")
    parts = urllib.parse.urlsplit(base)
    p = parts.path.rstrip("/") or "/"
    if p in ("/", "/v1"):
        return urllib.parse.urlunsplit((parts.scheme, parts.netloc, path, parts.query, parts.fragment))
    return base


def build_request_body(model: str, messages: List[Dict[str, str]], temperature: float, extra: Dict[str, Any]) -> Dict[str, Any]:
    body: Dict[str, Any] = {
        "model": model,
        "messages": messages,
        "temperature": min(max(float(temperature), 0.0), 2.0),
        "stream": True,
    }
    body.update(extra)
    return body


def _extract_error(obj: Dict[str, Any]) -> Optional[str]:
    """从响应 JSON 提取错误消息（兼容 error.message / error 字符串 / message）。"""
    err = obj.get("error")
    if isinstance(err, dict):
        return str(err.get("message") or json.dumps(err, ensure_ascii=False)[:300])
    if isinstance(err, str) and err:
        return err
    if isinstance(obj.get("message"), str) and obj.get("message"):
        return str(obj["message"])
    return None


def _read_error_body(e: urllib.error.HTTPError) -> str:
    try:
        raw = e.read(4096)
    except OSError:
        return ""
    try:
        obj = json.loads(raw)
        return _extract_error(obj) or raw.decode("utf-8", "replace")[:300]
    except (ValueError, UnicodeDecodeError):
        return raw.decode("utf-8", "replace")[:300]


_RETRYABLE_STATUS = {408, 429, 500, 502, 503, 504}


class LlmClient:
    def __init__(self, api: ApiConfig):
        self.api = api

    # ------------------------------------------------------------------
    # 流式调用
    # ------------------------------------------------------------------
    def chat_stream(
        self,
        messages: List[Dict[str, str]],
        on_delta: Callable[[str], None],
        cancel: Optional[CancelEvent] = None,
        temperature: Optional[float] = None,
    ) -> str:
        """流式请求；返回完整文本。失败抛 LlmError。"""
        cancel = cancel or CancelEvent()
        url = build_chat_url(self.api.base_url)
        body = build_request_body(
            self.api.model, messages,
            temperature if temperature is not None else self.api.temperature,
            self.api.extra_body,
        )
        attempts = self.api.max_retries + 1
        for attempt in range(1, attempts + 1):
            if cancel.is_set():
                raise CancelledError()
            try:
                result = self._post_stream(url, body, on_delta, cancel)
                if cancel.is_set():
                    raise CancelledError()
                return result
            except LlmError as e:
                if cancel.is_set():
                    raise CancelledError() from None
                if not e.retryable or attempt >= attempts:
                    raise
                if attempt < attempts:
                    time.sleep(self.api.retry_delay_ms / 1000.0)
        raise LlmError("network", "请求失败")  # pragma: no cover

    def _post_stream(
        self,
        url: str,
        body: Dict[str, Any],
        on_delta: Callable[[str], None],
        cancel: CancelEvent,
    ) -> str:
        headers = {
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
            "User-Agent": f"stranslate-lite/{__version__}",
        }
        key = self.api.resolve_api_key()
        if key:
            headers["Authorization"] = "Bearer " + key

        req = urllib.request.Request(url, data=json.dumps(body, ensure_ascii=False).encode("utf-8"), headers=headers, method="POST")
        try:
            resp = urllib.request.urlopen(req, timeout=self.api.timeout_seconds)
        except urllib.error.HTTPError as e:
            retryable = e.code in _RETRYABLE_STATUS
            msg = _read_error_body(e) or f"HTTP {e.code}"
            raise LlmError("api", f"API 返回错误（HTTP {e.code}）：{msg}", retryable=retryable, status=e.code) from None
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            if cancel.is_set():
                raise CancelledError() from None
            reason = getattr(e, "reason", None) or e
            raise LlmError("network", f"网络错误：{reason}", retryable=True) from None

        try:
            with resp:
                sock = None
                try:
                    sock = resp.fp.raw._sock  # type: ignore[attr-defined]
                except (AttributeError, OSError):
                    pass
                cancel.bind_socket(sock)
                return self._read_stream(resp, on_delta, cancel)
        except CancelledError:
            raise
        except (TimeoutError, OSError) as e:
            if cancel.is_set():
                raise CancelledError() from None
            raise LlmError("network", f"读取流中断：{e}", retryable=True) from None
        finally:
            cancel.bind_socket(None)

    def _read_stream(self, resp: Any, on_delta: Callable[[str], None], cancel: CancelEvent) -> str:
        """SSE 流读取（含非流式 JSON 回退）。"""
        result: List[str] = []
        buf: List[str] = []
        saw_sse = False
        is_think = False
        started = False

        for raw in resp:
            if cancel.is_set():
                raise CancelledError()
            try:
                line = raw.decode("utf-8", "replace").strip()
            except AttributeError:  # 某些实现已返回 str
                line = str(raw).strip()
            if not line:
                continue
            if line.startswith("data:"):
                saw_sse = True
                payload = line[len("data:"):].strip()
                if not payload:
                    continue
                if payload == "[DONE]":
                    break
                if not payload.startswith("{"):
                    continue  # 心跳/状态行，直接忽略
                try:
                    obj = json.loads(payload)
                except ValueError:
                    continue
                if not isinstance(obj, dict):
                    continue
                err = _extract_error(obj)
                if err:
                    raise LlmError("api", f"API 返回错误：{err}")
                choices = obj.get("choices") or [{}]
                delta = (choices[0].get("delta") if choices and isinstance(choices[0], dict) else None) or {}
                content = delta.get("content")
                if content is None:
                    continue  # reasoning_content 等字段不拼接
                content = str(content)
                t = content.strip()
                if t == "<think>":
                    is_think = True
                    continue
                if t == "</think>":
                    is_think = False
                    continue
                if is_think:
                    continue
                if not started and not content.strip():
                    continue  # 跳过流首空白（推理结束后的换行优化）
                started = True
                result.append(content)
                on_delta(content)
            elif line.startswith(":"):
                continue  # SSE 注释
            else:
                buf.append(line)

        if not saw_sse and buf:
            # 非流式回退：服务端忽略 stream=true，返回完整 JSON
            try:
                obj = json.loads("\n".join(buf))
            except ValueError:
                raise LlmError("api", "无法解析响应：既非 SSE 流也非 JSON") from None
            if not isinstance(obj, dict):
                raise LlmError("api", "无法解析响应：根节点不是对象")
            err = _extract_error(obj)
            if err:
                raise LlmError("api", f"API 返回错误：{err}")
            choices = obj.get("choices") or []
            if not choices:
                raise LlmError("empty", "响应中没有 choices 字段")
            message = choices[0].get("message") or {}
            content = message.get("content")
            if not content:
                raise LlmError("empty", "响应中没有内容")
            content = str(content)
            result.append(content)
            on_delta(content)
            started = True

        if not started:
            raise LlmError("empty", "空响应：未收到任何内容（可检查模型名与接口地址）")
        if cancel.is_set():
            # 取消时关闭 socket 可能让流以 EOF 正常结束，此处兜底检查
            raise CancelledError()
        return "".join(result)

    # ------------------------------------------------------------------
    # 非流式便捷方法
    # ------------------------------------------------------------------
    def chat(
        self,
        messages: List[Dict[str, str]],
        cancel: Optional[CancelEvent] = None,
        temperature: Optional[float] = None,
    ) -> str:
        chunks: List[str] = []
        return self.chat_stream(messages, chunks.append, cancel=cancel, temperature=temperature)
