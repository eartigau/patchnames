"""
The memory card of a camera put in this machine, and the button of the
search page that copies what is new on it to the folder where photos wait
to be sent to the archive (the first of config 'inbox_dirs', ~/photo): the
first of the buttons under "Regarder" when the page shows those photos.

A card is a volume of /Volumes with a DCIM folder at its root, where every
camera writes its photos (DCIM/101NZ502...), that is not a drive of the
archive. Its files are those of the extensions of config 'extensions', in
DCIM and its folders.

look()   reads the card with exiftool (half a minute for 2000 raw files; a
         file read once is not read again while it stays as it is) and says
         which of its files are new. A file is not new when:
           - the button copied it before: the table from_card of the index
             keeps each file it copied (its camera, date, name and size on
             the card), so that a photo deleted since, from ~/photo or from
             the archive, does not come back;
           - the index has a photo of the same owner (core.who), file number
             and extension, taken a whole number of quarter hours from the
             date the camera gave it, SHIFT hours at most: the photos copied
             by hand before the button, and those put to the hour since
             (inbox.times), whose names changed with their hour;
           - a file of that name and size is in ~/photo already.
         The index has no videos: a video copied by hand is not known.
copy()   copies the new files to ~/photo under the names they have on the
         card, with their dates; the other buttons then put their hours
         right, name them and send them to the archive. Nothing is deleted
         from the card nor written over: a file whose name is taken by
         another one, in ~/photo or in the index, is left on the card and
         counted. A copy that would leave less than ROOM on the disk of
         ~/photo does not start.

What is copied is added to inbox.log, in the folder of the index.
"""

import os
import re
import shutil
from datetime import datetime

from . import config as _config_module
from . import core
from . import doc
from . import inbox
from . import photoindex
from . import remote

#: where the volumes are mounted
VOLUMES = '/Volumes'
#: what exiftool is asked of each file of a card (-fast: the file number is
#:  in the maker notes, which -fast2 leaves out)
TAGS = ['-fast', '-Model', '-DateTimeOriginal', '-CreateDate', '-FileNumber']
#: a canonical name, with its number: YYMMDD_HHMMSS[_WHO]_NUM.ext
NAME = re.compile(r'^(\d{6})_(\d{6})(?:_([A-Z]{2}))?_([^.]+)\.(\w+)$')
#: how far the hour of a photo may have moved since it left the card, in
#:  hours (the time zones go from -12 to +14), by steps of STEP seconds
SHIFT, STEP = 14, 900
#: what the disk of ~/photo keeps free after a copy, at least, in bytes
ROOM = 2e9

#: what was read of the files of the cards, {path: ((size, mtime), what it is)}
_seen = {}


def cards(cfg=None):
    """the memory cards in this machine: the volumes that have a DCIM folder,
    less the drives the archive is on"""
    if not os.path.isdir(VOLUMES):
        return []
    drives = {os.path.join(VOLUMES, place[len(VOLUMES) + 1:].split(os.sep)[0])
              for place in _config_module.archive_roots(cfg)
              if place.startswith(VOLUMES + os.sep)}
    return [os.path.join(VOLUMES, name) for name in sorted(os.listdir(VOLUMES))
            if not name.startswith('.') and os.path.join(VOLUMES, name) not in drives
            and os.path.isdir(os.path.join(VOLUMES, name, 'DCIM'))]


def name_of(root):
    """what a card is called in the page: its volume ('NIKON Z 50')"""
    return os.path.basename(root).strip() or root


def files(root, cfg=None):
    """the photos and videos of a card, folder after folder"""
    cfg = cfg or _config_module.load()
    wanted = tuple('.' + pattern.rsplit('.', 1)[1].lower() for pattern in cfg['extensions'])
    found = []
    for folder, subs, names in os.walk(os.path.join(root, 'DCIM')):
        subs[:] = sorted(sub for sub in subs if not sub.startswith('.'))
        # (the files ._* are what macOS writes beside a file on such a card)
        found += [os.path.join(folder, name) for name in sorted(names)
                  if name.lower().endswith(wanted) and not name.startswith('.')]
    return found


