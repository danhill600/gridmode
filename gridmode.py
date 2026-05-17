#!/usr/bin/env python3

import json
import logging
import os
import posixpath
import queue
import random
import re
import shlex
import shutil
import subprocess
import sys
import tkinter as tk
import tkinter.font as tkfont
import time
import threading
from collections import OrderedDict
from tkinter import simpledialog, ttk

import requests

from gridmode_config import config_to_mapping, load_app_config
from mpd_service import (
    delete_queue_album_occurrence,
    append_current_song_to_playlist,
    play_queue_album,
    queue_items_from_playlist,
    save_current_queue_as_playlist,
    song_playlist_position,
)
from phone_service import (
    copy_local_cover_to_remote_album,
    copy_remote_album_to_phone,
    list_phone_album_dirs,
    PhoneTransferCancelled,
    prepare_album_for_phone,
)
from gridmode_cache import (
    DEFAULT_USER_AGENT,
    Image,
    album_group_key,
    cached_cover_path,
    connect_mpd,
    expand_path,
    fetch_database_album_records,
    fetch_playlist_album_records,
    ensure_dir,
    get_cover_path,
    require_cfg,
    safe_filename,
    _tag_to_str,
)

DEFAULT_CONFIG = "config.toml"
APP_BG = "#073642"
APP_BG_SELECTED = "#0b4a45"
APP_FG = "#eee8d5"
APP_ACCENT = "#859900"
PANE_FOCUS = "#b8d46a"
PANEL_BG = "#050505"


