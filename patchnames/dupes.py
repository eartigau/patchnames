"""
Duplicates in the archive: the same picture under two names.

A photo archived with the wrong time, then corrected, leaves two files: the
same picture, another time in its EXIF and in its name. find() looks for the
files whose image data is the same whatever their metadata says (the hash
exiftool computes of the image data alone, ImageDataHash), among those that
could be: same extension, nearly the same size, and the same camera number
in their name, or the same name, or times a whole number of minutes apart.
The hashes are kept in the index, so that a search stopped or run again
only reads the files it has not read.

decide() says which file of each group is the right one. The eBird
checklists have the right time: a time that falls inside a checklist wins
over one that does not. When the checklists do not tell two times apart (no
checklist, or both inside one), the photos of the same day that have the
same gap between their two times and that the checklists did tell apart
decide for the others; failing that, those of the same folders. Among the
files at the right time, the one in the archive layout (20YY_MM/oiseaux or
/autres) is kept. A group nothing decides is left alone and listed.

derived() looks for the pictures made from a raw file (a JPG, a DNG) that
are not at the time of that raw file: the raw file is right, the picture
made from it takes its time and its name.

apply() moves the duplicates to the folder 'doublons' of the archive, under
the path they had: nothing is deleted, the photographer does that. What was
moved is kept in the index (table moved), so that the copy from a drive to
the other never brings a duplicate back nor copies the folder 'doublons',
and so that the same files are put aside on the other copy (settle()).
"""

import collections
import os
import re
import shlex
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

from . import photoindex
from . import remote

#: where the duplicates go, at the root of the archive
FOLDER = 'doublons'
#: a canonical name: YYMMDD_HHMMSS[_WHO]_NUM.ext
NAME = re.compile(r'^(\d{6})_(\d{6})(?:_([A-Z]{2}))?_([^.]+)\.(\w+)$')
#: a file of the archive layout: 20YY_MM/oiseaux/... or 20YY_MM/autres/...
PLACE = re.compile(r'^(\d{4})_(\d{2})/(?:oiseaux|autres)/[^/]+$')
#: what is compared: pictures and films (not the sidecars, nor the copies
#:  exiftool leaves behind)
KINDS = ('nef', 'jpg', 'jpeg', 'dng', 'heic', 'tif', 'tiff', 'png', 'mp4', 'mov')
#: a picture made from a raw file
DERIVED = ('jpg', 'jpeg', 'dng', 'tif', 'tiff')
#: two files this close in size may be the same picture: a change of EXIF
#:  moves the size of a file by a few hundred bytes
NEAR = 0.001
#: a picture made from a raw file is looked for within this many seconds of it
WINDOW = 2 * 86400
#: the votes of a folder pair needed to decide for it, and how many must agree
VOTES, AGREE = 5, 0.95

SCHEMA = """
CREATE TABLE IF NOT EXISTS hashes (path TEXT PRIMARY KEY, size INTEGER, mtime REAL, hash TEXT,
    taken TEXT);
CREATE TABLE IF NOT EXISTS moved (path TEXT PRIMARY KEY, kept TEXT, why TEXT, moved TEXT);
"""


def _db():
    db = photoindex.connect()
    db.executescript(SCHEMA)
    return db


# ---------------------------------------------------------------------------
# What the archive holds
# ---------------------------------------------------------------------------

def _server(root):
    """(server, path there) of an archive root mounted from a server that
    answers to ssh; None for a drive of this machine"""
    where = remote.place(os.path.join(root, 'x'))
    return (where[0], os.path.dirname(where[1])) if where else None


