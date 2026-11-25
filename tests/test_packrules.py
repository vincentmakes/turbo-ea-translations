import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import packrules  # noqa: E402

HEAD = "a" * 40


def good_pack(**overrides):
    pack = {
        "format": "turbo-ea-language-pack",
        "format_version": 1,
        "app_version": "2.214.0",
        "locale": {"code": "nl", "name": "Nederlands", "english_name": "Dutch", "rtl": False},
        "mode": "share",
        "instructions": "Translate …",
        "strings": {"common": {"actions.save": "Opslaan"}},
        "source_hashes": {"common": {"actions.save": "0123456789ab"}},
    }
    pack.update(overrides)
    return pack


class CodeAndVersion(unittest.TestCase):
    def test_codes(self):
        for code in ("nl", "pt-BR", "zh-Hant", "zh-Hant-TW", "fil"):
            self.assertIsNone(packrules.code_problem(code), code)
        for code in ("NL", "n", "nl-br", "nl_BR", "nl\n", "../x", "nl/x", "", None, 5, "abcd", "zh-Hant-TWX"):
            self.assertEqual(packrules.code_problem(code), "invalid_code", repr(code))
        for code in ("en", "de", "fr", "es", "it", "pt", "zh", "ru", "da", "ar"):
            self.assertEqual(packrules.code_problem(code), "built_in_code")
        self.assertEqual(packrules.code_problem("iw"), "invalid_code")

    def test_versions(self):
        for good in ("2.214.0", "0.0.0", "10.0.12"):
            self.assertTrue(packrules.is_version(good))
        for bad in ("v2.214.0", "2.214", "2.214.0-rc1", "02.1.0", "2.214.0\n", " 2.214.0", "1.2.3;rm -rf /",
                    "../../main", "2.214.0/../x", "", None, 2):
            self.assertFalse(packrules.is_version(bad), repr(bad))

    def test_refs_only_from_strict_version(self):
        self.assertEqual(
            packrules.turbo_ea_refs("2.214.0"),
            ["refs/tags/v2.214.0", "refs/tags/2.214.0", "refs/heads/main"],
        )
        with self.assertRaises(ValueError):
            packrules.turbo_ea_refs("main")
        with self.assertRaises(ValueError):
            packrules.turbo_ea_refs("1.0.0 --upload-pack=x")

    def test_logins(self):
        for good in ("octocat", "a", "a-b", "A1-b2-c3", "x" * 39):
            self.assertTrue(packrules.is_login(good), good)
        for bad in ("", "-a", "a-", "a--b", "x" * 40, "dependabot[bot]", "a;rm -rf", "$(id)", "a b", "a\n", None):
            self.assertFalse(packrules.is_login(bad), repr(bad))

    def test_blob_sha_matches_git(self):
        # `printf 'hello\n' | git hash-object --stdin`
        self.assertEqual(packrules.git_blob_sha(b"hello\n"), "ce013625030ba8dba906f756967f9e9ca394464a")


class ChangedFiles(unittest.TestCase):
    def test_one_pack(self):
        v = packrules.classify_changes([{"filename": "packs/nl/pack.json", "status": "added"}], 1)
        self.assertEqual((v.kind, v.code, v.path), ("contribution", "nl", "packs/nl/pack.json"))
        v = packrules.classify_changes([{"filename": "packs/pt-BR/pack.json", "status": "modified"}], 1)
        self.assertEqual((v.kind, v.code), ("contribution", "pt-BR"))

    def test_not_about_packs(self):
        v = packrules.classify_changes([{"filename": ".github/workflows/judge.yml", "status": "modified"}], 1)
        self.assertEqual(v.kind, "other")
        v = packrules.classify_changes([{"filename": "README.md", "status": "modified"}], 1)
        self.assertEqual(v.kind, "other")

    def test_invalid(self):
        cases = [
            [{"filename": "packs/nl/pack.json", "status": "added"},
             {"filename": ".github/workflows/judge.yml", "status": "modified"}],
            [{"filename": "packs/nl/pack.json", "status": "added"},
             {"filename": "packs/nl/MAINTAINERS", "status": "added"}],
            [{"filename": "packs/nl/MAINTAINERS", "status": "added"}],
            [{"filename": "index.json", "status": "modified"}, {"filename": "packs/x", "status": "added"}],
            [{"filename": "packs/../x/pack.json", "status": "added"}],
            [{"filename": "packs/nl/../../.github/pack.json", "status": "added"}],
            [{"filename": "packs/nl/sub/pack.json", "status": "added"}],
            [{"filename": "packs/nl/pack.json ", "status": "added"}],
            [{"filename": "packs/nl/pack.json\n", "status": "added"}],
            [{"filename": "packs/NL/pack.json", "status": "added"}],
            [{"filename": "packs/en/pack.json", "status": "added"}],
            [{"filename": "packs/nl/pack.json", "status": "removed"}],
            [{"filename": "packs/nl/pack.json", "status": "renamed", "previous_filename": "packs/fy/pack.json"}],
            [{"filename": "packs/nl/pack.json", "status": "modified", "previous_filename": "x"}],
            [{"filename": "packs/nl/pack.json", "status": "added"}],  # with a truncated total below
        ]
        for i, files in enumerate(cases):
            total = 400 if i == len(cases) - 1 else None
            v = packrules.classify_changes(files, total)
            self.assertEqual(v.kind, "invalid", files)

    def test_rename_out_of_packs_is_caught(self):
        v = packrules.classify_changes(
            [{"filename": "docs/x.json", "status": "renamed", "previous_filename": "packs/nl/pack.json"}], 1
        )
        self.assertEqual(v.kind, "invalid")