def forget(root):
    """drop what was read of a card that was taken out"""
    for path in [path for path in _seen if path.startswith(root + os.sep)]:
        del _seen[path]


def destination(cfg=None):
    """the folder the files of a card go to: the first of config 'inbox_dirs'"""
    folders = doc.inbox_dirs(cfg)
    if not folders:
        raise RuntimeError("la config n'a pas de dossier où les photos attendent (inbox_dirs)")
    return folders[0]


def free(cfg=None):
    """the room left on the disk of that folder, in bytes"""
    place = destination(cfg)
    while not os.path.isdir(place):
        place = os.path.dirname(place)
    return shutil.disk_usage(place).free


def _home(path):
    home = os.path.expanduser('~')
    return '~' + path[len(home):] if path.startswith(home + os.sep) else path


def _gb(size):
    """'3,9 Go'"""
    return f'{size / 1e9:.1f} Go'.replace('.', ',')


def _what(path, row, stat):
    """what is known of a file of a card from its tags: its key in from_card,
    its owner, number and extension as its canonical name will have them
    (core.get_exif), and the date its camera gave it"""
    size, mtime = stat
    when = None
    for tag in ('DateTimeOriginal', 'CreateDate'):
        try:
            when = datetime.strptime(str(row.get(tag) or '')[:19], '%Y:%m:%d %H:%M:%S')
            break
        except ValueError:
            continue        # (none, or a video's 0000:00:00)
    if when is None:
        when = datetime.fromtimestamp(mtime).replace(microsecond=0)
    model = str(row.get('Model') or '')
    ext = os.path.splitext(path)[1][1:].lower().replace('jpeg', 'jpg').replace('tiff', 'tif')
    return {'key': f'{model}|{when:%Y-%m-%d %H:%M:%S}|{os.path.basename(path)}|{size}',
            'who': core.who({'CAMERAMODELNAME': model}).lstrip('_') if model else '',
            'number': str(row.get('FileNumber') or '').split('-')[-1], 'ext': ext,
            'when': when, 'size': size}


def _moved(when, stamps):
    """whether one of these canonical dates and hours ('YYMMDDHHMMSS') is the
    date `when` of the camera moved by a whole number of quarter hours"""
    for stamp in stamps:
        try:
            shift = (datetime.strptime(stamp, '%y%m%d%H%M%S') - when).total_seconds()
        except ValueError:
            continue
        if abs(shift) <= SHIFT * 3600 + 2 and abs(shift - round(shift / STEP) * STEP) <= 2:
            return True
    return False


def look(root, tell=None, chunk=200):
    """Read a card and say what is new on it (see the module): {'files',
    'bytes', 'new', 'new_bytes', 'known', 'copied', 'there', 'to'} and
    'todo', the new files as [(path, key, size)], in the order of the card.
    tell(done, total) as its files are read."""
    cfg = _config_module.load()
    paths = files(root, cfg)
    stats = {}
    for path in paths:
        stat = os.stat(path)
        stats[path] = (stat.st_size, stat.st_mtime)
    unread = [path for path in paths if _seen.get(path, (None,))[0] != stats[path]]
    for start in range(0, len(unread), chunk):
        part = unread[start:start + chunk]
        rows = {row['SourceFile']: row for row in remote.rows(TAGS, part)}
        for path in part:
            _seen[path] = (stats[path], _what(path, rows.get(path, {}), stats[path]))
        if tell:
            tell(min(start + chunk, len(unread)), len(unread))
    info = {path: _seen[path][1] for path in paths}
    dest = destination(cfg)
    db = photoindex.connect()
    try:
        copied = {row[0] for row in db.execute('SELECT key FROM from_card')}
        # the photos of the index that may be these files: their dates and
        #   hours, by owner, number and extension
        wanted = {(what['who'], what['number'], what['ext']) for what in info.values()
                  if what['number']}
        stamps = {}
        for (name,) in db.execute('SELECT name FROM photos'):
            found = NAME.match(name or '')
            if found and (found.group(3) or '', found.group(4), found.group(5)) in wanted:
                stamps.setdefault((found.group(3) or '', found.group(4), found.group(5)),
                                  []).append(found.group(1) + found.group(2))
    finally:
        db.close()
    out = {'files': len(paths), 'bytes': sum(size for size, _ in stats.values()), 'new': 0,
           'new_bytes': 0, 'known': 0, 'copied': 0, 'there': 0, 'to': _home(dest), 'todo': []}
    for path in paths:
        what = info[path]
        here = os.path.join(dest, os.path.basename(path))
        if what['key'] in copied:
            out['copied'] += 1
        elif what['number'] and _moved(what['when'], stamps.get(
                (what['who'], what['number'], what['ext']), [])):
            out['known'] += 1
        elif os.path.isfile(here) and os.path.getsize(here) == what['size']:
            out['there'] += 1
        else:
            out['new'] += 1
            out['new_bytes'] += what['size']
            out['todo'].append((path, what['key'], what['size']))
    return out