def listing(root):
    """every file of an archive root, {path relative to it: (size,
    modification time)}, the folder of the duplicates left out. Asked of the
    server in one command when the root is mounted from one: through the
    mount it would take hours."""
    found = {}
    server = _server(root)
    if server:
        code, out, err = remote._ssh(
            server[0], f"cd {shlex.quote(server[1])} && find . -type f -printf '%s\\t%T@\\t%P\\n'",
            timeout=3600)
        if not out:
            raise RuntimeError(f"le serveur n'a pas donné la liste des fichiers : {err.strip()[:200]}")
        for line in out.decode('utf-8', 'replace').splitlines():
            size, mtime, path = line.split('\t', 2)
            found[path] = (int(size), float(mtime))
    else:
        for folder, _, names in os.walk(root):
            for name in names:
                full = os.path.join(folder, name)
                try:
                    stat = os.stat(full)
                except OSError:
                    continue
                found[os.path.relpath(full, root)] = (stat.st_size, stat.st_mtime)
    return {path: what for path, what in found.items()
            if not path.startswith(FOLDER + '/') and not os.path.basename(path).startswith('.')
            and not path.endswith('_exiftool_tmp')}


def _kind(path):
    return os.path.splitext(path)[1].lstrip('.').lower()


def _named(path):
    """(date and time, who, camera number or None) from a canonical name;
    None for another name"""
    found = NAME.match(os.path.basename(path))
    if not found:
        return None
    try:
        when = datetime.strptime(found.group(1) + found.group(2), '%y%m%d%H%M%S')
    except ValueError:
        return None
    return when, found.group(3), found.group(4) if found.group(4).isdigit() else None


def candidates(files, folders=None):
    """the files that could be the same picture as another: the same
    extension and nearly the same size, and the same camera number in their
    name, or the same name, or (names without a camera number) the same
    minutes and seconds. folders: only the groups that touch these folders
    (the starts of paths of the archive: 2026_07, mexico2026/oiseaux...)."""
    groups = collections.defaultdict(list)
    for path, (size, _) in files.items():
        kind = _kind(path)
        if kind not in KINDS or not size:
            continue
        named = _named(path)
        keys = [('name', os.path.basename(path).lower())]
        if named and named[2]:
            keys.append(('number', named[1], named[2], kind))
        elif named:
            keys.append(('time', kind, named[0].strftime('%M%S')))
        for key in keys:
            groups[key].append((size, path))
    found = set()
    for group in groups.values():
        group.sort()
        linked = set()
        for (small, one), (large, other) in zip(group, group[1:]):
            if large - small <= NEAR * large:
                linked.update((one, other))
        if folders and not any(path.startswith(tuple(folders)) for path in linked):
            continue
        found |= linked
    return found


# ---------------------------------------------------------------------------
# The same picture: the hash of the image data
# ---------------------------------------------------------------------------

def hashes(root, files, paths, tell=None, jobs=3, batch=40):
    """the hash of the image data of files (exiftool's ImageDataHash: what
    the picture is, whatever its metadata says) and the time their EXIF
    gives, {path: (hash, time)}. Read from the index for the files that have
    not changed since they were hashed; the others are read by exiftool, on
    the server of the archive when it has one, `batch` to a call and `jobs`
    calls at once. tell(done, total) as it goes."""
    db = _db()
    known = {row['path']: row for row in db.execute('SELECT * FROM hashes')}
    found, todo = {}, []
    for path in sorted(paths):
        size, mtime = files[path]
        row = known.get(path)
        if row and row['size'] == size and abs(row['mtime'] - mtime) < 2:
            found[path] = (row['hash'], row['taken'])
        else:
            todo.append(path)
    total, done = len(todo), 0
    if tell:
        tell(0, total)

    def read(part):
        rows = remote.rows(['-ImageDataHash', '-DateTimeOriginal', '-CreateDate'],
                           [os.path.join(root, path) for path in part], timeout=3600)
        return {os.path.relpath(row['SourceFile'], root): row for row in rows}

    try:
        with ThreadPoolExecutor(max_workers=max(1, jobs)) as pool:
            waiting = {pool.submit(read, todo[k:k + batch]): todo[k:k + batch]
                       for k in range(0, total, batch)}
            for future in as_completed(waiting):
                part = waiting[future]
                try:
                    rows = future.result()
                except Exception:
                    rows = {}
                for path in part:
                    row = rows.get(path) or {}
                    value = row.get('ImageDataHash')
                    taken = str(row.get('DateTimeOriginal') or row.get('CreateDate') or '')[:19]
                    if value:
                        found[path] = (str(value), taken)
                        db.execute('INSERT OR REPLACE INTO hashes VALUES (?, ?, ?, ?, ?)',
                                   (path, files[path][0], files[path][1], str(value), taken))
                db.commit()
                done += len(part)
                if tell:
                    tell(done, total)
    finally:
        db.close()
    return found


