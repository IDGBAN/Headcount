#!/usr/bin/env python3
"""Headcount, a follower tracker built on Instagram's own data export.

    python headcount.py ingest [path]   read an export zip/folder and save a snapshot
    python headcount.py serve           diff the saved snapshots and open the viewer
"""

import io
import json
import re
import socket
import sys
import threading
import traceback
import webbrowser
import zipfile
import zlib
from collections.abc import Callable, Iterator
from datetime import datetime
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from itertools import pairwise
from pathlib import Path
from typing import Any, Literal, TypedDict, cast
from urllib.parse import SplitResult, quote, urlsplit

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
IGNORE: list[str] = [
    # "someaccount",
]

# a filename containing any of these is not a follower list
SKIP_TOKENS = ("request", "unfollow", "block", "restrict", "close_friend",
               "hide", "pending", "removed", "recently", "hashtag")

# and neither is anything inside a folder with one of these names. threads keeps its own
# follower lists, and macos adds __macosx/._* metadata twins when it re-zips a folder
SKIP_FOLDERS = ("threads", "__macosx")

# ingest refuses an export missing more than this share of the last snapshot's followers, which is
# nearly always a date-limited or wrong-account export. 0.5 by default, 1 turns it off.
# losses smaller than DROP_FLOOR accounts are always accepted
MAX_FOLLOWER_DROP = 0.5
DROP_FLOOR = 20

PROFILE_URL = "https://www.instagram.com/{}/"
HANDLE_PATTERN = re.compile(r"[a-z0-9._]{1,30}")

Kind = Literal["followers", "following"]
KINDS: tuple[Kind, ...] = ("followers", "following")


class Account(TypedDict):
    username: str
    href: str
    timestamp: int | None


class Snapshot(TypedDict):
    taken_at: str
    source: str
    followers: list[Account]
    following: list[Account]


def classify(path: str) -> Kind | None:
    *folders, name = path.lower().split("/")
    if any(folder in SKIP_FOLDERS for folder in folders) or name.startswith("._"):
        return None
    if not name.endswith(".json") or any(token in name for token in SKIP_TOKENS):
        return None
    if "following" in name:
        return "following"
    if "follower" in name:
        return "followers"
    return None


def plural(count: int, word: str) -> str:
    return f"{count} {word}{'' if count == 1 else 's'}"


def normalise_handle(handle: str) -> str:
    return handle.strip().lstrip("@").lower()


def profile_url(handle: str) -> str:
    return PROFILE_URL.format(quote(handle))


def parse_link(href: object) -> SplitResult | None:
    if not isinstance(href, str):
        return None
    try:
        return urlsplit(href)
    except ValueError:
        return None


def pick_handle(entry: dict[str, object], title: object) -> str:
    link = parse_link(entry.get("href"))
    host = link.hostname if link else None
    # threads lists use the same layout, but their links point at threads.com
    if host and host != "instagram.com" and not host.endswith(".instagram.com"):
        return ""

    # following.json entries have no "value", the handle is only in the href and the parent's title
    from_href = link.path.rstrip("/").rsplit("/", 1)[-1] if link else ""
    for candidate in (entry.get("value"), from_href, title):
        if isinstance(candidate, str):
            handle = normalise_handle(candidate)
            if HANDLE_PATTERN.fullmatch(handle):
                return handle
    return ""


def extract_accounts(node: object) -> list[Account]:
    """Instagram nests handles under string_list_data at varying depths, so walk the whole tree."""
    found: list[Account] = []

    if isinstance(node, dict):
        entries = node.get("string_list_data")
        if isinstance(entries, list):
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                handle = pick_handle(entry, node.get("title"))
                if handle:
                    timestamp = entry.get("timestamp")
                    found.append({
                        "username": handle,
                        "href": profile_url(handle),
                        "timestamp": timestamp if isinstance(timestamp, int) else None,
                    })
        for key, value in node.items():
            if key != "string_list_data":
                found.extend(extract_accounts(value))

    elif isinstance(node, list):
        for item in node:
            found.extend(extract_accounts(item))

    return found


