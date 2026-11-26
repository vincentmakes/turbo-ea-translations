"""judge.py's resolve, inspect, validate and merge steps against an in-memory
fake of the GitHub API that models commits as real trees."""

import hashlib
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
RESERVED = "f" * 40
OWNER_ID = 99


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


def blob(data, mode="100644", typ="blob"):
    return (mode, typ, data)


BASE_FILES = {"README.md": blob(b"readme"), "packs/README.md": blob(b"packs")}


class FakeApi:
    """``trees`` maps a commit to ``{path: (mode, type, bytes-or-None)}``."""

    def __init__(self, *, files, head, main, base=None, pr=None, reviews=(), merged=None,
                 commits=None, search_count=0, language_commits=0, owner_type="User"):
        self.files = files
        self.trees = {HEAD: {**BASE_FILES, **head}, MAIN: {**BASE_FILES, **main},
                      BASE: {**BASE_FILES, **(main if base is None else base)}}
        self.trees[MERGED] = merged if merged is not None else {**self.trees[MAIN], **head}
        self.parents = {MERGED: [MAIN]}
        self.pr = {
            "number": 7, "state": "open", "draft": False, "changed_files": len(files),
            "head": {"sha": HEAD, "repo": {"owner": {"login": "alice"}}}, "base": {"ref": "main"},
            "user": {"login": "alice", "id": 1},
        }
        self.pr.update(pr or {})
        self.reviews = list(reviews)
        self.reviews_complete = True
        self.commits = commits if commits is not None else [{"sha": HEAD, "parents": [{"sha": BASE}]}]
        self.search_count = search_count
        self.language_commits = language_commits
        self.owner_type = owner_type
        self.main_sha = MAIN
        self.blobs = {}
        self.blob_calls = 0
        self.calls = []
        self.repo = "o/r"

    # -- helpers --------------------------------------------------------
    def r(self, suffix):
        return "/repos/o/r" + suffix

    def _sha(self, data):
        sha = packrules.git_blob_sha(data)
        self.blobs[sha] = data
        return sha

    def tree_entry(self, commit, path):
        tree = self.trees[commit]
        if path in tree:
            mode, typ, data = tree[path]
            sha = self._sha(data) if data is not None else "9" * 40
            return {"path": path.rsplit("/", 1)[-1], "mode": mode, "type": typ, "sha": sha,
                    "size": len(data or b"")}
        prefix = path + "/"
        inner = sorted((p, v) for p, v in tree.items() if p.startswith(prefix))
        if inner:
            digest = hashlib.sha1(repr(inner).encode()).hexdigest()
            return {"path": path.rsplit("/", 1)[-1], "mode": "040000", "type": "tree", "sha": digest}
        return None

    def tree_listing(self, commit, path=""):
        prefix = path + "/" if path else ""
        names = {p[len(prefix):].split("/", 1)[0] for p in self.trees[commit] if p.startswith(prefix)}
        if path and not names:
            return None
        return [self.tree_entry(commit, prefix + n) for n in sorted(names)]

    def commit_parents(self, sha):
        return self.parents.get(sha, [])

    # -- API surface used by judge.py -----------------------------------
    def get(self, path, **params):
        if path == "/repos/o/r/pulls/7":
            return self.pr
        if path == "/repos/o/r":
            return {"default_branch": "main", "owner": {"login": "o", "id": OWNER_ID, "type": self.owner_type}}
        if path.startswith("/repos/o/r/compare/"):
            return {"files": self.files, "merge_base_commit": {"sha": BASE},
                    "total_commits": len(self.commits), "commits": self.commits}
        if path == "/search/issues":
            return {"total_count": self.search_count}
        raise AssertionError(path)


    def paginate_complete(self, path, limit=1000, **params):
        assert path == "/repos/o/r/pulls/7/reviews"
        return self.reviews, self.reviews_complete

    def default_branch(self):
        return "main"

    def branch_sha(self, branch):
        return self.main_sha

    def blob(self, sha):
        self.blob_calls += 1
        return self.blobs[sha]

    def file_text(self, ref, path):
        entry = self.tree_entry(ref, path)
        return self.blobs[entry["sha"]].decode() if entry else None

    def request(self, method, path, **kw):
        if method == "GET" and path == "/repos/o/r/commits":
            assert kw["params"]["path"] == "packs/nl/pack.json"
            return [{}] * self.language_commits
        self.calls.append((method, path, kw.get("body")))
        if path.endswith("/merge"):
            return {"merged": True, "sha": MERGED}
        return {}

    def put_file(self, path, content, message, branch, sha):
        self.calls.append(("PUT", path, content.decode()))
        # The reservation becomes the new tip of main, and the merge's parent.
        self.trees[RESERVED] = {**self.trees[MAIN], path: blob(content)}
        self.trees[MERGED] = {**self.trees[MERGED], path: blob(content)}
        self.parents[MERGED] = [RESERVED]
        self.main_sha = RESERVED