def _when(path, taken):
    """the time of a file: what its EXIF says, else what its name says"""
    try:
        return datetime.strptime(taken, '%Y:%m:%d %H:%M:%S')
    except (TypeError, ValueError):
        named = _named(path)
        return named[0] if named else None


def find(found):
    """the groups of files with the same image data, [[path, ...]], from
    what hashes() returned"""
    groups = collections.defaultdict(list)
    for path, (value, _) in found.items():
        groups[value].append(path)
    return [sorted(group) for group in groups.values() if len(group) > 1]


# ---------------------------------------------------------------------------
# Which one is right
# ---------------------------------------------------------------------------

def _inside(data, when):
    """the eBird checklist a time falls inside, None if none"""
    if not data or when is None:
        return None
    match = data.match(when, 0)
    return match.checklist if match and match.kind == 'inside' else None


def _layout(path, when):
    """whether a file is where the archive keeps a photo of that time"""
    found = PLACE.match(path)
    return bool(found and when and (int(found.group(1)), int(found.group(2))) == (when.year, when.month))


def _hours(seconds):
    hours = seconds / 3600
    return f'{hours:+.0f} h' if hours == round(hours) else f'{seconds / 60:+.0f} min'


def decide(groups, found, data):
    """which file of each group of identical pictures is kept: returns
    (plans, undecided), plans being [{'keep': path, 'move': [paths], 'why':
    text}] and undecided [{'files': [paths], 'why': text}]"""
    times = {path: _when(path, found[path][1]) for group in groups for path in group}
    lists = {}

    def inside(when):
        if when not in lists:
            lists[when] = _inside(data, when)
        return lists[when]

    # what the checklists say of each group whose files have two times
    verdicts, days, folders = {}, collections.Counter(), collections.defaultdict(collections.Counter)
    for k, group in enumerate(groups):
        distinct = sorted({times[path] for path in group if times[path]})
        if len(distinct) != 2:
            continue
        early, late = distinct
        gap = round((late - early).total_seconds())
        pair = tuple(sorted({path.split('/')[0] for path in group}))
        said = 1 if inside(early) and not inside(late) else -1 if inside(late) and not inside(early) else 0
        verdicts[k] = (early, late, gap, pair, said)
        if said:
            days[(early.date(), gap, said)] += 1
            folders[(pair, gap)][said] += 1

    plans, undecided = [], []
    for k, group in enumerate(groups):
        distinct = sorted({times[path] for path in group if times[path]})
        right, why = None, ''
        if len(distinct) <= 1:
            right, why = (distinct[0] if distinct else None), 'même image, même heure'
        elif k in verdicts:
            early, late, gap, pair, said = verdicts[k]
            if said:
                right = early if said > 0 else late
                why = f'{_hours(gap)} entre les deux ; liste eBird {inside(right).sid}'
            else:
                same_day = {s: days[(early.date(), gap, s)] for s in (1, -1)}
                votes = folders[(pair, gap)]
                if (same_day[1] > 0) != (same_day[-1] > 0):
                    said = 1 if same_day[1] else -1
                    why = (f'{_hours(gap)} entre les deux, comme {same_day[said]} photos du même '
                           'jour que les listes eBird départagent')
                elif sum(votes.values()) >= VOTES and max(votes.values()) >= AGREE * sum(votes.values()):
                    said = max(votes, key=votes.get)
                    why = (f'{_hours(gap)} entre les deux, comme {votes[said]} photos des mêmes '
                           'dossiers que les listes eBird départagent')
                right = early if said > 0 else late if said < 0 else None
        else:
            good = [when for when in distinct if inside(when)]
            if len(good) == 1:
                right, why = good[0], f'{len(distinct)} heures ; liste eBird {inside(good[0]).sid}'
        if len(distinct) > 1 and right is None:
            undecided.append({'files': group, 'why': 'les listes eBird ne départagent pas '
                              + ' et '.join(f'{when:%Y-%m-%d %H:%M:%S}' for when in distinct)})
            continue
        # among the files at the right time: the one where the archive keeps it
        at_time = [path for path in group if times[path] == right] or group
        keep = min(at_time, key=lambda path: (not _layout(path, right), path.count('/') < 2, path))
        plans.append({'keep': keep, 'move': [path for path in group if path != keep], 'why': why})
    return plans, undecided


