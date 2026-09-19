# -*- coding: utf-8 -*-
"""Slack export common module: API client (rate-limit/network retry),
caches, and channel persistence.

Two export modes (see main.py):
  api     -- call the Web API directly (fast, but rate-limit prone)
  browser -- simulate user scrolling in the client; only thread replies and
             attachments go through the API (default, see browser_export.py)
"""
import hashlib
import html as html_mod
import json
import posixpath
import re
import time
from pathlib import Path

import requests

API = "https://slack.com/api"

# Export order: DMs/group DMs first, channels last
TYPE_ORDER = {"im": 0, "mpim": 0, "private_channel": 1, "public_channel": 2}

RAW_SKIP_FILES = {"users.json", "meta.json", "channels.json", "_channels_cache.json"}


class SlackError(RuntimeError):
    pass


class SlackAPI:
    """Web API client with automatic 429 rate-limit and network retries."""

    def __init__(self, token, cookies=None, d_s=""):
        self.sess = requests.Session()
        self.sess.headers.update({
            "Authorization": f"Bearer {token}",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        })
        # xoxc tokens only work with browser cookies (d / d-s etc.)
        parts = []
        if cookies:
            parts = [f"{c['name']}={c['value']}" for c in cookies]
        elif d_s:
            parts = [f"d-s={d_s}"]
        if parts:
            self.sess.headers["Cookie"] = "; ".join(parts)

    def call(self, method, **params):
        url = f"{API}/{method}"
        last_err = None
        for attempt in range(12):
            try:
                r = self.sess.post(url, data=params, timeout=60)
            except requests.RequestException as e:
                last_err = e
                wait = min(60, 5 * (attempt + 1))
                print(f"    网络错误（{type(e).__name__}），{wait}s 后重试 …", flush=True)
                time.sleep(wait)
                continue
            if r.status_code == 429:
                wait = int(r.headers.get("Retry-After", "5")) + 1
                print(f"    触发限流，等待 {wait}s …", flush=True)
                time.sleep(wait)
                continue
            try:
                data = r.json()
            except ValueError:
                last_err = RuntimeError(f"HTTP {r.status_code} 非 JSON 响应")
                time.sleep(5)
                continue
            if not data.get("ok"):
                err = data.get("error")
                if err == "ratelimited":
                    time.sleep(5)
                    continue
                if err in ("invalid_auth", "not_authed", "account_inactive"):
                    raise SlackError(
                        f"{method}: {err} —— 会话已过期，请运行 `python main.py login --relogin` 重新登录")
                raise SlackError(f"{method}: {err}")
            return data
        raise SlackError(f"{method} 多次重试后仍失败：{last_err}")

    def paginate(self, method, key, **params):
        """Paginate automatically and return all items."""
        params.setdefault("limit", 200)
        cursor = None
        items = []
        while True:
            if cursor:
                params["cursor"] = cursor
            data = self.call(method, **params)
            items.extend(data.get(key, []))
            cursor = (data.get("response_metadata") or {}).get("next_cursor")
            if not cursor:
                return items

    def download(self, url, dest: Path):
        fails = 0
        for attempt in range(6):
            try:
                r = self.sess.get(url, timeout=45, stream=True)
                if r.status_code == 429:
                    time.sleep(int(r.headers.get("Retry-After", "5")) + 1)
                    continue
                if r.status_code != 200:
                    return False
                dest.parent.mkdir(parents=True, exist_ok=True)
                with open(dest, "wb") as f:
                    for chunk in r.iter_content(65536):
                        f.write(chunk)
                return True
            except requests.RequestException as e:
                fails += 1
                print(f"    附件下载错误（{type(e).__name__}），重试 …", flush=True)
                if fails >= 3:
                    # Persistent network failure: skip and let the next run
                    # re-download missing files
                    return False
                time.sleep(min(10 * fails, 30))
        return False


# ---------- helpers ----------

def _safe_name(name):
    # Also strip "&": external page titles often contain HTML entities
    # (&amp; &#x27; ...) which break offline links if left in file names
    return re.sub(r'[\\/:*?"<>|\r\n&]+', "_", name)[:80] or "file"


# Slack Docs (canvas/quip documents) downloaded via url_private_download
# are HTML fragments (<div class="quip-canvas-content">...); wrap them in a
# full page so they open standalone offline
_DOC_TMPL = """<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
{canvas_css}
</head>
<body>
{content}
<div class="foot">导出自 Slack 画板文档 · {updated}</div>
<script>
// Offline interaction: channel/message anchor links (viewer/index.html#c=..)
// are forwarded to the parent viewer; direct double-click navigates here
document.addEventListener('click', function(e){{
  var a = e.target && e.target.closest ? e.target.closest('a') : null;
  if (!a) return;
  var h = a.getAttribute('href') || '';
  if (h.indexOf('viewer/index.html#') === -1) return;
  e.preventDefault();
  var tgt = (window.parent && window.parent !== window) ? window.parent : window;
  tgt.location.href = h;
}});
</script>
</body>
</html>
"""