class Base(unittest.TestCase):
    def run_cmd(self, api, fn, extra_env=None):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        out = Path(self.tmp.name) / "out"
        outputs = Path(self.tmp.name) / "outputs"
        self.summary_file = Path(self.tmp.name) / "summary"
        env = {"PR_NUMBER": "7", "HEAD_SHA": HEAD, "GITHUB_OUTPUT": str(outputs),
               "GITHUB_STEP_SUMMARY": str(self.summary_file)}
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

    def summary_text(self):
        return self.summary_file.read_text() if self.summary_file.exists() else ""


NEW = [{"filename": "packs/nl/pack.json", "status": "added"}]


class Inspect(Base):
    def inspect(self, api):
        return self.run_cmd(api, judge.cmd_inspect)

    def test_new_pack_is_a_contribution(self):
        data = pack_bytes()
        api = FakeApi(files=NEW, head={"packs/nl/pack.json": blob(data)}, main={})
        code, outputs, out = self.inspect(api)
        self.assertEqual(code, 0)
        self.assertEqual(outputs["kind"], "contribution")
        self.assertEqual(outputs["code"], "nl")
        self.assertEqual(outputs["app_version"], "2.214.0")
        self.assertEqual(outputs["blob_sha"], packrules.git_blob_sha(data))
        self.assertEqual((out / "pack.json").read_bytes(), data)

    def test_symlink_refused(self):
        api = FakeApi(files=NEW, head={"packs/nl/pack.json": blob(b"../../etc/passwd", "120000")}, main={})
        code, outputs, _ = self.inspect(api)
        self.assertEqual((code, outputs["kind"]), (1, "other"))

    def test_submodule_refused(self):
        api = FakeApi(files=NEW, head={"packs/nl/pack.json": ("160000", "commit", None)}, main={})
        self.assertEqual(self.inspect(api)[0], 1)

    def test_executable_refused(self):
        api = FakeApi(files=NEW, head={"packs/nl/pack.json": blob(pack_bytes(), "100755")}, main={})
        self.assertEqual(self.inspect(api)[0], 1)

    def test_size_cap_before_download(self):
        api = FakeApi(files=NEW, head={"packs/nl/pack.json": blob(b" " * (packrules.MAX_PACK_BYTES + 1))},
                      main={})
        self.assertEqual(self.inspect(api)[0], 1)
        self.assertEqual(api.blob_calls, 0)

    def test_blob_id_mismatch_refused(self):
        api = FakeApi(files=NEW, head={"packs/nl/pack.json": blob(pack_bytes())}, main={})
        real_blob = api.blob
        api.blob = lambda sha: real_blob(sha) + b" "
        self.assertEqual(self.inspect(api)[0], 1)

    def test_merge_commit_in_pr_refused(self):
        commits = [{"sha": HEAD, "parents": [{"sha": BASE}, {"sha": "1" * 40}]}]
        api = FakeApi(files=NEW, head={"packs/nl/pack.json": blob(pack_bytes())}, main={}, commits=commits)
        self.assertEqual(self.inspect(api)[0], 1)
        self.assertIn("merge commit", self.summary_text())

    def test_folder_must_match_code(self):
        api = FakeApi(files=[{"filename": "packs/fy/pack.json", "status": "added"}],
                      head={"packs/fy/pack.json": blob(pack_bytes("nl"))}, main={})
        self.assertEqual(self.inspect(api)[0], 1)

    def test_workflow_change_is_left_to_owner(self):
        api = FakeApi(files=[{"filename": ".github/workflows/judge.yml", "status": "modified"}], head={}, main={})
        code, outputs, _ = self.inspect(api)
        self.assertEqual((code, outputs["kind"]), (0, "other"))

    def test_maintainers_change_is_left_to_owner(self):
        api = FakeApi(files=[{"filename": "packs/nl/MAINTAINERS", "status": "modified"}], head={}, main={})
        code, outputs, _ = self.inspect(api)
        self.assertEqual((code, outputs["kind"]), (0, "other"))

    def test_pack_plus_workflow_fails(self):
        api = FakeApi(files=NEW + [{"filename": ".github/workflows/judge.yml", "status": "modified"}],
                      head={"packs/nl/pack.json": blob(pack_bytes())}, main={})
        code, outputs, _ = self.inspect(api)
        self.assertEqual((code, outputs["kind"]), (1, "other"))

    def test_pack_controlled_text_is_not_live_markdown(self):
        data = pack_bytes(**{"[x](https://evil.example)": 1})
        api = FakeApi(files=NEW, head={"packs/nl/pack.json": blob(data)}, main={})
        self.assertEqual(self.inspect(api)[0], 1)
        text = self.summary_text()
        self.assertIn("<pre>", text)
        self.assertNotRegex(text.split("<pre>")[0], r"\]\(")

    def test_moved_head_is_superseded(self):
        api = FakeApi(files=[], head={}, main={}, pr={"head": {"sha": "b" * 40}})
        code, outputs, _ = self.inspect(api)
        self.assertEqual((code, outputs["kind"]), (0, "superseded"))

    def test_other_base_branch_is_not_judged(self):
        api = FakeApi(files=[], head={}, main={}, pr={"base": {"ref": "dev"}})
        self.assertEqual(self.inspect(api)[1]["kind"], "other")

    def test_bad_inputs_rejected(self):
        api = FakeApi(files=[], head={}, main={})
        with self.assertRaises(SystemExit):
            self.run_cmd(api, judge.cmd_inspect, {"PR_NUMBER": "7; rm -rf /"})
        with self.assertRaises(SystemExit):
            self.run_cmd(api, judge.cmd_inspect, {"HEAD_SHA": "main"})


