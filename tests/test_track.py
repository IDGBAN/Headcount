import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import unittest
import zipfile
from contextlib import redirect_stderr, redirect_stdout
from http.client import HTTPConnection
from io import StringIO
from pathlib import Path
from unittest import mock

import track

ROOT = Path(__file__).resolve().parent.parent


def followers_file(*names):
    return [{"title": "", "media_list_data": [], "string_list_data": [
        {"href": f"https://www.instagram.com/{name}", "value": name, "timestamp": 1700000000},
    ]} for name in names]


# following.json has no "value", the handle is in the title and in an /_u/ href
def following_file(*names):
    return {"relationships_following": [{"title": name, "string_list_data": [
        {"href": f"https://www.instagram.com/_u/{name}", "timestamp": 1700000000},
    ]} for name in names]}


def hashtags_file(*tags):
    return {"relationships_following_hashtags": [{"title": "", "string_list_data": [
        {"href": f"https://www.instagram.com/explore/tags/{tag}", "value": tag},
    ]} for tag in tags]}


def export_files(followers=(), following=()):
    folder = "connections/followers_and_following/"
    return {
        folder + "followers_1.json": followers_file(*followers),
        folder + "following.json": following_file(*following),
        folder + "following_hashtags.json": hashtags_file("sunsets"),
        folder + "pending_follow_requests.json": followers_file("pending"),
        folder + "recently_unfollowed_profiles.json": followers_file("gone"),
    }


def write_zip(path, files):
    with zipfile.ZipFile(path, "w") as archive:
        for name, content in files.items():
            archive.writestr(name, content if isinstance(content, bytes) else json.dumps(content))
    return path


def snapshot(taken_at, followers=(), following=()):
    def accounts(names):
        return [{"username": name, "href": track.profile_url(name), "timestamp": None}
                for name in names]
    return {"taken_at": taken_at, "source": "test.zip",
            "followers": accounts(followers), "following": accounts(following)}


def quietly(function, *args):
    with redirect_stdout(StringIO()) as out, redirect_stderr(StringIO()):
        function(*args)
    return out.getvalue()


