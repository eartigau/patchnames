"""
The search page of the photos (`patchdoc gui`): a small web server, for this
machine only, over the index of photoindex.py.

The page (gui/index.html, app.js) asks for:

    /api/facets              what the filters offer
    /api/search?...          the sequences that pass the filters
    /api/places?...          their places, for the map
    /api/sequence?seq=       the frames of a sequence
    /api/checklist?sid=      an eBird checklist and its species
    /api/day?date=           the eBird checklists of a day
    /api/attach              (POST) tie a sequence to a checklist by hand, or untie it
    /api/orient              how the look for photos on their side goes; (POST)
                             start it or stop it (doc.orient)
    /thumb/NAME /preview/NAME   the images of a photo
    /eye/NAME                the eye of the bird of a photo at full resolution
    /api/reveal, /api/open   (POST) show a photo in the Finder, open it
    /api/describe            (POST) have Claude describe a sequence
    /api/write               (POST) write into its files what the index says
    /api/job?id=             how that is going
    /api/batch               (POST) the same for a list of sequences, or for
                             every sequence of the archive that has no
                             description, in the background; (GET) how that
                             is going
    /api/batch/stop          (POST) drop the sequences still waiting
    /api/archive             the copies of the archive, and the copy from one
                             to another
    /api/archive/look        (POST) use a copy, and list what it holds
    /api/orphans             (POST) compare two copies file by file for the
                             files only one has (sync.orphans), or show the
                             list found last in the Finder
    /api/inbox               the photos that wait to be sent to the archive;
                             (POST) look at their folders again
    /api/inbox/act           (POST) put their hours right, name them, or send
                             them to the archive (inbox.py)
    /api/inbox/stop          (POST) end that after the file under way
    /api/backup              the backup of the index on GitHub; (POST) make
                             one and send it (backup.py)
    /api/faces?name=         the faces of a photo, looked for if nobody did
    /api/face                (POST) who a face is
    /api/pano                (POST) stitch a sequence into a panorama
    /api/quit                (POST) stop this server (started from the Dock, it
                             has no terminal to press Ctrl-C in)
    /api/rotate              (POST) turn a photo a quarter turn: the Orientation
                             of its file is rewritten, its pictures follow

Thumbnails and previews come from the index folder, a local store that makes
browsing fast and needs no drive; one that is missing is made from the photo
itself when its file can be reached (the archive drive mounted), and kept.
While the page is served the store fills itself in the background: the frame
that stands for each sequence, the newest first (config 'gui_fill_thumbs').

The button "Identifier la séquence avec Claude" of the viewer runs
doc.describe_sequence, one Claude call whose answer goes to the index, then
doc.write_sequence, which writes it into the header of every file of the
sequence with its place and the position of its eBird checklist. The page
has the answer as soon as Claude gives it; the files follow, a long burst on
a network drive taking minutes, and a write cut short by stopping the server
is finished the next time it starts. "Écrire dans les fichiers" does the
second step alone. The button above the results does both for every
sequence shown that has no description yet, two at a time in the background:
it goes on when the page is closed, and the page says how far it is when it
comes back. The calls of a session stop at config 'doc_budget', if one is set.

"Identifier toute l'archive" queues every sequence of the archive that has
no description, the newest first: days of work, which the index remembers
(meta 'batch_archive'), so that a server started again goes on with it until
the page stops it. The sequences asked for in the page meanwhile go ahead of
it. A batch that fails several sequences in a row (Claude at its usage
limit, the network or the drive gone) waits and tries again instead of
failing all it has left, and a sequence that failed is tried once more after
Claude has answered again.

When the page shows the photos that wait to be sent to the archive, three
buttons act on their files (inbox.py), one at a time and in the background:
the hour the index found for the photos whose camera clock was off is
written into them, every file takes its canonical name, and they are moved
to the archive in use, each to its folder. The index follows the files.

"Regarder" also names each copy of the archive when it has several: a click
on one makes it the copy the page reads and writes, and shows what it holds,
that is the photos of the archive less those a listing of the copy did not
find there (doc.index_copy, done again in the background at each click).

The server listens on 127.0.0.1 only, answers only to its own address (so
that no other site can reach it through the browser), and the actions that
touch the machine or call Claude take a photo of the index, never a path,
and a header a page of another site cannot send.
"""

import json
import mimetypes
import os
import sqlite3
import subprocess
import sys
import threading
import time
import webbrowser
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

from . import config as _config_module
from . import photoindex
from . import remote
from . import sync
from .ebird import country_name

STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'gui')
#: the filters of /api/search (see photoindex._where)
FILTERS = ('q', 'species', 'place', 'region', 'year', 'since', 'until', 'topic', 'kind',
           'flag', 'checklist', 'sort', 'section', 'person', 'eye', 'clear', 'level', 'place_of')
#: the extensions of the raw files, which get the "Ouvrir avec" button
RAW = ('nef', 'dng')
#: images made from the photos at the same time, at most (the archive may be
#:  a slow network drive)
_making = threading.Semaphore(4)
#: the thumbnails of the sequences: how many the store has, out of how many
_store = {'have': 0, 'total': 0, 'filling': False}
#: the sequences Claude was asked to describe from the page, {seq: state}, and
#:  what that cost since the server started
_jobs, _lock, _spent = {}, threading.Lock(), {'total': 0.0}
#: the batch of the button above the results: the sequences waiting, those
#:  being done, and what became of the others
_batch = {'waiting': [], 'active': [], 'total': 0, 'done': 0, 'failed': 0, 'note': '',
          'finished': [], 'workers': 0, 'started': 0.0, 'ended': 0.0, 'sizes': {},
          'photos': 0, 'photos_done': 0,
          # how many finished sequences the page is no longer told of (see KEPT)
          'base': 0,
          # the sequences that failed and wait for Claude to answer again to be
          #   tried once more, {sequence: error}, and those that had that try
          'retry': {}, 'again': set(),
          # the failures in a row, the waits in a row, until when the batch
          #   waits, the error that made it wait, and whether the archive is away
          'streak': 0, 'holds': 0, 'until': 0.0, 'why': '', 'away': False,
          # whether it is the batch of the whole archive
          'archive': False}