def setup_logging(cache_dir):
    ensure_dir(cache_dir)
    log_path = f"{cache_dir}/gridmode.log"
    logging.basicConfig(
        filename=log_path,
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    return log_path


def log_exception(exc_type, exc_value, exc_traceback):
    logging.critical(
        "Uncaught exception",
        exc_info=(exc_type, exc_value, exc_traceback),
    )


def make_placeholder(size_px, text="No Art"):
    img = tk.PhotoImage(width=size_px, height=size_px)
    img.put("#222222", to=(0, 0, size_px, size_px))
    return img


def elide_text(text, font, max_width):
    if font.measure(text) <= max_width:
        return text

    ellipsis = "..."
    lo = 0
    hi = len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        candidate = text[:mid].rstrip() + ellipsis
        if font.measure(candidate) <= max_width:
            lo = mid
        else:
            hi = mid - 1
    return text[:lo].rstrip() + ellipsis


def album_item(record, mtime=None):
    return {
        "artist": record.artist,
        "album": record.album,
        "key": record.key,
        "rel_dir": record.rel_dir,
        "mtime": mtime,
        "search_text": f"{record.artist} {record.album}".casefold(),
    }


def phone_folder_match_key(name):
    name = os.path.basename(str(name or ""))
    name = re.sub(r"\[[^\]]*\]", " ", name)
    name = re.sub(r"\{[^}]*\}", " ", name)
    name = re.sub(r"\((?:19|20)\d{2}[^)]*\)", " ", name)
    name = re.sub(r"\b(?:19|20)\d{2}\b", " ", name)
    name = re.sub(r"\b(?:web|cd|vinyl|flac|mp3|lossy|v0|vbr|320|24bit|16bit)\b", " ", name, flags=re.IGNORECASE)
    name = re.sub(r"[^0-9a-zA-Z]+", " ", name)
    return re.sub(r"\s+", " ", name).strip().casefold()


def release_year_from_song(song):
    for tag in ("originaldate", "date", "releasedate", "year"):
        value = _tag_to_str(song.get(tag, "")).strip()
        match = re.search(r"\b(1[89]\d{2}|20\d{2})\b", value)
        if match:
            return match.group(1)
    return ""


def release_year_from_songs(songs):
    for song in songs:
        year = release_year_from_song(song)
        if year:
            return year
    return ""


def library_index_path(cache_dir):
    return os.path.join(cache_dir, "library_index.json")


def artist_bios_path(cache_dir):
    return os.path.join(cache_dir, "artist_bios.json")


def serialize_library_item(item):
    return {
        "artist": item["artist"],
        "album": item["album"],
        "key": list(item["key"]) if item["key"] is not None else None,
        "rel_dir": item.get("rel_dir", ""),
        "mtime": item.get("mtime"),
        "search_text": item.get("search_text", ""),
    }


def deserialize_library_item(item):
    artist = str(item.get("artist", ""))
    album = str(item.get("album", ""))
    key = item.get("key")
    if isinstance(key, list):
        key = tuple(str(part) for part in key)
    else:
        key = None
    return {
        "artist": artist,
        "album": album,
        "key": key,
        "rel_dir": str(item.get("rel_dir", "")),
        "mtime": item.get("mtime"),
        "search_text": str(item.get("search_text") or f"{artist} {album}").casefold(),
    }


class GridModeApp:
    def __init__(self, root, cfg):
        self.root = root
        self.cfg = cfg
        self.configure_styles()
        self.cache_dir = expand_path(require_cfg(cfg, "cache", "dir"))
        self.columns = int(require_cfg(cfg, "ui", "columns"))
        self.cell_size = int(require_cfg(cfg, "ui", "cell_size"))
        self.padding = int(require_cfg(cfg, "ui", "padding"))
        self.font = require_cfg(cfg, "ui", "font")
        self.title_gap = int(cfg.get("ui", {}).get("title_gap", 4))
        self.title_font = tkfont.Font(font=self.font)
        self.title_line_height = self.title_font.metrics("linespace")
        self.top_gap = int(cfg.get("ui", {}).get("top_gap", 4))
        self.row_gap = int(cfg.get("ui", {}).get("row_gap", 8))

        self.albums = []
        self.album_keys = []
        self.cover_paths = []
        self.image_cache = OrderedDict()
        self.placeholder_image = None
        self.max_image_cache = int(cfg.get("ui", {}).get("max_image_cache", 600))
        self.nowplaying_cover_size = int(cfg.get("ui", {}).get("nowplaying_cover_size", 420))
        self.nowplaying_title_font = tkfont.Font(font=cfg.get("ui", {}).get("nowplaying_title_font", "Courier 30 bold"))
        self.nowplaying_text_font = tkfont.Font(font=cfg.get("ui", {}).get("nowplaying_text_font", "Courier 17"))
        self.nowplaying_current_font = tkfont.Font(font=cfg.get("ui", {}).get("nowplaying_current_font", "Courier 17 bold"))
        self.nowplaying_bio_font = tkfont.Font(font=cfg.get("ui", {}).get("nowplaying_bio_font", "Courier 17"))
        self.nowplaying_profile = None
        self.nowplaying_profiles = self.build_nowplaying_profiles()
        self.help_font = tkfont.Font(font=cfg.get("ui", {}).get("help_font", "Courier 11"))
        self.loading_font = tkfont.Font(font=cfg.get("ui", {}).get("loading_font", "Courier 18 bold"))
        self.selected_index = 0
        self.last_selected_index = None
        self.covers_ok = 0
        self.covers_failed = 0
        self.convert_available = shutil.which("convert") is not None
        self.fullscreen = False
        self.row_height = self.cell_size + self.top_gap + self.title_gap + self.title_line_height + self.row_gap
        self.key_repeat_id = None
        self.key_repeat_key = None
        self.key_repeat_action = None
        self.key_repeat_delay = 260
        self.key_repeat_interval = 55
        self.key_release_id = None
        self.mpd_event_queue = queue.Queue()
        self.mpd_idle_stop = threading.Event()
        self.mpd_idle_thread = None
        self.load_result_queue = queue.Queue()
        self.phone_transfer_pending = 0
        self.phone_transfer_cancel_event = None
        self.phone_transfer_dialog = None
        self.phone_transfer_phase_var = None
        self.phone_transfer_detail_var = None
        self.phone_transfer_elapsed_var = None
        self.phone_transfer_cancel_button = None
        self.phone_transfer_close_button = None
        self.phone_transfer_phone_button = None
        self.phone_transfer_started_at = None
        self.phone_transfer_done = False
        self.phone_transfer_elapsed_after_id = None
        self.loading_views = set()
        self.loading_after_id = None
        self.loading_frame = 0
        self.loading_text = ""
        self.help_tab_visible = False
        self.help_return_view = "queue"
        self.nowplaying_rerender_id = None
        self.loading_text_item = None
        self.loading_bg_item = None

        ensure_dir(self.cache_dir)

        self.active_view = "queue"
        self.phone_enabled = bool(cfg.get("phone", {}).get("enabled"))
        self.grid_views = ["queue", "library"] + (["phone"] if self.phone_enabled else [])
        self.view_items = {view: [] for view in self.grid_views}
        self.view_loaded = {view: False for view in self.grid_views}
        self.view_selected_indices = {view: 0 for view in self.grid_views}
        self.view_grid_cache = {}
        self.pending_leader = None
        self.current_playlist_name = None
        self.library_search_query = ""
        self.library_search_matches = []
        self.info_tab_visible = False
        self.info_return_view = "queue"
        self.info_album = None
        self.nowplaying_info = None
        self.info_info = None
        self.track_selected_indices = {"nowplaying": 0, "info": 0}
        self.track_selection_manual = {"nowplaying": False, "info": False}
        self.text_pane_focus = {"nowplaying": "tracks", "info": "tracks"}
        self.library_error = ""
        self.artist_bios = self.load_artist_bios()
        self.nowplaying_cover = None

        self.tabs = ttk.Notebook(self.root)
        self.queue_tab = ttk.Frame(self.tabs)
        self.library_tab = ttk.Frame(self.tabs)
        self.phone_tab = ttk.Frame(self.tabs)
        self.nowplaying_tab = ttk.Frame(self.tabs)
        self.info_tab = ttk.Frame(self.tabs)
        self.help_tab = ttk.Frame(self.tabs)
        self.tabs.add(self.queue_tab, text="Queue")
        self.tabs.add(self.library_tab, text="Library")
        if self.phone_enabled:
            self.tabs.add(self.phone_tab, text="Phone")
        self.tabs.add(self.nowplaying_tab, text="Now Playing")
        self.tabs.pack(side="top", fill="x")

        self.status = ttk.Label(self.root, text="")
        self.status.pack(side="bottom", fill="x")
        self.search_var = tk.StringVar()
        self.search_entry = ttk.Entry(self.root, textvariable=self.search_var, style="Search.TEntry")
        self.search_entry.bind("<Return>", self.finish_library_search)
        self.search_entry.bind("<Escape>", self.cancel_library_search)
        self.search_entry.bind("<Control-u>", self.clear_library_search_entry)
        self.search_entry.bind("<KeyPress>", lambda event: None)
        self.search_entry.bindtags(
            tuple(
                tag
                for tag in self.search_entry.bindtags()
                if tag not in ("all", str(self.root))
            )
        )

        self.content = ttk.Frame(self.root)
        self.content.pack(side="top", fill="both", expand=True)

        self.canvas = tk.Canvas(self.content, highlightthickness=0, bg="#000000")
        self.canvas.pack(side="left", fill="both", expand=True)

        self.help_frame = ttk.Frame(self.content, padding=16)
        self.help_text = tk.Text(
            self.help_frame,
            wrap="word",
            bg=PANEL_BG,
            fg=APP_FG,
            insertbackground=APP_FG,
            relief="flat",
            padx=18,
            pady=18,
            font=self.help_font,
        )
        self.help_text.pack(fill="both", expand=True)
        self.help_text.insert("1.0", self.key_help_text())
        self.help_text.configure(state="disabled")

        self.nowplaying_frame = ttk.Frame(self.content, padding=12)
        title_box_height = self.nowplaying_title_box_height()
        self.nowplaying_left = tk.Frame(
            self.nowplaying_frame,
            width=self.nowplaying_cover_size,
            height=self.nowplaying_cover_size + title_box_height + 10,
            bg=APP_BG,
        )
        self.nowplaying_left.pack(side="left", fill="y")
        self.nowplaying_left.pack_propagate(False)
        self.nowplaying_cover_label = ttk.Label(self.nowplaying_left)
        self.nowplaying_cover_label.pack(side="top", anchor="center")
        title_char_width = max(self.nowplaying_title_font.measure("0"), 1)
        self.nowplaying_title_box = tk.Frame(
            self.nowplaying_left,
            width=self.nowplaying_cover_size,
            height=title_box_height,
            bg=APP_BG,
        )
        self.nowplaying_title_box.pack(side="top", fill="x", pady=(10, 0))
        self.nowplaying_title_box.pack_propagate(False)
        self.nowplaying_title = ttk.Label(
            self.nowplaying_title_box,
            text="",
            justify="center",
            wraplength=self.nowplaying_cover_size,
            font=self.nowplaying_title_font,
        )
        self.nowplaying_title.configure(width=max(1, self.nowplaying_cover_size // title_char_width), anchor="center")
        self.nowplaying_title.place(x=self.nowplaying_cover_size // 2, rely=0.5, anchor="center")

        self.nowplaying_right = ttk.Frame(self.nowplaying_frame)
        self.nowplaying_right.pack(side="left", fill="both", expand=True, padx=(16, 0))
        self.nowplaying_right.columnconfigure(0, weight=1)
        self.nowplaying_right.rowconfigure(0, weight=1, uniform="nowplaying_text")
        self.nowplaying_right.rowconfigure(1, weight=1, uniform="nowplaying_text")
        self.track_box = tk.Frame(self.nowplaying_right, bg=PANE_FOCUS, bd=0, highlightthickness=0, padx=2, pady=2)
        self.track_box.grid(row=0, column=0, sticky="nsew")
        self.track_box.grid_propagate(False)
        self.bio_box = tk.Frame(self.nowplaying_right, bg=PANEL_BG, bd=0, highlightthickness=0, padx=2, pady=2)
        self.bio_box.grid(row=1, column=0, sticky="nsew", pady=(12, 0))
        self.bio_box.grid_propagate(False)
        self.track_text = tk.Text(
            self.track_box,
            height=12,
            wrap="none",
            bg=PANEL_BG,
            fg="#eeeeee",
            font=self.nowplaying_text_font,
            insertbackground="#eeeeee",
            relief="flat",
            highlightthickness=0,
        )
        self.track_text.tag_configure("selected", background="#12323a", foreground="#fdf6e3")
        self.track_text.tag_configure("current", font=self.nowplaying_current_font, foreground=APP_ACCENT)
        self.track_text.pack(side="top", fill="both", expand=True)
        self.track_text.bind("<Tab>", lambda event: self.toggle_text_pane_focus())
        self.track_text.bind("<ISO_Left_Tab>", lambda event: self.toggle_text_pane_focus())
        self.bio_text = tk.Text(
            self.bio_box,
            height=10,
            wrap="word",
            bg=PANEL_BG,
            fg="#dddddd",
            font=self.nowplaying_bio_font,
            insertbackground="#eeeeee",
            relief="flat",
            highlightthickness=0,
        )
        self.bio_text.pack(side="top", fill="both", expand=True)
        self.bio_text.bind("<Tab>", lambda event: self.toggle_text_pane_focus())
        self.bio_text.bind("<ISO_Left_Tab>", lambda event: self.toggle_text_pane_focus())

        self.canvas.bind("<Configure>", self.on_canvas_configure)
        self.canvas.bind("<Button-1>", self.on_canvas_click)
        self.canvas.bind_all("<MouseWheel>", self.on_mousewheel)
        self.canvas.bind_all("<Button-4>", self.on_mousewheel)
        self.canvas.bind_all("<Button-5>", self.on_mousewheel)

        arrow_intercepts = (self.tabs, self.canvas, self.track_text, self.bio_text)
        self.bind_repeating_key("Left", lambda: self.move_selection(-1, 0), intercept_widgets=arrow_intercepts)
        self.bind_repeating_key("Right", lambda: self.move_selection(1, 0), intercept_widgets=arrow_intercepts)
        self.bind_repeating_key("Up", lambda: self.move_vertical(-1), intercept_widgets=arrow_intercepts)
        self.bind_repeating_key("Down", lambda: self.move_vertical(1), intercept_widgets=arrow_intercepts)
        self.bind_repeating_key("h", lambda: self.move_selection(-1, 0))
        self.bind_repeating_key("l", lambda: self.move_selection(1, 0))
        self.bind_repeating_key("k", lambda: self.move_vertical(-1))
        self.bind_repeating_key("j", lambda: self.move_vertical(1))
        self.bind_repeating_key("J", lambda: self.move_pane_or_page(1))
        self.bind_repeating_key("K", lambda: self.move_pane_or_page(-1))
        self.bind_repeating_key("Next", lambda: self.move_pane_or_page(1), intercept_widgets=arrow_intercepts)
        self.bind_repeating_key("Prior", lambda: self.move_pane_or_page(-1), intercept_widgets=arrow_intercepts)
        self.root.bind_all("H", lambda e: self.switch_relative_tab(-1))
        self.root.bind_all("L", lambda e: self.switch_relative_tab(1))
        self.root.bind_all("g", lambda e: self.handle_g_key())
        self.root.bind_all("G", lambda e: self.jump_grid_position("bottom"))
        self.root.bind_all("M", lambda e: self.jump_grid_position("middle"))
        self.root.bind_all("z", lambda e: self.jump_grid_position("random"))
        self.root.bind_all("s", lambda e: self.save_current_playlist())
        self.root.bind_all("S", lambda e: self.save_current_playlist_as())
        self.root.bind_all("t", lambda e: self.append_current_song_to_sick_tunes())
        self.root.bind_all("/", lambda e: self.begin_library_search())
        self.root.bind_all("n", lambda e: self.next_library_search_match(1))
        self.root.bind_all("N", lambda e: self.next_library_search_match(-1))
        self.root.bind_all("<Return>", lambda e: self.on_select())
        self.root.bind_all("<Tab>", lambda e: self.toggle_text_pane_focus())
        self.root.bind_all("<ISO_Left_Tab>", lambda e: self.toggle_text_pane_focus())
        self.root.bind("1", lambda e: self.switch_tab(0))
        self.root.bind("2", lambda e: self.switch_tab(1))
        self.root.bind("3", lambda e: self.switch_tab(2))
        self.root.bind("4", lambda e: self.switch_tab(3))
        self.root.bind_all("a", lambda e: self.add_selected_library_album_after_current())
        self.root.bind_all("A", lambda e: self.append_selected_library_album())
        self.root.bind_all("d", lambda e: self.remove_selected_queue_album())
        self.root.bind_all("p", lambda e: self.send_selected_album_to_phone())
        self.root.bind_all("i", lambda e: self.toggle_info_tab())
        self.root.bind_all("o", lambda e: self.jump_to_current_album())
        self.root.bind_all("?", lambda e: self.show_key_help())
        self.root.bind("f", lambda e: self.toggle_fullscreen())
        self.root.bind("<Escape>", lambda e: self.set_fullscreen(False))
        self.root.bind("r", lambda e: self.refresh_from_key())
        self.root.bind_all("q", lambda e: self.close_info_or_quit())
        self.tabs.bind("<<NotebookTabChanged>>", self.on_tab_changed)
        self.root.bind("<Configure>", self.on_root_configure)

        self.refresh()
        self.set_fullscreen(True)
        self.root.after(250, self.set_fullscreen, True)
        self.root.after(1000, self.set_fullscreen, True)
        self.start_mpd_idle_thread()
        self.root.after(250, self.poll_mpd_events)

    def configure_styles(self):
        self.root.configure(bg=APP_BG)
        self.style = ttk.Style(self.root)
        try:
            self.style.theme_use("clam")
        except tk.TclError:
            pass
        self.style.configure(".", background=APP_BG, foreground=APP_FG)
        self.style.configure("TFrame", background=APP_BG)
        self.style.configure("TLabel", background=APP_BG, foreground=APP_FG)
        self.style.configure("TNotebook", background=APP_BG, borderwidth=0)
        self.style.configure("TNotebook.Tab", background=APP_BG, foreground=APP_FG, padding=(8, 3))
        self.style.configure("Pane.TFrame", background=APP_BG, borderwidth=2, relief="flat")
        self.style.configure("FocusedPane.TFrame", background=APP_ACCENT, borderwidth=2, relief="solid")
        self.style.configure("Search.TEntry", foreground="#3a3a3a")
        self.style.map(
            "TNotebook.Tab",
            background=[("selected", APP_BG_SELECTED)],
            foreground=[("selected", APP_FG)],
        )

    def build_nowplaying_profiles(self):
        base = {
            "cover": self.nowplaying_cover_size,
            "title_size": self.nowplaying_title_font.actual("size"),
            "text_size": self.nowplaying_text_font.actual("size"),
            "bio_size": self.nowplaying_bio_font.actual("size"),
        }
        return {
            "small": self.scaled_nowplaying_profile(base, 0.76),
            "medium": self.scaled_nowplaying_profile(base, 0.88),
            "large": base,
        }

    def scaled_nowplaying_profile(self, base, scale):
        return {
            "cover": max(280, round(base["cover"] * scale)),
            "title_size": max(18, round(base["title_size"] * scale)),
            "text_size": max(12, round(base["text_size"] * scale)),
            "bio_size": max(12, round(base["bio_size"] * scale)),
        }

    def on_root_configure(self, event=None):
        if event is not None and event.widget is not self.root:
            return
        self.apply_nowplaying_profile_for_size(self.root.winfo_width(), self.root.winfo_height())

    def nowplaying_profile_for_size(self, width, height):
        if width < 1200 or height < 720:
            return "small"
        if width < 1700 or height < 950:
            return "medium"
        return "large"

    def apply_nowplaying_profile_for_size(self, width, height):
        if width <= 1 or height <= 1:
            return
        profile_name = self.nowplaying_profile_for_size(width, height)
        if profile_name == self.nowplaying_profile:
            return
        self.nowplaying_profile = profile_name
        profile = self.nowplaying_profiles[profile_name]
        self.apply_nowplaying_profile(profile)

    def apply_nowplaying_profile(self, profile):
        self.nowplaying_cover_size = profile["cover"]
        self.nowplaying_title_font.configure(size=profile["title_size"])
        self.nowplaying_text_font.configure(size=profile["text_size"])
        self.nowplaying_current_font.configure(size=profile["text_size"])
        self.nowplaying_bio_font.configure(size=profile["bio_size"])

        title_box_height = self.nowplaying_title_box_height()
        self.nowplaying_left.configure(
            width=self.nowplaying_cover_size,
            height=self.nowplaying_cover_size + title_box_height + 10,
        )
        self.nowplaying_title_box.configure(width=self.nowplaying_cover_size, height=title_box_height)
        title_char_width = max(self.nowplaying_title_font.measure("0"), 1)
        self.nowplaying_title.configure(
            wraplength=self.nowplaying_cover_size,
            width=max(1, self.nowplaying_cover_size // title_char_width),
        )
        self.nowplaying_title.place_configure(x=self.nowplaying_cover_size // 2)
        self.track_text.configure(font=self.nowplaying_text_font)
        self.track_text.tag_configure("current", font=self.nowplaying_current_font)
        self.bio_text.configure(font=self.nowplaying_bio_font)
        info = self.current_track_info()
        if self.active_view in ("nowplaying", "info") and info:
            self.render_nowplaying(info)

    def nowplaying_title_box_height(self):
        return self.nowplaying_title_font.metrics("linespace") * 4

    def bind_repeating_key(self, key, action, intercept_widgets=()):
        self.root.bind_all(
            f"<KeyPress-{key}>",
            lambda event, key=key, action=action: self.start_key_repeat(key, action),
        )
        self.root.bind_all(
            f"<KeyRelease-{key}>",
            lambda event, key=key: self.schedule_key_repeat_stop(key),
        )
        for widget in intercept_widgets:
            widget.bind(
                f"<KeyPress-{key}>",
                lambda event, key=key, action=action: self.start_key_repeat(key, action),
            )
            widget.bind(
                f"<KeyRelease-{key}>",
                lambda event, key=key: self.schedule_key_repeat_stop(key),
            )

    def start_key_repeat(self, key, action):
        if self.pending_leader:
            self.pending_leader = None
            self.update_status("command cancelled")
            return "break"
        if self.active_view == "help":
            return "break"
        if self.key_release_id is not None:
            self.root.after_cancel(self.key_release_id)
            self.key_release_id = None
        if self.key_repeat_key == key:
            return "break"

        self.stop_key_repeat()
        self.key_repeat_key = key
        self.key_repeat_action = action
        action()
        self.key_repeat_id = self.root.after(self.key_repeat_delay, self.repeat_key_action)
        return "break"

    def repeat_key_action(self):
        if self.key_repeat_key is None or self.key_repeat_action is None:
            return
        self.key_repeat_action()
        self.key_repeat_id = self.root.after(self.key_repeat_interval, self.repeat_key_action)

    def schedule_key_repeat_stop(self, key):
        if self.key_repeat_key != key:
            return "break"
        if self.key_release_id is not None:
            self.root.after_cancel(self.key_release_id)
        self.key_release_id = self.root.after(80, self.stop_key_repeat)
        return "break"

    def stop_key_repeat(self):
        if self.key_repeat_id is not None:
            self.root.after_cancel(self.key_repeat_id)
        if self.key_release_id is not None:
            self.root.after_cancel(self.key_release_id)
        self.key_repeat_id = None
        self.key_release_id = None
        self.key_repeat_key = None
        self.key_repeat_action = None

    def start_mpd_idle_thread(self):
        self.mpd_idle_thread = threading.Thread(target=self.mpd_idle_loop, daemon=True)
        self.mpd_idle_thread.start()

    def mpd_idle_loop(self):
        mpd_host = require_cfg(self.cfg, "mpd", "host")
        mpd_port = require_cfg(self.cfg, "mpd", "port")
        mpd_password = self.cfg.get("mpd", {}).get("password", "")

        while not self.mpd_idle_stop.is_set():
            client = None
            try:
                client = connect_mpd(mpd_host, mpd_port, mpd_password, timeout=None)
                while not self.mpd_idle_stop.is_set():
                    events = client.idle("player", "playlist")
                    if events:
                        self.mpd_event_queue.put(tuple(events))
            except Exception:
                if not self.mpd_idle_stop.is_set():
                    logging.exception("MPD idle listener failed")
                    time.sleep(5)
            finally:
                if client is not None:
                    try:
                        client.close()
                        client.disconnect()
                    except Exception:
                        pass

    def poll_mpd_events(self):
        events = set()
        while True:
            try:
                events.update(self.mpd_event_queue.get_nowait())
            except queue.Empty:
                break

        if events:
            self.handle_mpd_events(events)

        if not self.mpd_idle_stop.is_set():
            self.root.after(250, self.poll_mpd_events)

    def handle_mpd_events(self, events):
        if "playlist" in events:
            self.view_loaded["queue"] = False
            self.view_grid_cache.pop("queue", None)
        if self.active_view == "nowplaying" and ({"player", "playlist"} & events):
            self.refresh_nowplaying()

    def stop_mpd_idle_thread(self):
        self.mpd_idle_stop.set()

    def refresh(self, rebuild=False):
        if self.active_view == "nowplaying":
            self.refresh_nowplaying()
            return
        if self.active_view == "info":
            self.refresh_info_tab()
            return
        if self.active_view == "library":
            self.start_library_load(rebuild=rebuild)
            return
        if self.active_view == "phone":
            self.view_items["phone"] = self.load_phone_items()
            self.view_loaded["phone"] = True
        else:
            self.view_items["queue"] = self.load_queue_items()
            self.view_loaded["queue"] = True
        self.prepare_active_grid(use_cache=False)
        self.reset_images()
        self.render_grid()
        logging.info(
            "Loaded %d %s albums; covers cached=%d missing=%d",
            len(self.albums),
            self.active_view,
            self.covers_ok,
            self.covers_failed,
        )

    def refresh_from_key(self):
        if self.search_entry.winfo_ismapped():
            return "break"
        self.refresh(rebuild=True)
        return "break"

    def prepare_active_grid(self, use_cache=True):
        if use_cache and self.apply_grid_cache(self.active_view):
            return
        self.albums = [(item["artist"], item["album"]) for item in self.active_items()]
        self.album_keys = [item["key"] for item in self.active_items()]
        self.cover_paths, self.covers_ok, self.covers_failed = self.cover_paths_for(self.albums, self.album_keys)
        self.view_grid_cache[self.active_view] = {
            "albums": self.albums,
            "album_keys": self.album_keys,
            "cover_paths": self.cover_paths,
            "covers_ok": self.covers_ok,
            "covers_failed": self.covers_failed,
        }

    def apply_grid_cache(self, view):
        cached = self.view_grid_cache.get(view)
        if not cached:
            return False
        self.albums = cached["albums"]
        self.album_keys = cached["album_keys"]
        self.cover_paths = cached["cover_paths"]
        self.covers_ok = cached["covers_ok"]
        self.covers_failed = cached["covers_failed"]
        return True

    def active_items(self):
        return self.view_items[self.active_view]

    def load_records(self, library=False):
        mpd_host = require_cfg(self.cfg, "mpd", "host")
        mpd_port = require_cfg(self.cfg, "mpd", "port")
        mpd_password = self.cfg.get("mpd", {}).get("password", "")

        client = connect_mpd(mpd_host, mpd_port, mpd_password, timeout=120 if library else 10)
        try:
            if library:
                return fetch_database_album_records(client)
            return fetch_playlist_album_records(client)
        finally:
            try:
                client.close()
                client.disconnect()
            except Exception:
                pass

    def load_queue_items(self):
        mpd_host = require_cfg(self.cfg, "mpd", "host")
        mpd_port = require_cfg(self.cfg, "mpd", "port")
        mpd_password = self.cfg.get("mpd", {}).get("password", "")

        client = connect_mpd(mpd_host, mpd_port, mpd_password, timeout=10)
        try:
            return queue_items_from_playlist(client.playlistinfo())
        finally:
            try:
                client.close()
                client.disconnect()
            except Exception:
                pass

    def load_phone_items(self):
        phone_cfg = self.cfg.get("phone", {})
        music_cfg = self.cfg.get("music", {})
        if not phone_cfg.get("enabled"):
            return []
        ssh_host = phone_cfg.get("ssh_host", "")
        music_root = phone_cfg.get("music_root", "")
        if not ssh_host or not music_root:
            self.update_status("phone config missing ssh_host or music_root")
            return []
        try:
            dirs = list_phone_album_dirs(music_cfg.get("ssh_host", ""), ssh_host, music_root)
        except Exception as e:
            logging.exception("Failed to load phone albums")
            self.update_status(f"phone load error: {e}")
            return []

        known = self.phone_library_lookup()
        items = []
        for entry in dirs:
            name = str(entry.get("name", ""))
            rel_dir = str(entry.get("rel_dir", name))
            item = (
                known.get(rel_dir)
                or known.get(os.path.basename(rel_dir))
                or known.get(phone_folder_match_key(rel_dir))
                or known.get(phone_folder_match_key(name))
                or {
                    "artist": name,
                    "album": "",
                    "key": (name.casefold(), f"phone:{rel_dir.casefold()}"),
                    "rel_dir": rel_dir,
                    "mtime": entry.get("mtime"),
                    "search_text": name.casefold(),
                }
            )
            phone_item = dict(item)
            phone_item["phone_rel_dir"] = rel_dir
            phone_item["phone_name"] = name
            phone_item["mtime"] = entry.get("mtime", phone_item.get("mtime"))
            items.append(phone_item)
        items.sort(key=lambda item: (-(item.get("mtime") or 0), item.get("artist", "").casefold()))
        return items

    def phone_library_lookup(self):
        lookup = {}
        for item in self.load_library_index():
            rel_dir = item.get("rel_dir", "")
            if not rel_dir:
                continue
            lookup[rel_dir] = item
            lookup.setdefault(os.path.basename(rel_dir), item)
            normalized = phone_folder_match_key(rel_dir)
            if normalized:
                lookup.setdefault(normalized, item)
        return lookup

    def load_library_items(self, rebuild=False):
        cached_items = self.load_library_index()
        if not rebuild:
            if cached_items:
                self.library_error = ""
                return cached_items

        records = self.load_records(library=True)
        mtimes = self.load_library_mtimes(records)
        if mtimes is None:
            if cached_items:
                return cached_items
            mtimes = {}
        items = [album_item(record, mtimes.get(record.rel_dir)) for record in records]
        items.sort(
            key=lambda item: (
                item["mtime"] is None,
                -(item["mtime"] or 0),
                item["artist"].casefold(),
                item["album"].casefold(),
            )
        )
        self.save_library_index(items)
        return items

    def load_library_index(self):
        path = library_index_path(self.cache_dir)
        if not os.path.exists(path):
            return []
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            logging.exception("Failed to load library index")
            return []
        items = data.get("albums", [])
        if not isinstance(items, list):
            return []
        return [deserialize_library_item(item) for item in items]

    def save_library_index(self, items):
        path = library_index_path(self.cache_dir)
        tmp_path = path + ".tmp"
        payload = {
            "version": 1,
            "generated_at": time.time(),
            "sort": "directory_mtime_desc",
            "albums": [serialize_library_item(item) for item in items],
        }
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2, sort_keys=True)
                f.write("\n")
            os.replace(tmp_path, path)
        except OSError:
            logging.exception("Failed to save library index")

    def load_library_mtimes(self, records):
        music_cfg = self.cfg.get("music", {})
        music_root = music_cfg.get("root", "")
        ssh_host = music_cfg.get("ssh_host", "")
        rel_dirs = sorted({record.rel_dir for record in records if record.rel_dir})
        if not music_root or not rel_dirs:
            return {}
        if not ssh_host:
            return self.load_local_mtimes(music_root, rel_dirs)
        return self.load_remote_mtimes(ssh_host, music_root, rel_dirs)

    def load_local_mtimes(self, music_root, rel_dirs):
        mtimes = {}
        for rel_dir in rel_dirs:
            path = os.path.join(music_root, rel_dir)
            try:
                mtimes[rel_dir] = os.path.getmtime(path)
            except OSError:
                pass
        return mtimes

    def load_remote_mtimes(self, ssh_host, music_root, rel_dirs):
        script = r"""
import json
import os
import sys

payload = json.load(sys.stdin)
root = payload["root"]
out = {}
for rel_dir in payload["rel_dirs"]:
    try:
        out[rel_dir] = os.path.getmtime(os.path.join(root, rel_dir))
    except OSError:
        pass
json.dump(out, sys.stdout)
"""
        try:
            proc = subprocess.run(
                ["ssh", "-o", "BatchMode=yes", ssh_host, "python3 -c " + shlex.quote(script)],
                input=json.dumps({"root": music_root, "rel_dirs": rel_dirs}),
                text=True,
                capture_output=True,
                timeout=120,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as e:
            self.library_error = f"library mtime error: {e}"
            logging.exception("Failed to load library mtimes")
            return None
        if proc.returncode != 0:
            stderr = proc.stderr.strip()
            self.library_error = f"library mtime error: {stderr or proc.returncode}"
            logging.warning("Failed to load library mtimes: %s", stderr or proc.returncode)
            return None
        try:
            return {key: float(value) for key, value in json.loads(proc.stdout).items()}
        except (TypeError, ValueError, json.JSONDecodeError):
            self.library_error = "library mtime error: bad ssh output"
            logging.exception("Bad library mtime output")
            return None

    def on_tab_changed(self, event=None):
        tab_id = self.tabs.select()
        tab_text = self.tabs.tab(tab_id, "text")
        if self.active_view in self.view_selected_indices:
            self.view_selected_indices[self.active_view] = self.selected_index
        if tab_text == "Now Playing":
            self.active_view = "nowplaying"
            self.show_nowplaying_panel()
            self.update_text_pane_focus()
            self.refresh_nowplaying()
            return
        if tab_text == "Info":
            self.active_view = "info"
            self.show_nowplaying_panel()
            self.update_text_pane_focus()
            self.refresh_info_tab()
            return
        if tab_text == "Help":
            self.active_view = "help"
            self.show_help_panel()
            self.update_status()
            return
        if tab_text == "Library":
            self.active_view = "library"
        elif tab_text == "Phone":
            self.active_view = "phone"
        else:
            self.active_view = "queue"
        self.show_grid_panel()
        if not self.view_loaded[self.active_view]:
            if self.active_view == "library":
                self.start_library_load()
            else:
                self.refresh()
            return
        self.prepare_active_grid()
        self.reset_images()
        self.render_grid()

    def show_grid_panel(self):
        self.help_frame.pack_forget()
        self.nowplaying_frame.pack_forget()
        if not self.canvas.winfo_ismapped():
            self.canvas.pack(side="left", fill="both", expand=True)

    def show_nowplaying_panel(self):
        self.help_frame.pack_forget()
        self.canvas.pack_forget()
        if not self.nowplaying_frame.winfo_ismapped():
            self.nowplaying_frame.pack(side="top", fill="both", expand=True)

    def show_help_panel(self):
        self.canvas.pack_forget()
        self.nowplaying_frame.pack_forget()
        if not self.help_frame.winfo_ismapped():
            self.help_frame.pack(side="top", fill="both", expand=True)

    def switch_tab(self, idx):
        if idx < 0 or idx >= len(self.tabs.tabs()):
            self.update_status(f"no tab {idx + 1}")
            return
        self.tabs.select(idx)
        return "break"

    def switch_relative_tab(self, direction):
        visible_tabs = list(self.tabs.tabs())
        if not visible_tabs:
            return "break"
        current = self.tabs.select()
        try:
            idx = visible_tabs.index(current)
        except ValueError:
            idx = 0
        next_idx = max(0, min(len(visible_tabs) - 1, idx + direction))
        if next_idx != idx:
            self.tabs.select(visible_tabs[next_idx])
        return "break"

    def view_tab_index(self, view):
        labels = {"queue": "Queue", "library": "Library", "phone": "Phone", "nowplaying": "Now Playing", "info": "Info"}
        wanted = labels.get(view)
        if wanted is None:
            return None
        for idx, tab_id in enumerate(self.tabs.tabs()):
            if self.tabs.tab(tab_id, "text") == wanted:
                return idx
        return None

    def select_view_tab(self, view):
        idx = self.view_tab_index(view)
        if idx is not None:
            self.tabs.select(idx)

    def start_library_load(self, rebuild=False):
        if "library" in self.loading_views:
            self.render_loading("hold up loading your library doggie")
            return
        self.loading_views.add("library")
        self.view_grid_cache.pop("library", None)
        self.view_loaded["library"] = False
        self.begin_loading_screen()
        self.root.update_idletasks()
        self.render_loading("hold up loading your library doggie")
        self.start_loading_animation("hold up loading your library doggie")
        self.root.update_idletasks()
        self.root.after(80, self.start_library_load_worker, rebuild)

    def start_library_load_worker(self, rebuild):
        if "library" not in self.loading_views:
            return
        thread = threading.Thread(target=self.library_load_worker, args=(rebuild,), daemon=True)
        thread.start()
        self.root.after(100, self.poll_load_results)

    def library_load_worker(self, rebuild):
        started_at = time.monotonic()
        try:
            items = self.load_library_items(rebuild=rebuild)
            items_loaded_at = time.monotonic()
            albums = [(item["artist"], item["album"]) for item in items]
            album_keys = [item["key"] for item in items]
            cover_paths, covers_ok, covers_failed = self.cover_paths_for(albums, album_keys)
            covers_loaded_at = time.monotonic()
            logging.info(
                "Library load timings: items=%.3fs covers=%.3fs total=%.3fs",
                items_loaded_at - started_at,
                covers_loaded_at - items_loaded_at,
                covers_loaded_at - started_at,
            )
            self.load_result_queue.put(
                {
                    "view": "library",
                    "items": items,
                    "albums": albums,
                    "album_keys": album_keys,
                    "cover_paths": cover_paths,
                    "covers_ok": covers_ok,
                    "covers_failed": covers_failed,
                    "error": None,
                }
            )
        except Exception as e:
            logging.exception("Failed to load library in background")
            self.load_result_queue.put({"view": "library", "error": e})

    def poll_load_results(self):
        handled = False
        while True:
            try:
                result = self.load_result_queue.get_nowait()
            except queue.Empty:
                break
            handled = True
            if result.get("view") == "library":
                self.finish_library_load(result)
            elif result.get("view") == "phone_transfer_progress":
                self.update_phone_transfer_dialog(result.get("phase", "Sending to phone..."))
            elif result.get("view") == "phone_transfer":
                self.finish_phone_transfer(result)

        if self.loading_views or self.phone_transfer_pending:
            self.root.after(100, self.poll_load_results)
        elif handled:
            self.stop_loading_animation()

    def finish_library_load(self, result):
        self.loading_views.discard("library")
        self.stop_loading_animation()
        error = result.get("error")
        if error is not None:
            self.library_error = f"library load error: {error}"
            self.update_status(self.library_error)
            if self.active_view == "library":
                self.render_loading("Library load failed")
            return

        self.view_items["library"] = result["items"]
        self.view_loaded["library"] = True
        self.view_grid_cache["library"] = {
            "albums": result["albums"],
            "album_keys": result["album_keys"],
            "cover_paths": result["cover_paths"],
            "covers_ok": result["covers_ok"],
            "covers_failed": result["covers_failed"],
        }
        if self.active_view != "library":
            return
        self.clear_loading_items()
        self.apply_grid_cache("library")
        self.reset_images()
        self.render_grid()
        logging.info(
            "Loaded %d library albums; covers cached=%d missing=%d",
            len(self.albums),
            self.covers_ok,
            self.covers_failed,
        )

    def start_loading_animation(self, text):
        self.loading_text = text
        if self.loading_after_id is None:
            self.animate_loading()

    def stop_loading_animation(self):
        if self.loading_after_id is not None:
            self.root.after_cancel(self.loading_after_id)
            self.loading_after_id = None
        self.loading_frame = 0

    def clear_loading_items(self):
        self.canvas.delete("loading")
        self.loading_bg_item = None
        self.loading_text_item = None

    def begin_loading_screen(self):
        self.canvas.delete("all")
        self.loading_bg_item = None
        self.loading_text_item = None

    def animate_loading(self):
        if self.active_view == "library" and "library" in self.loading_views:
            self.render_loading(self.loading_text)
            self.loading_frame += 1
            delay = 160
            self.loading_after_id = self.root.after(delay, self.animate_loading)
        else:
            self.loading_after_id = None

    def render_loading(self, text):
        self.update_scrollregion()
        self.canvas.update_idletasks()
        width = max(self.canvas.winfo_width(), 1)
        height = max(self.canvas.winfo_height(), 1)
        if self.loading_bg_item is None:
            self.loading_bg_item = self.canvas.create_rectangle(
                0,
                0,
                width,
                height,
                fill=PANEL_BG,
                outline=PANEL_BG,
                tags=("loading",),
            )
            self.canvas.tag_lower(self.loading_bg_item)
        else:
            self.canvas.coords(self.loading_bg_item, 0, 0, width, height)

        spinner = "|/-\\"[self.loading_frame % 4]
        if self.loading_text_item is None:
            self.loading_text_item = self.canvas.create_text(
                width // 2,
                height // 2,
                text=f"{text} {spinner}",
                fill=APP_FG,
                font=self.font,
                anchor="center",
                tags=("loading",),
            )
        else:
            self.canvas.coords(self.loading_text_item, width // 2, height // 2)
            self.canvas.itemconfigure(self.loading_text_item, text=f"{text} {spinner}")
        self.update_status(text.lower())

    def load_cover_paths(self):
        paths, ok, failed = self.cover_paths_for(self.albums, self.album_keys)
        self.covers_ok = ok
        self.covers_failed = failed
        return paths

    def cover_paths_for(self, albums, album_keys):
        cover_lookup = self.cover_file_lookup()
        paths = []
        covers_ok = 0
        covers_failed = 0
        for idx, (artist, album) in enumerate(albums):
            album_key = album_keys[idx] if idx < len(album_keys) else None
            path = self.cached_cover_path_from_lookup(cover_lookup, artist, album, album_key)
            paths.append(path)
            if path:
                covers_ok += 1
            else:
                covers_failed += 1
        return paths, covers_ok, covers_failed

    def cover_file_lookup(self):
        try:
            names = sorted(os.listdir(self.cache_dir))
        except OSError:
            return {"names": set(), "prefix": {}}
        name_set = set(names)
        prefix = {}
        for name in names:
            if not name.endswith(".png") or "--" not in name:
                continue
            label = name.split("--", 1)[0]
            prefix.setdefault(label, name)
        return {"names": name_set, "prefix": prefix}

    def cached_cover_path_from_lookup(self, lookup, artist, album, album_key):
        path = get_cover_path(self.cache_dir, artist, album, album_key=album_key)
        name = os.path.basename(path)
        if name in lookup["names"]:
            return path

        legacy_path = get_cover_path(self.cache_dir, artist, album)
        legacy_name = os.path.basename(legacy_path)
        if legacy_name != name and legacy_name in lookup["names"]:
            return legacy_path

        label = safe_filename(f"{artist} - {album}")
        fallback_name = lookup["prefix"].get(label)
        if fallback_name:
            return os.path.join(self.cache_dir, fallback_name)
        return None

    def reset_images(self):
        self.image_cache.clear()
        self.placeholder_image = make_placeholder(self.cell_size)

    def get_image(self, idx):
        if idx < 0 or idx >= len(self.cover_paths):
            return self.placeholder_image
        path = self.cover_paths[idx]
        if not path:
            return self.placeholder_image
        if path in self.image_cache:
            image = self.image_cache.pop(path)
            self.image_cache[path] = image
            return image
        try:
            image = tk.PhotoImage(file=path)
        except Exception:
            return self.placeholder_image
        self.image_cache[path] = image
        while len(self.image_cache) > self.max_image_cache:
            self.image_cache.popitem(last=False)
        return image

    def render_grid(self):
        self.canvas.delete("all")
        self.selected_index = min(
            self.view_selected_indices.get(self.active_view, 0),
            max(len(self.albums) - 1, 0),
        )
        self.last_selected_index = None
        self.update_status()
        self.update_scrollregion()
        self.render_visible()
        self.root.after(0, self.ensure_visible, self.selected_index)

    def cell_rect(self, idx):
        cell_w = self.cell_size + (self.padding * 2)
        row = idx // self.columns
        col = idx % self.columns
        x0 = self.grid_x_offset() + col * cell_w + self.padding
        y0 = row * self.row_height
        x1 = x0 + cell_w - self.padding
        y1 = y0 + self.row_height - self.row_gap
        return x0, y0, x1, y1

    def grid_x_offset(self):
        grid_w = self.columns * (self.cell_size + (self.padding * 2))
        canvas_w = self.canvas.winfo_width()
        return max((canvas_w - grid_w) // 2, 0)

    def render_visible(self):
        self.canvas.delete("all")
        if not self.albums:
            return

        top = self.canvas.canvasy(0)
        bottom = top + self.canvas.winfo_height()
        start_row = max(0, int(top // self.row_height) - 1)
        end_row = min(
            (len(self.albums) + self.columns - 1) // self.columns,
            int(bottom // self.row_height) + 2,
        )
        first_idx = start_row * self.columns
        last_idx = min(len(self.albums), end_row * self.columns)
        cell_w = self.cell_size + (self.padding * 2)

        for idx in range(first_idx, last_idx):
            artist, album = self.albums[idx]
            x0, y0, x1, y1 = self.cell_rect(idx)
            self.canvas.create_rectangle(
                x0,
                y0,
                x1,
                y1,
                fill="#111111",
                outline="#222222",
                width=1,
            )

            image = self.get_image(idx)
            img_x = x0 + (cell_w - self.padding - self.cell_size) // 2
            img_y = y0 + self.top_gap
            self.canvas.create_image(img_x, img_y, image=image, anchor="nw")

            self.canvas.create_text(
                x0 + (cell_w - self.padding) // 2,
                img_y + self.cell_size + self.title_gap,
                text=self.title_text(artist, cell_w - (self.padding * 2)),
                fill="#eeeeee",
                font=self.font,
                anchor="n",
                justify="center",
                width=cell_w - (self.padding * 2),
            )
            if idx == self.selected_index:
                self.canvas.create_rectangle(
                    x0,
                    y0,
                    x1,
                    y1,
                    outline="#7fff00",
                    width=4,
                )
    def title_text(self, artist, max_width):
        return elide_text(artist, self.title_font, max_width)

    def update_status(self, note=None):
        if self.active_view == "nowplaying":
            msg = "Now Playing"
            if note:
                msg += f" | {note}"
            pane = self.text_pane_focus.get("nowplaying", "tracks")
            msg += f" | j/k={pane}, J/K=tracks/bio, r=refresh, q=quit"
            self.status.configure(text=msg)
            return
        if self.active_view == "info":
            msg = "Info"
            if note:
                msg += f" | {note}"
            pane = self.text_pane_focus.get("info", "tracks")
            msg += f" | j/k={pane}, J/K=tracks/bio, i=close info, r=refresh, q=quit"
            self.status.configure(text=msg)
            return
        if self.active_view == "help":
            msg = "Help"
            if note:
                msg += f" | {note}"
            msg += " | ? or q=close help"
            self.status.configure(text=msg)
            return
        view = {"queue": "Queue", "library": "Library", "phone": "Phone"}.get(self.active_view, self.active_view.title())
        msg = f"{view}: {len(self.albums)} albums | covers cached {self.covers_ok}, missing {self.covers_failed}"
        if self.active_view == "library":
            dated = sum(1 for item in self.active_items() if item.get("mtime") is not None)
            msg += f" | mtimes {dated}/{len(self.albums)}"
            if self.library_error:
                msg += f" | {self.library_error}"
        if not self.convert_available and Image is None:
            msg += " | no ImageMagick or Pillow"
        elif not self.convert_available:
            msg += " | no ImageMagick"
        if note:
            msg += f" | {note}"
        msg += " | 1/2/3/4 tabs, Enter=add/select, p=send to phone, o=now playing, f=fullscreen, Esc=exit fullscreen, r=refresh, q=quit"
        self.status.configure(text=msg)

    def set_fullscreen(self, enabled):
        self.fullscreen = bool(enabled)
        self.root.attributes("-fullscreen", self.fullscreen)
        self.root.after(0, self.on_canvas_configure)
        self.root.after(0, self.ensure_visible, self.selected_index)

    def toggle_fullscreen(self):
        self.set_fullscreen(not self.fullscreen)

    def close_info_or_quit(self):
        if self.active_view == "help":
            self.close_key_help()
            return "break"
        if self.active_view == "info":
            self.close_info_tab()
            return "break"
        self.root.destroy()
        return "break"

    def show_key_help(self):
        if self.active_view == "help":
            self.close_key_help()
            return "break"
        self.help_return_view = self.active_view if self.active_view in (*self.grid_views, "nowplaying", "info") else "queue"
        if not self.help_tab_visible:
            self.tabs.add(self.help_tab, text="Help")
            self.help_tab_visible = True
        self.tabs.select(self.help_tab)
        return "break"

    def handle_g_key(self):
        if self.active_view not in self.grid_views:
            return "break"
        if self.pending_leader == "g":
            self.pending_leader = None
            return self.jump_grid_position("top")
        self.pending_leader = "g"
        self.update_status("g: press g for top")
        return "break"

    def jump_grid_position(self, where):
        self.pending_leader = None
        if self.active_view not in self.grid_views or not self.albums:
            return "break"
        if where == "top":
            idx = 0
        elif where == "bottom":
            idx = len(self.albums) - 1
        elif where == "middle":
            idx = len(self.albums) // 2
        elif where == "random":
            idx = random.randrange(len(self.albums))
        else:
            return "break"
        self.view_selected_indices[self.active_view] = idx
        self.selected_index = idx
        self.last_selected_index = None
        self.ensure_visible(idx)
        self.render_visible()
        return "break"

    def begin_library_search(self):
        if self.active_view != "library":
            return "break"
        self.pending_leader = None
        if not self.search_entry.winfo_ismapped():
            self.search_entry.pack(side="bottom", fill="x", before=self.status)
        self.search_var.set(self.library_search_query)
        self.search_entry.focus_set()
        self.search_entry.icursor("end")
        self.update_status("search library: artist or album")
        return "break"

    def finish_library_search(self, event=None):
        query = self.search_var.get().strip()
        self.search_entry.pack_forget()
        self.canvas.focus_set()
        if not query:
            self.library_search_query = ""
            self.library_search_matches = []
            self.update_status("search cleared")
            return "break"
        self.library_search_query = query
        folded = query.casefold()
        self.library_search_matches = [
            idx
            for idx, item in enumerate(self.view_items["library"])
            if folded in item.get("search_text", "").casefold()
        ]
        if not self.library_search_matches:
            self.update_status(f"no matches: {query}")
            return "break"
        self.jump_to_search_match(1, include_current=True)
        return "break"

    def cancel_library_search(self, event=None):
        self.search_entry.pack_forget()
        self.canvas.focus_set()
        self.update_status("search cancelled")
        return "break"

    def clear_library_search_entry(self, event=None):
        self.search_var.set("")
        return "break"

    def next_library_search_match(self, direction):
        if self.active_view != "library":
            return "break"
        if not self.library_search_query:
            self.update_status("no active search")
            return "break"
        if not self.library_search_matches:
            self.update_status(f"no matches: {self.library_search_query}")
            return "break"
        self.jump_to_search_match(direction)
        return "break"

    def jump_to_search_match(self, direction, include_current=False):
        matches = self.library_search_matches
        current = self.selected_index
        if direction >= 0:
            candidates = [idx for idx in matches if idx >= current] if include_current else [idx for idx in matches if idx > current]
            idx = candidates[0] if candidates else matches[0]
        else:
            candidates = [idx for idx in matches if idx <= current] if include_current else [idx for idx in matches if idx < current]
            idx = candidates[-1] if candidates else matches[-1]
        self.view_selected_indices["library"] = idx
        self.selected_index = idx
        self.last_selected_index = None
        self.ensure_visible(idx)
        self.render_visible()
        self.update_status(f"search {self.library_search_matches.index(idx) + 1}/{len(matches)}: {self.library_search_query}")

    def close_key_help(self):
        return_view = self.help_return_view if self.help_return_view in (*self.grid_views, "nowplaying", "info") else "queue"
        if self.help_tab_visible:
            self.tabs.hide(self.help_tab)
            self.help_tab_visible = False
        self.select_view_tab(return_view)
        return "break"

    def key_help_text(self):
        return "\n".join(
            [
                "Gridmode Keys",
                "",
                "Tabs",
                "  1  Queue",
                "  2  Library",
                "  3  Now Playing",
                "",
                "Grid Navigation",
                "  h / Left   move left",
                "  l / Right  move right",
                "  j / Down   move down",
                "  k / Up     move up",
                "  J          page down",
                "  K          page up",
                "  PageDown   page down",
                "  PageUp     page up",
                "  z          jump to random album",
                "  gg         jump to top",
                "  G          jump to bottom",
                "  M          jump to middle",
                "  mouse      scroll and click to select",
                "",
                "Queue",
                "  Enter      play selected album",
                "  d          remove selected album from current playlist",
                "  i          open selected album info",
                "  o          jump selection to currently playing album",
                "  s          save current playlist",
                "  S          save current playlist as",
                "",
                "Library",
                "  /          search artist or album",
                "  n / N      next / previous search match",
                "  Enter      insert selected album after current album and play it",
                "  a          insert selected album after current album",
                "  A          append selected album to end of playlist",
                "  i          open selected album info",
                "  o          jump selection to currently playing album",
                "  r          rebuild/refresh library",
                "",
                "Info",
                "  j / k      move track highlight",
                "  J / K      move j/k focus between tracks and artist bio",
                "  Enter      insert album after current album and play highlighted track",
                "  i or q     close Info tab",
                "",
                "Now Playing",
                "  j / k      move track highlight",
                "  J / K      move j/k focus between tracks and artist bio",
                "  Enter      play highlighted track",
                "  o          jump highlight to currently playing track",
                "",
                "Global",
                "  H / L      move to previous / next tab",
                "  t          add current song to sick_tunes",
                "  f          toggle fullscreen",
                "  Esc        exit fullscreen",
                "  ?          show this help",
                "  q          quit, except Info where it closes Info",
            ]
        )

    def configure_cell_selection(self, idx, selected):
        self.render_visible()

    def update_selection(self):
        if self.last_selected_index == self.selected_index:
            return
        previous_index = self.last_selected_index
        self.last_selected_index = self.selected_index
        previous_row = previous_index // self.columns if previous_index is not None else None
        selected_row = self.selected_index // self.columns
        if previous_row != selected_row:
            self.ensure_visible(self.selected_index)
        self.render_visible()

    def move_selection(self, dx, dy):
        if self.active_view not in self.grid_views:
            return
        if not self.albums:
            return
        cols = self.columns
        idx = self.selected_index
        x = idx % cols
        y = idx // cols
        x = max(0, min(cols - 1, x + dx))
        y = max(0, y + dy)
        new_idx = y * cols + x
        if new_idx >= len(self.albums):
            new_idx = len(self.albums) - 1
        if new_idx == self.selected_index:
            return
        self.view_selected_indices[self.active_view] = new_idx
        self.selected_index = new_idx
        self.update_selection()

    def move_vertical(self, direction):
        if self.active_view in ("nowplaying", "info"):
            if self.text_pane_focus.get(self.active_view) == "bio":
                self.bio_text.yview_scroll(direction * 3, "units")
                return
            self.move_track_selection(direction)
            return
        self.move_selection(0, direction)

    def toggle_text_pane_focus(self):
        if self.active_view not in ("nowplaying", "info"):
            return
        current = self.text_pane_focus.get(self.active_view, "tracks")
        self.text_pane_focus[self.active_view] = "bio" if current == "tracks" else "tracks"
        self.update_text_pane_focus()
        return "break"

    def move_pane_or_page(self, direction):
        if self.active_view in ("nowplaying", "info"):
            self.set_text_pane_focus("bio" if direction > 0 else "tracks")
            return
        self.page_scroll(direction)

    def set_text_pane_focus(self, pane):
        if self.active_view not in ("nowplaying", "info"):
            return
        self.text_pane_focus[self.active_view] = pane
        self.update_text_pane_focus()

    def update_text_pane_focus(self):
        focused = self.text_pane_focus.get(self.active_view, "tracks")
        if focused == "bio":
            self.track_box.configure(bg=PANEL_BG)
            self.bio_box.configure(bg=PANE_FOCUS)
        else:
            self.track_box.configure(bg=PANE_FOCUS)
            self.bio_box.configure(bg=PANEL_BG)
        self.update_status()

    def move_track_selection(self, direction):
        info = self.current_track_info()
        if not info or not info["tracks"]:
            return
        view = self.active_view
        idx = self.track_selected_indices.get(view, 0)
        idx = max(0, min(len(info["tracks"]) - 1, idx + direction))
        if idx == self.track_selected_indices.get(view, 0):
            return
        self.track_selected_indices[view] = idx
        self.track_selection_manual[view] = True
        self.update_track_highlight()

    def current_track_info(self):
        if self.active_view == "nowplaying":
            return self.nowplaying_info
        if self.active_view == "info":
            return self.info_info
        return None

    def update_track_highlight(self):
        info = self.current_track_info()
        if not info:
            return
        view = self.active_view
        selected = min(self.track_selected_indices.get(view, 0), max(len(info["tracks"]) - 1, 0))
        self.track_selected_indices[view] = selected
        self.track_text.configure(state="normal")
        self.track_text.tag_remove("selected", "1.0", "end")
        if info["tracks"]:
            line = selected + 1
            self.track_text.tag_add("selected", f"{line}.0", f"{line}.end")
            self.track_text.see(f"{line}.0")
        self.track_text.configure(state="disabled")

    def page_scroll(self, direction):
        if self.active_view not in self.grid_views:
            return
        if not self.albums:
            return
        self.canvas.yview_scroll(direction, "page")
        self.select_visible_scroll_anchor()
        self.render_visible()

    def select_visible_scroll_anchor(self):
        top_row = max(0, int(self.canvas.canvasy(0) // self.row_height))
        col = self.selected_index % self.columns
        new_idx = min(top_row * self.columns + col, len(self.albums) - 1)
        if new_idx < 0:
            return
        self.view_selected_indices[self.active_view] = new_idx
        self.selected_index = new_idx
        self.last_selected_index = new_idx

    def on_canvas_click(self, event):
        idx = self.index_at_canvas_point(event.x, event.y)
        if idx is None:
            return "break"
        self.canvas.focus_set()
        self.view_selected_indices[self.active_view] = idx
        self.selected_index = idx
        self.last_selected_index = idx
        self.render_visible()
        return "break"

    def index_at_canvas_point(self, x, y):
        if not self.albums:
            return None
        canvas_x = self.canvas.canvasx(x)
        canvas_y = self.canvas.canvasy(y)
        cell_w = self.cell_size + (self.padding * 2)
        col = int((canvas_x - self.grid_x_offset()) // cell_w)
        row = int(canvas_y // self.row_height)
        if col < 0 or col >= self.columns or row < 0:
            return None
        idx = row * self.columns + col
        if idx < 0 or idx >= len(self.albums):
            return None
        x0, y0, x1, y1 = self.cell_rect(idx)
        if x0 <= canvas_x <= x1 and y0 <= canvas_y <= y1:
            return idx
        return None

    def jump_to_current_album(self):
        if self.active_view == "help":
            return
        if self.active_view == "nowplaying":
            self.select_current_nowplaying_track()
            return
        if self.active_view == "info":
            return
        mpd_host = require_cfg(self.cfg, "mpd", "host")
        mpd_port = require_cfg(self.cfg, "mpd", "port")
        mpd_password = self.cfg.get("mpd", {}).get("password", "")

        try:
            client = connect_mpd(mpd_host, mpd_port, mpd_password)
            try:
                song = client.currentsong()
            finally:
                try:
                    client.close()
                    client.disconnect()
                except Exception:
                    pass
        except Exception as e:
            logging.exception("Failed to read current song from MPD")
            self.update_status(f"mpd error: {e}")
            return

        artist = _tag_to_str(song.get("artist", "")).strip()
        album = _tag_to_str(song.get("album", "")).strip()
        if not artist or not album:
            self.update_status("now playing has no album")
            return

        current_key = album_group_key(song)
        for idx, grid_key in enumerate(self.album_keys):
            if grid_key == current_key:
                if idx != self.selected_index:
                    self.selected_index = idx
                    self.update_selection()
                else:
                    self.ensure_visible(idx)
                self.update_status(f"now playing: {artist} - {album}")
                return

        self.update_status(f"now playing not in grid: {artist} - {album}")

    def select_current_nowplaying_track(self):
        info = self.nowplaying_info
        if not info:
            self.refresh_nowplaying()
            info = self.nowplaying_info
        if not info:
            return
        current_idx = next((idx for idx, track in enumerate(info["tracks"]) if track["current"]), None)
        if current_idx is None:
            self.update_status("no current track in album")
            return
        self.track_selected_indices["nowplaying"] = current_idx
        self.track_selection_manual["nowplaying"] = False
        self.update_track_highlight()

    def refresh_nowplaying(self):
        try:
            info = self.load_nowplaying_info()
        except Exception as e:
            logging.exception("Failed to load now playing info")
            self.update_nowplaying_empty(f"mpd error: {e}")
            return

        if not info:
            self.update_nowplaying_empty("nothing playing")
            return

        previous = self.nowplaying_info
        if previous and previous.get("key") == info.get("key"):
            if self.track_selection_manual.get("nowplaying", False):
                self.track_selected_indices["nowplaying"] = min(
                    self.track_selected_indices.get("nowplaying", 0),
                    max(len(info["tracks"]) - 1, 0),
                )
            else:
                self.track_selected_indices["nowplaying"] = self.current_track_index(info)
        else:
            self.track_selected_indices["nowplaying"] = 0
            self.track_selection_manual["nowplaying"] = False
        self.nowplaying_info = info
        self.render_nowplaying(info)
        self.update_status(f"{info['artist']} - {info['album']}")

    def current_track_index(self, info):
        return next((idx for idx, track in enumerate(info["tracks"]) if track["current"]), 0)

    def toggle_info_tab(self):
        if self.active_view == "nowplaying":
            return "break"
        if self.active_view == "info":
            self.close_info_tab()
            return "break"
        if self.active_view not in ("queue", "library") or not self.albums:
            return "break"

        item = self.active_items()[self.selected_index]
        self.info_album = dict(item)
        self.info_return_view = self.active_view
        self.track_selected_indices["info"] = 0
        self.track_selection_manual["info"] = False
        self.text_pane_focus["info"] = "tracks"
        if not self.info_tab_visible:
            self.tabs.add(self.info_tab, text="Info")
            self.info_tab_visible = True
        self.tabs.select(self.info_tab)
        return "break"

    def close_info_tab(self):
        return_view = self.info_return_view if self.info_return_view in self.grid_views else "queue"
        if self.info_tab_visible:
            self.tabs.hide(self.info_tab)
            self.info_tab_visible = False
        self.info_album = None
        self.info_info = None
        self.select_view_tab(return_view)

    def refresh_info_tab(self):
        if not self.info_album:
            self.update_nowplaying_empty("no album selected")
            return
        try:
            info = self.load_selected_album_info(self.info_album)
        except Exception as e:
            logging.exception("Failed to load selected album info")
            self.update_nowplaying_empty(f"mpd error: {e}")
            return
        if not info:
            self.update_nowplaying_empty("no album selected")
            return
        self.info_info = info
        self.track_selected_indices["info"] = min(
            self.track_selected_indices.get("info", 0),
            max(len(info["tracks"]) - 1, 0),
        )
        self.render_nowplaying(info)
        self.update_status(f"{info['artist']} - {info['album']}")

    def load_selected_album_info(self, item):
        mpd_host = require_cfg(self.cfg, "mpd", "host")
        mpd_port = require_cfg(self.cfg, "mpd", "port")
        mpd_password = self.cfg.get("mpd", {}).get("password", "")

        client = connect_mpd(mpd_host, mpd_port, mpd_password, timeout=30)
        try:
            if self.info_return_view == "library":
                songs = self.find_library_album_songs(client, item)
            else:
                songs = self.find_queue_album_songs(client, item)
        finally:
            try:
                client.close()
                client.disconnect()
            except Exception:
                pass

        return self.album_info_from_songs(item, songs)

    def find_queue_album_songs(self, client, item):
        try:
            playlist = client.playlistinfo()
        except Exception:
            return []
        positions = item.get("positions")
        if positions:
            wanted = set(positions)
            return [
                song
                for song in playlist
                if song_playlist_position(song) in wanted
            ]
        return self.filter_album_songs(playlist, item.get("key"))

    def album_info_from_songs(self, item, songs):
        artist = item.get("artist", "")
        album = item.get("album", "")
        album_key = item.get("key")
        tracks = []
        for idx, song in enumerate(songs):
            try:
                pos = int(song.get("pos", idx))
            except (TypeError, ValueError):
                pos = idx
            tracks.append(
                {
                    "pos": pos,
                    "track": _tag_to_str(song.get("track", "")).strip(),
                    "title": _tag_to_str(song.get("title", "")).strip() or os.path.basename(_tag_to_str(song.get("file", ""))),
                    "artist": _tag_to_str(song.get("artist", "")).strip(),
                    "file": _tag_to_str(song.get("file", "")).strip(),
                    "current": False,
                }
            )
        tracks.sort(key=lambda track: track["pos"])

        return {
            "artist": artist,
            "album": album,
            "year": release_year_from_songs(songs),
            "key": album_key,
            "rel_dir": item.get("rel_dir", ""),
            "current": None,
            "tracks": tracks,
            "cover_path": cached_cover_path(self.cache_dir, artist, album, album_key=album_key),
            "bio": self.artist_bio(artist),
        }

    def load_nowplaying_info(self):
        mpd_host = require_cfg(self.cfg, "mpd", "host")
        mpd_port = require_cfg(self.cfg, "mpd", "port")
        mpd_password = self.cfg.get("mpd", {}).get("password", "")

        client = connect_mpd(mpd_host, mpd_port, mpd_password, timeout=30)
        try:
            current = client.currentsong()
            if not current:
                return None
            playlist = client.playlistinfo()
        finally:
            try:
                client.close()
                client.disconnect()
            except Exception:
                pass

        current_pos = int(current.get("pos", 0))
        current_key = album_group_key(current)
        artist = _tag_to_str(current.get("albumartist", "")).strip() or _tag_to_str(current.get("artist", "")).strip()
        album = _tag_to_str(current.get("album", "")).strip()
        rel_dir = posixpath.dirname(_tag_to_str(current.get("file", "")).strip())
        if current_key is None:
            album_block = [current]
        else:
            start_pos = current_pos
            end_pos = current_pos
            for idx in range(current_pos - 1, -1, -1):
                if album_group_key(playlist[idx]) != current_key:
                    break
                start_pos = idx
            for idx in range(current_pos + 1, len(playlist)):
                if album_group_key(playlist[idx]) != current_key:
                    break
                end_pos = idx
            album_block = playlist[start_pos : end_pos + 1]

        tracks = []
        for song in album_block:
            try:
                pos = int(song.get("pos", -1))
            except (TypeError, ValueError):
                pos = -1
            tracks.append(
                {
                    "pos": pos,
                    "track": _tag_to_str(song.get("track", "")).strip(),
                    "title": _tag_to_str(song.get("title", "")).strip() or os.path.basename(_tag_to_str(song.get("file", ""))),
                    "artist": _tag_to_str(song.get("artist", "")).strip(),
                    "file": _tag_to_str(song.get("file", "")).strip(),
                    "current": pos == current_pos,
                }
            )
        tracks.sort(key=lambda item: item["pos"])

        cover_path = cached_cover_path(self.cache_dir, artist, album, album_key=current_key)
        return {
            "artist": artist,
            "album": album,
            "year": release_year_from_songs(album_block) or release_year_from_song(current),
            "key": current_key,
            "rel_dir": rel_dir,
            "current": current,
            "tracks": tracks,
            "cover_path": cover_path,
            "bio": self.artist_bio(artist),
        }

    def update_nowplaying_empty(self, message):
        self.nowplaying_cover = make_placeholder(self.nowplaying_cover_size)
        self.nowplaying_cover_label.configure(image=self.nowplaying_cover)
        self.nowplaying_title.configure(text=message)
        if self.active_view == "nowplaying":
            self.nowplaying_info = None
        elif self.active_view == "info":
            self.info_info = None
        self.set_text_widget(self.track_text, "")
        self.set_text_widget(self.bio_text, "")
        self.update_status(message)

    def render_nowplaying(self, info, schedule_rerender=True):
        if info["cover_path"]:
            try:
                self.nowplaying_cover = self.load_nowplaying_cover(info["cover_path"])
            except Exception:
                self.nowplaying_cover = make_placeholder(self.nowplaying_cover_size)
        else:
            self.nowplaying_cover = make_placeholder(self.nowplaying_cover_size)
        self.nowplaying_cover_label.configure(image=self.nowplaying_cover)
        title_lines = [info["artist"], info["album"]]
        if info.get("year"):
            title_lines.append(str(info["year"]))
        self.nowplaying_title.configure(text="\n".join(title_lines))

        lines = [self.track_line(info, track) for track in info["tracks"]]
        padded_lines = [self.pad_track_line(line) for line in lines]
        self.track_text.configure(state="normal")
        self.track_text.delete("1.0", "end")
        selected = min(
            self.track_selected_indices.get(self.active_view, 0),
            max(len(info["tracks"]) - 1, 0),
        )
        self.track_selected_indices[self.active_view] = selected
        for idx, track in enumerate(info["tracks"]):
            line = padded_lines[idx]
            start = self.track_text.index("end-1c")
            self.track_text.insert("end", line + "\n")
            end = self.track_text.index("end-1c")
            if track["current"]:
                self.track_text.tag_add("current", start, end)
            if idx == selected:
                self.track_text.tag_add("selected", start, end)
        self.track_text.configure(state="disabled")
        if info["tracks"]:
            self.scroll_track_row_into_view(selected)

        bio = info["bio"] or "No artist bio cached."
        self.set_text_widget(self.bio_text, bio)
        if schedule_rerender:
            self.schedule_nowplaying_rerender()

    def track_line(self, info, track):
        number = track["track"].split("/", 1)[0]
        prefix = f"{number:>3}  " if number else "     "
        line = f"{prefix}{track['title']}"
        if track["artist"] and track["artist"] != info["artist"]:
            line += f" - {track['artist']}"
        return line

    def pad_track_line(self, line):
        char_width = max(self.nowplaying_text_font.measure("W"), 1)
        widget_width = self.track_text.winfo_width()
        if widget_width < char_width * 8:
            return line
        width = max(widget_width - 12, char_width)
        cols = max(int(width // char_width), len(line))
        if self.nowplaying_text_font.measure(line) > width:
            line = elide_text(line, self.nowplaying_text_font, width)
        return line.ljust(cols)

    def scroll_track_row_into_view(self, idx):
        if idx < 0:
            return
        self.track_text.see(f"{idx + 1}.0")

    def schedule_nowplaying_rerender(self):
        if self.active_view not in ("nowplaying", "info"):
            return
        if self.nowplaying_rerender_id is not None:
            return
        self.nowplaying_rerender_id = self.root.after(100, self.rerender_nowplaying_after_layout)

    def rerender_nowplaying_after_layout(self):
        self.nowplaying_rerender_id = None
        info = self.current_track_info()
        if info and self.track_text.winfo_width() >= self.nowplaying_text_font.measure("W") * 8:
            self.render_nowplaying(info, schedule_rerender=False)

    def load_nowplaying_cover(self, path):
        if Image is None:
            return tk.PhotoImage(file=path)
        with Image.open(path) as image:
            image = image.convert("RGB")
            image = image.resize((self.nowplaying_cover_size, self.nowplaying_cover_size))
            ppm = f"P6 {image.width} {image.height} 255\n".encode("ascii") + image.tobytes()
        return tk.PhotoImage(data=ppm, format="PPM")

    def set_text_widget(self, widget, text):
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        widget.insert("1.0", text)
        widget.configure(state="disabled")

    def load_artist_bios(self):
        path = artist_bios_path(self.cache_dir)
        if not os.path.exists(path):
            return {}
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            logging.exception("Failed to load artist bio cache")
            return {}
        return data if isinstance(data, dict) else {}

    def save_artist_bios(self):
        path = artist_bios_path(self.cache_dir)
        tmp_path = path + ".tmp"
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(self.artist_bios, f, indent=2, sort_keys=True)
                f.write("\n")
            os.replace(tmp_path, path)
        except OSError:
            logging.exception("Failed to save artist bio cache")

    def artist_bio(self, artist):
        if not artist:
            return ""
        key = artist.casefold()
        cached = self.artist_bios.get(key)
        if isinstance(cached, dict) and "bio" in cached:
            return cached["bio"]

        api_key = self.cfg.get("lastfm", {}).get("api_key", "")
        if not api_key:
            return ""
        params = {
            "method": "artist.getInfo",
            "artist": artist,
            "api_key": api_key,
            "format": "json",
            "autocorrect": "1",
        }
        headers = {"User-Agent": DEFAULT_USER_AGENT}
        try:
            resp = requests.get("https://ws.audioscrobbler.com/2.0/", params=params, headers=headers, timeout=10)
        except requests.RequestException:
            logging.exception("Artist bio request failed")
            return ""
        if resp.status_code != 200:
            logging.warning("Artist bio request failed: %s", resp.status_code)
            return ""
        try:
            data = resp.json()
        except ValueError:
            return ""
        bio = ""
        artist_data = data.get("artist") if isinstance(data, dict) else None
        if isinstance(artist_data, dict):
            bio_data = artist_data.get("bio")
            if isinstance(bio_data, dict):
                bio = _tag_to_str(bio_data.get("summary", "")).strip()
                if "<a href=" in bio:
                    bio = re.sub(r"<[^>]+>", "", bio).strip()
        self.artist_bios[key] = {"artist": artist, "bio": bio, "updated_at": int(time.time())}
        self.save_artist_bios()
        return bio

    def on_select(self):
        if self.active_view == "help":
            return "break"
        if self.active_view == "nowplaying":
            self.play_selected_nowplaying_track()
            return "break"
        if self.active_view == "info":
            self.play_selected_info_track()
            return "break"
        if not self.albums:
            return "break"
        item = self.active_items()[self.selected_index]
        if self.active_view == "library":
            self.add_library_album_after_current_album(item, play=True)
            return "break"
        if self.active_view == "phone":
            self.update_status(f"on phone: {item.get('artist', '')} - {item.get('album', '')}".rstrip(" -"))
            return "break"
        self.play_selected_queue_album(item)
        return "break"

    def selected_library_item(self):
        if self.active_view != "library" or not self.albums:
            return None
        return self.active_items()[self.selected_index]

    def add_selected_library_album_after_current(self):
        if self.active_view == "help":
            return "break"
        item = self.selected_library_item()
        if item is None:
            return "break"
        self.add_library_album_after_current_album(item, play=False)
        return "break"

    def append_selected_library_album(self):
        if self.active_view == "help":
            return "break"
        item = self.selected_library_item()
        if item is None:
            return "break"
        self.append_library_album_to_playlist(item)
        return "break"

    def selected_album_for_phone(self):
        if self.active_view in ("queue", "library") and self.albums:
            return dict(self.active_items()[self.selected_index])
        if self.active_view == "info" and self.info_info:
            return dict(self.info_info)
        if self.active_view == "nowplaying":
            if not self.nowplaying_info:
                self.refresh_nowplaying()
            if self.nowplaying_info:
                return dict(self.nowplaying_info)
        return None

    def show_phone_transfer_dialog(self, item):
        if self.phone_transfer_dialog is not None and self.phone_transfer_dialog.winfo_exists():
            self.phone_transfer_dialog.lift()
            return

        artist = item.get("artist", "")
        album = item.get("album", "")
        title = f"{artist} - {album}".strip(" -") or item.get("rel_dir", "selected album")
        self.phone_transfer_phase_var = tk.StringVar(value="Starting phone send")
        self.phone_transfer_detail_var = tk.StringVar(value=title)
        self.phone_transfer_elapsed_var = tk.StringVar(value="Elapsed 0s")
        self.phone_transfer_done = False
        self.phone_transfer_started_at = time.monotonic()

        dialog = tk.Toplevel(self.root)
        self.phone_transfer_dialog = dialog
        dialog.title("Sending to Phone")
        dialog.transient(self.root)
        dialog.resizable(False, False)
        dialog.configure(bg=APP_BG)
        dialog.protocol("WM_DELETE_WINDOW", self.cancel_or_close_phone_transfer_dialog)

        frame = ttk.Frame(dialog, padding=16)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="Sending to phone", font=self.nowplaying_title_font).pack(anchor="w")
        ttk.Label(frame, textvariable=self.phone_transfer_detail_var, wraplength=520).pack(anchor="w", pady=(8, 0))
        ttk.Label(frame, textvariable=self.phone_transfer_phase_var, wraplength=520).pack(anchor="w", pady=(12, 0))
        ttk.Label(frame, textvariable=self.phone_transfer_elapsed_var).pack(anchor="w", pady=(4, 0))

        buttons = ttk.Frame(frame)
        buttons.pack(fill="x", pady=(16, 0))
        self.phone_transfer_phone_button = ttk.Button(buttons, text="Phone Tab", command=self.finish_phone_transfer_and_show_phone)
        self.phone_transfer_phone_button.pack(side="left")
        self.phone_transfer_phone_button.configure(state="disabled")
        self.phone_transfer_cancel_button = ttk.Button(buttons, text="Cancel", command=self.cancel_phone_transfer)
        self.phone_transfer_cancel_button.pack(side="right")
        self.phone_transfer_close_button = ttk.Button(buttons, text="Close", command=self.close_phone_transfer_dialog)
        self.phone_transfer_close_button.pack(side="right", padx=(0, 8))
        self.phone_transfer_close_button.configure(state="disabled")

        self.update_phone_transfer_elapsed()
        dialog.update_idletasks()
        x = self.root.winfo_rootx() + max(0, (self.root.winfo_width() - dialog.winfo_width()) // 2)
        y = self.root.winfo_rooty() + max(0, (self.root.winfo_height() - dialog.winfo_height()) // 3)
        dialog.geometry(f"+{x}+{y}")
        dialog.lift()

    def update_phone_transfer_elapsed(self):
        if self.phone_transfer_dialog is None or not self.phone_transfer_dialog.winfo_exists():
            self.phone_transfer_elapsed_after_id = None
            return
        if self.phone_transfer_started_at is not None and self.phone_transfer_elapsed_var is not None:
            elapsed = int(time.monotonic() - self.phone_transfer_started_at)
            self.phone_transfer_elapsed_var.set(f"Elapsed {elapsed}s")
        if not self.phone_transfer_done:
            self.phone_transfer_elapsed_after_id = self.root.after(500, self.update_phone_transfer_elapsed)
        else:
            self.phone_transfer_elapsed_after_id = None

    def update_phone_transfer_dialog(self, phase, detail=None):
        if self.phone_transfer_phase_var is not None:
            self.phone_transfer_phase_var.set(phase)
        if detail and self.phone_transfer_detail_var is not None:
            self.phone_transfer_detail_var.set(detail)
        self.update_status(phase.lower())

    def complete_phone_transfer_dialog(self, phase, detail=None, success=False):
        self.phone_transfer_done = True
        self.update_phone_transfer_dialog(phase, detail)
        if self.phone_transfer_cancel_button is not None:
            self.phone_transfer_cancel_button.configure(state="disabled")
        if self.phone_transfer_close_button is not None:
            self.phone_transfer_close_button.configure(state="normal")
        if success and self.phone_transfer_phone_button is not None:
            self.phone_transfer_phone_button.configure(state="normal")

    def cancel_or_close_phone_transfer_dialog(self):
        if self.phone_transfer_done:
            self.close_phone_transfer_dialog()
        else:
            self.cancel_phone_transfer()

    def cancel_phone_transfer(self):
        if self.phone_transfer_done:
            return
        if self.phone_transfer_cancel_event is not None:
            self.phone_transfer_cancel_event.set()
        if self.phone_transfer_cancel_button is not None:
            self.phone_transfer_cancel_button.configure(state="disabled")
        self.update_phone_transfer_dialog("Cancelling phone send...")

    def close_phone_transfer_dialog(self):
        if self.phone_transfer_elapsed_after_id is not None:
            self.root.after_cancel(self.phone_transfer_elapsed_after_id)
            self.phone_transfer_elapsed_after_id = None
        if self.phone_transfer_dialog is not None and self.phone_transfer_dialog.winfo_exists():
            self.phone_transfer_dialog.destroy()
        self.phone_transfer_dialog = None

    def finish_phone_transfer_and_show_phone(self):
        self.close_phone_transfer_dialog()
        if "phone" in self.grid_views:
            self.switch_tab(self.grid_views.index("phone"))

    def send_selected_album_to_phone(self):
        if self.active_view == "help":
            return "break"
        item = self.selected_album_for_phone()
        if item is None:
            return "break"
        if self.phone_transfer_pending:
            self.update_status("phone send already in progress")
            if self.phone_transfer_dialog is not None and self.phone_transfer_dialog.winfo_exists():
                self.phone_transfer_dialog.lift()
            return "break"
        phone_cfg = self.cfg.get("phone", {})
        if not phone_cfg.get("enabled"):
            self.update_status("phone is not enabled")
            return "break"
        if not phone_cfg.get("ssh_host") or not phone_cfg.get("music_root"):
            self.update_status("phone config missing ssh_host or music_root")
            return "break"
        if not item.get("rel_dir"):
            self.update_status("selected album has no source directory")
            return "break"

        self.phone_transfer_pending += 1
        self.phone_transfer_cancel_event = threading.Event()
        self.show_phone_transfer_dialog(item)
        self.update_status(f"sending to phone: {item.get('artist', '')} - {item.get('album', '')}".rstrip(" -"))
        thread = threading.Thread(target=self.phone_transfer_worker, args=(item, self.phone_transfer_cancel_event), daemon=True)
        thread.start()
        self.root.after(100, self.poll_load_results)
        return "break"

    def phone_transfer_worker(self, item, cancel_event):
        def progress(phase):
            self.load_result_queue.put({"view": "phone_transfer_progress", "phase": phase})

        try:
            music_cfg = self.cfg.get("music", {})
            phone_cfg = self.cfg.get("phone", {})
            progress("Preparing album for phone...")
            prepared = prepare_album_for_phone(
                music_cfg.get("ssh_host", ""),
                music_cfg.get("root", ""),
                item.get("rel_dir", ""),
                lossy_root=music_cfg.get("lossy_root", ""),
                prefer_lossy=music_cfg.get("prefer_lossy_for_phone", True),
                transcode_missing=music_cfg.get("transcode_missing_lossy", False),
                cancel_event=cancel_event,
            )
            progress("Copying cover art...")
            cover_path = self.cached_cover_path_from_lookup(
                self.cover_file_lookup(),
                item.get("artist", ""),
                item.get("album", ""),
                item.get("key"),
            )
            cover_copied = copy_local_cover_to_remote_album(
                cover_path,
                music_cfg.get("ssh_host", ""),
                prepared["path"],
                cancel_event=cancel_event,
            )
            progress("Copying album to phone...")
            copied = copy_remote_album_to_phone(
                music_cfg.get("ssh_host", ""),
                prepared["path"],
                phone_cfg.get("ssh_host", ""),
                phone_cfg.get("music_root", ""),
                cancel_event=cancel_event,
            )
            self.load_result_queue.put(
                {
                    "view": "phone_transfer",
                    "item": item,
                    "prepared": prepared,
                    "copied": copied,
                    "cover_copied": cover_copied,
                    "error": None,
                }
            )
        except PhoneTransferCancelled as e:
            self.load_result_queue.put({"view": "phone_transfer", "item": item, "cancelled": True, "error": e})
        except Exception as e:
            logging.exception("Failed to send album to phone")
            self.load_result_queue.put({"view": "phone_transfer", "item": item, "error": e})

    def finish_phone_transfer(self, result):
        self.phone_transfer_pending = max(0, self.phone_transfer_pending - 1)
        self.phone_transfer_cancel_event = None
        error = result.get("error")
        item = result.get("item") or {}
        if result.get("cancelled"):
            self.complete_phone_transfer_dialog("Phone send cancelled")
            self.update_status("phone send cancelled")
            return
        if error is not None:
            message = f"phone send error: {error}"
            self.complete_phone_transfer_dialog("Phone send failed", message)
            self.update_status(message)
            return
        prepared = result.get("prepared") or {}
        copied = result.get("copied") or {}
        self.view_loaded["phone"] = False
        self.view_grid_cache.pop("phone", None)
        kind = prepared.get("kind", "album")
        created = ", transcoded" if prepared.get("created") else ""
        cover = ", cover" if result.get("cover_copied") else ""
        message = f"sent {kind}{created}{cover}: {item.get('artist', '')} - {item.get('album', '')} -> {copied.get('name', 'phone')}".rstrip(" -")
        self.complete_phone_transfer_dialog("Phone send complete", message, success=True)
        self.update_status(message)

    def save_current_playlist(self):
        if self.active_view != "queue":
            return "break"
        if not self.current_playlist_name:
            return self.save_current_playlist_as()
        self.save_queue_to_stored_playlist(self.current_playlist_name)
        return "break"

    def save_current_playlist_as(self):
        if self.active_view != "queue":
            return "break"
        name = simpledialog.askstring(
            "Save playlist as",
            "Playlist name:",
            initialvalue=self.current_playlist_name or "",
            parent=self.root,
        )
        if not name:
            self.update_status("playlist save cancelled")
            return "break"
        self.save_queue_to_stored_playlist(name)
        return "break"

    def save_queue_to_stored_playlist(self, name):
        mpd_host = require_cfg(self.cfg, "mpd", "host")
        mpd_port = require_cfg(self.cfg, "mpd", "port")
        mpd_password = self.cfg.get("mpd", {}).get("password", "")
        try:
            client = connect_mpd(mpd_host, mpd_port, mpd_password, timeout=30)
            try:
                result = save_current_queue_as_playlist(client, name)
            finally:
                try:
                    client.close()
                    client.disconnect()
                except Exception:
                    pass
        except Exception as e:
            logging.exception("Failed to save current playlist")
            self.update_status(f"playlist save error: {e}")
            return
        if result.get("ok"):
            self.current_playlist_name = result["name"]
            self.update_status(f"saved playlist: {self.current_playlist_name}")
        else:
            self.update_status(f"playlist save error: {result.get('error', 'unknown')}")

    def append_current_song_to_sick_tunes(self):
        mpd_host = require_cfg(self.cfg, "mpd", "host")
        mpd_port = require_cfg(self.cfg, "mpd", "port")
        mpd_password = self.cfg.get("mpd", {}).get("password", "")
        playlist_name = "sick_tunes"
        try:
            client = connect_mpd(mpd_host, mpd_port, mpd_password, timeout=30)
            try:
                result = append_current_song_to_playlist(client, playlist_name)
            finally:
                try:
                    client.close()
                    client.disconnect()
                except Exception:
                    pass
        except Exception as e:
            logging.exception("Failed to append current song to sick_tunes")
            self.update_status(f"sick_tunes error: {e}")
            return "break"
        if result.get("ok"):
            self.update_status(f"added current song to {playlist_name}")
        else:
            self.update_status(f"{playlist_name} error: {result.get('error', 'unknown')}")
        return "break"

    def selected_queue_item(self):
        if self.active_view != "queue" or not self.albums:
            return None
        return self.active_items()[self.selected_index]

    def remove_selected_queue_album(self):
        if self.active_view == "help":
            return "break"
        item = self.selected_queue_item()
        if item is None:
            return "break"
        self.remove_queue_album_from_playlist(item)
        return "break"

    def selected_track(self, view):
        info = self.nowplaying_info if view == "nowplaying" else self.info_info
        if not info or not info["tracks"]:
            return None, None
        idx = min(self.track_selected_indices.get(view, 0), len(info["tracks"]) - 1)
        return idx, info["tracks"][idx]

    def play_selected_nowplaying_track(self):
        _, track = self.selected_track("nowplaying")
        if track is None:
            self.update_status("no track selected")
            return
        pos = track.get("pos")
        if pos is None or pos < 0:
            self.update_status("track has no playlist position")
            return
        self.play_playlist_position(pos, "playing track")

    def play_playlist_position(self, pos, status_prefix):
        mpd_host = require_cfg(self.cfg, "mpd", "host")
        mpd_port = require_cfg(self.cfg, "mpd", "port")
        mpd_password = self.cfg.get("mpd", {}).get("password", "")
        try:
            client = connect_mpd(mpd_host, mpd_port, mpd_password, timeout=30)
            try:
                client.play(str(pos))
            finally:
                try:
                    client.close()
                    client.disconnect()
                except Exception:
                    pass
        except Exception as e:
            logging.exception("Failed to play selected track")
            self.update_status(f"mpd play error: {e}")
            return
        if self.active_view == "nowplaying":
            self.track_selection_manual["nowplaying"] = True
        self.update_status(f"{status_prefix}: {pos}")

    def play_selected_info_track(self):
        selected_idx, selected_track = self.selected_track("info")
        if selected_track is None:
            self.update_status("no track selected")
            return

        info = self.info_info
        tracks = info["tracks"] if info else []
        mpd_host = require_cfg(self.cfg, "mpd", "host")
        mpd_port = require_cfg(self.cfg, "mpd", "port")
        mpd_password = self.cfg.get("mpd", {}).get("password", "")

        try:
            client = connect_mpd(mpd_host, mpd_port, mpd_password, timeout=30)
            try:
                insert_pos = self.insert_position_after_current_album(client)
                play_pos = None
                for idx, track in enumerate(tracks):
                    file_path = track.get("file", "")
                    if not file_path:
                        continue
                    client.addid(file_path, str(insert_pos))
                    if idx == selected_idx:
                        play_pos = insert_pos
                    insert_pos += 1
                if play_pos is None:
                    self.update_status("selected track has no file")
                    return
                client.play(str(play_pos))
            finally:
                try:
                    client.close()
                    client.disconnect()
                except Exception:
                    pass
        except Exception as e:
            logging.exception("Failed to add info album and play selected track")
            self.update_status(f"mpd add error: {e}")
            return

        self.view_loaded["queue"] = False
        self.update_status(f"playing: {info['artist']} - {selected_track['title']}")

    def play_selected_queue_album(self, item):
        mpd_host = require_cfg(self.cfg, "mpd", "host")
        mpd_port = require_cfg(self.cfg, "mpd", "port")
        mpd_password = self.cfg.get("mpd", {}).get("password", "")

        try:
            client = connect_mpd(mpd_host, mpd_port, mpd_password, timeout=30)
            try:
                if play_queue_album(client, item):
                    self.update_status(f"playing: {item['artist']} - {item['album']}")
                    return
            finally:
                try:
                    client.close()
                    client.disconnect()
                except Exception:
                    pass
        except Exception as e:
            logging.exception("Failed to play selected queue album")
            self.update_status(f"mpd play error: {e}")
            return

        self.update_status(f"album not in queue: {item['artist']} - {item['album']}")

    def remove_queue_album_from_playlist(self, item):
        mpd_host = require_cfg(self.cfg, "mpd", "host")
        mpd_port = require_cfg(self.cfg, "mpd", "port")
        mpd_password = self.cfg.get("mpd", {}).get("password", "")

        try:
            client = connect_mpd(mpd_host, mpd_port, mpd_password, timeout=30)
            try:
                result = delete_queue_album_occurrence(client, item)
                if result["stale"]:
                    self.update_status("queue changed; refreshed before delete")
                    self.view_loaded["queue"] = False
                    if self.active_view == "queue":
                        self.refresh()
                    return
                if not result["ok"]:
                    self.update_status(f"album not in queue: {item['artist']} - {item['album']}")
                    return
            finally:
                try:
                    client.close()
                    client.disconnect()
                except Exception:
                    pass
        except Exception as e:
            logging.exception("Failed to remove queue album")
            self.update_status(f"mpd delete error: {e}")
            return

        self.view_loaded["queue"] = False
        if self.active_view == "queue":
            previous_index = self.selected_index
            self.refresh()
            if self.albums:
                self.selected_index = min(previous_index, len(self.albums) - 1)
                self.ensure_visible(self.selected_index)
                self.render_grid()
        self.update_status(f"removed: {item['artist']} - {item['album']} ({result['deleted']} tracks)")

    def add_library_album_after_current_album(self, item, play=True):
        mpd_host = require_cfg(self.cfg, "mpd", "host")
        mpd_port = require_cfg(self.cfg, "mpd", "port")
        mpd_password = self.cfg.get("mpd", {}).get("password", "")

        try:
            client = connect_mpd(mpd_host, mpd_port, mpd_password, timeout=30)
            try:
                songs = self.find_library_album_songs(client, item)
                if not songs:
                    self.update_status(f"no tracks found: {item['artist']} - {item['album']}")
                    return
                insert_pos = self.insert_position_after_current_album(client)
                play_pos = insert_pos
                for song in songs:
                    client.addid(_tag_to_str(song.get("file", "")), str(insert_pos))
                    insert_pos += 1
                if play:
                    client.play(str(play_pos))
            finally:
                try:
                    client.close()
                    client.disconnect()
                except Exception:
                    pass
        except Exception as e:
            logging.exception("Failed to add library album to queue")
            self.update_status(f"mpd add error: {e}")
            return

        self.view_loaded["queue"] = False
        action = "playing added album" if play else "added album after current"
        self.update_status(f"{action}: {item['artist']} - {item['album']} ({len(songs)} tracks)")

    def append_library_album_to_playlist(self, item):
        mpd_host = require_cfg(self.cfg, "mpd", "host")
        mpd_port = require_cfg(self.cfg, "mpd", "port")
        mpd_password = self.cfg.get("mpd", {}).get("password", "")

        try:
            client = connect_mpd(mpd_host, mpd_port, mpd_password, timeout=30)
            try:
                songs = self.find_library_album_songs(client, item)
                if not songs:
                    self.update_status(f"no tracks found: {item['artist']} - {item['album']}")
                    return
                for song in songs:
                    client.addid(_tag_to_str(song.get("file", "")))
            finally:
                try:
                    client.close()
                    client.disconnect()
                except Exception:
                    pass
        except Exception as e:
            logging.exception("Failed to append library album to queue")
            self.update_status(f"mpd append error: {e}")
            return

        self.view_loaded["queue"] = False
        self.update_status(f"appended album: {item['artist']} - {item['album']} ({len(songs)} tracks)")

    def insert_position_after_current_album(self, client):
        song = client.currentsong()
        playlist = client.playlistinfo()
        if not song:
            return len(playlist)

        try:
            current_pos = int(song.get("pos", len(playlist) - 1))
        except (TypeError, ValueError):
            return len(playlist)

        current_key = album_group_key(song)
        if current_key is None:
            return min(current_pos + 1, len(playlist))

        end_pos = current_pos
        for idx in range(current_pos + 1, len(playlist)):
            if album_group_key(playlist[idx]) != current_key:
                break
            end_pos = idx
        return min(end_pos + 1, len(playlist))

    def find_library_album_songs(self, client, item):
        rel_dir = item.get("rel_dir", "")
        selected_key = item.get("key")
        candidates = []
        for base_dir in self.album_search_dirs(rel_dir):
            try:
                candidates = client.find("base", base_dir)
            except Exception:
                candidates = []
            songs = self.filter_album_songs(candidates, selected_key)
            if songs:
                return songs

        try:
            candidates = client.find("album", item["album"])
        except Exception:
            candidates = []
        return self.filter_album_songs(candidates, selected_key)

    def album_search_dirs(self, rel_dir):
        if not rel_dir:
            return []
        dirs = [rel_dir]
        leaf = os.path.basename(rel_dir).casefold()
        if re.fullmatch(r"(cd|disc|disk|vol|volume)\s*[-_. ]*\d+.*", leaf):
            parent = os.path.dirname(rel_dir)
            if parent:
                dirs.insert(0, parent)
        return list(dict.fromkeys(dirs))

    def filter_album_songs(self, songs, selected_key):
        seen = set()
        filtered = []
        for song in songs:
            file_path = _tag_to_str(song.get("file", "")).strip()
            if not file_path or file_path in seen:
                continue
            if selected_key is not None and album_group_key(song) != selected_key:
                continue
            seen.add(file_path)
            filtered.append(song)
        return filtered

    def on_canvas_configure(self, event=None):
        self.update_scrollregion()
        self.render_visible()

    def update_scrollregion(self):
        rows = (len(self.albums) + self.columns - 1) // self.columns
        total_w = max(self.canvas.winfo_width(), self.columns * (self.cell_size + (self.padding * 2)))
        total_h = rows * self.row_height
        self.canvas.configure(scrollregion=(0, 0, total_w, total_h))

    def on_mousewheel(self, event):
        if self.active_view not in self.grid_views:
            return
        if event.num == 4:
            self.canvas.yview_scroll(-3, "units")
        elif event.num == 5:
            self.canvas.yview_scroll(3, "units")
        else:
            self.canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
        self.render_visible()

    def ensure_visible(self, idx):
        if not self.albums or idx >= len(self.albums):
            return
        row = idx // self.columns
        y = row * self.row_height
        h = self.row_height
        rows = (len(self.albums) + self.columns - 1) // self.columns
        total_h = rows * self.row_height
        if total_h <= 0:
            return
        top = self.canvas.canvasy(0)
        bottom = top + self.canvas.winfo_height()
        if y < top:
            self.canvas.yview_moveto(y / total_h)
        elif y + h > bottom:
            self.canvas.yview_moveto((y + h - self.canvas.winfo_height()) / total_h)
        self.render_visible()


def main():
    cfg_path = DEFAULT_CONFIG
    if len(sys.argv) > 1:
        cfg_path = sys.argv[1]

    try:
        cfg = config_to_mapping(load_app_config(cfg_path))
    except Exception as e:
        print(str(e))
        sys.exit(1)

    cache_dir = expand_path(require_cfg(cfg, "cache", "dir"))
    log_path = setup_logging(cache_dir)
    old_excepthook = sys.excepthook

    def excepthook(exc_type, exc_value, exc_traceback):
        log_exception(exc_type, exc_value, exc_traceback)
        old_excepthook(exc_type, exc_value, exc_traceback)

    sys.excepthook = excepthook

    logging.info(
        "Gridmode starting; config=%s cache=%s log=%s",
        cfg_path,
        cache_dir,
        log_path,
    )

    root = tk.Tk()
    root.title("Gridmode")
    root.attributes("-fullscreen", True)

    def report_callback_exception(exc_type, exc_value, exc_traceback):
        log_exception(exc_type, exc_value, exc_traceback)
        old_excepthook(exc_type, exc_value, exc_traceback)

    root.report_callback_exception = report_callback_exception

    app = None
    try:
        app = GridModeApp(root, cfg)
        root.mainloop()
    finally:
        if app is not None:
            try:
                app.stop_key_repeat()
                app.stop_mpd_idle_thread()
            except Exception:
                pass
        logging.info("Gridmode shutdown")


if __name__ == "__main__":
    main()
