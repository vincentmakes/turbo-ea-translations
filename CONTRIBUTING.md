# Contributing a language pack

Thank you for translating Turbo EA. This page covers everything from exporting
the file to how your pull request gets merged.

## 1. What you need

* A Turbo EA installation (a version with **Custom Languages**) where you are an
  administrator (the `admin.settings` permission).
* The language added there as a custom language, and translated — fully or in
  part. A partial pack is welcome: untranslated strings show in English.
* A GitHub account.

The language code must be two or three lower-case letters, optionally followed
by a script and/or a region — `nl`, `he`, `hi`, `fil`, `pt-BR`, `zh-Hant`,
`zh-Hant-TW` (at most 10 characters). It cannot be one of the built-in
languages: `en de fr es it pt zh ru da ar`. Regional variants are separate
languages: `pt-BR` is its own pack.

## 2. Export the pack

In Turbo EA: **Admin → Settings → General → Custom Languages**, then on your
language **Export language pack → Share (community pack)**.

Use **Share (community pack)** and nothing else. It contains the interface
translations and the translated names of Turbo EA's *built-in* metamodel (card
types, fields, relations … as Turbo EA ships them) — nothing about your own
installation: no card types or fields you created, no renamed labels, no
dashboards or categories of yours. The other export modes are refused by the
check because they can contain your organisation's data.

Rename the downloaded file to `pack.json`. Do not edit it by hand: fix strings
in Turbo EA (**Review strings**) and export again.

## 3. Open the pull request (web interface only)

**A new language** (no `packs/<code>/` folder yet):

1. Go to the [`packs/`](packs/) folder of this repository.
2. Click **Add file → Create new file**. In the name box type
   `<code>/pack.json` (for example `nl/pack.json`) — typing the `/` creates the
   folder.
3. Paste the whole content of your exported file. (Or, instead of steps 2–3:
   on your computer put `pack.json` into a folder named after the code, and
   drag that **folder** onto the page opened by **Add file → Upload files**.)
4. **Commit changes… → Propose changes → Create pull request.** GitHub forks the
   repository for you automatically.

**An update to an existing language:** open `packs/<code>/pack.json`, click the
pencil (**Edit this file**), select everything, paste your new export over it,
then **Commit changes… → Propose changes → Create pull request**.

Rules for the pull request — the check enforces them:

* It changes **exactly one file**: `packs/<code>/pack.json`. Not `MAINTAINERS`,
  not `index.json`, not the README — those are maintained by automation or by
  the repository owner. A pull request that changes anything else is never
  merged automatically.
* The folder name equals the `locale.code` inside the file.
* At most 5 MB, a regular file (no symlink).
* One language per pull request.

## 4. What the check verifies

The **Pack check** runs on every pull request. It never runs anything from your
pull request; it reads your file as data. It checks:

* the file list (rules above), and that the file is a regular file under 5 MB;
* the file is UTF-8 JSON, an object, not nested more than 8 levels, with no
  duplicate keys;
* the header: `"format": "turbo-ea-language-pack"`, `"format_version": 1`,
  `"mode": "share"`, `app_version` a plain version (`2.214.0`), the language
  code (valid, not built-in, equal to the folder), the native name (one line,
  at most 60 characters), the English name (letters, spaces, brackets, dashes),
  `rtl` true or false, and `source_hashes` made of 12-character hashes;
* then **Turbo EA's own validator** (`language_pack.py`, the same code the app
  runs on import, taken from the latest Turbo EA release) against the English
  text of the Turbo EA version your pack was exported from (the release tag of
  `app_version`; Turbo EA's `main` branch if that version is not released yet).
  It reports one line per problem: `namespace  key  reason`.

| reason | what it means |
|---|---|
| `unknown_key` | the key does not exist in that version's English text (or a `metamodel` key has an unknown shape) |
| `unknown_namespace` | a section that does not exist |
| `empty` | the translation is blank |
| `too_long` | much longer than the English (4 × English + 200 characters; 500 for `metamodel`) |
| `placeholder_mismatch` | a `{{placeholder}}` was lost, added or altered |
| `nesting_mismatch` | a `$t(...)` reference was lost, added or altered |
| `markup_not_in_source` | a tag (`<b>`, `<1>` …) the English text does not have |
| `url_not_in_source` | a link the English text does not have |
| `control_chars` | an invisible control character or a bidi override |
| `not_a_string` / `bad_key` | the file was edited into an invalid shape |
| `invalid_code`, `invalid_english_name`, `bad_source_hashes`, `bad_format_version` | header problems |

Open the check's **Details** to see the report in the job summary. Fix the
strings in Turbo EA, export again and push the new file to the same pull request
(edit the file on your fork's branch); the check runs again.

### Check a pack on your own computer first (optional)

With Python 3.10+ and `git`:

```sh
git clone https://github.com/vincentmakes/turbo-ea-translations
cd turbo-ea-translations
mkdir -p packs/nl && cp ~/Downloads/your-export.json packs/nl/pack.json
python3 scripts/check_pack.py packs/nl/pack.json
```

It applies the same rules and runs the same validator, downloaded from
Turbo EA's repository at the matching version. `--turbo-ea <dir>` uses a local
turbo-ea checkout instead.

## 5. How it gets merged — maintainers

Nobody from the Turbo EA project reviews packs. Instead:

* **New language:** once the check passes, the pull request merges
  automatically, and **you become the language's maintainer**: your GitHub login
  and numeric user id are written to `packs/<code>/MAINTAINERS` (the id keeps
  the role with your account even if a login is renamed and re-registered).
* **Update from a maintainer:** merges automatically once the check passes.
* Both need the pull request to come from a branch in **your own fork** —
  which is what the web interface does for you. A pull request from a branch
  someone else owns waits for a maintainer's approval instead (or, for a new
  language, for the repository owner).
* **Update from anyone else:** waits until one of the language's maintainers
  submits an **approving review** of the latest commit. It then merges
  automatically (usually within minutes; at the latest at the daily sweep).
  A maintainer's "Request changes" blocks it until they approve. If you push new
  commits, an earlier approval no longer counts — the new version needs one.
* If the pack changed on the default branch after you opened your pull request,
  it is not merged automatically: update your branch (or open a new pull
  request from the current version) so the check runs on exactly what would be
  merged.

If the check passes but nothing happens, the check's job summary says what the
pull request is waiting for.

**Maintainers:** please review pull requests for your language — you are the
only reviewers it has. To add a co-maintainer, or to hand the language over,
open an issue or a pull request changing `packs/<code>/MAINTAINERS`; the
repository owner merges those by hand. If a language has no maintainer left,
changes to it wait for the repository owner.

## 6. Licence

This repository does not have a licence yet. Check the README for the current
situation before contributing.

## Changes to the repository itself

Pull requests that change scripts, workflows or documentation are welcome too;
they are never merged automatically and are reviewed by the repository owner.
Run the tests with `python3 -m unittest discover -s tests`.
