#!/usr/bin/env python3
"""Follower tracker built on Instagram's own data export.

    python track.py ingest [path]   read an export zip/folder and save a snapshot
    python track.py serve           diff the saved snapshots and open the viewer
"""

import json
import sys
import webbrowser
import zipfile
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# ---------------------------------------------------------------- config

ROOT = Path(__file__).resolve().parent
SNAPSHOT_DIR = ROOT / "snapshots"
INDEX_FILE = ROOT / "index.html"

PORT = 8412
OPEN_BROWSER = True

# searched when ingest is called with no path
EXPORT_INBOX = Path.home() / "Downloads"
INBOX_GLOB = "*instagram*.zip"

# a snapshot identical to the previous one gets dropped instead of saved
SKIP_IDENTICAL = True

# handles kept out of "doesn't follow back" - brands, alts, anyone you don't expect a follow from
IGNORE = [
    # "someaccount",
]

# a filename containing any of these is not a follower list
SKIP_TOKENS = ("request", "unfollow", "block", "restrict", "close_friend",
               "hide", "pending", "removed", "recently")

# ---------------------------------------------------------------- parsing


def classify(filename):
    name = filename.lower()
    if not name.endswith(".json"):
        return None
    if any(token in name for token in SKIP_TOKENS):
        return None
    if "following" in name:
        return "following"
    if "follower" in name:
        return "followers"
    return None


def extract_accounts(node):
    """Instagram nests handles under string_list_data at varying depths, so walk the whole tree."""
    found = []

    if isinstance(node, dict):
        entries = node.get("string_list_data")
        if isinstance(entries, list):
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                handle = (entry.get("value") or "").strip()
                href = (entry.get("href") or "").strip()
                if not handle and href:
                    handle = href.rstrip("/").rsplit("/", 1)[-1]
                if handle:
                    found.append({
                        "username": handle.lower(),
                        "href": href or f"https://www.instagram.com/{handle}/",
                        "timestamp": entry.get("timestamp"),
                    })
        for key, value in node.items():
            if key != "string_list_data":
                found.extend(extract_accounts(value))

    elif isinstance(node, list):
        for item in node:
            found.extend(extract_accounts(item))

    return found


def iter_export_files(source):
    if source.is_file() and source.suffix.lower() == ".zip":
        with zipfile.ZipFile(source) as archive:
            for name in archive.namelist():
                if not name.endswith("/"):
                    yield name, archive.read(name)
    elif source.is_dir():
        for path in sorted(source.rglob("*.json")):
            yield str(path.relative_to(source)), path.read_bytes()
    else:
        raise SystemExit(f"not a zip or a folder: {source}")


def build_snapshot(source):
    buckets = {"followers": {}, "following": {}}

    for name, raw in iter_export_files(source):
        kind = classify(Path(name).name)
        if kind is None:
            continue
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        for account in extract_accounts(data):
            buckets[kind].setdefault(account["username"], account)

    if not buckets["followers"] and not buckets["following"]:
        raise SystemExit(
            f"no follower data found in {source}\n"
            "the export needs to be requested in JSON format, not HTML"
        )

    return {
        "taken_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "source": source.name,
        "followers": sorted(buckets["followers"].values(), key=lambda a: a["username"]),
        "following": sorted(buckets["following"].values(), key=lambda a: a["username"]),
    }


# ---------------------------------------------------------------- storage


def handles(snapshot, kind):
    return {account["username"] for account in snapshot.get(kind, [])}