EXISTING = {"packs/nl/pack.json": blob(pack_bytes(instructions="old"))}


class Merge(Base):
    def merge(self, api, data):
        env = {"CODE": "nl", "BLOB_SHA": packrules.git_blob_sha(data)}
        return self.run_cmd(api, judge.cmd_merge, env)

    def paths(self, api):
        return [p for _, p, _ in api.calls]

    def update(self, maintainers=b"bob 2\n", **kw):
        data = pack_bytes()
        main = {**EXISTING, "packs/nl/MAINTAINERS": blob(maintainers)}
        files = [{"filename": "packs/nl/pack.json", "status": "modified"}]
        api = FakeApi(files=files, head={"packs/nl/pack.json": blob(data)}, main=main, **kw)
        return api, data

    def test_new_language_reserves_maintainer_then_merges(self):
        data = pack_bytes()
        api = FakeApi(files=NEW, head={"packs/nl/pack.json": blob(data)}, main={})
        code, _, _ = self.merge(api, data)
        self.assertEqual(code, 0, self.summary_text())
        self.assertEqual(self.paths(api)[:2], ["packs/nl/MAINTAINERS", "/repos/o/r/pulls/7/merge"])
        maint = api.calls[0][2]
        self.assertEqual(packrules.parse_maintainers(maint), ([packrules.Maintainer("alice", 1)], 0))
        merge_body = api.calls[1][2]
        self.assertEqual((merge_body["sha"], merge_body["merge_method"]), (HEAD, "squash"))
        self.assertTrue(merge_body["commit_message"])
        self.assertIn("/repos/o/r/actions/workflows/registry.yml/dispatches", self.paths(api))

    def test_existing_language_non_maintainer_waits(self):
        api, data = self.update()
        self.assertEqual(self.merge(api, data)[0], 0)
        self.assertEqual(api.calls, [])
        self.assertIn("bob", self.summary_text())  # names the maintainers to ask

    def test_existing_language_maintainer_merges_without_new_maintainers(self):
        api, data = self.update(maintainers=b"alice 1\n")
        self.assertEqual(self.merge(api, data)[0], 0, self.summary_text())
        self.assertIn("/repos/o/r/pulls/7/merge", self.paths(api))
        self.assertNotIn("packs/nl/MAINTAINERS", self.paths(api))

    def test_renamed_maintainer_keeps_role_reused_login_does_not(self):
        api, data = self.update(maintainers=b"oldname 1\n")
        self.merge(api, data)
        self.assertIn("/repos/o/r/pulls/7/merge", self.paths(api))
        api, data = self.update(maintainers=b"alice 555\n")
        self.merge(api, data)
        self.assertEqual(api.calls, [])

    def test_maintainer_approval_merges(self):
        api, data = self.update(reviews=[{"user": {"login": "bob", "id": 2}, "state": "APPROVED",
                                          "commit_id": HEAD}])
        self.merge(api, data)
        self.assertIn("/repos/o/r/pulls/7/merge", self.paths(api))

    def test_owner_approval_merges_language_without_maintainers(self):
        api, data = self.update(maintainers=b"", reviews=[
            {"user": {"login": "o", "id": OWNER_ID}, "state": "APPROVED", "commit_id": HEAD}])
        self.merge(api, data)
        self.assertIn("/repos/o/r/pulls/7/merge", self.paths(api))

    def test_owner_of_an_organisation_is_not_a_user_id(self):
        api, data = self.update(maintainers=b"", owner_type="Organization", reviews=[
            {"user": {"login": "o", "id": OWNER_ID}, "state": "APPROVED", "commit_id": HEAD}])
        self.merge(api, data)
        self.assertEqual(api.calls, [])

    def test_incomplete_review_list_waits(self):
        api, data = self.update(maintainers=b"bob 2\n", reviews=[
            {"user": {"login": "bob", "id": 2}, "state": "APPROVED", "commit_id": HEAD}])
        api.reviews_complete = False
        self.merge(api, data)
        self.assertEqual(api.calls, [])

    def test_daily_limits(self):
        api, data = self.update(maintainers=b"alice 1\n", search_count=packrules.MAX_MERGES_PER_AUTHOR_PER_DAY)
        self.merge(api, data)
        self.assertEqual(api.calls, [])
        api, data = self.update(maintainers=b"alice 1\n",
                                language_commits=packrules.MAX_MERGES_PER_LANGUAGE_PER_DAY)
        self.merge(api, data)
        self.assertEqual(api.calls, [])

    def test_daily_limit_check_fails_closed(self):
        api, data = self.update(maintainers=b"alice 1\n")

        def broken(path, **params):
            if path == "/search/issues":
                raise gh_api.ApiError(503, "unavailable")
            return FakeApi.get(api, path, **params)

        api.get = broken
        self.merge(api, data)
        self.assertEqual(api.calls, [])

    def test_head_moved_since_validation(self):
        data = pack_bytes()
        api = FakeApi(files=[], head={}, main={}, pr={"head": {"sha": "b" * 40}})
        self.assertEqual(self.merge(api, data)[0], 0)
        self.assertEqual(api.calls, [])

    def test_content_rechecked_at_merge_time(self):
        api, data = self.update(maintainers=b"alice 1\n")
        api.files = api.files + [{"filename": "scripts/judge.py", "status": "modified"}]
        self.assertEqual(self.merge(api, data)[0], 1)
        self.assertEqual(api.calls, [])

    def test_blob_must_be_the_validated_one(self):
        api, _ = self.update(maintainers=b"alice 1\n")
        self.assertEqual(self.merge(api, pack_bytes(instructions="something else"))[0], 1)
        self.assertEqual(api.calls, [])

    def test_merge_commit_refused_at_merge_time(self):
        api, data = self.update(maintainers=b"alice 1\n",
                                commits=[{"sha": HEAD, "parents": [{"sha": BASE}, {"sha": MAIN}]}])
        self.assertEqual(self.merge(api, data)[0], 1)
        self.assertEqual(api.calls, [])

    def test_pack_changed_on_main_since_branching(self):
        data = pack_bytes()
        main = {"packs/nl/pack.json": blob(pack_bytes(instructions="newer")),
                "packs/nl/MAINTAINERS": blob(b"alice 1\n")}
        base = {"packs/nl/pack.json": blob(pack_bytes(instructions="older")),
                "packs/nl/MAINTAINERS": blob(b"alice 1\n")}
        api = FakeApi(files=[{"filename": "packs/nl/pack.json", "status": "modified"}],
                      head={"packs/nl/pack.json": blob(data)}, main=main, base=base)
        self.assertEqual(self.merge(api, data)[0], 0)
        self.assertEqual(api.calls, [])

    def test_draft_is_not_merged(self):
        data = pack_bytes()
        api = FakeApi(files=NEW, head={"packs/nl/pack.json": blob(data)}, main={}, pr={"draft": True})
        self.merge(api, data)
        self.assertEqual(api.calls, [])

    def test_post_merge_extra_file_fails_loudly(self):
        api, data = self.update(maintainers=b"alice 1\n")
        api.trees[MERGED] = {**api.trees[MERGED], "scripts/judge.py": blob(b"reverted")}
        self.assertEqual(self.merge(api, data)[0], 1)
        self.assertIn("Post-merge check failed", self.summary_text())
        self.assertNotIn("/repos/o/r/actions/workflows/registry.yml/dispatches", self.paths(api))

    def test_post_merge_other_pack_deleted_fails(self):
        api, data = self.update(maintainers=b"alice 1\n")
        api.trees[MAIN]["packs/fy/pack.json"] = blob(b"{}")
        self.assertEqual(self.merge(api, data)[0], 1)  # fy missing from the merge commit

    def test_post_merge_wrong_parent_fails(self):
        api, data = self.update(maintainers=b"alice 1\n")
        api.parents[MERGED] = ["1" * 40]
        self.assertEqual(self.merge(api, data)[0], 1)

    def test_merge_refused_by_github(self):
        api, data = self.update(maintainers=b"alice 1\n")

        real = api.request

        def refuse(method, path, **kw):
            if path.endswith("/merge"):
                raise gh_api.ApiError(409, "Head branch was modified")
            return real(method, path, **kw)

        api.request = refuse
        self.assertEqual(self.merge(api, data)[0], 1)

    def test_reservation_failure_blocks_merge(self):
        data = pack_bytes()
        api = FakeApi(files=NEW, head={"packs/nl/pack.json": blob(data)}, main={})

        def refuse(*args):
            raise gh_api.ApiError(409, "protected branch")

        api.put_file = refuse
        self.assertEqual(self.merge(api, data)[0], 1)
        self.assertNotIn("/repos/o/r/pulls/7/merge", self.paths(api))