class ReadingPacks(unittest.TestCase):
    def test_good(self):
        data = json.dumps(good_pack()).encode()
        pack = packrules.parse_pack_bytes(data)
        self.assertEqual(packrules.header_problems(pack, "nl"), [])

    def test_bom_is_tolerated(self):
        data = "﻿".encode() + json.dumps(good_pack()).encode()
        self.assertEqual(packrules.parse_pack_bytes(data)["mode"], "share")

    def test_size_cap(self):
        with self.assertRaises(packrules.PackError):
            packrules.parse_pack_bytes(b" " * (packrules.MAX_PACK_BYTES + 1))

    def test_deep_nesting(self):
        deep = ("[" * 100000 + "]" * 100000).encode()
        with self.assertRaisesRegex(packrules.PackError, "deeper"):
            packrules.parse_pack_bytes(deep)
        # Brackets inside strings do not count.
        self.assertTrue(packrules.json_depth_ok(json.dumps({"a": "[[[[[[[[[[[[[[[[\\\"[[[["})))

    def test_duplicate_keys(self):
        text = '{"format": "turbo-ea-language-pack", "format": "x"}'
        with self.assertRaisesRegex(packrules.PackError, "duplicate"):
            packrules.parse_pack_bytes(text.encode())

    def test_not_json_or_not_object(self):
        for data in (b"not json", b"[]", b"\xff\xfe", b'"x"'):
            with self.assertRaises(packrules.PackError):
                packrules.parse_pack_bytes(data)

    def test_header_problems(self):
        def problems(**kw):
            return packrules.header_problems(good_pack(**kw), "nl")

        self.assertTrue(problems(mode="current"))
        self.assertTrue(problems(mode="missing"))
        self.assertTrue(problems(format_version=2))
        self.assertTrue(problems(format_version=True))
        self.assertTrue(problems(app_version="v2.214.0"))
        self.assertTrue(problems(app_version="2.214.0; curl evil"))
        self.assertTrue(problems(extra="x"))
        self.assertTrue(problems(strings={}))
        self.assertTrue(problems(strings={"common": "x"}))
        self.assertTrue(problems(source_hashes={"common": {"a": "not-a-hash"}}))
        self.assertTrue(problems(locale={"code": "fy", "name": "Frysk", "english_name": "Frisian", "rtl": False}))
        self.assertTrue(problems(locale={"code": "de", "name": "Deutsch", "english_name": "German", "rtl": False}))
        self.assertTrue(problems(locale={"code": "nl", "name": "Ned‮erlands", "english_name": "Dutch", "rtl": False}))
        self.assertTrue(problems(locale={"code": "nl", "name": "Nederlands", "english_name": "Dutch;", "rtl": False}))
        self.assertTrue(problems(locale={"code": "nl", "name": "Nederlands", "english_name": "Dutch", "rtl": "no"}))
        self.assertTrue(problems(locale={"code": "nl", "name": "\n", "english_name": "Dutch", "rtl": False}))
        self.assertTrue(problems(locale={"code": "nl", "name": "N", "english_name": "Dutch", "rtl": False, "x": 1}))
        self.assertTrue(problems(locale="nl"))
        self.assertEqual(problems(), [])


M = packrules.Maintainer
IDS = {"alice": 1, "bob": 2, "zed": 26, "mallory": 13}


class Maintainers(unittest.TestCase):
    def test_parse(self):
        text = ("# comment\nalice 1\n\n@Bob  # trailing\nalice\nnot a login\n$(id)\ncarol-x 33\n"
                "dave 1x\neve 1 2\nfrank ١٢\n")
        self.assertEqual(packrules.parse_maintainers(text), [M("alice", 1), M("Bob"), M("carol-x", 33)])

    def test_file_roundtrip(self):
        text = packrules.maintainers_file("nl", "alice", 1)
        self.assertEqual(packrules.parse_maintainers(text), [M("alice", 1)])
        for args in (("nl", "a;b", 1), ("../x", "alice", 1), ("nl", "alice", None), ("nl", "alice", "1")):
            with self.assertRaises(ValueError):
                packrules.maintainers_file(*args)

    def test_id_pins_the_account(self):
        self.assertTrue(M("alice", 1).is_user("Alice", 1))
        self.assertFalse(M("alice", 1).is_user("alice", 999))  # renamed login re-registered
        self.assertTrue(M("alice").is_user("alice", 999))
        self.assertFalse(M("alice").is_user("alicex", 1))


