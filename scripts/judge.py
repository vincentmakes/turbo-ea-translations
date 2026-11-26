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
    # <pre>: the message can quote pack-controlled text, which must not render
    # as Markdown (links, images) in the job summary.
    summary(f"### ❌ {esc(title)}\n\n<pre>{esc(message)}</pre>\n")
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


def sweep_window(numbers: list[int], day: int, size: int = SWEEP_LIMIT) -> list[int]:
    """A window of ``size`` PR numbers that moves by ``size`` every day, so
    every open PR is swept within ``ceil(n / size)`` days however many there
    are (judging a PR does not change its "updated" time, so a fixed window
    would see the same PRs forever)."""
    ordered = sorted(numbers)
    if len(ordered) <= size:
        return ordered
    start = (day * size) % len(ordered)
    window = ordered[start:start + size]
    return window + ordered[: size - len(window)]


def cmd_resolve() -> int:
    """Output ``prs``: a JSON list of ``{"number", "head_sha"}`` to judge."""
    import time

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
    elif event == "workflow_run":
        wanted = env("RUN_HEAD_SHA")
        if not packrules.is_sha(wanted):
            return fail("workflow_run without a head SHA")
        default = api.default_branch()
        found = api.get(api.r(f"/commits/{wanted}/pulls")) or []
        if not found:  # not every fork commit is indexed: fall back to a scan
            found = api.paginate(api.r("/pulls"), limit=1000, state="open", base=default)
        for pr in found:
            head = pr.get("head") or {}
            if (pr.get("state") == "open" and not pr.get("draft") and head.get("sha") == wanted
                    and (pr.get("base") or {}).get("ref") == default):
                prs.append({"number": str(int(pr["number"])), "head_sha": wanted})
    else:  # schedule
        default = api.default_branch()
        open_prs = {
            int(pr["number"]): pr["head"]["sha"]
            for pr in api.paginate(api.r("/pulls"), limit=1000, state="open", base=default)
            if packrules.is_sha((pr.get("head") or {}).get("sha")) and not pr.get("draft")
        }
        day = int(time.time() // 86400)
        prs = [{"number": str(n), "head_sha": open_prs[n]} for n in sweep_window(list(open_prs), day)]
    set_output("prs", json.dumps(prs, separators=(",", ":")))
    print(f"{len(prs)} pull request(s) to judge")
    return 0


# ---------------------------------------------------------------------------
# inspect
# ---------------------------------------------------------------------------


class Refused(Exception):
    """The head does not qualify; ``str()`` is the reason for the contributor."""


def check_head(api: Any, pr: dict[str, Any], default_sha: str, head_sha: str):
    """Everything about the PR's content that decides whether it is a
    contribution, read at the exact ``head_sha``. Run by ``inspect`` and again
    by ``merge`` (against the then-current default branch), so the merge never
    relies on what an earlier job saw.

    Returns ``(verdict, entry, compare)``; ``verdict.kind == "other"`` for a PR
    that is not a pack contribution. Raises :class:`Refused`."""
    # Three-dot compare at the exact head SHA: the PR's own diff and commits.
    compare = api.get(api.r(f"/compare/{default_sha}...{head_sha}"), per_page=packrules.MAX_PR_COMMITS)
    files = compare.get("files") or []
    verdict = packrules.classify_changes(files, int(pr.get("changed_files") or 0))
    if verdict.kind == "other":
        return verdict, None, compare
    if verdict.kind != "contribution":
        raise Refused(verdict.reason)
    problem = packrules.history_problem(compare)
    if problem:
        raise Refused(problem)
    assert verdict.path is not None
    entry = api.tree_entry(head_sha, verdict.path)
    if entry is None:
        raise Refused(f"{verdict.path} not found at {head_sha[:12]}")
    if entry.get("type") != "blob" or entry.get("mode") != packrules.MODE_REGULAR:
        raise Refused(f"{verdict.path} must be a regular file (not a symlink, submodule or executable)")
    size = int(entry.get("size") or 0)
    if size > packrules.MAX_PACK_BYTES:  # checked before anything is downloaded
        raise Refused(f"{verdict.path} is {size} bytes; the limit is {packrules.MAX_PACK_BYTES}")
    return verdict, entry, compare


def cmd_inspect(out_dir: Path) -> int:
    number, head_sha = pr_inputs()
    api = client()
    set_output("kind", "other")
    pr = api.get(api.r(f"/pulls/{number}"))
    if pr.get("state") != "open":
        summary(f"### Not judged\n\nPR #{number} is not open.")
        return 0
    if pr["head"]["sha"] != head_sha:
        summary(f"### Not judged: superseded\n\nPR #{number} has moved past `{head_sha[:12]}`; "
                "the run for its new head judges it.")
        set_output("kind", "superseded")
        return 0
    default = api.default_branch()
    if pr["base"]["ref"] != default:
        summary(f"### Not judged\n\nPR #{number} does not target `{esc(default)}`.")
        return 0
    default_sha = api.branch_sha(default)
    try:
        verdict, entry, _compare = check_head(api, pr, default_sha, head_sha)
    except Refused as exc:
        return fail(str(exc))
    if verdict.kind == "other":
        summary(f"### Not a pack contribution — not judged\n\nPR #{number} {esc(verdict.reason)}. "
                "It is left to the repository owner and is never merged automatically.")
        return 0
    assert verdict.code is not None and entry is not None
    data = api.blob(entry["sha"])
    if packrules.git_blob_sha(data) != entry["sha"] or len(data) != int(entry.get("size") or 0):
        return fail("the pack's content does not match its git object id")
    try:
        pack = packrules.parse_pack_bytes(data)
    except packrules.PackError as exc:
        return fail(str(exc))
    problems = packrules.header_problems(pack, verdict.code)
    if problems:
        return fail("\n".join(problems))
    author = (pr.get("user") or {}).get("login", "")
    exists = api.tree_entry(default_sha, f"packs/{verdict.code}") is not None
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "pack.json").write_bytes(data)
    set_output("kind", "contribution")
    set_output("code", verdict.code)
    set_output("app_version", pack["app_version"])
    set_output("blob_sha", entry["sha"])
    set_output("head_sha", head_sha)
    summary(
        f"### Pack <code>{esc(verdict.code)}</code> — {esc(pack['locale']['english_name'])}\n\n"
        f"* PR #{number} by <code>{esc(author)}</code> at <code>{head_sha[:12]}</code>\n"
        f"* {'update of an existing language' if exists else 'new language'}\n"
        f"* made with Turbo EA {esc(pack['app_version'])}\n"
    )
    return 0