# Canvas page styles matching Slack's rendering (fonts/sizes/columns/quotes/
# link colors). Shared by template and link localization; "canvas-v2" is the
# idempotency marker for re-injection
_CANVAS_CSS = """<style>
/* slack-canvas-style canvas-v2 */
body { font: 15px/1.466 Lato, -apple-system, "Segoe UI", "Microsoft YaHei",
       "PingFang SC", sans-serif; color: #1d1c1d; max-width: 860px;
       margin: 24px auto; padding: 0 20px 60px; }
.quip-canvas-content h1:first-child { margin-top: 0; }
h1 { font-size: 26px; font-weight: 700; line-height: 1.25; margin: 28px 0 8px; }
h2 { font-size: 21px; font-weight: 700; line-height: 1.3; margin: 24px 0 6px; }
h3 { font-size: 17px; font-weight: 700; line-height: 1.3; margin: 18px 0 4px; }
h4, h5, h6 { font-size: 15px; font-weight: 700; margin: 14px 0 4px; }
p { margin: 0 0 2px; }
ul, ol { margin: 6px 0; padding-left: 26px; }
li { margin: 3px 0; }
blockquote { border-left: 3px solid rgba(29,28,29,.35); color: #1d1c1d;
             margin: 8px 0; padding: 2px 0 2px 14px; }
blockquote p:last-child { margin-bottom: 0; }
img { max-width: 100%; }
code { background: #f4f4f4; padding: 1px 4px; border-radius: 4px;
       font-size: .9em; }
pre { background: #f8f8f8; border: 1px solid #ebebeb; padding: 10px 14px;
      border-radius: 6px; overflow: auto; line-height: 1.45; }
table { border-collapse: collapse; margin: 8px 0; }
td, th { border: 1px solid #ddd; padding: 5px 9px; text-align: left;
         vertical-align: top; }
th { background: #f8f8f8; font-weight: 700; }
a, lnk[href] { color: #1264a3; text-decoration: none; cursor: pointer; }
a:hover, lnk[href]:hover { text-decoration: underline; }
/* Slack canvas multi-column layout: .flexbox children .flexbox-column
   render side by side with equal width */
.flexbox { display: flex; flex-wrap: wrap; align-items: flex-start;
           column-gap: 20px; margin: 8px 0; }
.flexbox-column { flex: 1 1 0; min-width: 0; }
.flexbox-column img { height: auto; }
/* Canvas inline images: responsive (drop the native 64px placeholder) */
img.doc-img { max-width: 100%; height: auto; display: block; margin: 2px 0;
              border-radius: 4px; }
/* Canvas embedded files (after embedded-file placeholder replacement) */
.embed-file { margin: 12px 0; max-width: 100%; }
.embed-file img { max-width: 100%; border-radius: 8px;
                  border: 1px solid #e8e8e8; display: block; }
.ef-bar { background: #f8f8f8; border: 1px solid #e4e4e4;
          border-bottom: 0; border-radius: 8px 8px 0 0;
          padding: 8px 12px; font-size: 13.5px; }
.ef-bar a { color: #1264a3; text-decoration: none; display: flex;
            align-items: center; gap: 8px; flex-wrap: wrap; }
.ef-bar a:hover { text-decoration: underline; }
.ef-bar b { font-weight: 600; }
.ef-ico { font-size: 16px; }
.ef-meta { color: #888; font-size: 12px; font-weight: 400; }
.ef-pdf { width: 100%; height: 680px; border: 1px solid #e4e4e4;
          border-radius: 0 0 8px 8px; background: #52565a; }
.ef-card { display: flex; align-items: center; gap: 10px;
           background: #f8f8f8; border: 1px solid #e4e4e4;
           border-radius: 8px; padding: 10px 14px;
           text-decoration: none; color: #1d1c1d; }
.ef-card:hover { border-color: #1264a3; }
.ef-card .ef-ico { font-size: 22px; }
.ef-body { display: flex; flex-direction: column; min-width: 0; }
.ef-body b { font-weight: 600; overflow-wrap: anywhere; }
/* User mentions (@Uxxxx -> readable name) */
.mention { background: rgba(18,100,163,.12); color: #1264a3;
           border-radius: 4px; padding: 1px 5px; font-weight: 500; }
.mention:hover { background: rgba(18,100,163,.22); }
.foot { color: #999; font-size: 12px; margin-top: 48px;
        border-top: 1px solid #eee; padding-top: 10px; }
</style>
"""

_LOCALIZE_STYLE_CSS = _CANVAS_CSS


# Slack custom elements in canvas fragments -> standard HTML (renders
# correctly even without JS):
#   <lnk href>   links (email/member names; unconverted = not clickable)
#   <control>    inline wrapper for mentions etc.
_LNK_OPEN_RE = re.compile(r"<lnk\b([^>]*)>")
_CTRL_OPEN_RE = re.compile(r"<control\b([^>]*)>")


def _normalize_canvas_tags(html: str):
    """Convert quip-canvas custom tags to standard tags.

    Returns (new html, replacement count).
    """
    n = len(_LNK_OPEN_RE.findall(html)) + html.count("</lnk>")
    n += len(_CTRL_OPEN_RE.findall(html)) + html.count("</control>")
    if not n:
        return html, 0
    html = _LNK_OPEN_RE.sub(r"<a\1>", html).replace("</lnk>", "</a>")
    html = _CTRL_OPEN_RE.sub(r"<span\1>", html).replace(
        "</control>", "</span>")
    return html, n


