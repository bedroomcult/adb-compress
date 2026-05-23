# Parallel Media Compressor (ADB & Local)

A high-performance, multi-process media utility designed to compress large image and video libraries while maintaining quality. Built for both standard local storage and Android devices via ADB.

## ✨ Key Features

-   **Streaming ADB Engine**: Processes files one-by-one (Pull → Process → Push). Zero bulk staging = low local disk requirement.
-   **SQLite Media Indexing**: Persistent database (`media_index.sqlite`) tracks metadata (mtime, size, resolution, bitrate). 
-   **Smart Change Detection**: Auto-detects modified files and re-probes only when necessary.
-   **YouTube-Standard Presets**: Built-in templates (4K to 480p) with recommended Bitrate and CRF settings.
-   **Hardware Acceleration**: Automatically detects and uses GPU encoders (NVENC, AMF, QSV, VideoToolbox).
-   **Anonymity Mode**: Mask filenames in logs with sequential numbering (`File #1`, etc.) for privacy.
-   **Robust Probing**: Displays resolution and bitrate for all files, including skipped ones.
-   **Safe Abort**: Graceful termination with 'Q' key or Ctrl+C.

## 🚀 Installation

### 1. Requirements
-   **Python 3.8+**
-   **FFmpeg & FFprobe**: Required for video processing.
-   **ADB**: Required for Android phone support.
-   **Pillow**: Required for image processing.

### 2. Setup
```bash
# Clone the repo
git clone https://github.com/bedroomcult/adb-compress.git
cd adb-compress

# Install Python dependencies
pip install Pillow
```

## 📖 Usage

### Interactive Wizard (Recommended)
Simply run the script without arguments to start the guided setup:
```bash
python adb-compress.py
```

### Command Line Examples

**Local Compression:**
```bash
# Compress local folder to 1080p template
python adb-compress.py /path/to/media -o ./compressed
```

**ADB (Phone) Compression:**
```bash
# Pull from phone, compress to 720p, push back (overwrites originals)
python adb-compress.py /sdcard/DCIM/Camera --adb
```

**Indexing Only (Probing):**
```bash
# Only populate the database with metadata, no compression
python adb-compress.py /sdcard/DCIM/Camera --adb --index-only
```

**Anonymity Mode:**
```bash
# Hide filenames in console output
python adb-compress.py /path/to/media -o ./out --anon
```

## 🗃️ Database (`media_index.sqlite`)
The script generates a small SQLite database in your source directory.
-   **Local**: Stored directly in the media folder.
-   **ADB**: Pulled to PC during session, updated, and pushed back to phone.
-   **Benefit**: Instant filtering by age/size and lightning-fast startups for large folders.

## 🛠️ Configuration
| Argument | Description | Default |
| :--- | :--- | :--- |
| `source` | Local or Remote path to scan | (Required) |
| `-o`, `--output` | Destination for compressed files | None |
| `--overwrite` | Overwrite local files in-place | False |
| `-e`, `--encoder` | Select ffmpeg encoder | libx264 |
| `-a`, `--age` | Minimum file age (days) | 30 |
| `--min-size` | Minimum file size (MB) | 0 |
| `-r`, `--max-width`| Max resolution width | 1920 |
| `-c`, `--crf` | Quality (0-51, lower is better) | 28 |
| `--anon` | Enable Anonymity Mode | False |
| `--index-only` | Perform probing only | False |
| `--adb` | Enable ADB mode | False |

## ⚖️ License
MIT License. See `LICENSE` for details.
