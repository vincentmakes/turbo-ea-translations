"""The rules that judge a language-pack contribution, without any I/O.

Everything here is pure (no network, no filesystem, no subprocess) so it can be
unit-tested and so the workflow scripts that do the I/O stay thin. Every value
this module receives may come from a pull request and is treated as untrusted.

Standard library only, Python 3.10+.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PACK_FORMAT = "turbo-ea-language-pack"
PACK_FORMAT_VERSION = 1
#: The only export mode accepted here: the app's "Share (community pack)".
SHARE_MODE = "share"
#: The top-level keys of a format_version 1 pack; anything else is refused.
PACK_KEYS = frozenset(
    {"format", "format_version", "app_version", "locale", "mode", "instructions",
     "strings", "source_hashes"}
)
LOCALE_KEYS = frozenset({"code", "name", "english_name", "rtl"})

#: Languages Turbo EA ships itself; a community pack can never use these codes.
BUILT_IN_CODES = frozenset({"en", "de", "fr", "es", "it", "pt", "zh", "ru", "da", "ar"})
#: Deprecated base codes i18next rewrites to another code (same list as the app).
DEPRECATED_BASE_CODES = frozenset({"iw", "in", "ji", "jw", "mo", "sh", "tl", "cmn"})
MAX_CODE_LENGTH = 10
_CODE = re.compile(r"[a-z]{2,3}(?:-[A-Z][a-z]{3})?(?:-[A-Z]{2})?")

#: ``x.y.z`` with no leading zeros and bounded digit counts; nothing else (no
#: ``v`` prefix, no suffix, no whitespace) ever reaches a git ref or a URL.
_VERSION = re.compile(r"(0|[1-9][0-9]{0,3})\.(0|[1-9][0-9]{0,4})\.(0|[1-9][0-9]{0,5})")
_SHA = re.compile(r"[0-9a-f]{40}")
_HASH12 = re.compile(r"[0-9a-f]{12}")
#: A GitHub login: letters, digits and single inner hyphens, at most 39 chars.
_LOGIN = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9]|-(?=[A-Za-z0-9])){0,38}")
_ENGLISH_NAME = re.compile(r"[A-Za-z][A-Za-z ()\-]{1,59}")
_PR_NUMBER = re.compile(r"[1-9][0-9]{0,8}")

#: The one path a contribution may change. The code group is the same pattern
#: as :data:`_CODE`, so ``..``, ``/``, ``\`` or a NUL can never match.
_PACK_PATH = re.compile(r"packs/([a-z]{2,3}(?:-[A-Z][a-z]{3})?(?:-[A-Z]{2})?)/pack\.json")

#: A complete real pack is about 1 MB (2 MB in a script that takes 3 UTF-8
#: bytes per character); the cap leaves room without inviting bulk data.
MAX_PACK_BYTES = 3 * 1024 * 1024
#: The built-in metamodel has a few hundred labels; invented keys beyond that
#: would only be dead weight in every installation.
MAX_METAMODEL_KEYS = 5000
#: Commits a contribution PR may carry; each must have exactly one parent so
#: the PR's history is a straight line from one commit of the default branch
#: (one merge base: what is diffed is what a squash merge applies).
MAX_PR_COMMITS = 100
#: Automatic merges per author and per language in 24 hours.
MAX_MERGES_PER_AUTHOR_PER_DAY = 10
MAX_MERGES_PER_LANGUAGE_PER_DAY = 5
#: Deepest JSON nesting a pack needs is 3 (pack > strings > namespace); the
#: limit leaves room and keeps the parser far from recursion limits.
MAX_JSON_DEPTH = 8
MAX_KEY_LENGTH = 1000
MAX_NAMESPACES = 50
MAX_PROBLEMS = 50
MAX_NATIVE_NAME_LENGTH = 60

#: Git file modes. Only a regular, non-executable file is a pack.
MODE_REGULAR = "100644"

_BIDI_OVERRIDES = frozenset({"\u202d", "\u202e"})
_INVISIBLE = frozenset(
    "\u00ad\u200b\u200c\u200d\u200e\u200f\u2060\u2066\u2067\u2068\u2069\ufeff"
)


# ---------------------------------------------------------------------------
# Small validators
# ---------------------------------------------------------------------------


#: The validator's own patterns for markup and links (language_pack.py): what
#: HTML reads as a tag opening, and a URL by scheme, ``www.`` or ``//host.tld``.
_TAG_OPEN = re.compile(r"<[A-Za-z/!?]")
_URL = re.compile(r"(?:https?://|www\.|(?<![:/\w])//[A-Za-z0-9\-]+\.[A-Za-z0-9\-])", re.IGNORECASE)


def is_plain_text(text: str) -> bool:
    """No tag opening, no link, no control character or bidi override: the
    rules the validator applies to ``strings``, for the free-text header
    fields it does not look at (``locale.name``, ``instructions``)."""
    return not (_TAG_OPEN.search(text) or _URL.search(text) or has_forbidden_chars(text))


def is_user_id(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 0 < value < 10**12


def code_problem(code: Any) -> str | None:
    """Why ``code`` cannot be a community language code, or ``None``."""
    if not isinstance(code, str) or len(code) > MAX_CODE_LENGTH or not _CODE.fullmatch(code):
        return "invalid_code"
    if code.split("-")[0] in DEPRECATED_BASE_CODES:
        return "invalid_code"
    if code in BUILT_IN_CODES:
        return "built_in_code"
    return None


def is_version(value: Any) -> bool:
    return isinstance(value, str) and bool(_VERSION.fullmatch(value))


def is_sha(value: Any) -> bool:
    return isinstance(value, str) and bool(_SHA.fullmatch(value))


def is_login(value: Any) -> bool:
    return isinstance(value, str) and bool(_LOGIN.fullmatch(value))


def is_pr_number(value: Any) -> bool:
    return isinstance(value, str) and bool(_PR_NUMBER.fullmatch(value))


def has_forbidden_chars(text: str) -> bool:
    """Control characters (but newline and tab), lone surrogates, bidi overrides."""
    for ch in text:
        if ch in _BIDI_OVERRIDES:
            return True
        category = unicodedata.category(ch)
        if category == "Cs" or (category == "Cc" and ch not in "\n\t"):
            return True
    return False


def is_valid_native_name(name: Any) -> bool:
    return (
        isinstance(name, str)
        and 0 < len(name) <= MAX_NATIVE_NAME_LENGTH
        and not all(ch.isspace() or ch in _INVISIBLE for ch in name)
        and "\n" not in name
        and "\t" not in name
        and is_plain_text(name)
    )


def is_valid_english_name(name: Any) -> bool:
    return isinstance(name, str) and bool(_ENGLISH_NAME.fullmatch(name))


def turbo_ea_refs(app_version: str) -> list[str]:
    """The turbo-ea git refs to try, in order, for a pack's ``app_version``:
    the release tag (``v`` prefixed, as turbo-ea names them, then bare), and
    ``main`` as the fallback for a version not tagged yet.

    Refuses (``ValueError``) anything that is not a strict ``x.y.z``."""
    if not is_version(app_version):
        raise ValueError("app_version is not a plain x.y.z version")
    return [f"refs/tags/v{app_version}", f"refs/tags/{app_version}", "refs/heads/main"]


def git_blob_sha(data: bytes) -> str:
    """The git object id of a blob holding ``data``."""
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


# ---------------------------------------------------------------------------
# The changed-file rule
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ChangeVerdict:
    """What a pull request's file list makes of it.

    ``kind`` is ``contribution`` (exactly one ``packs/<code>/pack.json`` added
    or modified), ``invalid`` (it touches a pack file but breaks the rule: the
    contributor has to fix it) or ``other`` (it touches no pack file at all —
    a MAINTAINERS change, docs, scripts: work for the owner, not judged here
    and never merged automatically)."""

    kind: str
    code: str | None = None
    path: str | None = None
    reason: str = ""


def classify_changes(files: list[Mapping[str, Any]], total_files: int | None = None) -> ChangeVerdict:
    """Apply the contribution rule to a compare/PR file list.

    ``files`` holds ``{"filename", "status", "previous_filename"?}`` entries as
    the GitHub API returns them; ``total_files`` is the count the API reports
    when it may have truncated the list."""
    names: list[str] = []
    for entry in files:
        for field in ("filename", "previous_filename"):
            value = entry.get(field)
            if isinstance(value, str):
                names.append(value)
    count = max(len(files), total_files or 0)
    touches_a_pack = any(n.startswith("packs/") and "pack.json" in n.lower() for n in names)
    if not touches_a_pack:
        return ChangeVerdict("other", reason="changes no pack.json; not a pack contribution")
    if count != 1 or len(files) != 1:
        return ChangeVerdict(
            "invalid",
            reason=f"a contribution changes exactly one file, packs/<code>/pack.json; this one changes {count}",
        )
    entry = files[0]
    status = entry.get("status")
    filename = entry.get("filename")
    if status not in ("added", "modified"):
        return ChangeVerdict("invalid", reason=f"the pack file must be added or modified, not {status!s:.20}")
    if entry.get("previous_filename") is not None:
        return ChangeVerdict("invalid", reason="renamed files are not accepted")
    match = _PACK_PATH.fullmatch(filename) if isinstance(filename, str) else None
    if match is None:
        return ChangeVerdict(
            "invalid",
            reason="the only file a contribution may change is packs/<code>/pack.json "
            "(MAINTAINERS, index.json and everything else are maintained by automation or the owner)",
        )
    code = match.group(1)
    problem = code_problem(code)
    if problem is not None:
        return ChangeVerdict("invalid", reason=f"folder name is not an accepted language code ({problem})")
    return ChangeVerdict("contribution", code=code, path=filename)


def history_problem(compare: Mapping[str, Any]) -> str | None:
    """Why a PR's commits (from a ``compare`` response) are not a straight
    line, or ``None``.

    Every commit must have exactly one parent, and the list must be complete.
    With a merge commit in the PR, the head can have several merge bases with
    the default branch; GitHub's three-dot diff then shows one base's view
    while the squash merge uses a virtual base and can silently revert or
    delete files the diff never showed."""
    total = compare.get("total_commits")
    commits = compare.get("commits")
    if not isinstance(total, int) or not isinstance(commits, list):
        return "the PR's commit list is unavailable"
    if total > MAX_PR_COMMITS:
        return f"the PR has {total} commits; at most {MAX_PR_COMMITS} (squash them, or open a new PR)"
    if len(commits) != total:
        return "the PR's commit list is incomplete"
    for commit in commits:
        parents = commit.get("parents") if isinstance(commit, Mapping) else None
        if not isinstance(parents, list) or len(parents) != 1:
            return ("the PR contains a merge commit; update your branch by rebasing "
                    "(or open a new PR from the current default branch), not with a merge")
    return None


def differing_entries(a: Iterable[Mapping[str, Any]], b: Iterable[Mapping[str, Any]]) -> set[str]:
    """Names whose entry (mode, type, object id) differs between two git tree
    listings, including names present in only one of them."""
    def index(entries: Iterable[Mapping[str, Any]]) -> dict[str, tuple[Any, Any, Any]]:
        return {e.get("path"): (e.get("mode"), e.get("type"), e.get("sha")) for e in entries}

    ia, ib = index(a), index(b)
    return {name for name in set(ia) | set(ib) if ia.get(name) != ib.get(name)}


# ---------------------------------------------------------------------------
# Reading a pack safely
# ---------------------------------------------------------------------------


def json_depth_ok(text: str, limit: int = MAX_JSON_DEPTH) -> bool:
    """Whether no array/object in ``text`` nests deeper than ``limit``.

    A linear scan that skips string contents, run before ``json.loads`` so a
    hostile file cannot drive the parser into deep recursion."""
    depth = 0
    in_string = False
    escaped = False
    for ch in text:
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
        elif ch == '"':
            in_string = True
        elif ch in "[{":
            depth += 1
            if depth > limit:
                return False
        elif ch in "]}":
            depth -= 1
    return True


class PackError(ValueError):
    """The file is not a pack this repository accepts."""


def _no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise PackError(f"duplicate JSON key {json.dumps(key, ensure_ascii=True)[:80]}")
        out[key] = value
    return out


def parse_pack_bytes(data: bytes) -> dict[str, Any]:
    """Size cap, strict UTF-8, depth cap, duplicate-key refusal, then parse."""
    if len(data) > MAX_PACK_BYTES:
        raise PackError(f"pack is {len(data)} bytes; the limit is {MAX_PACK_BYTES}")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PackError("pack is not valid UTF-8") from exc
    if text.startswith("\ufeff"):
        # turbo-ea's validator refuses a byte-order mark too (it cannot read
        # the file); a fresh export never has one.
        raise PackError("pack starts with a byte-order mark; save it as UTF-8 without BOM")
    if not json_depth_ok(text):
        raise PackError(f"JSON nests deeper than {MAX_JSON_DEPTH} levels")
    try:
        pack = json.loads(text, object_pairs_hook=_no_duplicates)
    except PackError:
        raise
    except (ValueError, RecursionError) as exc:
        raise PackError(f"not valid JSON: {str(exc)[:200]}") from exc
    if not isinstance(pack, dict):
        raise PackError("a pack is a JSON object")
    return pack


def header_problems(pack: Mapping[str, Any], folder_code: str | None) -> list[str]:
    """What this repository requires of a pack beyond turbo-ea's own validator:
    the header fields, the ``share`` mode, the folder name and the shape of
    ``strings``/``source_hashes``. Returns human-readable problems."""
    problems: list[str] = []
    extra = sorted(set(pack) - PACK_KEYS)
    if extra:
        problems.append("unknown top-level key(s): " + ", ".join(json.dumps(k)[:40] for k in extra[:5]))
    if pack.get("format") != PACK_FORMAT:
        problems.append(f'"format" must be "{PACK_FORMAT}"')
    if type(pack.get("format_version")) is not int or pack.get("format_version") != PACK_FORMAT_VERSION:
        problems.append(f'"format_version" must be {PACK_FORMAT_VERSION}')
    if not is_version(pack.get("app_version")):
        problems.append('"app_version" must be a plain version such as "2.214.0"')
    if pack.get("mode") != SHARE_MODE:
        problems.append(
            '"mode" must be "share": export the pack with "Share (community pack)" in '
            "Admin > Settings > Custom Languages"
        )
    instructions = pack.get("instructions")
    if instructions is not None and not (
        isinstance(instructions, str) and len(instructions) <= 2000 and is_plain_text(instructions)
    ):
        problems.append('"instructions" must be the plain text of the export: at most 2000 characters, '
                        "no markup, no links, no control characters")
    locale = pack.get("locale")
    if not isinstance(locale, dict):
        problems.append('"locale" must be an object')
        locale = {}
    else:
        extra_locale = sorted(set(locale) - LOCALE_KEYS)
        if extra_locale:
            problems.append("unknown locale key(s): " + ", ".join(json.dumps(k)[:40] for k in extra_locale[:5]))
    code = locale.get("code")
    problem = code_problem(code)
    if problem == "built_in_code":
        problems.append(f"locale.code {code!r} is a language Turbo EA ships itself")
    elif problem is not None:
        problems.append("locale.code is not a valid language code")
    elif folder_code is not None and code != folder_code:
        problems.append(f"locale.code {code!r} does not match the folder packs/{folder_code}/")
    if not is_valid_native_name(locale.get("name")):
        problems.append("locale.name must be the language's own name: one line, at most 60 characters, "
                        "no markup or links")
    if not is_valid_english_name(locale.get("english_name")):
        problems.append("locale.english_name must be 2-60 letters, spaces, brackets or dashes")
    if not isinstance(locale.get("rtl"), bool):
        problems.append("locale.rtl must be true or false")
    for section in ("strings", "source_hashes"):
        value = pack.get(section)
        if not isinstance(value, dict):
            problems.append(f'"{section}" must be an object of namespaces')
            continue
        for ns, entries in value.items():
            if not isinstance(entries, dict):
                problems.append(f'"{section}".{json.dumps(ns)[:40]} must be an object')
                continue
            for key, item in entries.items():
                if len(key) > MAX_KEY_LENGTH:
                    problems.append(f'a key in "{section}".{json.dumps(ns)[:40]} is longer than {MAX_KEY_LENGTH}')
                    break
                if section == "source_hashes" and not (isinstance(item, str) and _HASH12.fullmatch(item)):
                    problems.append(f'"source_hashes".{json.dumps(ns)[:40]} holds a value that is not a 12-hex hash')
                    break
        if isinstance(value, dict) and len(value) > MAX_NAMESPACES:
            problems.append(f'"{section}" has {len(value)} namespaces; Turbo EA has fewer than {MAX_NAMESPACES}')
    strings = pack.get("strings")
    if isinstance(strings, dict) and not any(isinstance(v, dict) and v for v in strings.values()):
        problems.append('"strings" is empty')
    metamodel = strings.get("metamodel") if isinstance(strings, dict) else None
    if isinstance(metamodel, dict) and len(metamodel) > MAX_METAMODEL_KEYS:
        problems.append(f'"strings".metamodel has {len(metamodel)} keys; at most {MAX_METAMODEL_KEYS}')
    if len(problems) > MAX_PROBLEMS:
        problems = problems[:MAX_PROBLEMS] + [f"… and {len(problems) - MAX_PROBLEMS} more"]
    return problems


# ---------------------------------------------------------------------------
# Maintainers and the merge decision
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Maintainer:
    """A language's maintainer. ``user_id`` is GitHub's numeric account id and
    is what identifies them: a login can be renamed (the role stays with the
    account) and a freed login can be registered by someone else (who gets
    nothing). ``login`` is kept for display."""

    login: str
    user_id: int

    def is_user(self, user_id: Any) -> bool:
        return is_user_id(user_id) and user_id == self.user_id


def parse_maintainers(text: str) -> tuple[list[Maintainer], int]:
    """``(maintainers, ignored)`` from a MAINTAINERS file: one per line as
    ``login <numeric user id>``; ``#`` starts a comment, blank lines are
    skipped, a leading ``@`` is tolerated. A line without a valid id, or any
    other malformed line, is ignored and counted in ``ignored`` so the
    check's summary can point it out."""
    found: list[Maintainer] = []
    seen: set[int] = set()
    ignored = 0
    for raw in text.splitlines():
        parts = raw.split("#", 1)[0].split()
        if not parts:
            continue
        login = parts[0][1:] if parts[0].startswith("@") else parts[0]
        uid = parts[1] if len(parts) == 2 else ""
        if not (is_login(login) and uid.isascii() and uid.isdigit() and len(uid) <= 12 and int(uid) > 0):
            ignored += 1
            continue
        if int(uid) not in seen:
            seen.add(int(uid))
            found.append(Maintainer(login, int(uid)))
    return found, ignored


