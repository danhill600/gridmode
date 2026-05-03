#!/usr/bin/env python3

import argparse
import csv
import json
import os
import shutil
import sys

from gridmode_cache import (
    DEFAULT_USER_AGENT,
    RateLimiter,
    connect_mpd,
    cached_cover_path,
    copy_local_cover,
    ensure_dir,
    expand_path,
    fetch_database_album_records,
    fetch_playlist_album_records,
    get_cover_path,
    hydrate_cover,
    load_index,
    require_cfg,
    save_index,
    update_index_entry,
)
from gridmode_config import config_to_mapping, load_app_config

DEFAULT_CONFIG = "config.toml"


def parse_args():
    parser = argparse.ArgumentParser(description="Hydrate Gridmode's local album cover cache.")
    parser.add_argument("config", nargs="?", default=DEFAULT_CONFIG)
    parser.add_argument("--playlist", action="store_true", help="hydrate only the current MPD playlist")
    parser.add_argument("--limit", type=int, default=0, help="stop after this many missing covers are attempted")
    parser.add_argument("--music-root", default=None, help="music root matching MPD file paths")
    parser.add_argument("--ssh-host", default=None, help="SSH host for reading remote local art; empty disables SSH")
    parser.add_argument("--user-agent", default=DEFAULT_USER_AGENT)
    parser.add_argument("--rate", type=float, default=1.0, help="maximum Last.fm requests per second")
    parser.add_argument("--max-backoff", type=float, default=300.0)
    parser.add_argument("--no-local", action="store_true", help="skip local cover art lookup")
    parser.add_argument("--no-lastfm", action="store_true", help="skip Last.fm lookup")
    parser.add_argument("--retry-failures", action="store_true", help="retry albums already listed in failures.json")
    parser.add_argument("--dry-run", action="store_true", help="show what would be hydrated without writing covers")
    return parser.parse_args()


def load_records(cfg, playlist=False):
    mpd_host = require_cfg(cfg, "mpd", "host")
    mpd_port = require_cfg(cfg, "mpd", "port")
    mpd_password = cfg.get("mpd", {}).get("password", "")

    client = connect_mpd(mpd_host, mpd_port, mpd_password, timeout=120)
    try:
        if playlist:
            return fetch_playlist_album_records(client)
        return fetch_database_album_records(client)
    finally:
        try:
            client.close()
            client.disconnect()
        except Exception:
            pass


