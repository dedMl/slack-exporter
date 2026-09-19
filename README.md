# slack-exporter

**[English](README.md)** | **[简体中文](README.zh-CN.md)**

Export your Slack workspace (DMs, group DMs, channels, canvas docs and
attachments) to local JSON + an offline-browsable HTML viewer, using your
own logged-in account. No admin approval or official export request needed.

> For personal backup / archival of conversations you have access to.
> Respect your workspace's rules and local regulations; do not use this to
> exfiltrate data you are not allowed to access.

## Features

- **No admin export needed** - logs in as a regular user via a real browser
  (Playwright + local Chrome/Edge)
- **Chinese/English adaptive at runtime** - the tool auto-detects and adapts
  to both Chinese and English Slack client interfaces while exporting
  (loading indicators, "beginning of conversation" markers, etc.)
- **Two export modes**
  - `browser` (default): simulates a user opening each channel and scrolling
    up; captures the client's own API responses. Gentle on rate limits,
    resumable, includes a server-side completeness verification pass
  - `api`: direct Web API calls (fast, rate-limit prone)
- **Incremental updates** - re-runs only fetch new messages and merge them
- **Attachments** - files, images and Slack canvas docs downloaded and
  linked locally; canvas cross-links localized into an offline web
- **Offline HTML viewer** - Slack-like UI with channels sidebar, threads,
  reactions, message search, avatar caching; works from `file://`
- **Crash resilient** - per-channel checkpoint files, auto-restart after
  browser crashes, thread/verify retry markers

## Install

```bash
pip install -r requirements.txt
playwright install chromium   # fallback browser; local Chrome/Edge preferred
```

Copy `config.example.json` to `config.json` and set `workspace_url` (e.g.
`https://your-team.slack.com`). `email`/`password` are optional - email
codes and MFA can be entered manually in the browser window.

## Usage

```bash
python main.py                  # interactive wizard
python main.py login            # login only
python main.py export           # incremental export
python main.py export --update full        # full re-export
python main.py export --scope dm           # DMs/group DMs only
python main.py export --scope docs         # canvas docs only
python main.py html             # (re)build the offline viewer
```

First run opens a browser window: log in (email code / 2FA works), the
session is saved to `session.json` + `.profile/` and reused afterwards.

Results:

```
output/raw/       raw JSON data (one file per channel)
output/files/     attachments and offline canvas pages
output/viewer/index.html   open in any browser, fully offline
```

## Config reference (config.json)

| Key | Default | Description |
|---|---|---|
| `workspace_url` | - | Workspace URL, required |
| `email` / `password` | empty | Optional login autofill |
| `download_files` | `true` | Download attachments |
| `max_file_mb` | `200` | Skip attachments larger than this |
| `channel_delay_s` | `2` | Pause between channels |
| `scroll_wait_ms` | `900` | Wait between scroll steps |
| `scroll_idle_limit` | `15` | Idle rounds before giving up at top |
| `thread_pace_s` | `0.5` | Pause between thread fetches |
| `max_scrolls` / `max_channel_seconds` | `500` / `900` | Per-channel caps |

## Notes

- Requires a locally installed Chrome or Edge (the bundled Chromium is a
  fallback)
- `config.json`, `session.json`, `storage.json` and `.profile/` hold
  personal credentials - never share or commit them
- Session tokens expire; on auth errors run
  `python main.py login --relogin`

## License

MIT
