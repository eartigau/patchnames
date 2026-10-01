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


## Keywords with Claude

`patchnames keywords` describes the bird photos of the current directory
with Claude and writes the result into the files themselves (EXIF, IPTC and
XMP): a title, the species (French and Latin names), the habitat, a short
description in French, and a Lightroom hierarchy (Espèce, Espèce latine,
Habitat). An uncertain identification gets the keyword
`identification à confirmer`, with the candidates in the description.

```bash
patchnames keywords              # the bird photos not yet described
patchnames keywords --dry-run    # show Claude's answers, write nothing
patchnames --keywords            # describe, then rename and archive as usual
```

- **What is looked at:** only the photos taken with a bird lens (config
  `bird_lenses`; `--all-lenses` for all), and not the ones that already have a
  title (`--force` to redo them).
- **Sequences:** the photos are grouped into sequences (a new one after 30 s
  without a shot, `--gap`). Claude looks at the middle frame of each sequence,
  at a crop of it at full resolution around the camera's focus point (where a
  distant bird is large enough to identify; the centre of the frame when the
  file has no focus data) and at a contact sheet of all its frames, and its
  answer is written into every frame: a burst of a hundred frames costs one
  call.
- **Your explanations:** `~/.config/patchnames/keywords.md` (written on the
  first run, yours to edit) is passed to Claude as it is: region, naming
  conventions (French names of the Québec list, Latin names of eBird/Clements),
  how prudent to be, habitat words, description style.
- **Needs** the `claude` command (Claude Code) logged in: it runs in print
  mode, allowed only to read the images (`--model` to pick a model). About 20 s
  per sequence.
- **Backups:** exiftool keeps each original as `<file>_original`
  (`--no-backup` to skip; `exiftool -delete_original .` removes them later).
- Set `"keywords_auto": true` in the config to describe before every
  `patchnames` run.

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