def main():
    args = parse_args()
    cfg = config_to_mapping(load_app_config(args.config))
    cache_dir = expand_path(require_cfg(cfg, "cache", "dir"))
    ensure_dir(cache_dir)
    cell_size = int(require_cfg(cfg, "ui", "cell_size"))
    api_key = cfg.get("lastfm", {}).get("api_key", "")
    api_secret = cfg.get("lastfm", {}).get("api_secret", "")
    if not args.no_lastfm and not api_key:
        print("Missing lastfm.api_key; set it in config or pass --no-lastfm.", file=sys.stderr)
        sys.exit(2)
    min_interval = 1.0 / args.rate if args.rate > 0 else 1.0
    use_imagemagick = shutil.which("convert") is not None
    music_cfg = cfg.get("music", {})
    music_root_arg = args.music_root if args.music_root is not None else music_cfg.get("root", "")
    ssh_host_arg = args.ssh_host if args.ssh_host is not None else music_cfg.get("ssh_host", "")

    records = load_records(cfg, playlist=args.playlist)
    index = load_index(cache_dir)
    known_failures = load_failures(cache_dir)
    limiter = RateLimiter(min_interval=min_interval, max_backoff=args.max_backoff)
    music_root = "" if args.no_local else music_root_arg
    ssh_host = "" if args.no_local or not music_root else ssh_host_arg
    work_items = []
    cached = 0
    skipped_failed = 0

    for library_idx, record in enumerate(records, start=1):
        cover_path = cached_cover_path(cache_dir, record.artist, record.album, album_key=record.key)
        if cover_path:
            cached += 1
            continue
        failure_id = failure_key(record)
        if not args.retry_failures and failure_id in known_failures:
            skipped_failed += 1
            continue
        work_items.append((library_idx, record))

    if args.limit:
        work_items = work_items[: args.limit]

    print(f"albums: {len(records)}")
    print(f"cached: {cached}")
    print(f"known failures skipped: {skipped_failed}")
    print(f"to hydrate: {len(work_items)}")
    print(f"cache: {cache_dir}")
    print(f"local art: {'off' if args.no_local else (ssh_host + ':' + music_root if ssh_host else music_root)}")
    print(f"lastfm: {'off' if args.no_lastfm else 'max %.3g/sec' % args.rate}")

    attempted = 0
    written = 0
    failed = 0
    failures = list(known_failures.values())

    for attempt_idx, (library_idx, record) in enumerate(work_items, start=1):
        attempted += 1

        label = f"{record.artist} - {record.album}"
        if args.dry_run:
            print(f"[{attempt_idx}/{len(work_items)} library {library_idx}/{len(records)}] would hydrate: {label}")
            continue

        if args.no_lastfm:
            path, source = copy_local_cover(
                record,
                cache_dir,
                music_root,
                cell_size,
                use_imagemagick=use_imagemagick,
                ssh_host=ssh_host,
            )
        else:
            path, source = hydrate_cover(
                record,
                cache_dir,
                cell_size,
                api_key,
                api_secret=api_secret,
                music_root=music_root,
                ssh_host=ssh_host,
                user_agent=args.user_agent,
                rate_limiter=limiter,
                use_imagemagick=use_imagemagick,
            )

        if path:
            written += 1
            update_index_entry(index, record, path, source)
            save_index(cache_dir, index)
            print(f"[{attempt_idx}/{len(work_items)} library {library_idx}/{len(records)}] {source}: {label}")
        else:
            failed += 1
            failure = {
                "index": library_idx,
                "artist": record.artist,
                "album": record.album,
                "reason": source,
                "rel_dir": record.rel_dir,
                "key": list(record.key),
            }
            known_failures[failure_key(record)] = failure
            failures = list(known_failures.values())
            if not args.dry_run:
                save_failures(cache_dir, failures)
            print(f"[{attempt_idx}/{len(work_items)} library {library_idx}/{len(records)}] failed ({source}): {label}")

    if not args.dry_run:
        save_index(cache_dir, index)
        save_failures(cache_dir, failures)
    print(
        f"done: cached={cached} skipped_failed={skipped_failed} "
        f"written={written} failed={failed} attempted={attempted}"
    )


def path_exists(path):
    try:
        return bool(path and os.path.exists(path))
    except OSError:
        return False


def failure_key(record):
    return "\0".join(record.key)


def load_failures(cache_dir):
    path = os.path.join(cache_dir, "failures.json")
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            items = json.load(f)
    except (OSError, ValueError):
        return {}

    failures = {}
    for item in items:
        key = item.get("key")
        if isinstance(key, list):
            failures["\0".join(str(part) for part in key)] = item
    return failures


def save_failures(cache_dir, failures):
    json_path = os.path.join(cache_dir, "failures.json")
    tmp_json_path = json_path + ".tmp"
    with open(tmp_json_path, "w", encoding="utf-8") as f:
        json.dump(failures, f, indent=2, sort_keys=True)
        f.write("\n")
    os.replace(tmp_json_path, json_path)

    tsv_path = os.path.join(cache_dir, "failures.tsv")
    tmp_tsv_path = tsv_path + ".tmp"
    with open(tmp_tsv_path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=["index", "artist", "album", "reason", "rel_dir", "key"],
            delimiter="\t",
        )
        writer.writeheader()
        for item in failures:
            row = dict(item)
            row["key"] = " | ".join(item["key"])
            writer.writerow(row)
    os.replace(tmp_tsv_path, tsv_path)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        sys.exit(130)
