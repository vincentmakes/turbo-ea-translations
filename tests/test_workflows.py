"""Static guard rails for .github/workflows (no YAML library needed).

* no `${{ … }}` expression inside a `run:` script (values go through `env:`);
* every workflow starts from `permissions: {}` or read-only `contents: read`;
* every `uses:` is a local workflow or pinned to a full commit SHA;
* no `secrets: inherit`, no cache/artifact actions, no checkout of a PR ref.
"""

import re
import unittest
from pathlib import Path

WORKFLOWS = sorted((Path(__file__).resolve().parent.parent / ".github" / "workflows").glob("*.yml"))


def run_blocks(text: str):
    """Yield (line number, script text) of every `run:` value."""
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        m = re.match(r"^(\s*)(?:-\s+)?run:\s*(.*)$", lines[i])
        if not m:
            i += 1
            continue
        indent = len(m.group(1))
        rest = m.group(2)
        start = i + 1
        body = [] if rest in ("|", ">", "|-", ">-", "|+", ">+") else [rest]
        i += 1
        while i < len(lines) and (not lines[i].strip() or len(lines[i]) - len(lines[i].lstrip()) > indent + 2):
            body.append(lines[i])
            i += 1
        yield start, "\n".join(body)


class Workflows(unittest.TestCase):
    def test_found(self):
        names = {p.name for p in WORKFLOWS}
        self.assertTrue({"judge.yml", "pack-check.yml", "sweep.yml", "registry.yml"} <= names)

    def test_no_expressions_in_run_scripts(self):
        for path in WORKFLOWS:
            for line, script in run_blocks(path.read_text()):
                self.assertNotIn("${{", script, f"{path.name}:{line}: pass values through env:")

    def test_top_level_permissions(self):
        for path in WORKFLOWS:
            text = path.read_text()
            self.assertTrue(
                re.search(r"^permissions: \{\}$", text, re.M) or re.search(r"^permissions:\n  contents: read$", text, re.M),
                f"{path.name}: top-level permissions must be {{}} or contents: read",
            )

    def test_actions_pinned(self):
        for path in WORKFLOWS:
            for m in re.finditer(r"^\s*(?:-\s+)?uses:\s*(\S+)", path.read_text(), re.M):
                ref = m.group(1)
                if ref.startswith("./"):
                    continue
                self.assertRegex(ref, r"^[\w.-]+/[\w./-]+@[0-9a-f]{40}$", f"{path.name}: pin {ref} to a SHA")

    def test_forbidden_patterns(self):
        for path in WORKFLOWS:
            text = path.read_text()
            for pattern in ("secrets: inherit", "actions/cache", "upload-artifact", "download-artifact",
                            "head.ref", "head_ref", "refs/pull/", "github.event.pull_request.title",
                            "github.event.pull_request.body", "github.event.comment", "persist-credentials: true"):
                self.assertNotIn(pattern, text, f"{path.name}: {pattern}")

    def test_privileged_triggers_explain_themselves(self):
        for path in WORKFLOWS:
            text = path.read_text()
            if re.search(r"^  (pull_request_target|workflow_run):", text, re.M):
                self.assertTrue(text.startswith("#"), f"{path.name}: explain why the trigger is safe at the top")
                self.assertRegex(text, r"(?i)safe")

    def test_only_judge_merge_and_registry_write(self):
        for path in WORKFLOWS:
            text = path.read_text()
            if path.name in ("judge.yml", "registry.yml", "pack-check.yml", "sweep.yml"):
                continue
            self.assertNotRegex(text, r":\s*write", path.name)


if __name__ == "__main__":
    unittest.main()
