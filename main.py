# -*- coding: utf-8 -*-
"""Slack export tool - entry point.

Usage:
  python main.py                  # interactive wizard: scope/mode/HTML
  python main.py login            # login only (saves session.json)
  python main.py export           # export data only (login required)
  python main.py html             # build offline HTML viewer only
  python main.py [all]            # login + export + HTML

Key options:
  --mode browser|api     export mode (default browser: simulate user scrolling)
  --scope dm,channels,docs
                         comma-separated export scope (default: all)
  --update incremental|full
                         incremental merge (default) or full re-export
  --headless             headless login (not recommended if captcha/MFA needed)
  --relogin              ignore saved session and force re-login
  --no-files             skip attachment downloads
  --channel KEYWORD      only export channels whose name contains KEYWORD
  --refresh-list         force refresh of the channel list cache
"""
import argparse
import json
import shutil
import sys
import time
from pathlib import Path

# When frozen with PyInstaller, keep data/config next to the EXE
if getattr(sys, "frozen", False):
    ROOT = Path(sys.executable).resolve().parent
else:
    ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from login import login, load_session                    # noqa: E402
from export import run_export, run_docs_export, SlackError  # noqa: E402
from browser_export import run_browser_export            # noqa: E402
from htmlgen import run_html                             # noqa: E402

try:
    from playwright._impl._errors import TargetClosedError
except ImportError:  # fallback for older playwright versions
    class TargetClosedError(Exception):
        pass


def load_config() -> dict:
    cfg_file = ROOT / "config.json"
    if not cfg_file.exists():
        example = ROOT / "config.example.json"
        if example.exists():
            shutil.copy(example, cfg_file)
            print(f"已从模板创建 {cfg_file}，请编辑后重新运行（workspace_url 必填）")
        else:
            print("缺少 config.json，请参考 config.example.json 手动创建")
        _pause_if_frozen()
        sys.exit(1)
    return json.loads(cfg_file.read_text(encoding="utf-8"))


def _pause_if_frozen():
    """Pause on EXE double-click launch so output stays visible."""
    if getattr(sys, "frozen", False):
        try:
            input("\n按回车键退出…")
        except EOFError:
            pass


def ensure_session(cfg, args):
    s = None if args.relogin else load_session()
    # Legacy xoxd session tokens cannot call the API; re-login to get xoxc
    if not s or not s.get("token") or not s["token"].startswith("xoxc-"):
        s = login(cfg["workspace_url"], cfg.get("email", ""),
                  cfg.get("password", ""), headless=args.headless)
    return s


def _ask(prompt: str) -> str:
    """input wrapper: return empty string on EOF (use default)."""
    try:
        return input(prompt).strip()
    except EOFError:
        return ""


def ask_interactive():
    """Interactive wizard when run without args.

    Returns (scope set, full export flag, generate HTML flag).
    """
    print()
    print("=" * 50)
    print("  Slack 记录导出工具 · 交互引导")
    print("=" * 50)

    # ① export scope
    print("\n① 导出哪些数据？")
    print("   1) 私信/群组（DM）")
    print("   2) 频道（Channels）")
    print("   3) 画板文档（Slack Docs，导出为离线 HTML）")
    print("   4) 以上全部")
    scope = None
    while scope is None:
        c = _ask("   请选择 [1/2/3/4，可逗号组合如 1,3，默认 4]：")
        if not c:
            c = "4"
        pick = {x.strip() for x in c.split(",")}
        if pick == {"4"}:
            scope = {"dm", "channels", "docs"}
        elif pick and pick <= {"1", "2", "3"}:
            scope = set()
            if "1" in pick:
                scope.add("dm")
            if "2" in pick:
                scope.add("channels")
            if "3" in pick:
                scope.add("docs")
        if scope is None:
            print("   输入无效，请输入 1/2/3/4（多选用逗号分隔）")

    # ② update mode
    print("\n② 导出方式？")
    print("   1) 增量更新（只拉取上次导出之后的新消息，快，推荐）")
    print("   2) 全量导出（忽略已有数据重新完整拉取，耗时长）")
    full = None
    while full is None:
        c = _ask("   请选择 [1/2，默认 1]：")
        if not c or c == "1":
            full = False
        elif c == "2":
            full = True
        else:
            print("   输入无效，请输入 1 或 2")

    # ③ offline HTML viewer
    print("\n③ 导出完成后是否重新生成离线 HTML 浏览器？")
    c = _ask("   是/否 [Y/n]：").lower()
    want_html = c not in ("n", "no", "否")

    names = {"dm": "私信/群组", "channels": "频道", "docs": "画板文档"}
    print(f"\n已选择：{' + '.join(names[s] for s in sorted(scope))}"
          f"；{'全量' if full else '增量'}导出"
          f"；{'生成' if want_html else '不生成'} HTML\n")
    return scope, full, want_html


