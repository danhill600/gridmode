#!/usr/bin/env python3

import argparse
import json
import os
import sys

from gridmode_config import config_to_mapping, load_app_config
from phone_service import list_phone_album_dirs, list_phone_album_tracks, lossy_album_match_key


DEFAULT_CONFIG = "config.toml"
DEFAULT_PLAYLIST_NAME = "lifeboat-recent.m3u"


def library_index_path(cache_dir):
    return os.path.join(cache_dir, "library_index.json")


def load_library_index(cache_dir):
    path = library_index_path(cache_dir)
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    albums = data.get("albums", [])
    if not isinstance(albums, list):
        return []
    return [normalize_library_item(item) for item in albums if isinstance(item, dict)]


def normalize_library_item(item):
    artist = str(item.get("artist") or "")
    album = str(item.get("album") or "")
    rel_dir = str(item.get("rel_dir") or "")
    mtime = item.get("mtime")
    try:
        mtime = float(mtime) if mtime is not None else None
    except (TypeError, ValueError):
        mtime = None
    return {
        "artist": artist,
        "album": album,
        "rel_dir": rel_dir,
        "mtime": mtime,
        "display": f"{artist} - {album}".strip(" -"),
    }


def build_library_lookup(library_items):
    exact = {}
    fuzzy = {}
    for item in library_items:
        names = [
            item.get("rel_dir", ""),
            os.path.basename(item.get("rel_dir", "")),
            item.get("display", ""),
        ]
        for name in names:
            if not name:
                continue
            exact.setdefault(os.path.basename(name).casefold(), item)
            key = lossy_album_match_key(name)
            if key:
                current = fuzzy.get(key)
                if current is None or item_mtime(item) > item_mtime(current):
                    fuzzy[key] = item
    return exact, fuzzy


def item_mtime(item):
    value = item.get("mtime")
    return value if isinstance(value, (int, float)) else 0


def match_phone_album(phone_album, exact_lookup, fuzzy_lookup):
    name = phone_album.get("name", "")
    match = exact_lookup.get(name.casefold())
    if match is not None:
        return match
    key = lossy_album_match_key(name)
    if key:
        return fuzzy_lookup.get(key)
    return None


def order_phone_albums(phone_albums, library_items):
    exact_lookup, fuzzy_lookup = build_library_lookup(library_items)
    ordered = []
    unmatched = []
    for phone_album in phone_albums:
        match = match_phone_album(phone_album, exact_lookup, fuzzy_lookup)
        if match is None:
            unmatched.append(phone_album.get("name", ""))
        ordered.append(
            {
                "phone": phone_album,
                "library": match,
                "sort_mtime": item_mtime(match or {}),
            }
        )
    ordered.sort(
        key=lambda item: (
            item["sort_mtime"],
            item["phone"].get("mtime") or 0,
            item["phone"].get("name", "").casefold(),
        ),
        reverse=True,
    )
    return ordered, unmatched


def m3u_text(ordered_albums, tracks_by_album):
    lines = ["#EXTM3U"]
    for item in ordered_albums:
        album_name = item["phone"].get("name", "")
        for track in tracks_by_album.get(album_name, []):
            lines.append(track)
    return "\n".join(lines) + "\n"


def write_phone_playlist(transfer_ssh_host, phone_ssh_host, phone_root, playlist_name, text, timeout=60):
    import shlex
    import subprocess

    if not playlist_name or "/" in playlist_name or playlist_name in (".", ".."):
        raise ValueError("playlist name must be one file name")
    script = r"""
root=$1
name=$2
[ -d "$root" ] || exit 2
cat > "$root/$name"
"""
    phone_cmd = "sh -c {} gridmode-write-phone-playlist {} {}".format(
        shlex.quote(script),
        shlex.quote(phone_root),
        shlex.quote(playlist_name),
    )
    transfer_cmd = "ssh -o BatchMode=yes {} {}".format(
        shlex.quote(phone_ssh_host),
        shlex.quote(phone_cmd),
    )
    proc = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", transfer_ssh_host, transfer_cmd],
        input=text,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or f"ssh exited {proc.returncode}")


def generate_playlist_from_config(cfg, playlist_name=DEFAULT_PLAYLIST_NAME, local_output="", copy_to_phone=True):
    music_cfg = cfg.get("music", {})
    phone_cfg = cfg.get("phone", {})
    cache_dir = cfg.get("cache", {}).get("dir", "")
    library_items = load_library_index(cache_dir)
    phone_albums = list_phone_album_dirs(
        music_cfg.get("ssh_host", ""),
        phone_cfg.get("ssh_host", ""),
        phone_cfg.get("music_root", ""),
    )
    ordered, unmatched = order_phone_albums(phone_albums, library_items)
    tracks_by_album = list_phone_album_tracks(
        music_cfg.get("ssh_host", ""),
        phone_cfg.get("ssh_host", ""),
        phone_cfg.get("music_root", ""),
        [item["phone"].get("name", "") for item in ordered],
    )
    text = m3u_text(ordered, tracks_by_album)
    if local_output:
        with open(local_output, "w", encoding="utf-8") as f:
            f.write(text)
    if copy_to_phone:
        write_phone_playlist(
            music_cfg.get("ssh_host", ""),
            phone_cfg.get("ssh_host", ""),
            phone_cfg.get("music_root", ""),
            playlist_name,
            text,
        )
    return {
        "playlist": playlist_name,
        "albums": len(phone_albums),
        "tracks": sum(len(tracks) for tracks in tracks_by_album.values()),
        "unmatched": unmatched,
        "local_output": local_output,
        "copied_to_phone": copy_to_phone,
    }


def generate_playlist(config_path=DEFAULT_CONFIG, playlist_name=DEFAULT_PLAYLIST_NAME, local_output="", copy_to_phone=True):
    return generate_playlist_from_config(
        config_to_mapping(load_app_config(config_path)),
        playlist_name=playlist_name,
        local_output=local_output,
        copy_to_phone=copy_to_phone,
    )


def parse_args(argv):
    parser = argparse.ArgumentParser(description="Generate a lifeboat M3U sorted by oldbeast library mtime.")
    parser.add_argument("--config", default=DEFAULT_CONFIG)
    parser.add_argument("--name", default=DEFAULT_PLAYLIST_NAME)
    parser.add_argument("--local-output", default="")
    parser.add_argument("--no-copy", action="store_true")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv or sys.argv[1:])
    result = generate_playlist(
        config_path=args.config,
        playlist_name=args.name,
        local_output=args.local_output,
        copy_to_phone=not args.no_copy,
    )
    print(
        "wrote {tracks} tracks from {albums} albums to {playlist}".format(**result)
    )
    if result["local_output"]:
        print("local copy: " + result["local_output"])
    if result["unmatched"]:
        print(f"unmatched albums: {len(result['unmatched'])}", file=sys.stderr)
        for name in result["unmatched"][:20]:
            print("  " + name, file=sys.stderr)
        if len(result["unmatched"]) > 20:
            print(f"  ...and {len(result['unmatched']) - 20} more", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
