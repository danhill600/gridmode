import json
import os
import posixpath
import shlex
import subprocess
import time
import re


AUDIO_EXTENSIONS = (
    ".mp3",
    ".flac",
    ".m4a",
    ".ogg",
    ".opus",
    ".wav",
    ".aiff",
    ".aif",
)


class PhoneTransferCancelled(RuntimeError):
    pass


def lossy_album_match_key(name):
    name = os.path.basename(str(name or ""))
    name = re.sub(r"\\+", "", name)
    name = re.sub(r"\[[^\]]*\]", " ", name)
    name = re.sub(r"\{[^}]*\}", " ", name)
    name = re.sub(r"\((?:19|20)\d{2}[^)]*\)", " ", name)
    name = re.sub(r"\b(?:19|20)\d{2}\b", " ", name)
    name = re.sub(r"\b(?:web|cd|vinyl|flac|mp3|lossy|v0|vbr|320|24bit|16bit)\b", " ", name, flags=re.IGNORECASE)
    name = re.sub(r"[^0-9a-zA-Z]+", " ", name)
    return re.sub(r"\s+", " ", name).strip().casefold()


def _check_cancel(cancel_event):
    if cancel_event is not None and cancel_event.is_set():
        raise PhoneTransferCancelled("phone send cancelled")


def _run_cancelable(cmd, timeout, cancel_event=None, **kwargs):
    _check_cancel(cancel_event)
    input_data = kwargs.pop("input", None)
    if input_data is not None:
        kwargs["stdin"] = subprocess.PIPE
    proc = subprocess.Popen(cmd, **kwargs)
    started_at = time.monotonic()
    while True:
        try:
            _check_cancel(cancel_event)
            stdout, stderr = proc.communicate(input=input_data, timeout=0.2)
            return subprocess.CompletedProcess(cmd, proc.returncode, stdout, stderr)
        except subprocess.TimeoutExpired:
            input_data = None
            if timeout is not None and time.monotonic() - started_at > timeout:
                proc.kill()
                proc.communicate()
                raise TimeoutError(f"command timed out after {timeout}s")
        except PhoneTransferCancelled:
            proc.terminate()
            try:
                proc.communicate(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.communicate()
            raise


def list_phone_album_dirs(transfer_ssh_host, phone_ssh_host, phone_root, timeout=30):
    if not transfer_ssh_host:
        raise ValueError("phone listing requires music.ssh_host")
    if not phone_ssh_host:
        raise ValueError("phone listing requires phone.ssh_host")
    if not phone_root:
        raise ValueError("phone listing requires phone.music_root")

    script = r"""
root=$1
for d in "$root"/*; do
    [ -d "$d" ] || continue
    name=${d##*/}
    case "$name" in .*) continue;; esac
    mtime=$(stat -c %Y "$d" 2>/dev/null || echo 0)
    printf '%s\t%s\n' "$mtime" "$name"
done
"""
    phone_cmd = "sh -c {} gridmode-list-phone {}".format(
        shlex.quote(script),
        shlex.quote(phone_root),
    )
    transfer_cmd = "ssh -o BatchMode=yes {} {}".format(
        shlex.quote(phone_ssh_host),
        shlex.quote(phone_cmd),
    )
    proc = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", transfer_ssh_host, transfer_cmd],
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or f"ssh exited {proc.returncode}")
    albums = []
    for line in proc.stdout.splitlines():
        if "\t" not in line:
            continue
        mtime_text, name = line.split("\t", 1)
        try:
            mtime = float(mtime_text)
        except ValueError:
            mtime = 0
        albums.append({"name": name, "rel_dir": name, "mtime": mtime})
    albums.sort(key=lambda item: item["name"].casefold())
    return albums


