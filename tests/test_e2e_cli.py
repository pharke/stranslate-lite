"""CLI 端到端测试：真实走 config → prompts → LLM(SSE) → stdout。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mock_server import MockOpenAIServer  # noqa: E402

from stranslate_lite.cli import main  # noqa: E402


def _write_config(path: Path, base_url: str, prompts_extra: str = "") -> None:
    path.write_text(
        f"""
[api]
base_url = "{base_url}"
api_key = "test-key"
model = "mock-model"

[prompts."翻译"]
name = "翻译"
[[prompts."翻译".messages]]
role = "system"
content = "translate to $target"
[[prompts."翻译".messages]]
role = "user"
content = "$content"

{prompts_extra}

[[hotkeys]]
key = "alt+q"
prompt = "翻译"
""".strip()
        + "\n",
        encoding="utf-8",
    )


def test_translate_cli_e2e(tmp_path, monkeypatch, capsys):
    srv = MockOpenAIServer("sse")
    try:
        cfg_path = tmp_path / "config.toml"
        _write_config(cfg_path, srv.url)
        monkeypatch.setenv("STRANSLATE_LITE_CONFIG", str(cfg_path))

        rc = main(["translate", "hello world"])
        captured = capsys.readouterr()
        assert rc == 0, captured.err
        assert captured.out.strip() == "你好，世界！"
        # 提示词模板正确渲染
        assert srv.last_request["messages"][0]["content"] == "translate to Simplified Chinese"
        assert srv.last_request["messages"][1]["content"] == "hello world"
    finally:
        srv.stop()


def test_translate_cli_stdin(tmp_path, monkeypatch, capsys):
    srv = MockOpenAIServer("sse_think")
    try:
        cfg_path = tmp_path / "config.toml"
        _write_config(cfg_path, srv.url)
        monkeypatch.setenv("STRANSLATE_LITE_CONFIG", str(cfg_path))
        monkeypatch.setattr(sys, "stdin", __import__("io").StringIO("from stdin"))

        rc = main(["translate", "--stdin"])
        captured = capsys.readouterr()
        assert rc == 0
        assert captured.out.strip() == "最终回答"
    finally:
        srv.stop()


def test_translate_cli_error_surface(tmp_path, monkeypatch, capsys):
    srv = MockOpenAIServer("sse_error")
    try:
        cfg_path = tmp_path / "config.toml"
        _write_config(cfg_path, srv.url)
        monkeypatch.setenv("STRANSLATE_LITE_CONFIG", str(cfg_path))

        rc = main(["translate", "x"])
        captured = capsys.readouterr()
        assert rc == 1
        assert "invalid api key" in captured.err
    finally:
        srv.stop()


def test_check_command(tmp_path, monkeypatch, capsys):
    srv = MockOpenAIServer("sse")
    try:
        cfg_path = tmp_path / "config.toml"
        _write_config(cfg_path, srv.url)
        monkeypatch.setenv("STRANSLATE_LITE_CONFIG", str(cfg_path))

        rc = main(["check", "--ping"])
        captured = capsys.readouterr()
        assert rc == 0, captured.err
        assert "✓ 配置有效" in captured.out
        assert "✓ API 连通" in captured.out
    finally:
        srv.stop()
