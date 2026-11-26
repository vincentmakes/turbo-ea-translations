"""Static guard rails for .github/workflows (no YAML library needed).

* no `${{ … }}` expression inside a `run:` script (values go through `env:`);
* every workflow starts from `permissions: {}` or read-only `contents: read`;
* only the jobs that must write can, and the validator step holds no token;
* every `uses:` is a local workflow or pinned to a full commit SHA;
* no `secrets: inherit`, no cache/artifact actions, no checkout of a PR ref.
"""

import re
import unittest
from pathlib import Path

WORKFLOWS = sorted((Path(__file__).resolve().parent.parent / ".github" / "workflows").glob("*.yml"))


def indent_of(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def run_blocks(text: str):
    """Yield ``(line number, script text)`` of every ``run:`` value, inline or
    block scalar (``|``, ``>-`` …). A block's content is every following line
    indented deeper than the ``run`` key itself (blank lines included)."""
    lines = text.splitlines()
    for i, line in enumerate(lines):
        m = re.match(r"^(\s*(?:-\s+)?)run:\s*(.*)$", line)
        if not m:
            continue
        key_col = len(m.group(1))  # column of "run", after any "- "
        rest = m.group(2)
        body = [] if re.fullmatch(r"[|>][+-]?", rest) else [rest]
        j = i + 1
        while j < len(lines) and (not lines[j].strip() or indent_of(lines[j]) > key_col):
            body.append(lines[j])
            j += 1
        yield i + 1, "\n".join(body)


def blocks(text: str, indent: int):
    """``{key: text}`` of the mapping entries at exactly ``indent`` spaces
    (jobs under ``jobs:`` are at 2), each with everything nested under it."""
    out, key, buf = {}, None, []
    for line in text.splitlines():
        m = re.match(rf"^ {{{indent}}}([A-Za-z0-9_-]+):", line)
        if m:
            if key:
                out[key] = "\n".join(buf)
            key, buf = m.group(1), [line]
        elif key is not None and line.strip() and indent_of(line) <= indent - 1:
            out[key] = "\n".join(buf)
            key, buf = None, []
        elif key is not None:
            buf.append(line)
    if key:
        out[key] = "\n".join(buf)
    return out


def jobs(text: str):
    return blocks(re.split(r"^jobs:\n", text, maxsplit=1, flags=re.M)[1], 2)


def steps(job_text: str):
    parts = re.split(r"\n\s+- (?=name:|run:|uses:)", job_text)
    return parts[1:]


class Scanner(unittest.TestCase):
    """The scanner itself must catch what it is for."""

    HOSTILE = """jobs:
  x:
    steps:
      - name: a
        run: |
          echo ok
          echo "${{ github.event.pull_request.title }}"
      - run: >-
          echo "${{ github.head_ref }}"
      - name: b
        run: echo ${{ github.event.issue.title }}
"""

    def test_catches_expressions_in_every_run_form(self):
        found = [script for _, script in run_blocks(self.HOSTILE) if "${{" in script]
        self.assertEqual(len(found), 3)

    def test_does_not_overrun_into_next_step(self):
        scripts = [s for _, s in run_blocks("    steps:\n      - run: |\n          a\n      - name: n\n"
                                            "        env:\n          X: ${{ github.sha }}\n")]
        self.assertEqual(scripts, ["          a"])

    def test_jobs_and_steps(self):
        js = jobs(self.HOSTILE)
        self.assertEqual(list(js), ["x"])
        self.assertEqual(len(steps(js["x"])), 3)


class Workflows(unittest.TestCase):
    def test_found(self):
        names = {p.name for p in WORKFLOWS}
        self.assertTrue({"judge.yml", "pack-check.yml", "sweep.yml", "registry.yml", "review-relay.yml",
                         "tests.yml"} <= names)

    def test_run_scripts_are_actually_scanned(self):
        text = (WORKFLOWS[0].parent / "judge.yml").read_text()
        scripts = [s for _, s in run_blocks(text)]
        self.assertTrue(any("git init" in s and "checkout" in s for s in scripts))

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
                            "github.event.pull_request.body", "github.event.comment", "persist-credentials: true",
                            "secrets."):
                self.assertNotIn(pattern, text, f"{path.name}: {pattern}")

    def test_privileged_triggers_explain_themselves(self):
        for path in WORKFLOWS:
            text = path.read_text()
            if re.search(r"^  (pull_request_target|workflow_run):", text, re.M):
                self.assertTrue(text.startswith("#"), f"{path.name}: explain why the trigger is safe at the top")
                self.assertRegex(text, r"(?i)safe")

    def test_write_permissions_per_job(self):
        """Exactly these jobs may hold a write permission."""
        allowed = {("judge.yml", "merge"), ("judge.yml", "decide"), ("registry.yml", "index"),
                   ("pack-check.yml", "judge"), ("sweep.yml", "judge")}  # callers: cap for judge.yml
        for path in WORKFLOWS:
            for name, job in jobs(path.read_text()).items():
                if (path.name, name) not in allowed:
                    self.assertNotRegex(job, r":\s*write", f"{path.name} job {name} must not write")

    def test_validate_job_is_read_only_and_validator_step_has_no_token(self):
        js = jobs((WORKFLOWS[0].parent / "judge.yml").read_text())
        self.assertNotRegex(js["validate"], r":\s*write")
        validator_steps = [s for s in steps(js["validate"]) if "judge.py\" validate" in s]
        self.assertEqual(len(validator_steps), 1)
        for forbidden in ("GH_TOKEN", "GITHUB_TOKEN", "github.token", "secrets"):
            self.assertNotIn(forbidden, validator_steps[0])

    def test_only_api_steps_get_the_token(self):
        for path in WORKFLOWS:
            for name, job in jobs(path.read_text()).items():
                for step in steps(job):
                    if "github.token" in step:
                        self.assertNotIn("git -C", step, f"{path.name}/{name}: a git step holds the token")
                        self.assertRegex(step, r"judge\.py\" (inspect|resolve|decide|merge)|build_index\.py\" publish",
                                         f"{path.name}/{name}: unexpected step with the token")


