"""
The photos that wait to be sent to the archive (config 'inbox_dirs'), and
what the search page does to them when they are what it shows: the three
buttons under "Regarder".

times()  puts right the hour of the photos whose camera clock was off. The
         index has, for each photo, the offset patchdoc found from the eBird
         checklists (doc.locate): it is written into the file (the three EXIF
         dates `patchnames time-offset` moves, and the time zone the camera
         recorded, which moves with them), and the file takes the name of
         its new hour. A photo whose clock was right is not touched, nor one
         whose file is no longer as the index read it.
names()  gives each file its canonical name, YYMMDD_HHMMSS[_WHO]_NUM.ext, as
         `patchnames` does (core.get_exif).
send()   moves each file to the archive in use, under 20YY_MM/oiseaux or
         /autres as `patchnames` does, once it has its canonical name.

The index follows each file (photoindex.rename): what is known of a photo
stays with it. Unlike `patchnames`, nothing is deleted nor written over: a
file whose name is taken, here or in the archive, is left where it is and
counted. What is done is added to inbox.log, in the folder of the index.

The files are those of the extensions of config 'extensions' (the videos go
to the archive with the photos); only photos have an hour to put right.
"""

import os
import shutil
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta

from . import config as _config_module
from . import core
from . import doc
from . import faces
from . import photoindex
from . import remote
from . import sync

#: what is said of the files left as they are: the first ones
SAID = 6


@contextmanager
def _free(seq):
    """the files of a sequence, when nobody else rewrites them (the search
    page passes its own, which waits for its other writers)"""
    yield True


def files(cfg=None):
    """the files that wait: those of the extensions of config 'extensions',
    directly in each folder of config 'inbox_dirs'"""
    cfg = cfg or _config_module.load()
    wanted = tuple('.' + pattern.rsplit('.', 1)[1].lower() for pattern in cfg['extensions'])
    found = []
    for folder in doc.inbox_dirs(cfg):
        if os.path.isdir(folder):
            found += [os.path.join(folder, name) for name in sorted(os.listdir(folder))
                      if name.lower().endswith(wanted) and not name.startswith('.')
                      and os.path.isfile(os.path.join(folder, name))]
    return found


def _row(db, path):
    """the photo of the index a file is: the one that has its name and is
    where it is; None when the index has none, or has that name elsewhere"""
    row = db.execute('SELECT * FROM photos WHERE name = ?', (os.path.basename(path),)).fetchone()
    return row if row is not None and row['path'] == path else None


def _hours(offset):
    """'+1 h', '-5,5 h'"""
    return f'{offset:+g} h'.replace('.', ',')


def _canonical(name):
    """whether a name looks like a canonical one (its hour may still be old)"""
    return bool(doc.NAME.match(name)) and os.path.splitext(name)[1].islower()


def plan(cfg=None):
    """what the three buttons would do, from the names of the files and the
    index alone: how many files wait and their size, how many do not have a
    canonical name, how many photos have a clock offset to write (and which
    offsets), and the archive they would go to"""
    cfg = cfg or _config_module.load()
    paths = files(cfg)
    offsets = {}
    if os.path.isfile(photoindex.db_path()):
        db = sqlite3.connect(f'file:{photoindex.db_path()}?mode=ro', uri=True, timeout=30)
        db.row_factory = sqlite3.Row
        for path in paths:
            row = _row(db, path)
            if row is not None and row['clock_offset']:
                offsets[row['clock_offset']] = offsets.get(row['clock_offset'], 0) + 1
        db.close()
    root = _config_module.archive_root(cfg)
    return {'files': len(paths), 'bytes': sum(os.path.getsize(path) for path in paths),
            'odd': sum(1 for path in paths if not _canonical(os.path.basename(path))),
            'retime': sum(offsets.values()),
            'offsets': [[_hours(offset), count] for offset, count in sorted(offsets.items())],
            'archive': {'name': sync.name_of(root), 'mounted': os.path.isdir(root)}}


def _log(line):
    with open(os.path.join(photoindex.index_dir(), 'inbox.log'), 'a', encoding='utf-8') as handle:
        handle.write(f'{datetime.now():%Y-%m-%d %H:%M:%S}  {line}\n')


def _left(out, path, why):
    """count a file left as it is, and say why for the first ones"""
    out['left'] += 1
    if len(out['problems']) < SAID:
        out['problems'].append(f'{os.path.basename(path)} : {why}')


