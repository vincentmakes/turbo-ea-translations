"""judge.py's inspect and merge steps against an in-memory fake of the API."""

import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import gh_api  # noqa: E402
import judge  # noqa: E402
import packrules  # noqa: E402

HEAD = "a" * 40
MAIN = "c" * 40
BASE = "d" * 40
MERGED = "e" * 40


def pack_bytes(code="nl", **kw):
    pack = {
        "format": "turbo-ea-language-pack", "format_version": 1, "app_version": "2.214.0",
        "locale": {"code": code, "name": "Nederlands", "english_name": "Dutch", "rtl": False},
        "mode": "share", "instructions": "x",
        "strings": {"common": {"actions.save": "Opslaan"}},
        "source_hashes": {"common": {"actions.save": "0123456789ab"}},
    }
    pack.update(kw)
    return json.dumps(pack).encode()


class FakeApi:
    """Trees are ``{commit: {path: (mode, type, bytes-or-None)}}``."""

    def __init__(self, *, files, head_tree, main_tree, base_tree=None, pr=None, reviews=(), merge_tree=None):
        self.files = files
        self.trees = {HEAD: head_tree, MAIN: main_tree, BASE: base_tree if base_tree is not None else main_tree,
                      MERGED: merge_tree if merge_tree is not None else head_tree}
        self.pr = {
            "number": 7, "state": "open", "draft": False, "changed_files": len(files),
            "head": {"sha": HEAD}, "base": {"ref": "main"}, "user": {"login": "alice"},
        }
        self.pr.update(pr or {})
        self.reviews = list(reviews)
        self.blobs = {}
        self.calls = []
        self.repo = "o/r"

    def r(self, suffix):
        return "/repos/o/r" + suffix

    def get(self, path, **params):
        if path == "/repos/o/r/pulls/7":
            return self.pr
        if path.startswith("/repos/o/r/compare/"):
            return {"files": self.files, "merge_base_commit": {"sha": BASE}}
        raise AssertionError(path)

    def paginate(self, path, limit=1000, **params):
        assert path == "/repos/o/r/pulls/7/reviews"
        return self.reviews

    def default_branch(self):
        return "main"

    def branch_sha(self, branch):
        return MAIN

    def tree_entry(self, commit, path):
        tree = self.trees[commit]
        if path in tree:
            mode, typ, data = tree[path]
            sha = packrules.git_blob_sha(data) if data is not None else "f" * 40
            if data is not None:
                self.blobs[sha] = data
            return {"mode": mode, "type": typ, "sha": sha, "size": len(data or b"")}
        if any(p.startswith(path + "/") for p in tree):
            return {"mode": "040000", "type": "tree", "sha": "0" * 40}
        return None

    def blob(self, sha):
        return self.blobs[sha]

    def file_text(self, ref, path):
        entry = self.tree_entry(ref, path)
        return self.blobs[entry["sha"]].decode() if entry else None

    def request(self, method, path, **kw):
        self.calls.append((method, path, kw.get("body")))
        if path.endswith("/merge"):
            return {"merged": True, "sha": MERGED}
        return {}

    def put_file(self, path, content, message, branch, sha):
        self.calls.append(("PUT", path, content.decode()))


class Base(unittest.TestCase):
    def run_cmd(self, api, fn, extra_env=None, *args):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        out = Path(self.tmp.name) / "out"
        outputs = Path(self.tmp.name) / "outputs"
        env = {"PR_NUMBER": "7", "HEAD_SHA": HEAD, "GITHUB_OUTPUT": str(outputs),
               "GITHUB_STEP_SUMMARY": str(Path(self.tmp.name) / "summary")}
        env.update(extra_env or {})
        with mock.patch.dict(os.environ, env), mock.patch.object(judge, "client", lambda: api), \
                redirect_stdout(io.StringIO()):
            code = fn(out) if fn is judge.cmd_inspect else fn()
        parsed = {}
        if outputs.exists():
            for line in outputs.read_text().splitlines():
                k, _, v = line.partition("=")
                parsed[k] = v
        return code, parsed, out