def wrap_slack_doc(frag_path: Path, dest: Path, meta: dict):
    """Wrap a downloaded quip-canvas HTML fragment into a standalone page."""
    b = frag_path.read_bytes()
    # A few canvases are UTF-16 (with BOM); decode others as tolerant UTF-8
    if b[:2] in (b"\xff\xfe", b"\xfe\xff"):
        html = b.decode("utf-16", errors="replace")
    else:
        html = b.decode("utf-8", errors="replace")
    # Slack custom tags (lnk/control) -> standard a/span
    html, _ = _normalize_canvas_tags(html)
    # Drop stray </img> closings and zero-width chars after images
    # (they add an extra line)
    html = re.sub(r"\u200b?</img>", "", html)
    title = meta.get("title") or meta.get("name") or "Slack 文档"
    updated = ""
    if meta.get("updated"):
        updated = time.strftime("%Y-%m-%d %H:%M",
                                time.localtime(float(meta["updated"])))
    dest.write_text(_DOC_TMPL.format(
        title=title.replace("<", "&lt;"), content=html, updated=updated,
        canvas_css=_CANVAS_CSS), encoding="utf-8")


def _channel_title(ch, users):
    t = ch.get("type")
    if t == "im":
        uid = ch.get("user")
        u = users.get(uid) or {}
        return u.get("real_name") or u.get("name") or uid or "私信"
    if t == "mpim":
        ids = ch.get("members") or []
        names = [(users.get(i) or {}).get("name") or i for i in ids[:6]]
        title = "、".join(names)
        return title + ("…" if len(ids) > 6 else "")
    return ch.get("name") or ch.get("id")


def _slim_user(u):
    p = u.get("profile") or {}
    return {
        "id": u.get("id"),
        "name": u.get("name"),
        "real_name": u.get("real_name"),
        "display_name": p.get("display_name"),
        "avatar": p.get("image_72"),
        "is_bot": bool(u.get("is_bot")),
        "deleted": bool(u.get("deleted")),
    }


def load_users(api, raw: Path):
    """User list with cache (avoid re-fetching on every export)."""
    f = raw / "users.json"
    if f.exists():
        print("使用用户列表缓存（users.json）", flush=True)
        return json.loads(f.read_text(encoding="utf-8"))
    print("获取用户列表 …", flush=True)
    users = {u["id"]: _slim_user(u)
             for u in api.paginate("users.list", "members", limit=200)}
    raw.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(users, ensure_ascii=False), encoding="utf-8")
    print(f"  共 {len(users)} 个用户", flush=True)
    return users


def list_channels(api, raw: Path, cfg, refresh=False):
    """Channel list with cache (a full fetch can take tens of minutes)."""
    cache = raw / "_channels_cache.json"
    if cache.exists() and not refresh:
        chans = json.loads(cache.read_text(encoding="utf-8"))
        print(f"使用频道列表缓存（{len(chans)} 个，--refresh-list 可刷新）", flush=True)
        return chans
    types = ",".join(cfg.get("types") or
                     ["public_channel", "private_channel", "mpim", "im"])
    print(f"获取频道列表（{types}）…", flush=True)
    chans = api.paginate("conversations.list", "channels",
                         types=types, exclude_archived="false", limit=200)
    raw.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(chans, ensure_ascii=False), encoding="utf-8")
    print(f"  共 {len(chans)} 个频道", flush=True)
    return chans


def make_meta(ch, messages, users):
    tss = [float(m["ts"]) for m in messages] or [0, 0]
    top = sum(1 for m in messages
              if not m.get("thread_ts") or m.get("thread_ts") == m.get("ts"))
    return {
        "id": ch.get("id"),
        "name": ch.get("name"),
        "title": _channel_title(ch, users),
        "type": ch.get("type"),
        "is_private": bool(ch.get("is_private")),
        "is_archived": bool(ch.get("is_archived")),
        "topic": (ch.get("topic") or {}).get("value") or "",
        "purpose": (ch.get("purpose") or {}).get("value") or "",
        "num_members": ch.get("num_members"),
        "created": ch.get("created"),
        "msg_count": top,
        "total_count": len(messages),
        "first_ts": min(tss),
        "last_ts": max(tss),
    }