def list_phone_album_tracks(transfer_ssh_host, phone_ssh_host, phone_root, album_names, timeout=120):
    if not transfer_ssh_host:
        raise ValueError("phone track listing requires music.ssh_host")
    if not phone_ssh_host:
        raise ValueError("phone track listing requires phone.ssh_host")
    if not phone_root:
        raise ValueError("phone track listing requires phone.music_root")

    safe_album_names = []
    for name in album_names:
        name = str(name or "")
        if not name or name in (".", "..") or "/" in name or name.startswith("."):
            continue
        safe_album_names.append(name)

    if not safe_album_names:
        return {}

    script = r"""
root=$1
shift
[ -d "$root" ] || exit 2
cd "$root" || exit 2
for album in "$@"; do
    case "$album" in ""|"."|".."|/*|*/*|.*) continue;; esac
    [ -d "$album" ] || continue
    find "$album" -type f \( \
        -iname '*.mp3' -o \
        -iname '*.flac' -o \
        -iname '*.m4a' -o \
        -iname '*.ogg' -o \
        -iname '*.opus' -o \
        -iname '*.wav' -o \
        -iname '*.aiff' -o \
        -iname '*.aif' \
    \) | sort | while IFS= read -r track; do
        printf '%s\t%s\n' "$album" "$track"
    done
done
"""
    phone_cmd = "sh -c {} gridmode-list-phone-tracks {} {}".format(
        shlex.quote(script),
        shlex.quote(phone_root),
        " ".join(shlex.quote(name) for name in safe_album_names),
    )
    transfer_cmd = "ssh -o BatchMode=yes {} {}".format(
        shlex.quote(phone_ssh_host),
        shlex.quote(phone_cmd),
    )
    proc = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", transfer_ssh_host, transfer_cmd],
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or f"ssh exited {proc.returncode}")

    tracks = {name: [] for name in safe_album_names}
    for line in proc.stdout.splitlines():
        if "\t" not in line:
            continue
        album, track = line.split("\t", 1)
        if album in tracks and track:
            tracks[album].append(track)
    return tracks