class TempDirTest(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        patcher = mock.patch.object(track, "SNAPSHOT_DIR", self.tmp / "snapshots")
        patcher.start()
        self.addCleanup(patcher.stop)


class ClassifyTests(unittest.TestCase):

    def test_follow_lists(self):
        self.assertEqual(track.classify("followers_1.json"), "followers")
        self.assertEqual(track.classify("followers_12.json"), "followers")
        self.assertEqual(track.classify("following.json"), "following")
        self.assertEqual(track.classify("Following.JSON"), "following")

    def test_threads_and_mac_metadata(self):
        self.assertIsNone(track.classify("your_instagram_activity/threads/followers.json"))
        self.assertIsNone(track.classify("__MACOSX/connections/._followers_1.json"))
        self.assertIsNone(track.classify("connections/followers_and_following/._following.json"))
        self.assertEqual(track.classify("connections/followers_and_following/following.json"),
                         "following")

    def test_other_files(self):
        for name in ("following_hashtags.json", "pending_follow_requests.json",
                     "recently_unfollowed_profiles.json", "close_friends.json",
                     "blocked_profiles.json", "restricted_profiles.json",
                     "removed_suggestions.json", "hide_story_from.json",
                     "follow_requests_you've_received.json", "followers_1.html",
                     "liked_posts.json"):
            self.assertIsNone(track.classify(name), name)


class ExtractAccountsTests(unittest.TestCase):

    def test_value_format(self):
        accounts = track.extract_accounts(followers_file("Alice", "bob.b"))
        self.assertEqual([a["username"] for a in accounts], ["alice", "bob.b"])
        self.assertEqual(accounts[0]["href"], "https://www.instagram.com/alice/")
        self.assertEqual(accounts[0]["timestamp"], 1700000000)

    def test_title_and_href_format(self):
        accounts = track.extract_accounts(following_file("carol_x"))
        self.assertEqual(accounts[0]["username"], "carol_x")
        self.assertEqual(accounts[0]["href"], "https://www.instagram.com/carol_x/")

    def test_href_only(self):
        data = [{"string_list_data": [{"href": "https://www.instagram.com/dave/?igsh=abc"}]}]
        self.assertEqual(track.extract_accounts(data)[0]["username"], "dave")

    def test_title_used_when_href_missing(self):
        data = [{"title": "erin", "string_list_data": [{"timestamp": 1}]}]
        self.assertEqual(track.extract_accounts(data)[0]["username"], "erin")

    def test_href_is_rebuilt_from_handle(self):
        data = [{"string_list_data": [{"value": "frank", "href": "javascript:alert(1)"}]}]
        self.assertEqual(track.extract_accounts(data)[0]["href"],
                         "https://www.instagram.com/frank/")

    def test_non_instagram_links_are_skipped(self):
        entry = {"href": "https://www.threads.com/@someone", "value": "someone"}
        data = {"text_post_app_text_post_app_followers": [
            {"title": "Some One", "string_list_data": [entry]}]}
        self.assertEqual(track.extract_accounts(data), [])

    def test_invalid_value_falls_back_to_href(self):
        data = [{"string_list_data": [
            {"value": "Hana Lee", "href": "https://www.instagram.com/hana.lee"}]}]
        self.assertEqual(track.extract_accounts(data)[0]["username"], "hana.lee")

    def test_nested_and_junk(self):
        data = {"a": {"b": [{"string_list_data": [
            "junk", {}, {"value": "  "}, {"value": "@grace", "timestamp": "soon"},
        ]}]}}
        accounts = track.extract_accounts(data)
        self.assertEqual([a["username"] for a in accounts], ["grace"])
        self.assertIsNone(accounts[0]["timestamp"])


class BuildSnapshotTests(TempDirTest):

    def test_zip(self):
        path = write_zip(self.tmp / "export.zip",
                         export_files(followers=["amy", "ben"], following=["ben", "cat"]))
        built = track.build_snapshot(path)
        self.assertEqual([a["username"] for a in built["followers"]], ["amy", "ben"])
        self.assertEqual([a["username"] for a in built["following"]], ["ben", "cat"])
        self.assertEqual(built["source"], "export.zip")

    def test_folder(self):
        for name, content in export_files(followers=["amy"], following=["cat"]).items():
            path = self.tmp / "export" / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(content), encoding="utf-8")
        built = track.build_snapshot(self.tmp / "export")
        self.assertEqual(track.handles(built, "followers"), {"amy"})
        self.assertEqual(track.handles(built, "following"), {"cat"})

    def test_split_follower_files_are_merged(self):
        files = export_files(followers=["amy"], following=["cat"])
        files["connections/followers_and_following/followers_2.json"] = followers_file("amy", "dan")
        built = track.build_snapshot(write_zip(self.tmp / "export.zip", files))
        self.assertEqual(track.handles(built, "followers"), {"amy", "dan"})

    def test_threads_and_mac_files_are_ignored(self):
        files = export_files(followers=["amy"], following=["cat"])
        files["your_instagram_activity/threads/followers.json"] = followers_file("tina")
        files["__MACOSX/connections/followers_and_following/._followers_1.json"] = b"\x00\x05\x16"
        built = track.build_snapshot(write_zip(self.tmp / "export.zip", files))
        self.assertEqual(track.handles(built, "followers"), {"amy"})

    def test_only_follow_lists_are_read(self):
        files = export_files(followers=["amy"], following=["cat"])
        files["media/posts/202401/video.mp4"] = b"\0" * 1024
        path = write_zip(self.tmp / "export.zip", files)
        with mock.patch.object(zipfile.ZipFile, "read", autospec=True,
                               side_effect=zipfile.ZipFile.read) as read:
            track.build_snapshot(path)
        names = {call.args[1].filename for call in read.call_args_list}
        self.assertEqual(names, {"connections/followers_and_following/followers_1.json",
                                 "connections/followers_and_following/following.json"})

    def test_missing_list_is_refused(self):
        files = {"followers_and_following/followers_1.json": followers_file("amy")}
        with self.assertRaises(SystemExit) as caught:
            track.build_snapshot(write_zip(self.tmp / "export.zip", files))
        self.assertIn("no following list", str(caught.exception))

    def test_html_export_is_refused(self):
        files = {"followers_and_following/followers_1.html": b"<html></html>"}
        with self.assertRaises(SystemExit) as caught:
            track.build_snapshot(write_zip(self.tmp / "export.zip", files))
        self.assertIn("HTML format", str(caught.exception))

    def test_nested_zip_is_explained(self):
        inner = write_zip(self.tmp / "inner.zip", export_files(["amy"], ["cat"]))
        outer = write_zip(self.tmp / "outer.zip", {"instagram-me-2026.zip": inner.read_bytes()})
        with self.assertRaises(SystemExit) as caught:
            track.build_snapshot(outer)
        self.assertIn("instagram-me-2026.zip", str(caught.exception))

    def test_broken_list_is_refused(self):
        files = export_files(followers=["amy"], following=["cat"])
        files["connections/followers_and_following/followers_2.json"] = b'[{"string_list'
        with self.assertRaises(SystemExit) as caught:
            track.build_snapshot(write_zip(self.tmp / "export.zip", files))
        self.assertIn("followers_2.json", str(caught.exception))

    def test_lists_without_accounts_are_refused(self):
        files = {"followers_1.json": [], "following.json": {"relationships_following": []}}
        with self.assertRaises(SystemExit) as caught:
            track.build_snapshot(write_zip(self.tmp / "export.zip", files))
        self.assertIn("no accounts", str(caught.exception))

    def test_backslash_names(self):
        files = {"connections\\followers_and_following\\" + name.rsplit("/", 1)[-1]: content
                 for name, content in export_files(["amy"], ["cat"]).items()}
        built = track.build_snapshot(write_zip(self.tmp / "export.zip", files))
        self.assertEqual(track.handles(built, "followers"), {"amy"})
        self.assertEqual(track.handles(built, "following"), {"cat"})

    def test_bad_paths(self):
        with self.assertRaises(SystemExit) as caught:
            track.build_snapshot(self.tmp / "nope.zip")
        self.assertIn("no such file", str(caught.exception))

        broken = self.tmp / "broken.zip"
        broken.write_bytes(b"not a zip")
        with self.assertRaises(SystemExit) as caught:
            track.build_snapshot(broken)
        self.assertIn("not a zip", str(caught.exception))