if __name__ == "__main__":
    unittest.main()


class Structure(unittest.TestCase):
    """The properties the security argument relies on, pinned one by one."""

    def text(self, name):
        return (WORKFLOWS[0].parent / name).read_text()

    def test_top_level_permissions_block_is_read_only(self):
        for path in WORKFLOWS:
            text = path.read_text()
            m = re.search(r"^permissions:(.*?)(?=^\S)", text, re.M | re.S)
            self.assertIsNotNone(m, path.name)
            block = m.group(1).strip()
            if block == "{}":
                continue
            for line in block.splitlines():
                self.assertRegex(line.strip(), r"^[a-z-]+: read$", f"{path.name}: {line}")

    def test_merge_runs_only_after_validate_and_decide(self):
        merge = jobs(self.text("judge.yml"))["merge"]
        self.assertIn("needs: [validate, decide]", merge)
        self.assertIn("needs.decide.outputs.action == 'merge'", merge)
        self.assertIn("needs.validate.outputs.head_sha == inputs.head_sha", merge)
        self.assertIn("group: merge-default-branch", merge)
        decide = jobs(self.text("judge.yml"))["decide"]
        self.assertIn("needs: validate", decide)
        self.assertIn("needs.validate.result == 'success'", decide)

    def test_pack_check_only_for_main(self):
        text = self.text("pack-check.yml")
        self.assertRegex(text, r"pull_request_target:\n(?:\s+#.*\n|\s+types:.*\n)*\s+branches: \[main\]")

    def test_no_checkout_action_at_all(self):
        for path in WORKFLOWS:
            self.assertNotIn("actions/checkout", path.read_text(), path.name)

    def test_privileged_env_values_come_from_trusted_sources(self):
        allowed = {
            "github.token", "github.repository", "github.sha", "github.event_name",
            "github.event.workflow_run.head_sha", "inputs.pr_number", "inputs.head_sha",
            "steps.inspect.outputs.app_version", "steps.inspect.outputs.new_language",
            "vars.TURBO_EA_VALIDATOR_SHA", "vars.AUTOMERGE_HALTED",
            "needs.validate.outputs.head_sha", "needs.validate.outputs.code",
            "needs.validate.outputs.blob_sha", "needs.validate.outputs.coverage",
            "needs.validate.outputs.identical",
        }
        for name in ("judge.yml", "sweep.yml", "registry.yml"):
            for job_name, job in jobs(self.text(name)).items():
                for step in steps(job):
                    env_block = re.search(r"env:\n((?:\s+[A-Z_]+: .*\n|\s+#.*\n)+)", step + "\n")
                    if not env_block:
                        continue
                    for expr in re.findall(r"\$\{\{\s*([^}]*?)\s*\}\}", env_block.group(1)):
                        self.assertIn(expr, allowed, f"{name}/{job_name}: env value {expr}")