def prepare_album_for_phone(
    music_ssh_host,
    music_root,
    rel_dir,
    lossy_root="",
    prefer_lossy=True,
    transcode_missing=False,
    timeout=900,
    cancel_event=None,
):
    if not music_ssh_host:
        raise ValueError("phone transfer requires music.ssh_host")
    if not music_root:
        raise ValueError("phone transfer requires music.root")
    if not rel_dir:
        raise ValueError("selected album has no source directory")

    payload = {
        "music_root": music_root,
        "rel_dir": rel_dir,
        "lossy_root": lossy_root,
        "prefer_lossy": bool(prefer_lossy),
        "transcode_missing": bool(transcode_missing),
    }
    script = r"""
import json
import os
import re
import shutil
import subprocess
import sys

AUDIO = (".mp3", ".flac", ".m4a", ".ogg", ".opus", ".wav", ".aiff", ".aif")
COPY_AS_IS = (".mp3", ".m4a", ".ogg", ".opus")
ART = (".png", ".jpg", ".jpeg", ".webp", ".gif")

payload = json.load(sys.stdin)
music_root = payload["music_root"]
rel_dir = payload["rel_dir"]
lossy_root = payload.get("lossy_root") or ""
prefer_lossy = bool(payload.get("prefer_lossy"))
transcode_missing = bool(payload.get("transcode_missing"))

source = os.path.normpath(os.path.join(music_root, rel_dir))
if not source.startswith(os.path.normpath(music_root) + os.sep):
    raise SystemExit("bad source path")
if not os.path.isdir(source):
    raise SystemExit("source album directory not found: " + source)

def result(path, kind, created=False, matched=False):
    json.dump({"path": path, "kind": kind, "created": created, "matched": matched}, sys.stdout)

def lossy_match_key(name):
    name = os.path.basename(str(name or ""))
    name = re.sub(r"\\+", "", name)
    name = re.sub(r"\[[^\]]*\]", " ", name)
    name = re.sub(r"\{[^}]*\}", " ", name)
    name = re.sub(r"\((?:19|20)\d{2}[^)]*\)", " ", name)
    name = re.sub(r"\b(?:19|20)\d{2}\b", " ", name)
    name = re.sub(r"\b(?:web|cd|vinyl|flac|mp3|lossy|v0|vbr|320|24bit|16bit)\b", " ", name, flags=re.IGNORECASE)
    name = re.sub(r"[^0-9a-zA-Z]+", " ", name)
    return re.sub(r"\s+", " ", name).strip().casefold()

def has_phone_audio(path):
    for current, dirs, files in os.walk(path):
        dirs[:] = [name for name in dirs if not name.startswith(".")]
        for name in files:
            if name.startswith("."):
                continue
            if os.path.splitext(name)[1].casefold() in COPY_AS_IS:
                return True
    return False

def find_existing_lossy():
    rel_parent = os.path.dirname(rel_dir)
    source_leaf = os.path.basename(rel_dir)
    lossy_parent = os.path.normpath(os.path.join(lossy_root, rel_parent))
    if not lossy_parent.startswith(os.path.normpath(lossy_root)):
        return ""
    wanted = lossy_match_key(source_leaf)
    if not wanted or not os.path.isdir(lossy_parent):
        return ""
    for name in sorted(os.listdir(lossy_parent)):
        if name.startswith("."):
            continue
        candidate = os.path.normpath(os.path.join(lossy_parent, name))
        if not candidate.startswith(os.path.normpath(lossy_root) + os.sep):
            continue
        if os.path.isdir(candidate) and lossy_match_key(name) == wanted and has_phone_audio(candidate):
            return candidate
    return ""

if not prefer_lossy:
    result(source, "source")
    raise SystemExit(0)

if not lossy_root:
    raise SystemExit("lossy requested but music.lossy_root is not configured")

lossy = os.path.normpath(os.path.join(lossy_root, rel_dir))
if not lossy.startswith(os.path.normpath(lossy_root) + os.sep):
    raise SystemExit("bad lossy path")
if os.path.isdir(lossy):
    result(lossy, "lossy")
    raise SystemExit(0)

matched_lossy = find_existing_lossy()
if matched_lossy:
    result(matched_lossy, "lossy", matched=True)
    raise SystemExit(0)

if not transcode_missing:
    raise SystemExit("lossy album not found: " + lossy)

os.makedirs(lossy, exist_ok=True)
converted = 0
copied = 0
for current, dirs, files in os.walk(source):
    dirs[:] = [name for name in dirs if not name.startswith(".")]
    rel_current = os.path.relpath(current, source)
    dst_dir = lossy if rel_current == "." else os.path.join(lossy, rel_current)
    os.makedirs(dst_dir, exist_ok=True)
    for name in files:
        if name.startswith("."):
            continue
        ext = os.path.splitext(name)[1].casefold()
        src = os.path.join(current, name)
        if ext == ".flac":
            dst = os.path.join(dst_dir, os.path.splitext(name)[0] + ".mp3")
            if not os.path.exists(dst):
                subprocess.run(
                    ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-i", src, "-qscale:a", "0", dst],
                    check=True,
                )
            converted += 1
        elif ext in COPY_AS_IS or ext in ART:
            shutil.copy2(src, os.path.join(dst_dir, name))
            copied += 1

if converted == 0 and copied == 0:
    raise SystemExit("no phone-suitable audio or art found under " + source)

result(lossy, "lossy", created=True)
"""
    cmd = ["ssh", "-o", "BatchMode=yes", music_ssh_host, "python3 -c " + shlex.quote(script)]
    proc = _run_cancelable(
        cmd,
        timeout,
        cancel_event,
        input=json.dumps(payload),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or proc.stdout.strip() or f"ssh exited {proc.returncode}")
    return json.loads(proc.stdout)


def copy_remote_album_to_phone(
    transfer_ssh_host,
    source_dir,
    phone_ssh_host,
    phone_root,
    timeout=900,
    cancel_event=None,
):
    if not transfer_ssh_host:
        raise ValueError("transfer ssh host is required")
    if not source_dir:
        raise ValueError("source directory is required")
    if not phone_ssh_host:
        raise ValueError("phone ssh host is required")
    if not phone_root:
        raise ValueError("phone music_root is required")

    leaf = source_dir.rstrip("/").rsplit("/", 1)[-1]
    if not leaf:
        raise ValueError("bad source directory")

    source_arg = source_dir.rstrip("/") + "/"
    dest_arg = f"{phone_ssh_host}:{phone_root.rstrip('/')}/{leaf}/"
    mkdir_cmd = "mkdir -p " + shlex.quote(phone_root)
    cmd_text = "ssh -o BatchMode=yes {} {} && rsync -a --delete {} {}".format(
        shlex.quote(phone_ssh_host),
        shlex.quote(mkdir_cmd),
        shlex.quote(source_arg),
        shlex.quote(dest_arg),
    )
    cmd = ["ssh", "-o", "BatchMode=yes", transfer_ssh_host, cmd_text]
    proc = _run_cancelable(
        cmd,
        timeout,
        cancel_event,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or proc.stdout.strip() or f"rsync exited {proc.returncode}")
    return {"name": leaf, "destination": f"{phone_ssh_host}:{phone_root.rstrip('/')}/{leaf}"}