def review(login, state, commit=HEAD, user_id=None):
    return {"user": {"login": login, "id": user_id or IDS.get(login, 0)}, "state": state, "commit_id": commit}


class Decide(unittest.TestCase):
    def d(self, **kw):
        args = dict(author="zed", author_id=26, head_owner=None, language_exists=True,
                    maintainers=[M("alice", 1), M("bob", 2)], reviews=[], head_sha=HEAD)
        args.update(kw)
        if args["head_owner"] is None:
            args["head_owner"] = args["author"]
        return packrules.decide(**args)

    def test_new_language_merges(self):
        decision = self.d(language_exists=False, maintainers=[])
        self.assertEqual((decision.action, decision.new_language), ("merge", True))

    def test_new_language_from_someone_elses_branch_waits(self):
        self.assertEqual(self.d(language_exists=False, maintainers=[], head_owner="mallory").action, "wait")

    def test_bad_author_login(self):
        self.assertEqual(self.d(author="evil[bot]", language_exists=False).action, "skip")
        self.assertEqual(self.d(author="a;rm -rf /").action, "skip")
        self.assertEqual(self.d(author_id=None, language_exists=False).action, "skip")

    def test_maintainer_author_merges(self):
        self.assertEqual(self.d(author="Alice", author_id=1).action, "merge")

    def test_reused_login_is_not_the_maintainer(self):
        self.assertEqual(self.d(author="alice", author_id=999).action, "wait")
        self.assertEqual(self.d(reviews=[review("bob", "APPROVED", user_id=999)]).action, "wait")

    def test_maintainer_on_someone_elses_branch_needs_approval(self):
        self.assertEqual(self.d(author="alice", author_id=1, head_owner="mallory").action, "wait")
        decision = self.d(author="alice", author_id=1, head_owner="mallory", reviews=[review("bob", "APPROVED")])
        self.assertEqual(decision.action, "merge")

    def test_other_author_waits(self):
        self.assertEqual(self.d().action, "wait")

    def test_no_maintainers_waits(self):
        self.assertEqual(self.d(maintainers=[], reviews=[review("alice", "APPROVED")]).action, "wait")

    def test_maintainer_approval_merges(self):
        self.assertEqual(self.d(reviews=[review("bob", "APPROVED")]).action, "merge")

    def test_approval_of_older_commit_does_not_count(self):
        self.assertEqual(self.d(reviews=[review("bob", "APPROVED", "b" * 40)]).action, "wait")

    def test_non_maintainer_approval_does_not_count(self):
        self.assertEqual(self.d(reviews=[review("mallory", "APPROVED")]).action, "wait")

    def test_later_changes_requested_wins(self):
        reviews = [review("bob", "APPROVED"), review("bob", "CHANGES_REQUESTED")]
        self.assertEqual(self.d(reviews=reviews).action, "wait")

    def test_dismissed_approval_does_not_count(self):
        reviews = [review("bob", "APPROVED"), review("bob", "DISMISSED")]
        self.assertEqual(self.d(reviews=reviews).action, "wait")

    def test_comment_after_approval_keeps_it(self):
        reviews = [review("bob", "APPROVED"), review("bob", "COMMENTED")]
        self.assertEqual(self.d(reviews=reviews).action, "merge")

    def test_any_maintainer_blocking(self):
        reviews = [review("bob", "APPROVED"), review("alice", "CHANGES_REQUESTED")]
        self.assertEqual(self.d(reviews=reviews).action, "wait")

    def test_malformed_reviews_ignored(self):
        reviews = [{"user": None, "state": "APPROVED"}, {"state": "APPROVED"}, review("bob", "APPROVED")]
        self.assertEqual(self.d(reviews=reviews).action, "merge")


class Stats(unittest.TestCase):
    def test_counts_and_coverage(self):
        english = {"common": {"a": "Save", "b": "Cancel"}, "admin": {"c": "X", "d": "Y"}}
        pack = good_pack(
            strings={"common": {"a": "Opslaan", "b": "Annuleren", "b_few": "x"}, "metamodel": {"type.A.label": "A"}},
            source_hashes={"common": {"a": packrules.source_hash("Save"), "b": "000000000000"}},
        )
        stats = packrules.pack_stats(pack, english)
        self.assertEqual(stats["ui_strings"], 3)
        self.assertEqual(stats["metamodel_strings"], 1)
        self.assertEqual(stats["english_strings"], 4)
        self.assertEqual(stats["translated"], 2)
        self.assertEqual(stats["current"], 1)
        self.assertEqual(stats["coverage"], 50.0)

    def test_flatten(self):
        self.assertEqual(
            packrules.flatten_english({"a": {"b": "x", "c": {"d": "y"}}, "e.f": "z", "n": 1}),
            {"a.b": "x", "a.c.d": "y", "e.f": "z"},
        )


if __name__ == "__main__":
    unittest.main()