def main():
    ap = argparse.ArgumentParser(description="Slack 记录导出工具")
    ap.add_argument("command", nargs="?", default="all",
                    choices=["login", "export", "html", "all"],
                    help="执行的步骤，默认 all")
    ap.add_argument("--headless", action="store_true", help="无头浏览器登录")
    ap.add_argument("--relogin", action="store_true", help="强制重新登录")
    ap.add_argument("--no-files", action="store_true", help="不下载附件")
    ap.add_argument("--channel", default="", help="只导出名称包含关键字的频道")
    ap.add_argument("--refresh-list", action="store_true",
                    help="强制重新拉取频道列表（默认用缓存）")
    ap.add_argument("--mode", choices=["browser", "api"], default="browser",
                    help="导出方式：browser=模拟用户滚动加载（默认，稳）；"
                         "api=直接调 API（快，易限流）")
    ap.add_argument("--scope", default="dm,channels,docs",
                    help="导出范围，逗号分隔可多选：dm=私信/群组，"
                         "channels=频道，docs=画板文档（默认全部）")
    ap.add_argument("--update", choices=["incremental", "full"],
                    default="incremental",
                    help="更新方式：incremental=增量（默认，只拉新消息）；"
                         "full=全量（忽略已有数据重新导出）")
    ap.add_argument("--force", action="store_true",
                    help="等同 --update full（兼容旧参数）")
    args = ap.parse_args()

    # No args -> interactive wizard; otherwise follow CLI args
    interactive = len(sys.argv) == 1
    if interactive:
        scope, full, want_html = ask_interactive()
    else:
        raw_scope = args.scope or "dm,channels,docs"
        scope = {s.strip() for s in raw_scope.split(",") if s.strip()}
        bad = scope - {"dm", "channels", "docs"}
        if bad:
            sys.exit(f"--scope 取值无效：{bad}（可选 dm / channels / docs）")
        full = args.force or args.update == "full"
        want_html = args.command in ("html", "all")

    cfg = load_config()
    out_dir = ROOT / cfg.get("output_dir", "output")

    if args.command in ("login", "all"):
        ensure_session(cfg, args)

    if args.command in ("export", "all"):
        session = ensure_session(cfg, args)
        exp_cfg = dict(cfg)
        exp_cfg["download_files"] = cfg.get("download_files", True) and not args.no_files
        exp_cfg["channel_filter"] = args.channel
        want_msg = "dm" in scope or "channels" in scope
        # Message scope: both -> all (None); exactly one -> that scope
        msg_scope = None
        if "dm" in scope and "channels" not in scope:
            msg_scope = "dm"
        elif "channels" in scope and "dm" not in scope:
            msg_scope = "channels"
        scope_desc = {"dm": "私信/群组", "channels": "频道",
                      "docs": "画板文档"}
        print(f"导出范围：{' + '.join(scope_desc[s] for s in scope)}；"
              f"方式：{'全量' if full else '增量'}", flush=True)
        try:
            if want_msg:
                if args.mode == "browser":
                    # Chrome may crash after long runs (TargetClosedError);
                    # export is resumable, so auto-restart and continue
                    crash = 0
                    while True:
                        try:
                            run_browser_export(session, exp_cfg, out_dir,
                                               force=full,
                                               refresh_list=args.refresh_list,
                                               scope=msg_scope)
                            break  # finished normally
                        except TargetClosedError:
                            crash += 1
                            if crash > 50:
                                sys.exit("Chrome 反复崩溃超过 50 次，终止。")
                            print(f"\n[!] Chrome 崩溃（第 {crash} 次），"
                                  f"10 秒后重启导出（断点续传）…", flush=True)
                            time.sleep(10)
                else:
                    if msg_scope:
                        print("提示：api 模式暂不支持 --scope 过滤，"
                              "将导出全部消息类对话", flush=True)
                    run_export(session, exp_cfg, out_dir,
                               force=full, refresh_list=args.refresh_list)
            if "docs" in scope:
                if not (out_dir / "raw").exists():
                    print("无已导出消息，画板文档需先导出消息（--scope dm,"
                          "channels）才能发现", flush=True)
                else:
                    run_docs_export(session, out_dir, force=full)
        except SlackError as e:
            sys.exit(f"导出失败：{e}")

    if want_html:
        raw = out_dir / "raw"
        if not raw.exists():
            if interactive:
                _pause_if_frozen()
            sys.exit("未找到导出数据，请先运行 python main.py export")
        index = run_html(out_dir)
        print(f"用浏览器打开即可离线浏览：{index}")
    if interactive:
        # Keep the console window open on EXE double-click launch
        _pause_if_frozen()


if __name__ == "__main__":
    main()