# ---------------------------------------------------------------------------
# A picture made from a raw file, at another time than the raw file
# ---------------------------------------------------------------------------

def derived(root, files, moving=(), folders=None):
    """the pictures made from a raw file (JPG, DNG, TIFF with its camera
    number) that are not at its time: [{'path', 'raw', 'name', 'taken'}],
    the name and the time each should take. The raw file is the only one of
    that camera number within WINDOW of the picture, taken with the same
    camera at the same exposure; `moving`: the raw files that are being put
    aside as duplicates, which do not count; folders: only the pictures
    under these starts of paths, or whose raw file is."""
    raws, made = collections.defaultdict(list), []
    stems = set()
    for path in files:
        named, kind = _named(path), _kind(path)
        if not named or not named[2] or path in moving:
            continue
        if kind == 'nef':
            raws[(named[1], named[2])].append((named[0], path))
            stems.add(os.path.splitext(os.path.basename(path))[0])
        elif kind in DERIVED:
            made.append((named, path))
    pairs = []
    for named, path in made:
        if os.path.splitext(os.path.basename(path))[0] in stems:
            continue
        near = [raw for when, raw in raws[(named[1], named[2])]
                if abs((when - named[0]).total_seconds()) <= WINDOW]
        if len({os.path.basename(raw) for raw in near}) == 1:
            raw = sorted(near, key=lambda raw: not _layout(raw, _named(raw)[0]))[0]
            if not folders or path.startswith(tuple(folders)) or raw.startswith(tuple(folders)):
                pairs.append((path, raw))
    if not pairs:
        return []
    asked = sorted({path for pair in pairs for path in pair})
    rows = {}
    for start in range(0, len(asked), 60):
        part = asked[start:start + 60]
        for row in remote.rows(['-DateTimeOriginal', '-OffsetTimeOriginal', '-Model', '-ExposureTime',
                                '-FNumber', '-ISO', '-FocalLength'],
                               [os.path.join(root, path) for path in part]):
            rows[os.path.relpath(row['SourceFile'], root)] = row
    out = []
    for path, raw in pairs:
        one, other = rows.get(path) or {}, rows.get(raw) or {}
        same = [key for key in ('Model', 'ExposureTime', 'FNumber', 'ISO') if one.get(key) is not None
                and other.get(key) is not None]
        if len(same) < 3 or any(str(one[key]) != str(other[key]) for key in same):
            continue        # (not shown to be the same shot: left alone)
        if not other.get('DateTimeOriginal'):
            continue
        name = os.path.splitext(os.path.basename(raw))[0] + os.path.splitext(path)[1]
        if os.path.join(os.path.dirname(path), name) in files:
            continue        # (a picture already has that name)
        out.append({'path': path, 'raw': raw, 'name': name, 'taken': str(other['DateTimeOriginal'])[:19],
                    'zone': other.get('OffsetTimeOriginal')})
    return out


# ---------------------------------------------------------------------------
# Putting the duplicates aside
# ---------------------------------------------------------------------------

