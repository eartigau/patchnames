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

# The same, with every photo described by Claude and the birds identified
# with your eBird checklists (see below), and the page to search them
patchdoc
patchdoc gui
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

## patchdoc: every photo described, the birds identified with eBird

`patchdoc` does what `patchnames` does (rename, archive) and, on the way,
has Claude describe every photo. A photo taken beyond 200 mm is looked at as
a bird photo: Claude tries to name the species, with your eBird checklists as
a prior. The result goes into the files themselves and into an index, which
`patchdoc gui` searches by species, place, date, subject or any word.

```bash
cd /path/to/new/photos
patchdoc --dry-run     # show what Claude says, change nothing
patchdoc               # describe, write into the files, rename, archive, index
patchdoc gui           # the search page, in the browser
```

How it works:

- **Sequences.** The photos are grouped into sequences (a new one after 30 s
  without a shot, `--gap`), the bird photos (focal length above
  `bird_focal_min`, 200 mm; `--min-focal`) apart from the others. One Claude
  call per sequence, three at a time (`--jobs`): about 20 s and $0.10 at API
  prices per sequence, whatever its length.
- **eBird as a prior.** Most bird photos are taken while a checklist is being
  filled. The checklist whose hours hold a sequence gives its place and the
  species noted there; Claude gets that list (and the other lists of the day)
  with the photo. It is a prior, not the answer: Claude is told that the
  list may be wrong or incomplete, names what it sees, and says in one
  sentence when the photo does not fit the list. A sequence whose species is
  not on its checklist is flagged (`absent de la liste eBird`), one that is
  not sure gets `identification à confirmer`.
- **A camera clock that is off.** A camera left on the time of another
  country, or on the wrong side of a daylight saving change, is off by whole
  hours. For each day, the offset that puts the most sequences inside
  checklists is looked for and applied before matching, and said at the
  start of the run. It has to make sense with the time zone the camera
  records (a Nikon Z does): a camera on Montréal time in Sweden is 6 h
  behind, not 3. A camera that records no zone only gets a correction that
  holds over several days; `--clock 6` sets it yourself (`--clock 0` for
  none).
- **What is written into each file** (as `patchnames keywords` does, with
  exiftool; `--no-backup` to skip the `<file>_original` copies): title,
  description, keywords (species in French and Latin, habitat, subject), a
  Lightroom hierarchy (Espèce, Espèce latine, Habitat, Sujet), the place
  (location, county, state, country) and the checklist as the IPTC event
  (`eBird S123456789`).
- **Your explanations** are the same file as for `patchnames keywords`:
  `~/.config/patchnames/keywords.md` (`--instructions` for another one).
- A sequence Claude could not describe stays in the folder: run `patchdoc`
  again. Photos that already have a title are not described again
  (`--force`), only indexed. `--index-only` describes and indexes without
  touching the files, to try it in a folder of the archive.

### The eBird data

`patchdoc` uses the newest "Download My Data" export it finds: a
`ebird_*.zip` in `~/Downloads` (config `ebird_search`) is copied to
`~/.config/patchnames/ebird/`. When the photos are newer than the export, a
new one is asked for before describing (`--no-refresh` to skip, `--refresh`
to force):

```bash
patchdoc ebird             # what data is on disk, which accounts are known
patchdoc ebird --login     # keep the eBird and mailbox accounts in the macOS keychain
patchdoc ebird --refresh   # ask eBird for a new export and wait for it
```

eBird sends the link of an export by email, a few minutes after the request.
With the eBird account in the keychain, the request is sent for you;
without it, the eBird page opens in the browser and you click "Request My
Observations". With the mailbox account in the keychain (IMAP; Proton Mail
Bridge on `127.0.0.1:1143` by default, config `imap_host` and `imap_port`),
the link is read from the email and the zip downloaded; without it, open the
link yourself: the zip is picked up when it lands in `~/Downloads`. The
passwords live in the macOS keychain only (or in `PATCHNAMES_EBIRD_USER`,
`PATCHNAMES_EBIRD_PASSWORD`, `PATCHNAMES_IMAP_USER`,
`PATCHNAMES_IMAP_PASSWORD`), never in a file.

### The search page

`patchdoc gui` opens http://127.0.0.1:8765/ (`--port`), served to this
machine only. Each tile is a sequence; a click shows the photo, the full
description, the species, the place, the eBird checklist with its species,
and the file (Show in Finder, Open). Filters: free text, species, place,
region, year and dates, subject, telephoto or not, and what needs a look
(identification to confirm, species not on the checklist, bird photos
without a checklist).

The index, with its thumbnails and previews, is in `~/photo/patchdoc`
(config `index_dir`), so searching needs neither the archive drive nor the
raw files. Two ways to fill it with what is already archived:

```bash
patchdoc index                                    # the whole archive, from the file names alone
patchdoc index --exif ~/mnt/URUBU/photo/2026_10   # read what the files of a folder say of themselves
```

The first takes a few minutes: the name of a photo gives its date and time,
its folder says whether it was taken with a bird lens, and the eBird
checklists give the place. Such photos are not described, but they can be found by place and
date, and by the species of their checklist (the box under the filters). The
second reads the title, description and keywords already written into the
files (by `patchdoc` or `patchnames keywords`), and the camera and lens;
count a second per photo on a network drive.

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
  "bird_lenses": ["200-500mm", "300mm"],
  "bird_focal_min": 200,
  "index_dir": "~/photo/patchdoc",
  "ebird_dir": "~/.config/patchnames/ebird",
  "ebird_search": ["~/Downloads/ebird_*.zip", "~/Downloads/MyEBirdData*.csv"]
}
```

`PATCHNAMES_CONFIG=/path/to/other.json` uses another config file, to try
things away from the archive.

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
