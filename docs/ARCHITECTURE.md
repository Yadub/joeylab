# How JoeyLab is built

This is for contributors: what each part does, how the parts fit, and the rules that keep users' photos safe.
The [README](../README.md) describes what JoeyLab does for people using it.

JoeyLab is one Python package, `src/joeylab`, with three front ends on one core:

* the **command line** (`cli.py`, Typer);
* the **web app** (`web/`, FastAPI and a single-page app in plain JavaScript modules, no build step);
* the **MCP server** (`mcp.py`, JSON-RPC over stdio) for AI assistants.

All three read and write the same SQLite catalog through the same library functions. None of them talks to the
others; they coordinate through the catalog and a few lock files.

## Where things live

Everything JoeyLab writes goes under `~/.joeylab` (or `$JOEYLAB_HOME`, which the tests set to a temporary
folder):

| Path | What |
|---|---|
| `projects/<library>/catalog.db` | the catalog: one SQLite file per library, in WAL mode |
| `projects/<library>/thumbs/` | a 768 px JPEG of every photo, made during analysis; the AI and search stages read these, not the originals |
| `projects/<library>/thumbs_s/`, `previews/` | smaller thumbnails, 2048 px previews and zoom tiles for the web app, made on demand (previews also ahead of culling) and pruned by size |
| `projects/<library>/atlas/` | Explore's small thumbnails, a thousand to an image, made on demand and replaced when their photos change |
| `projects/<library>/overview.json`, `explore/` | the last full count of the library's overview (`web/overview.py`) and Explore's arrangements other than "what's in them", each with what it was made from, so a restart opens at once |
| `projects/<library>/update.lock` | one update at a time per library (`locks.py`) |
| `models/` | the aesthetic predictor, the MediaPipe model, the chosen face models (InsightFace's or OpenCV's), the GeoNames town list, cached text vectors |
| `cache/search/` | the search vectors as one memory-mapped file per library, rebuilt from the catalog whenever it changes |
| `backups/<library>/` | catalog backups (`backup.py`) |
| `backups/removed/<library>-<time>/` | a removed library's last catalog, its backups and its logs (`libraries.py`) |
| `copies.db` | the index of copies every library shares: each file's library, photo, drive and path on it, size and fingerprints, to find backups on other drives (`copies.py`) |
| `projects/<library>/copies.lock` | one refresh of a library's rows in that index at a time |
| `taste.npz`, `taste.json` | the taste model, trained on every library's decisions |
| `xmp-backups/` | earlier versions of XMP sidecars JoeyLab updated |
| `trash-log/`, `trash.lock` | a log of every move to the Trash; one move at a time on this Mac |
| `move-log/` | a log of every move and rename made in the app, written before anything moves |
| `split-log/`, `staging/` | a log of every move of folders to another library (`split.py`); a new library being written, before it takes its name |
| `logs/` | the background service's output |

The SigLIP 2 and CLIP weights are fetched by open_clip through the Hugging Face cache (`~/.cache/huggingface`).
`config.offline_first` loads them without asking the Hub for updates once they're cached.

## The pipeline

`pipeline.run_all` runs one update of a library. The CLI (`joeylab run`), the web app's jobs and the folder
watcher all go through it, under the library's update lock:

```
scan ──► analyse ──► group ──► faces ──► AI embeddings ──► search index ──► refine ──► untouched check
                       │                                                     │
          dupes, grouping, selections, scoring        faces.cluster, taste, geo, events, grouping again
          (Cull, Duplicates and suggestions work       (with the new vectors) and scoring again
           from here on)
```

The useful steps come first: `group` runs straight after analysis, from capture times, camera bodies and
perceptual hashes (plus any vectors from earlier updates), so stacks, duplicates and suggestions are there within
seconds of the photos being analysed. The slow, optional steps follow, and `refine` folds in what they found.

Each step reports itself through `Reporter.step()` and `Reporter.finished()` (`pipeline.STEPS`); a web job keeps
the current step and the finished ones, and pages that wait on one say so (`views/waiting.js`). Each stage only
works on what changed since the last run, so an update with nothing new takes seconds.

Work that can hang in native code has a time limit (`deadline.py`): Apple Vision gets `VISION_TIMEOUT_S` a photo
and the graphics processor `GPU_TIMEOUT_S` a batch. Past it, Vision is left out for the rest of that update, or
the indexing step stops and its model is reloaded on the CPU; what was done is kept, the update carries on, and
the next update picks up the rest.