def export_files(source: Path) -> Iterator[tuple[str, Callable[[], bytes]]]:
    """Yield (path, reader) for every file in an export zip or folder, without reading it."""
    if source.is_dir():
        for path in sorted(source.rglob("*")):
            if path.is_file():
                yield path.relative_to(source).as_posix(), path.read_bytes

    elif zipfile.is_zipfile(source):
        with zipfile.ZipFile(source) as archive:
            for info in archive.infolist():
                if not info.is_dir():
                    # zipfile only turns backslash separators into slashes on windows
                    yield info.filename.replace("\\", "/"), partial(archive.read, info)

    elif source.exists():
        raise SystemExit(f"not a zip or a folder: {source}")
    else:
        raise SystemExit(f"no such file or folder: {source}")


def load_follow_list(path: str, read: Callable[[], bytes], source: Path) -> object:
    try:
        raw = read()
    except (OSError, zipfile.BadZipFile, zlib.error, NotImplementedError, RuntimeError) as error:
        raise SystemExit(f"couldn't read {path} from {source.name} ({error})\n"
                         "try downloading the export again") from None

    # skipping a broken list would make everyone on it look like they left
    try:
        return json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise SystemExit(f"{path} in {source.name} isn't valid JSON\n"
                         "try downloading the export again") from None


def explain_missing_lists(paths: list[str]) -> str:
    lowered = [path.lower() for path in paths]
    if any(path.endswith(".html") and classify(path[:-5] + ".json") for path in lowered):
        return "the export is in HTML format, request it again as JSON"
    nested = [path for path in paths if path.lower().endswith(".zip")]
    if nested:
        return f"it holds another zip ({nested[0]}), extract that and ingest it instead"
    return "request the export again in JSON format, with followers and following included"


def build_snapshot(source: Path) -> Snapshot:
    buckets: dict[Kind, dict[str, Account]] = {kind: {} for kind in KINDS}
    found_lists: set[Kind] = set()
    other_files: list[str] = []

    for path, read in export_files(source):
        kind = classify(path)
        if kind is None:
            other_files.append(path)
            continue
        found_lists.add(kind)
        for account in extract_accounts(load_follow_list(path, read, source)):
            buckets[kind].setdefault(account["username"], account)

    if not found_lists:
        raise SystemExit(f"no follower lists found in {source}\n"
                         f"{explain_missing_lists(other_files)}")

    # diffing against an export that lacks one list would report every account on it as gone
    missing = [kind for kind in KINDS if kind not in found_lists]
    if missing:
        raise SystemExit(
            f"{source.name} has no {missing[0]} list, so it can't be compared "
            "with other snapshots\n"
            "request the export again with both followers and following included"
        )

    if not buckets["followers"] and not buckets["following"]:
        raise SystemExit(
            f"found the follower lists in {source} but no accounts in them\n"
            "the export format may have changed, see classify() and extract_accounts()"
        )

    return {
        "taken_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "source": source.resolve().name,
        "followers": sorted(buckets["followers"].values(), key=lambda a: a["username"]),
        "following": sorted(buckets["following"].values(), key=lambda a: a["username"]),
    }


def handles(snapshot: Snapshot, kind: Kind) -> set[str]:
    return {account["username"] for account in snapshot[kind]}


def same_accounts(first: Snapshot, second: Snapshot) -> bool:
    return all(handles(first, kind) == handles(second, kind) for kind in KINDS)


def parse_time(stamp: str) -> datetime:
    moment = datetime.fromisoformat(stamp)
    return moment if moment.tzinfo else moment.astimezone()


def read_snapshot(path: Path) -> Snapshot | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        parse_time(data["taken_at"])
    except (OSError, UnicodeDecodeError, ValueError, KeyError, TypeError):
        return None

    if not all(isinstance(data.get(kind), list) for kind in KINDS):
        return None
    for kind in KINDS:
        data[kind] = [account for account in data[kind]
                      if isinstance(account, dict) and isinstance(account.get("username"), str)]
    return cast(Snapshot, data)


