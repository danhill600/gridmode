import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import time
from difflib import SequenceMatcher
from urllib.parse import quote
from dataclasses import dataclass, field

import mpd
import requests

try:
    from PIL import Image
except Exception:  # pragma: no cover
    Image = None

try:
    import tomllib
except Exception:  # pragma: no cover
    tomllib = None


DEFAULT_USER_AGENT = "gridmode/0.1 album-cover-cache"
LOCAL_ART_NAMES = (
    "cover",
    "folder",
    "front",
    "album",
    "artwork",
)
IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".webp", ".gif")


@dataclass
class AlbumRecord:
    artist: str
    album: str
    key: tuple
    rel_dir: str = ""
    release_mbid: str = ""
    release_group_mbid: str = ""
    artists: set = field(default_factory=set)
    has_album_artist: bool = False


class RateLimiter:
    def __init__(self, min_interval=1.0, max_backoff=300.0):
        self.min_interval = float(min_interval)
        self.max_backoff = float(max_backoff)
        self.next_request_at = 0.0
        self.backoff = 0.0

    def wait(self):
        now = time.monotonic()
        delay = max(self.next_request_at - now, 0.0)
        if delay:
            time.sleep(delay)

    def success(self):
        self.backoff = 0.0
        self.next_request_at = time.monotonic() + self.min_interval

    def failure(self, hard=False):
        if self.backoff <= 0:
            self.backoff = 8.0 if hard else 2.0
        else:
            self.backoff = min(self.backoff * 2.0, self.max_backoff)
        self.next_request_at = time.monotonic() + max(self.min_interval, self.backoff)


def load_config(path):
    if tomllib is None:
        raise RuntimeError("tomllib not available; use Python 3.11+ or add tomli")
    if not os.path.exists(path):
        raise FileNotFoundError(f"Config not found: {path}")
    with open(path, "rb") as f:
        return tomllib.load(f)


def require_cfg(cfg, *keys):
    cur = cfg
    for key in keys:
        if key not in cur:
            raise KeyError("Missing config: " + ".".join(keys))
        cur = cur[key]
    return cur


def connect_mpd(host, port, password="", timeout=10):
    client = mpd.MPDClient()
    client.timeout = timeout
    client.idletimeout = None
    client.connect(host, int(port))
    if password:
        client.password(password)
    return client


def _tag_to_str(value):
    if isinstance(value, (list, tuple)):
        for item in value:
            if item:
                value = item
                break
        else:
            value = ""
    if value is None:
        return ""
    return str(value)


def _album_dir(item):
    file_path = _tag_to_str(item.get("file", "")).strip()
    if not file_path:
        return ""
    song_dir = os.path.dirname(file_path)
    if song_dir.endswith("cue"):
        song_dir = song_dir.rsplit("/", 1)[0]
    return song_dir


def _normalized_album_dir(item):
    song_dir = _album_dir(item)
    if not song_dir:
        return ""

    leaf = os.path.basename(song_dir).casefold()
    if re.fullmatch(r"(cd|disc|disk|vol|volume)\s*[-_. ]*\d+.*", leaf):
        return os.path.dirname(song_dir)
    return song_dir


def album_group_key(item):
    album = _tag_to_str(item.get("album", "")).strip()
    if not album:
        return None

    mb_album_id = _tag_to_str(item.get("musicbrainz_albumid", "")).strip()
    if mb_album_id:
        return (album.casefold(), f"mbid:{mb_album_id.casefold()}")

    album_dir = _normalized_album_dir(item)
    if album_dir:
        group = album_dir.casefold()
    else:
        album_artist = _tag_to_str(item.get("albumartist", "")).strip()
        group = album_artist.casefold() or _tag_to_str(item.get("artist", "")).strip().casefold()

    return (album.casefold(), group)


def album_records_from_items(items):
    records = []
    by_key = {}
    for item in items:
        artist = _tag_to_str(item.get("artist", "")).strip()
        album = _tag_to_str(item.get("album", "")).strip()
        if not artist or not album:
            continue

        key = album_group_key(item)
        if key is None:
            continue

        if key not in by_key:
            album_artist = _tag_to_str(item.get("albumartist", "")).strip()
            release_mbid = _tag_to_str(item.get("musicbrainz_albumid", "")).strip()
            release_group_mbid = _tag_to_str(item.get("musicbrainz_releasegroupid", "")).strip()
            record = AlbumRecord(
                artist=album_artist or artist,
                album=album,
                key=key,
                rel_dir=_album_dir(item),
                release_mbid=release_mbid,
                release_group_mbid=release_group_mbid,
                artists={artist.casefold()},
                has_album_artist=bool(album_artist),
            )
            by_key[key] = record
            records.append(record)
            continue

        record = by_key[key]
        record.artists.add(artist.casefold())
        if not record.has_album_artist and len(record.artists) > 1:
            record.artist = "Various Artists"
    return records


