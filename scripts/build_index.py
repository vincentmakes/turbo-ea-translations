"""Generate index.json from the packs on the default branch.

    python scripts/build_index.py build --repo . --out index.json
    python scripts/build_index.py publish --file index.json      # needs GH_TOKEN

``build`` reads ``packs/<code>/pack.json`` as data (regular files only, size
and depth capped, same header rules as a contribution), counts the strings and
measures coverage against turbo-ea's English files of each pack's app_version.
``publish`` writes the file to the default branch through the contents API
when it changed. The index holds no timestamp of its own, so an unchanged
repository produces a byte-identical file and no commit.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))
import packrules  # noqa: E402

INDEX_FORMAT = "turbo-ea-language-pack-index"
INDEX_FORMAT_VERSION = 1
SOURCE_REPO = "vincentmakes/turbo-ea-translations"

EnglishLoader = Callable[[str], "tuple[str, dict[str, dict[str, str]]] | None"]


def last_update(repo: Path, rel: str) -> str | None:
    """Commit date (ISO 8601) of the last commit touching ``rel``."""
    try:
        out = subprocess.run(
            ["git", "log", "-1", "--format=%cI", "--", rel],
            cwd=repo, capture_output=True, text=True, timeout=60, check=True,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    return out or None


def read_regular_file(path: Path, limit: int) -> bytes | None:
    """The bytes of ``path`` if it is a regular file (not a symlink) within
    ``limit`` bytes, else ``None``."""
    try:
        info = path.lstat()
    except OSError:
        return None
    if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
        return None
    return path.read_bytes()


def build_entry(code: str, data: bytes, english: tuple[str, dict[str, dict[str, str]]] | None,
                updated: str | None) -> dict[str, Any]:
    pack = packrules.parse_pack_bytes(data)
    problems = packrules.header_problems(pack, code)
    if problems:
        raise packrules.PackError("; ".join(problems))
    locale = pack["locale"]
    entry: dict[str, Any] = {
        "code": code,
        "name": locale["name"],
        "english_name": locale["english_name"],
        "rtl": locale["rtl"],
        "app_version": pack["app_version"],
        "path": f"packs/{code}/pack.json",
        "sha256": hashlib.sha256(data).hexdigest(),
        "size": len(data),
        "updated": updated,
    }
    stats = packrules.pack_stats(pack, english[1] if english else None)
    if english:
        stats["compared_with"] = english[0]
    entry.update(stats)
    return entry


def build_index(repo: Path, load_english: EnglishLoader, log: Callable[[str], None] = print) -> dict[str, Any]:
    packs_dir = repo / "packs"
    languages: list[dict[str, Any]] = []
    if packs_dir.is_dir() and not packs_dir.is_symlink():
        for folder in sorted(packs_dir.iterdir(), key=lambda p: p.name):
            code = folder.name
            if folder.is_symlink() or not folder.is_dir() or packrules.code_problem(code) is not None:
                continue
            data = read_regular_file(folder / "pack.json", packrules.MAX_PACK_BYTES)
            if data is None:
                log(f"skipped packs/{code}: no regular pack.json within the size limit")
                continue
            try:
                version = packrules.parse_pack_bytes(data).get("app_version")
                english = load_english(version) if packrules.is_version(version) else None
                languages.append(
                    build_entry(code, data, english, last_update(repo, f"packs/{code}/pack.json"))
                )
            except packrules.PackError as exc:
                log(f"skipped packs/{code}: {exc}")
    return {
        "format": INDEX_FORMAT,
        "format_version": INDEX_FORMAT_VERSION,
        "repository": SOURCE_REPO,
        "pack_url": f"https://raw.githubusercontent.com/{SOURCE_REPO}/{{ref}}/{{path}}",
        "languages": languages,
    }


def render(index: dict[str, Any]) -> str:
    return json.dumps(index, ensure_ascii=False, indent=2) + "\n"


def english_loader(work: Path) -> EnglishLoader:
    """Fetch each version's English files once (``main`` for untagged ones)."""
    import turbo_ea

    cache: dict[str, tuple[str, dict[str, dict[str, str]]] | None] = {}
    by_sha: dict[str, tuple[str, dict[str, dict[str, str]]]] = {}

    def load(version: str):
        if version in cache:
            return cache[version]
        try:
            label, sha = turbo_ea.resolve(version)
            if sha not in by_sha:
                dest = work / sha
                if dest.exists():
                    english = dest / turbo_ea.ENGLISH_PATH
                else:
                    _validator, english = turbo_ea.fetch(sha, dest, need_validator=False)
                by_sha[sha] = (label, turbo_ea.read_english(english))
            cache[version] = (label, by_sha[sha][1])
        except (RuntimeError, ValueError, OSError) as exc:
            print(f"no English files for {version}: {exc}")
            cache[version] = None
        return cache[version]

    return load


def cmd_build(repo: Path, out: Path) -> int:
    with tempfile.TemporaryDirectory() as work:
        index = build_index(repo, english_loader(Path(work)))
    out.write_text(render(index), encoding="utf-8")
    print(f"{len(index['languages'])} language(s) in {out}")
    return 0


def cmd_publish(file: Path) -> int:
    import gh_api

    api = gh_api.Client()
    branch = api.default_branch()
    content = file.read_bytes()
    head = api.branch_sha(branch)
    entry = api.tree_entry(head, "index.json")
    if entry is not None and entry.get("sha") == packrules.git_blob_sha(content):
        print("index.json is up to date")
        return 0
    api.put_file("index.json", content, "Update index.json", branch, entry.get("sha") if entry else None)
    print("index.json updated")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    p_build = sub.add_parser("build")
    p_build.add_argument("--repo", type=Path, default=Path("."))
    p_build.add_argument("--out", type=Path, default=Path("index.json"))
    p_publish = sub.add_parser("publish")
    p_publish.add_argument("--file", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.command == "build":
        return cmd_build(args.repo, args.out)
    return cmd_publish(args.file)


if __name__ == "__main__":
    sys.exit(main())
