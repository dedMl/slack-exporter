# -*- coding: utf-8 -*-
"""Browser-mode export (default): open each channel in the Slack client,
scroll up to load history like a user, and capture the client's own
conversations.* responses to collect messages.

Compared to direct API calls:
  - history is loaded by the client itself (built-in throttling/retry, no
    rate limiting unlike raw API calls)
  - thread replies and attachments still use the Web API (small volume,
    with rate-limit/network retry and pacing)
  - speed is configurable (config.json: channel_delay_s / scroll_wait_ms /
    thread_pace_s)
  - resumable: channels with raw/<channel_id>.json are skipped (--force
    forces a redo)
  - DMs/group DMs exported first, channels last
"""
import json
import random
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlparse, parse_qs

from playwright.sync_api import sync_playwright

from login import _launch, STEALTH_JS
from export import (SlackAPI, SlackError, TYPE_ORDER, _channel_title,
                    load_users, make_meta, save_channel,
                    write_index, write_meta_json)

# Endpoints the client calls when loading history (capturing their responses
# yields messages). The new client returns the first screen of history via
# conversations.view when opening a channel; scrolling up pages through
# conversations.history / loadLastMessages.
CAPTURE_NAMES = {"conversations.view", "conversations.history",
                 "conversations.replies", "conversations.loadLastMessages",
                 "conversations.loadLatestMessages"}

# Detect the message list scroll container reaching the top (container class
# names vary across client versions, so search generically; if no container
# is found return False and rely on the idle counter fallback)
AT_TOP_JS = """
() => {
  let best = null;
  for (const el of document.querySelectorAll('*')) {
    if (el.scrollHeight > el.clientHeight + 50 && el.clientHeight > 200)
      if (best === null || el.scrollHeight > best.scrollHeight) best = el;
  }
  return best ? best.scrollTop < 80 : false;
}
"""

# Marker shown when a conversation is scrolled to its very beginning
BEGINNING_JS = """
() => {
  const t = document.body.innerText || '';
  return /的开头/.test(t)
      || /the very beginning/i.test(t)
      || /beginning of (this|the) (conversation|channel|history)/i.test(t);
}
"""

# Virtual list loading indicator while fetching older history at the top
LOADING_JS = """
() => {
  const t = document.body.innerText || '';
  return /正在加载/.test(t) || /loading (history|messages)/i.test(t)
      || !!document.querySelector('[data-qa="loading_history"], [data-qa="loading"]');
}
"""


def _channel_from_request(req, url, fallback=None):
    """Parse the channel id from a request: query -> form/JSON body ->
    multipart body."""
    q = parse_qs(urlparse(url).query).get("channel")
    if q:
        return q[0]
    body = req.post_data or ""
    if not body:
        return fallback
    # form-urlencoded: channel=C123 / JSON: "channel":"C123"
    m = re.search(r'"?channel"?\s*[=:]\s*"?([A-Z][A-Za-z0-9]+)"?', body)
    if m:
        return m.group(1)
    # multipart/form-data: name="channel"\r\n\r\nC123
    m = re.search(r'name="channel"\s*\r?\n\s*\r?\n([A-Z][A-Za-z0-9]+)', body)
    if m:
        return m.group(1)
    return fallback


class ResponseCapture:
    """Listen to page responses and collect client-fetched messages per
    channel (deduplicated by ts).

    Data sources:
      - conversations.view: first screen when opening a channel
        (data.history.messages; channel id taken from data.channel.id in
        the response, most reliable)
      - conversations.history / loadLastMessages: pages triggered by
        scrolling up (data.messages, merged with data.latest_updates --
        the latest edited versions)

    Also tracks:
      - has_more[cid]: whether the last response carrying messages has
        earlier history, as the server-side "reached the top" signal
        (has_more from empty responses is unreliable)
      - channel_info[cid]: the channel object from view responses (fills
        conversation metadata for bare conversation ids collected on /dm)
    """

    def __init__(self):
        self._store = {}
        self.current = None
        self.viewed = set()   # channel ids with a view response received
        self.saw_any = False
        self.has_more = {}
        self.channel_info = {}

    def on_response(self, resp):
        try:
            url = resp.url
            if "conversations." not in url:
                return
            name = url.split("?")[0].split("/")[-1]
            if name not in CAPTURE_NAMES:
                return
            data = resp.json()
            if not isinstance(data, dict):
                return
            if name == "conversations.view":
                ch = data.get("channel") or {}
                cid = ch.get("id") or _channel_from_request(
                    resp.request, url, fallback=self.current)
                hist = data.get("history")
                msgs = (hist.get("messages") or []) if isinstance(hist, dict) else []
                if cid:
                    self.viewed.add(cid)
                    self.saw_any = True
                    if isinstance(ch, dict) and ch.get("id"):
                        self.channel_info[cid] = ch
            else:
                cid = _channel_from_request(resp.request, url,
                                            fallback=self.current)
                msgs = (data.get("messages") or [])
                lu = data.get("latest_updates")
                if isinstance(lu, list):
                    msgs = msgs + lu
            if not msgs or not cid:
                return
            self.saw_any = True
            if name == "conversations.view":
                hm = (data.get("history") or {}).get("has_more")
            else:
                hm = data.get("has_more")
            if hm is not None:
                self.has_more[cid] = bool(hm)
            st = self._store.setdefault(cid, {})
            for m in msgs:
                if isinstance(m, dict) and m.get("ts"):
                    # Overwrite directly: later pages/edits are newer
                    st[m["ts"]] = m
        except Exception:
            pass

    def count(self, cid):
        return len(self._store.get(cid, {}))

    def take(self, cid):
        st = self._store.pop(cid, {})
        return sorted(st.values(), key=lambda m: float(m["ts"]))