# ---------------------------------------------------------------------------
# validate
# ---------------------------------------------------------------------------


LOG_LINES = 2000
VERDICTS = {0: "✅ valid", 1: "❌ invalid"}


def cmd_validate(pack_file: Path, work: Path) -> int:
    """Exit 0 only when turbo-ea's validator exits 0. Its exit 1 is
    "invalid"; anything else (2 = unreadable input, a crash, a timeout) is
    reported as "could not run" and also fails the check."""
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
    lines = output.strip().splitlines()
    with untrusted_output():
        print(origin)
        print("\n".join(lines[:LOG_LINES]))
        if len(lines) > LOG_LINES:
            print(f"… {len(lines) - LOG_LINES} more line(s)")
    shown = "\n".join(lines[:200]) + ("\n… (truncated; more in the job log)" if len(lines) > 200 else "")
    verdict = VERDICTS.get(code, "⚠️ the validator could not run (no verdict)")
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


def within_daily_limits(api: Any, author: str, code: str) -> str | None:
    """Why this merge would exceed the per-day volume limits, or ``None``.
    Fails closed: an API error counts as over the limit."""
    import gh_api
    import time

    since = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 86400))
    try:
        found = api.get("/search/issues", q=f"repo:{api.repo} is:pr is:merged author:{author} merged:>={since}")
        by_author = int(found.get("total_count", 0))
        # `path` is also the name of the client's own argument: pass the query explicitly.
        commits = api.request("GET", api.r("/commits"),
                              params={"path": f"packs/{code}/pack.json", "since": since, "per_page": 100})
        by_language = len(commits) if isinstance(commits, list) else 0
    except (gh_api.ApiError, OSError, ValueError, TypeError) as exc:
        return f"could not check the daily limits ({exc}); the next sweep retries"
    if by_author >= packrules.MAX_MERGES_PER_AUTHOR_PER_DAY:
        return f"{author} already had {by_author} pull requests merged in the last 24 hours"
    if by_language >= packrules.MAX_MERGES_PER_LANGUAGE_PER_DAY:
        return f"the {code} pack already changed {by_language} times in the last 24 hours"
    return None


