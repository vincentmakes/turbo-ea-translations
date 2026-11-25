import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import build_index  # noqa: E402
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
        self.assertEqual(self.build()["languages"][0]["updated"], "2026-01-02T03:04:05Z")


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
