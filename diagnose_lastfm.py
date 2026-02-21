#!/usr/bin/env python3

import sys
import traceback

try:
    import tomllib
except Exception:  # pragma: no cover
    tomllib = None

import pylast

DEFAULT_CONFIG = "config.toml"


def load_config(path):
    if tomllib is None:
        raise RuntimeError("tomllib not available; use Python 3.11+ or add tomli")
    with open(path, "rb") as f:
        return tomllib.load(f)


def require_cfg(cfg, *keys):
    cur = cfg
    for k in keys:
        if k not in cur:
            raise KeyError("Missing config: " + ".".join(keys))
        cur = cur[k]
    return cur


def main():
    cfg_path = DEFAULT_CONFIG
    if len(sys.argv) > 1:
        cfg_path = sys.argv[1]

    cfg = load_config(cfg_path)
    api_key = require_cfg(cfg, "lastfm", "api_key")
    api_secret = require_cfg(cfg, "lastfm", "api_secret")

    if not api_key or not api_secret:
        print("Missing lastfm credentials in config.")
        sys.exit(1)

    print("Last.fm creds: present (not printing secrets)")

    try:
        network = pylast.LastFMNetwork(api_key=api_key, api_secret=api_secret)
        album = network.get_album("Pink Floyd", "The Wall")
        url = album.get_cover_image(pylast.SIZE_EXTRA_LARGE)
        print(f"Cover URL: {url}")
        if not url:
            print("No URL returned for cover image.")
            sys.exit(2)
        print("Success: URL returned.")
    except Exception as e:
        print("Error while requesting cover:")
        print(str(e))
        traceback.print_exc()
        sys.exit(3)


if __name__ == "__main__":
    main()