### Scan (`scanner.py`, `exif.py`, `volumes.py`, `hashing.py`)

* Walks every tracked folder (a **root**) and reconciles the catalog with what's on disk: new files are added,
  changed files (size or mtime) are re-fingerprinted, files that disappeared are matched against new paths (a
  move or rename keeps its decisions), and anything left over is marked `missing`, never forgotten automatically.
* Moves are matched without reading the files where possible (`_moved_without_reading`): first the same inode,
  size and mtime on the same drive (a move or rename in Finder keeps all three); then, for drives that give a
  moved file a new inode, the same name, size and mtime in the folder most of its neighbours went to, with two
  files per pair of folders read to be sure. Made-up inode numbers (exFAT under macOS's FSKit numbers files down
  from 2^64) and numbers two files share are ignored. Only what's left is fingerprinted. A library folder renamed
  in place is followed by its own inode (`volumes._renamed_in_place`).
* The fingerprint (`quick_fp`) is the size plus the first and last 256 KB. A full hash is computed only when
  needed (exact duplicates among files of the same size, and Trash checks).
* Files modified in the last few seconds are left for the next scan (`SETTLE_SECONDS`): they may still be copying.
* Unavailable roots (an unplugged drive) and unreadable folders are skipped, not emptied.
* Folders the library leaves out (`excluded_folders`: moved to another library, see below) are never walked, and
  nothing under them counts as missing. The folder watcher ignores changes there too.
* A RAW and a JPEG with the same name in one folder, shot within 2 seconds, become one **shot**; files are the
  individual RAW, JPEG, HEIC or video files. A Live Photo's `.MOV` joins its still.
* Metadata comes from one batched `exiftool` call per set of new files.
* `volumes.py` remembers an external drive by its volume UUID, so a renamed drive is followed and a different
  drive mounted under the old name is held back rather than scanned.
* The scan also records size, mtime, ctime and inode for every file and folder (`ScanReport.stats`) for the
  untouched check at the end.

### Analyse (`analysis.py`, `imaging.py`)

Runs in a pool of worker processes (`spawn`). Each worker decodes a downscaled preview (JPEG DCT scaling, or a
RAW file's embedded JPEG), writes the 768 px thumbnail, and measures sharpness (Laplacian variance on tiles and
faces), exposure, perceptual hashes (pHash, dHash) and, with MediaPipe, closed eyes and smiles. A file that reads
fine but can't be decoded is set aside until it changes; one that vanished mid-read is retried next time.
`ANALYSIS_VERSION` is stored with the results; bump it when analysis changes, and existing catalogs re-analyse.

Each photo's colours (`colours.py`: the main hue, how colourful and how light it is, the colour most of it is and
a three-colour palette, in OKLab) are measured from its thumbnail straight after `group`, a few milliseconds a
photo, into the `colours` table. A library that predates them is measured in the background the first time
Explore needs them (`colours.backfill`, which only adds colour rows and so takes no update lock).

### Faces (`faces.py`)

Finds every face and turns it into a vector that identifies the person, on a 2048 px preview of each photo read
from the original, so small faces in group shots are found too. It checks eyes and smiles per face with MediaPipe on
the crop. Face recognition is off until the user turns it on and chooses one of two sets of models (`faces.MODELS`,
kept in `settings.json` with the opt-in):

| Set | Finds faces | Recognises them | Vector | Licence |
|---|---|---|---|---|
| `insightface`, "Most accurate" | SCRFD (ONNX Runtime) | ArcFace | 512 numbers | non-commercial research use only |
| `opencv`, "Free for any use" | YuNet (`cv2.FaceDetectorYN`), at two sizes since it finds faces of about 10 to 300 px | SFace (`cv2.FaceRecognizerSF`) | 128 numbers | MIT and Apache-2.0 |

Nothing is chosen for a new install, and nothing is downloaded before a choice (`ensure_models` downloads only the
chosen set, pinned by SHA-256). An install that had InsightFace's models before there was a choice counts as having
chosen them (`current()`).

The two sets' vectors can't be compared, so every face records which set found it (`faces.model`), and everything
that reads `faces.vec` uses only the chosen set's faces (`active()`): grouping and cohesion, naming a group,
"Is this …?" suggestions, the same person across the years, and a burst's faces side by side. Each set has its own
similarity bars (`limits()`); SFace's were measured on a real library of about 25,000 faces with 15 named people
(see the comment in `MODELS`).

