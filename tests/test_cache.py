"""翻译缓存测试：命中/未命中、禁用、LRU 淘汰、TTL 过期、键稳定性。"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

from stranslate_lite.cache import TranslationCache, cache_key  # noqa: E402
from stranslate_lite.config import CacheConfig  # noqa: E402


def _cfg(**kw) -> CacheConfig:
    base = {"enabled": True, "max_entries": 100, "ttl_days": 7}
    base.update(kw)
    return CacheConfig(**base)


@pytest.fixture
def cache(tmp_path, monkeypatch):
    monkeypatch.setenv("STRANSLATE_LITE_CONFIG", str(tmp_path / "config.toml"))
    return TranslationCache(_cfg(), path=tmp_path / "cache.db")


def _key(content):
    return cache_key("m", [{"role": "user", "content": content}])


def test_roundtrip(cache):
    k = _key("hello")
    assert cache.get(k) is None
    cache.put(k, "你好")
    assert cache.get(k) == "你好"


def test_key_stability_and_distinctness():
    a = _key("hello")
    b = _key("hello")
    assert a == b
    assert _key("world") != a
    assert cache_key("m", [{"role": "user", "content": "x"}]) != cache_key(
        "m2", [{"role": "user", "content": "x"}]
    )
    assert cache_key("m", [{"role": "system", "content": "A"}]) != cache_key(
        "m", [{"role": "system", "content": "B"}]
    )


def test_disabled(cache):
    cache.configure(_cfg(enabled=False))
    k = _key("hello")
    cache.put(k, "你好")
    assert cache.get(k) is None


def test_max_entries_zero_disables(cache):
    cache.configure(_cfg(max_entries=0))
    k = _key("hello")
    cache.put(k, "你好")
    assert cache.get(k) is None


def test_lru_eviction(cache):
    cache.configure(_cfg(max_entries=2))
    k1, k2, k3 = _key("a"), _key("b"), _key("c")
    cache.put(k1, "1")
    cache.put(k2, "2")
    cache.get(k1)  # 刷新 k1 访问时间，k2 成为最久未用
    cache.put(k3, "3")
    assert cache.get(k1) == "1"
    assert cache.get(k2) is None
    assert cache.get(k3) == "3"


def test_ttl_expiry(cache):
    cache.configure(_cfg(ttl_days=7))
    k = _key("old")
    cache.put(k, "旧")
    # 把 created_at 回拨到 8 天前
    conn = cache._db()
    conn.execute("UPDATE translations SET created_at=? WHERE key=?", (time.time() - 8 * 86400, k))
    conn.commit()
    assert cache.get(k) is None


def test_ttl_zero_never_expires(cache):
    cache.configure(_cfg(ttl_days=0))
    k = _key("forever")
    cache.put(k, "永久")
    conn = cache._db()
    conn.execute("UPDATE translations SET created_at=? WHERE key=?", (time.time() - 999 * 86400, k))
    conn.commit()
    assert cache.get(k) == "永久"


def test_empty_value_not_cached(cache):
    k = _key("empty")
    cache.put(k, "")
    assert cache.get(k) is None