def fetch_playlist_album_records(client):
    return album_records_from_items(client.playlistinfo())


def fetch_database_album_records(client):
    files = [entry.get("file", "") for entry in client.list("file")]
    dirs = {}
    root_files = []
    for file_path in files:
        if not file_path:
            continue
        rel_dir = os.path.dirname(file_path)
        if rel_dir:
            dirs.setdefault(rel_dir, file_path)
        else:
            root_files.append(file_path)

    items = []
    for rel_dir in sorted(dirs):
        try:
            items.extend(client.find("base", rel_dir))
        except Exception:
            try:
                items.extend(client.find("file", dirs[rel_dir]))
            except Exception:
                pass
    for file_path in root_files:
        try:
            items.extend(client.find("file", file_path))
        except Exception:
            pass
    return album_records_from_items(items)


def records_to_albums_and_keys(records):
    return [(record.artist, record.album) for record in records], [record.key for record in records]


def safe_hash(text):
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def cache_id_from_album_key(album_key):
    if album_key is None:
        return ""
    return safe_hash("\0".join(album_key))


def safe_filename(text, max_len=120):
    text = re.sub(r'[\\/:*?"<>|]+', "_", text)
    text = re.sub(r"\s+", " ", text).strip(" .")
    if not text:
        text = "album"
    return text[:max_len].rstrip(" .")


def ensure_dir(path):
    os.makedirs(path, exist_ok=True)


def expand_path(path):
    return os.path.abspath(os.path.expanduser(path))


def get_cover_path(cache_dir, artist, album, album_key=None):
    if album_key is not None:
        cache_id = cache_id_from_album_key(album_key)
        label = safe_filename(f"{artist} - {album}")
        return os.path.join(cache_dir, f"{label}--{cache_id[:12]}.png")

    key = f"{artist}::{album}"
    return os.path.join(cache_dir, safe_hash(key) + ".png")


def cached_cover_path(cache_dir, artist, album, album_key=None):
    path = get_cover_path(cache_dir, artist, album, album_key=album_key)
    if os.path.exists(path):
        return path

    legacy_path = get_cover_path(cache_dir, artist, album)
    if legacy_path != path and os.path.exists(legacy_path):
        return legacy_path

    label = safe_filename(f"{artist} - {album}")
    try:
        for name in sorted(os.listdir(cache_dir)):
            if name.startswith(label + "--") and name.endswith(".png"):
                return os.path.join(cache_dir, name)
    except OSError:
        pass
    return None