def load_snapshots():
    if not SNAPSHOT_DIR.exists():
        return []

    snapshots = []
    for path in sorted(SNAPSHOT_DIR.glob("*.json")):
        try:
            snapshots.append(json.loads(path.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            print(f"skipping unreadable snapshot: {path.name}")

    snapshots.sort(key=lambda snapshot: snapshot.get("taken_at", ""))
    return snapshots


def save_snapshot(snapshot):
    SNAPSHOT_DIR.mkdir(exist_ok=True)
    existing = load_snapshots()

    if SKIP_IDENTICAL and existing:
        previous = existing[-1]
        unchanged = all(handles(previous, kind) == handles(snapshot, kind)
                        for kind in ("followers", "following"))
        if unchanged:
            return None

    path = SNAPSHOT_DIR / f"{datetime.now():%Y-%m-%d_%H%M%S}.json"
    path.write_text(json.dumps(snapshot, indent=2), encoding="utf-8")
    return path


# ---------------------------------------------------------------- analysis


def analyse(snapshots):
    if not snapshots:
        return {"snapshot_count": 0}

    lookup = {}
    for snapshot in snapshots:
        for account in snapshot["followers"] + snapshot["following"]:
            lookup[account["username"]] = {
                "username": account["username"],
                "href": account["href"],
            }

    def expand(names):
        return [lookup.get(name, {"username": name,
                                  "href": f"https://www.instagram.com/{name}/"})
                for name in sorted(names)]

    timeline = []
    for older, newer in zip(snapshots, snapshots[1:]):
        timeline.append({
            "from": older["taken_at"],
            "to": newer["taken_at"],
            "lost_followers": expand(handles(older, "followers") - handles(newer, "followers")),
            "new_followers": expand(handles(newer, "followers") - handles(older, "followers")),
            "you_unfollowed": expand(handles(older, "following") - handles(newer, "following")),
            "you_followed": expand(handles(newer, "following") - handles(older, "following")),
        })

    latest = snapshots[-1]
    followers = handles(latest, "followers")
    following = handles(latest, "following")

    return {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "snapshot_count": len(snapshots),
        "taken_at": latest["taken_at"],
        "source": latest.get("source", ""),
        "totals": {
            "followers": len(followers),
            "following": len(following),
            "mutuals": len(followers & following),
        },
        "not_following_back": expand(following - followers - set(IGNORE)),
        "not_followed_back": expand(followers - following),
        "since_last": timeline[-1] if timeline else None,
        "timeline": list(reversed(timeline)),
    }


# ---------------------------------------------------------------- server


class Handler(BaseHTTPRequestHandler):

    def do_GET(self):
        if self.path.startswith("/api/data"):
            payload = json.dumps(analyse(load_snapshots())).encode("utf-8")
            self.respond(200, "application/json; charset=utf-8", payload)
        elif self.path in ("/", "/index.html"):
            if INDEX_FILE.exists():
                self.respond(200, "text/html; charset=utf-8", INDEX_FILE.read_bytes())
            else:
                self.respond(404, "text/plain; charset=utf-8",
                             b"index.html is missing from the project folder")
        else:
            self.respond(404, "text/plain; charset=utf-8", b"not found")

    def respond(self, status, content_type, body):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def serve():
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    url = f"http://127.0.0.1:{PORT}/"
    print(f"viewer running at {url}   (ctrl-c to stop)")

    if OPEN_BROWSER:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        server.server_close()


# ---------------------------------------------------------------- entry point


def newest_in_inbox():
    found = sorted(EXPORT_INBOX.glob(INBOX_GLOB), key=lambda path: path.stat().st_mtime)
    return found[-1] if found else None


def main(argv):
    command = argv[0] if argv else "serve"

    if command == "ingest":
        source = Path(argv[1]).expanduser() if len(argv) > 1 else newest_in_inbox()
        if source is None:
            raise SystemExit(f"no export matching {INBOX_GLOB} in {EXPORT_INBOX}")

        snapshot = build_snapshot(source)
        saved = save_snapshot(snapshot)
        print(f"{source.name}: {len(snapshot['followers'])} followers, "
              f"{len(snapshot['following'])} following")
        print(f"saved snapshots/{saved.name}" if saved
              else "identical to the last snapshot, nothing saved")

    elif command == "serve":
        serve()

    else:
        raise SystemExit(__doc__)


if __name__ == "__main__":
    main(sys.argv[1:])