def wait(reason: str) -> int:
    summary(f"### ⏳ Not merged yet\n\n<pre>{esc(reason)}</pre>\n")
    return 0


def reserve_maintainer(api: Any, code: str, author: str, author_id: int, number: str, default: str) -> str | None:
    """Write ``packs/<code>/MAINTAINERS`` for a new language BEFORE merging.

    Done first so a failure leaves nothing half-done: if this write fails the
    PR is simply not merged (the next run retries); if it succeeds and the
    merge then fails, the next run sees the author as the maintainer and
    merges. Returns an error, or ``None``."""
    import gh_api

    path = f"packs/{code}/MAINTAINERS"
    content = packrules.maintainers_file(code, author, author_id)
    try:
        api.put_file(path, content.encode("utf-8"),
                     f"Reserve the {code} language pack for {author} (#{number})", default, None)
        return None
    except gh_api.ApiError as exc:
        # The write may have been applied before the error came back.
        current = api.file_text(api.branch_sha(default), path)
        if current == content:
            return None
        return f"could not write {path}: {exc}"


def verify_merge(api: Any, merge_sha: Any, parent_sha: str, code: str, blob_sha: str) -> str | None:
    """Why the merge commit is not exactly "the default branch plus this one
    pack file", or ``None``: its only parent is ``parent_sha`` and, level by
    level, only ``packs`` / ``<code>`` / ``pack.json`` differ, the last being
    the validated blob."""
    if not packrules.is_sha(merge_sha):
        return "GitHub returned no merge commit"
    if api.commit_parents(merge_sha) != [parent_sha]:
        return "the merge commit's parent is not the default branch commit it was checked against"
    for path, expected in (("", "packs"), ("packs", code), (f"packs/{code}", "pack.json")):
        before = api.tree_listing(parent_sha, path) or []
        after = api.tree_listing(merge_sha, path) or []
        changed = packrules.differing_entries(before, after)
        if changed != {expected}:
            where = path or "the repository root"
            return f"in {where}, the merge changed {sorted(changed)[:10]} instead of only {expected!r}"
    entry = api.tree_entry(merge_sha, f"packs/{code}/pack.json") or {}
    if entry.get("sha") != blob_sha or entry.get("mode") != packrules.MODE_REGULAR:
        return "the merged pack is not the validated file"
    return None