def maintainers_file(code: str, login: str, user_id: int) -> str:
    if code_problem(code) is not None or not is_login(login) or not is_user_id(user_id):
        raise ValueError("bad code, login or user id")
    return (
        f"# Maintainers of the {code} language pack, one per line: GitHub login and numeric user id.\n"
        "# The id is what counts (https://api.github.com/users/<login> shows it); a line without one is ignored.\n"
        "# A pull request from a maintainer merges automatically once it passes the checks;\n"
        "# any other change to this pack needs an approving review from a maintainer.\n"
        "# Changes to this file are made by the repository owner.\n"
        f"{login} {user_id}\n"
    )


def latest_reviews(reviews: Iterable[Mapping[str, Any]]) -> dict[int, Mapping[str, Any]]:
    """Each reviewer's last state-changing review, by numeric user id.
    Plain comments do not change a reviewer's verdict."""
    latest: dict[int, Mapping[str, Any]] = {}
    for review in reviews:
        user = review.get("user")
        uid = user.get("id") if isinstance(user, Mapping) else None
        if not is_user_id(uid) or review.get("state") not in ("APPROVED", "CHANGES_REQUESTED", "DISMISSED"):
            continue
        latest[uid] = review
    return latest


@dataclass(frozen=True)
class Decision:
    action: str  # "merge" | "wait" | "skip"
    reason: str
    new_language: bool = False


