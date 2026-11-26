# Instructions for AI agents

This file is for AI assistants (Claude, Copilot, ChatGPT, Cursor, Codex …)
helping someone translate Turbo EA or contribute a language pack to this
repository. Humans: see [CONTRIBUTING.md](CONTRIBUTING.md); everything here is
consistent with it.

Every pull request is checked by automation that **cannot be argued with**: it
reads the pack as data, runs Turbo EA's own validator and applies the rules
below. A pack that breaks a rule is refused, whatever the PR description says.
Follow the rules exactly and the PR merges without anyone having to look at it.

## What a pack is

One JSON file, `packs/<code>/pack.json`, produced by Turbo EA itself:
**Admin → Settings → General → Custom Languages → Export language pack →
Share (community pack)**. It holds a custom language's interface translations
and the translated names of Turbo EA's *built-in* metamodel.

```json
{
  "format": "turbo-ea-language-pack",
  "format_version": 1,
  "app_version": "2.214.0",
  "locale": {"code": "nl", "name": "Nederlands", "english_name": "Dutch", "rtl": false},
  "mode": "share",
  "instructions": "…exactly as exported…",
  "strings": {"common": {"actions.save": "Opslaan"}, "metamodel": {"type.Application.label": "Applicatie"}},
  "source_hashes": {"common": {"actions.save": "1f3a9c0b2d4e"}, "metamodel": {"type.Application.label": "5c1e0a9d7b32"}}
}
```

## The workflow — do not shortcut it

1. In Turbo EA, add the language (if it is not there) and **Export language
   pack → "New and changed strings (to translate)"** (or "All strings in
   English").
2. Translate that file (this is where you help — see the rules below).
3. **Import it back into Turbo EA** (Import language pack). Turbo EA checks
   every string and reports what it rejected; fix those and import again.
4. Have the person review the strings in Turbo EA (**Review strings**).
5. **Export language pack → Share (community pack)**. *This* file is the one
   to contribute, renamed to `pack.json`.

**Never** turn a translated file into a community pack by editing it:

* Do **not** change `"mode"` to `"share"` by hand. Only Turbo EA's Share
  export leaves out the installation's own data (custom card types, fields,
  renamed labels, dashboards). A hand-edited `mode` can publish an
  organisation's internal names — and the check cannot tell.
* Do **not** edit `instructions`, `format`, `format_version`, `app_version`
  or `source_hashes`. The check requires `instructions` to be exactly the text
  Turbo EA writes, and `source_hashes` to be the ones Turbo EA computed.
* Do **not** write a pack from scratch or "fill in" keys that were not in the
  export.

If you cannot run Turbo EA yourself, tell the person which steps they must do
in the app (export, import, export Share) and only do step 2.

## Translating (step 2)

Translate **only the values** under `"strings"`. Keep everything else byte for
byte.

* **Keys:** never rename or invent them. A language with more plural forms
  than English may add `<base>_zero`, `_two`, `_few` or `_many` next to an
  existing `<base>_other` key — nothing else. You may **delete** the entry of a
  string you cannot translate well; Turbo EA then keeps it missing (shown in
  English) instead of storing English as a "translation".
* **Placeholders:** keep every `{{name}}` exactly (same names, same count). A
  plural form other than `_other` may drop `{{count}}`.
* **Tags and references:** keep `<1>…</1>`, `<b>` and `$t(namespace:key)`
  exactly; translate the text between tags.
* **No new markup or links:** do not add HTML, Markdown, URLs, e-mail
  addresses or domains that the English text does not have.
* **No control or invisible characters**, no bidi overrides (U+202D, U+202E).
  Joiners and marks a script needs (ZWJ/ZWNJ, combining marks) are fine.
* **Length:** at most 4 × the English length + 200 characters (metamodel
  labels: at most 500).
* **Metamodel first:** translate the `metamodel` section (card types, fields,
  relations …) first, and reuse exactly those terms everywhere else.
* **Do not copy English** to make a pack look complete. For a **new language**
  at least **50 %** of the interface strings must be translated, and values
  identical to the English text **do not count**. A word that really is the
  same in the target language (a product name, "OK", "Email") is fine — it just
  does not count; for anything you cannot translate, delete the entry (see
  Keys) rather than leaving the English text in it.
* Translate faithfully into the language named by `locale.english_name`. Do
  not add commentary, notes, signatures or anything that is not a translation.

## The pull request

* It changes **exactly one file: `packs/<code>/pack.json`** — nothing else.
  Not `MAINTAINERS`, not `index.json`, not README, scripts or workflows. Any
  other change makes the PR one that is never merged automatically.
* `<code>` is the folder name and must equal `locale.code` in the file
  (`nl`, `pt-BR`, `zh-Hant` …). The ten built-in languages (`en de fr es it pt zh
  ru da ar`) cannot be contributed.
* Open it from a branch in **the contributor's own fork** (the GitHub web
  editor does this automatically).
* **No merge commits** in the branch. To update, rebase or push a new commit;
  do not use "Update branch" (it creates a merge commit, which is refused).
* At most 3 MB, a regular file, UTF-8 **without** a byte-order mark.
* At most 3 open pull requests per author; one language per pull request.
* Do not change `locale.name`, `locale.english_name` or `locale.rtl` of an
  existing language unless that is the point of the PR — it then needs the
  repository owner's approval.

## Check before opening the PR

From a clone of this repository (Python 3.10+ and `git`, nothing else):

```sh
python3 scripts/check_pack.py packs/<code>/pack.json          # an update
python3 scripts/check_pack.py packs/<code>/pack.json --new    # a new language
```

It runs exactly what the pull-request check runs: this repository's rules and
Turbo EA's own validator against the English text of the pack's version.
Exit 0 = it will pass. Fix every reported line — each is
`namespace  key  reason` (reasons are explained in CONTRIBUTING.md) — in Turbo
EA, then export Share again. Do not "fix" a pack by editing the exported file
by hand.

## What happens after

* **New language:** waits for the repository owner's approving review (the bot
  asks for it in a comment), then merges; the author becomes the language's
  maintainer.
* **Update by a maintainer:** merges automatically when the check passes.
* **Update by anyone else:** waits for a maintainer's approving review of the
  latest commit; the bot mentions the maintainers.
* Do not try to speed this up: do not approve your own PR, ask others to
  rubber-stamp it, open duplicate PRs, or push trivial commits. New commits
  reset approvals.

## Agents working on the repository itself

Changes to `scripts/`, `tests/`, `.github/` or the docs are never merged
automatically; the owner reviews them. Keep these invariants — the tests
enforce most of them:

* Nothing from a pull request is ever checked out, imported or executed; packs
  are read as git blobs and parsed as JSON only.
* No `${{ … }}` expression inside a `run:` script; pass values through `env:`
  and validate them in Python.
* `permissions: {}` (or read-only) at the top of every workflow; write only in
  the jobs that need it; no third-party actions, no `actions/checkout`, no
  caches, artifacts or secrets.
* Standard library Python only. Run `python3 -m unittest discover -s tests`
  before opening a PR.