def load_snapshots() -> list[Snapshot]:
    snapshots = []
    for path in sorted(SNAPSHOT_DIR.glob("*.json")):
        snapshot = read_snapshot(path)
        if snapshot is None:
            print(f"skipping unreadable snapshot: {path.name}", file=sys.stderr)
        else:
            snapshots.append(snapshot)

    # sorting the raw strings goes wrong when the utc offset changes between snapshots (dst, travel)
    snapshots.sort(key=lambda snapshot: parse_time(snapshot["taken_at"]))
    return snapshots


def save_snapshot(snapshot: Snapshot) -> Path:
    SNAPSHOT_DIR.mkdir(exist_ok=True)
    stem = f"{parse_time(snapshot['taken_at']):%Y-%m-%d_%H%M%S}"
    path = SNAPSHOT_DIR / f"{stem}.json"
    suffix = 2
    while path.exists():
        path = SNAPSHOT_DIR / f"{stem}_{suffix}.json"
        suffix += 1

    # written beside the target and renamed, so a crash never leaves half a snapshot behind
    temp = path.with_name(path.name + ".partial")
    temp.write_text(json.dumps(snapshot, indent=2), encoding="utf-8")
    temp.replace(path)
    return path


def expand(names: set[str]) -> list[dict[str, str]]:
    return [{"username": name, "href": profile_url(name)} for name in sorted(names)]


def analyse(snapshots: list[Snapshot]) -> dict[str, object]:
    if not snapshots:
        return {"snapshot_count": 0}

    states = [(snapshot, {kind: handles(snapshot, kind) for kind in KINDS})
              for snapshot in snapshots]

    timeline = []
    for (older, before), (newer, after) in pairwise(states):
        timeline.append({
            "from": older["taken_at"],
            "to": newer["taken_at"],
            "lost_followers": expand(before["followers"] - after["followers"]),
            "new_followers": expand(after["followers"] - before["followers"]),
            "you_unfollowed": expand(before["following"] - after["following"]),
            "you_followed": expand(after["following"] - before["following"]),
        })

    latest, current = states[-1]
    followers = current["followers"]
    following = current["following"]
    ignored = {normalise_handle(handle) for handle in IGNORE}

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
        "not_following_back": expand(following - followers - ignored),
        "not_followed_back": expand(followers - following),
        "since_last": timeline[-1] if timeline else None,
        "timeline": list(reversed(timeline)),
    }


# the viewer's script and styles are inline, everything else (including fetches to
# other origins) is refused
CONTENT_POLICY = ("default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
                  "img-src data:; connect-src 'self'; base-uri 'none'; form-action 'none'; "
                  "frame-ancestors 'none'")


class LocalServer(ThreadingHTTPServer):
    # on windows SO_REUSEADDR lets a second server bind a port that is already taken
    allow_reuse_address = sys.platform != "win32"

    def server_bind(self) -> None:
        super().server_bind()
        self.allowed_hosts = {f"127.0.0.1:{self.server_port}", f"localhost:{self.server_port}"}

    def handle_error(self, request: socket.socket | tuple[bytes, socket.socket],
                     client_address: Any) -> None:
        # a reload or a closed tab mid-response isn't worth a traceback
        if not isinstance(sys.exc_info()[1], ConnectionError):
            super().handle_error(request, client_address)


