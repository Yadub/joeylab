# Roadmap

What's planned after 0.9.0, roughly in order of how much it would help. Nothing here is promised by a date. If
one of these matters to you, say so in an issue; if you'd like to work on one, open an issue first so we can agree
on the approach (see [CONTRIBUTING.md](../CONTRIBUTING.md)).

Every item keeps JoeyLab's founding rule: your photos are only ever read.

## Lighter and faster

* **One embedding model.** Use SigLIP 2 for everything (grouping, similar photos, aesthetics, taste) and drop
  CLIP ViT-L/14. This roughly halves the first-run download (about 3.4 GB today) and the memory used. It needs a
  labelled comparison first, so that picks and groups don't shift silently for existing libraries.
* **The Cull list in pages.** Cull loads every stack at once, which is about 15 MB of data with 500,000 undecided
  photos. Loading it in pages keeps very large libraries quick.
* **Updates the app can see.** When JoeyLab itself is updated, an open tab keeps running the old code until it
  reloads. The app should say "JoeyLab was updated · Reload".

## Better at what it already does

* **Event names by what they show.** Events are named from their photos ("Climbing", "Wedding", "Dance
  performance"). Each kind's accuracy should be measured against hand-named events, and the weaker kinds improved.
* **More unreadable RAW files recovered.** Some RAW files can't be opened by LibRaw; today they're shown from
  their embedded preview when there is one. A newer LibRaw, or exiftool's preview extraction, may recover more.
* **Across the years, for unnamed groups.** A child's face groups from different ages are offered as someone
  you've named. Two unnamed groups of the same child should also be offered to each other.

## New

* **Apple Photos import.** Today Add folder can point at the Photos library's originals. Reading its favourites,
  albums and people as well would bring that work across.
* **Keywords embedded in photo files.** Keywords are read from XMP sidecars and Lightroom catalogs, but not yet from
  inside the photo files themselves.
* **A gear page.** Cameras are listed by body on Home, and lens, focal length and ISO are filters. A page of their
  own would show what you shoot with and how often.
* **One command layer.** The command line, the web API and the MCP server share the library functions, but each
  defines its own commands. A single registry would keep them in step and make new commands available everywhere
  at once.

## Packaging and platforms

* **PyPI**, published from GitHub Actions, so installing is `uv tool install "joeylab[all]"` plus exiftool.
* **A Homebrew tap**, for a one-line install that also brings exiftool.
* **Better face models with a permissive licence.** Face recognition offers OpenCV's YuNet (MIT) and SFace
  (Apache-2.0) beside InsightFace's non-commercial models, but they group people less well. A stronger recogniser
  that may be used commercially would close the gap.
* **Model downloads pinned** to fixed versions and checked against their SHA-256.
* **Linux support for the core** (scanning, culling, search, the web app), without Apple Vision or launchd.