def _name(db, path):
    """give a file its canonical name, where it is, and let the index follow:
    (its path now, its EXIF as core.get_exif reads it, what was done: 'same',
    'renamed', 'taken' when that name is already a file there or a photo of
    the index, 'unread' when its EXIF cannot be read)"""
    try:
        hdr = core.get_exif(path)
    except Exception:
        return path, None, 'unread'
    name, new = os.path.basename(path), hdr['REFERENCE NAME']
    target = os.path.join(os.path.dirname(path), new)
    if new == name:
        return path, hdr, 'same'
    if ((os.path.exists(target) and not os.path.samefile(path, target))
            or db.execute('SELECT 1 FROM photos WHERE name = ?', (new,)).fetchone()):
        return path, hdr, 'taken'
    mine = _row(db, path) is not None
    os.rename(path, target)
    _log(f'nom    {path} -> {new}')
    if mine:
        done = photoindex.rename(db, {name: target})
        if done and faces.available():
            faces.renamed(db, done)
    return target, hdr, 'renamed'


def names(tell=None, stop=None, hold=_free):
    """Give every file that waits its canonical name. tell(counts) after each
    file, stop: an Event that ends it, hold(sequence): see _free. Returns
    {'done', 'total', 'renamed', 'left', 'problems'}."""
    paths = files()
    out = {'done': 0, 'total': len(paths), 'renamed': 0, 'left': 0, 'problems': []}
    db = photoindex.connect()
    try:
        for path in paths:
            if stop is not None and stop.is_set():
                break
            row = _row(db, path)
            with hold(row['seq'] if row else None) as mine:
                if not mine:
                    break
                _, _, what = _name(db, path)
            out['done'] += 1
            out['renamed'] += what == 'renamed'
            if what == 'taken':
                _left(out, path, 'son nom canonique est déjà pris')
            elif what == 'unread':
                _left(out, path, 'ses dates sont illisibles')
            if tell:
                tell(out)
        return out
    finally:
        db.close()


def _shift(offset, zone):
    """the exiftool arguments that move by `offset` hours the three dates
    `patchnames time-offset` moves, and set the time zone the camera
    recorded to `zone` (hours from UTC; None for a camera that records none)"""
    seconds = round(abs(offset) * 3600)
    by = f"{'+=' if offset > 0 else '-='}{seconds // 3600}:{seconds % 3600 // 60}:{seconds % 60}"
    args = [f'-EXIF:{tag}{by}' for tag in ('DateTimeOriginal', 'CreateDate', 'ModifyDate')]
    if zone is not None:
        minutes = round(abs(zone) * 60)
        text = f"{'+' if zone >= 0 else '-'}{minutes // 60:02d}:{minutes % 60:02d}"
        args += [f'-{tag}={text}' for tag in ('OffsetTimeOriginal', 'OffsetTime',
                                              'OffsetTimeDigitized')]
    return args


def _retime(db, rows, backup, out):
    """write its hour into each of these files, {path: its row in the index}:
    the files of one sequence. Each is read first (one that is no longer as
    the index read it is left alone) and read again afterwards: the index
    follows the files whose hour did change, and they are named again."""
    seen = doc.read_exif(list(rows), fast=True)
    groups = {}
    for path, row in rows.items():
        when = doc._when(seen.get(path, {}))
        if when is None or when.isoformat(' ') != (row['taken'] or '')[:19]:
            _left(out, path, "le fichier n'est plus tel que la base l'a lu")
            continue
        zone = doc._zone(seen[path])
        groups.setdefault((row['clock_offset'],
                           None if zone is None else zone + row['clock_offset']), []).append(path)
    head = ['-q', '-m'] + ([] if backup else ['-overwrite_original'])
    for (offset, zone), paths in groups.items():
        remote.run_many([(head + _shift(offset, zone), paths)])
        after = doc.read_exif(paths, fast=True)
        for path in paths:
            row = rows[path]
            when = datetime.fromisoformat(row['taken'][:19]) + timedelta(hours=offset)
            if doc._when(after.get(path, {})) != when:
                _left(out, path, "l'heure n'a pas pu être écrite")
                continue
            db.execute('UPDATE photos SET taken = ?, local = ?, clock_offset = 0, zone = ? '
                       'WHERE id = ?', (when.isoformat(' '), when.isoformat(' '),
                                        row['zone'] if zone is None else zone, row['id']))
            db.commit()
            _log(f'heure  {path} {_hours(offset)} -> {when:%Y-%m-%d %H:%M:%S}')
            _, _, what = _name(db, path)
            out['done'] += 1
            out['renamed'] += what == 'renamed'
            if what == 'taken':
                _left(out, path, "mis à l'heure, mais son nouveau nom est déjà pris")