def save_channel(raw: Path, files_dir: Path, meta, messages, api, cfg):
    """Download attachments and write raw/<cid>.json (its existence marks the
    channel as done, enabling resume)."""
    cid = meta["id"]
    if cfg.get("download_files", True):
        max_bytes = cfg.get("max_file_mb", 200) * 1024 * 1024
        n_ok = 0
        consec_fail = 0   # consecutive failed attachments (reset on success)
        skip_rest = False
        for m in messages:
            if skip_rest:
                break
            for f in m.get("files") or []:
                url = f.get("url_private_download") or f.get("url_private")
                if not url:
                    continue
                if (f.get("size") or 0) > max_bytes:
                    f["local_path"] = None
                    continue
                name = _safe_name(f.get("name") or f.get("title")
                                  or f.get("filetype") or "file")
                # Slack Docs (canvas): download as HTML fragment, wrap into
                # a standalone page
                is_doc = f.get("mimetype") == "application/vnd.slack-docs"
                if is_doc:
                    dest = files_dir / cid / f"{f.get('id', '')}__{name}.html"
                else:
                    dest = files_dir / cid / f"{f.get('id', '')}__{name}"
                if not dest.exists():
                    frag = dest.with_suffix("") if is_doc else dest
                    if not api.download(url, frag):
                        consec_fail += 1
                        if consec_fail >= 3:
                            # Persistent network failure: skip remaining
                            # attachments of this channel (json still saved;
                            # next run re-downloads missing files)
                            skip_rest = True
                            print("    连续 3 个附件下载失败，跳过本频道剩余"
                                  "附件（下次运行自动补齐）", flush=True)
                            break
                        continue
                    if is_doc:
                        wrap_slack_doc(frag, dest, f)
                        frag.unlink()
                consec_fail = 0
                f["local_path"] = f"files/{cid}/{dest.name}".replace("\\", "/")
                n_ok += 1
        if n_ok:
            print(f"    下载附件 {n_ok} 个", flush=True)
    (raw / f"{cid}.json").write_text(
        json.dumps({"channel": meta, "messages": messages}, ensure_ascii=False),
        encoding="utf-8")


# ---------- canvas document link localization ----------
# Four kinds of links inside canvas HTML, all rewritten for offline jumps
# matching Slack behavior:
#   /docs/<T>/<F>      -> another canvas: downloaded and rewritten to a local
#                         relative path (recursively)
#   /files/.../<F>/    -> Slack file: local attachment if exported, otherwise
#                         fetched on demand
#   /archives/<C>/p..  -> channel/message: rewritten to viewer anchor
#                         (viewer#c=..&t=..)
#   other external     -> single-page snapshot into files/_web/, keep online
#                         on failure

_SLACK_URL_RE = re.compile(r"^https?://(?:[\w-]+\.)*slack\.com(/.*)$",
                           re.I)
_FID_RE = re.compile(r"\b(F[A-Z0-9]{8,})\b")
_CID_RE = re.compile(r"\b([CGD][A-Z0-9]{8,})\b")
_IMG_PROXY_RE = re.compile(r"^https?://slack-imgs\.com/", re.I)


class _WebSess:
    """Unauthenticated session: never send Slack tokens/cookies when
    snapshotting external sites or images."""

    _sess = None

    @classmethod
    def get(cls, url, timeout=20, **kw):
        if cls._sess is None:
            cls._sess = requests.Session()
            cls._sess.headers.update({
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                              "AppleWebKit/537.36 (KHTML, like Gecko) "
                              "Chrome/126.0 Safari/537.36",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            })
        return cls._sess.get(url, timeout=timeout, **kw)


def _rel_to(base_doc_rel: str, target_rel: str) -> str:
    """Relative path from one exported file to another (both relative to
    out_dir)."""
    return posixpath.relpath(
        target_rel, posixpath.dirname(base_doc_rel)).replace("\\", "/")


def _fetch_slack_file(fid, api, out_dir, index, pending_docs):
    """Fetch a Slack file by ID on demand (canvas -> offline page, other
    files saved as-is).

    Returns the path relative to out_dir, or None on failure. Canvases are
    appended to pending_docs so their own links get localized too. File meta
    (mime/name/size) is cached in index["finfo"] for embed rendering.
    """
    dlink = out_dir / "files" / "_doclinks"
    finfo = index.setdefault("finfo", {})
    if fid in index["docs"]:
        rel = index["docs"][fid]
        finfo.setdefault(fid, {"path": rel, "mime":
                               "application/vnd.slack-docs"})
        return rel
    if fid in index["files"]:
        rel = index["files"][fid]
        if rel:
            finfo.setdefault(fid, {"path": rel})
        return rel
    if fid in finfo:   # failed before, skip repeated files.info calls
        return finfo[fid].get("path")
    try:
        fi = api.call("files.info", file=fid).get("file") or {}
    except SlackError as e:
        print(f"  文件 {fid} 信息获取失败：{e}", flush=True)
        index["files"][fid] = None
        finfo[fid] = {"path": None}
        return None
    url = fi.get("url_private_download") or fi.get("url_private")
    if not url:
        index["files"][fid] = None
        finfo[fid] = {"path": None}
        return None
    name = _safe_name(fi.get("name") or fi.get("title")
                      or fi.get("filetype") or "file")
    is_doc = fi.get("mimetype") == "application/vnd.slack-docs"
    dest = dlink / (f"{fid}__{name}" + (".html" if is_doc else ""))
    dest.parent.mkdir(parents=True, exist_ok=True)
    if not dest.exists():
        if is_doc:
            frag = dest.with_suffix("")
            if not api.download(url, frag):
                index["files"][fid] = None
                finfo[fid] = {"path": None}
                return None
            wrap_slack_doc(frag, dest, fi)
            frag.unlink()
        else:
            if not api.download(url, dest):
                index["files"][fid] = None
                finfo[fid] = {"path": None}
                return None
    rel = f"files/_doclinks/{dest.name}"
    print(f"  链接目标已补导：{fi.get('title') or name} → {rel}", flush=True)
    finfo[fid] = {"path": rel, "mime": fi.get("mimetype"),
                  "name": fi.get("name") or fi.get("title") or name,
                  "size": fi.get("size") or 0}
    if is_doc:
        index["docs"][fid] = rel
        pending_docs.append((fid, rel))
    else:
        index["files"][fid] = rel
    return rel