def cmd_merge() -> int:
    import gh_api

    number, head_sha = pr_inputs()
    code, blob_sha = env("CODE"), env("BLOB_SHA")
    if packrules.code_problem(code) is not None or not packrules.is_sha(blob_sha):
        raise SystemExit("CODE / BLOB_SHA missing or malformed")
    path = f"packs/{code}/pack.json"
    api = client()
    pr = api.get(api.r(f"/pulls/{number}"))
    repo = api.get(api.r(""))
    default = api.default_branch()  # validated branch name
    if pr.get("state") != "open" or pr["head"]["sha"] != head_sha or pr["base"]["ref"] != default:
        summary(f"PR #{number} changed since it was validated (closed, new commits or new base); not merged.")
        return 0
    if pr.get("draft"):
        return wait("the PR is a draft; mark it ready for review to have it merged")
    user = pr.get("user") or {}
    author, author_id = user.get("login", ""), user.get("id")
    head_owner = ((pr["head"].get("repo") or {}).get("owner") or {}).get("login")
    owner = repo.get("owner") or {}
    owner_id = owner.get("id") if owner.get("type") == "User" else None
    default_sha = api.branch_sha(default)

    # Re-derive everything about the head against the current default branch.
    try:
        verdict, entry, compare = check_head(api, pr, default_sha, head_sha)
    except Refused as exc:
        return fail(str(exc), title="No longer a valid contribution")
    if verdict.kind != "contribution" or verdict.path != path or (entry or {}).get("sha") != blob_sha:
        return fail("the PR's content is not the file that was validated", title="Not merged")

    exists = api.tree_entry(default_sha, f"packs/{code}") is not None
    maintainers, ignored = packrules.parse_maintainers(
        api.file_text(default_sha, f"packs/{code}/MAINTAINERS") or "")
    if ignored:
        summary(f"⚠️ packs/{esc(code)}/MAINTAINERS has {ignored} line(s) without a valid "
                "<code>login user-id</code>; they are ignored.")
    reviews, complete = api.paginate_complete(api.r(f"/pulls/{number}/reviews"), limit=1000)
    if not complete:
        return wait("too many reviews to read them all; left for the repository owner")
    decision = packrules.decide(
        author=author, author_id=author_id, head_owner=head_owner, language_exists=exists,
        maintainers=maintainers, reviews=reviews, head_sha=head_sha, owner_id=owner_id,
    )
    if decision.action != "merge":
        return wait(decision.reason)
    limit = within_daily_limits(api, author, code)
    if limit:
        return wait(limit)
    # The squash result must be byte-for-byte the validated file: refuse when
    # the pack on the default branch changed since the PR's merge base (a
    # three-way merge would then produce a file nobody validated).
    merge_base = compare["merge_base_commit"]["sha"]
    if (api.tree_entry(merge_base, path) or {}).get("sha") != (api.tree_entry(default_sha, path) or {}).get("sha"):
        return wait(f"the {code} pack changed on {default} after this PR was branched; rebase the branch "
                    "on the current version (or open a new PR from it) so the checks run on the file "
                    "that would actually be merged")

    if decision.new_language:
        error = reserve_maintainer(api, code, author, author_id, number, default)
        if error:
            return fail(error, title="Not merged: maintainer not recorded")
        default_sha = api.branch_sha(default)  # the reservation commit is the new parent
        summary(f"<code>{esc(author)}</code> is recorded as the maintainer of <code>{esc(code)}</code>.")

    verb = "Add" if decision.new_language else "Update"
    try:
        result = api.request(
            "PUT",
            api.r(f"/pulls/{number}/merge"),
            body={
                "sha": head_sha,
                "merge_method": "squash",
                "commit_title": f"{verb} {code} language pack (#{number})",
                "commit_message": f"Merged automatically: {decision.reason}.",
            },
        )
    except gh_api.ApiError as exc:
        return fail(str(exc), title="Merge refused by GitHub")
    problem = verify_merge(api, result.get("sha"), default_sha, code, blob_sha)
    if problem:
        return fail(f"{problem}. The owner must inspect commit {result.get('sha')!s:.40} on {default}; "
                    "the index was not refreshed.", title="Post-merge check failed")
    summary(f"### ✅ Merged — {esc(decision.reason)}\n")
    try:
        api.request("POST", api.r("/actions/workflows/registry.yml/dispatches"), body={"ref": default})
    except gh_api.ApiError as exc:
        return fail(f"could not start the index update ({exc}); the daily registry run will catch up",
                    title="Index not refreshed yet")
    return 0


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