def phone_album_dir_exists(transfer_ssh_host, phone_ssh_host, phone_root, album_name, timeout=60, cancel_event=None):
    if not transfer_ssh_host:
        raise ValueError("phone exists check requires music.ssh_host")
    if not phone_ssh_host:
        raise ValueError("phone exists check requires phone.ssh_host")
    if not phone_root:
        raise ValueError("phone exists check requires phone.music_root")
    if not album_name or "/" in album_name or album_name in (".", ".."):
        raise ValueError("phone exists check requires one album directory name")

    script = r"""
root=$1
name=$2
[ -d "$root/$name" ]
"""
    phone_cmd = "sh -c {} gridmode-phone-exists {} {}".format(
        shlex.quote(script),
        shlex.quote(phone_root),
        shlex.quote(album_name),
    )
    transfer_cmd = "ssh -o BatchMode=yes {} {}".format(
        shlex.quote(phone_ssh_host),
        shlex.quote(phone_cmd),
    )
    proc = _run_cancelable(
        ["ssh", "-o", "BatchMode=yes", transfer_ssh_host, transfer_cmd],
        timeout,
        cancel_event,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if proc.returncode == 0:
        return True
    if proc.returncode == 1:
        return False
    raise RuntimeError(proc.stderr.strip() or proc.stdout.strip() or f"phone exists check exited {proc.returncode}")


def delete_phone_album_dir(transfer_ssh_host, phone_ssh_host, phone_root, phone_rel_dir, timeout=120):
    if not transfer_ssh_host:
        raise ValueError("phone delete requires music.ssh_host")
    if not phone_ssh_host:
        raise ValueError("phone delete requires phone.ssh_host")
    if not phone_root:
        raise ValueError("phone delete requires phone.music_root")
    if not phone_rel_dir:
        raise ValueError("phone delete requires a selected album directory")

    script = r"""
root=$1
rel_dir=$2
case "$rel_dir" in
    ""|"."|".."|/*|*/*)
        printf '%s\n' "bad phone album path" >&2
        exit 2
        ;;
esac
[ -d "$root" ] || { printf '%s\n' "phone music root not found: $root" >&2; exit 2; }
cd "$root" || exit 2
[ -d "$rel_dir" ] || { printf '%s\n' "phone album not found: $rel_dir" >&2; exit 2; }
[ ! -L "$rel_dir" ] || { printf '%s\n' "phone album is a symlink: $rel_dir" >&2; exit 2; }
rm -rf -- "$rel_dir" || exit 1
printf '%s\t%s\n' "deleted" "$rel_dir"
"""
    phone_cmd = "sh -c {} gridmode-delete-phone {} {}".format(
        shlex.quote(script),
        shlex.quote(phone_root),
        shlex.quote(phone_rel_dir),
    )
    transfer_cmd = "ssh -o BatchMode=yes {} {}".format(
        shlex.quote(phone_ssh_host),
        shlex.quote(phone_cmd),
    )
    proc = _run_cancelable(
        ["ssh", "-o", "BatchMode=yes", transfer_ssh_host, transfer_cmd],
        timeout,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or proc.stdout.strip() or f"phone delete exited {proc.returncode}")
    line = proc.stdout.strip()
    status, _, name = line.partition("\t")
    if status != "deleted" or not name:
        raise RuntimeError(line or "bad phone delete output")
    return {"deleted": True, "name": name, "rel_dir": phone_rel_dir}


def copy_local_cover_to_remote_album(
    local_cover_path,
    remote_ssh_host,
    remote_album_dir,
    timeout=60,
    cancel_event=None,
):
    if not local_cover_path or not os.path.exists(local_cover_path):
        return False
    if not remote_ssh_host or not remote_album_dir:
        return False

    dest = posixpath.join(remote_album_dir.rstrip("/"), "cover.png")
    with open(local_cover_path, "rb") as cover:
        proc = _run_cancelable(
            ["ssh", "-o", "BatchMode=yes", remote_ssh_host, "cat > " + shlex.quote(dest)],
            timeout,
            cancel_event,
            stdin=cover,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.decode("utf-8", "replace").strip() or f"cover copy exited {proc.returncode}")
    return True