class StorageTests(TempDirTest):

    def test_round_trip(self):
        saved = track.save_snapshot(snapshot("2026-01-01T10:00:00+00:00", ["amy"], ["ben"]))
        self.assertEqual(saved.name, "2026-01-01_100000.json")
        self.assertEqual(track.load_snapshots(), [snapshot("2026-01-01T10:00:00+00:00",
                                                           ["amy"], ["ben"])])
        self.assertEqual(list(track.SNAPSHOT_DIR.glob("*.partial")), [])

    def test_same_second_does_not_overwrite(self):
        first = track.save_snapshot(snapshot("2026-01-01T10:00:00+00:00", ["amy"]))
        second = track.save_snapshot(snapshot("2026-01-01T10:00:00+00:00", ["ben"]))
        self.assertNotEqual(first, second)
        self.assertEqual(len(track.load_snapshots()), 2)

    def test_sorted_by_real_time_across_offsets(self):
        # 01:30 at -04:00 is before 01:10 at -05:00, the night the clocks go back
        track.save_snapshot(snapshot("2026-11-01T01:10:00-05:00", ["late"]))
        track.save_snapshot(snapshot("2026-11-01T01:30:00-04:00", ["early"]))
        order = [s["followers"][0]["username"] for s in track.load_snapshots()]
        self.assertEqual(order, ["early", "late"])

    def test_bad_files_are_skipped(self):
        track.save_snapshot(snapshot("2026-01-01T10:00:00+00:00", ["amy"]))
        folder = track.SNAPSHOT_DIR
        (folder / "truncated.json").write_text('{"taken_at": ', encoding="utf-8")
        (folder / "list.json").write_text("[]", encoding="utf-8")
        (folder / "no_lists.json").write_text('{"taken_at": "2026-01-01"}', encoding="utf-8")
        (folder / "bad_time.json").write_text(
            json.dumps(snapshot("yesterday", ["amy"])), encoding="utf-8")
        (folder / "binary.json").write_bytes(b"\xff\xfe\x00")

        with redirect_stderr(StringIO()) as err:
            loaded = track.load_snapshots()
        self.assertEqual(len(loaded), 1)
        self.assertEqual(err.getvalue().count("skipping unreadable snapshot"), 5)

    def test_bad_accounts_are_dropped(self):
        data = snapshot("2026-01-01T10:00:00+00:00", ["amy"])
        data["followers"].extend(["junk", {"href": "x"}])
        track.SNAPSHOT_DIR.mkdir()
        (track.SNAPSHOT_DIR / "a.json").write_text(json.dumps(data), encoding="utf-8")
        self.assertEqual(track.handles(track.load_snapshots()[0], "followers"), {"amy"})

    def test_missing_folder(self):
        self.assertEqual(track.load_snapshots(), [])