#: the writing into the files of what the index learned (the batch leaves it
#:  to _resume, one sequence after the other): how many sequences wait, the
#:  one being written and how far; _wake: there is something new to write;
#:  _writing: the sequences whose files somebody is rewriting
_writes = {'waiting': 0, 'seq': None, 'written': 0, 'files': 0, 'error': ''}
_wake, _writing = threading.Event(), set()
#: the copy of the archive from one of its drives to another (sync.py):
#:  'idle', 'planning' (rsync compares the two), 'planned' (what it would
#:  bring), 'running', 'done', 'stopped' or 'error', with the counts
_sync, _sync_stop = {'state': 'idle'}, threading.Event()
#: the look at the photos that wait to be sent to the archive (doc.index_inbox)
_inbox = {'state': 'idle'}
#: Claude's look for the photos that lie on their side (doc.orient): 'idle',
#:  'running', 'done' or 'error', with its counts; _orient_stop ends it
_orient, _orient_stop = {'state': 'idle'}, threading.Event()
#: the backup of the index on GitHub (backup.py): 'idle', 'running' (with the
#:  stage it is at), 'done' or 'error'
_backup = {'state': 'idle'}
#: the look for orphans between two copies of the archive (sync.orphans):
#:  'idle', 'running' (with the files listed so far), 'done' or 'error'; what
#:  was found last is in the index (meta 'orphans')
_orphans = {'state': 'idle'}
#: the listing of what each copy of the archive holds (doc.index_copy), {root:
#:  {'state': 'reading', 'done' or 'error', and what was found}}
_looks = {}
#: what a button of those photos is doing to them (inbox.py): 'idle',
#:  'running', 'done' or 'error', with what ('times', 'names' or 'send') and
#:  its counts; _act_stop ends it after the file under way
_act, _act_stop = {'state': 'idle'}, threading.Event()
#: sequences of a batch done at the same time (each reads and rewrites files
#:  of the archive)
BATCH_JOBS = 2
#: photos looked for on a copy of the archive, at most, to fill one page of
#:  what it holds (_held)
LOOKED = 6000
#: a batch that fails STREAK sequences in a row waits instead of failing all
#:  it has left (Claude at its usage limit, the network or the drive gone):
#:  HOLD minutes, twice as long each time in a row, an hour at most
STREAK, HOLD = 4, 10
#: the page is told what became of the last sequences a batch finished, this
#:  many at least (a batch of the whole archive finishes tens of thousands)
KEPT = 400
#: the sequences of the archive that have no description: those of the batch
#:  of the whole archive (the photos that wait to go there have a full path)
ARCHIVE_TODO = ("FROM photos WHERE rep = 1 AND described IS NULL AND seq IS NOT NULL "
                "AND path NOT LIKE '/%'")


def _db():
    """a read-only connection to the index"""
    path = os.path.join(photoindex.index_dir(), 'photos.sqlite')
    db = photoindex.collate(sqlite3.connect(f'file:{path}?mode=ro', uri=True, timeout=30))
    db.row_factory = sqlite3.Row
    return db


def _names(db):
    """the names the page offers for a face: those of config 'people', to
    start with, then everybody named so far"""
    first = [str(name).strip() for name in _config_module.load().get('people') or []
             if str(name).strip()]
    known = [row[0] for row in db.execute('SELECT name FROM people ORDER BY name COLLATE NOCASE')]
    return first + [name for name in known if name not in first]


def _public(row, root=None):
    """a photo as the page gets it: its path where it can be read now, or as
    the index keeps it (relative to the archive) when it cannot. root: the
    copy of the archive the page looks at; a photo of the archive is then
    looked for there and nowhere else."""
    out = {key: value for key, value in row.items() if key != 'place_norm'}
    out['country_name'] = country_name(row.get('country') or '') if row.get('country') else ''
    path = row.get('path')
    if root and path and not os.path.isabs(path):
        full = os.path.join(root, path)
        full = full if os.path.exists(full) else None
    else:
        full = photoindex.resolve(path)
    out['reachable'] = bool(full)
    out['path'] = full or path
    return out


