#!/usr/bin/env python3

import hashlib
import os
import queue
import shutil
import subprocess
import sys
import threading
import time

try:
    import tomllib
except Exception:  # pragma: no cover
    tomllib = None

import mpd
import pylast
import requests
import tkinter as tk
from tkinter import ttk

try:
    from PIL import Image, ImageTk  # optional
except Exception:  # pragma: no cover
    Image = None
    ImageTk = None

DEFAULT_CONFIG = "config.toml"


def load_config(path):
    if tomllib is None:
        raise RuntimeError("tomllib not available; use Python 3.11+ or add tomli")
    if not os.path.exists(path):
        raise FileNotFoundError(f"Config not found: {path}")
    with open(path, "rb") as f:
        cfg = tomllib.load(f)
    return cfg


def require_cfg(cfg, *keys):
    cur = cfg
    for k in keys:
        if k not in cur:
            raise KeyError("Missing config: " + ".".join(keys))
        cur = cur[k]
    return cur


def connect_mpd(host, port, password=""):
    client = mpd.MPDClient()
    client.timeout = 10
    client.idletimeout = None
    client.connect(host, int(port))
    if password:
        client.password(password)
    return client


def _tag_to_str(value):
    if isinstance(value, (list, tuple)):
        for v in value:
            if v:
                value = v
                break
        else:
            value = ""
    if value is None:
        return ""
    return str(value)


def fetch_playlist_albums(client):
    items = client.playlistinfo()
    seen = set()
    albums = []
    for item in items:
        artist = _tag_to_str(item.get("artist", "")).strip()
        album = _tag_to_str(item.get("album", "")).strip()
        if not artist or not album:
            continue
        key = (artist, album)
        if key in seen:
            continue
        seen.add(key)
        albums.append(key)
    return albums


def safe_hash(text):
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def ensure_dir(path):
    os.makedirs(path, exist_ok=True)


def download_file(url, dest_path):
    resp = requests.get(url, stream=True, timeout=20)
    if resp.status_code != 200:
        return False
    with open(dest_path, "wb") as f:
        for chunk in resp.iter_content(chunk_size=8192):
            if chunk:
                f.write(chunk)
    return True


