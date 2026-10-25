# Contributing to JoeyLab

Thank you for wanting to help. Bug reports, ideas, documentation fixes and code are all welcome. People trust
JoeyLab with their photos, so every change keeps the rules in
[The non-destructive rules](#the-non-destructive-rules) below.

[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) explains how the code fits together. Read it before a larger change.

## Setting up

You need macOS 14 or later (most of JoeyLab also runs on Linux, but it's only tested on macOS),
[uv](https://docs.astral.sh/uv/) and exiftool:

```sh
brew install uv exiftool
git clone https://github.com/Yadub/joeylab.git
cd joeylab
uv sync --all-extras
```

`uv sync` also installs the development tools (pytest, httpx). Leave out `--all-extras` for a quicker install of
the core only; the tests that need an extra skip themselves.

### Trying your changes without touching your own library

`JOEYLAB_HOME` moves JoeyLab's whole data folder, so a development copy can't touch your real catalogs:

```sh
export JOEYLAB_HOME=/tmp/joeylab-dev
uv run joeylab demo                  # a library of generated photos
uv run joeylab serve --port 8766     # a second port, so it doesn't collide with a running JoeyLab
```

If you point a development copy at real photos, use a copy of them, or mount the drive read-only
(`diskutil mount readOnly <device>`). JoeyLab doesn't write there, but code under development might.

## Running the tests

```sh
JOEYLAB_OFFLINE=1 HF_HUB_OFFLINE=1 uv run pytest -q
```

* The tests run offline. `JOEYLAB_OFFLINE=1` stops JoeyLab's own downloads (models, the town list) and
  `HF_HUB_OFFLINE=1` stops the Hugging Face library's.
* Each test gets its own `JOEYLAB_HOME` in a temporary folder and works on photos it generates, so the tests
  never read or change `~/.joeylab` or any real photos.
* Tests that need something that isn't there (the face models, exiftool) skip themselves.
* Continuous integration runs the same command on macOS 14 (Apple Silicon), then `uv build`.

Add tests with your change, in the file for its area. A change that touches anything in the non-destructive rules
needs a test that would fail if the rule were broken.

## Code style

* **Match the code around you.** Python 3.12, `from __future__ import annotations`, type hints where they help,
  docstrings that say what something is for. Lines run to about 120 characters.
* **Keep it small.** Prefer the standard library and the dependencies JoeyLab already has. A new dependency
  needs a good reason, a licence that allows redistribution, and a mention in
  [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
* **The front end** is plain JavaScript modules in `src/joeylab/web/static`, with no build step and no
  framework. Build elements with the `h()` helper from `ui.js`, and follow the existing views.
* **Plain wording.** Everything a person reads (the app, command output, errors, docs) is plain, specific,
  friendly English with British spelling (colour, favourite, licence). Say what happened and what to do next:
  "That's inside ~/Pictures/Trips, a folder JoeyLab only reads. Choose a folder outside it." rather than
  "Invalid destination". No jargon where an ordinary word will do.
* **Tunable numbers** live in `config.py` and `scoring.py`, with a comment saying what they do.
* **Analysis changes:** bump `ANALYSIS_VERSION` in `analysis.py` so existing catalogs re-analyse.
* **Catalog changes:** add a new step at the end of `db.MIGRATIONS`. Steps only add tables, columns and indexes,
  and a step that has been released is never edited.
* **Nothing calls out.** JoeyLab works offline after its one-time downloads, sends no telemetry and never calls
  an AI service. Features that would need that are out of scope; an assistant can use JoeyLab through its MCP
  server instead.

## The non-destructive rules

Every change must keep these. [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) explains each one in more detail.

1. **Photos are only read.** Never write, rename, move, touch or set attributes on a file or folder JoeyLab
   tracks, except through the explicit, user-started paths that exist for it (XMP sidecars and Move to Trash).
2. **What JoeyLab learns goes in its catalog**, in `~/.joeylab`, never beside the photos.
3. **Nothing is deleted.** The most JoeyLab does is move what the user chose to the Trash, after a read-only
   check, a typed confirmation and a re-check right before each move.
4. **Decisions belong to the user.** Suggestions never become ratings unless accepted; imports don't overwrite
   unless asked; every change is logged and can be undone.
5. **Missing isn't deleted.** A file that disappears is marked missing with its decisions kept; an unplugged drive
   is skipped, not emptied.
6. **Exports never land inside photo folders** or inside JoeyLab's own data folder.
7. **The untouched check stays true.** After every update JoeyLab checks that every tracked file and folder is
   exactly as it was. Nothing you add may change that.
8. **Files are the owner's business.** Anything that writes files, adds folders or moves things to the Trash is
   for the owner only (see [SECURITY.md](SECURITY.md)).

## Proposing a feature

Open an issue (there's a template) that describes the problem before the solution: what you were trying to do,
and what got in the way. For anything bigger than a small fix, wait for a reply on the issue before writing much
code, so the approach can be agreed first. [docs/ROADMAP.md](docs/ROADMAP.md) lists what's already planned.

Good proposals say which kind of library they help (a shoot to cull, or an archive to browse and tidy), and how
they keep the rules above.

## Pull requests

* One topic per pull request, with tests.
* Describe what changed and how you checked it, including what you looked at in the app.
* Update the README if you change what people see or type, and add a line to `CHANGELOG.md`.
* Make sure `JOEYLAB_OFFLINE=1 HF_HUB_OFFLINE=1 uv run pytest -q` passes.

By contributing, you agree that your contribution is released under the project's [MIT licence](LICENSE).

## Reporting a security problem

Please don't open a public issue. [SECURITY.md](SECURITY.md) says how to report it privately.
