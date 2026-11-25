"""Judge and merge language-pack pull requests. Run by .github/workflows/judge.yml.

Subcommands (each reads its inputs from environment variables the workflow
sets with ``env:``, never from interpolated script text):

``resolve``   which open pull requests to judge (sweep / review relay / manual).
``inspect``   read the PR's file list and pack bytes through the API at the exact
              head SHA, apply this repository's rules, write the pack to a file.
              Needs a read-only token.
``validate``  fetch turbo-ea's validator and English files for the pack's
              app_version and run it on that file. No token.
``merge``     re-check everything against the live PR and default branch, apply
              the maintainer policy and merge that exact SHA. Needs write.

Nothing from the pull request is ever executed, imported or checked out.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import secrets
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

sys.path.insert(0, str(Path(__file__).resolve().parent))
import packrules  # noqa: E402

# ---------------------------------------------------------------------------
# Workflow plumbing
# ---------------------------------------------------------------------------


def set_output(name: str, value: str) -> None:
    """Write a step output. Values are produced by this script from validated
    data; a newline would let a value forge another output, so it is refused."""
    if "\n" in value or "\r" in value:
        raise ValueError("output value with a newline")
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(f"{name}={value}\n")
    else:
        print(f"[output] {name}={value}")


def summary(markdown: str) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(markdown + "\n")
    else:
        print(markdown)


def esc(text: Any) -> str:
    """HTML-escape untrusted text for the job summary."""
    return html.escape(str(text), quote=True)


@contextmanager
def untrusted_output() -> Iterator[None]:
    """Stop the runner from reading workflow commands (``::set-env`` …) in
    whatever is printed inside the block (validator output quoting pack text)."""
    token = secrets.token_hex(16)
    print(f"::stop-commands::{token}", flush=True)
    try:
        yield
    finally:
        print(f"::{token}::", flush=True)


def env(name: str) -> str:
    return os.environ.get(name, "")


def fail(message: str, *, title: str = "Pack check failed") -> int:
    summary(f"### ❌ {esc(title)}\n\n{esc(message)}\n")
    with untrusted_output():
        print(f"{title}: {message}")
    return 1


def client():  # imported lazily so `validate` never needs the API module
    import gh_api

    return gh_api.Client()


def pr_inputs() -> tuple[str, str]:
    number, head_sha = env("PR_NUMBER"), env("HEAD_SHA")
    if not packrules.is_pr_number(number) or not packrules.is_sha(head_sha):
        raise SystemExit("PR_NUMBER / HEAD_SHA missing or malformed")
    return number, head_sha


# ---------------------------------------------------------------------------
# resolve
# ---------------------------------------------------------------------------

SWEEP_LIMIT = 100


def cmd_resolve() -> int:
    """Output ``prs``: a JSON list of ``{"number", "head_sha"}`` to judge."""
    api = client()
    event = env("EVENT_NAME")
    prs: list[dict[str, str]] = []
    if event == "workflow_dispatch":
        number = env("PR_NUMBER")
        if not packrules.is_pr_number(number):
            return fail("the pr_number input must be a pull request number")
        pr = api.get(api.r(f"/pulls/{number}"))
        if pr.get("state") == "open":
            prs.append({"number": number, "head_sha": pr["head"]["sha"]})
    else:
        wanted = env("RUN_HEAD_SHA")
        if event == "workflow_run" and not packrules.is_sha(wanted):
            return fail("workflow_run without a head SHA")
        default = api.default_branch()
        # Least recently updated first, so a PR waiting for a long time is
        # never crowded out of the sweep by newer ones.
        for pr in api.paginate(api.r("/pulls"), limit=1000, state="open", base=default,
                               sort="updated", direction="asc"):
            sha = pr.get("head", {}).get("sha")
            if not packrules.is_sha(sha) or pr.get("draft"):
                continue
            if event == "workflow_run" and sha != wanted:
                continue
            prs.append({"number": str(int(pr["number"])), "head_sha": sha})
        prs = prs[:SWEEP_LIMIT]
    set_output("prs", json.dumps(prs, separators=(",", ":")))
    print(f"{len(prs)} pull request(s) to judge")
    return 0


# ---------------------------------------------------------------------------
# inspect
# ---------------------------------------------------------------------------


def cmd_inspect(out_dir: Path) -> int:
    number, head_sha = pr_inputs()
    api = client()
    set_output("kind", "other")
    pr = api.get(api.r(f"/pulls/{number}"))
    if pr.get("state") != "open":
        summary(f"PR #{number} is not open; nothing to judge.")
        return 0
    if pr["head"]["sha"] != head_sha:
        summary(f"PR #{number} has moved past {head_sha[:12]}; the run for its new head judges it.")
        set_output("kind", "superseded")
        return 0
    default = api.default_branch()
    if pr["base"]["ref"] != default:
        summary(f"PR #{number} does not target `{esc(default)}`; not a pack contribution.")
        return 0
    default_sha = api.branch_sha(default)
    # Three-dot compare at the exact head SHA: the PR's own diff, from its
    # merge base. ``changed_files`` from the PR guards a truncated list.
    compare = api.get(api.r(f"/compare/{default_sha}...{head_sha}"), per_page=100)
    files = compare.get("files") or []
    verdict = packrules.classify_changes(files, int(pr.get("changed_files") or 0))
    if verdict.kind == "other":
        summary(f"PR #{number}: {esc(verdict.reason)}. It is left to the repository owner.")
        return 0
    if verdict.kind != "contribution":
        return fail(verdict.reason)
    assert verdict.code is not None and verdict.path is not None
    entry = api.tree_entry(head_sha, verdict.path)
    if entry is None:
        return fail(f"{verdict.path} not found at {head_sha[:12]}")
    if entry.get("type") != "blob" or entry.get("mode") != packrules.MODE_REGULAR:
        return fail(f"{verdict.path} must be a regular file (not a symlink, submodule or executable)")
    size = int(entry.get("size") or 0)
    if size > packrules.MAX_PACK_BYTES:
        return fail(f"{verdict.path} is {size} bytes; the limit is {packrules.MAX_PACK_BYTES}")
    data = api.blob(entry["sha"])
    if packrules.git_blob_sha(data) != entry["sha"] or len(data) != size:
        return fail("the pack's content does not match its git object id")
    try:
        pack = packrules.parse_pack_bytes(data)
    except packrules.PackError as exc:
        return fail(str(exc))
    problems = packrules.header_problems(pack, verdict.code)
    if problems:
        return fail("; ".join(problems))
    author = pr.get("user", {}).get("login", "")
    exists = api.tree_entry(default_sha, f"packs/{verdict.code}") is not None
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "pack.json").write_bytes(data)
    set_output("kind", "contribution")
    set_output("code", verdict.code)
    set_output("app_version", pack["app_version"])
    set_output("blob_sha", entry["sha"])
    set_output("head_sha", head_sha)
    summary(
        f"### Pack `{esc(verdict.code)}` — {esc(pack['locale']['english_name'])}\n\n"
        f"* PR #{number} by `{esc(author)}` at `{head_sha[:12]}`\n"
        f"* {'update of an existing language' if exists else 'new language'}\n"
        f"* made with Turbo EA {esc(pack['app_version'])}\n"
    )
    return 0


# ---------------------------------------------------------------------------
# validate
# ---------------------------------------------------------------------------


def cmd_validate(pack_file: Path, work: Path) -> int:
    import turbo_ea

    app_version = env("APP_VERSION")
    if not packrules.is_version(app_version):
        return fail("app_version is not a plain x.y.z version")
    try:
        got = turbo_ea.prepare(app_version, work, env("TURBO_EA_VALIDATOR_SHA"))
    except (RuntimeError, ValueError, OSError) as exc:
        return fail(str(exc), title="Could not get turbo-ea's validator")
    code, output = turbo_ea.run_validator(got["validator"], pack_file, got["english"])
    origin = (f"validator from turbo-ea {got['validator_label']} ({str(got['validator_sha'])[:12]}), "
              f"English text from {got['english_label']} ({str(got['english_sha'])[:12]})")
    with untrusted_output():
        print(origin)
        print(output)
    lines = output.strip().splitlines()
    shown = "\n".join(lines[:200]) + ("\n… (truncated; full report in the job log)" if len(lines) > 200 else "")
    verdict = "✅ valid" if code == 0 else "❌ invalid"
    summary(f"### Validation: {verdict}\n\n{esc(origin)}\n\n<pre>{esc(shown)}</pre>\n")
    if code == 1:
        summary(
            "Each line is `namespace  key  reason`. See CONTRIBUTING.md for what each reason means; "
            "fix the strings in Turbo EA, export **Share (community pack)** again and push the new file.\n"
        )
    return 0 if code == 0 else 1


# ---------------------------------------------------------------------------
# merge
# ---------------------------------------------------------------------------


def cmd_merge() -> int:
    import gh_api

    number, head_sha = pr_inputs()
    code, blob_sha = env("CODE"), env("BLOB_SHA")
    if packrules.code_problem(code) is not None or not packrules.is_sha(blob_sha):
        raise SystemExit("CODE / BLOB_SHA missing or malformed")
    path = f"packs/{code}/pack.json"
    api = client()
    pr = api.get(api.r(f"/pulls/{number}"))
    default = api.default_branch()
    if pr.get("state") != "open" or pr["head"]["sha"] != head_sha or pr["base"]["ref"] != default:
        summary(f"PR #{number} changed since it was validated (closed, new commits or new base); not merged.")
        return 0
    if pr.get("draft"):
        summary(f"PR #{number} is a draft; mark it ready for review to have it merged.")
        return 0
    user = pr.get("user") or {}
    author, author_id = user.get("login", ""), user.get("id")
    head_repo = pr["head"].get("repo") or {}
    head_owner = (head_repo.get("owner") or {}).get("login")
    default_sha = api.branch_sha(default)
    exists = api.tree_entry(default_sha, f"packs/{code}") is not None
    maintainers_text = api.file_text(default_sha, f"packs/{code}/MAINTAINERS") or ""
    maintainers = packrules.parse_maintainers(maintainers_text)
    reviews = api.paginate(api.r(f"/pulls/{number}/reviews"), limit=500)
    decision = packrules.decide(
        author=author, author_id=author_id, head_owner=head_owner, language_exists=exists,
        maintainers=maintainers, reviews=reviews, head_sha=head_sha,
    )
    if decision.action != "merge":
        summary(f"### ⏳ Not merged yet\n\n{esc(decision.reason)}\n")
        return 0
    # The squash result must be byte-for-byte the validated file: refuse when
    # the pack on the default branch changed since the PR's merge base (a
    # three-way merge would then produce a file nobody validated).
    compare = api.get(api.r(f"/compare/{default_sha}...{head_sha}"), per_page=1)
    merge_base = compare["merge_base_commit"]["sha"]
    before = api.tree_entry(merge_base, path)
    now = api.tree_entry(default_sha, path)
    if (before or {}).get("sha") != (now or {}).get("sha"):
        summary(
            f"### ⏳ Not merged\n\nThe `{esc(code)}` pack changed on `{esc(default)}` after this PR was "
            "branched. Update your branch from the latest version (or open a new PR from it) so the "
            "checks run on the file that would actually be merged.\n"
        )
        return 0
    verb = "Add" if decision.new_language else "Update"
    try:
        result = api.request(
            "PUT",
            api.r(f"/pulls/{number}/merge"),
            body={
                "sha": head_sha,
                "merge_method": "squash",
                "commit_title": f"{verb} {code} language pack (#{number})",
                "commit_message": "",
            },
        )
    except gh_api.ApiError as exc:
        return fail(str(exc), title="Merge refused by GitHub")
    merge_sha = result.get("sha")
    summary(f"### ✅ Merged — {esc(decision.reason)}\n")
    status = 0
    merged_entry = api.tree_entry(merge_sha, path) if packrules.is_sha(merge_sha) else None
    if (merged_entry or {}).get("sha") != blob_sha:
        status = fail("the merged pack differs from the validated one; the owner must check this commit",
                      title="Post-merge check failed")
    if decision.new_language:
        try:
            api.put_file(
                f"packs/{code}/MAINTAINERS",
                packrules.maintainers_file(code, author, author_id).encode("utf-8"),
                f"Make {author} the maintainer of the {code} language pack (#{number})",
                default,
                None,
            )
            summary(f"`{esc(author)}` is now the maintainer of `{esc(code)}`.")
        except gh_api.ApiError as exc:
            status = fail(f"could not write packs/{code}/MAINTAINERS: {exc}", title="Maintainer not recorded")
    try:
        api.request("POST", api.r("/actions/workflows/registry.yml/dispatches"), body={"ref": default})
    except gh_api.ApiError as exc:
        status = fail(f"could not start the index update: {exc}", title="Index not refreshed")
    return status


# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("resolve")
    p_inspect = sub.add_parser("inspect")
    p_inspect.add_argument("--out", type=Path, required=True)
    p_validate = sub.add_parser("validate")
    p_validate.add_argument("--pack", type=Path, required=True)
    p_validate.add_argument("--work", type=Path, required=True)
    sub.add_parser("merge")
    args = parser.parse_args(argv)
    if args.command == "resolve":
        return cmd_resolve()
    if args.command == "inspect":
        return cmd_inspect(args.out)
    if args.command == "validate":
        return cmd_validate(args.pack, args.work)
    return cmd_merge()


if __name__ == "__main__":
    sys.exit(main())
