"""配置模块测试。"""

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from stranslate_lite.config import (  # noqa: E402
    ConfigError,
    config_path,
    load_config,
    parse_config,
    write_example,
)


def test_example_roundtrip(tmp_path, monkeypatch):
    p = tmp_path / "config.toml"
    write_example(p, hotkey="alt+q")
    monkeypatch.setenv("STRANSLATE_LITE_CONFIG", str(p))
    cfg = load_config()
    assert cfg.api.model == "gpt-4o-mini"
    assert set(cfg.prompts) == {"翻译", "代码审阅"}
    assert [h.key for h in cfg.hotkeys] == ["alt+q", "alt+w"]
    assert cfg.default_hotkey().prompt == "翻译"
    assert config_path() == p


def test_example_refuses_overwrite(tmp_path):
    p = tmp_path / "config.toml"
    write_example(p)
    with pytest.raises(ConfigError, match="已存在"):
        write_example(p)


def test_api_key_env_resolution(monkeypatch):
    monkeypatch.setenv("MY_KEY", "sk-secret")
    cfg = parse_config({
        "api": {"api_key": "${MY_KEY}", "model": "m"},
        "prompts": {"p": {"name": "p", "messages": [{"role": "user", "content": "$content"}]}},
        "hotkeys": [{"key": "alt+q", "prompt": "p"}],
    })
    assert cfg.api.resolve_api_key() == "sk-secret"


def test_missing_prompt_reference():
    with pytest.raises(ConfigError, match="不存在"):
        parse_config({
            "prompts": {"p": {"name": "p", "messages": [{"role": "user", "content": "x"}]}},
            "hotkeys": [{"key": "alt+q", "prompt": "nope"}],
        })


def test_duplicate_hotkey():
    with pytest.raises(ConfigError, match="重复"):
        parse_config({
            "prompts": {"p": {"name": "p", "messages": [{"role": "user", "content": "x"}]}},
            "hotkeys": [
                {"key": "alt+q", "prompt": "p"},
                {"key": "alt+q", "prompt": "p"},
            ],
        })


def test_empty_prompts():
    with pytest.raises(ConfigError, match="至少"):
        parse_config({"prompts": {}, "hotkeys": [{"key": "alt+q", "prompt": "p"}]})


def test_bad_role():
    with pytest.raises(ConfigError, match="角色"):
        parse_config({
            "prompts": {"p": {"name": "p", "messages": [{"role": "tool", "content": "x"}]}},
            "hotkeys": [{"key": "alt+q", "prompt": "p"}],
        })


def test_extra_body_cannot_override_builtin():
    with pytest.raises(ConfigError, match="不能覆盖"):
        parse_config({
            "api": {"extra_body": {"model": "x"}},
            "prompts": {"p": {"name": "p", "messages": [{"role": "user", "content": "x"}]}},
            "hotkeys": [{"key": "alt+q", "prompt": "p"}],
        })


def test_temperature_range():
    with pytest.raises(ConfigError, match="范围"):
        parse_config({
            "api": {"temperature": 3},
            "prompts": {"p": {"name": "p", "messages": [{"role": "user", "content": "x"}]}},
            "hotkeys": [{"key": "alt+q", "prompt": "p"}],
        })


def test_ui_section_defaults_and_parsing():
    base = {
        "prompts": {"p": {"name": "p", "messages": [{"role": "user", "content": "x"}]}},
        "hotkeys": [{"key": "alt+q", "prompt": "p"}],
    }
    assert parse_config(dict(base)).ui.auto_close_seconds == 15.0
    assert parse_config({**base, "ui": {"auto_close_seconds": 0}}).ui.auto_close_seconds == 0.0
    with pytest.raises(ConfigError, match="范围"):
        parse_config({**base, "ui": {"auto_close_seconds": 9999}})
    with pytest.raises(ConfigError, match="未知字段"):
        parse_config({**base, "ui": {"nope": 1}})


def test_cache_section_defaults_and_parsing():
    base = {
        "prompts": {"p": {"name": "p", "messages": [{"role": "user", "content": "x"}]}},
        "hotkeys": [{"key": "alt+q", "prompt": "p"}],
    }
    cfg = parse_config(dict(base)).cache
    assert (cfg.enabled, cfg.max_entries, cfg.ttl_days) == (True, 500, 7)
    cfg2 = parse_config({**base, "cache": {"enabled": False, "max_entries": 0, "ttl_days": 0}}).cache
    assert (cfg2.enabled, cfg2.max_entries, cfg2.ttl_days) == (False, 0, 0)
    with pytest.raises(ConfigError, match="布尔"):
        parse_config({**base, "cache": {"enabled": "yes"}})
    with pytest.raises(ConfigError, match="未知字段"):
        parse_config({**base, "cache": {"nope": 1}})
