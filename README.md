# Follower ledger

Tracks who unfollowed you on Instagram, and who you follow that doesn't follow back.

Reads Instagram's official data export — no login, no session cookie, no scraping,
nothing that can get an account restricted. Python standard library only, no dependencies.
Everything stays on your machine; the server binds to `127.0.0.1`.

## Getting an export

Instagram app → **Settings → Accounts Centre → Your information and permissions →
Download your information**. Request **JSON** (not HTML), and if there's an option to
narrow the scope, pick followers and following only — it arrives much faster.

## Use

```
python track.py ingest export.zip     save a snapshot from an export
python track.py ingest                same, using the newest match in your Downloads
python track.py serve                 open the viewer
```

Ingest each new export as it arrives. The viewer diffs consecutive snapshots, so the
first one just sets a baseline and the second one starts showing departures.

## Config

Settings live at the top of `track.py`:

| | |
|---|---|
| `PORT` | viewer port |
| `OPEN_BROWSER` | launch a browser tab on `serve` |
| `EXPORT_INBOX` / `INBOX_GLOB` | where a bare `ingest` looks for exports |
| `SKIP_IDENTICAL` | drop a snapshot when nothing changed since the last one |
| `IGNORE` | handles to leave out of "doesn't follow back" |
| `SKIP_TOKENS` | filename fragments that mark a JSON file as *not* a follower list |

## Notes

Snapshots are plain JSON in `snapshots/` — that folder is gitignored, since it's a list
of everyone you know. Instagram reshuffles the export layout every so often, so files are
matched by name pattern and parsed by walking the JSON tree rather than by fixed paths.
If a future export stops parsing, `classify()` and `extract_accounts()` are the two
functions to look at.