def _held(db, root, offset, limit, **filters):
    """a page of what a copy of the archive holds (its root is the filter
    'place_of'): the search of the index, with each photo looked for on the
    copy itself, so that the page never shows one the copy does not have,
    whether the copy was listed yet or changed since (the listing,
    doc.index_copy, is what tells the index). Returns (the photos, their
    totals, where the search goes on from, whether more may follow)."""
    from concurrent.futures import ThreadPoolExecutor
    if not os.path.isdir(root):
        # (the copy is not there: what the index knows of it, out of reach)
        rows, totals = photoindex.search(db, offset=offset, limit=limit, **filters)
        return rows, totals, offset + len(rows), len(rows) == limit
    kept, totals, at, more = [], None, offset, True
    with ThreadPoolExecutor(max_workers=8) as pool:
        while len(kept) < limit and at - offset < LOOKED:
            size, start = (limit if totals is None else 600), at
            rows, counts = photoindex.search(db, offset=at, limit=size, counts=totals is None,
                                             **filters)
            totals = totals or counts
            there = pool.map(lambda row: os.path.exists(os.path.join(root, row['path'])), rows)
            for row, found in zip(rows, there):
                if len(kept) == limit:
                    break       # (the rest of these waits for the next page)
                at += 1
                if found:
                    kept.append(row)
            if at - start == len(rows) < size:
                more = False    # (the last photo of the search was looked at)
                break
    gone = at - offset - len(kept)
    sizes = max(1, totals['photos'] // max(1, totals['sequences']))
    totals = dict(totals, sequences=max(0, totals['sequences'] - gone),
                  photos=max(0, totals['photos'] - gone * sizes))
    return kept, totals, at, more


def _raw_app():
    """the application that opens the raw files (config 'raw_app'), None if
    it is not on this machine"""
    name = _config_module.load().get('raw_app')
    places = ('/Applications', os.path.expanduser('~/Applications'))
    return name if name and any(os.path.isdir(os.path.join(place, name + '.app'))
                                for place in places) else None


def _faces(name):
    """the faces of a photo for the page, looked for now if nobody did"""
    from . import faces
    if not faces.available():
        return {'error': "OpenCV n'est pas installé ici : pas de visages (pip install "
                         'opencv-python).'}
    db = photoindex.connect()
    try:
        row = db.execute('SELECT path, taken, faces FROM photos WHERE name = ?', (name,)).fetchone()
        if row is None:
            return {'error': 'photo inconnue'}
        if row['faces'] is None:
            path = _image_file(name, 'preview')
            if not path or not os.path.isfile(photoindex.preview_path(name)):
                return {'error': "La photo est hors d'atteinte : montez le disque d'archive "
                                 'pour y chercher les visages.'}
            from PIL import Image
            faces.scan(db, name, Image.open(path), row['taken'])
        return {'faces': faces.of_photo(db, name)}
    finally:
        db.close()


def _archive():
    """where the archive is expected, and where it is mounted now (None if
    it is not)"""
    roots = photoindex.roots()
    return {'roots': roots, 'mounted': next((root for root in roots if os.path.isdir(root)), None)}


def _inbox_status():
    """the photos that wait to be sent to the archive: the folders looked
    at, how many photos are there and how many the index has"""
    from .doc import inbox_dirs, inbox_files
    db = _db()
    indexed = db.execute("SELECT COUNT(*), COUNT(DISTINCT seq) FROM photos WHERE path LIKE '/%'").fetchone()
    db.close()
    home = os.path.expanduser('~')
    from . import inbox
    act = dict(_act)
    if act.get('started'):
        act['seconds'] = round((act.get('ended') or time.time()) - act['started'])
    return dict(_inbox, dirs=[folder.replace(home, '~', 1) for folder in inbox_dirs()],
                files=len(inbox_files()), photos=indexed[0], sequences=indexed[1],
                plan=inbox.plan(), act=act, copies=_copies()['copies'])


def _inbox_job():
    """look at the folders where photos wait, and bring what is new to the index"""
    from .doc import index_inbox
    try:
        found = index_inbox(tell=lambda done, total: _inbox.update(read=done, total=total))
        _inbox.update(found, state='done', error='')
    except Exception as exc:
        _inbox.update(state='error', error=str(exc))


@contextmanager
def _holding(seq):
    """the files of a sequence, taken while a button of the photos that wait
    renames or moves them (see _claim; None: a file the index does not have);
    False when the action was stopped while it waited for them"""
    mine = seq is None or _claim(seq, _act_stop)
    try:
        yield mine
    finally:
        if mine and seq is not None:
            _release(seq)


def _act_start(what, root=None):
    """start one of the three actions on the photos that wait: 'times',
    'names' or 'send' (to the copy of the archive `root`, the one in use
    without it); why not, when it cannot start"""
    if what not in ('times', 'names', 'send'):
        return 'action inconnue'
    root = (root or _archive()['mounted']) if what == 'send' else None
    if what == 'send' and (root not in photoindex.roots() or not os.path.isdir(root)):
        return "Cette copie de l'archive n'est pas montée : rien ne peut y être envoyé."
    db = _db()
    waiting = {row[0] for row in db.execute("SELECT DISTINCT seq FROM photos WHERE path LIKE '/%'")}
    db.close()
    with _lock:
        if _act.get('state') == 'running' or _inbox.get('state') == 'reading':
            return 'Une action est déjà en cours sur ces photos.'
        asked = (set(_batch['waiting']) | set(_batch['active']) | set(_batch['retry'])
                 | {seq for seq, job in _jobs.items() if job.get('state') in ('running', 'writing')})
        if waiting & asked:
            return ("Claude est en train d'identifier certaines de ces photos : attendez qu'il "
                    "ait fini, ou arrêtez-le.")
        if what == 'send' and _sync.get('state') in ('planning', 'running', 'backup'):
            return ("Une copie entre les disques de l'archive est en cours : attendez qu'elle "
                    'soit finie.')
        _act.clear()
        _act.update(state='running', what=what, started=time.time(), done=0, total=0)
    _act_stop.clear()
    threading.Thread(target=_act_job, args=(what, root), daemon=True).start()
    return None


def _act_job(what, root=None):
    """one of the three actions on the photos that wait, with its counts as
    it goes"""
    from . import inbox
    from .doc import index_inbox
    try:
        index_inbox()       # (the index has every photo that waits, to follow it)
        where = {'root': root} if what == 'send' else {}
        found = getattr(inbox, what)(tell=_act.update, stop=_act_stop, hold=_holding, **where)
        _act.update(found, state='done', stopped=_act_stop.is_set(), ended=time.time())
    except Exception as exc:
        _act.update(state='error', error=str(exc), ended=time.time())
    finally:
        _wake.set()         # (what waited to be written into these files may be now)


def _copies():
    """the copies of the archive as the page gets them, and the copy from
    one to the other if there is one"""
    state = dict(_sync)
    if state.get('started'):
        state['seconds'] = round((state.get('ended') or time.time()) - state['started'])
    return {'copies': [dict(copy, server=bool(copy['remote']), look=_looked(copy['root']))
                       for copy in sync.copies()],
            'sync': state, 'orphans': _orphans_status()}


def _orphans_status():
    """how the look for orphans goes, and what it found last"""
    state = dict(_orphans)
    if state.get('started'):
        state['seconds'] = round(time.time() - state['started'])
    try:
        state['last'] = json.loads(photoindex.meta('orphans') or '{}')
    except ValueError:
        state['last'] = {}
    return state


def _orphans_start():
    """compare the first two copies of the archive that are there, in the
    background; an error to say when that cannot be done"""
    there = [copy['root'] for copy in sync.copies() if copy['mounted']]
    with _lock:
        if _orphans.get('state') == 'running':
            return 'La comparaison est déjà en cours.'
        if _sync.get('state') in ('planning', 'running', 'backup'):
            return "Une copie est en cours : comparez quand elle sera finie."
        if len(there) < 2:
            return 'Branchez les deux copies pour les comparer.'
        _orphans.clear()
        _orphans.update(state='running', seen=0, started=time.time(),
                        total=sum(_files_of(root) for root in there[:2]))
    threading.Thread(target=_orphans_job, args=tuple(there[:2]), daemon=True).start()
    return ''


def _orphans_job(first, second):
    try:
        found = sync.orphans(first, second, tell=lambda seen: _orphans.update(seen=seen))
        db = photoindex.connect()
        photoindex.set_meta(db, 'orphans', json.dumps(found, ensure_ascii=False))
        for root in (first, second):
            # (how many files each copy has, for the bars that come next)
            photoindex.set_meta(db, 'files:' + root, found['files'][sync.name_of(root)])
        db.close()
        _orphans.update(state='done')
    except Exception as exc:
        _orphans.update(state='error', error=str(exc))


def _orient_status():
    """how the look for photos on their side goes, and how many are left"""
    from .doc import orient_left
    db = _db()
    left = orient_left(db)
    turned = db.execute("SELECT COUNT(*) FROM photos WHERE rep = 1 AND oriented = 'claude'").fetchone()[0]
    db.close()
    return dict(_orient, left_now=left, turned_all=turned)


def _orient_job():
    from .doc import orient

    @contextmanager
    def hold(seq):
        mine = _claim(seq, _orient_stop)
        try:
            yield mine
        finally:
            if mine:
                _release(seq)
    try:
        found = orient(stop=_orient_stop, tell=_orient.update, hold=hold)
        _orient.update(found, found=len(found['found']), state='done', ended=time.time())
    except Exception as exc:
        _orient.update(state='error', error=str(exc), ended=time.time())
    finally:
        _wake.set()


def _backup_status():
    """how the backup of the index goes, whether it has its password, and
    what the last one was"""
    from . import backup
    return dict(_backup, password=backup.has_password(), last=backup.last(), hint=backup.hint())


def _backup_job():
    from . import backup
    try:
        backup.run(lambda stage: _backup.update(stage=stage))
        _backup.update(state='done', error='')
    except Exception as exc:
        _backup.update(state='error', error=str(exc))


def _looked(root):
    """what the last listing of a copy of the archive found there, or how the
    one under way goes; {} for a copy never listed"""
    if root in _looks:
        return _looks[root]
    try:
        return dict(json.loads(photoindex.meta('copy:' + root) or '{}'))
    except ValueError:
        return {}


def _look_start(root):
    """list in the background what a copy of the archive holds, unless that
    is under way"""
    with _lock:
        if _looks.get(root, {}).get('state') == 'reading':
            return
        _looks[root] = dict(_looked(root), state='reading')
    threading.Thread(target=_look_job, args=(root,), daemon=True).start()


def _look_job(root):
    from .doc import index_copy
    try:
        _looks[root] = dict(index_copy(root), state='done')
    except Exception as exc:
        _looks[root] = {'state': 'error', 'error': str(exc)}


def _sync_start(src, dst, go):
    """start comparing two copies of the archive, or copying from one to the
    other (go); False when a copy is already under way"""
    with _lock:
        state = _sync.get('state')
        if state in ('planning', 'running', 'backup'):
            return False
        # (what was just compared is not compared again before it is copied)
        known = go and state == 'planned' and (_sync.get('src'), _sync.get('dst')) == (src, dst)
        if known:
            _sync.update(state='running')
        else:
            _sync.update(state='planning', src=src, dst=dst, from_name=sync.name_of(src),
                         to_name=sync.name_of(dst), files=0, bytes=0, sent=0, copied=0, pace='',
                         error='', started=time.time(), ended=0)
    threading.Thread(target=_sync_job, args=(src, dst, go, known), daemon=True).start()
    return True


def _files_of(root):
    """how many files a copy of the archive holds, as last counted: by a
    comparison with another copy, else by a listing of it; 0 when nobody
    counted yet"""
    try:
        return int(photoindex.meta('files:' + root) or 0) or int(_looked(root).get('files') or 0)
    except (TypeError, ValueError):
        return 0


def _sync_job(src, dst, go, known):
    """compare two copies of the archive (what `src` has that is newer than
    in `dst`, or missing there; known: that was just done), and copy it if
    asked (go)"""
    try:
        # the duplicates put aside on one copy (patchdoc doublons) are put
        #   aside on the other too, and the copy leaves them alone
        from . import dupes
        skip = sync.skip_file(dupes.excluded())
        if skip and not known:
            _sync.update(aside=sum(dupes.settle(root) for root in (src, dst) if os.path.isdir(root)))
        if not known:
            _sync_stop.clear()
            _sync.update(seen=0, total=_files_of(src))
            found = sync.plan(src, dst, skip, tell=lambda seen: _sync.update(seen=seen),
                              stop=_sync_stop)
            if found is None:
                _sync.update(state='idle', ended=time.time())       # (stopped)
                return
            _sync.update(found)
            # (how many files that copy has, for the bar of the next comparison)
            db = photoindex.connect()
            photoindex.set_meta(db, 'files:' + src, found['seen'])
            db.close()
        if not go or not _sync['files']:
            if not _sync['files']:
                # nothing to copy: the two copies are alike, the index is left on both
                _sync.update(saved=sync.backup([src, dst]))
            _sync.update(state='planned', ended=time.time())
            return
        _sync_stop.clear()
        _sync.update(state='running', started=time.time(), ended=0, sent=0, copied=0, pace='')
        ended = sync.run(src, dst, stop=_sync_stop, skip=skip,
                         tell=lambda sent, copied, pace: _sync.update(sent=sent, copied=copied,
                                                                      pace=pace))
        if ended == 'done':
            # a copy of the index, which lives on this machine only, on both drives
            _sync.update(state='backup')
            _sync.update(saved=sync.backup([src, dst]))
            if os.path.isdir(dst):
                _look_start(dst)        # (what that copy holds has changed)
        _sync.update(state=ended, ended=time.time())
    except Exception as exc:
        _sync.update(state='error', error=str(exc), ended=time.time())
    finally:
        _wake.set()


def _image_file(name, kind):
    """the thumbnail or the preview of a photo, made if it is missing and the
    photo can be reached; None otherwise"""
    path = photoindex.thumb_path(name) if kind == 'thumb' else photoindex.preview_path(name)
    if os.path.isfile(path):
        return path
    db = _db()
    row = db.execute('SELECT path FROM photos WHERE name = ?', (name,)).fetchone()
    db.close()
    full = photoindex.resolve(row['path']) if row else None
    if not full or not os.path.isfile(full):
        return None
    from .keywords import _image
    with _making:
        if not os.path.isfile(path):
            try:
                photoindex.save_images(_image(full, big=kind == 'preview'), name,
                                       preview=kind == 'preview')
            except Exception:
                return None
    return path if os.path.isfile(path) else None


def _fill(stop):
    """make the thumbnails the store lacks, the newest sequences first, two
    at a time so that the page keeps most of the drive; waits while the
    archive is not mounted"""
    from concurrent.futures import ThreadPoolExecutor
    db = _db()
    rows = db.execute('SELECT name FROM photos WHERE rep = 1 '
                      'ORDER BY COALESCE(local, taken) DESC').fetchall()
    db.close()
    todo = [row['name'] for row in rows if not os.path.isfile(photoindex.thumb_path(row['name']))]
    _store.update(total=len(rows), have=len(rows) - len(todo), filling=bool(todo))
    # first, the pictures kept on their side are made upright (doc.straighten)
    from .doc import straighten
    while not stop.is_set() and not _archive()['mounted']:
        stop.wait(60)
    try:
        straighten(stop, lambda counts: _store.update(straight=counts))
    except Exception as exc:
        _store.update(straight={'error': str(exc)})

    def ready():
        """wait for the archive, and for Claude's sequences to be read: they
        need the drive more than the thumbnails do"""
        while not stop.is_set() and not _archive()['mounted']:
            stop.wait(60)
        while not stop.is_set() and _asking():
            stop.wait(5)
        return not stop.is_set()

    def make(name):
        if ready() and _image_file(name, 'thumb'):
            _store['have'] += 1

    def faces_of(name):
        if ready():
            _faces(name)

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(make, todo))
        _store['filling'] = False
        # then the faces of the photos that are not of birds, the newest first
        if _config_module.load().get('gui_scan_faces', True):
            db = _db()
            unseen = [row['name'] for row in db.execute(
                'SELECT name FROM photos WHERE rep = 1 AND faces IS NULL AND COALESCE(bird, 0) = 0 '
                "AND COALESCE(topic, '') != 'oiseau' ORDER BY COALESCE(local, taken) DESC")]
            db.close()
            list(pool.map(faces_of, unseen))