A switch finds every face again: each photo's faces stamp (`shots.faces_fp`) names the set's version, so every
photo is due at the next update. A face found again keeps what the user said about the face in the same place
(`_carry`: centres within half a face; each old face goes to its nearest): a name, or "not this person", always,
and on a switch also its group and the grouping's person, so groups keep their ids and `face_not_same` answers keep
pointing at the right group. People, other names, "me" (kept by name) and hidden people aren't stored per face, so
they stay as they are. The library's `faces_model` setting records which set it was last fully grouped with; until
it matches, `cluster()` regroups from scratch, and `switch_progress()` tells People and Settings how far it is.

`cluster()` groups faces into people:

* two faces in one photo are never put in one group, and groups that share a photo are never merged;
* named people anchor the groups, and "not them" answers are remembered (`face_not_same`);
* a full regrouping runs at most once a day or when many faces are new; otherwise new faces are placed into the
  existing groups;
* group ids are stable and never reused, so names and links keep pointing at the same faces.

`across_years()` suggests unnamed groups that are a named person at another age.

The InsightFace models are for non-commercial use only, which is why the choice is offered and nothing is chosen
by default.

### AI embeddings and search (`ai.py`, `search.py`, `embeddings.py`, `scenes.py`)

These stages read the 768 px thumbnails, not the originals.

* `ai.py` embeds each thumbnail with CLIP ViT-L/14 (open_clip) and scores it with the LAION aesthetic predictor.
  These feed scene grouping, similar photos, taste and the quality score.
* `search.py` indexes each thumbnail with SigLIP 2 (`ViT-B-16-SigLIP2-384`) on the whole frame plus 9 overlapping
  tiles, and on macOS with Apple Vision labels and text recognition (`pyobjc-framework-vision`). A search combines
  SigLIP's calibrated match probability with label and text hits. The vectors are read from one memory-mapped
  float16 file in `cache/search`, stamped with a counter the indexer bumps (`meta.search_gen`) so it's never stale.
* `embeddings.py` gives every other module one whole-frame vector per photo (SigLIP 2 when there is one, else
  CLIP), cached in memory until a counter changes.
* `scenes.py` names events by what they show ("Climbing", "Wedding") by comparing photos with a short list of
  descriptions, on the Mac.
