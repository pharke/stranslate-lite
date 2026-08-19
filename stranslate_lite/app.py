"""应用编排：热键 → 取词 → 提示词渲染 → LLM 流式调用 → 悬浮窗展示。

单飞（single-flight）模型：新触发的热键取消上一次仍在进行的任务（对齐 STranslate
替换翻译的「运行中再次触发即取消」行为）。

配置热重载：每次热键触发时重新读取配置文件，提示词 / API / 取词设置「保存即生效」；
快捷键列表本身的增删改（绑定关系）在启动时注册，需重启才能变更。
"""

from __future__ import annotations

import functools
import itertools
import logging
import threading
from typing import Optional

from .cache import TranslationCache, cache_key
from .capture import CaptureError, capture_and_postprocess
from .config import Config, ConfigError, Hotkey, load_config
from .llm import CancelledError, CancelEvent, LlmClient, LlmError
from .platform.base import PlatformAdapter
from .prompts import render_messages

logger = logging.getLogger(__name__)

_PENDING_TEXT = "⏳ 调用中…"


class App:
    def __init__(self, config: Config, adapter: PlatformAdapter):
        self.config = config
        self.adapter = adapter
        self._cache = TranslationCache(config.cache)
        self._lock = threading.Lock()
        self._current: Optional[CancelEvent] = None
        self._current_key: Optional[str] = None
        self._last_panel_key: Optional[str] = None
        self._jobs = itertools.count(1)

    # ------------------------------------------------------------------
    # 启动
    # ------------------------------------------------------------------
    def start(self) -> None:
        for hotkey in self.config.hotkeys:
            cb = functools.partial(self.on_hotkey, hotkey.key)
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
    # 配置热重载
    # ------------------------------------------------------------------
    def _reload_config(self) -> Optional[Config]:
        """返回最新配置；读取失败返回 None（错误面板已在 on_hotkey 展示）。"""
        try:
            return load_config()
        except ConfigError as e:
            logger.warning("配置重载失败：%s", e)
            key = f"job-{next(self._jobs)}"
            self.adapter.show_result(key, f"配置重载失败：{e}")
            return None

    # ------------------------------------------------------------------
    # 热键回调（任意线程）
    # ------------------------------------------------------------------
    def on_hotkey(self, key_spec: str) -> None:
        cfg = self._reload_config()
        if cfg is None:
            return
        hotkey = next((h for h in cfg.hotkeys if h.key == key_spec), None)
        if hotkey is None:
            logger.warning(
                "快捷键 %s 已不在配置中（快捷键绑定变更需重启生效），忽略本次触发", key_spec
            )
            return
        self.config = cfg
        self.adapter.set_auto_close_seconds(cfg.ui.auto_close_seconds)
        self._cache.configure(cfg.cache)
        with self._lock:
            if self._current is not None:
                old_cancel, old_key = self._current, self._current_key
                old_cancel.cancel()
                self.adapter.close_result(old_key or "")
            cancel = CancelEvent()
            key = f"job-{next(self._jobs)}"
            self._current = cancel
            self._current_key = key
        # 单窗口语义（对齐 STranslate SingletonWindowOpener）：新触发先关掉
        # 上一个结果面板（哪怕其任务早已完成），避免窗口堆积。
        if self._last_panel_key:
            self.adapter.close_result(self._last_panel_key)
        self._last_panel_key = key
        threading.Thread(target=self._run_job, args=(cfg, hotkey, cancel, key), daemon=True, name=f"job-{key}").start()

    # ------------------------------------------------------------------
    # 任务线程
    # ------------------------------------------------------------------
    def _run_job(self, cfg: Config, hotkey: Hotkey, cancel: CancelEvent, key: str) -> None:
        try:
            text = capture_and_postprocess(self.adapter, cfg.capture)
            if cancel.is_set():
                raise CancelledError()

            prompt = cfg.prompt(hotkey.prompt)
            source = hotkey.source_lang or prompt.source_lang or cfg.api.source_lang
            target = hotkey.target_lang or prompt.target_lang or cfg.api.target_lang
            messages = render_messages(prompt, text, source, target)

            # 缓存优先（对齐 STranslate checkCacheFirst）：命中直接展示，不调 API
            ck = cache_key(cfg.api.model, messages)
            cached = self._cache.get(ck)
            if cached is not None:
                logger.info("命中翻译缓存，跳过 API 调用")
                self.adapter.show_result(key, cached)
                return

            self.adapter.show_result(key, _PENDING_TEXT)

            # on_delta 收到的是流式增量，而面板更新是整段替换语义：
            # 与 STranslate 插件一致（插件内累加 result.Text，UI 展示完整文本）
            buffer: list = []

            def on_delta(t: str) -> None:
                if cancel.is_set():
                    return
                buffer.append(t)
                self.adapter.update_result(key, "".join(buffer))

            # 每次任务都用最新配置实例化客户端（配合配置热重载）
            client = LlmClient(cfg.api)
            result = client.chat_stream(messages, on_delta, cancel=cancel)
            # 成功后写入缓存（取消/失败/空结果不入缓存）
            self._cache.put(ck, result)
        except CancelledError:
            self.adapter.close_result(key)
        except ConfigError as e:
            self.adapter.show_result(key, f"配置错误：{e}")
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