def _asking():
    """whether Claude is being shown a sequence, or is about to be (a batch
    that waits leaves the drive to the others)"""
    with _lock:
        held = _batch['until'] > time.time() or _batch['away']
        return bool(_batch['active'] or (_batch['waiting'] and not held)
                    or any(job.get('state') == 'running' for job in _jobs.values()))


def _claim(seq, wait=None):
    """take the files of a sequence to rewrite them (two writers would undo
    each other); wait: an Event that ends the wait. False if not taken."""
    while True:
        with _lock:
            if seq not in _writing:
                _writing.add(seq)
                return True
        if wait is None or wait.wait(1):
            return False


def _release(seq):
    with _lock:
        _writing.discard(seq)


def _work(seq, ask, write=True):
    """the work of the buttons: Claude's description of a sequence (ask),
    kept in the index, then written into its files (write; the batch leaves
    that to _resume, so that the sequences are identified at Claude's pace
    and not at the drive's); its state goes to _jobs: 'running', 'writing'
    (with the photo as the index now has it, and how many files are
    written), 'done', or 'error'"""
    from .doc import describe_sequence, write_sequence
    job = {'state': 'running' if ask else 'writing', 'since': time.time()}

    def tell(**changes):
        with _lock:
            job.update(changes)
            _jobs[seq] = dict(job)

    tell()          # (what an earlier try at this sequence left is forgotten)

    def photo():
        db = _db()
        row = db.execute('SELECT * FROM photos WHERE seq = ? AND rep = 1', (seq,)).fetchone()
        db.close()
        return _public(dict(row)) if row else None

    try:
        if ask:
            # (the frames of a burst of bird photos are read one by one first)
            _, cost = describe_sequence(
                seq, progress=lambda done, total, stage=None: tell(measured=done, frames=total,
                                                                   stage=stage))
            with _lock:
                _spent['total'] += cost
        if not write:
            tell(state='done', photo=photo())
            _wake.set()
            return
        tell(state='writing', photo=photo(), written=0, files=0)
        _claim(seq, threading.Event())          # (waits for _resume to let go of it)
        try:
            write_sequence(seq, progress=lambda done, total: tell(written=done, files=total))
        except Exception as exc:
            tell(state='error', photo=photo(),
                 error=("La description est dans la base, mais l'écriture dans les fichiers a "
                        f'échoué : {exc}') if ask else str(exc))
            return
        finally:
            _release(seq)
        tell(state='done', photo=photo())
    except Exception as exc:
        tell(state='error', error=str(exc))


