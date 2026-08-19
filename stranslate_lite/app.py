"""应用编排：热键 → 取词 → 提示词渲染 → LLM 流式调用 → 悬浮窗展示。

单飞（single-flight）模型：新触发的热键取消上一次仍在进行的任务（对齐 STranslate
替换翻译的「运行中再次触发即取消」行为）。
"""

from __future__ import annotations

import functools
import itertools
import logging
import threading
from typing import Optional

from .capture import CaptureError, capture_and_postprocess
from .config import Config, Hotkey
from .llm import CancelledError, CancelEvent, LlmClient, LlmError
from .platform.base import PlatformAdapter
from .prompts import render_messages

logger = logging.getLogger(__name__)

_PENDING_TEXT = "⏳ 调用中…"


class App:
    def __init__(self, config: Config, adapter: PlatformAdapter):
        self.config = config
        self.adapter = adapter
        self.client = LlmClient(config.api)
        self._lock = threading.Lock()
        self._current: Optional[CancelEvent] = None
        self._current_key: Optional[str] = None
        self._jobs = itertools.count(1)

    # ------------------------------------------------------------------
    # 启动
    # ------------------------------------------------------------------
    def start(self) -> None:
        for hotkey in self.config.hotkeys:
            cb = functools.partial(self.on_hotkey, hotkey)
            self.adapter.register_hotkey(hotkey.key, cb)
            logger.info("已注册快捷键 %s → 提示词“%s”", hotkey.key, hotkey.prompt)
        self.adapter.run()

    def stop(self) -> None:
        with self._lock:
            cancel = self._current
        if cancel is not None:
            cancel.cancel()
        self.adapter.stop()

    # ------------------------------------------------------------------
    # 热键回调（任意线程）
    # ------------------------------------------------------------------
    def on_hotkey(self, hotkey: Hotkey) -> None:
        with self._lock:
            if self._current is not None:
                old_cancel, old_key = self._current, self._current_key
                old_cancel.cancel()
                self.adapter.close_result(old_key or "")
            cancel = CancelEvent()
            key = f"job-{next(self._jobs)}"
            self._current = cancel
            self._current_key = key
        threading.Thread(target=self._run_job, args=(hotkey, cancel, key), daemon=True, name=f"job-{key}").start()

    # ------------------------------------------------------------------
    # 任务线程
    # ------------------------------------------------------------------
    def _run_job(self, hotkey: Hotkey, cancel: CancelEvent, key: str) -> None:
        try:
            text = capture_and_postprocess(self.adapter, self.config.capture)
            if cancel.is_set():
                raise CancelledError()

            prompt = self.config.prompt(hotkey.prompt)
            source = hotkey.source_lang or prompt.source_lang or self.config.api.source_lang
            target = hotkey.target_lang or prompt.target_lang or self.config.api.target_lang
            messages = render_messages(prompt, text, source, target)

            self.adapter.show_result(key, _PENDING_TEXT)

            # on_delta 收到的是流式增量，而面板更新是整段替换语义：
            # 与 STranslate 插件一致（插件内累加 result.Text，UI 展示完整文本）
            buffer: list = []

            def on_delta(t: str) -> None:
                if cancel.is_set():
                    return
                buffer.append(t)
                self.adapter.update_result(key, "".join(buffer))

            self.client.chat_stream(messages, on_delta, cancel=cancel)
        except CancelledError:
            self.adapter.close_result(key)
        except CaptureError as e:
            self.adapter.show_result(key, f"取词失败：{e}")
        except LlmError as e:
            self.adapter.show_result(key, f"调用失败：{e}")
        except Exception as e:  # 兜底，避免任务线程静默死亡
            logger.exception("任务异常")
            self.adapter.show_result(key, f"发生异常：{e}")
        finally:
            with self._lock:
                if self._current is cancel:
                    self._current = None
                    self._current_key = None