class AnalyseTests(unittest.TestCase):

    def test_empty(self):
        self.assertEqual(track.analyse([]), {"snapshot_count": 0})

    def test_single_snapshot(self):
        result = track.analyse([snapshot("2026-01-01T00:00:00+00:00", ["amy"], ["ben"])])
        self.assertIsNone(result["since_last"])
        self.assertEqual(result["timeline"], [])
        self.assertEqual(result["totals"], {"followers": 1, "following": 1, "mutuals": 0})

    def test_diff(self):
        result = track.analyse([
            snapshot("2026-01-01T00:00:00+00:00", ["amy", "ben", "cat"], ["amy", "dan"]),
            snapshot("2026-02-01T00:00:00+00:00", ["amy", "eve"], ["amy", "fay"]),
            snapshot("2026-03-01T00:00:00+00:00", ["amy", "eve", "gus"], ["amy", "fay"]),
        ])

        def names(accounts):
            return [a["username"] for a in accounts]

        first = result["timeline"][-1]
        self.assertEqual(names(first["lost_followers"]), ["ben", "cat"])
        self.assertEqual(names(first["new_followers"]), ["eve"])
        self.assertEqual(names(first["you_unfollowed"]), ["dan"])
        self.assertEqual(names(first["you_followed"]), ["fay"])
        self.assertEqual(result["since_last"], result["timeline"][0])
        self.assertEqual(names(result["since_last"]["new_followers"]), ["gus"])
        self.assertEqual(names(result["not_following_back"]), ["fay"])
        self.assertEqual(names(result["not_followed_back"]), ["eve", "gus"])
        self.assertEqual(result["totals"], {"followers": 3, "following": 2, "mutuals": 1})
        self.assertEqual(result["not_following_back"][0]["href"],
                         "https://www.instagram.com/fay/")

    def test_ignore_is_case_and_at_insensitive(self):
        data = [snapshot("2026-01-01T00:00:00+00:00", [], ["brand", "pal"])]
        with mock.patch.object(track, "IGNORE", ["@Brand"]):
            result = track.analyse(data)
        self.assertEqual([a["username"] for a in result["not_following_back"]], ["pal"])