def _eye_file(name):
    """the square around the eye of the bird of a photo, which the loupe of
    the page shows: kept when the photo was described, made from the photo
    itself otherwise if it can be reached; None when no eye is known"""
    db = _db()
    row = db.execute('SELECT path, eye_x, eye_y FROM photos WHERE name = ?', (name,)).fetchone()
    db.close()
    if row is None or row['eye_x'] is None or row['eye_y'] is None:
        return None
    path = photoindex.eye_path(name)
    if os.path.isfile(path):
        return path
    full = photoindex.resolve(row['path'])
    if not full:
        return None
    from .doc import _around, read_frame
    with _making:
        if not os.path.isfile(path):
            try:
                img = read_frame(full)[1]
                photoindex.save_eye(_around(img, row['eye_x'] * img.width, row['eye_y'] * img.height),
                                    name)
            except Exception:
                return None
    return path if os.path.isfile(path) else None


def _batch_add(names):
    """queue for Claude the sequences of these photos that have no
    description yet; what was queued and what was left out"""
    if not _archive()['mounted']:
        return {'error': "Le disque d'archive n'est pas monté : Claude ne peut pas voir les "
                         'séquences.'}
    db = _db()
    described, sizes = 0, {}
    for name in names[:5000]:
        row = db.execute('SELECT seq, seq_n, described FROM photos WHERE name = ?',
                         (str(name),)).fetchone()
        if row is None or not row['seq']:
            continue
        if row['described']:
            described += 1
        else:
            sizes.setdefault(row['seq'], row['seq_n'] or 1)
    db.close()
    return {'queued': _batch_queue(sizes), 'described': described}


def _batch_archive():
    """queue for Claude every sequence of the archive that has no description
    yet, the newest first. The index keeps that this is under way (meta
    'batch_archive'): a server started again goes on with it (_batch_again)."""
    if not _archive()['mounted']:
        return {'error': "Le disque d'archive n'est pas monté : Claude ne peut pas voir les "
                         'séquences.'}
    db = _db()
    sizes = {row['seq']: row['seq_n'] or 1 for row in db.execute(
        f'SELECT seq, seq_n {ARCHIVE_TODO} ORDER BY COALESCE(local, taken) DESC')}
    db.close()
    if not sizes:
        _batch_keep(False)
        return {'queued': 0}
    _batch_keep(True)
    return {'queued': _batch_queue(sizes, archive=True)}


def _batch_keep(on):
    """keep in the index whether the batch of the whole archive is under way"""
    db = photoindex.connect()
    photoindex.set_meta(db, 'batch_archive', '1' if on else '')
    db.close()


def _batch_again(stop):
    """go on with the batch of the whole archive a server left under way,
    once the archive is there"""
    while not stop.is_set() and not _archive()['mounted']:
        stop.wait(60)
    # (unless the page stopped it meanwhile)
    if not stop.is_set() and photoindex.meta('batch_archive') == '1':
        _batch_archive()


def _batch_queue(sizes, archive=False):
    """add sequences to the batch, {sequence: its number of photos}, and
    start the workers it lacks; how many were not in it yet. Those of the
    whole archive go to the end of the queue; those asked for in the page go
    to its front, ahead of an archive that may take days."""
    with _lock:
        if not (_batch['waiting'] or _batch['active']):
            # a new batch: the count of the last one is forgotten
            _batch.update(total=0, done=0, failed=0, note='', finished=[], base=0,
                          started=time.time(), ended=0.0, sizes={}, photos=0, photos_done=0,
                          retry={}, again=set(), streak=0, holds=0, until=0.0, why='',
                          away=False, archive=False)
        busy = set(_batch['active']) | set(_batch['retry'])
        known = busy | set(_batch['waiting'])
        new = [seq for seq in sizes if seq not in known]
        if archive:
            _batch['waiting'] += new
            _batch['archive'] = True
        else:
            _batch['waiting'] = ([seq for seq in sizes if seq not in busy]
                                 + [seq for seq in _batch['waiting'] if seq not in sizes])
        _batch['total'] += len(new)
        _batch['sizes'].update({seq: sizes[seq] for seq in new})
        _batch['photos'] += sum(sizes[seq] for seq in new)
        start = max(0, min(BATCH_JOBS, len(_batch['waiting'])) - _batch['workers'])
        _batch['workers'] += start
    for _ in range(start):
        threading.Thread(target=_batch_worker, daemon=True).start()
    return len(new)


def _batch_stop():
    """drop the sequences of the batch still waiting; those under way finish"""
    with _lock:
        left = len(_batch['waiting']) + len(_batch['retry'])
        _batch['total'] -= left
        _batch['photos'] -= sum(_batch['sizes'].get(seq, 1)
                                for seq in _batch['waiting'] + list(_batch['retry']))
        _batch['waiting'], _batch['retry'], _batch['until'] = [], {}, 0.0
        whole, _batch['archive'] = _batch['archive'], False
        _batch['note'] = (f'Arrêt demandé : {left} séquences retirées de la file ; '
                          'celles en cours se terminent.') if left else _batch['note']
    if whole or photoindex.meta('batch_archive') == '1':
        _batch_keep(False)


def _batch_done(seq, photo, error):
    """count a sequence the batch is through with, and keep what became of it
    for the page (the lock is held)"""
    _batch['failed' if error else 'done'] += 1
    _batch['photos_done'] += _batch['sizes'].get(seq, 1)
    _batch['finished'].append({'seq': seq, 'photo': photo, 'error': error})
    if len(_batch['finished']) >= 2 * KEPT:
        # the oldest are forgotten, with what was kept of their work
        for old in _batch['finished'][:KEPT]:
            if _jobs.get(old['seq'], {}).get('state') in ('done', 'error'):
                del _jobs[old['seq']]
        del _batch['finished'][:KEPT]
        _batch['base'] += KEPT


def _batch_next():
    """what a worker of the batch does next: ('do', a sequence), ('wait',
    seconds) while the batch waits (see STREAK) or the archive is away, or
    ('end', whether the batch of the whole archive is over)"""
    budget = float(_config_module.load().get('doc_budget') or 0)
    mounted = bool(_archive()['mounted'])
    with _lock:
        if _batch['waiting'] and budget and _spent['total'] >= budget:
            _batch['note'] = (f"Plafond d'appels à Claude de cette session atteint : "
                              f"{len(_batch['waiting'])} séquences laissées (config doc_budget ; "
                              'relancez patchdoc gui pour repartir de zéro).')
            # (the batch of the whole archive goes on with the next session)
            _batch['waiting'], _batch['archive'] = [], False
        if not _batch['waiting']:
            _batch['workers'] -= 1
            whole = False
            if not _batch['active']:
                # the end: the sequences that failed and never saw Claude
                #   answer again stay failed
                for seq, error in _batch['retry'].items():
                    _batch_done(seq, None, error)
                _batch['retry'], _batch['until'], _batch['away'] = {}, 0.0, False
                _batch['ended'] = _batch['ended'] or time.time()
                whole, _batch['archive'] = _batch['archive'], False
            return 'end', whole
        _batch['away'] = not mounted
        wait = max(_batch['until'] - time.time(), 0.0 if mounted else 30.0)
        if wait > 0:
            return 'wait', wait
        seq = _batch['waiting'].pop(0)
        _batch['active'].append(seq)
        return 'do', seq