* `explore.py` makes Library › Explore's arrangements: every analysed photo gets a cell of its own on a grid
  (`snap`), placed by what it shows (t-SNE on the vectors, regions found by k-means and named from a list of plain
  words with the same model's text encoder), by colour, light, time, quality or camera. Measured axes are spaced
  by rank. Positions come from the whole library; filters only hide photos. The picture-vector layout is made in
  the background and kept in the catalog's settings; the others take up to a couple of seconds on a library of
  75,000 photos and are kept in memory and in the library's folder (`explore/`), each with the key of what it was
  made from. Small thumbnails are served a thousand to an image (`atlas_sheet`, cached in the library's folder).
* `query.py` turns the search box into filters: fields (`person:`, `place:`, `is:pick`, `rating:>3`, `#keyword`)
  and plain language ("Sam in Lisbon in 2023 that I haven't picked"), read locally; what's left is the picture
  description.

The `ai` extra brings PyTorch and open_clip; without it, grouping falls back to time and perceptual hashes, and
search is unavailable. JoeyLab never calls a remote AI service.

### Group and refine (organise)

`pipeline.group` and `pipeline.refine` run the fast, catalog-only steps (no image decoding); `pipeline.organize`
(`joeylab organize`) runs both at once:

* `faces.cluster` (above);
* `taste.train_all` (`taste.py`): a regularised logistic regression on quality signals and the CLIP embedding,
  trained on every library's picks and rejects. It's only allowed to shape scores once cross-validation shows it
  ranks keepers above rejects reliably;
* `grouping.run` (`grouping.py`): bursts and scenes along each camera body's timeline, by time gap and pHash
  distance, plus embedding similarity with the `ai` extra. The strictness is the Stacks level (1–5,
  `config.GROUPING_LEVELS`). Group ids are stable across runs;
* `dupes.run` (`dupes.py`): exact duplicates (identical bytes) and copies (the same capture instant and
  near-identical pixels), with one member of each set marked to keep; renamed or re-exported edits are linked to
  their original instead. Sets are formed per drive (`volumes.drive_keys`): identical files on different drives of
  one library are a `backup` set instead, never removable, whose one shown copy (`choose_shown`) is culled and
  stacked while the others are flagged `backup-copy` and left out of culling, stacks and events;
* `selections.update` (`selections.py`): folders that are a hand-picked subset of another folder, saved as albums
  pointing at the original files;
* `geo.assign` (`geo.py`): GPS to the nearest town in the GeoNames list, offline; photos without GPS borrow the
  place of one taken within 3 hours (a place you set on an event is kept);
* `events.run` (`events.py`): events (gaps of 3 hours or jumps of 100 km) and trips (runs of events further than
  the trip distance from every home, or abroad; homes and the distance are app-wide, in `settings.json`), with
  stable ids so names and merges stay. An event without GPS takes a guessed place from its folders' names
  (`Gazetteer.folder_place`) or its cameras' time zone; the rule trips were found by is kept per library, and a
  page that finds it out of date finds them again in the background; `clocksync.py` finds camera clock offsets, applied to the shared timeline
  only;
* `scoring.run` (`scoring.py`): the 0–100 quality score, flags and suggestions (pick, reject, worth checking).

### Backups across drives and libraries (`copies.py`)

Each library is its own catalog, so one can't see another's files. `~/.joeylab/copies.db` is a small index every
library shares, holding only what identifies a file: its library and photo (the catalog's ids), its drive (the
volume UUID, or `mac` for this Mac's own disk) and path on that drive, its size, the scan's quick fingerprint, its
full content hash once read, and when it was last seen. It has its own schema version and is only ever derived
from the catalogs: a damaged one is set aside and built again.

* **Refresh.** After `group`, each update refreshes its library's rows (`refresh`): only what changed is written,
  in transactions of 2,000 rows (WAL; `copies.lock` allows one refresh per library). Rows are kept by file id,
  which the scanner and `moves.py` keep across moves and renames, so the index follows Finder and in-app moves.
  Files that have gone leave; files on a drive that isn't connected keep their rows, and the drive its last-seen
  time. A folder on a disk mounted outside `/Volumes` can't be asked who it is while it's away, so each library
  remembers its folders' drives (`volumes.remember_drives`). Moves and Trash runs in the app refresh at once.
* **Probable and checked.** The same size and fingerprint on another drive is a probable backup. `confirm` reads
  the candidates whole, read-only, paced (40 MB/s in the background), only on connected drives and only files
  exactly as indexed; the background service runs it between updates and stops as soon as one starts. A
  re-saved or exported version is a different file, so it is never a backup.
* **Into the catalog.** `sync` copies what concerns a library into its catalog's `backups` table (m14), whenever
  the index's generation has changed. The Library's `backup` filter (`elsewhere`, `checked`, `only`), the rail's
  counts and Tidy › Backups read that table; the viewer, the Trash dialogs and MCP ask the index for details.
* **Information only.** A backup never makes a file go: the Trash check counts a copy as staying only on the same
  drive, and its dialogs name only checked backups.

### The untouched check (`integrity.py`)

After the update, every path the scan recorded is `lstat`-ed again, and size, mtime, ctime and inode are compared.
The result goes into the `integrity` table and Tidy shows it: "all N files and folders are exactly as before the
update", or the list of what changed. JoeyLab never writes there, so a difference means another app did. Keep
this check meaningful: nothing in the pipeline may write, touch, or set extended attributes on a tracked file or
folder.

## The catalog (`db.py`)

One SQLite file per library. The main tables:

* `roots`: the tracked folders, and `excluded_folders`: folders inside them the library leaves out;
* `files` and `shots`: every file, and every photo (a RAW and its JPEG share a shot), each with a `status` of
  `present` or `missing`;
* `metrics`, `colours`, `groups`, `dup_sets`, `dup_members`, `faces`, `people`, `places`, `events`: what JoeyLab
  computed;
* `backups`: which of the library's files have an identical copy on another drive, copied from `copies.db`;
* `person_aliases`: each person's other names, any one name (whatever its case) belonging to one person;
* `decisions`: the user's stars, picks and labels, kept apart from computed data so re-analysis never touches
  them, and kept when a file goes missing in case it comes back;
* `decision_log`: every change to decisions or keywords, append-only. Undo and redo replay it, which is why undo
  survives a reload and works across devices; each undo and each redo is logged as a step of its own, and redo is
  possible only until the next new decision. Each entry records its source (web, cli, import, an assistant, undo,
  redo) and the viewer;
* `albums`, `album_items`, `keywords`, `shot_keywords`, `shares`, `comments`, `viewer_decisions`: the user's work
  and other viewers' favourites and comments;
* `meta` (counters that tell in-memory caches to reload, and id high-water marks) and `settings` (the library's
  settings, and the frames you tagged as covering each `shotlist` item, which follow a photo when two of its files
  are joined or a copy of it stays behind). One counter, `counts:gen` (`db.COUNTS_GEN`), is kept by triggers on
  decisions, photos, files, metrics, stacks and duplicate sets (`db.COUNTED`), in the same transaction as the
  change, whoever makes it: the overview's counts that confirm your actions are counted again when it moves.

**Big libraries.** Counting rows of a table of vectors reads every row (a library of 75,000 photos keeps over a
gigabyte of search vectors), so `embeddings`, `search_vecs` and `vision` have an index on their photo column
(`db.COUNT_INDEXES`) and are counted from it. Faces are read by group or by person from two covering indexes
(`faces_by_group`, `faces_by_person`), never their vectors, except through `faces.vectors()`, which keeps every
face's vector in memory (a face's vector never changes; new faces have higher ids). Queries that read most of the
library write `+s.status` (or `Filter.sql(scan=True)`): nearly every photo is present, and reading them in one pass
beats looking each up from the status index, which sampled statistics make the planner prefer.

Ids in `shots`, `files` and `faces` are never reused (`db.new_id`), so nothing left pointing at an old id lands on
a different photo.

**Migrations.** `db.MIGRATIONS` is a numbered list of steps; `PRAGMA user_version` records how many have run.
`connect()` runs the missing steps in one transaction, after copying the catalog to
`backups/<library>/pre-upgrade-v<N>-….db`. A failed upgrade is rolled back and reported, leaving the catalog as it
was. Steps only add tables, columns and indexes. To change the schema, add a new function at the end of the list;
never edit one that has shipped.

**Backups** (`backup.py`). The web server backs up every catalog daily, after `PRAGMA quick_check`, and keeps
14 daily, 8 weekly and 12 monthly copies. Daily copies leave out the search index (rebuilt from thumbnails); weekly
and monthly ones are full. `restore` keeps the catalog it replaces.

## Concurrency

* `locks.project_lock` (an `fcntl` lock on `update.lock`) allows one update per library at a time, across the CLI,
  the service and background re-scoring.
* `cleanup.trash_lock` allows one move to the Trash at a time on the Mac, across the app and Terminal.
* Moves and renames made in the app hold the library's update lock, so a scan (the folder watcher's, say) never
  runs while one is half done, and the untouched check always compares files as they were when its update began.
* SQLite runs in WAL mode, so readers (the web app) aren't blocked while an update writes.
* `copies.db` is written by every library's update and by the background service: WAL, short transactions, and
  one refresh per library at a time (`copies.lock`).

## The web app (`web/`)

* `server.py` builds the FastAPI app: the request guard, image routes (`/img/...`: thumbnails, previews, 512 px
  tiles of the full size for 50% and 100% zoom, face crops, the original file), the share page and the
  single-page app. `Background` re-scores, regroups and retrains taste off the request, respecting the update
  lock; `Prerender` makes the undecided photos' previews ahead of culling (bounded, one at a time, not on
  battery), writing only to the preview cache.
