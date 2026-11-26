"""Judge and merge language-pack pull requests. Run by .github/workflows/judge.yml.

Subcommands (each reads its inputs from environment variables the workflow
sets with ``env:``, never from interpolated script text):

``resolve``   which open pull requests to judge (sweep / review relay / manual).
``inspect``   read the PR's file list and pack bytes through the API at the exact
              head SHA, apply this repository's rules, write the pack to a file.
              Needs a read-only token.
``validate``  fetch turbo-ea's validator and English files for the pack's
              app_version and run it on that file. No token.
``decide``    check everything against the live PR and default branch, apply the
              maintainer policy, and ask for a review (one edited comment per PR)
              when one is needed. Merges nothing.
``merge``     under the repository-wide lock, re-check everything again, apply
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
        found = api.paginate(api.r(f"/commits/{wanted}/pulls"), limit=100)
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


def language_exists(api: Any, commit_sha: str, code: str) -> bool:
    """A language exists when its ``pack.json`` is on the default branch. A
    MAINTAINERS file alone (a reservation whose merge did not happen) does not
    make it exist: the next attempt is judged as a new language again."""
    return api.tree_entry(commit_sha, f"packs/{code}/pack.json") is not None


def open_prs_of(api: Any, default: str, author_id: Any) -> int | None:
    prs, complete = api.paginate_complete(api.r("/pulls"), limit=1000, state="open", base=default)
    if not complete:
        return None
    return sum(1 for pr in prs if (pr.get("user") or {}).get("id") == author_id)


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
    author_id = (pr.get("user") or {}).get("id")
    count = open_prs_of(api, default, author_id)
    if count is None or count > packrules.MAX_OPEN_PRS_PER_AUTHOR:
        return fail(f"the author has more than {packrules.MAX_OPEN_PRS_PER_AUTHOR} open pull requests here; "
                    "close some (or wait until they are merged) and push again")
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
    exists = language_exists(api, default_sha, verdict.code)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "pack.json").write_bytes(data)
    set_output("kind", "contribution")
    set_output("code", verdict.code)
    set_output("app_version", pack["app_version"])
    set_output("blob_sha", entry["sha"])
    set_output("head_sha", head_sha)
    set_output("new_language", "false" if exists else "true")
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
    """Exit 0 only when turbo-ea's validator exits 0, the ``instructions``
    are the export's own text, and a new language reaches the minimum
    coverage. The validator's exit 1 is "invalid"; anything else (2 =
    unreadable input, a crash, a timeout) is "could not run" and fails too."""
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
    if code != 0:
        return 1
    pack = packrules.parse_pack_bytes(pack_file.read_bytes())
    if "instructions" in pack:
        try:
            expected = turbo_ea.expected_instructions(Path(str(got["english_validator"])),
                                                      pack["locale"]["english_name"])
        except (RuntimeError, ValueError, OSError) as exc:
            return fail(str(exc), title="Could not check the instructions")
        if pack["instructions"] != expected:
            return fail('"instructions" must be exactly the text Turbo EA writes into the export; '
                        "export the pack again instead of editing it")
    return check_coverage(pack_file, Path(str(got["english"])), str(got["english_label"]))


def check_coverage(pack_file: Path, english_dir: Path, english_label: str) -> int:
    """Report the pack's coverage of the English text it was made from, and
    refuse a new language below :data:`packrules.MIN_NEW_LANGUAGE_COVERAGE`.
    Values identical to the English text do not count toward the minimum."""
    import turbo_ea

    pack = packrules.parse_pack_bytes(pack_file.read_bytes())
    stats = packrules.pack_stats(pack, turbo_ea.read_english(english_dir))
    coverage = float(stats.get("coverage_differing", 0.0))
    set_output("coverage", f"{coverage:.1f}")
    set_output("identical", str(int(stats.get("identical_to_english", 0))))
    summary(
        f"### Coverage\n\n{stats['translated']} of {stats['english_strings']} interface strings of "
        f"{esc(english_label)} carry a value; {stats['identical_to_english']} of them are identical to the "
        f"English text, so **{coverage:.1f}%** count as translated. {stats['metamodel_strings']} metamodel "
        "labels.\n"
    )
    if env("NEW_LANGUAGE") == "true" and not coverage >= packrules.MIN_NEW_LANGUAGE_COVERAGE:
        return fail(f"a new language needs at least {packrules.MIN_NEW_LANGUAGE_COVERAGE:.0f}% of the interface "
                    f"strings translated (values copied from English do not count); this pack has "
                    f"{coverage:.1f}%. Translate more in Turbo EA, export again and push the new file.",
                    title="Not enough of the interface translated")
    return 0


# ---------------------------------------------------------------------------
# decide and merge
# ---------------------------------------------------------------------------

HALT_TITLE = "[automerge halted]"
BOT_LOGIN = "github-actions[bot]"


def halted(api: Any) -> str | None:
    """Why automatic merging is switched off, or ``None``: the owner set the
    repository variable ``AUTOMERGE_HALTED``, or a post-merge check failed and
    opened a halt issue that is still open (closing it resumes merging)."""
    if env("AUTOMERGE_HALTED").strip():
        return "automatic merging is halted by the repository variable AUTOMERGE_HALTED"
    issues, complete = api.paginate_complete(api.r("/issues"), limit=1000, state="open", creator=BOT_LOGIN)
    if not complete:
        return "could not read the list of open issues; not merging"
    for issue in issues:
        user = issue.get("user") or {}
        if ("pull_request" not in issue and user.get("login") == BOT_LOGIN
                and str(issue.get("title", "")).startswith(HALT_TITLE)):
            return f"automatic merging is halted until issue #{int(issue.get('number', 0))} is closed"
    return None


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


def ask_for_review(api: Any, number: str, head_sha: str, decision: Any) -> None:
    """Keep ONE comment per PR that mentions who has to approve: posted the
    first time, edited (not re-posted) for later commits. Never fatal."""
    import gh_api

    details = ""
    try:
        coverage = float(env("COVERAGE"))
        identical = int(env("IDENTICAL") or "0")
        details = (f"Coverage: **{coverage:.1f}%** of the interface strings translated "
                   f"({identical} values are identical to the English text and do not count).")
    except ValueError:
        pass
    try:
        body = packrules.waiting_comment(head_sha, decision, details)
        comments, complete = api.paginate_complete(api.r(f"/issues/{number}/comments"), limit=1000)
        if not complete:
            return
        mine = [c for c in comments if (c.get("user") or {}).get("login") == BOT_LOGIN
                and packrules.REVIEW_REQUEST_MARKER in str(c.get("body", ""))]
        if mine:
            if mine[0].get("body") == body:
                return
            api.request("PATCH", api.r(f"/issues/comments/{int(mine[0]['id'])}"), body={"body": body})
        else:
            api.request("POST", api.r(f"/issues/{number}/comments"), body={"body": body})
        summary("Asked " + ", ".join(f"<code>{esc(login)}</code>" for login in decision.ask) + " for a review.")
    except (gh_api.ApiError, OSError, ValueError, KeyError, TypeError) as exc:
        summary(f"Could not post the review request comment: <pre>{esc(exc)}</pre>")


def locale_of(api: Any, commit_sha: str, code: str) -> dict[str, Any] | None:
    entry = api.tree_entry(commit_sha, f"packs/{code}/pack.json")
    if entry is None or entry.get("type") != "blob" or int(entry.get("size") or 0) > packrules.MAX_PACK_BYTES:
        return None
    try:
        locale = packrules.parse_pack_bytes(api.blob(entry["sha"])).get("locale")
    except packrules.PackError:
        return None
    return locale if isinstance(locale, dict) else None


class Plan:
    """What :func:`evaluate` found for a PR that should merge now."""

    def __init__(self, **kw: Any):
        self.__dict__.update(kw)


def evaluate(api: Any, number: str, head_sha: str, code: str, blob_sha: str, *, comment: bool) -> Plan | int:
    """Every check before a merge, against the live PR and default branch:
    returns a :class:`Plan` when the PR should be merged now, else the exit
    status after reporting why not (and, with ``comment``, asking whoever has
    to approve). Writes nothing else."""
    path = f"packs/{code}/pack.json"
    stop = halted(api)
    if stop:
        return wait(stop)
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
    owner_login = owner.get("login") if owner_id is not None else None
    default_sha = api.branch_sha(default)

    # Re-derive everything about the head against the current default branch.
    try:
        verdict, entry, compare = check_head(api, pr, default_sha, head_sha)
    except Refused as exc:
        return fail(str(exc), title="No longer a valid contribution")
    if verdict.kind != "contribution" or verdict.path != path or (entry or {}).get("sha") != blob_sha:
        return fail("the PR's content is not the file that was validated", title="Not merged")

    exists = language_exists(api, default_sha, code)
    locale_changed = False
    if exists:
        current, proposed = locale_of(api, default_sha, code), locale_of(api, head_sha, code)
        locale_changed = current is None or proposed is None or any(
            current.get(k) != proposed.get(k) for k in ("name", "english_name", "rtl"))
    maintainers, ignored = packrules.parse_maintainers(
        api.file_text(default_sha, f"packs/{code}/MAINTAINERS") or "") if exists else ([], 0)
    if ignored:
        summary(f"⚠️ packs/{esc(code)}/MAINTAINERS has {ignored} line(s) without a valid "
                "<code>login user-id</code>; they are ignored.")
    reviews, complete = api.paginate_complete(api.r(f"/pulls/{number}/reviews"), limit=1000)
    if not complete:
        return wait("too many reviews to read them all; left for the repository owner")
    decision = packrules.decide(
        author=author, author_id=author_id, head_owner=head_owner, language_exists=exists,
        maintainers=maintainers, reviews=reviews, head_sha=head_sha, owner_id=owner_id,
        owner_login=owner_login, locale_changed=locale_changed,
    )
    if decision.action != "merge":
        if comment and decision.ask:
            ask_for_review(api, number, head_sha, decision)
        return wait(decision.reason)
    if decision.new_language:
        try:
            coverage = float(env("COVERAGE"))
        except ValueError:
            coverage = float("nan")
        if not coverage >= packrules.MIN_NEW_LANGUAGE_COVERAGE:  # NaN fails too
            return wait(f"a new language needs at least {packrules.MIN_NEW_LANGUAGE_COVERAGE:.0f}% coverage "
                        "measured by this run's validation")
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
    return Plan(pr=pr, default=default, author=author, author_id=author_id, owner_login=owner_login,
                decision=decision)


def cmd_decide() -> int:
    """The read-mostly half: decide, and ask for reviews. Outputs
    ``action=merge`` only when the merge job (which repeats every check under
    the repository-wide lock) should run."""
    number, head_sha = pr_inputs()
    code, blob_sha = env("CODE"), env("BLOB_SHA")
    if packrules.code_problem(code) is not None or not packrules.is_sha(blob_sha):
        raise SystemExit("CODE / BLOB_SHA missing or malformed")
    set_output("action", "wait")
    result = evaluate(client(), number, head_sha, code, blob_sha, comment=True)
    if isinstance(result, Plan):
        set_output("action", "merge")
        summary(f"Ready to merge: {esc(result.decision.reason)}")
        return 0
    return result


def reserve_maintainer(api: Any, code: str, author: str, author_id: int, number: str, default: str) -> str | None:
    """Write ``packs/<code>/MAINTAINERS`` for a new language BEFORE merging.

    If this write fails the PR is not merged (the next run retries). If it
    succeeds and the merge then fails, nothing is granted: the language only
    exists once its pack.json is on the default branch, so the next attempt
    still needs the owner's approval and the minimum coverage. A reservation
    for another author is never overwritten. Returns an error, or ``None``."""
    import gh_api

    path = f"packs/{code}/MAINTAINERS"
    content = packrules.maintainers_file(code, author, author_id)
    current = api.file_text(api.branch_sha(default), path)
    if current is not None:
        if packrules.parse_maintainers(current)[0] == packrules.parse_maintainers(content)[0]:
            return None
        return f"{path} already exists for someone else; left for the owner"
    try:
        api.put_file(path, content.encode("utf-8"),
                     f"Reserve the {code} language pack for {author} (#{number})", default, None)
        return None
    except gh_api.ApiError as exc:
        # The write may have been applied before the error came back.
        if api.file_text(api.branch_sha(default), path) == content:
            return None
        return f"could not write {path}: {exc}"


def verify_merge(api: Any, merge_sha: Any, code: str, blob_sha: str) -> str | None:
    """Why the merge commit is not exactly "its parent plus this one pack
    file", or ``None``: it has exactly one parent and, level by level, only
    ``packs`` / ``<code>`` / ``pack.json`` differ from that parent, the last
    being the validated blob. (Compared with the actual parent, so an index
    commit landing just before the merge is not a false alarm.)"""
    if not packrules.is_sha(merge_sha):
        return "GitHub returned no merge commit"
    parents = api.commit_parents(merge_sha)
    if len(parents) != 1 or not packrules.is_sha(parents[0]):
        return "the merge commit does not have exactly one parent"
    parent = parents[0]
    for path, expected in (("", "packs"), ("packs", code), (f"packs/{code}", "pack.json")):
        before = api.tree_listing(parent, path) or []
        after = api.tree_listing(merge_sha, path) or []
        changed = packrules.differing_entries(before, after)
        if changed != {expected}:
            where = path or "the repository root"
            return f"in {where}, the merge changed {sorted(changed)[:10]} instead of only {expected!r}"
    entry = api.tree_entry(merge_sha, f"packs/{code}/pack.json") or {}
    if entry.get("sha") != blob_sha or entry.get("mode") != packrules.MODE_REGULAR:
        return "the merged pack is not the validated file"
    return None


def halt(api: Any, number: str, merge_sha: Any, problem: str, owner_login: Any) -> None:
    """Open the issue that stops all automatic merging until the owner closes
    it, mentioning the owner. Best effort: the job fails loudly either way."""
    import gh_api

    who = f"@{owner_login} " if packrules.is_login(owner_login) else ""
    body = (f"{who}the post-merge check of PR #{int(number)} failed: {problem}.\n\n"
            f"Commit: `{merge_sha!s:.40}`. Automatic merging is halted until this issue is closed. "
            "Inspect the commit (revert it if needed), then close this issue.")
    try:
        api.request("POST", api.r("/issues"), body={"title": f"{HALT_TITLE} post-merge check failed (#{int(number)})",
                                                     "body": body})
    except (gh_api.ApiError, OSError) as exc:
        summary(f"Could not open the halt issue: <pre>{esc(exc)}</pre>")


def merge_pr(api: Any, number: str, head_sha: str, title: str, message: str) -> tuple[str | None, str | None]:
    """``(merge commit sha, error)``. Retries GitHub's "base branch was
    modified" refusals while the head is unchanged; after a network error,
    asks GitHub whether the merge happened anyway."""
    import gh_api
    import time

    body = {"sha": head_sha, "merge_method": "squash", "commit_title": title, "commit_message": message}
    for attempt in range(4):
        try:
            result = api.request("PUT", api.r(f"/pulls/{number}/merge"), body=body)
            return result.get("sha"), None
        except gh_api.ApiError as exc:
            if exc.status not in (405, 409) or attempt == 3:
                return None, str(exc)
            time.sleep(3 * (attempt + 1))
            pr = api.get(api.r(f"/pulls/{number}"))
            if pr.get("state") != "open" or pr["head"]["sha"] != head_sha:
                return None, "the PR changed while merging"
        except OSError as exc:
            pr = api.get(api.r(f"/pulls/{number}"))
            if pr.get("merged") and packrules.is_sha(pr.get("merge_commit_sha")):
                return pr["merge_commit_sha"], None
            return None, f"network error while merging: {exc}"
    return None, "merge not completed"


def cmd_merge() -> int:
    import gh_api

    number, head_sha = pr_inputs()
    code, blob_sha = env("CODE"), env("BLOB_SHA")
    if packrules.code_problem(code) is not None or not packrules.is_sha(blob_sha):
        raise SystemExit("CODE / BLOB_SHA missing or malformed")
    api = client()
    plan = evaluate(api, number, head_sha, code, blob_sha, comment=False)
    if not isinstance(plan, Plan):
        return plan
    decision = plan.decision
    if decision.new_language:
        error = reserve_maintainer(api, code, plan.author, plan.author_id, number, plan.default)
        if error:
            return fail(error, title="Not merged: maintainer not recorded")
        summary(f"<code>{esc(plan.author)}</code> is recorded as the maintainer of <code>{esc(code)}</code>.")
    verb = "Add" if decision.new_language else "Update"
    merge_sha, error = merge_pr(api, number, head_sha, f"{verb} {code} language pack (#{number})",
                                f"Merged automatically: {decision.reason}.")
    if error:
        return fail(error, title="Merge refused by GitHub")
    problem = verify_merge(api, merge_sha, code, blob_sha)
    if problem:
        halt(api, number, merge_sha, problem, plan.owner_login)
        return fail(f"{problem}. Automatic merging is halted until the owner closes the halt issue; "
                    f"inspect commit {merge_sha!s:.40} on {plan.default}.", title="Post-merge check failed")
    summary(f"### ✅ Merged — {esc(decision.reason)}\n")
    try:
        api.request("POST", api.r("/actions/workflows/registry.yml/dispatches"), body={"ref": plan.default})
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
    sub.add_parser("decide")
    sub.add_parser("merge")
    args = parser.parse_args(argv)
    if args.command == "resolve":
        return cmd_resolve()
    if args.command == "inspect":
        return cmd_inspect(args.out)
    if args.command == "validate":
        return cmd_validate(args.pack, args.work)
    if args.command == "decide":
        return cmd_decide()
    return cmd_merge()


if __name__ == "__main__":
    sys.exit(main())