def _batch_worker():
    """take the sequences of the batch one after the other until none is
    left, the batch is stopped, or the calls of the session reach the budget.
    A sequence that fails is tried once more after Claude has answered again;
    STREAK failures in a row make the batch wait before it tries again."""
    while True:
        what, seq = _batch_next()
        if what == 'end':
            if seq:
                _batch_keep(False)
            return
        if what == 'wait':
            time.sleep(min(seq, 5))         # (a batch that is stopped is not kept waiting)
            continue
        _work(seq, True, write=False)
        with _lock:
            job = _jobs.get(seq, {})
            _batch['active'].remove(seq)
            if job.get('state') == 'done':
                _batch_done(seq, job.get('photo'), None)
                _batch['streak'] = _batch['holds'] = 0
                # Claude answers: the sequences that failed have their second try
                _batch['again'].update(_batch['retry'])
                _batch['waiting'][:0] = list(_batch['retry'])
                _batch['retry'] = {}
                continue
            error = job.get('error') or 'échec'
            if seq in _batch['again']:
                _batch_done(seq, None, error)
            else:
                _batch['retry'][seq] = error
            _batch['streak'] += 1
            if _batch['streak'] >= STREAK and _batch['until'] <= time.time():
                _batch['until'] = time.time() + 60 * min(60, HOLD * 2 ** _batch['holds'])
                _batch['holds'] += 1
                _batch['why'] = error


def _batch_status(since=0):
    """how the batch goes, with what became of the sequences finished since
    the `since` first ones: the counts, the seconds it has run, its photos
    (those of the sequences done, and those read so far of the sequences
    under way), what each sequence under way is at, whether it waits, the
    writing into the files that follows, and what the batch of the whole
    archive would have to do"""
    db = _db()
    left = db.execute(f'SELECT COUNT(*) {ARCHIVE_TODO}').fetchone()[0]
    db.close()
    with _lock:
        jobs = [dict({key: _jobs.get(seq, {}).get(key)
                      for key in ('state', 'measured', 'frames', 'stage')},
                     seq=seq, photos=_batch['sizes'].get(seq, 1),
                     seconds=round(time.time() - (_jobs.get(seq, {}).get('since') or time.time())))
                for seq in _batch['active']]
        running = bool(_batch['waiting'] or _batch['active'])
        # (a page that was away for long, or that saw another batch, starts again)
        at = since - _batch['base']
        at = at if 0 <= at <= len(_batch['finished']) else 0
        hold = max(0, round(_batch['until'] - time.time())) if _batch['waiting'] else 0
        return {'total': _batch['total'], 'done': _batch['done'], 'failed': _batch['failed'],
                'waiting': len(_batch['waiting']), 'active': len(_batch['active']),
                'running': running, 'note': _batch['note'],
                'finished': _batch['finished'][at:],
                'next': _batch['base'] + len(_batch['finished']),
                'seconds': round((time.time() if running else _batch['ended'] or time.time())
                                 - _batch['started']) if _batch['started'] else 0,
                'photos': _batch['photos'], 'photos_done': _batch['photos_done'],
                'photos_read': sum(min(job['measured'] or 0, job['photos']) for job in jobs),
                'jobs': jobs, 'writes': dict(_writes),
                'retry': len(_batch['retry']), 'hold': hold,
                'why': _batch['why'] if hold else '', 'away': running and _batch['away'],
                'archive': {'on': running and _batch['archive'], 'left': left}}


def _resume(stop):
    """write into their files what the index learned of sequences without
    writing it yet: those a stopped server left waiting, and those whose
    people were just named; looks again every half minute, when the archive
    is there"""
    from .doc import pending_writes, write_sequence
    while not stop.is_set():
        _wake.clear()
        # (not while the files are being copied from a drive to the other)
        copying = _sync.get('state') in ('planning', 'running', 'backup')
        todo = pending_writes() if _archive()['mounted'] and not copying else []
        for count, seq in enumerate(todo):
            if stop.is_set():
                return
            busy = _jobs.get(seq, {}).get('state') in ('running', 'writing')
            if busy or not _claim(seq):
                continue
            _writes.update(waiting=len(todo) - count, seq=seq, written=0, files=0)
            try:
                write_sequence(seq, progress=lambda done, total: _writes.update(written=done,
                                                                                files=total))
                _writes['error'] = ''
            except Exception as exc:
                _writes['error'] = f'{seq} : {exc}'
            finally:
                _release(seq)
        _writes.update(waiting=0, seq=None, written=0, files=0)
        # again when a sequence has just been described, or in half a minute
        _wake.wait(30)