def _web_snapshot(url, out_dir, failed):
    """Snapshot an external page/image into files/_web/.

    Returns relative path or None.
    """
    if url in failed:
        return None
    h = hashlib.sha1(url.encode()).hexdigest()[:16]
    web = out_dir / "files" / "_web"
    hits = list(web.glob(f"{h}__*"))
    if hits:
        return f"files/_web/{hits[0].name}"
    try:
        r = _WebSess.get(url)
        ct = (r.headers.get("content-type") or "").lower()
        if r.status_code != 200 or len(r.content) > 3 * 1024 * 1024:
            raise RuntimeError(f"HTTP {r.status_code}")
        web.mkdir(parents=True, exist_ok=True)
        if "html" in ct:
            tm = re.search(r"<title[^>]*>(.*?)</title>",
                           r.content[:65536].decode("utf-8", errors="replace"),
                           re.I | re.S)
            fname = _safe_name(re.sub(
                r"\s+", " ", html_mod.unescape(
                    tm.group(1) if tm else url)).strip()[:60]) or h
            dest = web / f"{h}__{fname}.html"
            # Relative resources inside snapshots cannot be resolved; keep
            # text content only
            dest.write_bytes(r.content)
        elif (ct.startswith("image/") or ct.startswith("audio/")
                or ct.startswith("video/") or "pdf" in ct):
            ext = {"image/jpeg": ".jpg", "image/png": ".png",
                   "image/gif": ".gif", "image/webp": ".webp",
                   "application/pdf": ".pdf"}.get(ct.split(";")[0].strip(),
                                                  Path(url).suffix or ".bin")
            dest = web / f"{h}{ext}"
            dest.write_bytes(r.content)
        else:
            raise RuntimeError(f"不支持的类型 {ct}")
        return f"files/_web/{dest.name}"
    except Exception:
        failed.add(url)
        return None


# Canvas embedded-file placeholder: Slack export fragments downgrade
# embedded attachments to plain text
# <p class='embedded-file'>File ID: sf:Fxxx, File URL: ...</p>
_EMBED_RE = re.compile(
    r"<p[^>]*class=['\"]embedded-file['\"][^>]*>\s*"
    r"File ID:\s*sf:(F[A-Z0-9]+)[^<]*</p>", re.I)


def _fmt_size(n):
    n = n or 0
    if n > 1024 * 1024:
        return f"{n / 1024 / 1024:.1f} MB"
    if n > 1024:
        return f"{n // 1024} KB"
    return f"{n} B"


def _embed_html(info, path):
    """Render an embedded file as a Slack-like offline element.

    PDF -> file bar + inline preview; image -> shown directly; others ->
    file card.
    """
    mime = info.get("mime") or ""
    name = html_mod.escape(info.get("name") or "文件")
    size = _fmt_size(info.get("size"))
    if mime.startswith("image/"):
        return (f'<div class="embed-file"><a href="{path}" target="_blank">'
                f'<img loading="lazy" src="{path}" alt="{name}"></a></div>')
    if mime == "application/pdf":
        return (f'<div class="embed-file">'
                f'<div class="ef-bar"><a href="{path}" target="_blank">'
                f'<span class="ef-ico">📄</span><b>{name}</b>'
                f'<span class="ef-meta">{size} · PDF · 点击新窗口打开</span>'
                f'</a></div>'
                f'<iframe class="ef-pdf" src="{path}" '
                f'title="{name}"></iframe></div>')
    # Embedded canvases / tables / others: card style, click to open
    ico = "📝" if mime == "application/vnd.slack-docs" else "📎"
    kind = ("画板文档" if mime == "application/vnd.slack-docs"
            else (info.get("name") or "").rsplit(".", 1)[-1].upper() or "文件")
    return (f'<div class="embed-file"><a class="ef-card" href="{path}" '
            f'target="_blank"><span class="ef-ico">{ico}</span>'
            f'<span class="ef-body"><b>{name}</b>'
            f'<span class="ef-meta">{kind} · {size} · 点击打开</span>'
            f'</span></a></div>')


def _replace_embedded_files(html, api, out_dir, doc_rel, index, pending_docs):
    """Replace embedded-file text placeholders with rendered local elements."""
    stats = {"n": 0}

    def sub(m):
        fid = m.group(1)
        rel = _fetch_slack_file(fid, api, out_dir, index, pending_docs)
        if not rel:
            # Some types (e.g. Slackbot huddle transcripts) cannot be
            # downloaded (403): render a notice card keeping the File ID
            # for traceability
            stats["n"] += 1
            return (f'<div class="embed-file"><div class="ef-bar" '
                    f'style="opacity:.7"><span class="ef-ico">⚠️</span>'
                    f'<b>嵌入文件无法离线导出</b>'
                    f'<span class="ef-meta">Slack 限制该文件下载'
                    f'（sf:{fid}）</span></div></div>')
        stats["n"] += 1
        path = _rel_to(doc_rel, rel)
        return _embed_html(index["finfo"].get(fid) or {}, path)

    out = _EMBED_RE.sub(sub, html)
    return out, stats["n"]