def decide(
    *,
    author: Any,
    author_id: Any,
    head_owner: Any,
    language_exists: bool,
    maintainers: list[Maintainer],
    reviews: Iterable[Mapping[str, Any]],
    head_sha: str,
    owner_id: Any = None,
) -> Decision:
    """The merge policy for a contribution that passed validation.

    * New language (no ``packs/<code>/`` on the default branch): merge; the
      author becomes its first maintainer.
    * Existing language, author is a maintainer: merge (even if a
      co-maintainer asked for changes: maintainers are peers).
    * Existing language, otherwise: merge once a maintainer other than the
      author has approved this exact head commit and no maintainer's latest
      review requests changes; else wait.

    The repository owner (``owner_id``) counts as a maintainer of every
    language. Merging on the author's own standing needs the PR's branch to
    live in the author's own fork (``head_owner``): otherwise whoever can push
    to that branch would get their commits merged under the author's name."""
    if not is_login(author) or not is_user_id(author_id):
        return Decision("skip", "the author is not a plain GitHub user account; left for the owner")
    own_branch = isinstance(head_owner, str) and head_owner.lower() == author.lower()
    if not language_exists:
        if not own_branch:
            return Decision("wait", "a new language must come from a branch in the author's own fork; "
                            "left for the owner")
        return Decision("merge", "new language: merges automatically, the author becomes its maintainer", True)
    deciders = {m.user_id: m.login for m in maintainers}
    if is_user_id(owner_id):
        deciders.setdefault(owner_id, "the repository owner")
    if own_branch and author_id in deciders:
        return Decision("merge", f"{author} maintains this language")
    approvers: list[str] = []
    for uid, review in latest_reviews(reviews).items():
        if uid == author_id or uid not in deciders:
            continue
        if review.get("state") == "CHANGES_REQUESTED":
            return Decision("wait", f"maintainer {deciders[uid]} requested changes")
        if review.get("state") == "APPROVED" and review.get("commit_id") == head_sha:
            approvers.append(deciders[uid])
    if approvers:
        return Decision("merge", "approved by maintainer " + ", ".join(sorted(approvers)))
    if not maintainers:
        return Decision("wait", "this language has no maintainer listed; waiting for the repository owner")
    return Decision("wait", "waiting for an approving review of the latest commit from a maintainer: "
                    + ", ".join(sorted(m.login for m in maintainers)))