class Handler(BaseHTTPRequestHandler):
    server_version = 'patchdoc'

    def log_message(self, *args):
        pass

    def _send(self, status, body, kind='application/json; charset=utf-8', cache=False):
        if not isinstance(body, bytes):
            body = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header('Content-Type', kind)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'max-age=86400' if cache else 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.end_headers()
        self.wfile.write(body)

    def _file(self, path, cache=False):
        with open(path, 'rb') as handle:
            body = handle.read()
        kind = mimetypes.guess_type(path)[0] or 'application/octet-stream'
        if kind.startswith('text/') or kind.endswith('javascript'):
            kind += '; charset=utf-8'
        self._send(200, body, kind, cache)

    def _local(self):
        """only requests made to this server's own address are answered"""
        port = self.server.server_address[1]
        if self.headers.get('Host') in (f'127.0.0.1:{port}', f'localhost:{port}'):
            return True
        self._send(403, {'error': "ce n'est pas ce serveur"})
        return False

    def do_GET(self):
        if not self._local():
            return
        url = urlparse(self.path)
        args = {key: values[0] for key, values in parse_qs(url.query).items()}
        try:
            if url.path == '/':
                self._file(os.path.join(STATIC, 'index.html'))
            elif url.path.startswith('/static/'):
                path = os.path.join(STATIC, os.path.basename(url.path))
                if os.path.isfile(path):
                    self._file(path)
                else:
                    self._send(404, {'error': 'fichier introuvable'})
            elif url.path.startswith(('/thumb/', '/preview/')):
                kind, name = url.path.strip('/').split('/', 1)
                name = os.path.basename(unquote(name))
                path = _image_file(name, kind) or (kind == 'preview' and _image_file(name, 'thumb'))
                if path:
                    self._file(path, cache=True)
                else:
                    self._send(404, {'error': "pas d'image"})
            elif url.path.startswith('/eye/'):
                path = _eye_file(os.path.basename(unquote(url.path[len('/eye/'):])))
                if path:
                    self._file(path, cache=True)
                else:
                    self._send(404, {'error': "pas d'œil connu"})
            elif url.path == '/api/facets':
                db = _db()
                from .doc import _cost_guess
                cfg = _config_module.load()
                each = _cost_guess(cfg.get('keywords_model'), cfg.get('doc_effort'))[0]
                facets = dict(photoindex.facets(db), index=photoindex.index_dir(),
                              archive=_archive(), thumbs=dict(_store), raw_app=_raw_app(),
                              faces=__import__('patchnames.faces', fromlist=['x']).available(),
                              pano=__import__('patchnames.pano', fromlist=['x']).available(),
                              claude={'each': each, 'spent': _spent['total'],
                                      'budget': float(cfg.get('doc_budget') or 0)},
                              names=_names(db), remote=remote.status(ask=False))
                db.close()
                facets['regions'] = [(state, country, country_name(country or ''), count)
                                     for state, country, count in facets['regions']]
                self._send(200, facets)
            elif url.path == '/api/search':
                db = _db()
                filters = {key: args[key] for key in FILTERS if args.get(key)}
                offset, limit = int(args.get('offset') or 0), min(int(args.get('limit') or 60), 200)
                copy = filters.get('place_of', '')
                if copy in photoindex.roots():
                    # what one copy of the archive holds: looked for on the copy itself
                    rows, totals, then, more = _held(db, copy, offset, limit,
                                                     listed=args.get('listed') == '1', **filters)
                    photos = [_public(row, copy) for row in rows]
                else:
                    rows, totals = photoindex.search(db, offset=offset, limit=limit,
                                                     listed=args.get('listed') == '1', **filters)
                    photos, then, more = [_public(row) for row in rows], offset + len(rows), len(rows) == limit
                db.close()
                self._send(200, {'photos': photos, 'totals': totals, 'next': then, 'more': more})
            elif url.path == '/api/places':
                db = _db()
                filters = {key: args[key] for key in FILTERS if args.get(key)}
                self._send(200, photoindex.places(db, listed=args.get('listed') == '1', **filters))
                db.close()
            elif url.path == '/api/sequence':
                db = _db()
                self._send(200, photoindex.sequence(db, args.get('seq', '')))
                db.close()
            elif url.path == '/api/day':
                db = _db()
                self._send(200, photoindex.checklists_of(db, args.get('date', '')[:10]))
                db.close()
            elif url.path == '/api/checklist':
                db = _db()
                self._send(200, photoindex.checklist(db, args.get('sid', '')) or {})
                db.close()
            elif url.path == '/api/job':
                with _lock:
                    self._send(200, _jobs.get(args.get('id', ''), {'state': 'unknown'}))
            elif url.path == '/api/batch':
                self._send(200, _batch_status(int(args.get('since') or 0)))
            elif url.path == '/api/archive':
                self._send(200, _copies())
            elif url.path == '/api/inbox':
                self._send(200, _inbox_status())
            elif url.path == '/api/backup':
                self._send(200, _backup_status())
            elif url.path == '/api/orient':
                self._send(200, _orient_status())
            elif url.path == '/api/faces':
                self._send(200, _faces(os.path.basename(args.get('name', ''))))
            else:
                self._send(404, {'error': 'introuvable'})
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:
            self._send(500, {'error': str(exc)})

    def do_POST(self):
        if not self._local():
            return
        # a header no other site can send without asking first, and never gets
        if self.headers.get('X-Patchdoc') != '1' or self.path not in (
                '/api/reveal', '/api/open', '/api/describe', '/api/write', '/api/batch',
                '/api/batch/stop', '/api/face', '/api/pano', '/api/rotate', '/api/archive/use',
                '/api/archive/look', '/api/sync',
                '/api/sync/stop', '/api/species', '/api/inbox', '/api/inbox/act',
                '/api/inbox/stop', '/api/backup', '/api/quit', '/api/attach', '/api/orient',
                '/api/orphans'):
            self._send(403, {'error': 'refusé'})
            return
        try:
            body = json.loads(self.rfile.read(int(self.headers.get('Content-Length') or 0)))
            if self.path == '/api/orphans':
                # look for orphans between the two copies, or show the list found last
                said = ''
                if body.get('show'):
                    listed = os.path.join(photoindex.index_dir(), sync.ORPHANS)
                    if os.path.isfile(listed) and sys.platform == 'darwin':
                        subprocess.run(['open', '-R', listed], capture_output=True)
                else:
                    said = _orphans_start()
                self._send(200, dict(_copies(), error=said))
                return
            if self.path == '/api/batch':
                self._send(200, _batch_archive() if body.get('all')
                           else _batch_add(list(body.get('names') or [])))
                return
            if self.path in ('/api/archive/use', '/api/archive/look', '/api/sync',
                             '/api/sync/stop'):
                # (the copies of the archive are named by the config, never by the page)
                roots = photoindex.roots()
                if self.path == '/api/sync/stop':
                    _sync_stop.set()
                    if _sync.get('state') == 'planned':
                        _sync.update(state='idle')
                elif self.path in ('/api/archive/use', '/api/archive/look'):
                    root = str(body.get('root'))
                    if root not in roots or not os.path.isdir(root):
                        self._send(200, dict(_copies(), error="Cette copie de l'archive n'est pas "
                                                                'branchée.'))
                        return
                    _config_module.save({'photo_archive_use': root})
                    if self.path == '/api/archive/look':
                        _look_start(root)
                else:
                    src, dst = str(body.get('src')), str(body.get('dst'))
                    if src not in roots or dst not in roots or src == dst:
                        self._send(200, dict(_copies(), error='copies inconnues'))
                        return
                    if not _sync_start(src, dst, bool(body.get('go'))):
                        self._send(200, dict(_copies(), error='Une copie est déjà en cours.'))
                        return
                self._send(200, _copies())
                return
            if self.path == '/api/orient':
                if body.get('go'):
                    with _lock:
                        busy = _orient.get('state') == 'running'
                        if not busy:
                            _orient.clear()
                            _orient.update(state='running', started=time.time(), looked=0, turned=0)
                    if not busy:
                        _orient_stop.clear()
                        threading.Thread(target=_orient_job, daemon=True).start()
                else:
                    _orient_stop.set()
                self._send(200, _orient_status())
                return
            if self.path == '/api/attach':
                # a sequence tied to a checklist by hand, or untied
                from .doc import attach
                db = _db()
                row = db.execute('SELECT seq FROM photos WHERE name = ?',
                                 (str(body.get('name')),)).fetchone()
                db.close()
                try:
                    if row is None:
                        raise RuntimeError('photo inconnue')
                    fixed = attach(row['seq'], str(body['sid']) if body.get('sid') else None)
                except RuntimeError as exc:
                    self._send(200, {'error': str(exc)})
                    return
                _wake.set()         # (the files are to say it)
                self._send(200, {'photo': _public(fixed)})
                return
            if self.path == '/api/quit':
                self._send(200, {'ok': True})
                # (from another thread: this one is inside the loop it ends)
                threading.Thread(target=self.server.shutdown, daemon=True).start()
                return
            if self.path == '/api/backup':
                with _lock:
                    busy = _backup.get('state') == 'running'
                    if not busy:
                        _backup.update(state='running', stage='', error='')
                if not busy:
                    threading.Thread(target=_backup_job, daemon=True).start()
                self._send(200, _backup_status())
                return
            if self.path == '/api/inbox/act':
                refused = _act_start(str(body.get('what')), body.get('root') and str(body['root']))
                self._send(200, dict(_inbox_status(), refused=refused or ''))
                return
            if self.path == '/api/inbox/stop':
                _act_stop.set()
                self._send(200, _inbox_status())
                return
            if self.path == '/api/inbox':
                with _lock:
                    # (not while their files are being renamed or moved)
                    busy = _inbox.get('state') == 'reading' or _act.get('state') == 'running'
                    if not busy:
                        _inbox.update(state='reading', read=0, total=0, error='')
                if not busy:
                    threading.Thread(target=_inbox_job, daemon=True).start()
                self._send(200, _inbox_status())
                return
            if self.path == '/api/species':
                # the photographer sets an identification right
                from .doc import correct_species
                db = _db()
                row = db.execute('SELECT seq FROM photos WHERE name = ?',
                                 (str(body.get('name')),)).fetchone()
                db.close()
                if row is None or not row['seq']:
                    self._send(200, {'error': 'photo inconnue'})
                    return
                try:
                    fixed = correct_species(row['seq'], str(body.get('old') or ''),
                                            str(body.get('new') or ''))
                except RuntimeError as exc:
                    self._send(200, {'error': str(exc)})
                    return
                _wake.set()         # (the files of the sequence are to be written again)
                self._send(200, {'photo': _public(fixed)})
                return
            if self.path == '/api/rotate':
                # a quarter turn of the photo on screen, or of its whole sequence
                from .doc import turn
                db = _db()
                row = db.execute('SELECT seq FROM photos WHERE name = ?',
                                 (str(body.get('name')),)).fetchone()
                db.close()
                if row is None:
                    self._send(200, {'error': 'photo inconnue'})
                    return
                if not _claim(row['seq']):
                    self._send(200, {'error': "Les fichiers de cette séquence sont en train d'être "
                                              'écrits : réessayez dans un instant.'})
                    return
                try:
                    done = turn(str(body.get('name')), str(body.get('way')), bool(body.get('all')))
                except RuntimeError as exc:
                    self._send(200, {'error': f'La photo n\'a pas été tournée : {exc}.'})
                    return
                finally:
                    _release(row['seq'])
                _wake.set()         # (the place of an eye is to be written again)
                db = _db()
                shown = db.execute('SELECT * FROM photos WHERE seq = (SELECT seq FROM photos '
                                   'WHERE name = ?) AND rep = 1', (done[0],)).fetchone()
                frames = photoindex.sequence(db, shown['seq']) if shown else []
                db.close()
                self._send(200, {'turned': done, 'photo': _public(dict(shown)) if shown else None,
                                 'frames': frames})
                return
            if self.path == '/api/pano':
                from .doc import panorama_of
                db = _db()
                row = db.execute('SELECT seq FROM photos WHERE name = ?',
                                 (str(body.get('name')),)).fetchone()
                db.close()
                try:
                    self._send(200, {'photo': _public(panorama_of(row['seq']))} if row
                               else {'error': 'photo inconnue'})
                except RuntimeError as exc:
                    self._send(200, {'error': f'Pas de panorama : {exc}.'})
                return
            if self.path == '/api/face':
                from . import faces
                db = photoindex.connect()
                face = db.execute('SELECT photo FROM faces WHERE id = ?', (body.get('id'),)).fetchone()
                if face is None:
                    self._send(404, {'error': 'visage inconnu'})
                else:
                    faces.name(db, body['id'], str(body.get('person') or ''), body.get('born'))
                    row = db.execute('SELECT * FROM photos WHERE seq = (SELECT seq FROM photos '
                                     'WHERE name = ?) AND rep = 1', (face['photo'],)).fetchone()
                    self._send(200, {'faces': faces.of_photo(db, face['photo']),
                                     'photo': _public(dict(row)) if row else None,
                                     'people': photoindex.facets(db)['people'],
                                     'names': _names(db)})
                db.close()
                return
            if self.path == '/api/batch/stop':
                _batch_stop()
                self._send(200, {'ok': True})
                return
            db = _db()
            row = db.execute('SELECT path, seq FROM photos WHERE name = ?',
                             (str(body.get('name')),)).fetchone()
            db.close()
            if self.path in ('/api/describe', '/api/write'):
                ask = self.path == '/api/describe'
                budget = float(_config_module.load().get('doc_budget') or 0)
                if row is None:
                    self._send(404, {'error': 'photo inconnue'})
                elif ask and budget and _spent['total'] >= budget:
                    self._send(200, {'error': "Le plafond d'appels à Claude de cette session est "
                                              'atteint (config doc_budget ; relancez patchdoc gui '
                                              'pour repartir de zéro).'})
                else:
                    with _lock:
                        busy = _jobs.get(row['seq'], {}).get('state') in ('running', 'writing')
                        if not busy:
                            _jobs[row['seq']] = {'state': 'running' if ask else 'writing'}
                    if not busy:
                        threading.Thread(target=_work, args=(row['seq'], ask),
                                         daemon=True).start()
                    self._send(200, {'job': row['seq']})
                return
            full = photoindex.resolve(row['path']) if row else None
            if not full:
                self._send(404, {'error': "le fichier est hors d'atteinte (le disque d'archive "
                                          'est-il monté ?)'})
                return
            how = ['-R'] if self.path == '/api/reveal' else []
            if body.get('app') and _raw_app() and full.lower().endswith(RAW):
                how = ['-a', _raw_app()]        # the application of the config, never one asked for
            if sys.platform == 'darwin':
                subprocess.run(['open'] + how + [full], capture_output=True)
            self._send(200, {'ok': True})
        except Exception as exc:
            self._send(500, {'error': str(exc)})