# User mentions in canvases: Slack export fragments keep only the raw ID
# <a>@Uxxxx</a>
_MENTION_RE = re.compile(r"<a>@([UW][A-Z0-9]{8,})</a>")


def _user_label(u):
    """User display name: display_name -> real_name -> name."""
    return ((u or {}).get("display_name")
            or (u or {}).get("real_name")
            or (u or {}).get("name") or "")


def _replace_user_mentions(html, users):
    """Replace raw @Uxxxx IDs with readable names (Slack-style tags).

    users is the {uid: _slim_user} cache; unknown users stay unchanged.
    """
    stats = {"n": 0}

    def sub(m):
        uid = m.group(1)
        nm = _user_label((users or {}).get(uid))
        if not nm:
            return m.group(0)
        stats["n"] += 1
        return (f'<span class="mention" title="{uid}">'
                f'@{html_mod.escape(nm)}</span>')

    return _MENTION_RE.sub(sub, html), stats["n"]


# Canvas inline images: <img src='/collab-slack-blob/<token>/<FID>?size_name=..'
#                         width='64' height='64' ...> (single-quoted
# attributes, site-relative paths)
_IMG_BLOB_RE = re.compile(
    r"""<img\b[^>]*\ssrc=(['"])/collab-slack-blob/\w+/(F[A-Z0-9]+)[^'"]*\1"""
    r"""[^>]*>""", re.I)
_IMG_SIZE_RE = re.compile(r"""\s(width|height)=(['"])\d+\2""", re.I)


def _replace_blob_images(html, api, out_dir, doc_rel, index, pending_docs):
    """Download canvas inline images and rewrite <img> to local responsive
    versions."""
    stats = {"n": 0}

    def sub(m):
        tag, fid = m.group(0), m.group(2)
        rel = index["files"].get(fid)
        if rel is None:
            rel = _fetch_slack_file(fid, api, out_dir, index, pending_docs)
        if not rel:
            return tag   # download failed: keep as-is (online path is
                         # unreachable offline anyway)
        stats["n"] += 1
        src = _rel_to(doc_rel, rel).replace("&", "&amp;")
        tag = re.sub(r"""\ssrc=(['"])[^'"]*\1""", f' src="{src}"', tag,
                     count=1)
        # Native tags are 64px thumbnails; switch to CSS responsive sizing
        tag = _IMG_SIZE_RE.sub("", tag)
        if "doc-img" not in tag:
            tag = tag.replace("<img", '<img class="doc-img"', 1)
        return tag

    return _IMG_BLOB_RE.sub(sub, html), stats["n"]


# Image alt placeholders: alt="_SLACK_FILE_ALT_PLACEHOLDER_Fxxx"
_ALT_PH_RE = re.compile(
    r"""alt=(["'])_SLACK_FILE_ALT_PLACEHOLDER_(F[A-Z0-9]+)\1""")


def _replace_alt_placeholders(html, index):
    """Replace Slack alt placeholders with real file names (from finfo)."""
    stats = {"n": 0}

    def sub(m):
        fid = m.group(2)
        info = index.get("finfo", {}).get(fid) or {}
        nm = html_mod.escape(info.get("name") or "")
        stats["n"] += 1
        return f'alt="{nm}"'

    return _ALT_PH_RE.sub(sub, html), stats["n"]


