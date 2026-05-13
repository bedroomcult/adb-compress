# adb-compress.py

Batch-compress images and videos on your PC, with optional Android phone support via ADB. Heavy encoding runs on the PC — not on your phone.

---

## Features

- Compress `.jpg`, `.jpeg`, `.png`, `.webp`, `.mp4`, `.mkv`, `.mov`, `.avi`
- Auto-detects GPU encoder (NVIDIA, AMD, Intel, Apple Silicon) with CPU fallback
- ADB mode: pull files from phone → compress on PC → push back (or save locally)
- Live per-file progress bar with `fps` and `speed` readout
- Filter by minimum file age and minimum file size
- Interactive guided setup wizard when run with no arguments
- Cancel at any time with `Q + Enter` — partial files and temp cache are cleaned up automatically
- Startup dependency check with platform-specific install instructions

---

## Requirements

### Python packages

```
pip install Pillow
```

### External tools

| Tool | Purpose | Required |
|------|---------|----------|
| `ffmpeg` | Video compression | Yes |
| `ffprobe` | Duration probe for % progress bar | Recommended (ships with ffmpeg) |
| `adb` | Android phone file transfer | ADB mode only |

#### Install ffmpeg

**Windows**
```
scoop install ffmpeg
# or
winget install Gyan.FFmpeg
```

**macOS**
```
brew install ffmpeg
```

**Linux**
```
sudo apt install ffmpeg        # Ubuntu / Debian
sudo dnf install ffmpeg        # Fedora
sudo pacman -S ffmpeg          # Arch
```

#### Install adb (Android Debug Bridge)

**Windows**
```
scoop install adb
# or
winget install Google.PlatformTools
```

**macOS**
```
brew install android-platform-tools
```

**Linux**
```
sudo apt install adb           # Ubuntu / Debian
sudo pacman -S android-tools   # Arch
```

Then enable USB Debugging on your phone:
> Settings → About phone → tap **Build number** 7 times → Developer options → enable **USB Debugging**

---

## Usage

### Guided wizard (recommended for first-time use)

Run with no arguments to get an interactive step-by-step setup:

```
python compress_media.py
```

The wizard walks through: mode, source path, recursive scan, output destination, filters, and quality settings, then prints a summary before starting.

---

### Local mode

Compress files on your PC.

**Save to a separate output folder (safe):**
```
python compress_media.py /path/to/media -o /path/to/output
```

**Overwrite originals in-place ⚠:**
```
python compress_media.py /path/to/media --overwrite
```

---

### ADB mode

Compress files from your Android phone. Files are pulled to a temporary folder on your PC, compressed, then pushed back.

**Pull → compress → push back to phone (replaces originals on device):**
```
python compress_media.py /storage/emulated/0/DCIM/Camera --adb
```

**Pull → compress → save to local folder (don't push back):**
```
python compress_media.py /storage/emulated/0/DCIM/Camera --adb --adb-keep-local -o ./compressed
```

In guided wizard mode, the script queries the connected device and presents a folder picker so you don't need to type paths manually:

```
  🔍 Scanning device for media folders...

  Select a folder on your device:
    1) Camera roll  (/storage/emulated/0/DCIM/Camera)   (default)
    2) All DCIM  (/storage/emulated/0/DCIM)
    3) Downloads  (/storage/emulated/0/Download)
    4) WhatsApp Media  (/storage/emulated/0/Android/media/com.whatsapp/WhatsApp/Media)
    5) Enter path manually
```

---

## All options

```
usage: compress_media.py [source] [options]

positional arguments:
  source                Local directory or device path (with --adb)

output:
  -o, --output DIR      Output directory for compressed files
  --overwrite           Overwrite originals in-place (local mode only, DANGEROUS)

filtering:
  -a, --age DAYS        Minimum file age in days (default: 30)
  --min-size MB         Minimum file size in MB (default: 0)
  --recursive           Include subdirectories

quality:
  -r, --res WIDTH       Target width in pixels (default: 1280)
  -c, --crf N           Video CRF value 0–51, lower = better (default: 28)

ADB:
  --adb                 Enable ADB mode
  --adb-keep-local      Save compressed files locally instead of pushing back
                        (requires --output)
```

---

## GPU auto-detection

At startup the script probes ffmpeg with a 1-frame encode to find the best available encoder. The result is cached for the session.

| Priority | Encoder | Hardware |
|----------|---------|----------|
| 1 | `h264_nvenc` | NVIDIA GPU |
| 2 | `h264_amf` | AMD GPU |
| 3 | `h264_qsv` | Intel iGPU / Arc |
| 4 | `h264_videotoolbox` | Apple Silicon / macOS |
| 5 | `libx264` | CPU (fallback) |

The detected encoder is printed before processing begins. CRF values are automatically remapped to the equivalent quality flag for each encoder (`-cq` for NVENC, `-global_quality` for QSV, etc.).

---

## Progress output

While a video encodes:

```
[2/5] 🔧 holiday_clip.mp4  [████████░░░░░░░░░░░░]  42%  |  fps=87  speed=3.6x  (Q+Enter to cancel)
```

If `ffprobe` is unavailable, the bar falls back to a raw timestamp (`00:00:05.92`) instead of a percentage.

---

## Cancelling

Press **`Q` then `Enter`** at any time during processing.

- The current ffmpeg process is killed immediately
- Any partial output file is deleted
- In ADB mode, the temporary pull/compress cache is fully cleared
- Already-completed files are not affected

```
🛑 Cancelled after 3 file(s). Partial files removed.
```

> **Why Q + Enter instead of Ctrl+C?**
> On Windows, Ctrl+C can kill the ffmpeg subprocess instead of Python, leaving orphaned temp files. Q + Enter uses a background thread and always cleans up correctly on all platforms.

---

## ADB workflow detail

```
For each file on the device:
  1. adb pull  →  temp folder on PC
  2. compress  →  GPU/CPU encodes locally
  3. adb push  →  back to original path on device
     (or save to local output folder with --adb-keep-local)
  4. temp files deleted immediately after each file
```

The temp folder (`adb_compress_*` in the system temp directory) is managed by Python's `TemporaryDirectory` and is always cleaned up on exit, even after a crash.

---

## Supported formats

| Type | Extensions |
|------|-----------|
| Images | `.jpg` `.jpeg` `.png` `.webp` |
| Videos | `.mp4` `.mkv` `.mov` `.avi` |

Images are resized to the target width (if larger) and saved at quality 85 using Pillow's LANCZOS resampling. Videos are re-encoded with H.264 at the target width and CRF value.

---

## Common Android media paths

| Folder | Path |
|--------|------|
| Camera roll | `/storage/emulated/0/DCIM/Camera` |
| All DCIM | `/storage/emulated/0/DCIM` |
| Downloads | `/storage/emulated/0/Download` |
| Pictures | `/storage/emulated/0/Pictures` |
| WhatsApp | `/storage/emulated/0/Android/media/com.whatsapp/WhatsApp/Media` |
| Telegram | `/storage/emulated/0/Telegram` |
| SD card | `/storage/XXXX-XXXX/DCIM` (ID varies by device) |

If `/storage/emulated/0/` paths don't work, try `/sdcard/` — they point to the same location on most devices.
