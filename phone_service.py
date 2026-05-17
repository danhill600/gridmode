import json
import os
import posixpath
import shlex
import subprocess


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


def prepare_album_for_phone(
    music_ssh_host,
    music_root,
    rel_dir,
    lossy_root="",
    prefer_lossy=True,
    transcode_missing=False,
    timeout=900,
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

def result(path, kind, created=False):
    json.dump({"path": path, "kind": kind, "created": created}, sys.stdout)

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
    proc = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", music_ssh_host, "python3 -c " + shlex.quote(script)],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or proc.stdout.strip() or f"ssh exited {proc.returncode}")
    return json.loads(proc.stdout)


def copy_remote_album_to_phone(transfer_ssh_host, source_dir, phone_ssh_host, phone_root, timeout=900):
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
    cmd = "ssh -o BatchMode=yes {} {} && rsync -a --delete {} {}".format(
        shlex.quote(phone_ssh_host),
        shlex.quote(mkdir_cmd),
        shlex.quote(source_arg),
        shlex.quote(dest_arg),
    )
    proc = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", transfer_ssh_host, cmd],
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or proc.stdout.strip() or f"rsync exited {proc.returncode}")
    return {"name": leaf, "destination": f"{phone_ssh_host}:{phone_root.rstrip('/')}/{leaf}"}


def copy_local_cover_to_remote_album(local_cover_path, remote_ssh_host, remote_album_dir, timeout=60):
    if not local_cover_path or not os.path.exists(local_cover_path):
        return False
    if not remote_ssh_host or not remote_album_dir:
        return False

    dest = posixpath.join(remote_album_dir.rstrip("/"), "cover.png")
    with open(local_cover_path, "rb") as cover:
        proc = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", remote_ssh_host, "cat > " + shlex.quote(dest)],
            stdin=cover,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.decode("utf-8", "replace").strip() or f"cover copy exited {proc.returncode}")
    return True
