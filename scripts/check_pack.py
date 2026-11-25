"""Check a language pack on your own machine, the way the pull-request check does.

    python3 scripts/check_pack.py packs/nl/pack.json
    python3 scripts/check_pack.py my-export.json --code nl
    python3 scripts/check_pack.py packs/nl/pack.json --turbo-ea ~/src/turbo-ea

It applies this repository's rules (size, JSON shape, header, "share" mode,
folder name = language code) and then runs turbo-ea's own validator against the
English files of the Turbo EA version the pack was exported from. Without
``--turbo-ea`` both are downloaded (needs ``git``) from
github.com/vincentmakes/turbo-ea: the validator of the latest release, the
English files at the pack's release tag (``main`` when not released yet).
``--sha`` uses one turbo-ea commit for both. Standard library only; 3.10+.

Exit status: 0 valid, 1 invalid, 2 the check could not run.
"""

from __future__ import annotations

import argparse
import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import packrules  # noqa: E402
import turbo_ea  # noqa: E402

_PATH_CODE = re.compile(r"(?:^|.*/)packs/([^/]+)/pack\.json")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check a Turbo EA language pack like the PR check does.")
    parser.add_argument("pack", type=Path)
    parser.add_argument("--code", help="the folder code (default: taken from packs/<code>/pack.json)")
    parser.add_argument("--turbo-ea", type=Path, help="use a local turbo-ea checkout instead of downloading")
    parser.add_argument("--sha", default="", help="validate against this turbo-ea commit instead")
    args = parser.parse_args(argv)

    folder_code = args.code
    if folder_code is None:
        match = _PATH_CODE.fullmatch(args.pack.as_posix())
        folder_code = match.group(1) if match else None
    if folder_code is None:
        print("note: not under packs/<code>/ and no --code given; the folder rule is not checked")
    elif packrules.code_problem(folder_code):
        print(f"folder name {folder_code!r} is not an accepted language code "
              f"({packrules.code_problem(folder_code)})")
        return 1

    try:
        if args.pack.is_symlink() or not args.pack.is_file():
            print("the pack must be a regular file")
            return 1
        data = args.pack.read_bytes()
        pack = packrules.parse_pack_bytes(data)
    except packrules.PackError as exc:
        print(f"invalid: {exc}")
        return 1
    except OSError as exc:
        print(f"cannot read {args.pack}: {exc}")
        return 2
    problems = packrules.header_problems(pack, folder_code)
    if problems:
        for problem in problems:
            print(f"invalid: {problem}")
        return 1

    with tempfile.TemporaryDirectory() as work:
        try:
            if args.turbo_ea:
                label = f"local {args.turbo_ea}"
                validator = args.turbo_ea / turbo_ea.VALIDATOR_PATH
                english = args.turbo_ea / turbo_ea.ENGLISH_PATH
                if not validator.is_file():
                    print(f"no {turbo_ea.VALIDATOR_PATH} in {args.turbo_ea}")
                    return 2
            else:
                print("fetching turbo-ea's validator and English files …")
                got = turbo_ea.prepare(pack["app_version"], Path(work), args.sha)
                validator, english = got["validator"], got["english"]
                label = f"{got['english_label']}"
                print(f"validator: turbo-ea {got['validator_label']}; English text: {got['english_label']}")
        except (RuntimeError, ValueError, OSError) as exc:
            print(f"cannot get turbo-ea's validator: {exc}")
            return 2
        code, output = turbo_ea.run_validator(validator, args.pack.resolve(), english)
        print(output.rstrip())
        stats = packrules.pack_stats(pack, turbo_ea.read_english(english))
    print(f"compared with turbo-ea {label}: {stats.get('translated', 0)}/{stats.get('english_strings', 0)} "
          f"interface strings ({stats.get('coverage', 0)}%), {stats['metamodel_strings']} metamodel labels")
    if code == 0:
        print("✔ valid — this pack passes the same checks as the pull-request check")
    return {0: 0, 1: 1}.get(code, 2)


if __name__ == "__main__":
    sys.exit(main())