def serve(port=None, browser=True, fill=None):
    """run the search page until Ctrl-C (fill: make the missing thumbnails in
    the background; None as config 'gui_fill_thumbs' says)"""
    cfg = _config_module.load()
    port = int(port or cfg['gui_port'])
    fill = cfg.get('gui_fill_thumbs', True) if fill is None else fill
    photoindex.connect().close()        # make the index if there is none yet
    try:
        server = ThreadingHTTPServer(('127.0.0.1', port), Handler)
    except OSError as exc:
        print(f'Le port {port} est pris ({exc}) ; essayez `patchdoc gui --port {port + 1}`.')
        return
    server.daemon_threads = True
    url = f'http://127.0.0.1:{port}/'
    archive = _archive()
    print(f'patchdoc : la page de recherche est à {url}  (Ctrl-C pour arrêter)')
    print(f'  index : {photoindex.index_dir()}')
    print(f"  archive : {archive['mounted'] or 'non montée (' + ', '.join(archive['roots']) + ')'}")
    stop = threading.Event()
    threading.Thread(target=_resume, args=(stop,), daemon=True).start()
    # (whether the server of the archive has exiftool: asked once, ahead of the page)
    threading.Thread(target=remote.status, daemon=True).start()
    if fill:
        print('  vignettes : celles qui manquent sont faites en arrière-plan (--no-vignettes '
              'pour ne pas le faire)')
        threading.Thread(target=_fill, args=(stop,), daemon=True).start()
    if photoindex.meta('batch_archive') == '1':
        print("  identification de toute l'archive : elle reprend (le bouton « Arrêter » de la "
              'page y met fin)')
        threading.Thread(target=_batch_again, args=(stop,), daemon=True).start()
    if browser:
        threading.Timer(0.4, webbrowser.open, (url,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print()
    finally:
        stop.set()
        server.server_close()
