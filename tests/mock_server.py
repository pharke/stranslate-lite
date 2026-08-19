"""模拟 OpenAI 兼容服务端：用于端到端测试核心链路。"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, List, Optional


class MockOpenAIHandler(BaseHTTPRequestHandler):
    """按 server.behavior 配置响应：

    behavior 类型：
      "sse":           正常 SSE 流
      "sse_think":     SSE 流 + <think> 推理块（应被过滤）
      "json":          非流式完整 JSON（stream 参数被忽略的回退场景）
      "sse_error":     流中返回错误事件
      "http_500_then_sse": 首次 500，之后正常（测重试）
      "sse_reasoning": DeepSeek 风格 reasoning_content + content
    """

    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # 静默
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length)) if length else {}
        self.server.last_request = body  # type: ignore[attr-defined]
        behavior = self.server.behavior  # type: ignore[attr-defined]

        if behavior == "http_500_then_sse":
            with self.server.lock:  # type: ignore[attr-defined]
                if not self.server.served_once:  # type: ignore[attr-defined]
                    self.server.served_once = True  # type: ignore[attr-defined]
                    self.send_response(500)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(b'{"error": {"message": "temporary failure"}}')
                    return
            behavior = "sse"

        if behavior == "sse":
            self._sse_ok()
        elif behavior == "sse_think":
            self._sse_think()
        elif behavior == "sse_reasoning":
            self._sse_reasoning()
        elif behavior == "json":
            self._json_ok(body)
        elif behavior == "sse_error":
            self._sse_error()
        elif behavior == "slow_sse":
            self._slow_sse()
        elif behavior == "empty_sse":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        else:
            self.send_response(404)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"error": {"message": "not found"}}')

    def _sse(self, events: List[dict], chunk_delay: float = 0.0):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        import time as _t
        for ev in events:
            self.wfile.write(f"data: {json.dumps(ev, ensure_ascii=False)}\n\n".encode("utf-8"))
            self.wfile.flush()
            if chunk_delay:
                _t.sleep(chunk_delay)
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()

    def _delta(self, content):
        return {"choices": [{"delta": {"content": content}}]}

    def _sse_ok(self):
        self._sse([self._delta("你好，"), self._delta("世界！")])

    def _sse_think(self):
        self._sse([
            self._delta("<think>"),
            self._delta("内部推理内容"),
            self._delta("</think>"),
            self._delta("\n\n"),
            self._delta("最终回答"),
        ])

    def _sse_reasoning(self):
        self._sse([
            {"choices": [{"delta": {"reasoning_content": "思考中", "content": None}}]},
            self._delta("回答内容"),
        ])

    def _sse_error(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        self.wfile.write(b'data: {"error": {"message": "invalid api key"}}\n\n')
        self.wfile.flush()

    def _json_ok(self, body):
        payload = {
            "id": "x",
            "choices": [{"message": {"role": "assistant", "content": "非流式回答"}}],
        }
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _slow_sse(self):
        """逐块发送但每块间隔较长：用于取消测试。"""
        self._sse(
            [self._delta("第一块"), self._delta("第二块"), self._delta("第三块")],
            chunk_delay=0.3,
        )


class MockOpenAIServer:
    def __init__(self, behavior: str = "sse"):
        self.behavior = behavior
        self.lock = threading.Lock()
        self.served_once = False
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), MockOpenAIHandler)
        self.httpd.behavior = behavior  # type: ignore[attr-defined]
        self.httpd.lock = self.lock  # type: ignore[attr-defined]
        self.httpd.served_once = False  # type: ignore[attr-defined]
        self.httpd.last_request = None  # type: ignore[attr-defined]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.httpd.server_address[1]}"

    @property
    def last_request(self) -> Optional[dict]:
        return self.httpd.last_request  # type: ignore[attr-defined]

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
