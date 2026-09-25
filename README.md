# Headcount

[![CI](https://img.shields.io/github/actions/workflow/status/IDGBAN/Headcount/ci.yml?branch=main&label=CI&logo=github)](https://github.com/IDGBAN/Headcount/actions/workflows/ci.yml)
[![Latest release](https://img.shields.io/github/v/release/IDGBAN/Headcount?label=release&color=1DB954&logo=github)](https://github.com/IDGBAN/Headcount/releases/latest)
[![Downloads](https://img.shields.io/github/downloads/IDGBAN/Headcount/total?color=1DB954&logo=github)](https://github.com/IDGBAN/Headcount/releases)
[![License: AGPL v3](https://img.shields.io/github/license/IDGBAN/Headcount?color=663366)](LICENSE)

Shows who unfollowed you on Instagram, and who you follow that doesn't follow you back.

It reads the data export Instagram gives you, so it never logs in, never needs a session cookie
and doesn't scrape anything. Nothing it does can get an account restricted. It's one Python
script with no dependencies (Python 3.10 or newer), and everything stays on your machine: the
viewer only listens on `127.0.0.1`.

## Getting an export

In the Instagram app, go to Settings → Accounts Centre → Your information and permissions →
Download your information. Ask for JSON, not HTML, and set the date range to All time. The
default only covers the last year, and an export like that makes everyone who followed you
before then look like they left. If you can narrow what's included, pick followers and
following only. The export arrives much faster that way.

## Use

```
python headcount.py ingest export.zip     save a snapshot from an export
python headcount.py ingest                same, using the newest match in your Downloads
python headcount.py serve                 open the viewer
```

`ingest` also takes an unzipped export folder. On macOS and most Linux systems the command is
`python3`.

Ingest each new export as it arrives. The viewer compares every snapshot with the one before
it, so the first export only sets a baseline and departures start showing up from the second.

In the viewer, `/` jumps to the filter box and the arrow keys move between tabs.

## What ingest refuses

Some exports would quietly wreck the history, so `ingest` stops and says why instead of saving
them:

- an export with no followers list or no following list
- a follower list that's damaged or isn't valid JSON
- an export missing more than half the followers in the last snapshot, once at least 20 of
  them are missing. That's almost always a date range other than All time, or an export from a
  different account. If the drop is real, raise `MAX_FOLLOWER_DROP` and ingest it again.

## Config

Settings live at the top of `headcount.py`:

| Setting | What it does |
|---|---|
| `PORT` | port the viewer listens on |
| `OPEN_BROWSER` | open a browser tab when you run `serve` |
| `EXPORT_INBOX`, `INBOX_GLOB` | where a bare `ingest` looks for exports |
| `SKIP_IDENTICAL` | don't save a snapshot when nothing changed since the last one |
| `IGNORE` | handles to leave out of "doesn't follow back" |
| `SKIP_TOKENS`, `SKIP_FOLDERS` | file and folder names that mark a JSON file as not a follower list |
| `MAX_FOLLOWER_DROP`, `DROP_FLOOR` | share of followers an export can lose before ingest refuses it, and how many have to be missing before that check applies |

## Limits

The export only has usernames, no account IDs. An account that renames itself shows up as one
unfollow plus one new follower, and deactivated or blocked accounts look like unfollows too.

Snapshots are ordered by when you ingested them, not by when Instagram made the export, so
ingest exports in the order you downloaded them.

## Notes

Snapshots are plain JSON in `snapshots/`. That folder is gitignored since it's a list of
everyone you know, and so are export zips and unzipped export folders. Instagram reshuffles the
export layout every so often, so files are matched by name pattern and parsed by walking the
JSON tree rather than by fixed paths. If a future export stops parsing, `classify()` and
`extract_accounts()` are the two functions to look at.

## Development

```
python -m unittest                       run the tests
pip install -r requirements-dev.txt      ruff and mypy, pinned
ruff check .
mypy
```

CI runs all of it on Python 3.10 and 3.13.

## License
Released under the [GNU AGPLv3](LICENSE) license.
