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
            print(f"Created {cfg_file} from the template - edit it and re-run"
                  f" (workspace_url is required)")
        else:
            print("config.json not found - create it from config.example.json")
        _pause_if_frozen()
        sys.exit(1)
    return json.loads(cfg_file.read_text(encoding="utf-8"))


def _pause_if_frozen():
    """Pause on EXE double-click launch so output stays visible."""
    if getattr(sys, "frozen", False):
        try:
            input("\nPress Enter to exit…")
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
    print("  Slack Export Tool - Interactive Wizard")
    print("=" * 50)

    # ① export scope
    print("\n[1/3] What do you want to export?")
    print("   1) Direct messages & groups (DM)")
    print("   2) Channels")
    print("   3) Canvas docs (Slack Docs, exported as offline HTML)")
    print("   4) All of the above")
    scope = None
    while scope is None:
        c = _ask("   Select [1/2/3/4, comma-separated like 1,3, default 4]: ")
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
            print("   Invalid input - enter 1/2/3/4 (comma-separated for multiple)")

    # ② update mode
    print("\n[2/3] Update mode?")
    print("   1) Incremental (only fetch new messages since last export - fast, recommended)")
    print("   2) Full re-export (ignore existing data and fetch everything - slow)")
    full = None
    while full is None:
        c = _ask("   Select [1/2, default 1]: ")
        if not c or c == "1":
            full = False
        elif c == "2":
            full = True
        else:
            print("   Invalid input - enter 1 or 2")

    # ③ offline HTML viewer
    print("\n[3/3] Rebuild the offline HTML viewer after export?")
    c = _ask("   Yes/No [Y/n]: ").lower()
    want_html = c not in ("n", "no")

    names = {"dm": "DM", "channels": "channels", "docs": "canvas docs"}
    print(f"\nSelected: {' + '.join(names[s] for s in sorted(scope))}; "
          f"{'full' if full else 'incremental'} export; "
          f"{'generate' if want_html else 'skip'} HTML\n")
    return scope, full, want_html


def main():
    ap = argparse.ArgumentParser(description="Slack export tool")
    ap.add_argument("command", nargs="?", default="all",
                    choices=["login", "export", "html", "all"],
                    help="step to run (default: all)")
    ap.add_argument("--headless", action="store_true", help="headless browser login")
    ap.add_argument("--relogin", action="store_true", help="force re-login")
    ap.add_argument("--no-files", action="store_true", help="skip attachment downloads")
    ap.add_argument("--channel", default="",
                    help="only export channels whose name contains KEYWORD")
    ap.add_argument("--refresh-list", action="store_true",
                    help="force refresh of the channel list (cached by default)")
    ap.add_argument("--mode", choices=["browser", "api"], default="browser",
                    help="export mode: browser=simulate user scrolling "
                         "(default, stable); api=direct API calls (fast, rate-limited)")
    ap.add_argument("--scope", default="dm,channels,docs",
                    help="export scope, comma-separated: dm, channels, docs "
                         "(default: all)")
    ap.add_argument("--update", choices=["incremental", "full"],
                    default="incremental",
                    help="update mode: incremental (default, only new messages) "
                         "or full (re-export everything)")
    ap.add_argument("--force", action="store_true",
                    help="same as --update full (legacy alias)")
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
            sys.exit(f"Invalid --scope value(s): {bad} "
                     f"(allowed: dm / channels / docs)")
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
        scope_desc = {"dm": "DM", "channels": "channels", "docs": "canvas docs"}
        print(f"Export scope: {' + '.join(scope_desc[s] for s in scope)}; "
              f"mode: {'full' if full else 'incremental'}", flush=True)
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
                                sys.exit("Chrome crashed more than 50 times, aborting.")
                            print(f"\n[!] Chrome crashed (attempt {crash}), "
                                  f"restarting export in 10s (resumable)...",
                                  flush=True)
                            time.sleep(10)
                else:
                    if msg_scope:
                        print("Note: api mode does not support --scope filtering yet, "
                              "all message conversations will be exported", flush=True)
                    run_export(session, exp_cfg, out_dir,
                               force=full, refresh_list=args.refresh_list)
            if "docs" in scope:
                if not (out_dir / "raw").exists():
                    print("No exported messages found - run the message export first "
                          "(--scope dm,channels) to discover canvas docs", flush=True)
                else:
                    run_docs_export(session, out_dir, force=full)
        except SlackError as e:
            sys.exit(f"Export failed: {e}")

    if want_html:
        raw = out_dir / "raw"
        if not raw.exists():
            if interactive:
                _pause_if_frozen()
            sys.exit("No exported data found - run python main.py export first")
        index = run_html(out_dir)
        print(f"Open this file in a browser to view offline: {index}")
    if interactive:
        # Keep the console window open on EXE double-click launch
        _pause_if_frozen()


if __name__ == "__main__":
    main()
