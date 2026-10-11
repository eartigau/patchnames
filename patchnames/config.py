import json
import os
from pathlib import Path

# PATCHNAMES_CONFIG: another config file (to try things away from the archive)
CONFIG_PATH = Path(os.environ.get('PATCHNAMES_CONFIG')
                   or Path.home() / '.config' / 'patchnames' / 'config.json')

DEFAULTS = {
    'exif_cache_dir': str(Path.home() / 'photo' / 'exifdata'),
    'photo_archive_root': str(Path.home() / 'mnt' / 'URUBU' / 'photo'),
    # other places the same archive may be mounted at (the drive plugged in
    #   directly, another network mount): the first one that is there is used
    'photo_archive_mounts': [],
    'dng_converter': '/Applications/Adobe DNG Converter.app/Contents/MacOS/Adobe DNG Converter',
    'extensions': ['*.NEF', '*.JPG', '*.HEIC', '*.MP4', '*.DNG', '*.MOV', '*.DCM', '*.PSD'],
    'camera_owners': {
        'EA': ['D90', 'D100', 'NIKON Z 50', 'NIKON Z50_2', 'SM-J320W8', 'SGH-S730M', 'SM-J120W', 'HTC Desire C'],
        'MY': ['Canon PowerShot SX110 IS', 'PowerShot A70', 'PowerShot SX620'],
        'JO': ['DSC-H300'],
        'HA': ['iPhone 7', 'Nokia 1 Plus'],
        'PP': ['NIKON D600'],
        'AA': ['Lumia 530'],
        'MA': ['DMC-FZ200'],
    },
    'bird_lenses': ['200-500mm', '300mm'],
    # patchnames keywords: Claude describes the bird photos (keywords.py)
    'keywords_instructions': str(Path.home() / '.config' / 'patchnames' / 'keywords.md'),
    'keywords_gap': 30,
    'keywords_model': None,
    'keywords_auto': False,
    # patchdoc: every photo described by Claude, the birds identified with the
    #   eBird checklists as a prior (doc.py), and the search page (gui.py)
    'bird_focal_min': 200,      # mm: a photo taken beyond is looked at as a bird photo
    'doc_jobs': 3,              # Claude calls at the same time
    'doc_effort': None,         # low, medium, high...: None for the default of claude
    'doc_budget': 10,           # dollars at API prices a run may spend on Claude; 0: no limit
    'index_dir': str(Path.home() / 'photo' / 'patchdoc'),   # the index, thumbnails, previews
    'gui_port': 8765,
    'gui_fill_thumbs': True,    # the search page makes the missing thumbnails in the background
    'gui_backup': False,        # keep <file>_original when the search page writes into a file
    'raw_app': 'Affinity',      # the application of the "Ouvrir avec" button of a raw file
    # the eBird data (ebird.py)
    'ebird_dir': str(Path.home() / '.config' / 'patchnames' / 'ebird'),
    'ebird_search': ['~/Downloads/ebird_*.zip', '~/Downloads/MyEBirdData*.csv'],
    'ebird_refresh': True,      # ask eBird for a new export when photos are newer than the data
    'imap_host': '127.0.0.1',   # Proton Mail Bridge
    'imap_port': 1143,
    'imap_folders': ['INBOX', 'All Mail'],
}


def load():
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH) as f:
            cfg = json.load(f)
        for k, v in DEFAULTS.items():
            if k not in cfg:
                cfg[k] = v
        return cfg
    return dict(DEFAULTS)


def archive_roots(cfg=None):
    """the places the archive may be mounted at: 'photo_archive_root', then
    each of 'photo_archive_mounts'"""
    cfg = cfg or load()
    places = [cfg['photo_archive_root']] + list(cfg.get('photo_archive_mounts') or [])
    return [os.path.expanduser(place).rstrip(os.sep) for place in places if place]


def archive_root(cfg=None):
    """the archive where it is mounted now; its usual place when it is not"""
    roots = archive_roots(cfg)
    return next((root for root in roots if os.path.isdir(root)), roots[0])


def init():
    if CONFIG_PATH.exists():
        print(f'Config already exists at {CONFIG_PATH}')
        print('Edit it directly or delete it and re-run to reset to defaults.')
        return
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_PATH, 'w') as f:
        json.dump(DEFAULTS, f, indent=2)
    print(f'Default config written to {CONFIG_PATH}')
    print('Edit it to match your setup.')
    from .keywords import write_template
    write_template()
