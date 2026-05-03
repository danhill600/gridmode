# Gridmode

Gridmode is a keyboard-first MPD client built around albums as the main unit of
navigation. It shows the current playlist and library as cover grids, can insert
albums after the currently playing album, and has Now Playing/Info views for
tracks and artist notes.

## Setup

Install Python dependencies:

```sh
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
```

Create a local config:

```sh
cp config.sample.toml config.toml
```

Edit `config.toml` for your MPD server, cache directory, and optional music
directory lookup. `config.toml` is ignored by git and is expected to contain
local hostnames, paths, and API credentials.

Run the app:

```sh
.venv/bin/python gridmode.py
```

## Configuration

Required sections:

- `mpd`: host, port, and optional password for the MPD server.
- `cache`: local directory for covers, logs, library index, and artist bio cache.
- `ui`: grid and font settings.

Optional sections:

- `lastfm`: API credentials used for cover and artist bio lookups.
- `music`: local or SSH-accessible music root used for local cover discovery and
  library directory mtimes. Leave these empty if Gridmode should rely only on MPD
  metadata and remote web cover lookup.

If `music.ssh_host` is set, it should be an SSH host alias or hostname that can
read `music.root`.

## Cover Hydration

Hydrate missing album covers:

```sh
.venv/bin/python hydrate_covers.py
```

Useful options:

```sh
.venv/bin/python hydrate_covers.py --playlist
.venv/bin/python hydrate_covers.py --limit 100
.venv/bin/python hydrate_covers.py --no-local
.venv/bin/python hydrate_covers.py --no-musicbrainz
.venv/bin/python hydrate_covers.py --no-lastfm
.venv/bin/python hydrate_covers.py --only-failures --no-lastfm
```

Hydration tries local cover art first, then MusicBrainz/Cover Art Archive, then
Last.fm. MusicBrainz and Last.fm requests are rate-limited. Local cover lookup
is disabled when `music.root` is empty or when `--no-local` is passed. Progress
is written to `hydrate.log` in the configured cache directory by default; use
`--log-file /tmp/hydrate.log` to override it or `--no-log` to disable it.

## Current Keys

- `1`, `2`, `3`: Queue, Library, Now Playing tabs.
- `h`, `j`, `k`, `l`: move around cover grids.
- `J`, `K`: page movement in grids; track/bio focus in detail views.
- `Enter`: play/select current item.
- `o`: jump to the currently playing album or track.
- `i`: open/close an album Info tab.
- `a`: add selected library album after the currently playing album.
- `A`: append selected library album to the end of the playlist.
- `d`: remove selected Queue album occurrence from the current MPD playlist.
- `r`: refresh/rebuild the active view.
- `?`: open/close Help.
- `f`: toggle fullscreen.
- `q`: close transient tab or quit.

## Development Notes

The app is intentionally opinionated about album-oriented listening, but local
machine names, paths, and credentials should stay in `config.toml`, not tracked
source files.