def convert_to_png(src_path, dest_path, size_px, use_imagemagick=True):
    if use_imagemagick:
        cmd = ["convert", src_path, "-resize", f"{size_px}x{size_px}", dest_path]
        try:
            proc = subprocess.run(cmd, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except FileNotFoundError:
            proc = None
        if proc is not None and proc.returncode == 0 and os.path.exists(dest_path):
            return True

    if Image is None:
        return False
    try:
        with Image.open(src_path) as im:
            im = im.convert("RGB")
            im.thumbnail((size_px, size_px))
            im.save(dest_path, format="PNG")
        return True
    except Exception:
        return False


def download_file(url, dest_path, headers=None):
    try:
        resp = requests.get(url, stream=True, timeout=20, headers=headers)
    except requests.RequestException:
        remove_quietly(dest_path)
        return False
    if resp.status_code != 200:
        return False
    try:
        with open(dest_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)
    except requests.RequestException:
        remove_quietly(dest_path)
        return False
    return True


def candidate_local_images(album_dir):
    if not album_dir or not os.path.isdir(album_dir):
        return []
    images = []
    for name in os.listdir(album_dir):
        path = os.path.join(album_dir, name)
        if not os.path.isfile(path) or not name.lower().endswith(IMAGE_EXTENSIONS):
            continue
        stem = os.path.splitext(name)[0].casefold()
        priority = 0 if stem in LOCAL_ART_NAMES else 1
        try:
            size = os.path.getsize(path)
        except OSError:
            size = 0
        images.append((priority, -size, path))
    images.sort()
    return [path for _priority, _size, path in images]


def ssh_list_local_images(ssh_host, remote_dir):
    if not ssh_host or not remote_dir:
        return []
    script = "\n".join(
        [
            "import os, sys",
            "d = sys.argv[1]",
            "ext = ('.png', '.jpg', '.jpeg', '.webp', '.gif')",
            "preferred = {'cover', 'folder', 'front', 'album', 'artwork'}",
            "items = []",
            "for name in os.listdir(d):",
            "    path = os.path.join(d, name)",
            "    if not os.path.isfile(path) or not name.lower().endswith(ext):",
            "        continue",
            "    stem = os.path.splitext(name)[0].casefold()",
            "    priority = 0 if stem in preferred else 1",
            "    items.append((priority, -os.path.getsize(path), path))",
            "for _priority, _size, path in sorted(items):",
            "    print(path)",
        ]
    )
    command = f"python3 -c {shlex.quote(script)} {shlex.quote(remote_dir)}"
    proc = subprocess.run(
        ["ssh", ssh_host, command],
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    if proc.returncode != 0:
        return []
    return [line for line in proc.stdout.splitlines() if line]


def ssh_copy_file(ssh_host, remote_path, local_path):
    with open(local_path, "wb") as out:
        proc = subprocess.run(
            ["ssh", ssh_host, f"cat {shlex.quote(remote_path)}"],
            check=False,
            stdout=out,
            stderr=subprocess.DEVNULL,
        )
    return proc.returncode == 0 and os.path.exists(local_path)


def copy_local_cover(record, cache_dir, music_root, size_px, use_imagemagick=True, ssh_host=""):
    cached_path = cached_cover_path(cache_dir, record.artist, record.album, album_key=record.key)
    if cached_path:
        return cached_path, "cache"

    dest_path = get_cover_path(cache_dir, record.artist, record.album, album_key=record.key)

    if not record.rel_dir:
        return None, "no_album_dir"

    tmp_path = dest_path + ".local"
    if ssh_host:
        remote_dir = os.path.join(music_root, record.rel_dir)
        for remote_path in ssh_list_local_images(ssh_host, remote_dir):
            if ssh_copy_file(ssh_host, remote_path, tmp_path) and convert_to_png(tmp_path, dest_path, size_px, use_imagemagick):
                remove_quietly(tmp_path)
                return dest_path, "local"
            remove_quietly(tmp_path)
        return None, "no_local_art"

    album_dir = os.path.join(music_root, record.rel_dir)
    for path in candidate_local_images(album_dir):
        if convert_to_png(path, dest_path, size_px, use_imagemagick):
            return dest_path, "local"
    return None, "no_local_art"


def remove_quietly(path):
    try:
        os.remove(path)
    except OSError:
        pass


def fetch_lastfm_cover(
    record,
    cache_dir,
    api_key,
    size_px,
    api_secret="",
    user_agent=DEFAULT_USER_AGENT,
    rate_limiter=None,
    use_imagemagick=True,
):
    cached_path = cached_cover_path(cache_dir, record.artist, record.album, album_key=record.key)
    if cached_path:
        return cached_path, "cache"

    dest_path = get_cover_path(cache_dir, record.artist, record.album, album_key=record.key)

    legacy_path = get_cover_path(cache_dir, record.artist, record.album)
    if legacy_path != dest_path and os.path.exists(legacy_path):
        shutil.copyfile(legacy_path, dest_path)
        return dest_path, "legacy_cache"

    if rate_limiter is not None:
        rate_limiter.wait()

    params = {
        "method": "album.getInfo",
        "artist": record.artist,
        "album": record.album,
        "api_key": api_key,
        "format": "json",
    }
    if api_secret:
        params["api_secret"] = api_secret
    headers = {"User-Agent": user_agent}
    try:
        resp = requests.get("https://ws.audioscrobbler.com/2.0/", params=params, headers=headers, timeout=20)
    except requests.RequestException:
        if rate_limiter is not None:
            rate_limiter.failure()
        return None, "lastfm_request_failed"

    if resp.status_code == 429:
        if rate_limiter is not None:
            rate_limiter.failure(hard=True)
        return None, "lastfm_rate_limited"
    if resp.status_code != 200:
        if rate_limiter is not None:
            rate_limiter.failure()
        return None, f"lastfm_http_{resp.status_code}"

    if rate_limiter is not None:
        rate_limiter.success()

    try:
        data = resp.json()
    except ValueError:
        return None, "lastfm_bad_json"

    image_url = best_lastfm_image_url(data)
    if not image_url:
        return None, "no_lastfm_url"

    tmp_path = dest_path + ".download"
    if not download_file(image_url, tmp_path, headers=headers):
        return None, "download_failed"
    if convert_to_png(tmp_path, dest_path, size_px, use_imagemagick):
        remove_quietly(tmp_path)
        return dest_path, "lastfm"
    remove_quietly(tmp_path)
    return None, "convert_failed"


def best_lastfm_image_url(data):
    album = data.get("album") if isinstance(data, dict) else None
    images = album.get("image", []) if isinstance(album, dict) else []
    for item in reversed(images):
        url = item.get("#text", "") if isinstance(item, dict) else ""
        if url:
            return url
    return ""


def release_mbid_from_key(album_key):
    if not album_key:
        return ""
    for part in album_key:
        if isinstance(part, str) and part.startswith("mbid:"):
            return part.split(":", 1)[1]
    return ""


def fetch_cover_art_archive_cover(
    record,
    cache_dir,
    size_px,
    user_agent=DEFAULT_USER_AGENT,
    rate_limiter=None,
    use_imagemagick=True,
):
    cached_path = cached_cover_path(cache_dir, record.artist, record.album, album_key=record.key)
    if cached_path:
        return cached_path, "cache"

    dest_path = get_cover_path(cache_dir, record.artist, record.album, album_key=record.key)
    headers = {"User-Agent": user_agent}

    release_mbids = unique_values([record.release_mbid, release_mbid_from_key(record.key)])
    release_group_mbids = unique_values([record.release_group_mbid])
    for url in cover_art_urls(release_mbids, release_group_mbids):
        path = download_and_convert_cover(url, dest_path, size_px, headers, use_imagemagick)
        if path:
            return path, "coverartarchive"

    releases, reason = search_musicbrainz_releases(record, user_agent=user_agent, rate_limiter=rate_limiter)
    if not releases:
        return None, reason

    release_mbids = []
    release_group_mbids = []
    for release in releases:
        release_mbid = release.get("id", "")
        if release_mbid:
            release_mbids.append(release_mbid)
        release_group = release.get("release-group")
        if isinstance(release_group, dict) and release_group.get("id"):
            release_group_mbids.append(release_group["id"])

    for url in cover_art_urls(unique_values(release_mbids), unique_values(release_group_mbids)):
        path = download_and_convert_cover(url, dest_path, size_px, headers, use_imagemagick)
        if path:
            return path, "musicbrainz"
    return None, "no_coverartarchive_url"


def unique_values(values):
    seen = set()
    out = []
    for value in values:
        value = _tag_to_str(value).strip()
        if value and value not in seen:
            seen.add(value)
            out.append(value)
    return out


def cover_art_urls(release_mbids, release_group_mbids):
    for mbid in release_mbids:
        yield f"https://coverartarchive.org/release/{quote(mbid)}/front-500"
    for mbid in release_group_mbids:
        yield f"https://coverartarchive.org/release-group/{quote(mbid)}/front-500"


def download_and_convert_cover(url, dest_path, size_px, headers, use_imagemagick):
    tmp_path = dest_path + ".download"
    if not download_file(url, tmp_path, headers=headers):
        remove_quietly(tmp_path)
        return None
    if convert_to_png(tmp_path, dest_path, size_px, use_imagemagick):
        remove_quietly(tmp_path)
        return dest_path
    remove_quietly(tmp_path)
    return None


def search_musicbrainz_releases(record, user_agent=DEFAULT_USER_AGENT, rate_limiter=None):
    queries = [
        f'artist:"{record.artist}" AND release:"{record.album}"',
        f'release:"{record.album}"',
    ]
    for query in queries:
        releases, reason = query_musicbrainz_releases(query, record, user_agent=user_agent, rate_limiter=rate_limiter)
        if releases:
            return releases, reason
        if reason not in ("no_musicbrainz_release",):
            return [], reason
    return [], "no_musicbrainz_release"


def query_musicbrainz_releases(query, record, user_agent=DEFAULT_USER_AGENT, rate_limiter=None):
    if rate_limiter is not None:
        rate_limiter.wait()

    headers = {"User-Agent": user_agent}
    params = {"query": query, "fmt": "json", "limit": 8}
    try:
        resp = requests.get("https://musicbrainz.org/ws/2/release/", params=params, headers=headers, timeout=20)
    except requests.RequestException:
        if rate_limiter is not None:
            rate_limiter.failure()
        return [], "musicbrainz_request_failed"

    if resp.status_code in (429, 503):
        if rate_limiter is not None:
            rate_limiter.failure(hard=True)
        return [], "musicbrainz_rate_limited"
    if resp.status_code != 200:
        if rate_limiter is not None:
            rate_limiter.failure()
        return [], f"musicbrainz_http_{resp.status_code}"

    if rate_limiter is not None:
        rate_limiter.success()

    try:
        data = resp.json()
    except ValueError:
        return [], "musicbrainz_bad_json"

    releases = data.get("releases", []) if isinstance(data, dict) else []
    if not isinstance(releases, list):
        return [], "no_musicbrainz_release"

    filtered = []
    for release in releases:
        if not isinstance(release, dict):
            continue
        try:
            score = int(release.get("score", 0))
        except (TypeError, ValueError):
            score = 0
        title = _tag_to_str(release.get("title", ""))
        if score >= 70 and title_similarity(record.album, title) >= 0.7:
            filtered.append(release)
    return filtered, "no_musicbrainz_release"


def title_similarity(left, right):
    left_norm = normalize_title(left)
    right_norm = normalize_title(right)
    if not left_norm or not right_norm:
        return 0.0
    if left_norm == right_norm:
        return 1.0
    if left_norm in right_norm or right_norm in left_norm:
        return 0.85
    return SequenceMatcher(None, left_norm, right_norm).ratio()


def normalize_title(text):
    text = _tag_to_str(text).casefold()
    text = re.sub(r"\[[^\]]*\]|\{[^}]*\}|\([^)]*(?:reissue|remaster|bonus|anniversary)[^)]*\)", " ", text)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def hydrate_cover(
    record,
    cache_dir,
    size_px,
    api_key,
    api_secret="",
    music_root="",
    ssh_host="",
    user_agent=DEFAULT_USER_AGENT,
    rate_limiter=None,
    use_imagemagick=True,
    use_musicbrainz=True,
    use_lastfm=True,
):
    ensure_dir(cache_dir)
    cached_path = cached_cover_path(cache_dir, record.artist, record.album, album_key=record.key)
    if cached_path:
        return cached_path, "cache"

    if music_root:
        path, source = copy_local_cover(record, cache_dir, music_root, size_px, use_imagemagick, ssh_host)
        if path:
            return path, source

    musicbrainz_source = "musicbrainz_off"
    if use_musicbrainz:
        path, source = fetch_cover_art_archive_cover(
            record,
            cache_dir,
            size_px,
            user_agent=user_agent,
            rate_limiter=rate_limiter,
            use_imagemagick=use_imagemagick,
        )
        if path:
            return path, source
        musicbrainz_source = source

    if not use_lastfm:
        return None, musicbrainz_source

    return fetch_lastfm_cover(
        record,
        cache_dir,
        api_key,
        size_px,
        api_secret=api_secret,
        user_agent=user_agent,
        rate_limiter=rate_limiter,
        use_imagemagick=use_imagemagick,
    )


def load_index(cache_dir):
    path = os.path.join(cache_dir, "index.json")
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_index(cache_dir, index):
    ensure_dir(cache_dir)
    path = os.path.join(cache_dir, "index.json")
    tmp_path = path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(index, f, indent=2, sort_keys=True)
        f.write("\n")
    os.replace(tmp_path, path)


def update_index_entry(index, record, path, source):
    cache_id = cache_id_from_album_key(record.key)[:12]
    index[cache_id] = {
        "artist": record.artist,
        "album": record.album,
        "key": list(record.key),
        "rel_dir": record.rel_dir,
        "release_mbid": record.release_mbid,
        "release_group_mbid": record.release_group_mbid,
        "source": source,
        "path": os.path.basename(path) if path else "",
        "updated_at": int(time.time()),
    }
