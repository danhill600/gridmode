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
from tkinter import messagebox, simpledialog, ttk

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
    delete_phone_album_dir,
    list_phone_album_dirs,
    phone_album_dir_exists,
    PhoneTransferCancelled,
    prepare_album_for_phone,
)
from phone_playlist import DEFAULT_PLAYLIST_NAME, generate_playlist_from_config
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


def items_have_mtimes(items):
    return any(item.get("mtime") is not None for item in items)


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


def phone_index_path(cache_dir):
    return os.path.join(cache_dir, "phone_index.json")


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
        self.help_heading_font = tkfont.Font(font=cfg.get("ui", {}).get("help_heading_font", "Courier 11 bold"))
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
        self.phone_transfer_started_at = None
        self.phone_transfer_done = False
        self.phone_transfer_title = ""
        self.phone_delete_pending = 0
        self.transfer_tab_visible = False
        self.transfer_return_view = "library"
        self.loading_views = set()
        self.loading_after_id = None
        self.loading_frame = 0
        self.loading_text = ""
        self.mousewheel_remainder = 0
        self.help_tab_visible = False
        self.help_return_view = "queue"
        self.nowplaying_rerender_id = None
        self.loading_text_item = None
        self.loading_bg_item = None

        ensure_dir(self.cache_dir)

        self.active_view = "queue"
        self.phone_enabled = bool(cfg.get("phone", {}).get("enabled"))
        self.grid_views = ["queue", "library"] + (["phone"] if self.phone_enabled else [])
        self.marked_grid_items = {view: set() for view in self.grid_views}
        self.view_items = {view: [] for view in self.grid_views}
        self.view_loaded = {view: False for view in self.grid_views}
        self.view_selected_indices = {view: 0 for view in self.grid_views}
        self.view_grid_cache = {}
        self.pending_leader = None
        self.follow_mode = False
        self.follow_last_album_key = None
        self.current_playlist_name = None
        self.library_search_query = ""
        self.library_search_matches = []
        self.info_tab_visible = False
        self.info_return_view = "queue"
        self.hydrate_tab_visible = False
        self.hydrate_return_view = "library"
        self.hydrate_target_view = "library"
        self.tools_tab_visible = False
        self.tools_return_view = "queue"
        self.tools_selected_index = 0
        self.refresh_hydrate_after_load = None
        self.hydrate_process = None
        self.hydrate_tail_after_id = None
        self.hydrate_log_path = os.path.join(self.cache_dir, "hydrate.log")
        self.hydrate_log_position = self.hydrate_log_size()
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
        self.hydrate_tab = ttk.Frame(self.tabs)
        self.transfers_tab = ttk.Frame(self.tabs)
        self.tools_tab = ttk.Frame(self.tabs)
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

        self.hydrate_frame = ttk.Frame(self.content, padding=12)
        self.hydrate_text = tk.Text(
            self.hydrate_frame,
            wrap="none",
            bg=PANEL_BG,
            fg=APP_FG,
            insertbackground=APP_FG,
            relief="flat",
            padx=12,
            pady=12,
            font=self.help_font,
        )
        self.hydrate_text.pack(fill="both", expand=True)
        self.hydrate_text.configure(state="disabled")

        self.tools_frame = ttk.Frame(self.content, padding=12)
        self.tools_text = tk.Text(
            self.tools_frame,
            wrap="none",
            bg=PANEL_BG,
            fg=APP_FG,
            insertbackground=APP_FG,
            relief="flat",
            padx=12,
            pady=12,
            font=self.help_font,
        )
        self.tools_text.tag_configure("section", foreground=APP_ACCENT, font=self.help_heading_font)
        self.tools_text.tag_configure("selected", background="#12323a", foreground="#fdf6e3")
        self.tools_text.tag_configure("disabled", foreground="#586e75")
        self.tools_text.pack(fill="both", expand=True)
        self.tools_text.configure(state="disabled")

        self.transfers_frame = ttk.Frame(self.content, padding=12)
        self.transfers_text = tk.Text(
            self.transfers_frame,
            wrap="word",
            bg=PANEL_BG,
            fg=APP_FG,
            insertbackground=APP_FG,
            relief="flat",
            padx=12,
            pady=12,
            font=self.help_font,
        )
        self.transfers_text.pack(fill="both", expand=True)
        self.transfers_text.configure(state="disabled")

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
        self.bind_mousewheel(self.canvas)
        self.bind_mousewheel(self.track_text)
        self.bind_mousewheel(self.bio_text)
        self.bind_mousewheel(self.help_text)
        self.bind_mousewheel(self.hydrate_text)
        self.bind_mousewheel(self.tools_text)
        self.bind_mousewheel(self.transfers_text)

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
        self.root.bind_all("H", self.global_key(lambda e: self.switch_relative_tab(-1)))
        self.root.bind_all("L", self.global_key(lambda e: self.switch_relative_tab(1)))
        self.root.bind_all("g", self.global_key(lambda e: self.handle_g_key()))
        self.root.bind_all("G", self.global_key(lambda e: self.jump_grid_position("bottom")))
        self.root.bind_all("M", self.global_key(lambda e: self.jump_grid_position("middle")))
        self.root.bind_all("z", self.global_key(lambda e: self.jump_grid_position("random")))
        self.root.bind_all("s", self.global_key(lambda e: self.save_current_playlist()))
        self.root.bind_all("S", self.global_key(lambda e: self.save_current_playlist_as()))
        self.root.bind_all("t", self.global_key(lambda e: self.append_current_song_to_sick_tunes()))
        self.root.bind_all(":", self.global_key(lambda e: self.command_prompt()))
        self.root.bind_all("c", self.global_key(lambda e: self.cancel_phone_transfer_from_key()))
        self.root.bind_all("m", self.global_key(lambda e: self.toggle_phone_mark()))
        self.root.bind_all("/", self.global_key(lambda e: self.begin_library_search()))
        self.root.bind_all("n", self.global_key(lambda e: self.next_library_search_match(1)))
        self.root.bind_all("N", self.global_key(lambda e: self.next_library_search_match(-1)))
        self.root.bind_all("<Return>", self.global_key(lambda e: self.on_select()))
        self.root.bind_all("<Tab>", self.global_key(lambda e: self.toggle_text_pane_focus()))
        self.root.bind_all("<ISO_Left_Tab>", self.global_key(lambda e: self.toggle_text_pane_focus()))
        self.root.bind("1", lambda e: self.switch_tab(0))
        self.root.bind("2", lambda e: self.switch_tab(1))
        self.root.bind("3", lambda e: self.switch_tab(2))
        self.root.bind("4", lambda e: self.switch_tab(3))
        self.root.bind("5", lambda e: self.switch_tab(4))
        self.root.bind("6", lambda e: self.switch_tab(5))
        self.root.bind_all("a", self.global_key(lambda e: self.add_selected_library_album_after_current()))
        self.root.bind_all("A", self.global_key(lambda e: self.append_selected_library_album()))
        self.root.bind_all("d", self.global_key(lambda e: self.remove_selected_queue_album()))
        self.root.bind_all("p", self.global_key(lambda e: self.send_selected_album_to_phone()))
        self.root.bind_all("i", self.global_key(lambda e: self.toggle_info_tab()))
        self.root.bind_all("o", self.global_key(lambda e: self.jump_to_current_album()))
        self.root.bind_all("?", self.global_key(lambda e: self.show_key_help()))
        self.root.bind_all("f", self.global_key(lambda e: self.toggle_follow_mode()))
        self.root.bind("<Escape>", lambda e: self.set_fullscreen(False))
        self.root.bind("r", lambda e: self.refresh_from_key())
        self.root.bind_all("q", self.global_key(lambda e: self.close_info_or_quit()))
        self.root.bind_all("0", self.global_key(lambda e: self.jump_grid_row_edge("start")))
        self.root.bind_all("e", self.global_key(lambda e: self.jump_grid_row_edge("end")))
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
            "small": self.scaled_nowplaying_profile(base, cover=0.82, title=0.58, text=0.76, bio=0.76),
            "medium": self.scaled_nowplaying_profile(base, cover=1.50, title=0.64, text=0.82, bio=0.82),
            "large": base,
        }

    def scaled_nowplaying_profile(self, base, cover, title, text, bio):
        return {
            "cover": max(280, min(720, round(base["cover"] * cover))),
            "title_size": max(16, round(base["title_size"] * title)),
            "text_size": max(12, round(base["text_size"] * text)),
            "bio_size": max(12, round(base["bio_size"] * bio)),
        }

    def on_root_configure(self, event=None):
        if event is not None and event.widget is not self.root:
            return
        self.apply_nowplaying_profile_for_size(
            self.root.winfo_width(),
            self.root.winfo_height(),
            self.screen_dpi(),
            self.screen_width_inches(),
        )

    def screen_dpi(self):
        try:
            width_px = self.root.winfo_screenwidth()
            width_mm = self.root.winfo_screenmmwidth()
        except tk.TclError:
            return 96
        if width_px <= 0 or width_mm <= 0:
            return 96
        return width_px / (width_mm / 25.4)

    def screen_width_inches(self):
        try:
            width_mm = self.root.winfo_screenmmwidth()
        except tk.TclError:
            return 0
        return width_mm / 25.4 if width_mm > 0 else 0

    def nowplaying_profile_for_size(self, width, height, dpi=96, screen_width_inches=0):
        physically_large = screen_width_inches >= 34 and dpi < 95
        dense_desktop = dpi >= 110
        if width < 1200 or height < 720:
            return "small"
        if dense_desktop:
            return "medium"
        if physically_large and width >= 1700 and height >= 950:
            return "large"
        if width < 2400 or height < 1300:
            return "medium"
        return "large"

    def apply_nowplaying_profile_for_size(self, width, height, dpi=96, screen_width_inches=0):
        if width <= 1 or height <= 1:
            return
        profile_name = self.nowplaying_profile_for_size(width, height, dpi, screen_width_inches)
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
        return self.nowplaying_title_font.metrics("linespace") * 5

    def global_key(self, callback):
        def wrapped(event):
            if self.popup_has_focus(event):
                return None
            return callback(event)

        return wrapped

    def popup_has_focus(self, event=None):
        widget = getattr(event, "widget", None)
        if widget is None:
            try:
                widget = self.root.focus_get()
            except tk.TclError:
                return False
        if widget is None:
            return False
        try:
            return widget.winfo_toplevel() is not self.root
        except tk.TclError:
            return False

    def bind_repeating_key(self, key, action, intercept_widgets=()):
        self.root.bind_all(
            f"<KeyPress-{key}>",
            self.global_key(lambda event, key=key, action=action: self.start_key_repeat(key, action)),
        )
        self.root.bind_all(
            f"<KeyRelease-{key}>",
            self.global_key(lambda event, key=key: self.schedule_key_repeat_stop(key)),
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

    def bind_mousewheel(self, widget, bind_all=False):
        bind = widget.bind_all if bind_all else widget.bind
        bind("<MouseWheel>", self.on_mousewheel)
        bind("<Button-4>", self.on_mousewheel)
        bind("<Button-5>", self.on_mousewheel)

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
        if self.follow_mode and ({"player", "playlist"} & events):
            self.follow_current_playback()
        elif self.active_view == "nowplaying" and ({"player", "playlist"} & events):
            self.refresh_nowplaying(force_current_track=self.follow_mode)

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
            self.start_phone_load()
            return
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
        self.refresh_hydrate_after_load = self.active_view if self.active_view in self.grid_views else None
        self.refresh(rebuild=True)
        if self.active_view == "queue":
            self.maybe_offer_hydrate_after_refresh("queue")
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
        for item in self.load_phone_index():
            self.add_phone_lookup_item(lookup, item, override=True)
        for view in ("queue", "library"):
            for item in self.view_items.get(view, []):
                self.add_phone_lookup_item(lookup, item, override=False)
        for item in self.load_library_index():
            self.add_phone_lookup_item(lookup, item, override=False)
        return lookup

    def add_phone_lookup_item(self, lookup, item, override=False):
        keys = []
        phone_rel_dir = item.get("phone_rel_dir") or item.get("phone_name")
        if phone_rel_dir:
            keys.extend([phone_rel_dir, os.path.basename(phone_rel_dir)])
        rel_dir = item.get("rel_dir", "")
        if rel_dir:
            keys.extend([rel_dir, os.path.basename(rel_dir)])
        for key in keys:
            if not key:
                continue
            if override:
                lookup[key] = item
            else:
                lookup.setdefault(key, item)
            normalized = phone_folder_match_key(key)
            if normalized:
                if override:
                    lookup[normalized] = item
                else:
                    lookup.setdefault(normalized, item)

    def load_phone_index(self):
        path = phone_index_path(self.cache_dir)
        if not os.path.exists(path):
            return []
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            logging.exception("Failed to load phone index")
            return []
        items = data.get("albums", [])
        if not isinstance(items, list):
            return []
        out = []
        for item in items:
            if not isinstance(item, dict):
                continue
            album_item = deserialize_library_item(item)
            album_item["phone_rel_dir"] = str(item.get("phone_rel_dir", "") or "")
            album_item["phone_name"] = str(item.get("phone_name", "") or os.path.basename(album_item["phone_rel_dir"]))
            out.append(album_item)
        return out

    def save_phone_index(self, items):
        path = phone_index_path(self.cache_dir)
        tmp_path = path + ".tmp"
        payload = {
            "version": 1,
            "generated_at": time.time(),
            "albums": [self.serialize_phone_index_item(item) for item in items],
        }
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2, sort_keys=True)
                f.write("\n")
            os.replace(tmp_path, path)
        except OSError:
            logging.exception("Failed to save phone index")

    def serialize_phone_index_item(self, item):
        out = serialize_library_item(item)
        out["phone_rel_dir"] = item.get("phone_rel_dir", "")
        out["phone_name"] = item.get("phone_name", "")
        return out

    def remember_phone_index_item(self, item, copied):
        phone_name = str((copied or {}).get("name") or item.get("phone_name") or "")
        phone_rel_dir = str((copied or {}).get("rel_dir") or phone_name)
        if not phone_rel_dir:
            return
        remembered = dict(item)
        remembered["phone_rel_dir"] = phone_rel_dir
        remembered["phone_name"] = phone_name or os.path.basename(phone_rel_dir)
        existing = [
            old for old in self.load_phone_index()
            if (old.get("phone_rel_dir") or old.get("phone_name")) != phone_rel_dir
        ]
        existing.append(remembered)
        self.save_phone_index(existing)

    def forget_phone_index_items(self, rel_dirs):
        rel_dir_set = {str(rel_dir or "") for rel_dir in rel_dirs if rel_dir}
        if not rel_dir_set:
            return
        items = [
            item for item in self.load_phone_index()
            if (item.get("phone_rel_dir") or item.get("phone_name")) not in rel_dir_set
        ]
        self.save_phone_index(items)

    def load_library_items(self, rebuild=False):
        cached_items = self.load_library_index()
        if not rebuild:
            if cached_items:
                music_root = self.cfg.get("music", {}).get("root", "")
                if not music_root or items_have_mtimes(cached_items):
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
        if tab_text == "Hydrate":
            self.active_view = "hydrate"
            self.show_hydrate_panel()
            self.update_status()
            self.start_hydrate_tail()
            return
        if tab_text == "Tools":
            self.active_view = "tools"
            self.show_tools_panel()
            self.render_tools()
            self.update_status()
            return
        if tab_text == "Transfers":
            self.active_view = "transfers"
            self.show_transfers_panel()
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
            elif self.active_view == "phone":
                self.start_phone_load()
            else:
                self.refresh()
            return
        self.prepare_active_grid()
        self.reset_images()
        self.render_grid()

    def show_grid_panel(self):
        self.help_frame.pack_forget()
        self.nowplaying_frame.pack_forget()
        self.hydrate_frame.pack_forget()
        self.tools_frame.pack_forget()
        self.transfers_frame.pack_forget()
        if not self.canvas.winfo_ismapped():
            self.canvas.pack(side="left", fill="both", expand=True)

    def show_nowplaying_panel(self):
        self.help_frame.pack_forget()
        self.hydrate_frame.pack_forget()
        self.tools_frame.pack_forget()
        self.transfers_frame.pack_forget()
        self.canvas.pack_forget()
        if not self.nowplaying_frame.winfo_ismapped():
            self.nowplaying_frame.pack(side="top", fill="both", expand=True)

    def show_help_panel(self):
        self.canvas.pack_forget()
        self.nowplaying_frame.pack_forget()
        self.hydrate_frame.pack_forget()
        self.tools_frame.pack_forget()
        self.transfers_frame.pack_forget()
        if not self.help_frame.winfo_ismapped():
            self.help_frame.pack(side="top", fill="both", expand=True)

    def show_hydrate_panel(self):
        self.canvas.pack_forget()
        self.nowplaying_frame.pack_forget()
        self.help_frame.pack_forget()
        self.tools_frame.pack_forget()
        self.transfers_frame.pack_forget()
        if not self.hydrate_frame.winfo_ismapped():
            self.hydrate_frame.pack(side="top", fill="both", expand=True)

    def show_tools_panel(self):
        self.canvas.pack_forget()
        self.nowplaying_frame.pack_forget()
        self.help_frame.pack_forget()
        self.hydrate_frame.pack_forget()
        self.transfers_frame.pack_forget()
        if not self.tools_frame.winfo_ismapped():
            self.tools_frame.pack(side="top", fill="both", expand=True)

    def show_transfers_panel(self):
        self.canvas.pack_forget()
        self.nowplaying_frame.pack_forget()
        self.help_frame.pack_forget()
        self.hydrate_frame.pack_forget()
        self.tools_frame.pack_forget()
        if not self.transfers_frame.winfo_ismapped():
            self.transfers_frame.pack(side="top", fill="both", expand=True)

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
        labels = {
            "queue": "Queue",
            "library": "Library",
            "phone": "Phone",
            "nowplaying": "Now Playing",
            "info": "Info",
            "hydrate": "Hydrate",
            "tools": "Tools",
            "transfers": "Transfers",
        }
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

    def start_phone_load(self):
        if "phone" in self.loading_views:
            self.render_loading("ur phobe is lobing sweetpal")
            return
        self.loading_views.add("phone")
        self.view_grid_cache.pop("phone", None)
        self.view_loaded["phone"] = False
        self.begin_loading_screen()
        self.root.update_idletasks()
        self.render_loading("ur phobe is lobing sweetpal")
        self.start_loading_animation("ur phobe is lobing sweetpal")
        self.root.update_idletasks()
        self.root.after(80, self.start_phone_load_worker)

    def start_phone_load_worker(self):
        if "phone" not in self.loading_views:
            return
        thread = threading.Thread(target=self.phone_load_worker, daemon=True)
        thread.start()
        self.root.after(100, self.poll_load_results)

    def phone_load_worker(self):
        started_at = time.monotonic()
        try:
            items = self.load_phone_items()
            items_loaded_at = time.monotonic()
            albums = [(item["artist"], item["album"]) for item in items]
            album_keys = [item["key"] for item in items]
            cover_paths, covers_ok, covers_failed = self.cover_paths_for(albums, album_keys)
            covers_loaded_at = time.monotonic()
            logging.info(
                "Phone load timings: items=%.3fs covers=%.3fs total=%.3fs",
                items_loaded_at - started_at,
                covers_loaded_at - items_loaded_at,
                covers_loaded_at - started_at,
            )
            self.load_result_queue.put(
                {
                    "view": "phone_load",
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
            logging.exception("Failed to load phone in background")
            self.load_result_queue.put({"view": "phone_load", "error": e})

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
            elif result.get("view") == "phone_load":
                self.finish_phone_load(result)
            elif result.get("view") == "phone_transfer_progress":
                self.update_phone_transfer_status(result.get("phase", "Sending to phone..."))
            elif result.get("view") == "phone_transfer":
                self.finish_phone_transfer(result)
            elif result.get("view") == "phone_delete":
                self.finish_phone_delete(result)

        if self.phone_transfer_pending and self.active_view == "transfers":
            self.update_status()
        if self.loading_views or self.phone_transfer_pending or self.phone_delete_pending:
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
        self.maybe_offer_hydrate_after_refresh("library")
        logging.info(
            "Loaded %d library albums; covers cached=%d missing=%d",
            len(self.albums),
            self.covers_ok,
            self.covers_failed,
        )

    def finish_phone_load(self, result):
        self.loading_views.discard("phone")
        self.stop_loading_animation()
        error = result.get("error")
        if error is not None:
            self.update_status(f"phone load error: {error}")
            if self.active_view == "phone":
                self.render_loading("Phone load failed")
            return

        self.view_items["phone"] = result["items"]
        self.view_loaded["phone"] = True
        self.prune_phone_marks()
        self.view_grid_cache["phone"] = {
            "albums": result["albums"],
            "album_keys": result["album_keys"],
            "cover_paths": result["cover_paths"],
            "covers_ok": result["covers_ok"],
            "covers_failed": result["covers_failed"],
        }
        if self.active_view != "phone":
            return
        self.clear_loading_items()
        self.apply_grid_cache("phone")
        self.reset_images()
        self.render_grid()
        self.maybe_offer_hydrate_after_refresh("phone")
        logging.info(
            "Loaded %d phone albums; covers cached=%d missing=%d",
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
        self.canvas.yview_moveto(0)

    def animate_loading(self):
        if self.active_view in self.loading_views:
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
        top = self.canvas.canvasy(0)
        bottom = top + height
        if self.loading_bg_item is None:
            self.loading_bg_item = self.canvas.create_rectangle(
                0,
                top,
                width,
                bottom,
                fill=PANEL_BG,
                outline=PANEL_BG,
                tags=("loading",),
            )
            self.canvas.tag_lower(self.loading_bg_item)
        else:
            self.canvas.coords(self.loading_bg_item, 0, top, width, bottom)

        spinner = "|/-\\"[self.loading_frame % 4]
        if self.loading_text_item is None:
            self.loading_text_item = self.canvas.create_text(
                width // 2,
                top + height // 2,
                text=f"{text} {spinner}",
                fill=APP_FG,
                font=self.font,
                anchor="center",
                tags=("loading",),
            )
        else:
            self.canvas.coords(self.loading_text_item, width // 2, top + height // 2)
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
            if self.grid_item_marked(self.active_view, self.active_items()[idx]):
                self.render_phone_mark(x0, y0, x1)

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

    def render_phone_mark(self, x0, y0, x1):
        radius = 10
        cx = x1 - radius - 8
        cy = y0 + radius + 8
        self.canvas.create_oval(
            cx - radius,
            cy - radius,
            cx + radius,
            cy + radius,
            fill="#7fff00",
            outline="#d7ff9f",
            width=2,
        )
        self.canvas.create_text(
            cx,
            cy,
            text="v",
            fill="#003b00",
            font=self.font,
            anchor="center",
        )

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
        if self.active_view == "hydrate":
            running = self.hydrate_process is not None and self.hydrate_process.poll() is None
            msg = "Hydrate"
            if note:
                msg += f" | {note}"
            msg += " | running" if running else " | idle"
            msg += " | q=close hydrate"
            self.status.configure(text=msg)
            return
        if self.active_view == "tools":
            items = self.tools_items()
            item = items[self.tools_selected_index] if 0 <= self.tools_selected_index < len(items) else {}
            msg = f"Tools: {item.get('label', 'select an action')}"
            if item.get("type") == "disabled":
                msg += " [planned]"
            if note:
                msg += f" | {note}"
            msg += " | j/k move, J/K section, Enter run, q close"
            self.status.configure(text=msg)
            return
        if self.active_view == "transfers":
            msg = "Transfers"
            if note:
                msg += f" | {note}"
            if self.phone_transfer_pending and self.phone_transfer_started_at is not None:
                elapsed = int(time.monotonic() - self.phone_transfer_started_at)
                msg += f" | running {elapsed}s"
            elif self.phone_delete_pending:
                msg += " | deleting"
            else:
                msg += " | idle"
            msg += " | c=cancel active transfer, q=close transfers"
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
        if self.active_view in self.grid_views and self.marked_grid_items.get(self.active_view):
            msg += f" | marked {len(self.marked_items_for_view(self.active_view))}"
        if self.follow_mode:
            msg += " | follow"
        if note:
            msg += f" | {note}"
        msg += " | 1-6 tabs, Enter=add/select, m=mark, d=delete/remove, p=send to phone, t=sick tunes, :=command, o=now playing, f=follow, Esc=exit fullscreen, r=refresh tab, q=quit"
        self.status.configure(text=msg)

    def set_fullscreen(self, enabled):
        self.fullscreen = bool(enabled)
        self.root.attributes("-fullscreen", self.fullscreen)
        self.root.after(0, self.on_canvas_configure)
        self.root.after(0, self.ensure_visible, self.selected_index)

    def toggle_fullscreen(self):
        self.set_fullscreen(not self.fullscreen)

    def toggle_follow_mode(self):
        self.follow_mode = not self.follow_mode
        if self.follow_mode:
            self.follow_last_album_key = None
            self.follow_current_playback()
            self.update_status("follow mode on")
        else:
            self.follow_last_album_key = None
            self.update_status("follow mode off")
        return "break"

    def close_info_or_quit(self):
        if self.active_view == "help":
            self.close_key_help()
            return "break"
        if self.active_view == "hydrate":
            self.close_hydrate_tab()
            return "break"
        if self.active_view == "tools":
            self.close_tools_tab()
            return "break"
        if self.active_view == "transfers":
            self.close_transfers_tab()
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

    def show_hydrate_tab(self):
        self.hydrate_return_view = self.active_view if self.active_view in (*self.grid_views, "nowplaying", "info") else "library"
        if not self.hydrate_tab_visible:
            self.tabs.add(self.hydrate_tab, text="Hydrate")
            self.hydrate_tab_visible = True
        self.tabs.select(self.hydrate_tab)
        return "break"

    def toggle_tools_tab(self):
        if self.active_view == "tools":
            return self.close_tools_tab()
        return self.show_tools_tab()

    def show_tools_tab(self):
        self.tools_return_view = self.active_view if self.active_view in (*self.grid_views, "nowplaying", "info") else "queue"
        if not self.tools_tab_visible:
            self.tabs.add(self.tools_tab, text="Tools")
            self.tools_tab_visible = True
        self.tabs.select(self.tools_tab)
        return "break"

    def show_transfers_tab(self):
        self.transfer_return_view = self.active_view if self.active_view in (*self.grid_views, "nowplaying", "info", "hydrate") else "library"
        if not self.transfer_tab_visible:
            self.tabs.add(self.transfers_tab, text="Transfers")
            self.transfer_tab_visible = True
        self.tabs.select(self.transfers_tab)
        return "break"

    def close_transfers_tab(self):
        return_view = self.transfer_return_view if self.transfer_return_view in (*self.grid_views, "nowplaying", "info", "hydrate") else "library"
        if self.transfer_tab_visible:
            self.tabs.hide(self.transfers_tab)
            self.transfer_tab_visible = False
        self.select_view_tab(return_view)
        return "break"

    def close_hydrate_tab(self):
        return_view = self.hydrate_return_view if self.hydrate_return_view in (*self.grid_views, "nowplaying", "info") else "library"
        if self.hydrate_tab_visible:
            self.tabs.hide(self.hydrate_tab)
            self.hydrate_tab_visible = False
        self.select_view_tab(return_view)
        return "break"

    def close_tools_tab(self):
        return_view = self.tools_return_view if self.tools_return_view in (*self.grid_views, "nowplaying", "info") else "queue"
        if self.tools_tab_visible:
            self.tabs.hide(self.tools_tab)
            self.tools_tab_visible = False
        self.select_view_tab(return_view)
        return "break"

    def command_prompt(self):
        if self.search_entry.winfo_ismapped():
            return "break"
        command = simpledialog.askstring(
            "Gridmode command",
            "Command:",
            parent=self.root,
        )
        if command is None:
            self.update_status("command cancelled")
            return "break"
        return self.run_command(command)

    def run_command(self, command):
        name = re.sub(r"[\s_-]+", "-", str(command or "").strip().casefold())
        aliases = {
            "tool": "tools",
            "settings": "tools",
            "maintenance": "tools",
            "r": "refresh",
            "cover": "hydrate",
            "covers": "hydrate",
            "hydrate-covers": "hydrate",
            "u": "hydrate",
            "follow-mode": "follow",
            "fs": "fullscreen",
            "full-screen": "fullscreen",
            "append-album": "append",
            "append-selected": "append",
            "sick": "sick-tunes",
            "sick-tune": "sick-tunes",
            "sicktunes": "sick-tunes",
            "sick-tunes": "sick-tunes",
        }
        name = aliases.get(name, name)
        if name == "tools":
            return self.show_tools_tab()
        if name == "refresh":
            return self.refresh_from_key()
        if name == "hydrate":
            return self.confirm_hydrate_covers()
        if name == "follow":
            return self.toggle_follow_mode()
        if name == "fullscreen":
            self.toggle_fullscreen()
            return "break"
        if name == "sick-tunes":
            return self.append_current_song_to_sick_tunes()
        if name == "append":
            return self.append_selected_library_album()
        if not name:
            self.update_status("empty command")
        else:
            self.update_status(f"unknown command: {command}")
        return "break"

    def tools_items(self):
        items = [
            {"type": "section", "label": "Covers"},
            {
                "type": "action",
                "id": "update_return_tab",
                "label": "Refresh previous tab",
                "detail": "Run the normal r flow for the tab you opened Tools from.",
            },
            {
                "type": "action",
                "id": "hydrate_return_tab",
                "label": "Hydrate missing covers in previous tab",
                "detail": "Run the targeted cover hydrate without first refreshing the tab.",
            },
            {
                "type": "disabled",
                "label": "Deep cover repair: retry known failures",
                "detail": "Planned maintenance flow; global, slower, failure-aware.",
            },
            {
                "type": "disabled",
                "label": "Clear cover failure cache",
                "detail": "Planned explicit destructive maintenance action.",
            },
            {"type": "section", "label": "Library"},
            {
                "type": "action",
                "id": "rebuild_library",
                "label": "Rebuild library index",
                "detail": "Reload MPD library and rewrite library_index.json.",
            },
            {
                "type": "disabled",
                "label": "Recompute library mtimes",
                "detail": "Planned diagnostic/maintenance action.",
            },
            {"type": "section", "label": "Phone"},
            {
                "type": "action",
                "id": "refresh_phone",
                "label": "Refresh phone albums",
                "detail": "Relist phone folders and reconcile local identities.",
                "enabled": self.phone_enabled,
            },
            {
                "type": "disabled",
                "label": "Rebuild phone identity index",
                "detail": "Planned repair for phone_index.json.",
            },
            {
                "type": "disabled",
                "label": "Clear phone identity index",
                "detail": "Planned explicit destructive maintenance action.",
            },
            {
                "type": "disabled",
                "label": "Test phone SSH",
                "detail": "Planned connectivity diagnostic.",
            },
            {"type": "section", "label": "Logs"},
            {
                "type": "action",
                "id": "open_hydrate_log",
                "label": "Open Hydrate log",
                "detail": "Show the Hydrate tab and tail hydrate.log.",
            },
        ]
        return items

    def selectable_tools_indices(self):
        return [
            idx for idx, item in enumerate(self.tools_items())
            if item.get("type") == "action" and item.get("enabled", True)
        ]

    def render_tools(self):
        items = self.tools_items()
        selectable = self.selectable_tools_indices()
        if selectable:
            if self.tools_selected_index not in selectable:
                self.tools_selected_index = selectable[0]
        else:
            self.tools_selected_index = 0

        self.tools_text.configure(state="normal")
        self.tools_text.delete("1.0", "end")
        self.tools_text.insert("end", "Tools\n\n", ("section",))
        self.tools_text.insert("end", "j/k move, J/K jump section, Enter run, q close\n\n")
        for idx, item in enumerate(items):
            line_start = self.tools_text.index("end")
            if item["type"] == "section":
                self.tools_text.insert("end", f"{item['label']}\n", ("section",))
                continue
            marker = "›" if idx == self.tools_selected_index else " "
            disabled = item["type"] != "action" or not item.get("enabled", True)
            suffix = " [planned]" if disabled else ""
            tags = ("disabled",) if disabled else ()
            self.tools_text.insert("end", f"{marker} {item['label']}{suffix}\n", tags)
            if idx == self.tools_selected_index:
                self.tools_text.tag_add("selected", line_start, f"{line_start} lineend")
            detail = item.get("detail", "")
            if detail:
                self.tools_text.insert("end", f"    {detail}\n", ("disabled",))
            self.tools_text.insert("end", "\n")
        self.tools_text.configure(state="disabled")
        self.tools_text.see(f"{self.tools_line_for_index(self.tools_selected_index)}.0")

    def tools_line_for_index(self, target_index):
        line = 5
        for idx, item in enumerate(self.tools_items()):
            if idx == target_index:
                return line
            if item["type"] == "section":
                line += 1
            else:
                line += 3 if item.get("detail") else 2
        return line

    def move_tools_selection(self, direction):
        selectable = self.selectable_tools_indices()
        if not selectable:
            return
        try:
            pos = selectable.index(self.tools_selected_index)
        except ValueError:
            pos = 0
        pos = max(0, min(len(selectable) - 1, pos + direction))
        self.tools_selected_index = selectable[pos]
        self.render_tools()
        self.update_status()

    def move_tools_section(self, direction):
        items = self.tools_items()
        selectable = self.selectable_tools_indices()
        if not selectable:
            return
        current = self.tools_selected_index
        section_starts = [
            idx for idx, item in enumerate(items)
            if item["type"] == "section"
        ]
        current_section = 0
        for idx, section_idx in enumerate(section_starts):
            if section_idx < current:
                current_section = idx
        next_section = max(0, min(len(section_starts) - 1, current_section + direction))
        start = section_starts[next_section]
        candidates = [idx for idx in selectable if idx > start]
        if candidates:
            self.tools_selected_index = candidates[0]
        self.render_tools()
        self.update_status()

    def run_selected_tool(self):
        items = self.tools_items()
        if self.tools_selected_index < 0 or self.tools_selected_index >= len(items):
            return "break"
        item = items[self.tools_selected_index]
        if item.get("type") != "action" or not item.get("enabled", True):
            self.update_status("tool is planned, not wired yet")
            return "break"
        action = item.get("id")
        if action == "update_return_tab":
            view = self.tools_return_view if self.tools_return_view in (*self.grid_views, "nowplaying", "info") else "queue"
            self.close_tools_tab()
            self.select_view_tab(view)
            return self.refresh_from_key()
        if action == "hydrate_return_tab":
            view = self.tools_return_view if self.tools_return_view in self.grid_views else "queue"
            self.close_tools_tab()
            self.select_view_tab(view)
            return self.confirm_hydrate_covers()
        if action == "rebuild_library":
            self.close_tools_tab()
            self.select_view_tab("library")
            self.start_library_load(rebuild=True)
            return "break"
        if action == "refresh_phone":
            if not self.phone_enabled:
                self.update_status("phone is not enabled")
                return "break"
            self.close_tools_tab()
            self.select_view_tab("phone")
            self.start_phone_load()
            return "break"
        if action == "open_hydrate_log":
            self.show_hydrate_tab()
            self.start_hydrate_tail()
            return "break"
        self.update_status(f"unknown tool: {action}")
        return "break"

    def confirm_hydrate_covers(self):
        if self.active_view == "help":
            return "break"
        if self.hydrate_process is not None and self.hydrate_process.poll() is None:
            self.show_hydrate_tab()
            self.update_status("hydrate already running")
            return "break"
        return self.prompt_hydrate_active_view()

    def maybe_offer_hydrate_after_refresh(self, view):
        if self.refresh_hydrate_after_load != view:
            return "break"
        self.refresh_hydrate_after_load = None
        if self.active_view != view:
            return "break"
        if self.hydrate_process is not None and self.hydrate_process.poll() is None:
            self.update_status(f"{view} refreshed; hydrate already running")
            return "break"
        return self.prompt_hydrate_active_view(
            prompt_prefix=f"{view.capitalize()} refreshed. "
        )

    def prompt_hydrate_active_view(self, prompt_prefix=""):
        records_path, missing_count = self.write_active_missing_hydrate_records()
        if missing_count <= 0:
            self.update_status(f"{self.active_view}: no missing covers to hydrate")
            return "break"
        view_label = self.active_view.capitalize()
        hydrate_count = missing_count if missing_count <= 100 else f"up to 100 of {missing_count}"
        ok = messagebox.askyesno(
            "Hydrate covers",
            f"{prompt_prefix}Hydrate {hydrate_count} missing {view_label} cover{'s' if missing_count != 1 else ''} now?\n\n"
            "Progress will open in the Hydrate tab.",
            parent=self.root,
        )
        if not ok:
            self.update_status("hydrate cancelled")
            return "break"
        return self.start_hydrate_covers(records_path=records_path, retry_failures=True)

    def write_active_missing_hydrate_records(self):
        if self.active_view not in self.grid_views:
            return "", 0
        items = self.active_items()
        albums = [(item["artist"], item["album"]) for item in items]
        album_keys = [item["key"] for item in items]
        cover_paths, _, _ = self.cover_paths_for(albums, album_keys)
        records = []
        seen = set()
        for item, cover_path in zip(items, cover_paths):
            if cover_path:
                continue
            artist = str(item.get("artist", "")).strip()
            album = str(item.get("album", "")).strip()
            key = item.get("key")
            if not artist or not album or key is None:
                continue
            record_key = tuple(str(part) for part in key)
            if record_key in seen:
                continue
            seen.add(record_key)
            records.append(
                {
                    "artist": artist,
                    "album": album,
                    "key": list(record_key),
                    "rel_dir": item.get("rel_dir", ""),
                }
            )

        path = os.path.join(self.cache_dir, "hydrate_targets.json")
        payload = {
            "version": 1,
            "generated_at": time.time(),
            "view": self.active_view,
            "albums": records,
        }
        tmp_path = path + ".tmp"
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2, sort_keys=True)
                f.write("\n")
            os.replace(tmp_path, path)
        except OSError:
            logging.exception("Failed to save hydrate targets")
            return "", 0
        return path, len(records)

    def start_hydrate_covers(self, records_path="", retry_failures=False):
        self.show_hydrate_tab()
        self.hydrate_target_view = self.hydrate_return_view
        self.set_hydrate_text("")
        self.hydrate_log_position = self.hydrate_log_size()
        cmd = [sys.executable, "hydrate_covers.py"]
        if records_path:
            cmd.extend(["--records-file", records_path])
        else:
            cmd.append("--library-index")
        if retry_failures:
            cmd.append("--retry-failures")
        cmd.extend(["--limit", "100"])
        try:
            self.hydrate_process = subprocess.Popen(
                cmd,
                cwd=os.path.dirname(os.path.abspath(__file__)),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.STDOUT,
            )
        except OSError as e:
            self.append_hydrate_text(f"hydrate start error: {e}\n")
            self.update_status(f"hydrate start error: {e}")
            return "break"
        self.append_hydrate_text("started: " + " ".join(cmd) + "\n")
        self.start_hydrate_tail()
        self.update_status("hydrate started")
        return "break"

    def hydrate_log_size(self):
        try:
            return os.path.getsize(self.hydrate_log_path)
        except OSError:
            return 0

    def set_hydrate_text(self, text):
        self.hydrate_text.configure(state="normal")
        self.hydrate_text.delete("1.0", "end")
        if text:
            self.hydrate_text.insert("end", text)
        self.hydrate_text.configure(state="disabled")

    def append_hydrate_text(self, text):
        if not text:
            return
        self.hydrate_text.configure(state="normal")
        self.hydrate_text.insert("end", text)
        self.hydrate_text.see("end")
        self.hydrate_text.configure(state="disabled")

    def start_hydrate_tail(self):
        if self.hydrate_tail_after_id is None:
            self.tail_hydrate_log()

    def stop_hydrate_tail(self):
        if self.hydrate_tail_after_id is not None:
            self.root.after_cancel(self.hydrate_tail_after_id)
            self.hydrate_tail_after_id = None

    def tail_hydrate_log(self):
        try:
            with open(self.hydrate_log_path, "r", encoding="utf-8") as f:
                f.seek(self.hydrate_log_position)
                chunk = f.read()
                self.hydrate_log_position = f.tell()
        except OSError:
            chunk = ""
        if chunk:
            self.append_hydrate_text(chunk)

        running = self.hydrate_process is not None and self.hydrate_process.poll() is None
        if self.hydrate_process is not None and not running:
            code = self.hydrate_process.returncode
            self.append_hydrate_text(f"hydrate exited: {code}\n")
            self.hydrate_process = None
            target_view = self.hydrate_target_view if self.hydrate_target_view in self.grid_views else "library"
            self.view_grid_cache.pop(target_view, None)
            if self.active_view == target_view:
                self.prepare_active_grid(use_cache=False)
                self.reset_images()
                self.render_grid()
            else:
                self.update_status("hydrate finished")

        if running or self.active_view == "hydrate":
            self.hydrate_tail_after_id = self.root.after(500, self.tail_hydrate_log)
        else:
            self.hydrate_tail_after_id = None

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

    def jump_grid_row_edge(self, edge):
        self.pending_leader = None
        if self.active_view not in self.grid_views or not self.albums:
            return "break"
        cols = max(self.columns, 1)
        row_start = (self.selected_index // cols) * cols
        if edge == "start":
            idx = row_start
        elif edge == "end":
            idx = min(row_start + cols - 1, len(self.albums) - 1)
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
                "  3-6 visible tabs",
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
                "  0          first item in current row",
                "  e          last item in current row",
                "  z          jump to random album",
                "  gg         jump to top",
                "  G          jump to bottom",
                "  M          jump to middle",
                "  mouse      scroll and click to select",
                "",
                "Queue",
                "  Enter      play selected album",
                "  m          mark selected album for phone send",
                "  d          remove selected album from current playlist",
                "  p          send marked albums, or selected album if none marked",
                "  i          open selected album info",
                "  o          jump selection to currently playing album",
                "  s          save current playlist",
                "  S          save current playlist as",
                "  r          refresh queue and offer missing cover hydrate",
                "",
                "Library",
                "  /          search artist or album",
                "  n / N      next / previous search match",
                "  m          mark selected album for phone send",
                "  Enter      insert selected album after current album and play it",
                "  a          insert selected album after current album",
                "  A          append selected album to end of playlist",
                "  i          open selected album info",
                "  o          jump selection to currently playing album",
                "  r          rebuild library and offer missing cover hydrate",
                "",
                "Phone",
                "  m          mark selected album for phone delete",
                "  d          delete selected album from phone",
                "  i          open selected album info",
                "  r          refresh phone albums and offer missing cover hydrate",
                "",
                "Tools",
                "  :tools     open Tools",
                "  j / k      move between actions",
                "  J / K      jump between sections",
                "  Enter      run selected action",
                "  q          close Tools",
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
                "  p          send selected album to phone and show Transfers",
                "  c          cancel active transfer from Transfers",
                "  t          add current song to sick_tunes",
                "  f          toggle follow mode",
                "  :          command prompt; try tools, refresh, hydrate, follow, fullscreen, sick-tunes",
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
            return
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
        if self.active_view == "tools":
            self.move_tools_selection(direction)
            return
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
        if self.active_view == "tools":
            self.move_tools_section(direction)
            return
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
        if self.active_view not in self.grid_views:
            return "break"
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
        song = self.load_current_song_for_status()
        if not song:
            return
        self.select_current_album_in_grid(song, update_last_follow=False)

    def load_current_song_for_status(self):
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
            return None
        return song

    def select_current_album_in_grid(self, song, update_last_follow=True):
        artist = _tag_to_str(song.get("artist", "")).strip()
        album = _tag_to_str(song.get("album", "")).strip()
        if not artist or not album:
            self.update_status("now playing has no album")
            return False

        current_key = album_group_key(song)
        if update_last_follow and current_key is not None and current_key == self.follow_last_album_key:
            return False
        for idx, grid_key in enumerate(self.album_keys):
            if grid_key == current_key:
                if idx != self.selected_index:
                    self.selected_index = idx
                    self.update_selection()
                else:
                    self.ensure_visible(idx)
                if update_last_follow:
                    self.follow_last_album_key = current_key
                self.update_status(f"now playing: {artist} - {album}")
                return True

        self.update_status(f"now playing not in grid: {artist} - {album}")
        if update_last_follow:
            self.follow_last_album_key = current_key
        return False

    def follow_current_playback(self):
        if self.active_view == "nowplaying":
            self.refresh_nowplaying(force_current_track=True)
            return
        if self.active_view == "info":
            self.refresh_info_to_nowplaying()
            return
        if self.active_view not in ("queue", "library"):
            return
        if self.active_view == "queue" and not self.view_loaded.get("queue"):
            self.refresh()
        song = self.load_current_song_for_status()
        if not song:
            return
        self.select_current_album_in_grid(song, update_last_follow=True)

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

    def refresh_nowplaying(self, force_current_track=False):
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
        if force_current_track:
            self.track_selected_indices["nowplaying"] = self.current_track_index(info)
            self.track_selection_manual["nowplaying"] = False
        elif previous and previous.get("key") == info.get("key"):
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

    def refresh_info_to_nowplaying(self):
        try:
            info = self.load_nowplaying_info()
        except Exception as e:
            logging.exception("Failed to load now playing info for follow mode")
            self.update_nowplaying_empty(f"mpd error: {e}")
            return
        if not info:
            self.update_nowplaying_empty("nothing playing")
            return
        if self.follow_last_album_key is not None and info.get("key") == self.follow_last_album_key:
            self.track_selected_indices["info"] = self.current_track_index(info)
            self.track_selection_manual["info"] = False
            self.info_info = info
            self.render_nowplaying(info)
            self.update_status(f"{info['artist']} - {info['album']}")
            return
        self.follow_last_album_key = info.get("key")
        self.info_album = {
            "artist": info.get("artist", ""),
            "album": info.get("album", ""),
            "key": info.get("key"),
            "rel_dir": info.get("rel_dir", ""),
        }
        self.info_info = info
        self.track_selected_indices["info"] = self.current_track_index(info)
        self.track_selection_manual["info"] = False
        self.text_pane_focus["info"] = "tracks"
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
        if self.active_view not in ("queue", "library", "phone") or not self.albums:
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
            if self.info_return_view in ("library", "phone"):
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
        if Image is not None:
            with Image.open(path) as image:
                image = image.convert("RGB")
                image = image.resize((self.nowplaying_cover_size, self.nowplaying_cover_size))
                ppm = f"P6 {image.width} {image.height} 255\n".encode("ascii") + image.tobytes()
            return tk.PhotoImage(data=ppm, format="PPM")
        if self.convert_available:
            proc = subprocess.run(
                ["convert", path, "-resize", f"{self.nowplaying_cover_size}x{self.nowplaying_cover_size}", "ppm:-"],
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
            if proc.returncode == 0 and proc.stdout:
                return tk.PhotoImage(data=proc.stdout, format="PPM")
        return tk.PhotoImage(file=path)

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
        if self.active_view == "tools":
            return self.run_selected_tool()
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

    def phone_transfer_item_title(self, item):
        artist = item.get("artist", "")
        album = item.get("album", "")
        return f"{artist} - {album}".strip(" -") or item.get("rel_dir", "selected album")

    def transfer_timestamp(self):
        return time.strftime("%H:%M:%S")

    def append_transfers_text(self, text):
        if not text:
            return
        self.transfers_text.configure(state="normal")
        self.transfers_text.insert("end", text)
        self.transfers_text.see("end")
        self.transfers_text.configure(state="disabled")

    def append_transfer_line(self, text):
        self.append_transfers_text(f"[{self.transfer_timestamp()}] {text}\n")

    def start_phone_transfer_log(self, item):
        title = self.phone_transfer_item_title(item)
        self.phone_transfer_title = title
        self.phone_transfer_done = False
        self.phone_transfer_started_at = time.monotonic()
        self.show_transfers_tab()
        self.append_transfer_line(f"phone send started: {title}")

    def update_phone_transfer_status(self, phase):
        self.append_transfer_line(phase)
        self.update_status(phase.lower())

    def complete_phone_transfer_log(self, phase, detail=None):
        self.phone_transfer_done = True
        elapsed = ""
        if self.phone_transfer_started_at is not None:
            elapsed = f" ({int(time.monotonic() - self.phone_transfer_started_at)}s)"
        self.append_transfer_line(f"{phase}{elapsed}")
        if detail:
            self.append_transfer_line(detail)
        if phase == "Phone send complete" and "phone" in self.grid_views:
            self.append_transfer_line("press 3 for Phone tab")

    def cancel_phone_transfer(self):
        if self.phone_transfer_done:
            return
        if self.phone_transfer_cancel_event is not None:
            self.phone_transfer_cancel_event.set()
        self.update_phone_transfer_status("Cancelling phone send...")

    def cancel_phone_transfer_from_key(self):
        if self.active_view != "transfers":
            return None
        if not self.phone_transfer_pending:
            self.update_status("no active transfer")
            return "break"
        self.cancel_phone_transfer()
        return "break"

    def send_selected_album_to_phone(self):
        if self.active_view == "help":
            return "break"
        if self.phone_transfer_pending:
            self.update_status("phone send already in progress")
            self.show_transfers_tab()
            return "break"
        marked_items = self.marked_phone_send_items()
        if marked_items:
            return self.start_phone_transfer_many(marked_items)
        item = self.selected_album_for_phone()
        if item is None:
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
        self.start_phone_transfer_log(item)
        self.update_status(f"sending to phone: {item.get('artist', '')} - {item.get('album', '')}".rstrip(" -"))
        thread = threading.Thread(target=self.phone_transfer_worker, args=(item, self.phone_transfer_cancel_event), daemon=True)
        thread.start()
        self.root.after(100, self.poll_load_results)
        return "break"

    def start_phone_transfer_many(self, items):
        phone_cfg = self.cfg.get("phone", {})
        if not phone_cfg.get("enabled"):
            self.update_status("phone is not enabled")
            return "break"
        if not phone_cfg.get("ssh_host") or not phone_cfg.get("music_root"):
            self.update_status("phone config missing ssh_host or music_root")
            return "break"
        self.phone_transfer_pending += 1
        self.phone_transfer_cancel_event = threading.Event()
        self.phone_transfer_done = False
        self.phone_transfer_started_at = time.monotonic()
        self.show_transfers_tab()
        self.append_transfer_line(f"phone bulk send started: {len(items)} albums")
        self.update_status(f"sending {len(items)} marked albums to phone")
        thread = threading.Thread(target=self.phone_transfer_many_worker, args=(items, self.active_view, self.phone_transfer_cancel_event), daemon=True)
        thread.start()
        self.root.after(100, self.poll_load_results)
        return "break"

    def phone_transfer_worker(self, item, cancel_event):
        def progress(phase):
            self.load_result_queue.put({"view": "phone_transfer_progress", "phase": phase})

        try:
            prepared, copied, cover_copied = self.send_album_to_phone_for_worker(item, cancel_event, progress)
            playlist_result = None
            playlist_error = None
            if not copied.get("already_exists"):
                try:
                    playlist_result = self.update_phone_playlist_for_worker(progress)
                except Exception as e:
                    logging.exception("Failed to update phone playlist")
                    playlist_error = e
            self.load_result_queue.put(
                {
                    "view": "phone_transfer",
                    "item": item,
                    "prepared": prepared,
                    "copied": copied,
                    "cover_copied": cover_copied,
                    "playlist": playlist_result,
                    "playlist_error": playlist_error,
                    "error": None,
                }
            )
        except PhoneTransferCancelled as e:
            self.load_result_queue.put({"view": "phone_transfer", "item": item, "cancelled": True, "error": e})
        except Exception as e:
            logging.exception("Failed to send album to phone")
            self.load_result_queue.put({"view": "phone_transfer", "item": item, "error": e})

    def phone_transfer_many_worker(self, items, source_view, cancel_event):
        sent_items = []
        errors = []
        playlist_result = None
        playlist_error = None

        def progress(phase):
            self.load_result_queue.put({"view": "phone_transfer_progress", "phase": phase})

        try:
            for idx, item in enumerate(items, start=1):
                progress(f"[{idx}/{len(items)}] {self.phone_transfer_item_title(item)}")
                prepared, copied, cover_copied = self.send_album_to_phone_for_worker(item, cancel_event, progress)
                sent_items.append({"item": item, "prepared": prepared, "copied": copied, "cover_copied": cover_copied})
            playlist_result = None
            playlist_error = None
            if any(not (sent.get("copied") or {}).get("already_exists") for sent in sent_items):
                try:
                    playlist_result = self.update_phone_playlist_for_worker(progress)
                except Exception as e:
                    logging.exception("Failed to update phone playlist")
                    playlist_error = e
        except PhoneTransferCancelled as e:
            self.load_result_queue.put(
                {
                    "view": "phone_transfer",
                    "bulk": True,
                    "source_view": source_view,
                    "sent_items": sent_items,
                    "errors": errors,
                    "playlist": None,
                    "playlist_error": None,
                    "cancelled": True,
                    "error": e,
                }
            )
            return
        except Exception as e:
            logging.exception("Failed to send marked albums to phone")
            errors.append({"item": item if "item" in locals() else {}, "error": e})

        self.load_result_queue.put(
            {
                "view": "phone_transfer",
                "bulk": True,
                "source_view": source_view,
                "sent_items": sent_items,
                "errors": errors,
                "playlist": playlist_result,
                "playlist_error": playlist_error,
                "error": errors[0]["error"] if errors else None,
            }
        )

    def update_phone_playlist_for_worker(self, progress):
        progress("Updating phone playlist...")
        result = generate_playlist_from_config(
            self.cfg,
            playlist_name=DEFAULT_PLAYLIST_NAME,
            local_output=os.path.join(self.cache_dir, DEFAULT_PLAYLIST_NAME),
            copy_to_phone=True,
        )
        progress(f"Updated {result['playlist']}: {result['tracks']} tracks")
        return result

    def send_album_to_phone_for_worker(self, item, cancel_event, progress):
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
        phone_leaf = prepared["path"].rstrip("/").rsplit("/", 1)[-1]
        if phone_album_dir_exists(
            music_cfg.get("ssh_host", ""),
            phone_cfg.get("ssh_host", ""),
            phone_cfg.get("music_root", ""),
            phone_leaf,
            cancel_event=cancel_event,
        ):
            progress("it's already on the phone, man")
            return prepared, {"name": phone_leaf, "already_exists": True}, False
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
        return prepared, copied, cover_copied

    def finish_phone_transfer(self, result):
        self.phone_transfer_pending = max(0, self.phone_transfer_pending - 1)
        self.phone_transfer_cancel_event = None
        if result.get("bulk"):
            return self.finish_phone_transfer_many(result)
        error = result.get("error")
        item = result.get("item") or {}
        if result.get("cancelled"):
            self.complete_phone_transfer_log("Phone send cancelled")
            self.update_status("phone send cancelled")
            return
        if error is not None:
            message = f"phone send error: {error}"
            self.complete_phone_transfer_log("Phone send failed", message)
            self.update_status(message)
            return
        prepared = result.get("prepared") or {}
        copied = result.get("copied") or {}
        if copied.get("name"):
            self.remember_phone_index_item(item, copied)
        if copied.get("already_exists"):
            message = "it's already on the phone, man"
            self.complete_phone_transfer_log(message, self.phone_transfer_item_title(item))
            self.update_status(message)
            return
        playlist = result.get("playlist") or {}
        playlist_error = result.get("playlist_error")
        if playlist:
            self.append_transfer_line(f"updated {playlist.get('playlist')}: {playlist.get('tracks')} tracks")
        if playlist_error is not None:
            self.append_transfer_line(f"phone playlist error: {playlist_error}")
        self.view_loaded["phone"] = False
        self.view_grid_cache.pop("phone", None)
        kind = prepared.get("kind", "album")
        created = ", transcoded" if prepared.get("created") else ""
        matched = ", matched existing" if prepared.get("matched") else ""
        cover = ", cover" if result.get("cover_copied") else ""
        message = f"sent {kind}{matched}{created}{cover}: {item.get('artist', '')} - {item.get('album', '')} -> {copied.get('name', 'phone')}".rstrip(" -")
        self.complete_phone_transfer_log("Phone send complete", message)
        self.update_status(message)

    def finish_phone_transfer_many(self, result):
        self.phone_transfer_done = True
        source_view = result.get("source_view")
        sent_items = result.get("sent_items") or []
        errors = result.get("errors") or []
        if source_view in self.marked_grid_items:
            for sent in sent_items:
                key = self.grid_item_mark_key(source_view, sent.get("item") or {})
                if key:
                    self.marked_grid_items[source_view].discard(key)
        for sent in sent_items:
            item = sent.get("item") or {}
            copied = sent.get("copied") or {}
            if copied.get("name"):
                self.remember_phone_index_item(item, copied)
            if copied.get("already_exists"):
                self.append_transfer_line(f"it's already on the phone, man: {self.phone_transfer_item_title(item)}")
            else:
                self.append_transfer_line(f"sent to phone: {self.phone_transfer_item_title(item)} -> {copied.get('name', 'phone')}")
        for error in errors:
            item = error.get("item") or {}
            self.append_transfer_line(f"phone send error: {self.phone_transfer_item_title(item)}: {error.get('error')}")
        playlist = result.get("playlist") or {}
        playlist_error = result.get("playlist_error")
        if playlist:
            self.append_transfer_line(f"updated {playlist.get('playlist')}: {playlist.get('tracks')} tracks")
        if playlist_error is not None:
            self.append_transfer_line(f"phone playlist error: {playlist_error}")
        self.view_loaded["phone"] = False
        self.view_grid_cache.pop("phone", None)
        if result.get("cancelled"):
            self.update_status(f"phone send cancelled after {len(sent_items)} albums")
        elif errors:
            self.update_status(f"sent {len(sent_items)} marked; {len(errors)} failed")
        else:
            sent_count = sum(1 for sent in sent_items if not (sent.get("copied") or {}).get("already_exists"))
            skipped_count = len(sent_items) - sent_count
            if sent_count and skipped_count:
                self.update_status(f"sent {sent_count}; {skipped_count} already on phone")
            elif skipped_count:
                self.update_status("it's already on the phone, man")
            else:
                self.update_status(f"sent {sent_count} marked albums")
        if self.active_view == source_view:
            self.render_visible()

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

    def selected_phone_item(self):
        if self.active_view != "phone" or not self.albums:
            return None
        return dict(self.active_items()[self.selected_index])

    def grid_item_mark_key(self, view, item):
        if view == "phone":
            return item.get("phone_rel_dir") or item.get("rel_dir") or item.get("phone_name") or ""
        if item.get("rel_dir"):
            return item.get("rel_dir")
        if item.get("positions"):
            return "positions:" + ",".join(str(pos) for pos in item.get("positions", []))
        return repr(item.get("key") or (item.get("artist", ""), item.get("album", "")))

    def grid_item_marked(self, view, item):
        key = self.grid_item_mark_key(view, item)
        return bool(key and key in self.marked_grid_items.get(view, set()))

    def grid_item_display_name(self, item):
        return item.get("phone_name") or f"{item.get('artist', '')} - {item.get('album', '')}".strip(" -") or self.grid_item_mark_key(self.active_view, item)

    def toggle_phone_mark(self):
        if self.active_view not in self.grid_views:
            return None
        if not self.albums:
            return "break"
        item = self.active_items()[self.selected_index]
        key = self.grid_item_mark_key(self.active_view, item)
        if not key:
            self.update_status("selected album cannot be marked")
            return "break"
        marked = self.marked_grid_items.setdefault(self.active_view, set())
        name = self.grid_item_display_name(item)
        if key in marked:
            marked.remove(key)
            self.update_status(f"unmarked: {name}")
        else:
            marked.add(key)
            self.update_status(f"marked: {name}")
        self.render_visible()
        return "break"

    def marked_items_for_view(self, view):
        marked = self.marked_grid_items.get(view, set())
        if not marked:
            return []
        items = []
        seen = set()
        for item in self.view_items.get(view, []):
            key = self.grid_item_mark_key(view, item)
            if key and key in marked and key not in seen:
                marked_item = dict(item)
                if view == "phone":
                    marked_item["phone_rel_dir"] = key
                    marked_item["phone_name"] = marked_item.get("phone_name") or os.path.basename(key)
                items.append(marked_item)
                seen.add(key)
        return items

    def marked_phone_items(self):
        return self.marked_items_for_view("phone") if self.active_view == "phone" else []

    def marked_phone_send_items(self):
        if self.active_view not in ("queue", "library"):
            return []
        return [item for item in self.marked_items_for_view(self.active_view) if item.get("rel_dir")]

    def prune_phone_marks(self):
        if "phone" not in self.view_items:
            return
        live = {self.grid_item_mark_key("phone", item) for item in self.view_items.get("phone", [])}
        live.discard("")
        self.marked_grid_items.setdefault("phone", set()).intersection_update(live)

    def remove_selected_queue_album(self):
        if self.active_view == "help":
            return "break"
        if self.active_view == "phone":
            return self.confirm_delete_selected_phone_album()
        item = self.selected_queue_item()
        if item is None:
            return "break"
        self.remove_queue_album_from_playlist(item)
        return "break"

    def confirm_delete_selected_phone_album(self):
        if self.phone_delete_pending:
            self.show_transfers_tab()
            self.update_status("phone delete already in progress")
            return "break"
        marked_items = self.marked_phone_items()
        if marked_items:
            count = len(marked_items)
            sample = "\n".join(item.get("phone_name") or self.grid_item_mark_key("phone", item) for item in marked_items[:8])
            if count > 8:
                sample += f"\n...and {count - 8} more"
            ok = messagebox.askyesno(
                "Delete marked from phone",
                f"Delete {count} marked album{'s' if count != 1 else ''} from the phone?\n\n{sample}",
                parent=self.root,
            )
            if not ok:
                self.update_status("phone delete cancelled")
                return "break"
            self.start_phone_delete_many(marked_items)
            return "break"

        item = self.selected_phone_item()
        if item is None:
            return "break"
        phone_rel_dir = item.get("phone_rel_dir") or item.get("rel_dir")
        phone_name = item.get("phone_name") or os.path.basename(str(phone_rel_dir or ""))
        if not phone_rel_dir:
            self.update_status("selected phone album has no directory")
            return "break"
        ok = messagebox.askyesno(
            "Delete from phone",
            f"Delete this album from the phone?\n\n{phone_name}\n\nDirectory:\n{phone_rel_dir}",
            parent=self.root,
        )
        if not ok:
            self.update_status("phone delete cancelled")
            return "break"
        self.start_phone_delete(item, phone_rel_dir, phone_name)
        return "break"

    def start_phone_delete_many(self, items):
        phone_cfg = self.cfg.get("phone", {})
        music_cfg = self.cfg.get("music", {})
        if not phone_cfg.get("ssh_host") or not phone_cfg.get("music_root"):
            self.update_status("phone config missing ssh_host or music_root")
            return
        self.phone_delete_pending += 1
        self.show_transfers_tab()
        self.append_transfer_line(f"phone bulk delete started: {len(items)} albums")
        self.update_status(f"deleting {len(items)} marked albums from phone")
        thread = threading.Thread(
            target=self.phone_delete_many_worker,
            args=(items, music_cfg, phone_cfg),
            daemon=True,
        )
        thread.start()
        self.root.after(100, self.poll_load_results)

    def start_phone_delete(self, item, phone_rel_dir, phone_name):
        phone_cfg = self.cfg.get("phone", {})
        music_cfg = self.cfg.get("music", {})
        if not phone_cfg.get("ssh_host") or not phone_cfg.get("music_root"):
            self.update_status("phone config missing ssh_host or music_root")
            return
        self.phone_delete_pending += 1
        self.show_transfers_tab()
        self.append_transfer_line(f"phone delete started: {phone_name}")
        self.append_transfer_line(f"target: {phone_rel_dir}")
        self.update_status(f"deleting from phone: {phone_name}")
        thread = threading.Thread(
            target=self.phone_delete_worker,
            args=(dict(item), phone_rel_dir, phone_name, music_cfg, phone_cfg),
            daemon=True,
        )
        thread.start()
        self.root.after(100, self.poll_load_results)

    def phone_delete_worker(self, item, phone_rel_dir, phone_name, music_cfg, phone_cfg):
        try:
            deleted = delete_phone_album_dir(
                music_cfg.get("ssh_host", ""),
                phone_cfg.get("ssh_host", ""),
                phone_cfg.get("music_root", ""),
                phone_rel_dir,
            )
            playlist_result = None
            playlist_error = None
            try:
                playlist_result = self.update_phone_playlist_for_worker(
                    lambda phase: self.load_result_queue.put({"view": "phone_transfer_progress", "phase": phase})
                )
            except Exception as e:
                logging.exception("Failed to update phone playlist")
                playlist_error = e
            self.load_result_queue.put(
                {
                    "view": "phone_delete",
                    "item": item,
                    "name": phone_name,
                    "rel_dir": phone_rel_dir,
                    "deleted": deleted,
                    "playlist": playlist_result,
                    "playlist_error": playlist_error,
                    "error": None,
                }
            )
        except Exception as e:
            logging.exception("Failed to delete phone album")
            self.load_result_queue.put(
                {
                    "view": "phone_delete",
                    "item": item,
                    "name": phone_name,
                    "rel_dir": phone_rel_dir,
                    "error": e,
                }
            )

    def phone_delete_many_worker(self, items, music_cfg, phone_cfg):
        deleted_items = []
        errors = []
        playlist_result = None
        playlist_error = None
        for item in items:
            phone_rel_dir = item.get("phone_rel_dir") or item.get("rel_dir")
            phone_name = item.get("phone_name") or os.path.basename(str(phone_rel_dir or ""))
            try:
                deleted = delete_phone_album_dir(
                    music_cfg.get("ssh_host", ""),
                    phone_cfg.get("ssh_host", ""),
                    phone_cfg.get("music_root", ""),
                    phone_rel_dir,
                )
                deleted_items.append({"item": item, "name": phone_name, "rel_dir": phone_rel_dir, "deleted": deleted})
            except Exception as e:
                logging.exception("Failed to delete marked phone album")
                errors.append({"item": item, "name": phone_name, "rel_dir": phone_rel_dir, "error": e})
        if deleted_items:
            try:
                playlist_result = self.update_phone_playlist_for_worker(
                    lambda phase: self.load_result_queue.put({"view": "phone_transfer_progress", "phase": phase})
                )
            except Exception as e:
                logging.exception("Failed to update phone playlist")
                playlist_error = e
        self.load_result_queue.put(
            {
                "view": "phone_delete",
                "bulk": True,
                "deleted_items": deleted_items,
                "errors": errors,
                "playlist": playlist_result,
                "playlist_error": playlist_error,
                "error": errors[0]["error"] if errors else None,
            }
        )

    def finish_phone_delete(self, result):
        self.phone_delete_pending = max(0, self.phone_delete_pending - 1)
        if result.get("bulk"):
            return self.finish_phone_delete_many(result)
        name = result.get("name") or result.get("rel_dir") or "phone album"
        error = result.get("error")
        if error is not None:
            message = f"phone delete error: {error}"
            self.append_transfer_line(message)
            self.update_status(message)
            return

        deleted = result.get("deleted") or {}
        deleted_name = deleted.get("name") or name
        rel_dir = result.get("rel_dir") or deleted.get("rel_dir")
        if rel_dir:
            self.marked_grid_items.setdefault("phone", set()).discard(rel_dir)
            self.forget_phone_index_items([rel_dir])
        playlist = result.get("playlist") or {}
        playlist_error = result.get("playlist_error")
        if playlist:
            self.append_transfer_line(f"updated {playlist.get('playlist')}: {playlist.get('tracks')} tracks")
        if playlist_error is not None:
            self.append_transfer_line(f"phone playlist error: {playlist_error}")
        self.view_loaded["phone"] = False
        self.view_grid_cache.pop("phone", None)
        if self.active_view == "phone":
            previous_index = self.selected_index
            self.refresh()
            if self.albums:
                self.selected_index = min(previous_index, len(self.albums) - 1)
                self.ensure_visible(self.selected_index)
                self.render_grid()
        message = f"deleted from phone: {deleted_name}"
        self.append_transfer_line(message)
        self.update_status(message)

    def finish_phone_delete_many(self, result):
        deleted_items = result.get("deleted_items") or []
        errors = result.get("errors") or []
        for deleted in deleted_items:
            key = deleted.get("rel_dir")
            if key:
                self.marked_grid_items.setdefault("phone", set()).discard(key)
        self.forget_phone_index_items(deleted.get("rel_dir") for deleted in deleted_items)
        for deleted in deleted_items:
            key = deleted.get("rel_dir")
            name = (deleted.get("deleted") or {}).get("name") or deleted.get("name") or key
            self.append_transfer_line(f"deleted from phone: {name}")
        for error in errors:
            name = error.get("name") or error.get("rel_dir") or "phone album"
            self.append_transfer_line(f"phone delete error: {name}: {error.get('error')}")
        playlist = result.get("playlist") or {}
        playlist_error = result.get("playlist_error")
        if playlist:
            self.append_transfer_line(f"updated {playlist.get('playlist')}: {playlist.get('tracks')} tracks")
        if playlist_error is not None:
            self.append_transfer_line(f"phone playlist error: {playlist_error}")

        self.view_loaded["phone"] = False
        self.view_grid_cache.pop("phone", None)
        if self.active_view == "phone":
            previous_index = self.selected_index
            self.refresh()
            if self.albums:
                self.selected_index = min(previous_index, len(self.albums) - 1)
                self.ensure_visible(self.selected_index)
                self.render_grid()
        if errors:
            self.update_status(f"deleted {len(deleted_items)} marked; {len(errors)} failed")
        else:
            self.update_status(f"deleted {len(deleted_items)} marked albums")

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
        units = self.mousewheel_units(event)
        if units == 0:
            return "break"
        if self.active_view in ("nowplaying", "info"):
            widget = self.bio_text if self.text_pane_focus.get(self.active_view) == "bio" else self.track_text
            widget.yview_scroll(units, "units")
            return "break"
        if self.active_view == "help":
            self.help_text.yview_scroll(units, "units")
            return "break"
        if self.active_view == "hydrate":
            self.hydrate_text.yview_scroll(units, "units")
            return "break"
        if self.active_view == "transfers":
            self.transfers_text.yview_scroll(units, "units")
            return "break"
        if self.active_view not in self.grid_views:
            return "break"
        if abs(units) == 1:
            self.canvas.yview_scroll(units, "pages")
        else:
            self.canvas.yview_scroll(units, "units")
        self.render_visible()
        return "break"

    def mousewheel_units(self, event):
        if event.num == 4:
            return -3
        if event.num == 5:
            return 3
        delta = getattr(event, "delta", 0)
        if not delta:
            return 0
        if abs(delta) < 120:
            self.mousewheel_remainder = 0
            return -1 if delta > 0 else 1
        self.mousewheel_remainder += delta
        steps = int(self.mousewheel_remainder / 120)
        self.mousewheel_remainder -= steps * 120
        return -steps * 3

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
                app.stop_hydrate_tail()
            except Exception:
                pass
        logging.info("Gridmode shutdown")


if __name__ == "__main__":
    main()