# ---------------------------------------------------------------------------
# Index
# ---------------------------------------------------------------------------


def flatten_english(tree: Mapping[str, Any], prefix: str = "") -> dict[str, str]:
    """``{flat key: text}`` of an English locale file (string leaves only)."""
    out: dict[str, str] = {}
    for key, value in tree.items():
        path = f"{prefix}.{key}" if prefix else key
        if isinstance(value, Mapping):
            out.update(flatten_english(value, path))
        elif isinstance(value, str):
            out[path] = value
    return out


def source_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def pack_stats(pack: Mapping[str, Any], english: Mapping[str, Mapping[str, str]] | None) -> dict[str, Any]:
    """String counts of a pack, and its coverage of ``english``
    (``{namespace: {flat key: text}}`` of the version compared with).

    ``translated`` counts English UI keys the pack carries; ``current`` those
    whose source hash still equals the English text's; ``metamodel_strings``
    is reported apart (its English lives in each installation's database)."""
    strings = pack.get("strings") if isinstance(pack.get("strings"), Mapping) else {}
    hashes = pack.get("source_hashes") if isinstance(pack.get("source_hashes"), Mapping) else {}
    ui = sum(len(v) for k, v in strings.items() if k != "metamodel" and isinstance(v, Mapping))
    meta = len(strings.get("metamodel") or {}) if isinstance(strings.get("metamodel"), Mapping) else 0
    stats: dict[str, Any] = {"strings": ui + meta, "ui_strings": ui, "metamodel_strings": meta}
    if english is None:
        return stats
    total = sum(len(ns) for ns in english.values())
    translated = current = 0
    for ns_name, ns in english.items():
        entries = strings.get(ns_name)
        ns_hashes = hashes.get(ns_name) if isinstance(hashes.get(ns_name), Mapping) else {}
        if not isinstance(entries, Mapping):
            continue
        for key, text in ns.items():
            value = entries.get(key)
            if isinstance(value, str) and value:
                translated += 1
                if ns_hashes.get(key) == source_hash(text):
                    current += 1
    stats.update(
        {
            "english_strings": total,
            "translated": translated,
            "current": current,
            "coverage": round(100.0 * translated / total, 1) if total else 0.0,
        }
    )
    return stats