class Resolve(Base):
    def resolve(self, api, **env):
        return self.run_cmd(api, judge.cmd_resolve, env)

    def test_workflow_run_uses_commit_lookup(self):
        api = FakeApi(files=[], head={}, main={})
        listing = [{"number": 7, "state": "open", "draft": False, "head": {"sha": HEAD}, "base": {"ref": "main"}},
                   {"number": 8, "state": "open", "draft": False, "head": {"sha": "b" * 40},
                    "base": {"ref": "main"}}]
        api.get = lambda path, **p: listing if path == f"/repos/o/r/commits/{HEAD}/pulls" else FakeApi.get(api, path)
        _, outputs, _ = self.resolve(api, EVENT_NAME="workflow_run", RUN_HEAD_SHA=HEAD)
        self.assertEqual(json.loads(outputs["prs"]), [{"number": "7", "head_sha": HEAD}])

    def test_workflow_run_needs_a_sha(self):
        api = FakeApi(files=[], head={}, main={})
        self.assertEqual(self.resolve(api, EVENT_NAME="workflow_run", RUN_HEAD_SHA="$(id)")[0], 1)

    def test_dispatch_validates_number(self):
        api = FakeApi(files=[], head={}, main={})
        self.assertEqual(self.resolve(api, EVENT_NAME="workflow_dispatch", PR_NUMBER="7 && x")[0], 1)
        _, outputs, _ = self.resolve(api, EVENT_NAME="workflow_dispatch", PR_NUMBER="7")
        self.assertEqual(json.loads(outputs["prs"]), [{"number": "7", "head_sha": HEAD}])

    def test_sweep_window_rotates_over_everything(self):
        numbers = list(range(1, 251))
        seen = set()
        for day in range(3):
            window = judge.sweep_window(numbers, day, size=100)
            self.assertEqual(len(window), 100)
            seen |= set(window)
        self.assertEqual(seen, set(numbers))
        self.assertEqual(judge.sweep_window([3, 1, 2], 5, size=100), [1, 2, 3])


