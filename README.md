# Headcount

Headcount shows who unfollowed you on Instagram, and who you follow that doesn't follow you back.

It works from Instagram's official data export, so it doesn't log in, doesn't need your session
cookie and doesn't scrape anything. Nothing it does can get an account restricted. It only uses
the Python standard library, so there's nothing to install, and everything stays on your
machine: the server only listens on `127.0.0.1`.

## Getting an export

In the Instagram app, go to Settings → Accounts Centre → Your information and permissions →
Download your information. Ask for JSON, not HTML. If you get the option to narrow what's
included, pick just followers and following, because the export arrives much faster that way.

## Use

```
python headcount.py ingest export.zip     save a snapshot from an export
python headcount.py ingest                same, using the newest match in your Downloads
python headcount.py serve                 open the viewer
```

Ingest each new export when it arrives. The viewer compares every snapshot with the one before
it, so the first export only sets a baseline and departures start showing up from the second.

## Config

The settings are at the top of `headcount.py`:

| Setting | What it does |
|---|---|
| `PORT` | port the viewer runs on |
| `OPEN_BROWSER` | whether `serve` opens a browser tab |
| `EXPORT_INBOX`, `INBOX_GLOB` | where a bare `ingest` looks for exports |
| `SKIP_IDENTICAL` | whether to drop a snapshot that's identical to the last one |
| `IGNORE` | handles to leave out of "doesn't follow back" |
| `SKIP_TOKENS` | filename fragments that mark a JSON file as not a follower list |

## Notes

Snapshots are plain JSON files in `snapshots/`. That folder is gitignored because it's a list of
everyone you know.

Instagram reshuffles the export layout every so often, so the script matches files by name
pattern and walks the whole JSON tree instead of reading fixed paths. If a future export stops
parsing, look at `classify()` and `extract_accounts()` first.
