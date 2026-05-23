import os
import re
import time
import subprocess
import argparse
import sys
import tempfile
import threading
import shutil
import sqlite3
from pathlib import Path
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed

# Pillow is imported lazily in check_dependencies() below

# =============================================
# Database Helper
# =============================================

class MediaDB:
    def __init__(self, db_path):
        self.db_path = db_path
        self._init_db()

    def _init_db(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS media_index (
                    rel_path TEXT PRIMARY KEY,
                    size_bytes INTEGER,
                    mtime REAL,
                    width INTEGER,
                    bitrate_mbps REAL,
                    probe_info TEXT,
                    last_scanned REAL
                )
            """)

    def update_entry(self, rel_path, size, mtime, width, bitrate, probe_info):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                INSERT OR REPLACE INTO media_index 
                (rel_path, size_bytes, mtime, width, bitrate_mbps, probe_info, last_scanned)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (rel_path, size, mtime, width, bitrate, probe_info, time.time()))

    def get_entry(self, rel_path):
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute("SELECT width, bitrate_mbps, probe_info FROM media_index WHERE rel_path = ?", (rel_path,))
            return cursor.fetchone()

# =============================================
# Dependency checker
# =============================================

_INSTALL_GUIDES = {
    "ffmpeg": {
        "windows": [
            "  Option A - Scoop (recommended, auto-updates):",
            "    scoop install ffmpeg",
            "",
            "  Option B - winget:",
            "    winget install Gyan.FFmpeg",
            "",
            "  Option C - manual:",
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
            "  ffprobe ships with ffmpeg - install ffmpeg (see above) and it",
            "  will be included automatically.",
        ],
    },
    "adb": {
        "windows": [
            "  Option A - Scoop:",
            "    scoop install adb",
            "",
            "  Option B - winget:",
            "    winget install Google.PlatformTools",
            "",
            "  Option C - manual:",
            "    1. Download Platform Tools from",
            "       https://developer.android.com/tools/releases/platform-tools",
            "    2. Extract and add the folder to your PATH.",
            "",
            "  Then enable USB Debugging on your phone:",
            "    Settings -> About phone -> tap Build number 7x -> Developer options",
            "    -> enable USB Debugging.",
        ],
        "linux": [
            "  Ubuntu / Debian:   sudo apt install adb",
            "  Arch:              sudo pacman -S android-tools",
            "  Or via SDK:        https://developer.android.com/tools/releases/platform-tools",
            "",
            "  Then enable USB Debugging on your phone:",
            "    Settings -> About phone -> tap Build number 7x -> Developer options",
            "    -> enable USB Debugging.",
        ],
        "mac": [
            "  Homebrew:          brew install android-platform-tools",
            "  Or via SDK:        https://developer.android.com/tools/releases/platform-tools",
            "",
            "  Then enable USB Debugging on your phone:",
            "    Settings -> About phone -> tap Build number 7x -> Developer options",
            "    -> enable USB Debugging.",
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
    guides = _INSTALL_GUIDES.get(tool, {})
    plat = _platform()
    return guides.get(plat) or guides.get("any") or [f"  See https://github.com/search?q={tool}"]

def _check_cli(tool):
    return shutil.which(tool) is not None

def check_dependencies(need_adb=False):
    missing = []
    try:
        from PIL import Image  # noqa: F401
    except ImportError:
        missing.append("pillow")

    if not _check_cli("ffmpeg"):
        missing.append("ffmpeg")

    ffprobe_missing = not _check_cli("ffprobe")

    if need_adb and not _check_cli("adb"):
        missing.append("adb")

    if not missing and not ffprobe_missing:
        return

    width = 60
    if ffprobe_missing and not missing:
        print("-" * width)
        print("[!] ffprobe not found - sizing parameters might skip limit validation pre-checks.")
        print("   ffprobe is bundled with ffmpeg; install ffmpeg to fix this.")
        print("-" * width)
        print()
        return

    print("\n" + "-" * width)
    print("  [X] Missing dependencies detected")
    print("-" * width)

    for dep in missing:
        print(f"\n  * {dep}")
        for line in _guide(dep):
            print(line)

    if ffprobe_missing:
        print("\n  * ffprobe  (optional - needed for safety probes)")
        for line in _guide("ffprobe"):
            print(line)

    print("\n" + "-" * width)
    print("  Install the above, then re-run this script.")
    print("-" * width + "\n")
    sys.exit(1)

# =============================================
# Helpers
# =============================================

def get_size_format(b, factor=1024, suffix="B"):
    for unit in ["", "K", "M", "G", "T", "P"]:
        if b < factor:
            return f"{b:.2f}{unit}{suffix}"
        b /= factor

def get_file_age_days(mtime):
    return (time.time() - mtime) / (24 * 3600)

# =============================================
# Encoders
# =============================================

_ENCODER_CANDIDATES = [
    ("libx264",      "CPU (libx264) - Universal"),
    ("h264_nvenc",   "NVIDIA NVENC (Hardware Accelerated)"),
    ("h264_amf",     "AMD AMF (Hardware Accelerated)"),
    ("h264_qsv",     "Intel QSV (Hardware Accelerated)"),
    ("h264_videotoolbox", "Apple VideoToolbox (Hardware Accelerated)"),
]

_RESOLUTION_TEMPLATES = [
    ("4K (2160p)    - 30 Mbps [CRF 22]", 3840, 30, 22),
    ("1440p (QHD)   - 16 Mbps [CRF 24]", 2560, 16, 24),
    ("1080p (FHD)   -  8 Mbps [CRF 26]", 1920, 8, 26),
    ("720p (HD)     -  5 Mbps [CRF 28]", 1280, 5, 28),
    ("480p          -  2 Mbps [CRF 30]", 854, 2, 30)
]

# =============================================
# ADB helpers
# =============================================

def adb_check():
    result = subprocess.run(["adb", "devices"], capture_output=True, text=True)
    lines = [l.strip() for l in result.stdout.splitlines() if l.strip()]
    devices = [l for l in lines[1:] if "\tdevice" in l]
    if not devices:
        print("[X] No ADB device found. Connect your phone and enable USB debugging.")
        sys.exit(1)
    print(f"[*] ADB device ready: {devices[0].split(chr(9))[0]}")

def adb_list_files(remote_dir, recursive=False):
    depth = "" if recursive else "-maxdepth 1"
    extensions = (
        r"\( -iname '*.jpg' -o -iname '*.jpeg' -o -iname '*.png' "
        r"-o -iname '*.webp' -o -iname '*.mp4' -o -iname '*.mkv' "
        r"-o -iname '*.mov' -o -iname '*.avi' \)"
    )

    printf_cmd = f"find {remote_dir} {depth} -type f {extensions} -printf '%p\\0%s\\0%T@\\0'"
    r = subprocess.run(["adb", "shell", printf_cmd], stdout=subprocess.PIPE, stderr=subprocess.PIPE, encoding="utf-8", errors="replace")

    printf_unsupported = (r.returncode != 0 or "unknown option" in r.stderr.lower() or "invalid option" in r.stderr.lower())

    if not printf_unsupported:
        entries = []
        parts = r.stdout.split("\0")
        it = iter(parts)
        for path in it:
            path = path.strip()
            if not path: continue
            try:
                size = int(next(it).strip())
                mtime = float(next(it).strip())
                entries.append((path, size, mtime))
            except (StopIteration, ValueError):
                break
        if entries or not r.stdout.strip():
            return entries

    # Fallback
    list_cmd = f"find {remote_dir} {depth} -type f {extensions}"
    r2 = subprocess.run(["adb", "shell", list_cmd], stdout=subprocess.PIPE, stderr=subprocess.PIPE, encoding="utf-8", errors="replace")
    if r2.returncode != 0:
        return []

    paths = [p.strip() for p in r2.stdout.splitlines() if p.strip()]
    if not paths: return []

    stat_script = "; ".join(f'stat -c "%n\\0%s\\0%Y\\0" "{p}"' for p in paths)
    r3 = subprocess.run(["adb", "shell", stat_script], stdout=subprocess.PIPE, stderr=subprocess.PIPE, encoding="utf-8", errors="replace")
    entries = []
    parts = r3.stdout.split("\0")
    it = iter(parts)
    for path in it:
        path = path.strip()
        if not path: continue
        try:
            size = int(next(it).strip())
            mtime = float(next(it).strip())
            entries.append((path, size, mtime))
        except (StopIteration, ValueError):
            break
    return entries

def adb_pull(remote_path, local_path):
    subprocess.run(["adb", "pull", remote_path, str(local_path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)

def adb_push(local_path, remote_path):
    subprocess.run(["adb", "push", str(local_path), remote_path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)

def adb_streaming_worker(task):
    """
    Worker for a single file pipeline: pull -> process -> push/move -> cleanup.
    """
    remote_path, ext, size, max_width, crf, max_bitrate, encoder, output_dir, keep_local, tmp_dir = task
    fname = Path(remote_path).name
    
    # 1. Pull
    local_orig = Path(tmp_dir) / f"orig_{fname}"
    local_comp = Path(tmp_dir) / f"comp_{fname}"
    
    try:
        adb_pull(remote_path, local_orig)
    except Exception as e:
        return fname, f"error: pull failed ({e})", size, 0, ""

    # 2. Process
    probe_info = ""
    if ext in EXTENSIONS_IMG:
        res, probe_info = compress_image(local_orig, local_comp, max_width)
    else:
        res, probe_info = compress_video(local_orig, local_comp, max_width, crf, max_bitrate, encoder)
    
    comp_size = 0
    if res == "success":
        comp_size = local_comp.stat().st_size
        
        # 3. Push/Move
        if keep_local:
            shutil.move(str(local_comp), str(Path(output_dir) / fname))
            res = "saved_locally"
        else:
            try:
                adb_push(local_comp, remote_path)
            except Exception as e:
                res = f"error: push failed ({e})"
    
    # 4. Cleanup
    try:
        if local_orig.exists(): os.unlink(local_orig)
        if local_comp.exists(): os.unlink(local_comp)
    except: pass
    
    return fname, res, size, comp_size, probe_info

# =============================================
# Compression Core Logic (Worker Safe)
# =============================================

EXTENSIONS_IMG = {'.jpg', '.jpeg', '.png', '.webp'}
EXTENSIONS_VID = {'.mp4', '.mkv', '.mov', '.avi'}

def _probe_video(input_path):
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "format=duration,bit_rate:stream=width", "-of", "csv=p=0", str(input_path)],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, encoding="utf-8", errors="replace"
        )
        lines = [l.strip() for l in r.stdout.strip().splitlines() if l.strip()]
        if not lines: return None, None, None
        parts = [p.strip() for p in lines[0].split(",")]
        duration = float(parts[0]) if parts[0] else None
        bitrate_bps = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else None
        bitrate_mbps = round(bitrate_bps / 1_000_000, 1) if bitrate_bps else None
        width = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else None
        return duration, width, bitrate_mbps
    except Exception:
        return None, None, None

def compress_image(input_path, output_path, max_width):
    from PIL import Image
    import io as _io
    input_path = Path(input_path)
    output_path = Path(output_path)
    probe_info = ""
    try:
        original_size = input_path.stat().st_size
        with Image.open(input_path) as img:
            probe_info = f"{img.size[0]}x{img.size[1]}"
            if img.size[0] > max_width:
                w_pct = max_width / float(img.size[0])
                h_size = int(img.size[1] * w_pct)
                img = img.resize((max_width, h_size), Image.Resampling.LANCZOS)

            buf = _io.BytesIO()
            img.save(buf, format=img.format or "JPEG", optimize=True, quality=85)
            compressed_bytes = buf.getvalue()

            if len(compressed_bytes) >= original_size:
                shutil.copy2(input_path, output_path)
                return "skipped_larger", probe_info
            else:
                output_path.write_bytes(compressed_bytes)
                return "success", probe_info
    except Exception as e:
        return f"error: {e}", probe_info

def compress_video(input_path, output_path, max_width, crf, max_bitrate_mbps, encoder):
    input_path = Path(input_path)
    output_path = Path(output_path)
    
    duration, input_width, bitrate = _probe_video(input_path)
    probe_parts = []
    if input_width: probe_parts.append(f"{input_width}p")
    if bitrate: probe_parts.append(f"{bitrate}M")
    probe_info = "@".join(probe_parts)
    
    # Pre-check limits
    within_width = not max_width or not input_width or input_width <= max_width
    within_bitrate = not max_bitrate_mbps or not bitrate or bitrate <= max_bitrate_mbps
    if within_width and within_bitrate and (max_width or max_bitrate_mbps):
        return "skipped_limits", probe_info

    hw_quality_flags = {
        "h264_nvenc":        ["-cq", str(crf)],
        "h264_amf":          ["-qp_i", str(crf), "-qp_p", str(crf)],
        "h264_qsv":          ["-global_quality", str(crf)],
        "h264_videotoolbox": ["-q:v", str(crf)],
        "libx264":           ["-crf", str(crf), "-preset", "fast"],
    }
    quality_flags = hw_quality_flags.get(encoder, ["-crf", str(crf), "-preset", "fast"])

    vf_parts = []
    if input_width is not None and input_width > max_width:
        vf_parts.append(f"scale={max_width}:-2")

    cmd = ["ffmpeg", "-y", "-i", str(input_path), "-vcodec", encoder, *quality_flags]
    if vf_parts: cmd += ["-vf", ",".join(vf_parts)]
    if max_bitrate_mbps:
        cmd += ["-maxrate", f"{max_bitrate_mbps}M", "-bufsize", f"{max_bitrate_mbps * 2}M"]
    cmd += ["-acodec", "aac", "-loglevel", "error", str(output_path)]

    try:
        proc = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        if proc.returncode != 0:
            return f"error: ffmpeg failed ({proc.stderr.strip()})", probe_info
        
        if output_path.stat().st_size >= input_path.stat().st_size:
            return "skipped_larger", probe_info
            
        return "success", probe_info
    except Exception as e:
        return f"error: {e}", probe_info

# =============================================
# Unified Processing Worker Bridge
# =============================================

def process_file_worker(file_info):
    """
    Worker function executed inside the Process Pool.
    Handles a single local file conversion context.
    """
    fp_str, ext, size, max_width, crf, max_bitrate, encoder, out_dir_str, overwrite = file_info
    fp = Path(fp_str)
    
    if overwrite:
        tmp = fp.with_suffix(fp.suffix + ".tmp")
    else:
        tmp = Path(out_dir_str) / fp.name

    probe_info = ""
    if ext in EXTENSIONS_IMG:
        res, probe_info = compress_image(fp, tmp, max_width)
    else:
        res, probe_info = compress_video(fp, tmp, max_width, crf, max_bitrate, encoder)

    if res == "success":
        comp_size = tmp.stat().st_size
        if overwrite:
            try:
                os.replace(tmp, fp)
            except Exception as e:
                return fp.name, f"error: replace failed ({e})", size, 0, probe_info
        return fp.name, "success", size, comp_size, probe_info
    else:
        if tmp.exists():
            try: os.unlink(tmp)
            except: pass
        return fp.name, res, size, 0, probe_info

# =============================================
# Mode Runners
# =============================================

def run_local(args):
    source_path = Path(args.source)
    db_path = source_path / "media_index.sqlite"
    db = MediaDB(db_path)
    
    tasks = []
    total_size = 0
    min_size_bytes = args.min_size * 1024 * 1024

    print(f"[*] Scanning {args.source}...")
    iterator = source_path.rglob("*") if args.recursive else source_path.iterdir()
    for fp in iterator:
        if fp.is_file() and fp.suffix.lower() in EXTENSIONS_IMG | EXTENSIONS_VID:
            if fp.name == "media_index.sqlite": continue
            stat = os.stat(fp)
            if get_file_age_days(stat.st_mtime) >= args.age and stat.st_size >= min_size_bytes:
                rel_path = str(fp.relative_to(source_path))
                tasks.append((str(fp), fp.suffix.lower(), stat.st_size, args.max_width, args.crf, args.max_bitrate, args.encoder, args.output, args.overwrite, rel_path))
                total_size += stat.st_size

    if not tasks:
        print("No files matched your criteria.")
        return

    if args.index_only:
        print(f"[*] Indexing {len(tasks)} files into {db_path.name}...")
        for t in tasks:
            fp_str, ext, size, mw, crf, mb, enc, out, ovr, rel = t
            if ext in EXTENSIONS_IMG:
                from PIL import Image
                try:
                    with Image.open(fp_str) as img:
                        w, h = img.size
                        db.update_entry(rel, size, os.path.getmtime(fp_str), w, 0, f"{w}x{h}")
                        print(f"  [IDX] {rel[:40]} -> {w}x{h}")
                except: pass
            else:
                dur, w, br = _probe_video(fp_str)
                probe = f"{w}p@{br}M" if w and br else (f"{w}p" if w else "")
                db.update_entry(rel, size, os.path.getmtime(fp_str), w or 0, br or 0.0, probe)
                print(f"  [IDX] {rel[:40]} -> {probe}")
        print("[+] Indexing complete.")
        return

    print(f"[*] Selected Encoder: {args.encoder}")
    print(f"Found {len(tasks)} files totalizing {get_size_format(total_size)}.")
    if input("Proceed to process concurrently? (y/n): ").lower() != "y":
        sys.exit()

    if not args.overwrite and args.output:
        Path(args.output).mkdir(parents=True, exist_ok=True)

    print(f"\n[*] Processing utilizing concurrent process pools...")
    ok_count, skip_count, fail_count = 0, 0, 0
    
    with ProcessPoolExecutor() as executor:
        futures = {executor.submit(process_file_worker, tasks[i]): i + 1 for i in range(len(tasks))}
        for future in as_completed(futures):
            idx = futures[future]
            fname, status, orig_sz, comp_sz, probe_info = future.result()
            
            display_name = f"File #{idx}" if args.anon else fname[:30]
            probe_tag = f" [{probe_info}]" if probe_info else ""

            if status == "success":
                ok_count += 1
                saving = (1 - comp_sz / orig_sz) * 100
                print(f"  [OK] {display_name}{probe_tag} -> {get_size_format(orig_sz)} to {get_size_format(comp_sz)} ({saving:.0f}% smaller)")
            elif "skipped" in status:
                skip_count += 1
                reason = "would grow file" if "larger" in status else "within resolution/bitrate rules"
                print(f"  [-] {display_name}{probe_tag} -> Skipped ({reason})")
            else:
                fail_count += 1
                print(f"  [X] {display_name} -> Failed ({status})")

    print(f"\nFinished! Compressed: {ok_count} | Skipped: {skip_count} | Failed: {fail_count}")


def run_adb(args):
    adb_check()
    min_size_bytes = args.min_size * 1024 * 1024

    print(f"[*] Listing files on device at {args.source} ...")
    entries = adb_list_files(args.source, args.recursive)
    
    # Path to remote database
    remote_db = f"{args.source.rstrip('/')}/media_index.sqlite"
    local_db_name = "adb_media_index.sqlite"
    db = None
    
    # Try to pull existing index if available
    with tempfile.TemporaryDirectory(prefix="adb_index_") as itmp:
        itp = Path(itmp)
        ldb = itp / local_db_name
        try:
            adb_pull(remote_db, ldb)
            db = MediaDB(ldb)
            print("[*] Found existing media index on device. Using cached metadata.")
        except:
            # Create fresh local DB to track this session
            db = MediaDB(ldb)

        files_to_process = []
        total_size = 0
        for remote_path, size, mtime in entries:
            if Path(remote_path).name == "media_index.sqlite": continue
            if get_file_age_days(mtime) >= args.age and size >= min_size_bytes:
                rel_path = os.path.relpath(remote_path, args.source)
                probe_cached = ""
                if db:
                    cached = db.get_entry(rel_path)
                    if cached: probe_cached = cached[2]
                
                files_to_process.append((remote_path, Path(remote_path).suffix.lower(), size, rel_path, probe_cached))
                total_size += size

        if not files_to_process:
            print("No files matched criteria on device.")
            return

        if args.index_only:
            print("[!] Index-only mode for ADB requires full pull to probe. This script currently optimizes indexing during the streaming pipeline.")
            print("    Please run a standard compression session (even with extreme limits) to populate the index.")
            return

        print(f"[*] Selected Encoder: {args.encoder}")
        print(f"[*] Found {len(files_to_process)} files on device ({get_size_format(total_size)} total)")
        if input("Proceed to transfer and process streaming pipelines? (y/n): ").lower() != "y":
            sys.exit()

        if args.adb_keep_local:
            Path(args.output).mkdir(parents=True, exist_ok=True)

        ok_count, skip_count, fail_count = 0, 0, 0
        
        # Use a temp directory for staging the individual file cycle
        with tempfile.TemporaryDirectory(prefix="adb_streaming_") as tmpdir:
            td = Path(tmpdir)
            
            # Prepare tasks for the worker
            tasks = []
            for remote_path, ext, size, rel, cached_probe in files_to_process:
                tasks.append((remote_path, ext, size, args.max_width, args.crf, args.max_bitrate, args.encoder, args.output, args.adb_keep_local, str(td)))

            print(f"\n[*] Processing utilizing streaming pipelines (concurrently)...")
            
            with ThreadPoolExecutor(max_workers=3) as executor:
                futures = {executor.submit(adb_streaming_worker, tasks[i]): i for i in range(len(tasks))}
                for future in as_completed(futures):
                    idx = futures[future]
                    task_meta = tasks[idx]
                    rel_path = files_to_process[idx][3]
                    
                    fname, status, orig_sz, comp_sz, probe_info = future.result()
                    
                    # Update local DB index
                    if probe_info and db:
                        w, br = 0, 0.0
                        if "p" in probe_info:
                            try: w = int(probe_info.split("p")[0])
                            except: pass
                        if "@" in probe_info:
                            try: br = float(probe_info.split("@")[1].replace("M", ""))
                            except: pass
                        db.update_entry(rel_path, orig_sz, time.time(), w, br, probe_info)

                    display_name = f"File #{idx+1}" if args.anon else fname[:30]
                    probe_tag = f" [{probe_info}]" if probe_info else ""

                    if status == "success":
                        ok_count += 1
                        print(f"  [OK] Processed & Pushed: {display_name}{probe_tag}")
                    elif status == "saved_locally":
                        ok_count += 1
                        print(f"  [+] Saved Locally: {display_name}{probe_tag}")
                    elif "skipped" in status:
                        skip_count += 1
                        reason = "would grow file" if "larger" in status else "within resolution/bitrate rules"
                        print(f"  [-] Skipped: {display_name}{probe_tag} ({reason})")
                    else:
                        fail_count += 1
                        print(f"  [X] Failed: {display_name} | Reason: {status}")

            # Push index back to device if modified
            if db:
                try:
                    adb_push(ldb, remote_db)
                    print(f"[*] Media index updated on device: {remote_db}")
                except:
                    print("[!] Failed to push media index back to device.")

    print(f"\nDevice Processing Done! Results -> Success: {ok_count} | Skipped: {skip_count} | Failed: {fail_count}")

# =============================================
# Wizard Integration
# =============================================

def ask(prompt, default=None):
    suffix = f" [{default}]" if default is not None else ""
    try: value = input(f"  {prompt}{suffix}: ").strip()
    except (KeyboardInterrupt, EOFError):
        print("\nAborted.")
        sys.exit(0)
    return value if value else (str(default) if default is not None else "")

def ask_choice(prompt, choices, default=None):
    print(f"\n  {prompt}")
    for i, choice in enumerate(choices, 1):
        label = choice[0]
        marker = " (default)" if default is not None and i == default else ""
        print(f"    {i}) {label}{marker}")
    while True:
        raw = ask("Enter number", default=default)
        try:
            idx = int(raw)
            if 1 <= idx <= len(choices):
                choice = choices[idx - 1]
                return choice[1] if len(choice) == 2 else choice[1:]
        except ValueError: pass
        print(f"    [!] Please enter a number between 1 and {len(choices)}.")

def ask_bool(prompt, default=True):
    hint = "Y/n" if default else "y/N"
    raw = ask(f"{prompt} ({hint})", default="y" if default else "n").lower()
    return raw in ("y", "yes", "") if default else raw in ("y", "yes")

def guided_setup(ns=None):
    if ns is None:
        ns = argparse.Namespace(
            adb=False, encoder="libx264", overwrite=False, output=None,
            adb_keep_local=False, age=30, min_size=0.0, recursive=False, 
            max_width=1920, crf=28, max_bitrate=None, anon=False, source=None,
            index_only=False
        )

    print("\n========================================")
    print("    Parallel Media Compressor Wizard    ")
    print("========================================\n")

    default_mode = 2 if ns.adb else 1
    mode = ask_choice("Where are your targets located?", [("Local Disk Drive Folder", "local"), ("Android ADB USB Mount Path", "adb")], default=default_mode)
    ns.adb = (mode == "adb")
    
    ns.index_only = ask_bool("Perform Probing/Indexing ONLY? (No compression, just update SQLite)", default=ns.index_only)

    if not ns.index_only:
        default_encoder_idx = 1
        for idx, (val, _) in enumerate(_ENCODER_CANDIDATES, 1):
            if val == ns.encoder:
                default_encoder_idx = idx
                break
        ns.encoder = ask_choice("Select Codec Video Processing Pipeline Target Engine:", _ENCODER_CANDIDATES, default=default_encoder_idx)

    if ns.adb:
        ns.source = ask("Device path to scan", default=ns.source or "/sdcard/DCIM/Camera")
    else:
        ns.source = ask("Local folder directory absolute path", default=ns.source or "")
        # Expand ~ and make absolute
        if ns.source:
            ns.source = os.path.abspath(os.path.expanduser(ns.source))
        
        if not ns.source or not Path(ns.source).is_dir():
            print(f"  [!] Valid source directory parameters required to map targets. (Input: {ns.source})"); sys.exit(1)

    ns.recursive = ask_bool("Traverse sub-directories recursively?", default=ns.recursive)

    if not ns.index_only:
        if ns.adb:
            default_dest = 2 if ns.adb_keep_local else 1
            dest = ask_choice("What should happen to successful processed configurations?", [("Push back to rewrite storage directly (Destructive)", "push"), ("Stage into local environment workspace", "keep")], default=default_dest)
            if dest == "keep":
                ns.adb_keep_local = True
                ns.output = ask("Local path directory context location", default=ns.output or "./compressed")
            else:
                ns.adb_keep_local = False
        else:
            default_dest = 2 if ns.overwrite else 1
            dest = ask_choice("File mapping save architecture paradigm?", [("Write safe context inside isolated export target directory", "folder"), ("Overwrite local items in place (Destructive)", "overwrite")], default=default_dest)
            if dest == "folder":
                ns.overwrite = False
                ns.output = ask("Export destination path", default=ns.output or "./compressed")
            else:
                if not ask_bool("Destructive configurations confirmed? Backups recommended.", default=False): sys.exit(0)
                ns.overwrite = True

    raw_age = ask("Minimum lifetime duration age filters (Days)", default=ns.age)
    ns.age = int(raw_age) if str(raw_age).isdigit() else 30
    
    raw_size = ask("Minimum target filtering sizing file thresholds (MB)", default=ns.min_size)
    try: ns.min_size = float(raw_size)
    except: ns.min_size = 0.0

    if not ns.index_only:
        default_res_idx = 3
        for idx, (_, w, _, _) in enumerate(_RESOLUTION_TEMPLATES, 1):
            if w == ns.max_width:
                default_res_idx = idx
                break
        
        selected_res = ask_choice("Select maximum video width resolution:", _RESOLUTION_TEMPLATES, default=default_res_idx)
        ns.max_width = selected_res[0]
        ns.max_bitrate = selected_res[1]
        template_crf = selected_res[2]

        raw_crf = ask("Video processing CRF resolution density boundaries (0-51)", default=template_crf)
        ns.crf = int(raw_crf) if str(raw_crf).isdigit() else template_crf

    ns.anon = ask_bool("Enable Anonymity Mode? (Hide filenames in logs)", default=ns.anon)

    print("\nStarting execution routines matrix matching configurations setup requirements...")
    if not ask_bool("Confirm deployment setup pipeline initialization routines?", default=True): sys.exit(0)
    return ns

# =============================================
# Entry Main Loop Engine
# =============================================

def main():
    parser = argparse.ArgumentParser(description="Multiprocess engine utilities compressing assets safely across devices.")
    parser.add_argument("source", nargs="?", help="Local folder source directory path or target location device route descriptor.")
    
    output_group = parser.add_mutually_exclusive_group()
    output_group.add_argument("-o", "--output", help="Export matching destination path.")
    output_group.add_argument("--overwrite", action="store_true", help="Overwrite sources directly inline during local runs.")

    parser.add_argument("-e", "--encoder", choices=[c[0] for c in _ENCODER_CANDIDATES], help="Direct manual configuration selecting exact target encoder instead of relying on custom profile configurations.")
    parser.add_argument("-a", "--age", type=int, default=30)
    parser.add_argument("--min-size", type=float, default=0)
    parser.add_argument("--recursive", action="store_true")
    parser.add_argument("-r", "--max-width", type=int, default=1920)
    parser.add_argument("-c", "--crf", type=int, default=28)
    parser.add_argument("--max-bitrate", type=float, default=0)
    parser.add_argument("--anon", action="store_true", help="Hide filenames in logs and use sequential numbers instead.")
    parser.add_argument("--index-only", action="store_true", help="Only probe and index files into SQLite, do not compress.")
    
    adb_group = parser.add_argument_group("ADB configurations")
    adb_group.add_argument("--adb", action="store_true")
    adb_group.add_argument("--adb-keep-local", action="store_true")

    args = parser.parse_args()

    # Trigger wizard if source is missing, even if other flags are present
    if not args.source:
        args = guided_setup(args)

    need_adb = args.adb or "--adb" in sys.argv
    check_dependencies(need_adb=need_adb)

    if not args.source:
        parser.error("Source configurations required path arguments to run validation tasks properly.")

    if not args.encoder:
        args.encoder = "libx264"

    if args.adb:
        if args.overwrite: parser.error("Destructive configurations cannot enforce safety targets across adb connections automatically.")
        if args.adb_keep_local and not args.output: parser.error("Output parameters directory missing designation requirements maps.")
        run_adb(args)
    else:
        if not args.index_only and not args.overwrite and not args.output: parser.error("Provide destination directory paths or switch on explicit dangerous execution settings.")
        run_local(args)

if __name__ == "__main__":
    main()
