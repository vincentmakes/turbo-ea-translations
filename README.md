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
> project.** Each pack is checked automatically (see below) — the check makes
> sure a pack is well-formed and cannot inject markup, links or broken
> placeholders, but nobody checks the quality or the accuracy of the
> translations. Look at a pack in Turbo EA's **Review strings** screen before
> you offer it to your users.

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
version it was made with, string counts, coverage and SHA-256. It is generated
automatically after each merge; a future Turbo EA version will use it to list
and install packs directly.

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

**In short:** a new language merges automatically once it passes the check, and
its author becomes its maintainer. Changes to an existing language merge
automatically when they come from one of its maintainers, or once a maintainer
approves them.

## Layout

```
packs/<code>/pack.json     the language pack (the only file contributors change)
packs/<code>/MAINTAINERS   GitHub logins of the language's maintainers (automation / owner)
index.json                 generated list of all packs (automation)
scripts/                   the checks, and check_pack.py to run them on your machine
.github/workflows/         the automation
```

## Licence

This repository does not have a licence yet; the owner has to choose one. Until
a licence file is added here, no licence to the packs is granted beyond what
GitHub's Terms of Service allow.