class IngestTests(TempDirTest):

    def export(self, name, followers, following=("amy",)):
        return write_zip(self.tmp / name, export_files(followers=followers, following=following))

    def test_first_then_change_then_identical(self):
        out = quietly(track.ingest, self.export("one.zip", ["amy", "ben"]))
        self.assertIn("first snapshot", out)

        out = quietly(track.ingest, self.export("two.zip", ["amy", "cat"]))
        self.assertIn("1 unfollowed you, 1 new follower", out)

        out = quietly(track.ingest, self.export("three.zip", ["amy", "cat"]))
        self.assertIn("nothing saved", out)
        self.assertEqual(len(track.load_snapshots()), 2)

    def test_large_drop_is_refused(self):
        everyone = [f"user{n}" for n in range(60)]
        quietly(track.ingest, self.export("full.zip", everyone))
        with self.assertRaises(SystemExit) as caught:
            quietly(track.ingest, self.export("last_year.zip", everyone[:20]))
        self.assertIn("40 of the 60 followers", str(caught.exception))
        self.assertEqual(len(track.load_snapshots()), 1)

        with mock.patch.object(track, "MAX_FOLLOWER_DROP", 1):
            quietly(track.ingest, self.export("last_year.zip", everyone[:20]))
        self.assertEqual(len(track.load_snapshots()), 2)

    def test_small_account_drop_is_accepted(self):
        quietly(track.ingest, self.export("one.zip", ["amy", "ben", "cat", "dan"]))
        quietly(track.ingest, self.export("two.zip", ["amy"]))
        self.assertEqual(len(track.load_snapshots()), 2)

    def test_newest_in_inbox(self):
        older = self.export("instagram-a.zip", ["amy"])
        newer = self.export("instagram-b.zip", ["amy"])
        (self.tmp / "instagram-folder.zip").mkdir()
        stamp = older.stat().st_mtime
        os.utime(newer, (stamp + 60, stamp + 60))
        with mock.patch.object(track, "EXPORT_INBOX", self.tmp):
            self.assertEqual(track.newest_in_inbox(), newer)

    def test_empty_inbox(self):
        with mock.patch.object(track, "EXPORT_INBOX", self.tmp / "missing"), \
                self.assertRaises(SystemExit):
            track.ingest(None)


class ServerTests(TempDirTest):

    def setUp(self):
        super().setUp()
        self.server = track.LocalServer(("127.0.0.1", 0), track.Handler)
        self.port = self.server.server_port
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def get(self, path, host=None):
        connection = HTTPConnection("127.0.0.1", self.port, timeout=5)
        self.addCleanup(connection.close)
        connection.request("GET", path, headers={"Host": host or f"127.0.0.1:{self.port}"})
        response = connection.getresponse()
        return response, response.read()

    def test_data(self):
        track.save_snapshot(snapshot("2026-01-01T00:00:00+00:00", ["amy"], ["ben"]))
        response, body = self.get("/api/data?fresh=1")
        self.assertEqual(response.status, 200)
        self.assertEqual(json.loads(body)["snapshot_count"], 1)

    def test_index(self):
        response, body = self.get("/?tab=timeline", host=f"localhost:{self.port}")
        self.assertEqual(response.status, 200)
        self.assertIn(b"Follower ledger", body)
        self.assertIn("default-src 'none'", response.getheader("Content-Security-Policy"))

    def test_foreign_host_is_refused(self):
        response, _ = self.get("/api/data", host=f"attacker.example:{self.port}")
        self.assertEqual(response.status, 403)

    def test_unknown_path(self):
        response, _ = self.get("/api/database")
        self.assertEqual(response.status, 404)

    def test_analysis_failure_is_a_500(self):
        with mock.patch.object(track, "analyse", side_effect=RuntimeError("boom")), \
                redirect_stderr(StringIO()):
            response, _ = self.get("/api/data")
        self.assertEqual(response.status, 500)

    def test_port_in_use(self):
        with self.assertRaises(OSError):
            track.LocalServer(("127.0.0.1", self.port), track.Handler)


class CommandLineTests(unittest.TestCase):

    def test_help(self):
        self.assertIn("ingest", quietly(track.main, ["--help"]))

    def test_unknown_command(self):
        with self.assertRaises(SystemExit) as caught:
            track.main(["bogus"])
        self.assertIn("track.py ingest", str(caught.exception))


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class ViewerScriptTests(unittest.TestCase):

    def test_script_parses(self):
        page = (ROOT / "index.html").read_text(encoding="utf-8")
        script = re.search(r"<script>(.*?)</script>", page, re.S).group(1)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "viewer.js"
            path.write_text(script, encoding="utf-8")
            result = subprocess.run(["node", "--check", str(path)],
                                    capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