class Validate(Base):
    def validate(self, code):
        got = {"validator": Path("v.py"), "english": Path("en"), "validator_label": "v2.300.0",
               "validator_sha": "1" * 40, "english_label": "v2.214.0", "english_sha": "2" * 40}
        with mock.patch("turbo_ea.prepare", return_value=got), \
                mock.patch("turbo_ea.run_validator", return_value=(code, '"admin"\t"k"\tempty\n')):
            return self.run_cmd(None, lambda: judge.cmd_validate(Path("p.json"), Path("w")),
                                {"APP_VERSION": "2.214.0"})[0]

    def test_exit_codes(self):
        self.assertEqual(self.validate(0), 0)
        self.assertEqual(self.validate(1), 1)
        self.assertIn("invalid", self.summary_text())
        self.assertEqual(self.validate(2), 1)
        self.assertIn("could not run", self.summary_text())
        self.assertEqual(self.validate(-9), 1)

    def test_bad_version_never_fetches(self):
        with mock.patch("turbo_ea.prepare") as prepare:
            code = self.run_cmd(None, lambda: judge.cmd_validate(Path("p"), Path("w")),
                                {"APP_VERSION": "main"})[0]
        self.assertEqual(code, 1)
        prepare.assert_not_called()


class Outputs(unittest.TestCase):
    def test_output_refuses_newlines(self):
        with self.assertRaises(ValueError):
            judge.set_output("code", "nl\nkind=contribution")


if __name__ == "__main__":
    unittest.main()