def times(tell=None, stop=None, hold=_free):
    """Write into the photos that wait the hour the index has for them (see
    the module), sequence by sequence. Returns {'done', 'total', 'renamed',
    'left', 'problems'}."""
    backup = bool(_config_module.load().get('gui_backup'))
    db = photoindex.connect()
    try:
        seqs = {}
        for path in files():
            row = _row(db, path)
            if row is not None and row['clock_offset']:
                seqs.setdefault(row['seq'], []).append(path)
        out = {'done': 0, 'total': sum(len(paths) for paths in seqs.values()), 'renamed': 0,
               'left': 0, 'problems': []}
        for seq, paths in seqs.items():
            if stop is not None and stop.is_set():
                break
            with hold(seq) as mine:
                if not mine:
                    break
                # (read again: the sequence may have waited for another writer)
                rows = {path: _row(db, path) for path in paths}
                _retime(db, {path: row for path, row in rows.items()
                             if row is not None and row['clock_offset']}, backup, out)
            if tell:
                tell(out)
        return out
    finally:
        db.close()


def _send(db, root, path, out):
    """move one file to its folder of the archive `root`, under its canonical
    name; True when it went"""
    path, hdr, what = _name(db, path)
    if hdr is None:
        return _left(out, path, 'ses dates sont illisibles')
    if what == 'taken':
        return _left(out, path, 'son nom canonique est déjà pris')
    out['renamed'] += what == 'renamed'
    name = os.path.basename(path)
    known = db.execute('SELECT path FROM photos WHERE name = ?', (name,)).fetchone()
    if known is not None and known['path'] != path:
        return _left(out, path, 'la base a déjà cette photo ailleurs')
    month, kind = core.archive_folder(hdr)
    target = os.path.join(root, month, kind, name)
    if os.path.exists(target):
        return _left(out, path, "un fichier de ce nom est déjà dans l'archive")
    os.makedirs(os.path.dirname(target), exist_ok=True)
    # copied under another name first: the archive never holds half a file
    part = target + '.part'
    try:
        shutil.copy2(path, part)
        if os.path.getsize(part) != os.path.getsize(path):
            raise OSError('copie incomplète')
        os.replace(part, target)
    except OSError as exc:
        if os.path.exists(part):
            os.remove(part)
        return _left(out, path, f"la copie vers l'archive a échoué ({exc})")
    os.remove(path)
    _log(f'envoi  {path} -> {target}')
    if known is not None:
        photoindex.rename(db, {name: target})
    return True


def send(tell=None, stop=None, hold=_free):
    """Move every file that waits to the archive in use, each under its
    canonical name in its folder (see the module). Raises RuntimeError when
    the archive is not there. Returns {'done', 'total', 'sent', 'bytes',
    'all', 'renamed', 'left', 'problems', 'to'}: 'bytes' out of 'all' are
    through, 'sent' files went, to the archive named 'to'."""
    cfg = _config_module.load()
    root = _config_module.archive_root(cfg)
    if not os.path.isdir(root):
        raise RuntimeError("l'archive n'est pas montée")
    paths = files(cfg)
    sizes = {path: os.path.getsize(path) for path in paths}
    out = {'done': 0, 'total': len(paths), 'sent': 0, 'bytes': 0, 'all': sum(sizes.values()),
           'renamed': 0, 'left': 0, 'problems': [], 'to': sync.name_of(root)}
    db = photoindex.connect()
    try:
        for path in paths:
            if stop is not None and stop.is_set():
                break
            row = _row(db, path)
            with hold(row['seq'] if row else None) as mine:
                if not mine:
                    break
                out['sent'] += bool(_send(db, root, path, out))
            out['done'] += 1
            out['bytes'] += sizes[path]
            if tell:
                tell(out)
        return out
    finally:
        db.close()
