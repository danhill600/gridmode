#!/usr/bin/env python3

import sys
import traceback

import pylast

from gridmode_config import load_app_config

DEFAULT_CONFIG = "config.toml"


def main():
    cfg_path = DEFAULT_CONFIG
    if len(sys.argv) > 1:
        cfg_path = sys.argv[1]

    cfg = load_app_config(cfg_path)
    api_key = cfg.lastfm.api_key
    api_secret = cfg.lastfm.api_secret

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