def _detect_team(page, session):
    """Get the team id: prefer the one saved in session, otherwise open the
    workspace and poll the redirect URL."""
    if session.get("team_id"):
        return session["team_id"]
    ws = session.get("workspace_url", "https://slack.com")
    print(f"打开 {ws} …", flush=True)
    try:
        page.goto(ws, wait_until="domcontentloaded", timeout=60000)
    except Exception as e:
        print(f"  [!] 页面加载异常（{e}），继续尝试 …", flush=True)
    team = None
    deadline = time.time() + 90
    while time.time() < deadline and not team:
        m = re.search(r"app\.slack\.com/client/([A-Z][A-Za-z0-9]*)/", page.url)
        if m:
            team = m.group(1)
            break
        try:
            v = page.evaluate(
                "window.boot_data && (window.boot_data.teamId "
                "|| window.boot_data.team_id) || null")
            if v and re.fullmatch(r"[A-Z][A-Za-z0-9]+", str(v)):
                team = str(v)
                break
        except Exception:
            pass
        page.wait_for_timeout(1000)
    if not team:
        raise SystemExit("未能识别工作区 team id，请运行 python main.py login 重新登录")
    # Persist so later runs don't depend on the redirect
    try:
        session["team_id"] = team
        ROOT2 = (Path(sys.executable).resolve().parent
                 if getattr(sys, "frozen", False)
                 else Path(__file__).resolve().parent)
        (ROOT2 / "session.json").write_text(
            json.dumps(session, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass
    return team


def _wait_view(page, cap, cid, timeout_s=25):
    """Wait for the conversations.view response of the target channel
    (client has really opened it).

    Note: with a local cache the view response may carry no messages, so we
    only wait for the view itself; full history comes from scrolling up.
    """
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if page.locator("input#email, input[name='email'], input[type='email']").count():
            raise SystemExit("浏览器登录态已失效，请运行 python main.py login --relogin")
        if cid in cap.viewed:
            # Brief extra wait for first-screen requests to finish
            page.wait_for_timeout(2500)
            return True
        page.wait_for_timeout(500)
    return False


def _jump_via_search(page, title):
    """Jump into a channel via Ctrl+K search like a user (fallback when the
    direct URL fails)."""
    kw = (title or "").strip()[:30]
    if not kw:
        return
    try:
        page.keyboard.press("Control+k")
        page.wait_for_timeout(1500 + random.randint(0, 500))
        page.keyboard.type(kw, delay=60)
        page.wait_for_timeout(1800 + random.randint(0, 600))
        page.keyboard.press("Enter")
        page.wait_for_timeout(2000)
        # Close a possibly lingering search panel
        page.keyboard.press("Escape")
    except Exception:
        pass


def _scroll_history(page, cap, cid, cfg):
    """Scroll up continuously like a user until the conversation beginning.

    Stop signals (any one suffices):
      1. the "beginning of conversation" marker appears (most reliable)
      2. server confirms no earlier history (has_more=False on the last
         message-carrying response) plus several idle rounds
      3. long time with no new content after reaching the top (virtual
         lists are occasionally sticky, be tolerant)
      4. scroll count/time cap reached (hard stop against infinite loops)
    """
    wait_ms = int(cfg.get("scroll_wait_ms", 1200))
    max_scrolls = int(cfg.get("max_scrolls", 5000))
    max_seconds = int(cfg.get("max_channel_seconds", 1800))
    idle_limit = int(cfg.get("scroll_idle_limit", 15))
    vp = page.viewport_size or {"width": 1440, "height": 900}
    page.mouse.move(vp["width"] * 0.55, vp["height"] * 0.5)

    def at_beginning():
        try:
            return page.evaluate(BEGINNING_JS)
        except Exception:
            return False

    last = cap.count(cid)
    idle = 0
    empty_start = last == 0   # 0 first-screen messages: likely an empty
                              # conversation, confirm quickly
    start = time.time()
    for n in range(1, max_scrolls + 1):
        if time.time() - start > max_seconds:
            print(f"    达到单频道时间上限（{last} 条），停止滚动", flush=True)
            break
        page.mouse.wheel(0, -random.randint(1100, 1600))
        page.wait_for_timeout(wait_ms + random.randint(0, 400))
        cnt = cap.count(cid)
        if cnt > last:
            last = cnt
            idle = 0
            if n % 20 == 0:
                print(f"    已加载 {cnt} 条 …", flush=True)
            continue
        idle += 1
        # No new messages: check the completion signals
        if idle >= 2 and at_beginning():
            print(f"    已到对话开头（共 {cnt} 条）", flush=True)
            break
        if empty_start and idle >= 4:
            # Empty first screen + several idle scrolls: empty conversation
            print("    首屏为空且滚动无果，视为空对话", flush=True)
            break
        # "loading history" stuck: wait up to 10s more before counting idle
        try:
            loading = page.evaluate(LOADING_JS)
        except Exception:
            loading = False
        if loading:
            for _ in range(10):
                page.wait_for_timeout(1000)
                if cap.count(cid) > last:
                    break
            if cap.count(cid) > last:
                last = cap.count(cid)
                idle = 0
                continue
            print("    加载指示卡住超 10s，滚回最新重新拉取 …", flush=True)
            # User-like recovery: scroll to the latest messages first, then
            # page up again to re-trigger the "load earlier history" sentinel
            for _ in range(6):
                page.mouse.wheel(0, 2200 + random.randint(0, 400))
                page.wait_for_timeout(280)
            page.wait_for_timeout(1200 + random.randint(0, 600))
            for _ in range(5):
                page.mouse.wheel(0, -(2000 + random.randint(0, 500)))
                page.wait_for_timeout(600 + random.randint(0, 300))
            if cap.count(cid) > last:
                last = cap.count(cid)
                idle = 0
            continue
        if cap.has_more.get(cid) is False and idle >= 3:
            print(f"    服务器确认已到头（共 {cnt} 条）", flush=True)
            break
        try:
            at_top = page.evaluate(AT_TOP_JS)
        except Exception:
            at_top = False
        if at_top:
            # At top but not confirmed done: the client may still be
            # prepending the next page, wait a bit longer
            page.wait_for_timeout(1600 + random.randint(0, 600))
            cnt2 = cap.count(cid)
            if cnt2 > last:
                last = cnt2
                idle = 0
                continue
            if idle >= idle_limit:
                print(f"    顶部再无新内容（共 {cnt2} 条）", flush=True)
                break
            # Stuck at the very top: wheel has no effect (scrollTop is 0
            # and the sentinel no longer fires); scroll down then up again
            # to recreate an "approaching top" event
            page.mouse.wheel(0, 500 + random.randint(0, 200))
            page.wait_for_timeout(400 + random.randint(0, 200))
            page.mouse.wheel(0, -(700 + random.randint(0, 200)))
            page.wait_for_timeout(1200 + random.randint(0, 400))
        elif idle >= idle_limit * 2:
            # Neither at top nor new messages: page may be unfocused/stuck
            print(f"    滚动无响应（共 {cnt} 条）", flush=True)
            break


def _fetch_threads(api, by_ts, cid, cfg):
    """Fetch replies for threaded messages via API (paced, with retry)."""
    pace = float(cfg.get("thread_pace_s", 0.5))
    tops = [m for m in by_ts.values()
            if m.get("reply_count") and m.get("thread_ts") == m.get("ts")]
    if not tops:
        return 0
    added = 0
    for m in tops:
        try:
            reps = api.paginate("conversations.replies", "messages",
                                channel=cid, ts=m["thread_ts"], limit=200)
        except SlackError as e:
            print(f"    线程获取失败：{e}", flush=True)
            time.sleep(pace)
            continue
        for r in reps:
            if r.get("ts") and r["ts"] not in by_ts:
                by_ts[r["ts"]] = r
                added += 1
        time.sleep(pace)
    return added


def _page_eval(page, js, args):
    """Tolerant page.evaluate wrapper: if the page context dies (e.g.
    "Failed to fetch" after a network blip, or navigation), reload the
    client and retry once."""
    try:
        return page.evaluate(js, args)
    except Exception as e:
        if "Failed to fetch" not in str(e) and "Execution context" not in str(e):
            raise
        print("    页面上下文异常，重载客户端重试 …", flush=True)
        m = re.match(r"(https://app\.slack\.com/client/[^/]+)", page.url or "")
        page.goto(m.group(1) if m else "https://app.slack.com",
                  wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(5000 + random.randint(0, 2000))
        return page.evaluate(js, args)


def _fetch_threads_via_page(page, session, by_ts, cid, cfg):
    """Fetch thread replies inside the page (same-origin requests like the
    client itself, avoiding per-thread API calls hitting rate limits across
    many channels).

    Returns (replies added, capped by limit, fetch failed). On failure the
    caller should set needs_retry in meta so the next run refetches
    (incremental updates don't re-pull old threads).
    """
    pace_ms = int(float(cfg.get("thread_pace_s", 0.6)) * 1000)
    max_threads = int(cfg.get("max_threads_per_channel", 3000))
    tops = [m["ts"] for m in by_ts.values()
            if m.get("reply_count") and m.get("thread_ts") == m.get("ts")]
    if not tops:
        return 0, False, False
    capped = len(tops) > max_threads
    if capped:
        print(f"    线程过多（{len(tops)}），本次只取前 {max_threads} 个（已标记）",
              flush=True)
        tops = tops[:max_threads]
    try:
        res = _page_eval(page, FETCH_REPLIES_JS,
                         {"token": session["token"], "channel": cid,
                          "threads": tops, "pace_ms": pace_ms}) or {}
    except Exception as e:
        print(f"    线程拉取异常：{e}", flush=True)
        return 0, capped, True
    added = 0
    for tts, replies in (res or {}).items():
        for m in replies or []:
            if isinstance(m, dict) and m.get("ts") and m["ts"] not in by_ts:
                by_ts[m["ts"]] = m
                added += 1
    return added, capped, False


# Fetch conversations.list inside the client page with its own credentials
# -- same-origin, same cookies as the client, indistinguishable to the
# server; returns all conversations/channels within seconds
# (userBoot only contains joined channels and recently active DMs, missing
# un-joined public channels and long-inactive DMs)
FETCH_DMS_JS = """
async (args) => {
  const sleep = (ms) => new Promise(r => setTimeout(r, ms));
  const out = [];
  let cursor = '';
  for (let page = 0; page < 50; page++) {
    const url = '/api/conversations.list?types=' + args.types
      + '&limit=200&exclude_archived=false'
      + (cursor ? '&cursor=' + encodeURIComponent(cursor) : '');
    let resp;
    for (let retry = 0; retry < 12; retry++) {
      resp = await fetch(url, {headers: {'Authorization': 'Bearer ' + args.token}});
      if (resp.status === 429) {
        const wait = (parseInt(resp.headers.get('retry-after') || '5') + 1) * 1000;
        await sleep(Math.min(wait, 35000));
        continue;
      }
      break;
    }
    if (resp.status !== 200) { out.push({error: 'HTTP ' + resp.status}); break; }
    const data = await resp.json();
    if (!data.ok) { out.push({error: data.error}); break; }
    for (const c of data.channels || [])
      out.push({id: c.id, user: c.user || null, name: c.name || null,
                is_mpim: !!c.is_mpim, is_private: !!c.is_private,
                created: c.created || null, is_archived: !!c.is_archived,
                topic: c.topic || null, purpose: c.purpose || null,
                num_members: c.num_members || null});
    cursor = (data.response_metadata || {}).next_cursor || '';
    if (!cursor) { out.push({done: true}); break; }
    await sleep(1500);
  }
  return out;
}
"""

# Incremental: fetch all messages (incl. thread replies) with ts > since and
# merge into the existing export file
INCREMENTAL_JS = """
async (args) => {
  const sleep = (ms) => new Promise(r => setTimeout(r, ms));
  const all = [];
  let cursor = '';
  for (let page = 0; page < 100; page++) {
    let url = '/api/conversations.history?channel=' + encodeURIComponent(args.channel)
      + '&oldest=' + args.since + '&limit=200';
    if (cursor) url += '&cursor=' + encodeURIComponent(cursor);
    let resp;
    for (let r = 0; r < 12; r++) {
      resp = await fetch(url, {headers: {'Authorization': 'Bearer ' + args.token}});
      if (resp.status === 429) {
        await sleep(Math.min((parseInt(resp.headers.get('retry-after') || '5') + 1) * 1000, 35000));
        continue;
      }
      break;
    }
    if (resp.status !== 200) return {messages: all, error: 'HTTP ' + resp.status};
    const data = await resp.json();
    if (!data.ok) return {messages: all, error: data.error};
    for (const m of data.messages || []) all.push(m);
    cursor = (data.response_metadata || {}).next_cursor || '';
    if (!cursor || !data.has_more) break;
    await sleep(800);
  }
  return {messages: all};
}
"""

# Thread replies: fetch all replies per parent message ts (same-origin page
# requests, paced)
FETCH_REPLIES_JS = """
async (args) => {
  const sleep = (ms) => new Promise(r => setTimeout(r, ms));
  const out = {};
  for (const tts of args.threads) {
    const replies = [];
    let cursor = '';
    for (let page = 0; page < 20; page++) {
      let url = '/api/conversations.replies?channel=' + encodeURIComponent(args.channel)
        + '&ts=' + encodeURIComponent(tts) + '&limit=200';
      if (cursor) url += '&cursor=' + encodeURIComponent(cursor);
      let resp;
      for (let r = 0; r < 8; r++) {
        resp = await fetch(url, {headers: {'Authorization': 'Bearer ' + args.token}});
        if (resp.status === 429) {
          await sleep(Math.min((parseInt(resp.headers.get('retry-after') || '3') + 1) * 1000, 30000));
          continue;
        }
        break;
      }
      if (resp.status !== 200) break;
      const data = await resp.json();
      if (!data.ok) break;
      for (const m of data.messages || []) replies.push(m);
      cursor = (data.response_metadata || {}).next_cursor || '';
      if (!cursor || !data.has_more) break;
      await sleep(700);
    }
    out[tts] = replies;
    await sleep(args.pace_ms || 600);
  }
  return out;
}
"""


# Verification: check whether messages older than `earliest` still exist
# (existence = scrolling did not load everything).
# Note Slack semantics: `latest` is the exclusive upper bound (returns
# ts < latest), so querying "older" requires latest; `oldest` is the lower
# bound (returns ts > oldest) -- don't mix them up.
VERIFY_EARLIER_JS = """
async (args) => {
  const sleep = (ms) => new Promise(r => setTimeout(r, ms));
  const url = '/api/conversations.history?channel=' + encodeURIComponent(args.channel)
    + '&latest=' + args.earliest + '&limit=5';
  let resp;
  for (let r = 0; r < 8; r++) {
    resp = await fetch(url, {headers: {'Authorization': 'Bearer ' + args.token}});
    if (resp.status === 429) {
      await sleep(Math.min((parseInt(resp.headers.get('retry-after') || '3') + 1) * 1000, 30000));
      continue;
    }
    break;
  }
  if (resp.status !== 200) return {error: 'HTTP ' + resp.status};
  const data = await resp.json();
  if (!data.ok) return {error: data.error};
  const ms = data.messages || [];
  return {older_count: ms.length,
          earliest: ms.length ? ms[ms.length - 1].ts : null};
}
"""

# Fallback: page back from `earliest` to fetch all older history (guarantees
# completeness when scrolling gets stuck)
BACKFILL_JS = """
async (args) => {
  const sleep = (ms) => new Promise(r => setTimeout(r, ms));
  const all = [];
  let cursor = '';
  for (let page = 0; page < 500; page++) {
    let url = '/api/conversations.history?channel=' + encodeURIComponent(args.channel)
      + '&latest=' + args.earliest + '&limit=200';
    if (cursor) url += '&cursor=' + encodeURIComponent(cursor);
    let resp;
    for (let r = 0; r < 12; r++) {
      resp = await fetch(url, {headers: {'Authorization': 'Bearer ' + args.token}});
      if (resp.status === 429) {
        await sleep(Math.min((parseInt(resp.headers.get('retry-after') || '5') + 1) * 1000, 35000));
        continue;
      }
      break;
    }
    if (resp.status !== 200) return {messages: all, error: 'HTTP ' + resp.status};
    const data = await resp.json();
    if (!data.ok) return {messages: all, error: data.error};
    for (const m of data.messages || []) all.push(m);
    cursor = (data.response_metadata || {}).next_cursor || '';
    if (!cursor || !data.has_more) break;
    await sleep(1200);
  }
  return {messages: all};
}
"""


def _verify_and_backfill(page, session, cid, msgs):
    """Verify scroll completeness: confirm with the server that no history
    older than the current earliest message exists.

    Among scroll stop reasons, "server confirmed top / beginning marker" is
    reliable; "no new content at top / unresponsive" may stop early due to
    sticky virtual lists (once caused months of DM history to be missed).
    So every non-empty channel gets verified:
      - no older messages -> complete, pass
      - older messages exist -> page-fetch and backfill in-page, then verify
        again; at most 3 rounds (a single round may stop early on
        has_more/cursor; the loop guarantees nothing is missed)
    Returns (merged messages, note text).
    """
    if not msgs:
        return msgs, ""
    by_ts = {m["ts"]: m for m in msgs}
    total_extra = 0
    for round_no in range(3):
        earliest = min(float(m["ts"]) for m in by_ts.values())
        r = None
        for _ in range(3):
            r = _page_eval(page, VERIFY_EARLIER_JS,
                           {"token": session["token"], "channel": cid,
                            "earliest": str(earliest)})
            if isinstance(r, dict) and not r.get("error"):
                break
            page.wait_for_timeout(3000)
        if not isinstance(r, dict) or r.get("error"):
            break   # verification API failed: keep current results
        if not r.get("older_count"):
            note = "✓" if total_extra == 0 else f"（校验补拉 {total_extra} 条）✓"
            return sorted(by_ts.values(), key=lambda m: float(m["ts"])), note
        print("    校验发现缺失更早历史，翻页补拉 …", flush=True)
        r2 = _page_eval(page, BACKFILL_JS,
                        {"token": session["token"], "channel": cid,
                         "earliest": str(earliest)})
        extra = (r2.get("messages") or []) if isinstance(r2, dict) else []
        if not extra:
            print(f"    补拉失败：{(r2 or {}).get('error')}", flush=True)
            break
        for m in extra:
            if m.get("ts"):
                by_ts[m["ts"]] = m
        total_extra += len(extra)
        merged = sorted(by_ts.values(), key=lambda m: float(m["ts"]))
        print(f"    第{round_no + 1}轮补拉 {len(extra)} 条，最早 {merged[0]['ts']}",
              flush=True)
    merged = sorted(by_ts.values(), key=lambda m: float(m["ts"]))
    note = "" if total_extra == 0 else f"（校验补拉 {total_extra} 条）"
    return merged, note


def _fetch_all_dms_via_page(page, base, session, types="im,mpim", label="对话"):
    if "app.slack.com" not in page.url:
        page.goto(base, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(4000)
    res = page.evaluate(FETCH_DMS_JS,
                        {"token": session["token"], "types": types})
    errs = [r.get("error") for r in res if r.get("error")]
    convs = [r for r in res if r.get("id")]
    done = any(r.get("done") for r in res)
    if errs and not convs:
        raise SystemExit(f"页面内获取{label}列表失败：{errs}")
    if not done:
        print(f"    [!] {label}列表可能未取全（{errs[:2]}），后续可重跑补齐",
              flush=True)
    n_mpim = sum(1 for c in convs if c.get("is_mpim"))
    print(f"页面内获取{label}列表：{len(convs)} 个"
          + (f"（其中群组 {n_mpim}）" if n_mpim else ""), flush=True)
    return convs


def _list_channels_via_browser(page, raw: Path, refresh=False, team=None):
    """Capture the channel list from the client's startup userBoot response
    (no API calls, no rate limits).

    userBoot returns all channels, groups and DMs visible to the current
    user, structurally similar to conversations.list (channels/mpims/ims
    stored separately).
    """
    cache = raw / "_channels_cache.json"
    if cache.exists() and not refresh:
        chans = json.loads(cache.read_text(encoding="utf-8"))
        print(f"使用频道列表缓存（{len(chans)} 个，--refresh-list 可刷新）",
              flush=True)
        return chans

    # Navigate into the client first if not there yet (userBoot is only
    # sent at client startup)
    if "app.slack.com/client" not in page.url:
        if not team:
            raise SystemExit("缺少 team id，无法打开客户端")
        page.goto(f"https://app.slack.com/client/{team}",
                  wait_until="domcontentloaded", timeout=60000)

    data = {}

    def on_userboot(resp):
        try:
            if "userBoot" not in resp.url:
                return
            d = resp.json()
            if isinstance(d, dict) and (d.get("channels") or d.get("ims")
                                        or d.get("mpims")):
                data.update(d)
        except Exception:
            pass

    page.on("response", on_userboot)
    try:
        deadline = time.time() + 90
        while time.time() < deadline and not data:
            page.reload(wait_until="domcontentloaded")
            page.wait_for_timeout(6000)
    finally:
        try:
            page.remove_listener("response", on_userboot)
        except Exception:
            pass

    if not data:
        raise SystemExit("未能截获 client.userBoot（频道列表），请重试或重新登录")

    chans = []
    for c in data.get("channels") or []:
        chans.append({
            "id": c.get("id"), "name": c.get("name"),
            "created": c.get("created"),
            "is_archived": bool(c.get("is_archived")),
            "is_private": bool(c.get("is_private")),
            "is_member": bool(c.get("is_member")),
            "topic": c.get("topic"), "purpose": c.get("purpose"),
            "num_members": c.get("num_members"),
            "type": "private_channel" if c.get("is_private") else "public_channel",
        })
    for c in data.get("mpims") or []:
        chans.append({
            "id": c.get("id"), "name": c.get("name"),
            "created": c.get("created"),
            "is_archived": bool(c.get("is_archived")),
            "is_private": True, "members": c.get("members"),
            "type": "mpim",
        })
    for c in data.get("ims") or []:
        chans.append({
            "id": c.get("id"), "user": c.get("user"),
            "created": c.get("created"),
            "is_archived": bool(c.get("is_archived")),
            "is_private": True, "type": "im",
        })

    # Merge old cache (keep extra info previously fetched via API, e.g.
    # num_members/members)
    if cache.exists():
        try:
            old = {c.get("id"): c for c in
                   json.loads(cache.read_text(encoding="utf-8"))}
            for c in chans:
                o = old.get(c.get("id"))
                if o:
                    for k in ("num_members", "members", "name", "topic", "purpose"):
                        if c.get(k) in (None, "", []) and o.get(k):
                            c[k] = o[k]
        except Exception:
            pass

    cache.write_text(json.dumps(chans, ensure_ascii=False), encoding="utf-8")
    n_by = {}
    for c in chans:
        n_by[c["type"]] = n_by.get(c["type"], 0) + 1
    print(f"userBoot 截获频道列表（共 {len(chans)} 个）：{n_by}", flush=True)
    return chans


def run_browser_export(session, cfg, out_dir: Path, force=False,
                       refresh_list=False, only_channels=None,
                       scope=None):
    """scope: export scope, None=all; 'dm'=DMs/group DMs only;
    'channels'=channels only. Used by main.py --scope."""
    raw = out_dir / "raw"
    files_dir = out_dir / "files"
    raw.mkdir(parents=True, exist_ok=True)
    api = SlackAPI(session["token"], session.get("cookies"),
                   session.get("d_s", ""))

    users = load_users(api, raw)

    delay = float(cfg.get("channel_delay_s", 2))
    done = 0
    with sync_playwright() as p:
        context = _launch(p, headless=cfg.get("headless", False))
        context.add_init_script(STEALTH_JS)
        page = context.pages[0] if context.pages else context.new_page()
        cap = ResponseCapture()
        page.on("response", cap.on_response)
        try:
            team = _detect_team(page, session)
            base = f"https://app.slack.com/client/{team}"
            if only_channels is not None:
                chans = list(only_channels)
                uboot_ids = {c["id"] for c in chans}
            else:
                # Capture joined channels from userBoot (with topic/purpose)
                chans = _list_channels_via_browser(page, raw, refresh_list,
                                                   team=team)
                uboot_ids = {c["id"] for c in chans}
                # DMs/group DMs + all visible channels: in-page
                # conversations.list fetch for the authoritative full set
                # (userBoot only has joined channels and recently active
                # DMs, missing un-joined public channels and long-inactive
                # DMs)
                known = {c.get("id") for c in chans}
                if scope != "channels":
                    for c in _fetch_all_dms_via_page(page, base, session,
                                                     "im,mpim", "私信/群组"):
                        if c["id"] not in known:
                            known.add(c["id"])
                            chans.append({
                                "id": c["id"], "user": c.get("user"),
                                "name": c.get("name"), "created": c.get("created"),
                                "is_archived": bool(c.get("is_archived")),
                                "is_private": True,
                                "type": "mpim" if c.get("is_mpim") else "im",
                            })
                if scope != "dm":
                    for c in _fetch_all_dms_via_page(
                            page, base, session,
                            "public_channel,private_channel", "频道"):
                        if c["id"] not in known:
                            known.add(c["id"])
                            chans.append({
                                "id": c["id"], "name": c.get("name"),
                                "created": c.get("created"),
                                "is_archived": bool(c.get("is_archived")),
                                "is_private": bool(c.get("is_private")),
                                "topic": c.get("topic"), "purpose": c.get("purpose"),
                                "num_members": c.get("num_members"),
                                "type": ("mpim" if c.get("is_mpim")
                                         else "private_channel" if c.get("is_private")
                                         else "public_channel"),
                            })
                kw = (cfg.get("channel_filter") or "").strip().lower()
                if kw:
                    chans = [c for c in chans
                             if kw in _channel_title(c, users).lower()
                             or kw in (c.get("name") or "").lower()]
                # Scope filter: dm=DMs/group DMs only; channels=channels
                # only (userBoot mixes in joined channels, filter here)
                if scope == "dm":
                    chans = [c for c in chans
                             if c.get("type") in ("im", "mpim")]
                elif scope == "channels":
                    chans = [c for c in chans
                             if c.get("type") not in ("im", "mpim")]
            # DMs/group DMs first, then private channels, public last
            chans.sort(key=lambda c: TYPE_ORDER.get(c.get("type"), 1))
            n_new = sum(1 for c in chans
                        if force or not (raw / f"{c['id']}.json").exists())
            print(f"共 {len(chans)} 个频道（私信优先）：全量导出 {n_new} 个，"
                  f"增量更新 {len(chans) - n_new} 个", flush=True)
            if not chans:
                write_index(raw)
                return raw

            up_to_date = 0
            done_incr = 0
            for i, ch in enumerate(chans, 1):
                cid = ch["id"]
                title = _channel_title(ch, users)
                f_out = raw / f"{cid}.json"
                if f_out.exists() and not force:
                    # Already exported: incremental fetch of new messages,
                    # no re-scrolling
                    try:
                        d = json.loads(f_out.read_text(encoding="utf-8"))
                        stored = {m["ts"]: m for m in d.get("messages") or []}
                        # Channels whose last run failed (network blip etc.)
                        # need a retry pass this time
                        retry_mode = ((d.get("channel") or {})
                                      .get("needs_retry") or "")
                        since = max((float(ts) for ts in stored), default=0)
                        r = _page_eval(page, INCREMENTAL_JS,
                                       {"token": session["token"],
                                        "channel": cid, "since": str(since)})
                        new = [m for m in (r.get("messages") or [])
                               if isinstance(m, dict) and m.get("ts")]
                        if not new and not (r or {}).get("error") \
                                and not retry_mode:
                            up_to_date += 1
                            continue
                        if (r or {}).get("error") and not new:
                            print(f"[{i}/{len(chans)}] 「{title}」增量失败："
                                  f"{r.get('error')}", flush=True)
                            continue
                        by_ts = dict(stored)
                        for m in new:
                            by_ts[m["ts"]] = m
                        if retry_mode == "threads":
                            print("    补拉上次失败的线程回复 …", flush=True)
                            thread_src = dict(by_ts)
                        else:
                            thread_src = {m["ts"]: m for m in new
                                          if m.get("reply_count")
                                          and m.get("thread_ts") == m.get("ts")}
                        added = 0
                        thread_failed = False
                        if any(m.get("reply_count") and
                               m.get("thread_ts") == m.get("ts")
                               for m in thread_src.values()):
                            _a, _c, thread_failed = _fetch_threads_via_page(
                                page, session, thread_src, cid, cfg)
                            added = _a
                        by_ts.update(thread_src)
                        merged = sorted(by_ts.values(),
                                        key=lambda m: float(m["ts"]))
                        verify_failed = False
                        if retry_mode == "verify":
                            print("    补做上次失败的完整性校验 …", flush=True)
                            try:
                                merged, _vn = _verify_and_backfill(
                                    page, session, cid, merged)
                            except Exception as e:
                                verify_failed = True
                                print(f"    校验异常（跳过）：{e}", flush=True)
                        meta = d.get("channel") or {"id": cid}
                        meta["id"] = cid
                        meta.pop("needs_retry", None)
                        meta["total_count"] = len(merged)
                        meta["msg_count"] = sum(
                            1 for m in merged
                            if not m.get("thread_ts")
                            or m.get("thread_ts") == m.get("ts"))
                        tss = [float(m["ts"]) for m in merged]
                        meta["first_ts"], meta["last_ts"] = min(tss), max(tss)
                        if thread_failed:
                            meta["needs_retry"] = "threads"
                        elif verify_failed:
                            meta["needs_retry"] = "verify"
                        save_channel(raw, files_dir, meta, merged, api, cfg)
                        done += 1
                        done_incr += 1
                        extra = f"，补线程 {added} 条" if added else ""
                        print(f"[{i}/{len(chans)}] 「{title}」增量 +{len(new)} 条"
                              f"（共 {len(merged)}{extra}）", flush=True)
                    except Exception as e:
                        print(f"[{i}/{len(chans)}] 「{title}」增量异常：{e}",
                              flush=True)
                    page.wait_for_timeout(300 + random.randint(0, 400))
                    continue
                if cid not in uboot_ids:
                    # Un-joined public channel: the browser stays on a
                    # preview page where scrolling is useless -- fetch the
                    # full history in-page instead (same-origin requests)
                    print(f"[{i}/{len(chans)}] 「{title}」（{cid}）未加入，"
                          f"页面内全量拉取 …", flush=True)
                    try:
                        r = _page_eval(
                            page, BACKFILL_JS,
                            {"token": session["token"], "channel": cid,
                             "earliest": str(time.time())})
                        msgs = [m for m in ((r or {}).get("messages") or [])
                                if isinstance(m, dict) and m.get("ts")]
                        note = "（页面内拉取）"
                        if not msgs:
                            print("    无聊天内容，忽略", flush=True)
                        by_ts = {m["ts"]: m for m in msgs}
                        _a, capped, thread_failed = _fetch_threads_via_page(
                            page, session, by_ts, cid, cfg)
                        added = _a
                        merged = sorted(by_ts.values(),
                                        key=lambda m: float(m["ts"]))
                        verify_failed = False
                        try:
                            merged, vnote = _verify_and_backfill(
                                page, session, cid, merged)
                        except Exception as e:
                            vnote = ""
                            verify_failed = True
                            print(f"    校验异常（跳过）：{e}", flush=True)
                        meta = make_meta(ch, merged, users)
                        if capped:
                            meta["threads_capped"] = True
                        if thread_failed:
                            meta["needs_retry"] = "threads"
                        elif verify_failed:
                            meta["needs_retry"] = "verify"
                        save_channel(raw, files_dir, meta, merged, api, cfg)
                        done += 1
                        extra = f"（补线程回复 {added} 条）" if added else ""
                        print(f"    {len(merged)} 条消息{note}{extra}{vnote}",
                              flush=True)
                    except Exception as e:
                        print(f"    跳过：{e}", flush=True)
                    page.wait_for_timeout(400 + random.randint(0, 500))
                    continue
                print(f"[{i}/{len(chans)}] 滚动加载「{title}」（{cid}）…", flush=True)
                cap.current = cid
                try:
                    page.goto(f"{base}/{cid}", wait_until="domcontentloaded",
                              timeout=60000)
                    if not _wait_view(page, cap, cid):
                        # Direct link did not land in the channel (client
                        # stuck on home); use Ctrl+K jump search
                        print("    直链未进入频道，用跳转搜索 …", flush=True)
                        _jump_via_search(page, title)
                        _wait_view(page, cap, cid, timeout_s=20)
                    _scroll_history(page, cap, cid, cfg)
                    msgs = cap.take(cid)
                except SystemExit:
                    raise
                except Exception as e:
                    print(f"    跳过：{e}", flush=True)
                    cap.take(cid)
                    continue

                note = ""
                if not msgs and cid not in cap.viewed:
                    # No view response (usually an un-joined public channel
                    # where the client stays on a preview): fetch the full
                    # history in-page (same-origin, like the client, no rate
                    # limits)
                    print("    未等到频道数据，页面内全量拉取 …", flush=True)
                    r = _page_eval(page, BACKFILL_JS,
                                   {"token": session["token"],
                                    "channel": cid, "earliest": str(time.time())})
                    msgs = [m for m in ((r or {}).get("messages") or [])
                            if isinstance(m, dict) and m.get("ts")]
                    note = "（页面内拉取）"
                elif not msgs:
                    print("    无聊天内容，忽略", flush=True)

                by_ts = {m["ts"]: m for m in msgs}
                added, capped, thread_failed = _fetch_threads_via_page(
                    page, session, by_ts, cid, cfg)
                merged = sorted(by_ts.values(), key=lambda m: float(m["ts"]))
                # Completeness check: confirm no earlier history exists
                # (scrolling may stop early on sticky virtual lists); page-
                # fetch and backfill if missing
                verify_failed = False
                try:
                    merged, vnote = _verify_and_backfill(page, session, cid, merged)
                except Exception as e:
                    vnote = ""
                    verify_failed = True
                    print(f"    校验异常（跳过）：{e}", flush=True)
                # Fill metadata from the view response channel object (bare
                # conversation ids collected on /dm lack name/user, which
                # the view response has)
                info = cap.channel_info.get(cid) or {}
                if info:
                    ch = dict(ch)
                    for k in ("name", "user", "created", "members", "topic",
                              "purpose", "num_members"):
                        if ch.get(k) in (None, "", []) and info.get(k):
                            ch[k] = info[k]
                    if not ch.get("is_archived") and info.get("is_archived"):
                        ch["is_archived"] = True
                meta = make_meta(ch, merged, users)
                if capped:
                    meta["threads_capped"] = True
                if thread_failed:
                    meta["needs_retry"] = "threads"
                elif verify_failed:
                    meta["needs_retry"] = "verify"
                save_channel(raw, files_dir, meta, merged, api, cfg)
                done += 1
                extra = f"（补线程回复 {added} 条）" if added else ""
                print(f"    {len(merged)} 条消息{note}{extra}{vnote}", flush=True)
                # Pace control: random pause between channels
                time.sleep(delay + random.random())
        finally:
            context.close()

    metas = write_index(raw)
    write_meta_json(raw, session)
    total = sum(c.get("total_count") or 0 for c in metas)
    print(f"本次处理：全量导出 {done - done_incr} 个、增量更新 {done_incr} 个、"
          f"已最新 {up_to_date} 个；"
          f"累计 {len(metas)} 个频道 / {total} 条消息 → {raw}", flush=True)
    return raw


if __name__ == "__main__":
    from login import load_session
    s = load_session()
    cfg = json.loads((Path(__file__).parent / "config.json")
                     .read_text(encoding="utf-8"))
    run_browser_export(s, cfg, Path(__file__).parent / "output")
