import os
import re
import time
import subprocess
import argparse
import sys
import tempfile
import threading
import shutil
from pathlib import Path
# Pillow is imported lazily in check_dependencies() below

# ─────────────────────────────────────────────
# Dependency checker
# ─────────────────────────────────────────────

# Install instructions keyed by (tool, platform).
# platform is "windows", "linux", "mac", or "any".
_INSTALL_GUIDES = {
    "ffmpeg": {
        "windows": [
            "  Option A — Scoop (recommended, auto-updates):",
            "    scoop install ffmpeg",
            "",
            "  Option B — winget:",
            "    winget install Gyan.FFmpeg",
            "",
            "  Option C — manual:",
            "    1. Download a build from https://www.gyan.dev/ffmpeg/builds/",
            "    2. Extract and add the bin\\ folder to your PATH.",
        ],
        "linux": [
            "  Ubuntu / Debian:   sudo apt install ffmpeg",
            "  Fedora:            sudo dnf install ffmpeg",
            "  Arch:              sudo pacman -S ffmpeg",
        ],
        "mac": [
            "  Homebrew:          brew install ffmpeg",
            "  MacPorts:          sudo port install ffmpeg",
        ],
    },
    "ffprobe": {
        "any": [
            "  ffprobe ships with ffmpeg — install ffmpeg (see above) and it",
            "  will be included automatically.",
        ],
    },
    "adb": {
        "windows": [
            "  Option A — Scoop:",
            "    scoop install adb",
            "",
            "  Option B — winget:",
            "    winget install Google.PlatformTools",
            "",
            "  Option C — manual:",
            "    1. Download Platform Tools from",
            "       https://developer.android.com/tools/releases/platform-tools",
            "    2. Extract and add the folder to your PATH.",
            "",
            "  Then enable USB Debugging on your phone:",
            "    Settings → About phone → tap Build number 7× → Developer options",
            "    → enable USB Debugging.",
        ],
        "linux": [
            "  Ubuntu / Debian:   sudo apt install adb",
            "  Arch:              sudo pacman -S android-tools",
            "  Or via SDK:        https://developer.android.com/tools/releases/platform-tools",
            "",
            "  Then enable USB Debugging on your phone:",
            "    Settings → About phone → tap Build number 7× → Developer options",
            "    → enable USB Debugging.",
        ],
        "mac": [
            "  Homebrew:          brew install android-platform-tools",
            "  Or via SDK:        https://developer.android.com/tools/releases/platform-tools",
            "",
            "  Then enable USB Debugging on your phone:",
            "    Settings → About phone → tap Build number 7× → Developer options",
            "    → enable USB Debugging.",
        ],
    },
    "pillow": {
        "any": [
            "  pip install Pillow",
            "",
            "  If you are using a virtual environment, activate it first.",
            "  If pip is not found, try:  python -m pip install Pillow",
        ],
    },
}

def _platform():
    if sys.platform.startswith("win"):
        return "windows"
    if sys.platform.startswith("darwin"):
        return "mac"
    return "linux"

def _guide(tool):
    """Return install guide lines for `tool` on the current platform."""
    guides = _INSTALL_GUIDES.get(tool, {})
    plat = _platform()
    return guides.get(plat) or guides.get("any") or [f"  See https://github.com/search?q={tool}"]

def _check_cli(tool):
    """Return True if `tool` is on PATH."""
    return shutil.which(tool) is not None