class Inspect(Base):
    def inspect(self, api):
        return self.run_cmd(api, judge.cmd_inspect)

    def test_new_pack_is_a_contribution(self):
        data = pack_bytes()
        api = FakeApi(files=[{"filename": "packs/nl/pack.json", "status": "added"}],
                      head_tree={"packs/nl/pack.json": ("100644", "blob", data)}, main_tree={})
        code, outputs, out = self.inspect(api)
        self.assertEqual(code, 0)
        self.assertEqual(outputs["kind"], "contribution")
        self.assertEqual(outputs["code"], "nl")
        self.assertEqual(outputs["app_version"], "2.214.0")
        self.assertEqual(outputs["blob_sha"], packrules.git_blob_sha(data))
        self.assertEqual((out / "pack.json").read_bytes(), data)

    def test_symlink_refused(self):
        api = FakeApi(files=[{"filename": "packs/nl/pack.json", "status": "added"}],
                      head_tree={"packs/nl/pack.json": ("120000", "blob", b"../../etc/passwd")}, main_tree={})
        code, outputs, _ = self.inspect(api)
        self.assertEqual((code, outputs["kind"]), (1, "other"))

    def test_submodule_refused(self):
        api = FakeApi(files=[{"filename": "packs/nl/pack.json", "status": "added"}],
                      head_tree={"packs/nl/pack.json": ("160000", "commit", None)}, main_tree={})
        self.assertEqual(self.inspect(api)[0], 1)

    def test_executable_refused(self):
        api = FakeApi(files=[{"filename": "packs/nl/pack.json", "status": "added"}],
                      head_tree={"packs/nl/pack.json": ("100755", "blob", pack_bytes())}, main_tree={})
        self.assertEqual(self.inspect(api)[0], 1)

    def test_folder_must_match_code(self):
        api = FakeApi(files=[{"filename": "packs/fy/pack.json", "status": "added"}],
                      head_tree={"packs/fy/pack.json": ("100644", "blob", pack_bytes("nl"))}, main_tree={})
        self.assertEqual(self.inspect(api)[0], 1)

    def test_workflow_change_is_left_to_owner(self):
        api = FakeApi(files=[{"filename": ".github/workflows/judge.yml", "status": "modified"}],
                      head_tree={}, main_tree={})
        code, outputs, _ = self.inspect(api)
        self.assertEqual((code, outputs["kind"]), (0, "other"))

    def test_pack_plus_workflow_fails(self):
        api = FakeApi(files=[{"filename": "packs/nl/pack.json", "status": "added"},
                             {"filename": ".github/workflows/judge.yml", "status": "modified"}],
                      head_tree={"packs/nl/pack.json": ("100644", "blob", pack_bytes())}, main_tree={})
        code, outputs, _ = self.inspect(api)
        self.assertEqual((code, outputs["kind"]), (1, "other"))

    def test_moved_head_is_superseded(self):
        api = FakeApi(files=[], head_tree={}, main_tree={}, pr={"head": {"sha": "b" * 40}})
        code, outputs, _ = self.inspect(api)
        self.assertEqual((code, outputs["kind"]), (0, "superseded"))

    def test_other_base_branch_is_not_judged(self):
        api = FakeApi(files=[], head_tree={}, main_tree={}, pr={"base": {"ref": "dev"}})
        self.assertEqual(self.inspect(api)[1]["kind"], "other")

    def test_bad_inputs_rejected(self):
        api = FakeApi(files=[], head_tree={}, main_tree={})
        with self.assertRaises(SystemExit):
            self.run_cmd(api, judge.cmd_inspect, {"PR_NUMBER": "7; rm -rf /"})
        with self.assertRaises(SystemExit):
            self.run_cmd(api, judge.cmd_inspect, {"HEAD_SHA": "main"})