def localize_doc_html(html, api, out_dir, doc_rel, index, pending_docs,
                      users=None):
    """Rewrite hyperlinks inside canvas HTML to local offline versions.

    Returns (new HTML, rewrite count).
    """
    stats = {"n": 0}
    # Slack custom tags (lnk/control) -> standard a/span; must run before
    # link rewriting so lnk hrefs go through localization
    html, n_tag = _normalize_canvas_tags(html)
    stats["n"] += n_tag
    # Drop stray </img> closings and zero-width chars after images
    n_img_close = len(re.findall(r"\u200b?</img>", html))
    if n_img_close:
        html = re.sub(r"\u200b?</img>", "", html)
        stats["n"] += n_img_close
    # Embedded-file placeholders first (plain text, attribute regexes miss
    # them)
    html, n_emb = _replace_embedded_files(html, api, out_dir, doc_rel,
                                          index, pending_docs)
    stats["n"] += n_emb
    # User mentions: raw @Uxxxx -> readable names
    html, n_men = _replace_user_mentions(html, users)
    stats["n"] += n_men
    # Canvas inline images: /collab-slack-blob relative paths -> download
    # and localize
    html, n_img = _replace_blob_images(html, api, out_dir, doc_rel,
                                       index, pending_docs)
    stats["n"] += n_img
    # Image alt placeholders -> real names (after image fetching so finfo
    # has names)
    html, n_alt = _replace_alt_placeholders(html, index)
    stats["n"] += n_alt
    failed = index.setdefault("_failed_ext", set())

    def sub_attr(m):
        attr, quote, raw = m.group(1), m.group(2), m.group(3)
        u = html_mod.unescape(raw)
        nu = map_url(u)
        if nu is None:
            return m.group(0)
        stats["n"] += 1
        # Escape & inside attribute values per spec (browsers unescape
        # before loading the file)
        v = nu.replace("&", "&amp;")
        # Preserve original quote style (canvas fragments often use single)
        return f"{attr}={quote}{v}{quote}" if quote == "'" else f'{attr}="{v}"'

    def map_url(u):
        # Canvas inline images: /collab-slack-blob/<token>/<FID>?size_name=...
        # Site-relative paths in export fragments; fetch by file ID
        bm = re.match(r"^/collab-slack-blob/\w+/(F[A-Z0-9]+)", u)
        if bm:
            fid = bm.group(1)
            rel = index["files"].get(fid)
            if rel is None:
                rel = _fetch_slack_file(fid, api, out_dir, index,
                                        pending_docs)
            return _rel_to(doc_rel, rel) if rel else None
        if not u.startswith(("http://", "https://")):
            return None
        sm = _SLACK_URL_RE.match(u)
        if sm:
            path = sm.group(1)
            # Canvas cross-links
            dm = re.match(r"^/docs/\w+/(F[A-Z0-9]+)", path)
            if dm:
                rel = _fetch_slack_file(dm.group(1), api, out_dir, index,
                                        pending_docs)
                return _rel_to(doc_rel, rel) if rel else None
            # Slack file links
            fm = _FID_RE.search(path)
            if path.startswith(("/files", "/files-pri", "/file/")) and fm:
                rel = index["files"].get(fm.group(1))
                if rel is None:
                    rel = _fetch_slack_file(fm.group(1), api, out_dir,
                                            index, pending_docs)
                return _rel_to(doc_rel, rel) if rel else None
            # Channel / message links -> viewer anchors
            cm = re.match(r"^/archives/([A-Z0-9]+)(?:/p(\d{10,16}))?", path)
            if cm:
                hash = f"c={cm.group(1)}"
                if cm.group(2):  # permalink "p" = seconds(10 digits) + decimals
                    t = cm.group(2)
                    hash += f"&t={t[:10]}.{t[10:]}" if len(t) > 10 else f"&t={t}.0"
                return _rel_to(doc_rel, f"viewer/index.html") + "#" + hash
            # User profiles etc.: keep online
            return None
        # slack-imgs proxy / other external sites -> snapshot
        rel = _web_snapshot(u, out_dir, failed)
        return _rel_to(doc_rel, rel) if rel else None

    # Canvas fragments mix single/double quoted attributes; cover both
    out = re.sub(r"""\b(href|src)=(["'])([^"'>]+)\2""", sub_attr, html)
    if "canvas-v2" not in out:
        # Pages wrapped by older templates lack canvas styles (columns/font
        # sizes/link colors); inject idempotently; count as one rewrite so
        # the file gets written
        out = out.replace("</head>", _LOCALIZE_STYLE_CSS + "</head>", 1)
        stats["n"] += 1
    return out, stats["n"]


def run_docs_export(session, out_dir: Path, force=False):
    """Export Slack canvas documents (Slack Docs) as offline HTML.

    Scans exported raw/*.json messages for canvas docs (mimetype
    application/vnd.slack-docs), downloads each and wraps it into a
    standalone page, updating local_path in the json. No browser needed.

    force=True re-downloads already exported docs.
    Returns (exported this run, total docs).
    """
    raw = out_dir / "raw"
    files_dir = out_dir / "files"
    api = SlackAPI(session["token"], session.get("cookies"),
                   session.get("d_s", ""))
    exported = total = 0
    for fp in sorted(raw.glob("*.json")):
        if fp.name in RAW_SKIP_FILES:
            continue
        try:
            d = json.loads(fp.read_text(encoding="utf-8"))
        except Exception:
            continue
        cid = (d.get("channel") or {}).get("id") or fp.stem
        changed = False
        for m in d.get("messages") or []:
            for f in m.get("files") or []:
                if f.get("mimetype") != "application/vnd.slack-docs":
                    continue
                total += 1
                lp = f.get("local_path") or ""
                if (not force and lp.endswith(".html")
                        and (out_dir / lp).exists()):
                    continue  # already an offline page, skip
                url = f.get("url_private_download") or f.get("url_private")
                if not url:
                    continue
                name = _safe_name(f.get("name") or f.get("title") or "doc")
                dest = files_dir / cid / f"{f.get('id', '')}__{name}.html"
                frag = dest.with_suffix("")
                dest.parent.mkdir(parents=True, exist_ok=True)
                if not api.download(url, frag):
                    print(f"  画板下载失败：{cid}/{f.get('id')} "
                          f"「{f.get('title') or f.get('name')}」", flush=True)
                    continue
                wrap_slack_doc(frag, dest, f)
                frag.unlink()
                f["local_path"] = f"files/{cid}/{dest.name}".replace("\\", "/")
                changed = True
                exported += 1
                print(f"  画板已导出：{f.get('title') or f.get('name')}"
                      f" → {f['local_path']}", flush=True)
        if changed:
            fp.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    print(f"画板文档：共 {total} 个，本次导出 {exported} 个"
          f"{'（全部已是离线版）' if exported == 0 else ''}", flush=True)

    # ---- phase 2: build file index (canvas / attachment -> local path) ----
    # doc_paths collects every canvas file path: the same canvas may appear
    # in multiple channels and each copy needs link localization
    # (index["docs"] is deduplicated by fid for lookup only)
    index = {"docs": {}, "files": {}}
    doc_paths = []
    for fp in sorted(raw.glob("*.json")):
        if fp.name in RAW_SKIP_FILES:
            continue
        try:
            d = json.loads(fp.read_text(encoding="utf-8"))
        except Exception:
            continue
        for m in d.get("messages") or []:
            for f in m.get("files") or []:
                lp = f.get("local_path")
                if not lp or not (out_dir / lp).exists():
                    continue
                if f.get("mimetype") == "application/vnd.slack-docs":
                    index["docs"].setdefault(f.get("id"), lp)
                    doc_paths.append((f.get("id"), lp))
                else:
                    index["files"][f.get("id")] = lp

    # ---- phase 3: localize links inside canvases (recursively process
    # canvases/files discovered via links) ----
    print("画板链接本地化（互链/嵌入文件/用户名/频道跳转/外站快照）…",
          flush=True)
    users = load_users(api, raw)   # {uid: _slim_user} for mention replacement
    pending = list(doc_paths)
    processed = set()
    n_links = 0
    while pending:
        fid, rel = pending.pop(0)
        key = (fid, rel)   # each copy of the same canvas is processed once
        if key in processed or len(processed) > 300:  # guard against
                                                      # cross-link explosion
            continue
        processed.add(key)
        p = out_dir / rel
        if not p.exists():
            continue
        try:
            html = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        # localize appends newly discovered canvases to pending
        new_html, n = localize_doc_html(html, api, out_dir, rel, index,
                                        pending, users=users)
        if n:
            p.write_text(new_html, encoding="utf-8")
            n_links += n
    print(f"链接本地化完成：处理 {len(processed)} 个画板，"
          f"重写 {n_links} 处链接", flush=True)
    return exported, total