def check_dependencies(need_adb=False):
    """
    Verify all required tools are available.
    Prints a formatted install guide for each missing dependency and exits
    with code 1 if anything critical is absent.
    """
    missing = []

    # ── Python packages ────────────────────────
    try:
        from PIL import Image  # noqa: F401
    except ImportError:
        missing.append("pillow")

    # ── CLI tools ──────────────────────────────
    if not _check_cli("ffmpeg"):
        missing.append("ffmpeg")

    # ffprobe is optional (graceful fallback), but warn if absent
    ffprobe_missing = not _check_cli("ffprobe")

    if need_adb and not _check_cli("adb"):
        missing.append("adb")

    if not missing and not ffprobe_missing:
        return  # all good, nothing to print

    width = 60

    if ffprobe_missing and not missing:
        # Non-fatal: just a heads-up
        print("─" * width)
        print("⚠  ffprobe not found — video progress bars will show raw")
        print("   timestamps instead of percentages.")
        print("   ffprobe is bundled with ffmpeg; install ffmpeg to fix this.")
        print("─" * width)
        print()
        return

    # Fatal missing deps
    print()
    print("─" * width)
    print("  ❌  Missing dependencies detected")
    print("─" * width)

    for dep in missing:
        print(f"\n  ● {dep}")
        for line in _guide(dep):
            print(line)

    if ffprobe_missing:
        print("\n  ● ffprobe  (optional — needed for % progress bars)")
        for line in _guide("ffprobe"):
            print(line)

    print()
    print("─" * width)
    print("  Install the above, then re-run this script.")
    print("─" * width)
    print()
    sys.exit(1)

# ─────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────

def get_size_format(b, factor=1024, suffix="B"):
    for unit in ["", "K", "M", "G", "T", "P"]:
        if b < factor:
            return f"{b:.2f}{unit}{suffix}"
        b /= factor

def get_file_age_days(mtime):
    return (time.time() - mtime) / (24 * 3600)

def run(cmd, **kwargs):
    """Run a command and return CompletedProcess. Raises on non-zero exit."""
    return subprocess.run(cmd, check=True, **kwargs)

# ─────────────────────────────────────────────
# GPU / encoder detection
# ─────────────────────────────────────────────

# Encoder probe order: (ffmpeg_encoder, display_name)
_ENCODER_CANDIDATES = [
    ("h264_nvenc",   "NVIDIA NVENC"),
    ("h264_amf",     "AMD AMF"),
    ("h264_qsv",     "Intel QSV"),
    ("h264_videotoolbox", "Apple VideoToolbox"),
    ("libx264",      "CPU (libx264)"),
]

