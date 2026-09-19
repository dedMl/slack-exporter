# -*- coding: utf-8 -*-
"""Log into Slack with Playwright and save the session token (cookie `d` = xoxc).

Supported login methods: password, email code, 2FA (MFA).
The email is auto-filled; codes / MFA must be entered manually in the
browser window while the script polls for a successful login.

To avoid Slack's "unsupported browser" warning:
  - prefer a locally installed Chrome / Edge (real browser fingerprint)
  - inject a script to hide navigator.webdriver and other automation hints
  - use a dedicated persistent user dir (.profile/) to keep login state
"""
import json
import re
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright, Error as PWError

# When frozen with PyInstaller, keep session/.profile next to the EXE
if getattr(sys, "frozen", False):
    ROOT = Path(sys.executable).resolve().parent
else:
    ROOT = Path(__file__).resolve().parent
SESSION_FILE = ROOT / "session.json"
STATE_FILE = ROOT / "storage.json"
USER_DATA_DIR = ROOT / ".profile"

# UA matching the bundled Chromium (Chrome 151); used only when falling
# back to the bundled browser
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36")

# Hide automation fingerprints
STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
window.chrome = window.chrome || {runtime: {}};
Object.defineProperty(navigator, 'languages', {get: () => ['zh-CN', 'zh', 'en']});
Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
"""


def load_session():
    if SESSION_FILE.exists():
        return json.loads(SESSION_FILE.read_text(encoding="utf-8"))
    return None


def _cookie(context, name):
    for c in context.cookies("https://slack.com"):
        if c["name"] == name:
            return c["value"]
    return None


def _extract_xoxc(page, workspace_url, timeout_s=120):
    """Extract the API token (xoxc) from the client.

    The `d` cookie holds an xoxd session token; the API token must be taken
    from the client. Three fallbacks:
      1. listen for client API requests and read the Authorization header
         (most reliable)
      2. window.boot_data.api_token / inline JSON in the page
      3. match xoxc- in localStorage
    """
    captured = {}

    def on_request(req):
        try:
            auth = req.headers.get("authorization", "")
            if auth.startswith("Bearer xoxc-"):
                captured.setdefault("token", auth[len("Bearer "):])
        except Exception:
            pass

    page.on("request", on_request)

    # Make sure we are on the workspace client page (it keeps issuing API calls)
    try:
        if "app.slack.com" not in page.url and "/client" not in page.url:
            page.goto(workspace_url, wait_until="domcontentloaded", timeout=60000)
    except Exception:
        pass

    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if captured.get("token"):
            print("  Token captured from client API requests")
            return captured["token"]
        try:
            v = page.evaluate("window.boot_data && window.boot_data.api_token")
            if v and v.startswith("xoxc-"):
                print("  Token extracted from boot_data")
                return v
        except Exception:
            pass
        try:
            m = re.search(r'"api_token"\s*:\s*"(xoxc-[^"]+)"', page.content())
            if m:
                print("  Token extracted from inline page data")
                return m.group(1)
        except Exception:
            pass
        try:
            v = page.evaluate(
                "() => { for (let i = 0; i < localStorage.length; i++) {"
                " const v = localStorage.getItem(localStorage.key(i)) || '';"
                " const m = v.match(/xoxc-[A-Za-z0-9-]+/); if (m) return m[0]; }"
                " return null; }")
            if v:
                print("  Token extracted from localStorage")
                return v
        except Exception:
            pass
        page.wait_for_timeout(2000)
    return captured.get("token")


def _launch(p, headless):
    """Prefer local Chrome / Edge (real fingerprint), fall back to Chromium."""
    common = dict(
        headless=headless,
        args=["--disable-blink-features=AutomationControlled"],
        ignore_default_args=["--enable-automation"],
        locale="zh-CN",
        timezone_id="Asia/Shanghai",
        viewport={"width": 1440, "height": 900},
    )
    USER_DATA_DIR.mkdir(exist_ok=True)
    if headless:
        # Headless UA contains "HeadlessChrome" which Slack may reject;
        # disguise as regular Chrome
        common["user_agent"] = UA
    for channel in ("chrome", "msedge"):
        try:
            ctx = p.chromium.launch_persistent_context(
                str(USER_DATA_DIR), channel=channel, **common)
            print(f"  Using local {channel} browser")
            return ctx
        except PWError:
            continue
    ctx = p.chromium.launch_persistent_context(
        str(USER_DATA_DIR), user_agent=UA, **common)
    print("  Local Chrome/Edge not found - using bundled Chromium")
    return ctx


def _autofill(page, email, password):
    """Auto-fill the email; fill the password only if both input and config
    are present. Email codes / MFA cannot be automated and must be entered
    manually in the browser.
    """
    try:
        email_input = page.locator(
            "input#email, input[name='email'], input[type='email']").first
        email_input.wait_for(state="visible", timeout=10000)
        email_input.fill(email)
        page.keyboard.press("Enter")
        print(f"  Email {email} auto-filled and submitted")
    except Exception:
        print("  [!] Could not auto-fill the email - enter it manually in the browser")
        return
    page.wait_for_timeout(3000)

    if password:
        try:
            pw = page.locator("input#password, input[type='password']").first
            pw.wait_for(state="visible", timeout=8000)
            pw.fill(password)
            btn = page.locator("button[type='submit']").first
            if btn.count():
                btn.click()
            print("  Password auto-filled and submitted")
        except Exception:
            print("  No password field (email code login) - enter the code sent to your email")
    else:
        print("  -> No password configured: enter the email code in the browser (and MFA code if prompted)")


def login(workspace_url, email="", password="", headless=False, timeout_s=1800):
    """Open a browser, log into Slack, return and persist the session dict."""
    if headless:
        print("Note: email codes / MFA require manual input - "
              "headful mode (without --headless) is recommended")

    with sync_playwright() as p:
        context = _launch(p, headless)
        context.add_init_script(STEALTH_JS)
        page = context.pages[0] if context.pages else context.new_page()

        print(f"Opening {workspace_url} ...")
        try:
            page.goto(workspace_url, wait_until="domcontentloaded", timeout=60000)
        except Exception as e:
            print(f"  [!] Page load error ({e}), continuing anyway ...")
        page.wait_for_timeout(3000)

        token = _cookie(context, "d")
        if not token:
            if email:
                _autofill(page, email, password)
            token = _cookie(context, "d")
            if not token:
                print("Complete the login in the opened browser window "
                      "(email code / MFA can be entered manually), "
                      f"waiting up to {timeout_s // 60} minutes; "
                      "the script continues automatically once logged in ...")
                deadline = time.time() + timeout_s
                while time.time() < deadline:
                    if _cookie(context, "d"):
                        break
                    page.wait_for_timeout(1000)
            token = _cookie(context, "d")

        if not token:
            context.close()
            raise SystemExit("Login failed: no session token obtained, please retry")

        if not token.startswith("xoxc-"):
            print("Existing session detected - extracting API token (xoxc) from the client ...")
            token = _extract_xoxc(page, workspace_url) or token
        if not token.startswith("xoxc-"):
            context.close()
            raise SystemExit("Logged in but failed to extract an xoxc API token, please retry")

        cookies = [{"name": c["name"], "value": c["value"],
                    "domain": c.get("domain", "")}
                   for c in context.cookies("https://slack.com")]
        d_s = _cookie(context, "d-s") or ""
        session = {
            "token": token,
            "cookies": cookies,
            "d_s": d_s,
            "workspace_url": workspace_url,
            "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        SESSION_FILE.write_text(
            json.dumps(session, ensure_ascii=False, indent=2), encoding="utf-8")
        try:
            context.storage_state(path=str(STATE_FILE))
        except Exception:
            pass
        print("Login successful - session saved to session.json "
              "(login state kept in .profile/)")
        context.close()
        return session


if __name__ == "__main__":
    cfg_file = ROOT / "config.json"
    cfg = json.loads(cfg_file.read_text(encoding="utf-8")) if cfg_file.exists() else {}
    login(cfg.get("workspace_url", "https://slack.com/signin"),
          cfg.get("email", ""), cfg.get("password", ""))