class Handler(BaseHTTPRequestHandler):
    server: LocalServer

    def do_GET(self) -> None:
        # a site that points its own hostname at 127.0.0.1 (dns rebinding) would otherwise
        # be able to read the follower lists from the browser
        if self.headers.get("Host") not in self.server.allowed_hosts:
            self.respond(403, "text/plain; charset=utf-8", b"unexpected host")
            return

        path = urlsplit(self.path).path
        if path == "/api/data":
            self.send_data()
        elif path in ("/", "/index.html"):
            self.send_index()
        else:
            self.respond(404, "text/plain; charset=utf-8", b"not found")

    def send_data(self) -> None:
        try:
            payload = json.dumps(analyse(load_snapshots())).encode("utf-8")
        except Exception:
            traceback.print_exc()
            self.respond(500, "text/plain; charset=utf-8", b"couldn't analyse the snapshots")
            return
        self.respond(200, "application/json; charset=utf-8", payload)

    def send_index(self) -> None:
        try:
            page = INDEX_FILE.read_bytes()
        except FileNotFoundError:
            self.respond(404, "text/plain; charset=utf-8",
                         b"index.html is missing from the project folder")
            return
        self.respond(200, "text/html; charset=utf-8", page,
                     {"Content-Security-Policy": CONTENT_POLICY})

    def respond(self, status: int, content_type: str, body: bytes,
                headers: dict[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        pass


def serve() -> None:
    try:
        server = LocalServer(("127.0.0.1", PORT), Handler)
    except OSError as error:
        raise SystemExit(
            f"can't listen on port {PORT} ({error.strerror or error})\n"
            "the viewer may already be running; otherwise change PORT at the top of headcount.py"
        ) from None

    url = f"http://127.0.0.1:{PORT}/"
    print(f"viewer running at {url}   (ctrl-c to stop)")

    try:
        # a text-mode browser blocks until it exits, so it gets a thread of its own
        if OPEN_BROWSER:
            threading.Thread(target=webbrowser.open, args=(url,), daemon=True).start()
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        server.server_close()


def newest_in_inbox() -> Path | None:
    found = [path for path in EXPORT_INBOX.glob(INBOX_GLOB) if path.is_file()]
    return max(found, key=lambda path: path.stat().st_mtime, default=None)


def ingest(source: Path | None) -> None:
    if source is None:
        source = newest_in_inbox()
        if source is None:
            raise SystemExit(f"no export matching {INBOX_GLOB} in {EXPORT_INBOX}")

    snapshot = build_snapshot(source)
    existing = load_snapshots()
    previous = existing[-1] if existing else None

    print(f"{snapshot['source']}: {plural(len(snapshot['followers']), 'follower')}, "
          f"{len(snapshot['following'])} following")

    if previous is None:
        saved = save_snapshot(snapshot)
        print(f"saved snapshots/{saved.name}")
        print("first snapshot, the next export you ingest gets compared against this one")
        return

    before = handles(previous, "followers")
    after = handles(snapshot, "followers")
    lost, gained = before - after, after - before

    if len(lost) >= DROP_FLOOR and len(lost) > MAX_FOLLOWER_DROP * len(before):
        raise SystemExit(
            f"not saved: {len(lost)} of the {len(before)} followers in the last snapshot "
            "are missing from this export\n"
            "its date range probably wasn't set to All time, or it's from another account\n"
            "if the drop is real, raise MAX_FOLLOWER_DROP in headcount.py and ingest again"
        )

    if SKIP_IDENTICAL and same_accounts(previous, snapshot):
        print("identical to the last snapshot, nothing saved")
        return

    saved = save_snapshot(snapshot)
    print(f"saved snapshots/{saved.name}")
    print(f"since {parse_time(previous['taken_at']):%Y-%m-%d}: "
          f"{len(lost)} unfollowed you, {plural(len(gained), 'new follower')}")


def main(argv: list[str]) -> None:
    # export names can hold characters the console code page can't print
    if isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout.reconfigure(errors="backslashreplace")

    command, *args = argv or ["serve"]

    if command == "ingest" and len(args) <= 1:
        ingest(Path(args[0]).expanduser() if args else None)
    elif command == "serve" and not args:
        serve()
    elif command in ("help", "-h", "--help"):
        print((__doc__ or "").strip())
    else:
        raise SystemExit(__doc__)


if __name__ == "__main__":
    main(sys.argv[1:])
