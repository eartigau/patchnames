# patchnames

Rename and archive photo/video files using EXIF metadata.

**Canonical filename format:** `YYMMDD_HHMMSS[_WHO]_FILENUM.ext`

```
260429_143022_EA_0234.nef
260429_143022_0234.jpg
```

## Installation

```bash
pip install git+https://github.com/eartigau/patchnames.git
```

Then create your config file:

```bash
patchnames init-config
# Edit ~/.config/patchnames/config.json to match your setup
```

## Dependencies

- [`exiftool`](https://exiftool.org) must be on your PATH: `brew install exiftool`
- Adobe DNG Converter (optional, only needed for `nef-to-dng`)

## Usage

```bash
# Rename + archive all photos in the current directory
patchnames

# Convert Nikon Z50 II NEF → DNG
patchnames nef-to-dng

# Fix timestamps (camera was in wrong timezone)
patchnames time-offset '*.NEF' +=02:06:00
patchnames time-offset '*.NEF' -=00:30:00
patchnames time-offset '*.NEF'      # prompts interactively

# Force a date on files with no EXIF date
patchnames patchdate '*.jpg'        # prompts for YYMMDD
```

## Configuration

`~/.config/patchnames/config.json`:

```json
{
  "exif_cache_dir": "~/photo/exifdata",
  "photo_archive_root": "~/mnt/URUBU/photo",
  "dng_converter": "/Applications/Adobe DNG Converter.app/Contents/MacOS/Adobe DNG Converter",
  "extensions": ["*.NEF", "*.JPG", "*.HEIC", "*.MP4", "*.DNG", "*.MOV", "*.DCM", "*.PSD"],
  "camera_owners": {
    "EA": ["D90", "NIKON Z 50", "NIKON Z50_2"]
  },
  "bird_lenses": ["200-500mm", "300mm"]
}
```

### Archive layout

When `photo_archive_root` is mounted, files are moved to:

```
photo_archive_root/
  20YY_MM/
    oiseaux/   # shots with a telephoto lens (≥300 mm)
    autres/    # everything else
```

## Python API

```python
from patchnames import patchnames, time_offset, patchdate, nef_to_dng
```
