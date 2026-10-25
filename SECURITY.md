# Security

JoeyLab holds some of the most personal data people have: their photos, where they were taken, and the faces of
their family and friends. This page explains who JoeyLab lets do what, and how to report a problem.

## Reporting a vulnerability

Please report security problems **privately**, through GitHub's private vulnerability reporting: on the
repository's [Security](https://github.com/Yadub/joeylab/security) tab, choose **Report a vulnerability**
([direct link](https://github.com/Yadub/joeylab/security/advisories/new)). Only the maintainer sees the report.

Please don't open a public issue, discussion or pull request for a security problem.

Include:

* what the problem is and what someone could do with it;
* the steps to reproduce it, ideally on the demo library (`joeylab demo`) rather than your own photos;
* `joeylab --version`, your macOS version, and how JoeyLab was started (`serve`, `service install`, with which
  options).

You'll get an acknowledgement as soon as possible, normally within a week. Once a fix is out, the problem is
described in the changelog, with credit to you if you'd like it.

## Supported versions

Only the latest release gets security fixes. JoeyLab is at 0.9 (beta).

## The security model

### Who can open it

JoeyLab has one user: you. Only you can open it:

* at the Mac itself, or
* on your own phone and other devices signed in to Tailscale with the Mac's account, or
* (with `--host` and `--token`) through the link carrying the access token.

Anyone else who reaches it sees a notice that it's private, and nothing else. To show someone photos, send them a
share link for an album or an event: it shows that album, read-only, and nothing else of JoeyLab. People you
share with can mark favourites on it, which never change your picks.

### Who can reach it

* `joeylab serve` listens only on this Mac (`127.0.0.1`).
* `--tailscale` (off by default) publishes it to your own tailnet with `tailscale serve`. Only devices on your
  tailnet can connect, and JoeyLab refuses Tailscale Funnel traffic, so it is never on the open internet.
  Tailscale tells JoeyLab who each visitor is.
* A `--host` other than this Mac's own addresses requires `--token`. The printed link carries the token and sets
  a cookie.
* `joeylab share-server` runs a separate server that answers only share links (`/s/…`), never the library or
  its API. It's the one to publish beyond your tailnet if you want to share an album more widely.

### Other protections

* **Same-origin changes.** Every request that changes something must come from JoeyLab's own pages: requests
  carrying another site's `Origin` or `Sec-Fetch-Site`, or a non-JSON body, are refused. A web page you visit
  can't change your library.
* **Host names.** The server answers only to this Mac's own names (`localhost`, its tailnet name), which blocks
  DNS rebinding.
* **Files.** Exports refuse destinations inside the folders JoeyLab reads and inside its own data folder. Moving
  to the Trash checks every file first, needs the count typed back, and checks each file again just before the
  move.
* **Assistants.** The MCP server runs only for the person who started it, over stdio. It can't touch files, its
  changes are logged under its name and can be undone, and `joeylab mcp --read-only` lets it look without
  changing anything.
* **Local data.** `~/.joeylab` is private to your user account.
* **No outside calls.** JoeyLab sends no telemetry and calls no AI service. Its only network use is listed in
  the README's [Privacy](README.md#privacy) section.

### Known limitations

* **Other accounts on the same Mac** count as "at the Mac", so they're treated as the owner if they open
  JoeyLab while it's running. If others log in to your Mac, keep that in mind.
* **Anyone on your tailnet** who can reach JoeyLab can browse your library. Turn on `--tailscale` only on a
  tailnet whose members you'd show your photos to, and use Tailscale's access controls to narrow it further.
* **Share links** are bearer links: anyone who has the link can open that album until it expires or is revoked.
* **Face data** is biometric. It stays in the catalog on your Mac; Settings › People & privacy removes all of it.

## In scope

Problems in JoeyLab's own code, such as: someone getting more rights than the table above gives them; reading
photos or catalog data without access; a way to make JoeyLab change, move or delete a photo without the owner
asking; a page on another site being able to change the library; secrets or personal data in the repository.

Problems in dependencies (FastAPI, Pillow, LibRaw and so on) are best reported to those projects, but please tell
us too if JoeyLab's use of them makes things worse.
