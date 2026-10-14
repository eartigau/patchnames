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
    # where photos wait before they are sent to the archive: the search page
    #   can look at the files directly in these folders (not in their own
    #   subfolders), to describe and note them where they are
    'inbox_dirs': [str(Path.home() / 'photo'), str(Path.home() / 'photo' / 'oiseaux'),
                   str(Path.home() / 'photo' / 'autres')],
    # how rsync reaches a copy of the archive that is on a server, without
    #   going through its mount: {"~/mnt/URUBU/photo": "user@host:/path/photo"}
    #   (a copy mounted with sshfs is found without it while it is mounted)
    'photo_archive_remotes': {},
    # the copy to use when several are there (the search page sets it)
    'photo_archive_use': None,
    # exiftool run on the server of an archive mounted with sshfs (remote.py):
    #   None: when the server has one; False: never; or its path there
    'remote_exiftool': None,
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
    'doc_budget': 0,            # dollars at API prices a run may spend on Claude; 0: no limit
    'index_dir': str(Path.home() / 'photo' / 'patchdoc'),   # the index, thumbnails, previews
    'gui_port': 8765,
    'gui_fill_thumbs': True,    # the search page makes the missing thumbnails in the background
    'gui_backup': False,        # keep <file>_original when the search page writes into a file
    'raw_app': 'Affinity',      # the application of the "Ouvrir avec" button of a raw file
    'geocode': True,            # ask OpenStreetMap for the place of a photo that has a position
    'gui_scan_faces': True,     # the search page looks for the faces of the photos in the background
    'doc_eye_region': True,     # write where the eye of the bird is into the file, as an XMP
                                #   region named "œil" (the index has it in any case)
    'doc_eyes_model': None,     # the model of the second look at a burst of bird photos (the
                                #   eye of each frame); None: the same as the first look
    'people': [],               # the names the search page offers first for a face (your own
                                #   config file is the place for them)
    # the cameras the eBird checklists are kept with, as a regular expression on
    #   the name a file gives of its camera (with the maker's name or without it,
    #   as some software leaves it): only their photos are tied to a checklist,
    #   for their time and their place (doc.anchored). The D100, the D90, the
    #   Coolpix (E4500...) and the two Z 50
    'ebird_cameras': r'\bD100\b|\bD90\b|COOLPIX|^E\d{3,4}$|\bZ ?50(_2)?$',
    # the model and the effort of Claude when it looks for photos on their side
    #   (doc.orient): Haiku misses some that Sonnet finds
    'orient_model': 'sonnet',
    'orient_effort': 'low',
    # the backup of the index on GitHub (backup.py): the private repository it
    #   goes to, 'account/name' (None: patchdoc-sauvegarde of the account gh is
    #   logged in to), and how many backups are kept there
    'backup_repo': None,
    'backup_keep': 5,
    'pano_size': 3000,          # long side of the frames of a panorama when stitched; 0: full size
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
    """the places the archive may be at: 'photo_archive_root', then each of
    'photo_archive_mounts' (the same archive mounted elsewhere, or a copy of
    it on another drive); the one chosen in the search page,
    'photo_archive_use', comes first"""
    cfg = cfg or load()
    places = [cfg['photo_archive_root']] + list(cfg.get('photo_archive_mounts') or [])
    roots = [os.path.expanduser(place).rstrip(os.sep) for place in places if place]
    chosen = os.path.expanduser(cfg.get('photo_archive_use') or '').rstrip(os.sep)
    return sorted(roots, key=lambda root: root != chosen)


def save(changes):
    """keep these keys in the config file and leave the others as they are
    (the file is made if there is none)"""
    cfg = {}
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH) as f:
            cfg = json.load(f)
    cfg.update(changes)
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG_PATH.with_name(CONFIG_PATH.name + '.tmp')
    with open(tmp, 'w') as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)
        f.write('\n')
    os.replace(tmp, CONFIG_PATH)


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
