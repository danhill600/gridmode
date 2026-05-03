import posixpath

from gridmode_cache import _tag_to_str, album_group_key


def queue_items_from_playlist(playlist):
    items = []
    block = []
    block_key = None
    for song in playlist:
        key = album_group_key(song)
        if key is None:
            if block:
                items.append(queue_item_from_block(block, block_key))
                block = []
                block_key = None
            continue
        if block and key != block_key:
            items.append(queue_item_from_block(block, block_key))
            block = []
        block.append(song)
        block_key = key
    if block:
        items.append(queue_item_from_block(block, block_key))
    return items


def queue_item_from_block(songs, key):
    first = songs[0]
    artist = _tag_to_str(first.get("artist", "")).strip()
    album_artist = _tag_to_str(first.get("albumartist", "")).strip()
    album = _tag_to_str(first.get("album", "")).strip()
    artists = {
        _tag_to_str(song.get("artist", "")).strip().casefold()
        for song in songs
        if _tag_to_str(song.get("artist", "")).strip()
    }
    if not album_artist and len(artists) > 1:
        artist = "Various Artists"
    positions = []
    for song in songs:
        pos = song_playlist_position(song)
        positions.append(pos if pos is not None else len(positions))
    rel_dir = posixpath.dirname(_tag_to_str(first.get("file", "")).strip())
    label_artist = album_artist or artist
    return {
        "artist": label_artist,
        "album": album,
        "key": key,
        "rel_dir": rel_dir,
        "positions": positions,
        "mtime": None,
        "search_text": f"{label_artist} {album}".casefold(),
    }


def play_queue_album(client, item):
    positions = item.get("positions") or []
    if positions:
        client.play(str(min(positions)))
        return True

    for song in client.playlistinfo():
        if album_group_key(song) == item["key"]:
            client.play(str(song["pos"]))
            return True
    return False


def delete_queue_album_occurrence(client, item):
    positions = list(item.get("positions") or [])
    playlist = client.playlistinfo()
    if positions:
        by_pos = {}
        for song in playlist:
            pos = song_playlist_position(song)
            if pos is not None:
                by_pos[pos] = song
        stale = any(album_group_key(by_pos.get(pos, {})) != item["key"] for pos in positions)
        if stale:
            return {"ok": False, "stale": True, "deleted": 0}
    else:
        for song in playlist:
            if album_group_key(song) != item["key"]:
                continue
            pos = song_playlist_position(song)
            if pos is not None:
                positions.append(pos)

    if not positions:
        return {"ok": False, "stale": False, "deleted": 0}

    for pos in sorted(positions, reverse=True):
        client.delete(str(pos))
    return {"ok": True, "stale": False, "deleted": len(positions)}


def song_playlist_position(song):
    try:
        return int(song.get("pos"))
    except (TypeError, ValueError):
        return None
