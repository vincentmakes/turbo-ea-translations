"""Fetch turbo-ea's own pack validator and English locale files, and run it.

Only ever from https://github.com/vincentmakes/turbo-ea: the validator from the
latest ``v<x.y.z>`` release that has it (else ``main``), the English files at
the release tag of the pack's strictly validated ``app_version`` (``v<x.y.z>``
or ``<x.y.z>``, else ``main``), or both at a full commit SHA the repository
owner pins. The checkout is sparse: the validator file and
``frontend/src/i18n/locales/en/*.json`` only.

The validator runs as a separate ``python -I -S`` process with an empty
environment (no token, no runner variables) in an empty working directory.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import packrules  # noqa: E402

REPO_URL = "https://github.com/vincentmakes/turbo-ea.git"
VALIDATOR_PATH = "backend/app/services/language_pack.py"
ENGLISH_PATH = "frontend/src/i18n/locales/en"
_GIT_ENV = {
    "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
    "HOME": tempfile.gettempdir(),
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "LC_ALL": "C",
}
# Keep the proxy settings of the machine (contributors behind one), nothing else.
for _name in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "NO_PROXY", "no_proxy",
              "GIT_SSL_CAINFO", "SSL_CERT_FILE"):
    if _name in os.environ:
        _GIT_ENV[_name] = os.environ[_name]


def _git(*args: str, cwd: Path | None = None) -> str:
    result = subprocess.run(
        ["git", *args], cwd=cwd, env=_GIT_ENV, capture_output=True, text=True, timeout=300
    )
    if result.returncode != 0:
        raise RuntimeError(f"git {args[0]} failed: {result.stderr.strip()[:500]}")
    return result.stdout


def parse_ls_remote(output: str) -> dict[str, str]:
    """``{ref: commit sha}`` from ``git ls-remote`` output; an annotated tag's
    peeled ``^{}`` line wins over the tag object."""
    refs: dict[str, str] = {}
    peeled: dict[str, str] = {}
    for line in output.splitlines():
        sha, _, ref = line.partition("\t")
        if not packrules.is_sha(sha):
            continue
        if ref.endswith("^{}"):
            peeled[ref[:-3]] = sha
        else:
            refs[ref] = sha
    refs.update(peeled)
    return refs


def pick_ref(app_version: str, refs: dict[str, str]) -> tuple[str, str]:
    """``(label, sha)``: the first of :func:`packrules.turbo_ea_refs` present."""
    for ref in packrules.turbo_ea_refs(app_version):
        if ref in refs:
            return ref.rsplit("/", 1)[-1], refs[ref]
    raise RuntimeError("turbo-ea has neither a matching release tag nor a main branch")


def resolve(app_version: str, pinned_sha: str = "") -> tuple[str, str]:
    """``(label, commit sha)`` of the turbo-ea commit to validate against."""
    if pinned_sha:
        if not packrules.is_sha(pinned_sha):
            raise RuntimeError("TURBO_EA_VALIDATOR_SHA must be a full 40-character commit SHA")
        return f"pinned {pinned_sha[:12]}", pinned_sha
    candidates = packrules.turbo_ea_refs(app_version)  # raises on a bad version
    patterns = [p for ref in candidates for p in (ref, ref + "^{}")]
    return pick_ref(app_version, parse_ls_remote(_git("ls-remote", REPO_URL, *patterns)))


def latest_release(output: str) -> tuple[str, str] | None:
    """``(tag, commit sha)`` of the highest ``v<x.y.z>`` tag in ``git ls-remote
    --tags`` output, or ``None``."""
    refs = parse_ls_remote(output)
    best: tuple[tuple[int, int, int], str, str] | None = None
    for ref, sha in refs.items():
        tag = ref.removeprefix("refs/tags/")
        if not ref.startswith("refs/tags/v") or not packrules.is_version(tag[1:]):
            continue
        key = tuple(int(p) for p in tag[1:].split("."))
        if best is None or key > best[0]:
            best = (key, tag, sha)  # type: ignore[assignment]
    return None if best is None else (best[1], best[2])


def prepare(app_version: str, work: Path, pinned_sha: str = "") -> dict[str, object]:
    """Fetch what a pack is checked with:

    * the **validator** from the latest turbo-ea release that has it, else
      ``main`` — never from the pack's own ``app_version``, so a pack cannot
      choose an older, more lenient validator;
    * the **English files** of the pack's ``app_version`` (its release tag, else
      ``main``), the text the pack was translated from.

    ``pinned_sha`` (set by the repository owner) replaces both, for testing an
    unreleased turbo-ea. Returns labels, SHAs and paths."""
    if pinned_sha:
        label, sha = resolve(app_version, pinned_sha)
        validator, english = fetch(sha, work / sha)
        return {"validator_label": label, "validator_sha": sha, "validator": validator,
                "english_label": label, "english_sha": sha, "english": english}
    english_label, english_sha = resolve(app_version)
    fetched: dict[str, Path] = {}

    def checkout(sha: str) -> Path:
        if sha not in fetched:
            fetch(sha, work / sha, need_validator=False)
            fetched[sha] = work / sha
        return fetched[sha]

    candidates: list[tuple[str, str]] = []
    release = latest_release(_git("ls-remote", "--tags", REPO_URL, "refs/tags/v*"))
    if release is not None:
        candidates.append(release)
    main = parse_ls_remote(_git("ls-remote", REPO_URL, "refs/heads/main")).get("refs/heads/main")
    if main:
        candidates.append(("main", main))
    for label, sha in candidates:
        if (checkout(sha) / VALIDATOR_PATH).is_file():
            english = checkout(english_sha) / ENGLISH_PATH
            if not english.is_dir() or not any(english.glob("*.json")):
                raise RuntimeError(f"turbo-ea {english_label} has no English locale files")
            return {"validator_label": label, "validator_sha": sha, "validator": checkout(sha) / VALIDATOR_PATH,
                    "english_label": english_label, "english_sha": english_sha, "english": english}
    raise RuntimeError(
        f"no turbo-ea release or main has {VALIDATOR_PATH} yet (language packs are not released yet)"
    )


def fetch(sha: str, dest: Path, *, need_validator: bool = True) -> tuple[Path, Path]:
    """Sparse, blob-filtered checkout of ``sha`` into the new directory ``dest``.
    Returns ``(validator file, English directory)``."""
    if not packrules.is_sha(sha):
        raise RuntimeError("not a commit sha")
    dest.mkdir(parents=True, exist_ok=False)
    _git("init", "-q", str(dest))
    _git("remote", "add", "origin", REPO_URL, cwd=dest)
    _git("fetch", "-q", "--depth", "1", "--filter=blob:none", "origin", sha, cwd=dest)
    _git("sparse-checkout", "set", "--no-cone", "/" + VALIDATOR_PATH, f"/{ENGLISH_PATH}/*.json", cwd=dest)
    _git("-c", "advice.detachedHead=false", "checkout", "-q", "--detach", sha, cwd=dest)
    validator = dest / VALIDATOR_PATH
    english = dest / ENGLISH_PATH
    if need_validator and not validator.is_file():
        raise RuntimeError(
            f"turbo-ea {sha[:12]} has no {VALIDATOR_PATH} "
            "(language packs are newer than that version, or not released yet)"
        )
    if not english.is_dir() or not any(english.glob("*.json")):
        raise RuntimeError(f"turbo-ea {sha[:12]} has no English locale files")
    return validator, english


def run_validator(validator: Path, pack: Path, english: Path) -> tuple[int, str]:
    """Run ``language_pack.py validate`` isolated; ``(exit code, output)``."""
    with tempfile.TemporaryDirectory() as empty:
        result = subprocess.run(
            [sys.executable, "-I", "-S", str(validator), "validate", str(pack), "--english", str(english)],
            cwd=empty,
            env={"PATH": "/usr/bin:/bin", "LC_ALL": "C.UTF-8", "PYTHONIOENCODING": "utf-8"},
            capture_output=True,
            text=True,
            timeout=600,
        )
    return result.returncode, (result.stdout + result.stderr)


def read_english(english: Path) -> dict[str, dict[str, str]]:
    """``{namespace: {flat key: text}}`` of an English locale directory."""
    return {
        path.stem: packrules.flatten_english(json.loads(path.read_text(encoding="utf-8")))
        for path in sorted(english.glob("*.json"))
    }