def _moves(root, pairs):
    """rename files of an archive root, [(path, new path)] relative to it,
    never over a file that is there; the pairs that were done"""
    server = _server(root)
    if server:
        done = []
        for start in range(0, len(pairs), 500):
            part = pairs[start:start + 500]
            lines = [f'if [ -f {shlex.quote(old)} ] && [ ! -e {shlex.quote(new)} ]; then mkdir -p '
                     f'{shlex.quote(os.path.dirname(new) or ".")} && mv -n {shlex.quote(old)} '
                     f'{shlex.quote(new)} && echo {k}; fi' for k, (old, new) in enumerate(part)]
            code, out, _ = remote._ssh(server[0], f'cd {shlex.quote(server[1])} && sh -s',
                                       ('\n'.join(lines) + '\n').encode('utf-8'), timeout=3600)
            done += [part[int(k)] for k in out.decode().split() if k.isdigit()]
        return done
    done = []
    for old, new in pairs:
        source, target = os.path.join(root, old), os.path.join(root, new)
        if os.path.isfile(source) and not os.path.exists(target):
            os.makedirs(os.path.dirname(target), exist_ok=True)
            os.rename(source, target)
            done.append((old, new))
    return done


def apply(root, plans, fixes=()):
    """put the duplicates of `plans` aside, in the folder 'doublons' of the
    archive under the path they had, and give the pictures of `fixes` the
    time and the name of their raw file. The index follows, and keeps what
    was moved. Returns (files moved, pictures set right)."""
    wanted = [(path, plan) for plan in plans for path in plan['move']]
    moved = _moves(root, [(path, f'{FOLDER}/{path}') for path, _ in wanted])
    gone = {old for old, _ in moved}
    db = _db()
    now = datetime.now().isoformat(' ', 'seconds')
    for path, plan in wanted:
        if path not in gone:
            continue
        db.execute('INSERT OR REPLACE INTO moved VALUES (?, ?, ?, ?)', (path, plan['keep'], plan['why'], now))
        db.execute('DELETE FROM hashes WHERE path = ?', (path,))
        name, kept = os.path.basename(path), os.path.basename(plan['keep'])
        row = db.execute('SELECT path FROM photos WHERE name = ?', (name,)).fetchone()
        if row and name == kept:
            # the same name in two folders: the index follows the one kept
            db.execute('UPDATE photos SET path = ? WHERE name = ?', (plan['keep'], name))
        elif row and photoindex.shorten(row['path']) == path:
            photoindex.forget(db, [name])
    db.commit()

    fixed = 0
    for fix in fixes:
        full = os.path.join(root, fix['path'])
        args = ['-q', '-m', '-overwrite_original', f"-AllDates={fix['taken']}"]
        if fix.get('zone'):
            args += [f"-OffsetTime={fix['zone']}", f"-OffsetTimeOriginal={fix['zone']}",
                     f"-OffsetTimeDigitized={fix['zone']}"]
        if remote.run(args, [full])[0] != 0:
            continue
        new = os.path.join(os.path.dirname(fix['path']), fix['name'])
        if _moves(root, [(fix['path'], new)]):
            fixed += 1
            photoindex.forget(db, [os.path.basename(fix['path'])])
            db.execute('DELETE FROM hashes WHERE path = ?', (fix['path'],))
            # (on the other copy the picture still has its old name: it is a
            #   duplicate there once the one set right is copied)
            db.execute('INSERT OR REPLACE INTO moved VALUES (?, ?, ?, ?)',
                       (fix['path'], new, "mise à l'heure de son NEF", now))
    db.commit()
    db.close()
    return len(moved), fixed


def excluded():
    """the paths the copy from a drive to the other must leave alone: what
    was put aside as a duplicate (it would come back from the other copy)"""
    if not os.path.isfile(photoindex.db_path()):
        return []
    db = _db()
    paths = [row['path'] for row in db.execute('SELECT path FROM moved ORDER BY path')]
    db.close()
    return paths


def settle(root):
    """put aside, on an archive root, the duplicates that were put aside on
    another copy and are still in place on this one; how many"""
    paths = excluded()
    return len(_moves(root, [(path, f'{FOLDER}/{path}') for path in paths])) if paths else 0


# ---------------------------------------------------------------------------
# patchdoc doublons
# ---------------------------------------------------------------------------

