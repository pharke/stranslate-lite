"""近期翻译缓存：相同请求命中缓存，省 token、零延迟。

语义对齐 STranslate 的 History 缓存（checkCacheFirst && HistoryLimit > 0）：
- 翻译前先查缓存，命中直接展示，不调 API；
- 翻译成功后写入缓存（失败/取消/空结果不入缓存）；
- 键 = model + 渲染后的完整 messages（提示词文本、语言方向、原文任何变化
  都会自然生成新键）；
- SQLite 持久化（config 同目录 cache.db），LRU 淘汰 + TTL 过期；
- 缓存故障（磁盘/锁/损坏）静默降级，不影响翻译主流程。
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from .config import CacheConfig, config_path

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS translations (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    created_at REAL NOT NULL,
    accessed_at REAL NOT NULL
)
"""


def cache_db_path() -> Path:
    """缓存库路径：与配置文件同目录的 cache.db。"""
    return config_path().with_name("cache.db")


def cache_key(model: str, messages: List[Dict[str, str]]) -> str:
    """缓存键：model + 渲染后的完整 messages 的 SHA-256。"""
    payload = json.dumps(
        {"model": model, "messages": messages}, ensure_ascii=False, sort_keys=True
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class TranslationCache:
    def __init__(self, cfg: CacheConfig, path: Optional[Path] = None):
        self.cfg = cfg
        self.path = path or cache_db_path()
        self._lock = threading.Lock()
        self._conn: Optional[sqlite3.Connection] = None

    def configure(self, cfg: CacheConfig) -> None:
        """热重载配置（enabled/max_entries/ttl_days 下次操作生效）。"""
        self.cfg = cfg

    def _db(self) -> sqlite3.Connection:
        if self._conn is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute(_SCHEMA)
            self._conn.commit()
        return self._conn

    def get(self, key: str) -> Optional[str]:
        """命中返回缓存文本（刷新访问时间）；未命中/过期返回 None。"""
        if not self.cfg.enabled or self.cfg.max_entries <= 0:
            return None
        with self._lock:
            try:
                conn = self._db()
                row = conn.execute(
                    "SELECT value, created_at FROM translations WHERE key=?", (key,)
                ).fetchone()
                if row is None:
                    return None
                value, created_at = row
                now = time.time()
                if self.cfg.ttl_days > 0 and now - created_at > self.cfg.ttl_days * 86400.0:
                    conn.execute("DELETE FROM translations WHERE key=?", (key,))
                    conn.commit()
                    return None
                conn.execute("UPDATE translations SET accessed_at=? WHERE key=?", (now, key))
                conn.commit()
                return value
            except sqlite3.Error as e:  # 缓存故障不影响主流程
                logger.debug("缓存读取失败（忽略）：%s", e)
                return None

    def put(self, key: str, value: str) -> None:
        """写入缓存（空值忽略），并按 LRU 修剪到 max_entries。"""
        if not self.cfg.enabled or self.cfg.max_entries <= 0 or not value:
            return
        with self._lock:
            try:
                conn = self._db()
                now = time.time()
                conn.execute(
                    "INSERT INTO translations(key, value, created_at, accessed_at) "
                    "VALUES(?,?,?,?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
                    "created_at=excluded.created_at, accessed_at=excluded.accessed_at",
                    (key, value, now, now),
                )
                conn.execute(
                    "DELETE FROM translations WHERE key NOT IN "
                    "(SELECT key FROM translations ORDER BY accessed_at DESC LIMIT ?)",
                    (self.cfg.max_entries,),
                )
                conn.commit()
            except sqlite3.Error as e:
                logger.debug("缓存写入失败（忽略）：%s", e)
