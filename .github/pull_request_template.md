### What this changes

<!-- And the issue it closes, if there is one: "Closes #123". -->

### Why

### How it was checked

- [ ] `JOEYLAB_OFFLINE=1 HF_HUB_OFFLINE=1 uv run pytest -q` passes
- [ ] New or changed behaviour has tests
- [ ] Tried in the app (say which pages, and at phone width if it's in the web app), or not applicable

### The non-destructive rules

- [ ] Photo folders are still only read: no writes, renames, moves or attribute changes outside XMP sidecars and
      Move to Trash
- [ ] Nothing is deleted, and nothing new uses the network beyond what the README's Privacy section lists
- [ ] Anything that adds folders or writes files is for the owner only

### Docs

- [ ] The README is updated if what people see or type changed
- [ ] `CHANGELOG.md` has a line for this
- [ ] A new dependency is listed, with its licence, in `THIRD_PARTY_NOTICES.md`
