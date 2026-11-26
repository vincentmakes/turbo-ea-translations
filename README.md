# Turbo EA language packs

Community translations of [Turbo EA](https://github.com/vincentmakes/turbo-ea),
the self-hosted enterprise-architecture application, for languages it does not
ship itself.

Turbo EA comes with ten languages (English, German, French, Spanish, Italian,
Portuguese, Chinese, Russian, Danish and Arabic). An administrator can add any
other language as a **custom language** and fill it from a **language pack**: a
single JSON file with the translated interface text and the translated names of
the built-in metamodel. This repository collects those packs so that every
Turbo EA installation can use them.

> **Packs are provided by the community and are not reviewed by the Turbo EA
> project.** Each pack is checked automatically (see below): the translated
> strings cannot add markup, links or broken placeholders that the English text
> does not have, and the few free-text header fields (the language's native name
> and the export's instructions) cannot contain markup or links. Nobody checks
> the quality or the accuracy of the translations, and a pack may be very
> incomplete. Look at a pack in Turbo EA's **Review strings** screen before you
> offer it to your users.

## Using a pack

1. Open the language's folder under [`packs/`](packs/) and download
   `pack.json`.
2. In Turbo EA, go to **Admin → Settings → General → Custom Languages**, add the
   language (same code) if it is not there yet, and use **Import language
   pack** on it.
3. Review the strings, then switch the language on for your users.

A pack made with an older version of Turbo EA imports cleanly into a newer one:
strings that changed since are marked *changed in English*, and anything the
pack does not have yet is shown in English.

[`index.json`](index.json) lists every pack with its language, the Turbo EA
version it was made with, string counts, coverage, the SHA-256 of the file and
the commit it was last changed in. It is generated automatically after each
merge; a future Turbo EA version will use it to list and install packs
directly.

## Contributing a pack

You need a Turbo EA installation where you translated the language (by hand,
with the built-in AI translation, through an AI assistant, or by importing a
file you had translated elsewhere). Then:

1. **Admin → Settings → General → Custom Languages → Export language pack →
   Share (community pack)**. Only this export is accepted: it holds the
   interface text and the built-in metamodel names, and nothing specific to
   your installation.
2. Open a pull request that adds or replaces `packs/<code>/pack.json` — it can
   be done entirely in the GitHub web interface.

The full step-by-step guide, what the check verifies and how maintainers work
are in [CONTRIBUTING.md](CONTRIBUTING.md).

**In short:** a new language needs at least half of the interface translated
and the repository owner's approval; it then merges automatically and its author
becomes its maintainer. Changes to an existing language merge automatically
when they come from one of its maintainers, or once a maintainer approves them.
The bot mentions whoever has to approve.

## Layout

```
packs/<code>/pack.json     the language pack (the only file contributors change)
packs/<code>/MAINTAINERS   the language's maintainers: GitHub login + numeric user id (automation / owner)
index.json                 generated list of all packs (automation)
scripts/                   the checks, and check_pack.py to run them on your machine
.github/workflows/         the automation
```

## Repository settings (for the owner)

The automation relies on these settings; each line says why.

* **Merging:** allow **squash merging only** (Settings → General → Pull
  Requests). The bot squash-merges; a merge commit on `main` is what could give
  a crafted pull request two merge bases.
* **Ruleset on `main`** (Settings → Rules), applied to everyone including
  admins: **require linear history**, block **force pushes** and **deletion**.
  Do **not** require pull requests, status checks, reviews or code-owner reviews
  on `main`: the bot commits `MAINTAINERS` and `index.json` directly, and
  required reviews would mean you approve everything. Never make the
  path-filtered *Tests* check or *judge / merge* a required check (they do not
  run on every pull request).
* **No collaborators with write access.** Anyone with write can push to
  `.github/`, run the Sweep from their own branch with a write token, and edit
  `MAINTAINERS`. Contributors work from forks and need no access.
* **Actions → General:** workflow permissions can stay *read*; the workflows
  ask for write explicitly where needed. "Allow GitHub Actions to create and
  approve pull requests" is not needed. The approval setting for first-time
  contributors does not gate the pack check (`pull_request_target`); it does
  gate the *Tests* workflow and possibly the review relay (a maintainer's
  approval then takes effect at the daily sweep).
* **Scheduled workflows** (Sweep, Registry) are switched off by GitHub after 60
  days without repository activity; switch them back on in the Actions tab.
* **Until Turbo EA ships `language_pack.py`** in a release (or on `main`), every
  pack check fails with a clear message. To test earlier, set the repository
  variable `TURBO_EA_VALIDATOR_SHA` to a turbo-ea commit that has it, and remove
  it after the release.
* Delete merged branches (or turn on "Automatically delete head branches").

## Licence

Everything in this repository — the packs, the scripts and the documentation —
is published under the [MIT licence](LICENSE). By opening a pull request you
agree that your contribution is published under it. The packs translate the
English text of Turbo EA, which is itself licensed under FSL-1.1-MIT by the same
copyright holder.