* `overview.py` keeps each library's overview (the top bar's counts, Home and the rail). A request is answered at
  once from the last full count, even if the catalog changed since (`db.file_stamp`); a newer full count runs in
  the background, one at a time per library, a moment after the change, at most every 10 seconds, and every
  5 minutes while an update analyses, finds faces or indexes. The counts pages confirm your actions with (photos,
  picks and rejects, what's left to cull, suggestions, duplicate sets) are counted again (about 0.1 s) whenever
  `counts:gen` has moved, so they're exact straight after a decision, an Undo or a move to the Trash. The last full
  count is kept in `overview.json`; `/jobs/current` reports its `seq`, and the page fetches the overview again when
  it changes (Home draws the new numbers in place).
* `routes/` holds the JSON API under `/api/p/<library>/...`, one module per area: `library` (browsing, decisions,
  undo, history, settings), `people`, `places` (and events), `ai` (search, similar, sets), `explore` (Library › Explore),
  `keywords`, `share` (albums, exports, imports, share links, favourites, comments), `tidy` (duplicates, health,
  clocks, backups), `trash`, `history` (the History page and Put Back) and `moves` (moving and renaming).
* `deps.py` holds the shared pieces: catalog connections per request, filters from query strings, and the guards.
  `guard_problem` refuses requests for host names that aren't this Mac's (DNS rebinding) and changes that don't
  come from JoeyLab's own pages (an `Origin` or `Sec-Fetch-Site` from another site, or a non-JSON body).
  `is_owner` decides who may open the app at all (one user: the owner); see
  [SECURITY.md](../SECURITY.md) for the model.
* `net.py` decides where to listen and who may connect (loopback by default, the tailnet with `--tailscale`) and
  drives `tailscale serve`.
* `jobs.py` runs pipeline updates in the background, one per library, plus the periodic check and the daily
  backup. `watch.py` reacts to file-system events (watchdog, FSEvents on macOS) and starts an update once a folder
  has been quiet for a while.
* `static/` is the front end: `app.js` (router, top bar, command palette, keys), `store.js` (state), `api.js`,
  `grid.js` (a windowed justified grid), `loupe.js` (the viewer), `ui.js` (DOM helpers), and one module per page
  in `views/`. Fonts are served locally, so no page calls out to a font service. `app.css` starts with the design
  tokens: every colour as a `light-dark()` pair (the theme only sets `color-scheme`; the viewer and Cull stage are
  always dark), a type scale (`--fs-*`) and corner radii (`--radius-*`). Rules use these rather than literal values,
  and each colour pairing is chosen to meet WCAG AA in both themes. `views/keys.js` is the shortcuts help (each
  page's keys, by page; gestures on touch), and `views/waiting.js` the "still working" notes. `views/explore.js` draws
  Library › Explore on a canvas: tiny photos as one image of colour squares, then atlas thumbnails, then each
  photo's own thumbnail for what's on screen; it reads its colours with `ui.cssColors()` like every canvas.

`service.py` installs `joeylab serve` as a launchd agent so it runs at login.

`libraries.py` handles libraries as a whole: the name a library is saved under (a slug), the check a folder gets
before it's added (there, readable, with photos, counted quickly, and in no other library: a folder belongs to one
library, `claims` and `overlap`), and renaming or removing a library. All of it is JoeyLab's own data;
removing one backs its catalog up into `backups/removed/` first.

### Moving folders to another library (`split.py`, `web/routes/split.py`, `views/splitlib.js`)

A library's purpose decides how its photos are treated, so folders can be moved to a library of their own (new,
with its purpose, or existing) without reading anything again. `plan()` works out what would move, read-only (the
photos whose files are under the folders, with every file of each, and their faces), and says it in a sentence;
`run()` does it, catalog only:

1. Both libraries' update locks are taken, and the source's write lock (`BEGIN IMMEDIATE`) is held until its rows
   are gone, so nothing changes it meanwhile. Both catalogs are backed up (`backups/<library>/before-move-<id>-…`;
   `backup.prune` leaves these alone).
2. The other library is written in one transaction, from the source's backup attached as `s`: `INSERT … SELECT`
   per table, through temporary id maps. Every row keyed by photo, file or face goes (`SIMPLE`, faces, files,
   shots, `search_fts`, keywords and their links, decisions and `decision_log`, `raw_choices`, `manual_dates`,
   album items, backups, comments, favourites), with the stacks, events and face groups they're in, the people they're
   named as (joined to the person of the same name or other name there), keywords (by their place in the tree),
   albums (by name, or folder), places and camera clock offsets, and the settings kept by folder or photo (folder
   names, keep rules, merged events, shot-list tags). Tables only the source has are made first. Rows per table are
   compared with the backup before committing; a new library is written in `staging/<id>` and renamed into place.
3. Only then are the rows deleted from the source (rows, never tables), in one transaction, and the folders added
   to its `excluded_folders` (a folder that was one of its roots is simply removed). People, albums, stacks and
   events left with nothing are removed with it.
4. The thumbnails, previews, tiles and face crops are renamed into the other library's folder under their ids
   there, after the source's catalog is done: a stop leaves the source as it was before step 3, or (after it)
   loses no image, since `finish()` completes the rest from the log. The shared index's rows follow
   (`copies.move_rows`), keeping what's known of each file's content.

