"""命令行入口。

用法：
  stranslate-lite run                 # 启动后台工具（全局热键 + 悬浮窗）
  stranslate-lite translate <文本>     # 一次性调用（可 --prompt 指定提示词，或从 stdin 读入）
  stranslate-lite check [--ping|--hotkeys]  # 校验配置与系统权限；--ping 测试 API；--hotkeys 检测热键冲突
  stranslate-lite config --init|--path # 生成示例配置 / 打印配置路径
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from typing import List, Optional

from . import __version__
from .app import App
from .cache import TranslationCache, cache_key
from .config import ConfigError, config_path, load_config, write_example
from .llm import CancelledError, LlmClient, LlmError
from .platform import AdapterError, get_adapter
from .prompts import render_messages

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("stranslate_lite")


def _load_or_die() -> Optional[object]:
    try:
        return load_config()
    except ConfigError as e:
        print(f"错误：{e}", file=sys.stderr)
        return None


def _acquire_single_instance():
    """单实例锁（POSIX）：用 fcntl 文件锁保证同一时刻只有一个 run 实例。

    返回锁文件对象（进程存活期间需保持引用，退出时内核自动释放锁，无需清理、
    也不怕 kill -9）；若已有实例持有锁则返回 None。非 POSIX 平台（Windows
    实验适配）无 fcntl，返回哨兵对象表示“未加锁、继续运行”。
    """
    try:
        import fcntl
    except ImportError:  # pragma: no cover - Windows 无 fcntl
        return object()
    lock_path = config_path().with_name("run.lock")
    lock_file = open(lock_path, "w")
    try:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        lock_file.close()
        return None
    lock_file.truncate(0)
    lock_file.write(str(os.getpid()))
    lock_file.flush()
    return lock_file


# ---------------------------------------------------------------------------
# 子命令
# ---------------------------------------------------------------------------

def cmd_run(args: argparse.Namespace) -> int:
    cfg = _load_or_die()
    if cfg is None:
        return 1
    # 单实例守卫：重复双击 .app 时，新实例会因热键已被占用而注册失败“闪退”；
    # 这里直接复用已有实例（静默退出），避免二次启动的困惑。
    instance_lock = _acquire_single_instance()
    if instance_lock is None:
        logger.info("已有 stranslate-lite 实例在运行，本次启动直接退出。")
        return 0
    try:
        adapter = get_adapter()
    except AdapterError as e:
        print(f"错误：{e}", file=sys.stderr)
        return 1
    app = App(cfg, adapter)

    # 走 logger（stderr，无缓冲）而非 print：.app 后台运行时 stdout 被重定向到
    # 日志文件且按块缓冲，进程不退出就看不到启动信息；logger 实时落盘便于排查。
    logger.info("stranslate-lite v%s（%s）", __version__, adapter.name)
    logger.info("配置文件：%s", config_path())
    issues = adapter.permission_issues()
    for i in issues:
        logger.warning("权限提示：%s", i)
    for h in cfg.hotkeys:
        logger.info("快捷键 %s → 提示词“%s”", h.key, h.prompt)

    # instance_lock 在 app.start() 阻塞期间保持存活；进程退出时内核自动释放锁
    try:
        app.start()
    except KeyboardInterrupt:
        app.stop()
    except AdapterError as e:
        print(f"错误：{e}", file=sys.stderr)
        return 1
    return 0


def cmd_translate(args: argparse.Namespace) -> int:
    cfg = _load_or_die()
    if cfg is None:
        return 1

    if args.stdin:
        text = sys.stdin.read()
    elif args.text:
        text = args.text
    else:
        print("错误：请提供文本参数，或用 --stdin 从管道读入", file=sys.stderr)
        return 1

    # 选择触发器：--prompt 优先，其次 --hotkey（序号），否则默认第一个
    hotkey = None
    if args.prompt:
        for h in cfg.hotkeys:
            if h.prompt == args.prompt:
                hotkey = h
                break
        if hotkey is None:
            print(f"错误：没有绑定到提示词“{args.prompt}”的快捷键", file=sys.stderr)
            return 1
    elif args.hotkey is not None:
        if not (0 <= args.hotkey < len(cfg.hotkeys)):
            print(f"错误：快捷键序号 {args.hotkey} 越界（共 {len(cfg.hotkeys)} 个）", file=sys.stderr)
            return 1
        hotkey = cfg.hotkeys[args.hotkey]
    else:
        hotkey = cfg.default_hotkey()

    prompt = cfg.prompt(hotkey.prompt)
    source = hotkey.source_lang or prompt.source_lang or cfg.api.source_lang
    target = hotkey.target_lang or prompt.target_lang or cfg.api.target_lang
    messages = render_messages(prompt, text, source, target)

    # 缓存优先：命中直接输出，不调 API
    cache = TranslationCache(cfg.cache)
    ck = cache_key(cfg.api.model, messages)
    cached = cache.get(ck)
    if cached is not None:
        sys.stdout.write(cached + "\n")
        return 0

    client = LlmClient(cfg.api)
    try:
        if args.no_stream:
            result = client.chat(messages)
            print(result)
        else:
            result = client.chat_stream(messages, lambda t: (sys.stdout.write(t), sys.stdout.flush()))
            sys.stdout.write("\n")
        cache.put(ck, result)
        return 0
    except CancelledError:
        return 130
    except LlmError as e:
        print(f"\n错误：{e}", file=sys.stderr)
        return 1


def cmd_check(args: argparse.Namespace) -> int:
    try:
        cfg = load_config()
    except ConfigError as e:
        print(f"✗ 配置无效：{e}", file=sys.stderr)
        return 1
    print(f"✓ 配置有效：{config_path()}")
    print(f"  API：{cfg.api.base_url}（模型 {cfg.api.model}）")
    api_key = cfg.api.resolve_api_key()
    print(f"  API Key：{'已配置（' + str(len(api_key)) + ' 字符）' if api_key else '⚠ 未配置'}")
    print(f"  提示词：{', '.join(cfg.prompts)}")
    for h in cfg.hotkeys:
        print(f"  快捷键：{h.key} → “{h.prompt}”")

    adapter = get_adapter()
    issues = adapter.permission_issues()
    if issues:
        print("✗ 权限问题：")
        for i in issues:
            print(f"  - {i}")
    else:
        print("✓ 系统权限检查通过（或当前平台无权限要求）")

    if args.hotkeys:
        print("→ 尝试注册快捷键（检测是否被占用）…")
        failed = False
        for h in cfg.hotkeys:
            try:
                adapter.register_hotkey(h.key, lambda: None)
                print(f"  ✓ {h.key} → “{h.prompt}” 注册成功")
            except AdapterError as e:
                failed = True
                print(f"  ✗ {e}", file=sys.stderr)
        if failed:
            print("提示：冲突的快捷键请在配置文件中更换组合后重启本工具。", file=sys.stderr)
            return 1

    if args.ping:
        print("→ 正在向 API 发送最小请求测试…")
        if not api_key:
            print("✗ 未配置 API Key，跳过", file=sys.stderr)
            return 1
        prompt = next(iter(cfg.prompts.values()))
        hotkey = cfg.default_hotkey()
        source = (hotkey.source_lang if hotkey else None) or prompt.source_lang or cfg.api.source_lang
        target = (hotkey.target_lang if hotkey else None) or prompt.target_lang or cfg.api.target_lang
        messages = render_messages(prompt, "ping", source, target)
        try:
            client = LlmClient(cfg.api)
            result = client.chat(messages)
            print(f"✓ API 连通：{result[:120]}{'…' if len(result) > 120 else ''}")
        except LlmError as e:
            print(f"✗ API 测试失败：{e}", file=sys.stderr)
            return 1
    return 0


def cmd_config(args: argparse.Namespace) -> int:
    path = config_path()
    if args.init:
        try:
            write_example(path, hotkey=args.hotkey or "alt+q")
            print(f"已生成示例配置：{path}")
            print("请编辑其中的 api_key/model/base_url 与提示词后，运行 stranslate-lite check --ping 验证。")
        except ConfigError as e:
            print(f"错误：{e}", file=sys.stderr)
            return 1
    elif args.path:
        print(path)
    else:
        print(path)
    return 0


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="stranslate-lite", description="快捷键划词 + LLM 调用小工具")
    parser.add_argument("--version", action="version", version=f"stranslate-lite {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="启动后台工具（全局热键 + 悬浮窗）")
    p_run.set_defaults(func=cmd_run)

    p_t = sub.add_parser("translate", help="一次性调用 LLM")
    p_t.add_argument("text", nargs="?", help="要处理的文本")
    p_t.add_argument("--stdin", action="store_true", help="从标准输入读取文本")
    p_t.add_argument("--prompt", help="使用指定提示词（需有快捷键绑定到它）")
    p_t.add_argument("--hotkey", type=int, help="使用第 N 个快捷键对应的提示词（从 0 起）")
    p_t.add_argument("--no-stream", action="store_true", help="禁用流式输出")
    p_t.set_defaults(func=cmd_translate)

    p_c = sub.add_parser("check", help="校验配置与权限")
    p_c.add_argument("--ping", action="store_true", help="发送最小请求测试 API 连通性")
    p_c.add_argument("--hotkeys", action="store_true", help="尝试注册每个快捷键并报告冲突")
    p_c.set_defaults(func=cmd_check)

    p_cfg = sub.add_parser("config", help="配置管理")
    p_cfg.add_argument("--init", action="store_true", help="生成示例配置文件（已存在则报错）")
    p_cfg.add_argument("--path", action="store_true", help="打印配置文件路径")
    p_cfg.add_argument("--hotkey", help="示例配置中的第一个快捷键（默认 alt+q）")
    p_cfg.set_defaults(func=cmd_config)

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