def write_index(raw: Path):
    """Rebuild the channel index from raw/*.json (recoverable after
    interruption).

    Conversations without messages (empty DMs etc.) are excluded from the
    index; their files remain as resume markers to avoid re-scanning.
    """
    metas = []
    for f in raw.glob("*.json"):
        if f.name in RAW_SKIP_FILES:
            continue
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
            if isinstance(d, dict) and d.get("channel"):
                if not (d["channel"].get("total_count")
                        or d["channel"].get("msg_count")):
                    continue
                metas.append(d["channel"])
        except Exception:
            pass
    metas.sort(key=lambda c: (-(c.get("msg_count") or 0), c.get("title") or ""))
    (raw / "channels.json").write_text(
        json.dumps(metas, ensure_ascii=False), encoding="utf-8")
    return metas


def write_meta_json(raw: Path, session):
    (raw / "meta.json").write_text(
        json.dumps({
            "name": session.get("team_name")
            or (session.get("workspace_url") or "Slack")
            .replace("https://", "").split(".")[0],
            "exported_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }, ensure_ascii=False), encoding="utf-8")


# ---------- API-mode export (fallback) ----------

def run_export(session, cfg, out_dir: Path, force=False, refresh_list=False):
    api = SlackAPI(session["token"], session.get("cookies"),
                   session.get("d_s", ""))
    raw = out_dir / "raw"
    files_dir = out_dir / "files"
    raw.mkdir(parents=True, exist_ok=True)

    users = load_users(api, raw)
    chans = list_channels(api, raw, cfg, refresh_list)
    kw = (cfg.get("channel_filter") or "").strip().lower()
    if kw:
        chans = [c for c in chans
                 if kw in _channel_title(c, users).lower()
                 or kw in (c.get("name") or "").lower()]
    # DMs/group DMs first
    chans = [c for c in chans if c.get("type") in ("im", "mpim")] + \
            [c for c in chans if c.get("type") not in ("im", "mpim")]
    todo = [c for c in chans if force or not (raw / f"{c['id']}.json").exists()]
    print(f"待导出 {len(todo)}/{len(chans)} 个频道（已完成的自动跳过）", flush=True)

    for i, ch in enumerate(todo, 1):
        cid = ch["id"]
        title = _channel_title(ch, users)
        print(f"[{i}/{len(todo)}] 导出「{title}」（{cid}）…", flush=True)
        try:
            msgs = api.paginate("conversations.history", "messages",
                                channel=cid, limit=200)
        except SlackError as e:
            print(f"    跳过：{e}", flush=True)
            continue

        by_ts = {m["ts"]: m for m in msgs}
        for m in msgs:
            if m.get("thread_ts") and m.get("reply_count"):
                try:
                    reps = api.paginate("conversations.replies", "messages",
                                        channel=cid, ts=m["thread_ts"], limit=200)
                    for r in reps:
                        by_ts.setdefault(r["ts"], r)
                except SlackError as e:
                    print(f"    线程获取失败：{e}", flush=True)
        merged = sorted(by_ts.values(), key=lambda m: float(m["ts"]))

        meta = make_meta(ch, merged, users)
        save_channel(raw, files_dir, meta, merged, api, cfg)
        print(f"    {len(merged)} 条消息（含线程回复）", flush=True)

    metas = write_index(raw)
    total = sum(c.get("total_count") or 0 for c in metas)
    write_meta_json(raw, session)
    print(f"导出完成：{len(todo)} 个频道 / {total} 条消息 → {raw}", flush=True)
    return raw
