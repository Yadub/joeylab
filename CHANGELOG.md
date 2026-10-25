# Changelog

Notable changes to JoeyLab, newest first. Versions follow [semantic versioning](https://semver.org); until 1.0,
a minor version may change behaviour, and the changelog says how.

## 0.9.0 (unreleased)

The first public release. JoeyLab is a private photo library that runs on your Mac and only reads your photos.
What it does today:

### Libraries and safety

* Reads folders on the Mac and on external drives, incrementally: added, edited, moved, renamed and deleted files
  are reconciled on every update. Moved files keep their decisions; deleted ones are marked missing, not
  forgotten; unplugged drives are skipped, not emptied.
* External drives are known by their volume ID, so a renamed drive is followed and a different drive with the old
  name is held back.
* Duplicates are copies on the same drive. An identical copy on another drive is a backup: never an extra copy,
  never suggested for rejection and never moved to the Trash for being one. One small index shared by every library
  (`~/.joeylab/copies.db`) finds backups across libraries and drives; the same size and fingerprint make a probable
  backup, and the background service reads both files whole, slowly, to check it. Tidy › Backups shows, per drive
  and folder, what's backed up and where, and what exists on one drive only (drives that aren't connected say when
  they were last seen). The viewer, the Library's filters, the Duplicates page, the Trash dialogs, `joeylab backups`
  and MCP say it too.
* Everything it learns goes into a SQLite catalog in `~/.joeylab`. After every update, the untouched check
  confirms that every tracked file and folder is exactly as before.
* Every decision is logged; undo and redo work across the app, the command line and assistants, and survive a
  reload.
* Catalogs are backed up daily (14 daily, 8 weekly, 12 monthly copies) and before every upgrade.
* A folder is checked before it becomes a library (it's there, it can be read, it has photos), with a plain reason
  when it can't be used. Libraries get short names (`smith-wedding`), and can be renamed or removed (JoeyLab's
  records only; removing keeps a backup of the catalog). Files that can't be read aren't counted as photos.
* A folder belongs to one library. Folders can be moved to another library, new or existing, with everything known
  about their photos (decisions, faces and names, keywords, history, albums, backups, thumbnails and previews), so
  nothing is read again; both catalogs are backed up first, the copy is checked before anything leaves the source,
  and it can be moved back. From Library › Folders, Settings, or `joeylab library split`. On a library of 75,000
  photos, moving 72,000 of them (with 330,000 faces) took about two minutes.
* The first update stacks, finds duplicates and suggests picks straight after analysis; search, faces and places
  follow, and each page says what it's waiting for. Apple Vision and the graphics processor have time limits, so
  one stuck photo can't hang an update.
* Rejected photos, extra copies and empty folders can be moved to the macOS Trash by the owner, after a
  read-only check of what stays behind for each file, a typed count, and a re-check before each move.
* Photos and folders can be moved and renamed from the app (owner only): rename a folder, make one, move a folder
  or selected photos to a folder, or rename photos' files from a pattern. A photo moves with all its files and
  sidecars; nothing is overwritten; to another drive files are copied and checked before the originals go to the
  Trash. The catalog follows at once, every operation is logged before anything moves and can be moved back, and
  one cut short is finished from its log.
* Moves made in Finder are followed cheaply: a renamed folder of 2,000 photos is recognised without reading any of
  them (by inode on the same drive, or by name, size and time with two files checked), and a renamed library
  folder is followed.
* RAW + JPEG: keep the RAWs you may edit again, reject the rest and move the rejected ones to the Trash. Keep RAW
  or Reject RAW per event or trip (RAWs… in its menu), per selection, or per photo (R in the viewer); rules keep
  the RAW of picks, ★3 and up, favourites, album photos and edited ones, and only rejected RAWs ever go. The photo stays, shown from its JPEG, with every decision; only a JPEG that is
  there and reads whole lets its RAW go, and the RAW's own sidecar goes with it. Tidy › RAW + JPEG and
  `joeylab raw-list`.

### Culling and finding

* Bursts and scenes stacked along each camera's timeline, with a five-step Stacks slider.
* A 0–100 quality score from sharpness, exposure, faces (closed eyes, smiles) and aesthetics, with pick, reject
  and "worth checking" suggestions that never become decisions unless accepted.
* A taste model that learns from your picks and rejects once it predicts them reliably.
* Search by description with SigLIP 2 on the whole frame and 9 tiles, Apple Vision labels and text recognition,
  plain-language queries and field filters, all on the Mac.
* Similar photos, best-of sets, balanced selections and a shot list.
* Library › Explore: the whole library on one surface you zoom and pan like a map, arranged by what's in the
  photos (regions named with plain words by the search model, on the Mac), colour (a wheel of hues, black and white
  in the middle), light, time, quality or camera, each explained on the page. Switching glides every photo to its
  new place; far out each photo is a square of its main colour, closer in its thumbnail. Hover to peek, click to
  open, drag a box to select, and the Library's filters choose which photos show without moving the rest.

### People, places and events

* Face recognition, off until you turn it on and choose its models: the most accurate (InsightFace SCRFD and
  ArcFace; personal, non-commercial use only) or ones free for any use, including paid work (OpenCV's YuNet and
  SFace). Switching later finds every face again and keeps your names. Name a group once, "not them" corrections,
  unclear groups set aside, children's groups offered across the years.
* People can have other names ("Dad" is also "Papa"), found by search, ⌘K, naming faces and assistants; merging
  two people keeps both names, and every change can be undone.
* Places from GPS, worked out offline with the GeoNames town list; photos without GPS borrow a nearby photo's place.
  A map drawn on the Mac from Natural Earth's outlines (OpenStreetMap's street map only if you turn it on), one
  circle per place, and "Open in Google Maps" (or Apple Maps) links that send nothing until followed.
* Events and trips found from time and place across every camera, named from folders or from what the photos
  show; camera clock offsets found by matching photos of the same moment.
* Trips follow your homes (set for the whole Mac, or JoeyLab's guess, shown): an event further than the trip
  distance from every home, or in another country, is away, and a single day abroad is a trip. Events without
  GPS are placed by folder names and camera time zones, as guesses, or by you with Set place….

### Tidying

* Exact duplicates and re-saved copies, grouped by the folders they share; renamed exports linked to their
  originals as edits.
* Library health: photos on only one drive, damaged and unreadable files, undated photos, wrong extensions,
  orphan sidecars, empty folders.

### Working with others and other apps

* A web app for the Mac and phones, with keyboard shortcuts, a command palette, bulk changes on everything a filter
  matches, and swipe culling on a phone.
* One user per install: only you can open JoeyLab (at the Mac, or on your own devices over Tailscale); anyone
  else sees only share links you send them.
* Listens only on the Mac by default; `--tailscale` publishes it to your own tailnet; `share-server` serves share
  links alone.
* Albums, smart albums, share links with an expiry, link and copy folders, and self-contained galleries.
* Keywords in a tree, with XMP, Lightroom and sidecar round trips.
* Import from Lightroom Classic catalogs, XMP and EXIF ratings, and JoeyLab's portable decisions file; export
  lists, CSV, XMP sidecars and the portable decisions file.
* An MCP server so any MCP client (for example Codex or Claude Code) can search and organise a library, with a
  read-only option.
* A command line for everything, including culling in the terminal with inline images.
* A background service that starts at login, and a demo library of generated photos (`joeylab demo`).