def _clock(seconds):
    seconds = max(0, round(seconds))
    hours, minutes = seconds // 3600, seconds % 3600 // 60
    return f'{hours} h {minutes:02d}' if hours else f'{minutes}:{seconds % 60:02d}'


def _bar(done, total, start, width=28):
    """a line of progress, like tqdm: the share done, the time it took and
    the time left at this pace"""
    share = done / total if total else 1.0
    spent = time.time() - start
    left = f' · reste environ {_clock(spent * (total - done) / done)}' if done and done < total else ''
    full = round(width * share)
    print(f"\r  [{'█' * full}{'░' * (width - full)}] {done}/{total} · {100 * share:.0f} % · "
          f'{_clock(spent)} écoulées{left}   ', end='', flush=True)


def main(folders=None, move=False, root=None):
    """`patchdoc doublons`: say what is the same picture twice in the
    archive and which copy is right; move the others aside if asked"""
    from . import config as _config_module
    from . import ebird as _ebird
    root = root or _config_module.archive_root()
    if not os.path.isdir(root):
        print(f"{root} est introuvable (le disque d'archive est-il monté ?).")
        return
    print(f'Archive : {root}')
    print('Liste des fichiers…', end=' ', flush=True)
    files = listing(root)
    print(f'{len(files)} fichiers.')
    maybe = candidates(files, folders)
    print(f'{len(maybe)} fichiers pourraient être la même image qu\'un autre (même numéro '
          "d'appareil ou même nom, taille presque égale) : lecture de leurs données image.")
    start = time.time()
    try:
        found = hashes(root, files, maybe, tell=lambda done, total: _bar(done, total, start))
    except KeyboardInterrupt:
        print('\nArrêt demandé : ce qui est lu est gardé, relancez pour continuer.')
        return
    print()
    groups = find(found)
    data = _ebird.load()
    plans, undecided = decide(groups, found, data)
    moving = {path for plan in plans for path in plan['move']}
    fixes = derived(root, files, moving, folders)

    kinds = collections.Counter()
    for plan in plans:
        kinds[re.sub(r'S\d+|\d+ photos', '…', plan['why'])] += len(plan['move'])
    size = sum(files[path][0] for path in moving)
    print(f'{len(groups)} images présentes plus d\'une fois ; {len(moving)} fichiers en trop '
          f'({size / 1e9:.1f} Go) :')
    for why, count in kinds.most_common():
        print(f'  {count:7d}  {why}')
    if undecided:
        print(f'{len(undecided)} images que rien ne départage, laissées en place.')
    if fixes:
        print(f"{len(fixes)} images tirées d'un NEF (JPG, DNG) ne sont pas à l'heure de leur NEF.")
    plan_file = os.path.join(photoindex.index_dir(), 'doublons.tsv')
    with open(plan_file, 'w') as handle:
        handle.write('quoi\tfichier\tgardé ou nouveau nom\tpourquoi\n')
        for plan in plans:
            for path in plan['move']:
                handle.write(f"doublon\t{path}\t{plan['keep']}\t{plan['why']}\n")
        for group in undecided:
            handle.write(f"indécis\t{' | '.join(group['files'])}\t\t{group['why']}\n")
        for fix in fixes:
            handle.write(f"heure\t{fix['path']}\t{fix['name']}\tcomme {fix['raw']} ({fix['taken']})\n")
    print(f'Le détail, fichier par fichier : {plan_file}')
    if not move:
        print(f"Rien n'a été déplacé. `patchdoc doublons --deplacer` range les fichiers en trop "
              f"dans {os.path.join(root, FOLDER)} (rien n'est effacé) et met les images à "
              "l'heure de leur NEF.")
        return
    moved, fixed = apply(root, plans, fixes)
    print(f'{moved} fichiers rangés dans {os.path.join(root, FOLDER)}, {fixed} images mises à '
          "l'heure de leur NEF. Rien n'a été effacé ; la copie d'un disque à l'autre laissera "
          'ces fichiers de côté et les rangera de même sur l\'autre copie.')