class Merge(Base):
    def merge(self, api, data):
        env = {"CODE": "nl", "BLOB_SHA": packrules.git_blob_sha(data)}
        return self.run_cmd(api, judge.cmd_merge, env)

    def test_new_language_merges_and_records_maintainer(self):
        data = pack_bytes()
        api = FakeApi(files=[], head_tree={"packs/nl/pack.json": ("100644", "blob", data)}, main_tree={})
        code, _, _ = self.merge(api, data)
        self.assertEqual(code, 0)
        methods = [(m, p) for m, p, _ in api.calls]
        self.assertIn(("PUT", "/repos/o/r/pulls/7/merge"), methods)
        merge_body = next(b for m, p, b in api.calls if p.endswith("/merge"))
        self.assertEqual(merge_body["sha"], HEAD)
        self.assertEqual(merge_body["merge_method"], "squash")
        maint = next(b for m, p, b in api.calls if p == "packs/nl/MAINTAINERS")
        self.assertEqual(packrules.parse_maintainers(maint), ["alice"])
        self.assertIn(("POST", "/repos/o/r/actions/workflows/registry.yml/dispatches"), methods)

    def test_existing_language_non_maintainer_waits(self):
        data = pack_bytes()
        old = pack_bytes(instructions="old")
        main = {"packs/nl/pack.json": ("100644", "blob", old), "packs/nl/MAINTAINERS": ("100644", "blob", b"bob\n")}
        api = FakeApi(files=[], head_tree={"packs/nl/pack.json": ("100644", "blob", data)}, main_tree=main)
        self.assertEqual(self.merge(api, data)[0], 0)
        self.assertEqual(api.calls, [])

    def test_existing_language_maintainer_merges_without_new_maintainers(self):
        data = pack_bytes()
        main = {"packs/nl/pack.json": ("100644", "blob", pack_bytes(instructions="old")),
                "packs/nl/MAINTAINERS": ("100644", "blob", b"alice\n")}
        api = FakeApi(files=[], head_tree={"packs/nl/pack.json": ("100644", "blob", data)}, main_tree=main)
        self.assertEqual(self.merge(api, data)[0], 0)
        paths = [p for _, p, _ in api.calls]
        self.assertIn("/repos/o/r/pulls/7/merge", paths)
        self.assertNotIn("packs/nl/MAINTAINERS", paths)

    def test_maintainer_approval_merges(self):
        data = pack_bytes()
        main = {"packs/nl/pack.json": ("100644", "blob", pack_bytes(instructions="old")),
                "packs/nl/MAINTAINERS": ("100644", "blob", b"bob\n")}
        api = FakeApi(files=[], head_tree={"packs/nl/pack.json": ("100644", "blob", data)}, main_tree=main,
                      reviews=[{"user": {"login": "bob"}, "state": "APPROVED", "commit_id": HEAD}])
        self.merge(api, data)
        self.assertIn("/repos/o/r/pulls/7/merge", [p for _, p, _ in api.calls])

    def test_head_moved_since_validation(self):
        data = pack_bytes()
        api = FakeApi(files=[], head_tree={}, main_tree={}, pr={"head": {"sha": "b" * 40}})
        self.assertEqual(self.merge(api, data)[0], 0)
        self.assertEqual(api.calls, [])

    def test_pack_changed_on_main_since_branching(self):
        data = pack_bytes()
        main = {"packs/nl/pack.json": ("100644", "blob", pack_bytes(instructions="newer")),
                "packs/nl/MAINTAINERS": ("100644", "blob", b"alice\n")}
        base = {"packs/nl/pack.json": ("100644", "blob", pack_bytes(instructions="older")),
                "packs/nl/MAINTAINERS": ("100644", "blob", b"alice\n")}
        api = FakeApi(files=[], head_tree={"packs/nl/pack.json": ("100644", "blob", data)},
                      main_tree=main, base_tree=base)
        self.assertEqual(self.merge(api, data)[0], 0)
        self.assertEqual(api.calls, [])

    def test_draft_is_not_merged(self):
        data = pack_bytes()
        api = FakeApi(files=[], head_tree={}, main_tree={}, pr={"draft": True})
        self.merge(api, data)
        self.assertEqual(api.calls, [])

    def test_post_merge_mismatch_fails_loudly(self):
        data = pack_bytes()
        api = FakeApi(files=[], head_tree={"packs/nl/pack.json": ("100644", "blob", data)}, main_tree={},
                      merge_tree={"packs/nl/pack.json": ("100644", "blob", pack_bytes(instructions="other"))})
        self.assertEqual(self.merge(api, data)[0], 1)

    def test_merge_refused_by_github(self):
        data = pack_bytes()
        api = FakeApi(files=[], head_tree={"packs/nl/pack.json": ("100644", "blob", data)}, main_tree={})

        def refuse(method, path, **kw):
            raise gh_api.ApiError(409, "Head branch was modified")

        api.request = refuse
        self.assertEqual(self.merge(api, data)[0], 1)


class Outputs(unittest.TestCase):
    def test_output_refuses_newlines(self):
        with self.assertRaises(ValueError):
            judge.set_output("code", "nl\nkind=contribution")


if __name__ == "__main__":
    unittest.main()