def detect_encoder():
    """
    Try each hardware encoder in order by asking ffmpeg to encode a 1-frame
    black video. Returns (encoder_name, display_name) for the first that works,
    falling back to libx264.
    """
    # Build a minimal 1-frame null source test
    test_cmd_base = [
        "ffmpeg", "-y",
        "-f", "lavfi", "-i", "nullsrc=s=128x128:d=0.1",
        "-frames:v", "1",
        "-f", "null", "-",
    ]
    for encoder, label in _ENCODER_CANDIDATES:
        probe = test_cmd_base[:] 
        probe.insert(-2, "-vcodec")
        probe.insert(-2, encoder)
        result = subprocess.run(
            probe, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        if result.returncode == 0:
            return encoder, label
    # Should never reach here since libx264 is in the list, but just in case
    return "libx264", "CPU (libx264)"

_ENCODER: tuple[str, str] | None = None  # cached after first call

def get_encoder():
    global _ENCODER
    if _ENCODER is None:
        _ENCODER = detect_encoder()
    return _ENCODER

# ─────────────────────────────────────────────
# ADB helpers
# ─────────────────────────────────────────────

def adb_check():
    """Abort if no device is connected."""
    result = subprocess.run(
        ["adb", "devices"], capture_output=True, text=True
    )
    lines = [l.strip() for l in result.stdout.splitlines() if l.strip()]
    devices = [l for l in lines[1:] if "\tdevice" in l]
    if not devices:
        print("❌ No ADB device found. Connect your phone and enable USB debugging.")
        sys.exit(1)
    print(f"📱 ADB device ready: {devices[0].split(chr(9))[0]}")

def adb_list_files(remote_dir, recursive=False):
    """
    Return list of (remote_path, size_bytes, mtime_epoch) for media files.
    Uses `adb shell find` so we get mtime without a separate stat call.
    """
    depth = "" if recursive else "-maxdepth 1"
    extensions = r"\( -iname '*.jpg' -o -iname '*.jpeg' -o -iname '*.png' " \
                 r"-o -iname '*.webp' -o -iname '*.mp4' -o -iname '*.mkv' " \
                 r"-o -iname '*.mov' -o -iname '*.avi' \)"
    # printf gives us: path\0size\0mtime
    cmd = f"find {remote_dir} {depth} -type f {extensions} " \
          r"-printf '%p\0%s\0%T@\0'"
    result = subprocess.run(
        ["adb", "shell", cmd],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        print(f"❌ adb shell find failed: {result.stderr.strip()}")
        return []

    entries = []
    parts = result.stdout.split("\0")
    it = iter(parts)
    for path in it:
        path = path.strip()
        if not path:
            continue
        try:
            size = int(next(it).strip())
            mtime = float(next(it).strip())
            entries.append((path, size, mtime))
        except StopIteration:
            break
    return entries

def adb_pull(remote_path, local_path):
    run(["adb", "pull", remote_path, str(local_path)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

def adb_push(local_path, remote_path):
    run(["adb", "push", str(local_path), remote_path],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def adb_discover_paths():
    """
    Query the connected device for real storage roots and known media folders.
    Returns a list of (display_label, path) tuples, deduped and sorted.
    """
    # Candidate dirs to probe — covers stock Android, Samsung, Xiaomi, OnePlus, etc.
    candidates = [
        "/storage/emulated/0/DCIM/Camera",
        "/storage/emulated/0/DCIM",
        "/storage/emulated/0/Pictures",
        "/storage/emulated/0/Download",
        "/storage/emulated/0/Movies",
        "/storage/emulated/0/WhatsApp/Media",
        "/storage/emulated/0/Android/media/com.whatsapp/WhatsApp/Media",
        "/storage/emulated/0/Telegram",
        "/storage/emulated/0/Instagram",
        "/sdcard/DCIM/Camera",
        "/sdcard/DCIM",
        "/sdcard/Pictures",
        "/sdcard/Download",
        "/sdcard/Movies",
    ]

    # Also discover any extra SD card mount under /storage/
    r = subprocess.run(
        ["adb", "shell", "ls /storage/"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        encoding="utf-8", errors="replace",
    )
    for entry in r.stdout.split():
        entry = entry.strip()
        if entry and entry not in ("emulated", "self"):
            # Likely an SD card like /storage/XXXX-XXXX
            candidates += [
                f"/storage/{entry}/DCIM",
                f"/storage/{entry}/Pictures",
                f"/storage/{entry}/Download",
            ]

    # Check which paths actually exist and contain at least one media file
    probe_script = "; ".join(
        f'[ -d "{p}" ] && echo "EXISTS:{p}"' for p in candidates
    )
    r = subprocess.run(
        ["adb", "shell", probe_script],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        encoding="utf-8", errors="replace",
    )
    existing = []
    seen = set()
    for line in r.stdout.splitlines():
        line = line.strip()
        if line.startswith("EXISTS:"):
            path = line[len("EXISTS:"):]
            # Resolve /sdcard → /storage/emulated/0 duplicates
            resolved = path.replace("/sdcard", "/storage/emulated/0")
            if resolved not in seen:
                seen.add(resolved)
                existing.append(path)

    if not existing:
        return []

    # Label each path nicely
    labels = {
        "DCIM/Camera": "Camera roll",
        "DCIM":        "All DCIM",
        "Pictures":    "Pictures",
        "Download":    "Downloads",
        "Movies":      "Movies",
        "WhatsApp/Media": "WhatsApp Media",
        "com.whatsapp/WhatsApp/Media": "WhatsApp Media",
        "Telegram":    "Telegram",
        "Instagram":   "Instagram",
    }
    result = []
    for path in existing:
        label = path  # fallback
        for key, name in labels.items():
            if key in path:
                # Prepend SD card tag if not on internal storage
                prefix = "" if "emulated/0" in path or "/sdcard" in path else "SD: "
                label = f"{prefix}{name}  ({path})"
                break
        result.append((label, path))
    return result

# ─────────────────────────────────────────────
# Compression
# ─────────────────────────────────────────────

EXTENSIONS_IMG = {'.jpg', '.jpeg', '.png', '.webp'}
EXTENSIONS_VID = {'.mp4', '.mkv', '.mov', '.avi'}

def compress_image(input_path, output_path, target_width):
    from PIL import Image
    try:
        with Image.open(input_path) as img:
            if img.size[0] <= target_width:
                img.save(output_path, optimize=True, quality=85)
            else:
                w_pct = target_width / float(img.size[0])
                h_size = int(img.size[1] * w_pct)
                img = img.resize((target_width, h_size), Image.Resampling.LANCZOS)
                img.save(output_path, optimize=True, quality=85)
        return True
    except Exception as e:
        print(f"\n❌ Image Error {Path(input_path).name}: {e}")
        return False

def _probe_duration(input_path):
    """Return duration in seconds via ffprobe, or None if unavailable."""
    try:
        r = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-select_streams", "v:0",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                str(input_path),
            ],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            encoding="utf-8", errors="replace",
        )
        return float(r.stdout.strip())
    except Exception:
        return None

def _parse_time(time_str):
    """Convert HH:MM:SS.ss string to total seconds, or None on failure."""
    try:
        h, m, s = time_str.strip().split(":")
        return int(h) * 3600 + int(m) * 60 + float(s)
    except Exception:
        return None

def _bar(pct, width=20):
    """Return a compact ASCII progress bar like [████████░░░░]  63%"""
    filled = int(width * pct / 100)
    bar = "█" * filled + "░" * (width - filled)
    return f"[{bar}] {pct:3.0f}%"

def compress_video(input_path, output_path, target_width, crf, prefix=""):
    """
    Compress a video, showing a live single-line progress bar:
      [2/5] 🔧 holiday.mp4  [████████░░░░░░░░░░░░]  42%  |  fps=87  speed=3.6x
    """
    encoder, _ = get_encoder()

    hw_quality_flags = {
        "h264_nvenc":        ["-cq", str(crf)],
        "h264_amf":          ["-qp_i", str(crf), "-qp_p", str(crf)],
        "h264_qsv":          ["-global_quality", str(crf)],
        "h264_videotoolbox": ["-q:v", str(crf)],
        "libx264":           ["-crf", str(crf), "-preset", "fast"],
    }
    quality_flags = hw_quality_flags.get(encoder, ["-crf", str(crf), "-preset", "fast"])

    cmd = [
        "ffmpeg", "-y",
        "-i", str(input_path),
        "-vf", f"scale={target_width}:-2",
        "-vcodec", encoder,
        *quality_flags,
        "-acodec", "aac",
        "-stats",
        "-loglevel", "error",
        str(output_path),
    ]

    duration = _probe_duration(input_path)   # seconds, may be None
    term_width = shutil.get_terminal_size(fallback=(120, 24)).columns

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            encoding="utf-8",
            errors="replace",
        )

        for raw_line in proc.stderr:
            # Check cancel flag on every line — kills ffmpeg immediately
            if _cancel.is_set():
                proc.kill()
                proc.wait()
                print(f"\r{' ' * (term_width - 1)}\r", end="", flush=True)
                return None  # sentinel: cancelled mid-encode

            line = raw_line.rstrip()
            if not line:
                continue

            if "frame=" in line or "time=" in line:
                # --- build compact extra info (fps + speed only) ---
                fps   = re.search(r"fps=\s*([\d.]+)", line)
                speed = re.search(r"speed=\s*([\d.]+x)", line)
                extra = "  |  " + "  ".join(filter(None, [
                    f"fps={fps.group(1)}"   if fps   else None,
                    f"speed={speed.group(1)}" if speed else None,
                ]))

                # --- progress bar or fallback time display ---
                time_m = re.search(r"time=\s*(\S+)", line)
                if duration and time_m:
                    elapsed = _parse_time(time_m.group(1))
                    if elapsed is not None:
                        pct = min(elapsed / duration * 100, 100)
                        progress = _bar(pct)
                    else:
                        progress = time_m.group(1)
                elif time_m:
                    progress = time_m.group(1)   # no duration → show raw time
                else:
                    progress = ""

                hint = "  (Q+Enter to cancel)"
                display = f"{prefix}  {progress}{extra}{hint}"
                display = display[:term_width - 1].ljust(term_width - 1)
                print(f"\r{display}", end="", flush=True)

            else:
                # Real error from ffmpeg — print on its own line
                print(f"\n  \u26a0\ufe0f  {line}")

        proc.wait()
        print(f"\r{' ' * (term_width - 1)}\r", end="", flush=True)

        if proc.returncode != 0:
            print(f"\n\u274c Video Error {Path(input_path).name}: ffmpeg exited {proc.returncode}")
            return False
        return True

    except Exception as e:
        print(f"\n\u274c Video Error {Path(input_path).name}: {e}")
        return False



# ─────────────────────────────────────────────
# Cancellation
# ─────────────────────────────────────────────

_cancel = threading.Event()  # set this to request a graceful cancel

def _cancel_listener():
    """
    Background thread: wait for the user to press Q (then Enter).
    Sets _cancel so the main loop stops after the current file finishes,
    or kills a running ffmpeg process immediately.
    Works on all platforms without raw/cbreak terminal mode.
    """
    while not _cancel.is_set():
        try:
            key = input()
        except (EOFError, OSError):
            break
        if key.strip().lower() == "q":
            _cancel.set()
            break

def _start_cancel_listener():
    t = threading.Thread(target=_cancel_listener, daemon=True)
    t.start()

def _cleanup_file(path):
    """Silently remove a file if it exists."""
    try:
        p = Path(path)
        if p.exists():
            p.unlink()
    except Exception:
        pass

# ─────────────────────────────────────────────
# Local mode (original behaviour)
# ─────────────────────────────────────────────

def run_local(args):
    source_path = Path(args.source)
    files_to_process = []
    total_size = 0
    min_size_bytes = args.min_size * 1024 * 1024

    print(f"🔍 Scanning {args.source}...")
    iterator = source_path.rglob("*") if args.recursive else source_path.iterdir()
    for fp in iterator:
        if fp.is_file():
            ext = fp.suffix.lower()
            if ext in EXTENSIONS_IMG | EXTENSIONS_VID:
                age = get_file_age_days(os.stat(fp).st_mtime)
                size = fp.stat().st_size
                if age >= args.age and size >= min_size_bytes:
                    files_to_process.append((fp, ext, size))
                    total_size += size

    if not files_to_process:
        print("No files matched criteria.")
        return

    encoder, enc_label = get_encoder()
    print(f"🎞️  Video encoder: {enc_label} ({encoder})")
    print(f"\n⚠️  WARNING: --overwrite is ENABLED." if args.overwrite
          else f"\nOutput: {args.output}")
    print(f"Found {len(files_to_process)} files totalling {get_size_format(total_size)}.")
    if input("Proceed? (y/n): ").lower() != "y":
        sys.exit()

    if not args.overwrite:
        Path(args.output).mkdir(parents=True, exist_ok=True)

    _start_cancel_listener()
    print("  💡 Press Q + Enter at any time to cancel.")

    ok_count = 0
    for i, (fp, ext, size) in enumerate(files_to_process, 1):
        if _cancel.is_set():
            break

        if args.overwrite:
            tmp = fp.with_suffix(fp.suffix + ".tmp")
        else:
            tmp = Path(args.output) / fp.name

        tag = f"[{i}/{len(files_to_process)}] 🔧 {fp.name[:35]}"
        print(f"{tag}", end="\r", flush=True)

        if ext in EXTENSIONS_IMG:
            ok = compress_image(fp, tmp, args.res)
        else:
            ok = compress_video(fp, tmp, args.res, args.crf, prefix=tag)

        # None = cancelled mid-encode
        if ok is None:
            _cleanup_file(tmp)
            break

        if ok and args.overwrite:
            try:
                os.remove(fp)
                os.rename(tmp, fp)
            except Exception as e:
                print(f"\n❌ Overwrite failed {fp.name}: {e}")
        elif not ok and args.overwrite:
            _cleanup_file(tmp)

        if ok:
            ok_count += 1

    if _cancel.is_set():
        print(f"\n\n🛑 Cancelled after {ok_count} file(s). Partial files removed.")
    else:
        print(f"\n\n✅ Done! Processed {ok_count} file(s).")

# ─────────────────────────────────────────────
# ADB mode (pull → compress locally → push back)
# ─────────────────────────────────────────────

def run_adb(args):
    adb_check()

    min_size_bytes = args.min_size * 1024 * 1024

    print(f"🔍 Listing files on device at {args.source} ...")
    entries = adb_list_files(args.source, args.recursive)

    files_to_process = []
    total_size = 0
    for remote_path, size, mtime in entries:
        ext = Path(remote_path).suffix.lower()
        age = get_file_age_days(mtime)
        if age >= args.age and size >= min_size_bytes:
            files_to_process.append((remote_path, ext, size))
            total_size += size

    if not files_to_process:
        print("No files matched criteria.")
        return

    encoder, enc_label = get_encoder()
    print(f"🎞️  Video encoder: {enc_label} ({encoder})")
    print(f"\n📂 Found {len(files_to_process)} files on device "
          f"({get_size_format(total_size)} total)")
    if args.adb_keep_local:
        out_dir = Path(args.output)
        out_dir.mkdir(parents=True, exist_ok=True)
        print(f"   Compressed copies will be saved locally to: {out_dir}")
    else:
        print("   Compressed files will be pushed back to the device "
              "(originals overwritten).")
    if input("Proceed? (y/n): ").lower() != "y":
        sys.exit()

    ok_count = 0
    fail_count = 0

    _start_cancel_listener()
    print("  💡 Press Q + Enter at any time to cancel.")

    with tempfile.TemporaryDirectory(prefix="adb_compress_") as tmpdir:
        tmpdir = Path(tmpdir)
        for i, (remote_path, ext, size) in enumerate(files_to_process, 1):
            if _cancel.is_set():
                break

            fname = Path(remote_path).name
            label = fname[:38]

            # 1. Pull
            print(f"[{i}/{len(files_to_process)}] ⬇  Pulling  {label}...", end="\r", flush=True)
            local_orig = tmpdir / ("orig_" + fname)
            local_comp = tmpdir / ("comp_" + fname)
            try:
                adb_pull(remote_path, local_orig)
            except subprocess.CalledProcessError:
                print(f"\n❌ Pull failed: {fname}")
                fail_count += 1
                continue

            if _cancel.is_set():
                _cleanup_file(local_orig)
                break

            # 2. Compress
            if ext in EXTENSIONS_IMG:
                ok = compress_image(local_orig, local_comp, args.res)
            else:
                ok = compress_video(local_orig, local_comp, args.res, args.crf,
                                    prefix=f"[{i}/{len(files_to_process)}] 🔧 {fname[:35]}")

            _cleanup_file(local_orig)   # raw pull no longer needed

            # None = cancelled mid-encode
            if ok is None:
                _cleanup_file(local_comp)
                break

            if not ok:
                _cleanup_file(local_comp)
                fail_count += 1
                continue

            orig_size = size  # use the size from adb scan (local_orig already deleted)
            comp_size = local_comp.stat().st_size
            saving = (1 - comp_size / orig_size) * 100 if orig_size else 0

            # 3a. Save locally (no push)
            if args.adb_keep_local:
                dest = Path(args.output) / fname
                local_comp.rename(dest)
                print(f"[{i}/{len(files_to_process)}] 💾 Saved    {label} "
                      f"({saving:.0f}% smaller)")

            # 3b. Push back to device
            else:
                print(f"[{i}/{len(files_to_process)}] ⬆  Pushing  {label}...", end="\r", flush=True)
                try:
                    adb_push(local_comp, remote_path)
                    print(f"[{i}/{len(files_to_process)}] ✅ Done     {label} "
                          f"({saving:.0f}% smaller)")
                except subprocess.CalledProcessError:
                    print(f"\n❌ Push failed: {fname}")
                    _cleanup_file(local_comp)
                    fail_count += 1
                    continue

            _cleanup_file(local_comp)
            ok_count += 1
        # TemporaryDirectory auto-clears on exit — any remaining temp files gone

    print(f"\n{'─'*50}")
    if _cancel.is_set():
        print(f"🛑 Cancelled.  ✅ Done: {ok_count}   ❌ Failed: {fail_count}   🗑  Cache cleared.")
    else:
        print(f"✅ Success: {ok_count}   ❌ Failed: {fail_count}")

# ─────────────────────────────────────────────
# Guided setup wizard
# ─────────────────────────────────────────────

def ask(prompt, default=None):
    """Prompt the user, showing the default in brackets. Returns stripped input."""
    suffix = f" [{default}]" if default is not None else ""
    try:
        value = input(f"  {prompt}{suffix}: ").strip()
    except (KeyboardInterrupt, EOFError):
        print("\nAborted.")
        sys.exit(0)
    return value if value else (str(default) if default is not None else "")

def ask_choice(prompt, choices, default=None):
    """
    Present a numbered menu and return the chosen value.
    choices: list of (display_label, value)
    """
    print(f"\n  {prompt}")
    for i, (label, _) in enumerate(choices, 1):
        marker = " (default)" if default is not None and i == default else ""
        print(f"    {i}) {label}{marker}")
    while True:
        raw = ask("Enter number", default=default)
        try:
            idx = int(raw)
            if 1 <= idx <= len(choices):
                return choices[idx - 1][1]
        except ValueError:
            pass
        print(f"    ⚠  Please enter a number between 1 and {len(choices)}.")

def ask_bool(prompt, default=True):
    hint = "Y/n" if default else "y/N"
    raw = ask(f"{prompt} ({hint})", default="y" if default else "n").lower()
    return raw in ("y", "yes", "")  if default else raw in ("y", "yes")

def guided_setup():
    """Interactive wizard that returns a populated argparse.Namespace."""
    print()
    print("╔══════════════════════════════════════╗")
    print("║      Media Compressor — Setup        ║")
    print("╚══════════════════════════════════════╝")
    print("  (Press Ctrl+C at any time to cancel)\n")

    # ── Step 1: mode ────────────────────────────────
    mode = ask_choice(
        "Where are the files?",
        [
            ("On this PC / local folder", "local"),
            ("On my Android phone via ADB (USB)", "adb"),
        ],
        default=1,
    )

    ns = argparse.Namespace(
        adb=(mode == "adb"),
        overwrite=False,
        output=None,
        adb_keep_local=False,
        age=30,
        min_size=0.0,
        recursive=False,
        res=1280,
        crf=28,
    )

    # ── Step 2: source path ──────────────────────────
    print()
    if mode == "adb":
        print("  Common Android media paths:")
        print("    /sdcard/DCIM/Camera   — camera roll")
        print("    /sdcard/DCIM          — all DCIM")
        print("    /storage/emulated/0/DCIM — full path (use if above fails)")
        ns.source = ask("Device path to scan", default="/sdcard/DCIM/Camera")
    else:
        ns.source = ask("Local folder to scan")
        if not ns.source:
            print("  ⚠  Source folder is required.")
            sys.exit(1)
        if not Path(ns.source).is_dir():
            print(f"  ⚠  Folder not found: {ns.source}")
            sys.exit(1)

    # ── Step 3: recursive ───────────────────────────
    ns.recursive = ask_bool("Include sub-folders?", default=False)

    # ── Step 4: output / overwrite ──────────────────
    print()
    if mode == "adb":
        dest = ask_choice(
            "What to do with compressed files?",
            [
                ("Push back to phone (replaces originals)", "push"),
                ("Save to a local folder on this PC",       "keep"),
            ],
            default=1,
        )
        if dest == "keep":
            ns.adb_keep_local = True
            ns.output = ask("Local output folder", default="./compressed")
    else:
        dest = ask_choice(
            "Output destination?",
            [
                ("Save to a different folder (safe)", "folder"),
                ("Overwrite originals in-place (⚠ destructive)", "overwrite"),
            ],
            default=1,
        )
        if dest == "folder":
            ns.output = ask("Output folder", default="./compressed")
        else:
            print("  ⚠  Originals will be replaced. Make sure you have a backup.")
            if not ask_bool("Are you sure?", default=False):
                sys.exit(0)
            ns.overwrite = True

    # ── Step 5: filters ─────────────────────────────
    print()
    print("  ── Filters (press Enter to keep defaults) ──")
    raw_age = ask("Min file age in days", default=30)
    try:
        ns.age = int(raw_age)
    except ValueError:
        ns.age = 30

    raw_size = ask("Min file size in MB (0 = no limit)", default=0)
    try:
        ns.min_size = float(raw_size)
    except ValueError:
        ns.min_size = 0.0

    # ── Step 6: quality ─────────────────────────────
    print()
    print("  ── Quality (press Enter to keep defaults) ──")
    raw_res = ask("Target width in pixels", default=1280)
    try:
        ns.res = int(raw_res)
    except ValueError:
        ns.res = 1280

    raw_crf = ask("Video CRF (0–51, lower = better quality)", default=28)
    try:
        ns.crf = int(raw_crf)
    except ValueError:
        ns.crf = 28

    # ── Summary ─────────────────────────────────────
    print()
    print("  ── Summary ─────────────────────────────────")
    print(f"  Mode      : {'ADB (phone → PC → phone)' if ns.adb else 'Local'}")
    print(f"  Source    : {ns.source}")
    if ns.adb and not ns.adb_keep_local:
        print(f"  Output    : push back to device")
    elif ns.overwrite:
        print(f"  Output    : overwrite originals")
    else:
        print(f"  Output    : {ns.output}")
    print(f"  Recursive : {ns.recursive}")
    print(f"  Min age   : {ns.age} days")
    print(f"  Min size  : {ns.min_size} MB")
    print(f"  Width     : {ns.res}px   CRF: {ns.crf}")
    print()
    if not ask_bool("Start?", default=True):
        sys.exit(0)

    return ns

# ─────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Compress images/videos locally or via ADB (pull → compress → push).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples
────────
# Compress local files, save to /output
  python compress_media.py /path/to/media -o /output

# Compress local files, overwrite originals
  python compress_media.py /path/to/media --overwrite

# ADB: pull from phone, compress, push back (overwrites on device)
  python compress_media.py /sdcard/DCIM --adb

# ADB: pull, compress, save to local folder (don't push back)
  python compress_media.py /sdcard/DCIM --adb --adb-keep-local -o ./compressed

# Run with no arguments for interactive guided setup
  python compress_media.py
""",
    )

    # Source is always required (when not using guided setup)
    parser.add_argument("source", nargs="?",
        help="Local directory (default) or remote path on device (with --adb)")

    # Output / overwrite
    output_group = parser.add_mutually_exclusive_group()
    output_group.add_argument("-o", "--output",
        help="Output directory for compressed files")
    output_group.add_argument("--overwrite", action="store_true",
        help="Overwrite original files in-place (local mode only, DANGEROUS)")

    # Filtering
    parser.add_argument("-a", "--age", type=int, default=30,
        help="Minimum file age in days (default: 30)")
    parser.add_argument("--min-size", type=float, default=0,
        help="Minimum file size in MB (default: 0)")
    parser.add_argument("--recursive", action="store_true",
        help="Search subdirectories recursively")

    # Compression quality
    parser.add_argument("-r", "--res", type=int, default=1280,
        help="Target width in pixels (default: 1280)")
    parser.add_argument("-c", "--crf", type=int, default=28,
        help="Video CRF quality (0–51, lower = better, default: 28)")

    # ADB options
    adb_group = parser.add_argument_group("ADB options")
    adb_group.add_argument("--adb", action="store_true",
        help="Enable ADB mode: pull files from device, compress on PC, push back")
    adb_group.add_argument("--adb-keep-local", action="store_true",
        help="With --adb: save compressed files locally (-o required) instead of pushing back")

    args = parser.parse_args()

    # ── Dependency check ────────────────────────────
    # Pass need_adb=True only if --adb flag present or no args (wizard may enable it)
    need_adb = "--adb" in sys.argv or len(sys.argv) == 1
    check_dependencies(need_adb=need_adb)

    # ── No args → guided setup ───────────────────────
    if len(sys.argv) == 1:
        args = guided_setup()

    # ── Validation ──────────────────────────────────
    if not args.source:
        parser.error("source path is required (or run with no arguments for guided setup).")

    if args.adb:
        if args.overwrite:
            parser.error("--overwrite is not valid in --adb mode. "
                         "Use --adb-keep-local to save locally instead.")
        if args.adb_keep_local and not args.output:
            parser.error("--adb-keep-local requires --output <directory>.")
        run_adb(args)
    else:
        if not args.overwrite and not args.output:
            parser.error("Provide --output <directory> or --overwrite.")
        run_local(args)

if __name__ == "__main__":
    main()
