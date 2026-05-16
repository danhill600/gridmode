from dataclasses import dataclass

from gridmode_cache import expand_path, load_config


@dataclass(frozen=True)
class MpdConfig:
    host: str
    port: int
    password: str = ""


@dataclass(frozen=True)
class LastfmConfig:
    api_key: str = ""
    api_secret: str = ""


@dataclass(frozen=True)
class CacheConfig:
    dir: str


@dataclass(frozen=True)
class MusicConfig:
    root: str = ""
    ssh_host: str = ""
    lossy_root: str = ""
    prefer_lossy_for_phone: bool = True
    transcode_missing_lossy: bool = False


@dataclass(frozen=True)
class PhoneConfig:
    enabled: bool = False
    ssh_host: str = ""
    music_root: str = ""


@dataclass(frozen=True)
class UiConfig:
    columns: int
    cell_size: int
    padding: int
    font: str
    top_gap: int = 4
    title_gap: int = 4
    row_gap: int = 8
    max_image_cache: int = 600
    nowplaying_cover_size: int = 420
    nowplaying_title_font: str = "Courier 30 bold"
    nowplaying_text_font: str = "Courier 17"
    nowplaying_current_font: str = "Courier 17 bold"
    nowplaying_bio_font: str = "Courier 17"
    help_font: str = "Courier 11"
    loading_font: str = "Courier 18 bold"


@dataclass(frozen=True)
class AppConfig:
    mpd: MpdConfig
    lastfm: LastfmConfig
    cache: CacheConfig
    music: MusicConfig
    phone: PhoneConfig
    ui: UiConfig


def load_app_config(path):
    return parse_app_config(load_config(path))


def parse_app_config(raw):
    mpd = raw.get("mpd", {})
    cache = raw.get("cache", {})
    ui = raw.get("ui", {})
    lastfm = raw.get("lastfm", {})
    music = raw.get("music", {})
    phone = raw.get("phone", {})

    errors = []
    host = _string(mpd.get("host"))
    if not host:
        errors.append("mpd.host is required")
    port = _int(mpd.get("port"), "mpd.port", errors, min_value=1, max_value=65535)
    cache_dir = _string(cache.get("dir"))
    if not cache_dir:
        errors.append("cache.dir is required")

    columns = _int(ui.get("columns"), "ui.columns", errors, min_value=1)
    cell_size = _int(ui.get("cell_size"), "ui.cell_size", errors, min_value=1)
    padding = _int(ui.get("padding"), "ui.padding", errors, min_value=0)
    font = _string(ui.get("font"))
    if not font:
        errors.append("ui.font is required")

    if errors:
        raise ValueError("Invalid config:\n- " + "\n- ".join(errors))

    return AppConfig(
        mpd=MpdConfig(host=host, port=port, password=_string(mpd.get("password"))),
        lastfm=LastfmConfig(
            api_key=_string(lastfm.get("api_key")),
            api_secret=_string(lastfm.get("api_secret")),
        ),
        cache=CacheConfig(dir=expand_path(cache_dir)),
        music=MusicConfig(
            root=_string(music.get("root")),
            ssh_host=_string(music.get("ssh_host")),
            lossy_root=_string(music.get("lossy_root")),
            prefer_lossy_for_phone=_bool(music.get("prefer_lossy_for_phone"), True),
            transcode_missing_lossy=_bool(music.get("transcode_missing_lossy"), False),
        ),
        phone=PhoneConfig(
            enabled=_bool(phone.get("enabled"), False),
            ssh_host=_string(phone.get("ssh_host")),
            music_root=_string(phone.get("music_root")),
        ),
        ui=UiConfig(
            columns=columns,
            cell_size=cell_size,
            padding=padding,
            font=font,
            top_gap=_optional_int(ui, "top_gap", 4, min_value=0),
            title_gap=_optional_int(ui, "title_gap", 4, min_value=0),
            row_gap=_optional_int(ui, "row_gap", 8, min_value=0),
            max_image_cache=_optional_int(ui, "max_image_cache", 600, min_value=1),
            nowplaying_cover_size=_optional_int(ui, "nowplaying_cover_size", 420, min_value=1),
            nowplaying_title_font=_string(ui.get("nowplaying_title_font"), "Courier 30 bold"),
            nowplaying_text_font=_string(ui.get("nowplaying_text_font"), "Courier 17"),
            nowplaying_current_font=_string(ui.get("nowplaying_current_font"), "Courier 17 bold"),
            nowplaying_bio_font=_string(ui.get("nowplaying_bio_font"), "Courier 17"),
            help_font=_string(ui.get("help_font"), "Courier 11"),
            loading_font=_string(ui.get("loading_font"), "Courier 18 bold"),
        ),
    )


def config_to_mapping(config):
    return {
        "mpd": {
            "host": config.mpd.host,
            "port": config.mpd.port,
            "password": config.mpd.password,
        },
        "lastfm": {
            "api_key": config.lastfm.api_key,
            "api_secret": config.lastfm.api_secret,
        },
        "cache": {
            "dir": config.cache.dir,
        },
        "music": {
            "root": config.music.root,
            "ssh_host": config.music.ssh_host,
            "lossy_root": config.music.lossy_root,
            "prefer_lossy_for_phone": config.music.prefer_lossy_for_phone,
            "transcode_missing_lossy": config.music.transcode_missing_lossy,
        },
        "phone": {
            "enabled": config.phone.enabled,
            "ssh_host": config.phone.ssh_host,
            "music_root": config.phone.music_root,
        },
        "ui": {
            "columns": config.ui.columns,
            "cell_size": config.ui.cell_size,
            "padding": config.ui.padding,
            "font": config.ui.font,
            "top_gap": config.ui.top_gap,
            "title_gap": config.ui.title_gap,
            "row_gap": config.ui.row_gap,
            "max_image_cache": config.ui.max_image_cache,
            "nowplaying_cover_size": config.ui.nowplaying_cover_size,
            "nowplaying_title_font": config.ui.nowplaying_title_font,
            "nowplaying_text_font": config.ui.nowplaying_text_font,
            "nowplaying_current_font": config.ui.nowplaying_current_font,
            "nowplaying_bio_font": config.ui.nowplaying_bio_font,
            "help_font": config.ui.help_font,
            "loading_font": config.ui.loading_font,
        },
    }


def _string(value, default=""):
    if value is None:
        return default
    return str(value).strip()


def _bool(value, default=False):
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return bool(value)
    text = str(value).strip().casefold()
    if text in ("1", "true", "yes", "y", "on"):
        return True
    if text in ("0", "false", "no", "n", "off"):
        return False
    return default


def _int(value, name, errors, min_value=None, max_value=None):
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        errors.append(f"{name} must be an integer")
        return 0
    if min_value is not None and parsed < min_value:
        errors.append(f"{name} must be >= {min_value}")
    if max_value is not None and parsed > max_value:
        errors.append(f"{name} must be <= {max_value}")
    return parsed


def _optional_int(section, key, default, min_value=None, max_value=None):
    if key not in section:
        return default
    errors = []
    parsed = _int(section.get(key), f"ui.{key}", errors, min_value=min_value, max_value=max_value)
    if errors:
        raise ValueError("Invalid config:\n- " + "\n- ".join(errors))
    return parsed