def _copy(db, path, key, dest, out):
    """copy one file of a card to `dest`; True when it is there now"""
    name = os.path.basename(path)
    target = os.path.join(dest, name)
    size = os.path.getsize(path)
    if os.path.exists(target):
        if os.path.getsize(target) != size:
            return inbox._left(out, path, f"un autre fichier de ce nom est déjà dans {out['to']}")
        out['there'] += 1
    else:
        known = db.execute('SELECT path FROM photos WHERE name = ?', (name,)).fetchone()
        if known is not None and known['path'] != target:
            return inbox._left(out, path, 'la base a déjà une autre photo de ce nom')
        # copied under another name first: ~/photo never holds half a file
        part = target + '.part'
        try:
            shutil.copyfile(path, part)
            if os.path.getsize(part) != size:
                raise OSError('copie incomplète')
            stat = os.stat(path)
            os.utime(part, (stat.st_atime, stat.st_mtime))
            os.replace(part, target)
        except OSError as exc:
            if os.path.exists(part):
                os.remove(part)
            return inbox._left(out, path, f'la copie a échoué ({exc})')
        out['copied'] += 1
        inbox._log(f'carte  {path} -> {target}')
    db.execute('INSERT OR REPLACE INTO from_card VALUES (?, ?, ?)',
               (key, target, datetime.now().isoformat(' ', 'seconds')))
    db.commit()
    return True


def copy(root, tell=None, stop=None):
    """Copy the new files of a card to ~/photo (see the module). tell(counts)
    after each file, stop: an Event that ends it. Raises RuntimeError when
    the card is not there, or the disk would be left without room. Returns
    {'done', 'total', 'copied', 'there', 'bytes', 'all', 'left', 'problems',
    'to', 'card'}: 'bytes' out of 'all' are through, 'copied' files were
    copied, 'there' were in ~/photo already."""
    cfg = _config_module.load()
    if root not in cards(cfg):
        raise RuntimeError("la carte n'est plus branchée")
    dest = destination(cfg)
    todo = look(root)['todo']
    need = sum(size for _, _, size in todo)
    os.makedirs(dest, exist_ok=True)
    room = free(cfg)
    if need and room - need < ROOM:
        raise RuntimeError(f"il faut {_gb(need)} pour les fichiers de la carte et le disque de "
                           f"{_home(dest)} n'a que {_gb(room)} de libre (patchdoc y laisse "
                           f"toujours {_gb(ROOM)})")
    out = {'done': 0, 'total': len(todo), 'copied': 0, 'there': 0, 'bytes': 0, 'all': need,
           'left': 0, 'problems': [], 'to': _home(dest), 'card': name_of(root)}
    db = photoindex.connect()
    try:
        for path, key, size in todo:
            if stop is not None and stop.is_set():
                break
            if not os.path.isdir(root):
                raise RuntimeError(f"la carte a été retirée après {out['copied']} fichiers")
            _copy(db, path, key, dest, out)
            out['done'] += 1
            out['bytes'] += size
            if tell:
                tell(out)
        return out
    finally:
        db.close()
