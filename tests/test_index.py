import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import build_index  # noqa: E402
import gh_api  # noqa: E402
import packrules  # noqa: E402
import turbo_ea  # noqa: E402

ENGLISH = {"common": {"actions.save": "Save", "actions.cancel": "Cancel"}}


def write_pack(root: Path, code: str, **kw) -> bytes:
    pack = {
        "format": "turbo-ea-language-pack", "format_version": 1, "app_version": "2.214.0",
        "locale": {"code": code, "name": "Nederlands", "english_name": "Dutch", "rtl": False},
        "mode": "share", "instructions": "x",
        "strings": {"common": {"actions.save": "Opslaan"}, "metamodel": {"type.Application.label": "Applicatie"}},
        "source_hashes": {"common": {"actions.save": packrules.source_hash("Save")},
                          "metamodel": {"type.Application.label": "5c1e0a9d7b32"}},
    }
    pack.update(kw)
    folder = root / "packs" / code
    folder.mkdir(parents=True, exist_ok=True)
    data = json.dumps(pack, ensure_ascii=False).encode()
    (folder / "pack.json").write_bytes(data)
    return data


class BuildIndex(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.logs = []

    def build(self):
        return build_index.build_index(self.root, lambda v: ("v" + v, ENGLISH), self.logs.append)

    def test_entries(self):
        data = write_pack(self.root, "nl")
        write_pack(self.root, "fy", locale={"code": "fy", "name": "Frysk", "english_name": "Frisian", "rtl": False})
        index = self.build()
        self.assertEqual([e["code"] for e in index["languages"]], ["fy", "nl"])
        nl = index["languages"][1]
        self.assertEqual(nl["sha256"], __import__("hashlib").sha256(data).hexdigest())
        self.assertEqual(nl["app_version"], "2.214.0")
        self.assertEqual((nl["strings"], nl["ui_strings"], nl["metamodel_strings"]), (2, 1, 1))
        self.assertEqual((nl["english_strings"], nl["translated"], nl["current"], nl["coverage"]), (2, 1, 1, 50.0))
        self.assertEqual(nl["compared_with"], "v2.214.0")
        self.assertEqual(nl["path"], "packs/nl/pack.json")

    def test_invalid_entries_are_skipped(self):
        write_pack(self.root, "nl", mode="current")
        write_pack(self.root, "fy", locale={"code": "nl", "name": "N", "english_name": "Dutch", "rtl": False})
        (self.root / "packs" / "NOTACODE").mkdir()
        (self.root / "packs" / "README.md").write_text("x")
        self.assertEqual(self.build()["languages"], [])
        self.assertEqual(len(self.logs), 2)

    def test_symlinked_pack_is_skipped(self):
        target = self.root / "elsewhere.json"
        target.write_bytes(write_pack(self.root, "nl"))
        (self.root / "packs" / "nl" / "pack.json").unlink()
        os.symlink(target, self.root / "packs" / "nl" / "pack.json")
        self.assertEqual(self.build()["languages"], [])

    def test_no_english_still_lists(self):
        write_pack(self.root, "nl")
        index = build_index.build_index(self.root, lambda v: None, self.logs.append)
        self.assertNotIn("coverage", index["languages"][0])
        self.assertEqual(index["languages"][0]["strings"], 2)

    def test_render_is_stable(self):
        write_pack(self.root, "nl")
        self.assertEqual(build_index.render(self.build()), build_index.render(self.build()))

    def test_last_update_from_git(self):
        write_pack(self.root, "nl")
        env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.invalid",
                   GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.invalid",
                   GIT_COMMITTER_DATE="2026-01-02T05:04:05+02:00", GIT_CONFIG_GLOBAL=os.devnull)
        for args in (["init", "-q"], ["add", "."], ["commit", "-qm", "x"]):
            subprocess.run(["git", *args], cwd=self.root, env=env, check=True)
        entry = self.build()["languages"][0]
        self.assertEqual(entry["updated"], "2026-01-02T03:04:05Z")
        self.assertRegex(entry["commit"], r"^[0-9a-f]{40}$")


class LsRemote(unittest.TestCase):
    def test_peeled_tag_wins_and_order(self):
        out = (
            f"{'1' * 40}\trefs/heads/main\n"
            f"{'2' * 40}\trefs/tags/v2.214.0\n"
            f"{'3' * 40}\trefs/tags/v2.214.0^{{}}\n"
            "garbage line\n"
        )
        refs = turbo_ea.parse_ls_remote(out)
        self.assertEqual(turbo_ea.pick_ref("2.214.0", refs), ("v2.214.0", "3" * 40))
        self.assertEqual(turbo_ea.pick_ref("2.215.0", refs), ("main", "1" * 40))

    def test_latest_release_picks_highest_version(self):
        out = (
            f"{'1' * 40}\trefs/tags/v2.9.0\n"
            f"{'2' * 40}\trefs/tags/v2.10.0\n"
            f"{'3' * 40}\trefs/tags/v2.10.0^{{}}\n"
            f"{'4' * 40}\trefs/tags/v9.0.0-rc1\n"
            f"{'5' * 40}\trefs/tags/vlatest\n"
        )
        self.assertEqual(turbo_ea.latest_release(out), ("v2.10.0", "3" * 40))
        self.assertIsNone(turbo_ea.latest_release(""))

    def test_bad_version_never_resolves(self):
        with self.assertRaises(ValueError):
            turbo_ea.pick_ref("main", {})
        with self.assertRaises(RuntimeError):
            turbo_ea.resolve("2.214.0", pinned_sha="main")


if __name__ == "__main__":
    unittest.main()