def convert_to_png(src_path, dest_path, size_px):
    cmd = ["convert", src_path, "-resize", f"{size_px}x{size_px}", dest_path]
    try:
        proc = subprocess.run(cmd, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except FileNotFoundError:
        return False
    return proc.returncode == 0 and os.path.exists(dest_path)


def get_cover_path(cache_dir, artist, album):
    key = f"{artist}::{album}"
    return os.path.join(cache_dir, safe_hash(key) + ".png")


def pil_convert_to_png(src_path, dest_path, size_px):
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


def fetch_cover(network, cache_dir, artist, album, size_px, use_imagemagick=True):
    dest_path = get_cover_path(cache_dir, artist, album)
    if os.path.exists(dest_path):
        return dest_path, None

    try:
        lastfm_album = network.get_album(artist, album)
        url = lastfm_album.get_cover_image(pylast.SIZE_EXTRA_LARGE)
    except Exception:
        url = ""

    if not url:
        return None, "no_lastfm_url"

    tmp_path = dest_path + ".download"
    if not download_file(url, tmp_path):
        return None, "download_failed"

    if use_imagemagick and convert_to_png(tmp_path, dest_path, size_px):
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        return dest_path, None

    if pil_convert_to_png(tmp_path, dest_path, size_px):
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        return dest_path, None

    # Fallback: if convert failed, keep download if it is a png
    if tmp_path.lower().endswith(".png"):
        os.rename(tmp_path, dest_path)
        return dest_path, None

    return None, "convert_failed"


def make_placeholder(size_px, text="No Art"):
    img = tk.PhotoImage(width=size_px, height=size_px)
    img.put("#222222", to=(0, 0, size_px, size_px))
    return img


class GridModeApp:
    def __init__(self, root, cfg):
        self.root = root
        self.cfg = cfg
        self.cache_dir = require_cfg(cfg, "cache", "dir")
        self.columns = int(require_cfg(cfg, "ui", "columns"))
        self.cell_size = int(require_cfg(cfg, "ui", "cell_size"))
        self.padding = int(require_cfg(cfg, "ui", "padding"))
        self.font = require_cfg(cfg, "ui", "font")

        self.albums = []
        self.cells = []
        self.image_labels = []
        self.images = []
        self.selected_index = 0
        self.loader_thread = None
        self.loader_stop = threading.Event()
        self.queue = queue.Queue()
        self.covers_ok = 0
        self.covers_failed = 0
        self.convert_available = shutil.which("convert") is not None

        ensure_dir(self.cache_dir)

        self.status = ttk.Label(self.root, text="")
        self.status.pack(side="bottom", fill="x")

        self.canvas = tk.Canvas(self.root, highlightthickness=0, bg="#000000")
        self.scrollbar = ttk.Scrollbar(self.root, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self.scrollbar.set)
        self.scrollbar.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)

        self.grid_frame = ttk.Frame(self.canvas)
        self.canvas_window = self.canvas.create_window((0, 0), window=self.grid_frame, anchor="nw")

        self.grid_frame.bind("<Configure>", self.on_frame_configure)
        self.canvas.bind("<Configure>", self.on_canvas_configure)
        self.canvas.bind_all("<MouseWheel>", self.on_mousewheel)
        self.canvas.bind_all("<Button-4>", self.on_mousewheel)
        self.canvas.bind_all("<Button-5>", self.on_mousewheel)

        self.root.bind("<Left>", lambda e: self.move_selection(-1, 0))
        self.root.bind("<Right>", lambda e: self.move_selection(1, 0))
        self.root.bind("<Up>", lambda e: self.move_selection(0, -1))
        self.root.bind("<Down>", lambda e: self.move_selection(0, 1))
        self.root.bind("h", lambda e: self.move_selection(-1, 0))
        self.root.bind("l", lambda e: self.move_selection(1, 0))
        self.root.bind("k", lambda e: self.move_selection(0, -1))
        self.root.bind("j", lambda e: self.move_selection(0, 1))
        self.root.bind("J", lambda e: self.canvas.yview_scroll(1, "page"))
        self.root.bind("K", lambda e: self.canvas.yview_scroll(-1, "page"))
        self.root.bind("<Return>", lambda e: self.on_select())
        self.root.bind("r", lambda e: self.refresh())
        self.root.bind("q", lambda e: self.root.destroy())

        self.refresh()

    def refresh(self):
        self.stop_loader()
        self.albums = self.load_albums()
        self.render_grid()
        self.start_loader()

    def load_albums(self):
        mpd_host = require_cfg(self.cfg, "mpd", "host")
        mpd_port = require_cfg(self.cfg, "mpd", "port")
        mpd_password = self.cfg.get("mpd", {}).get("password", "")

        client = connect_mpd(mpd_host, mpd_port, mpd_password)
        try:
            albums = fetch_playlist_albums(client)
        finally:
            try:
                client.close()
                client.disconnect()
            except Exception:
                pass
        return albums

    def render_grid(self):
        for child in self.grid_frame.winfo_children():
            child.destroy()
        self.cells = []
        self.image_labels = []
        self.images = []

        placeholder = make_placeholder(self.cell_size)

        for idx, (artist, album) in enumerate(self.albums):
            row = idx // self.columns
            col = idx % self.columns

            cell = tk.Frame(
                self.grid_frame,
                bg="#111111",
                bd=1,
                relief="solid",
                highlightthickness=2,
                highlightbackground="#111111",
                width=self.cell_size + (self.padding * 2),
                height=self.cell_size + (self.padding * 2),
            )
            cell.grid(row=row, column=col, padx=self.padding, pady=self.padding, sticky="nsew")
            cell.grid_propagate(False)

            img_label = tk.Label(cell, image=placeholder, bg="#111111")
            img_label.image = placeholder
            img_label.pack(side="top")

            text = f"{artist}\n{album}"
            txt_label = tk.Label(cell, text=text, bg="#111111", fg="#eeeeee", font=self.font)
            txt_label.pack(side="top", fill="x")

            self.cells.append(cell)
            self.image_labels.append(img_label)
            self.images.append(placeholder)

        for c in range(self.columns):
            self.grid_frame.grid_columnconfigure(
                c,
                weight=1,
                uniform="cols",
                minsize=self.cell_size + (self.padding * 2),
            )

        self.selected_index = 0
        self.update_selection()
        self.covers_ok = 0
        self.covers_failed = 0
        self.update_status()
        self.root.after(0, self.ensure_visible, self.selected_index)

    def update_status(self, note=None):
        msg = f"{len(self.albums)} albums | covers ok {self.covers_ok}, failed {self.covers_failed}"
        if not self.convert_available and Image is None:
            msg += " | no ImageMagick or Pillow"
        elif not self.convert_available:
            msg += " | no ImageMagick"
        if note:
            msg += f" | {note}"
        msg += " | r=refresh, q=quit"
        self.status.configure(text=msg)

    def update_selection(self):
        for i, cell in enumerate(self.cells):
            if i == self.selected_index:
                cell.configure(
                    highlightbackground="#7fff00",
                    highlightthickness=4,
                    bg="#1a1a1a",
                )
            else:
                cell.configure(
                    highlightbackground="#111111",
                    highlightthickness=2,
                    bg="#111111",
                )
        self.ensure_visible(self.selected_index)

    def move_selection(self, dx, dy):
        if not self.cells:
            return
        cols = self.columns
        idx = self.selected_index
        x = idx % cols
        y = idx // cols
        x = max(0, min(cols - 1, x + dx))
        y = max(0, y + dy)
        new_idx = y * cols + x
        if new_idx >= len(self.cells):
            new_idx = len(self.cells) - 1
        self.selected_index = new_idx
        self.update_selection()

    def on_select(self):
        if not self.albums:
            return
        artist, album = self.albums[self.selected_index]
        print(f"Selected: {artist} - {album}")

    def start_loader(self):
        self.loader_stop.clear()
        self.loader_thread = threading.Thread(target=self.loader_loop, daemon=True)
        self.loader_thread.start()

    def stop_loader(self):
        if self.loader_thread and self.loader_thread.is_alive():
            self.loader_stop.set()
            self.loader_thread.join(timeout=1)

    def loader_loop(self):
        api_key = require_cfg(self.cfg, "lastfm", "api_key")
        api_secret = require_cfg(self.cfg, "lastfm", "api_secret")
        if not api_key or not api_secret:
            self.root.after(0, self.update_status, "missing lastfm creds")
            return

        network = pylast.LastFMNetwork(api_key=api_key, api_secret=api_secret)

        for idx, (artist, album) in enumerate(self.albums):
            if self.loader_stop.is_set():
                return
            path, err = fetch_cover(
                network,
                self.cache_dir,
                artist,
                album,
                self.cell_size,
                use_imagemagick=self.convert_available,
            )
            if path:
                self.covers_ok += 1
                self.root.after(0, self.set_image, idx, path)
            else:
                self.covers_failed += 1
                if err:
                    print(f"cover fail: {artist} - {album} ({err})")
                    self.root.after(0, self.update_status, err)
            self.root.after(0, self.update_status)

    def set_image(self, idx, path):
        if idx >= len(self.image_labels):
            return
        try:
            img = tk.PhotoImage(file=path)
        except Exception:
            return
        self.images[idx] = img
        self.image_labels[idx].configure(image=img)
        self.image_labels[idx].image = img

    def on_frame_configure(self, event):
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def on_canvas_configure(self, event):
        self.canvas.itemconfigure(self.canvas_window, width=event.width)

    def on_mousewheel(self, event):
        if event.num == 4:
            self.canvas.yview_scroll(-3, "units")
        elif event.num == 5:
            self.canvas.yview_scroll(3, "units")
        else:
            self.canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

    def ensure_visible(self, idx):
        if not self.cells or idx >= len(self.cells):
            return
        self.root.update_idletasks()
        cell = self.cells[idx]
        try:
            y = cell.winfo_y()
            h = cell.winfo_height()
        except Exception:
            return
        bbox = self.canvas.bbox("all")
        if not bbox:
            return
        total_h = bbox[3] - bbox[1]
        if total_h <= 0:
            return
        top = self.canvas.canvasy(0)
        bottom = top + self.canvas.winfo_height()
        if y < top:
            self.canvas.yview_moveto(y / total_h)
        elif y + h > bottom:
            self.canvas.yview_moveto((y + h - self.canvas.winfo_height()) / total_h)


def main():
    cfg_path = DEFAULT_CONFIG
    if len(sys.argv) > 1:
        cfg_path = sys.argv[1]

    try:
        cfg = load_config(cfg_path)
    except Exception as e:
        print(str(e))
        sys.exit(1)

    root = tk.Tk()
    root.title("Gridmode")
    app = GridModeApp(root, cfg)
    root.mainloop()


if __name__ == "__main__":
    main()