Afterwards each library is regrouped in the background (`recompute`: `pipeline.organize`, face groups' agreement,
its search cache). A move is logged in `split-log/` and can be moved back (`move_back`, the same operation in
reverse); a library a move made is removed once it's empty again.

**Ids.** In a new library every id stays as it is. In an existing one, an id never used there is kept, and the rest
get new ones above its high-water marks. Each library remembers in its settings which rows it gave to which
library and their ids there (`moved_out`, kept as runs of consecutive ids), so rows coming back take their old ids
again (a split stack or face group joins its other half); history that arrives from elsewhere goes before the
library's own steps, so its Undo reaches its own changes first. Ids are never handed out twice, so history and
albums never point at the wrong photo.

**Words:** the app and the command line use one word per idea: library (never "project", which is only the
code's old name for it), catalog, update, stack, burst, duplicate set and extra copy, folder group, face group,
pick and reject, suggestion. The README's "Words JoeyLab uses" says what each means; keep new text to those.

## MCP (`mcp.py`)

`joeylab mcp` speaks MCP (JSON-RPC 2.0, one message per line on stdio). Read tools list libraries, search, show a
photo's details (with its backups on other drives) and its 768 px thumbnail (never the original), and list people, events, places, keywords and
library health. With changes allowed, `set_decisions`, `create_album`, `tag_photos` and `undo_last` are added. They
go through the same `library.decide` and `keywords.assign` as the app, logged under the client's name, so the user
can undo them. MCP never touches photo files.

## Writing files: exports, the Trash, and moving and renaming

JoeyLab writes outside its own folder only when the user asks, and only through these paths:

* **Album folders, link folders, galleries, lists, CSV and JSON** (`export.py`, `selections.py`, `portable.py`).
  `export.check_dest` and `check_file_dest` refuse any destination inside a tracked folder (of any library) or
  inside `~/.joeylab`, comparing by file identity rather than spelling. A list or CSV may only replace an
  earlier export of the same kind. `--clean` removes only links JoeyLab recorded making.
* **XMP sidecars** (`export.write_sidecars`): only with `--write-sidecars`; new `.xmp` files are added, existing
  ones are updated only when asked, backed up first, and never get a lower rating than another app set.
* **The Trash** (`cleanup.py`, `web/routes/trash.py`). Moving rejected photos, duplicate extras or empty folders:
  * `plan()` checks every listed file first, read-only: it must be a present file of this library, unchanged since
    JoeyLab last read it, and it works out what stays behind on the same drive (an identical copy compared
    byte for byte, a re-saved copy, the photo's other RAW or JPEG file, or nothing). A backup on another drive is
    listed per file, for information, and never makes a file go. A path that is the same file as its "copy"
    (same device and inode) never counts as a copy.
  * The user sees the summary and types the count. Last copies go only when asked for.
  * Just before each move, the file and the copy that stays are checked again, and the web app re-checks that the
    photo is still rejected.
  * Moves use macOS's own Trash (`NSFileManager.trashItemAtURL`), so Finder's Put Back works. Every run is logged
    in `trash-log/`.
  * Only the owner can do this in the app.
* **Rejected RAWs** (`raws.py`, `web/routes/raws.py`, Tidy › RAW + JPEG, an event's RAWs…, `joeylab raw-list --trash`):
  the RAW file of a photo that also has a JPEG (or HEIC, TIFF), when you chose Reject RAW for it (per photo, per
  selection or per event). RAWs no rule keeps (picks, stars, favourites, albums, edited) are only "undecided" and stay
  unless you reject them; `raw-list --include-unkept` and `plan(include_unkept=True)` take them too. The same steps as above, and per RAW:
  the picture that stays must be there, not empty and decode whole (`imaging.decodes`), at the check and again just
  before the move, when the photo's RAW is also read again from the catalog. A RAW with nothing beside it is never a
  candidate. The RAW's own sidecars (`IMG_1.xmp` when it's the photo's only RAW and no other photo in the folder has
  that name, `IMG_1.CR3.xmp`) go with it, each logged; a JPEG's own sidecar never does. The RAW is marked missing at
  once and the photo stays present, shown from its JPEG. The log is a `<library>-files-…` Trash log, so Put Back
  reads it.
* **Put Back** (`history.py`, `web/routes/history.py`, the History page) undoes a move to the Trash: only a file or
  folder one of this library's Trash logs lists as moved, only from a Trash folder, only to the exact place it
  came from inside the library's folders, and never over anything there now (the system checks that in the same
  step as the move, `renamex_np` with `RENAME_EXCL`). A file put back is shown in the library again at once, and
  each Put Back is logged in `trash-log/`. Only the owner can do this.
* **Moving and renaming** (`moves.py`, `web/routes/moves.py`, the Library's Folders view and selection bar):
  renaming a folder, making one, moving a folder into another, moving photos to a folder, and renaming photos'
  files from a pattern. Only the owner can do this, and only when asked:
  * The plan is worked out first, read-only, and shown ("Move 24 photos (51 files, 1.2 GB) to … · same drive,
    instant"). Destinations must be inside the library's folders, not on a read-only drive, and not inside
    `~/.joeylab`.
  * A photo moves whole: every present file of the shot, plus the sidecars beside them (`.xmp`, `.aae` and the
    like; a stem sidecar only when no other file of that stem stays behind), keeping their names together.
  * Nothing is overwritten: a name taken on disk, in the catalog or earlier in the same plan is skipped or kept
    with " 2"; renames use `renamex_np` with `RENAME_EXCL` (where a drive doesn't support it, as exFAT under
    FSKit doesn't, the name is checked just before a plain rename, under the update lock).
  * On one drive a move is a rename. To another drive each file is copied under a hidden name, flushed
    (`F_FULLFSYNC`), read back and compared, and only then takes its name; the original goes to the Trash
    (`cleanup.move_to_trash`), never unlinked.
  * The catalog follows in the same transaction as each rename: `files.path`, the photo's folder, and everything
    kept by folder path (`volumes.rewrite_paths`: library folders, keep rules, selections, albums, smart album
    filters, folder names). Photos are paired again as a scan would, so the next scan finds nothing changed.
  * Every operation is logged in `move-log/` (JSON lines, private to the account): everything it will do, flushed
    before anything moves, then each step. `recover()`, run before every move and every update, finishes or rolls
    back one cut short, a photo at a time: a photo's files end up together, in the new place if any got there.
    Copies JoeyLab had not finished are removed (its own; the originals were never touched).
  * Every operation can be moved back (`plan_back`): renames are reversed; copies to another drive come back by
    putting the originals back from the Trash while they're there (the copies go to the Trash).

## The non-destructive rules

Every change must keep these:

1. **Photos are only read.** Open tracked files read-only. Never write, rename, move, touch or set attributes on a
   tracked file or folder, except through the explicit paths above, each started by the user.
2. **The catalog is the only place JoeyLab's knowledge goes.** No hidden files, databases or caches next to
   photos.
3. **Nothing is deleted.** The most JoeyLab does is move what the user chose to the Trash, after a read-only
   check, a typed confirmation and a re-check right before each move. A move to another drive sends the original
   to the Trash only after its copy is checked; the only files JoeyLab ever removes are its own unfinished
   copies, whose originals are still in place.
4. **Decisions are the user's.** Suggestions never become ratings unless the user accepts them; imports fill in
   only undecided photos unless asked to overwrite; every change is logged and undoable.
5. **Missing is not deleted.** A file that disappears is marked missing, with its decisions kept. An unavailable
   drive is skipped, not emptied.
6. **Exports never land inside photo folders** or JoeyLab's own data.
7. **The untouched check stays true.** If a change could alter a tracked file's size, mtime, ctime or inode, it
   breaks the promise the check makes.

## Tests (`tests/`)

* `conftest.py`'s `env` fixture gives each test its own `JOEYLAB_HOME` in a temporary folder and sets
  `JOEYLAB_OFFLINE=1`, so no test downloads anything or looks at a real library.
* Photos are generated (`make_jpeg`: random blocks, optional blur), with EXIF where a test needs it.
* Web tests use FastAPI's `TestClient` against `create_app()`.
* Run them with `JOEYLAB_OFFLINE=1 HF_HUB_OFFLINE=1 uv run pytest -q`. Tests that need something that isn't
  there (the face models, exiftool) skip themselves.

Tests for a new feature go in the file for its area. A change to anything in the rules above needs a test that
fails if the rule is broken: for example, that a destination inside a tracked folder is refused, or that a file
changed after the Trash check is left alone.