class Prepare(unittest.TestCase):
    """turbo_ea.prepare: where the validator and the English text come from."""

    TAGS = (f"{'1' * 40}\trefs/tags/v2.213.0\n{'2' * 40}\trefs/tags/v2.300.0\n")
    MAIN = f"{'9' * 40}\trefs/heads/main\n"

    def run_prepare(self, version, has_validator, pinned=""):
        def git(*args, cwd=None):
            assert args[0] == "ls-remote" and args[1] in ("--tags", turbo_ea.REPO_URL)
            if args[1] == "--tags":
                return self.TAGS
            wanted = set(args[2:])
            lines = self.TAGS + self.MAIN
            return "".join(line + "\n" for line in lines.splitlines() if line.split("\t")[1] in wanted)

        def fetch(sha, dest, need_validator=True):
            english = dest / turbo_ea.ENGLISH_PATH
            english.mkdir(parents=True)
            (english / "common.json").write_text("{}")
            if sha in has_validator:
                (dest / turbo_ea.VALIDATOR_PATH).parent.mkdir(parents=True)
                (dest / turbo_ea.VALIDATOR_PATH).write_text("")
            return dest / turbo_ea.VALIDATOR_PATH, english

        with tempfile.TemporaryDirectory() as work, mock.patch.object(turbo_ea, "_git", git), \
                mock.patch.object(turbo_ea, "fetch", fetch):
            got = turbo_ea.prepare(version, Path(work), pinned)
            return {k: v for k, v in got.items() if k.endswith(("label", "sha"))}

    def test_validator_from_latest_release_english_from_pack_version(self):
        got = self.run_prepare("2.213.0", has_validator={"2" * 40})
        self.assertEqual(got, {"validator_label": "v2.300.0", "validator_sha": "2" * 40,
                               "english_label": "v2.213.0", "english_sha": "1" * 40})

    def test_old_validator_is_never_used_even_if_the_pack_names_it(self):
        # 2.213.0 has a (hypothetical, more lenient) validator; the newest release wins.
        got = self.run_prepare("2.213.0", has_validator={"1" * 40, "2" * 40})
        self.assertEqual(got["validator_sha"], "2" * 40)

    def test_falls_back_to_main_then_fails_closed(self):
        got = self.run_prepare("2.301.0", has_validator={"9" * 40})
        self.assertEqual((got["validator_label"], got["english_label"]), ("main", "main"))
        with self.assertRaisesRegex(RuntimeError, "not released yet"):
            self.run_prepare("2.301.0", has_validator=set())

    def test_untagged_old_version_refused(self):
        with self.assertRaisesRegex(RuntimeError, "not newer than"):
            self.run_prepare("2.250.0", has_validator={"2" * 40})

    def test_pinned_sha(self):
        got = self.run_prepare("2.213.0", has_validator={"7" * 40}, pinned="7" * 40)
        self.assertEqual({got["validator_sha"], got["english_sha"]}, {"7" * 40})
        with self.assertRaises(RuntimeError):
            self.run_prepare("2.213.0", has_validator=set(), pinned="main")


class Publish(unittest.TestCase):
    def test_publishes_only_on_change(self):
        content = b'{"languages": []}\n'
        api = mock.Mock()
        api.default_branch.return_value = "main"
        api.branch_sha.return_value = "c" * 40
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "index.json"
            f.write_bytes(content)
            api.tree_entry.return_value = {"sha": packrules.git_blob_sha(content)}
            with mock.patch("gh_api.Client", return_value=api), redirect_stdout(io.StringIO()):
                build_index.cmd_publish(f)
            api.put_file.assert_not_called()
            api.tree_entry.return_value = {"sha": "0" * 40}
            with mock.patch("gh_api.Client", return_value=api), redirect_stdout(io.StringIO()):
                build_index.cmd_publish(f)
            api.put_file.assert_called_once_with("index.json", content, "Update index.json", "main", "0" * 40)


class Api(unittest.TestCase):
    def test_cross_host_redirect_refused(self):
        import urllib.error
        import urllib.request

        handler = gh_api._SameHostRedirects()
        req = urllib.request.Request("https://api.github.com/x", headers={"Authorization": "Bearer t"})
        with self.assertRaises(urllib.error.HTTPError):
            handler.redirect_request(req, None, 302, "Found", {}, "https://evil.example/x")
        same = handler.redirect_request(req, None, 302, "Found", {}, "https://api.github.com/y")
        self.assertEqual(same.full_url, "https://api.github.com/y")

    def test_paginate_complete_reports_truncation(self):
        client = gh_api.Client(token="", repo="o/r")
        pages = {1: [{}] * 100, 2: [{}] * 100, 3: [{}] * 5}
        client.get = lambda path, per_page, page, **kw: pages.get(page, [])
        self.assertEqual(len(client.paginate_complete("/x", limit=1000)[0]), 205)
        self.assertTrue(client.paginate_complete("/x", limit=1000)[1])
        items, complete = client.paginate_complete("/x", limit=150)
        self.assertEqual((len(items), complete), (150, False))

    def test_writes_are_never_retried(self):
        client = gh_api.Client(token="", repo="o/r")
        calls = []

        class Opener:
            def open(self, req, timeout):
                calls.append(req.get_method())
                raise urllib.error.HTTPError(req.full_url, 503, "busy", {}, io.BytesIO(b""))

        import urllib.error

        with mock.patch.object(gh_api, "_OPENER", Opener()), mock.patch("time.sleep"):
            with self.assertRaises(gh_api.ApiError):
                client.request("PUT", "/x", body={})
            with self.assertRaises(gh_api.ApiError):
                client.request("GET", "/x")
        self.assertEqual(calls, ["PUT", "GET", "GET", "GET"])
