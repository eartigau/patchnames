"""
The search page of the photos (`patchdoc gui`): a small web server, for this
machine only, over the index of photoindex.py.

The page (gui/index.html, app.js) asks for:

    /api/facets              what the filters offer
    /api/search?...          the sequences that pass the filters
    /api/places?...          their places, for the map
    /api/sequence?seq=       the frames of a sequence
    /api/checklist?sid=      an eBird checklist and its species
    /thumb/NAME /preview/NAME   the images of a photo
    /api/reveal, /api/open   (POST) show a photo in the Finder, open it
    /api/describe            (POST) have Claude describe a sequence
    /api/write               (POST) write into its files what the index says
    /api/job?id=             how that is going
    /api/batch               (POST) the same for a list of sequences, in the
                             background; (GET) how that is going
    /api/batch/stop          (POST) drop the sequences still waiting
    /api/faces?name=         the faces of a photo, looked for if nobody did
    /api/face                (POST) who a face is

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
comes back. The calls of a session stop at config 'doc_budget'.

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
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

from . import config as _config_module
from . import photoindex
from .ebird import country_name

STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'gui')
#: the filters of /api/search (see photoindex._where)
FILTERS = ('q', 'species', 'place', 'region', 'year', 'since', 'until', 'topic', 'kind',
           'flag', 'checklist', 'sort', 'section', 'person', 'eye', 'clear', 'level')
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
          'finished': [], 'workers': 0}
#: sequences of a batch done at the same time (each reads and rewrites files
#:  of the archive)
BATCH_JOBS = 2


def _db():
    """a read-only connection to the index"""
    path = os.path.join(photoindex.index_dir(), 'photos.sqlite')
    db = sqlite3.connect(f'file:{path}?mode=ro', uri=True, timeout=30)
    db.row_factory = sqlite3.Row
    return db


def _public(row):
    """a photo as the page gets it: its path where it can be read now, or as
    the index keeps it (relative to the archive) when it cannot"""
    out = {key: value for key, value in row.items() if key != 'place_norm'}
    out['country_name'] = country_name(row.get('country') or '') if row.get('country') else ''
    full = photoindex.resolve(row.get('path'))
    out['reachable'] = bool(full)
    out['path'] = full or row.get('path')
    return out


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

    def make(name):
        while not stop.is_set() and not _archive()['mounted']:
            stop.wait(60)
        if not stop.is_set() and _image_file(name, 'thumb'):
            _store['have'] += 1

    def faces_of(name):
        while not stop.is_set() and not _archive()['mounted']:
            stop.wait(60)
        if not stop.is_set():
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


def _work(seq, ask):
    """the work of the buttons: Claude's description of a sequence (ask),
    kept in the index, then written into its files; its state goes to _jobs:
    'running', 'writing' (with the photo as the index now has it, and how
    many files are written), 'done', or 'error'"""
    from .doc import describe_sequence, write_sequence
    job = {'state': 'running'}

    def tell(**changes):
        with _lock:
            job.update(changes)
            _jobs[seq] = dict(job)

    def photo():
        db = _db()
        row = db.execute('SELECT * FROM photos WHERE seq = ? AND rep = 1', (seq,)).fetchone()
        db.close()
        return _public(dict(row)) if row else None

    try:
        if ask:
            _, cost = describe_sequence(seq)
            with _lock:
                _spent['total'] += cost
        tell(state='writing', photo=photo(), written=0, files=0)
        try:
            write_sequence(seq, progress=lambda done, total: tell(written=done, files=total))
        except Exception as exc:
            tell(state='error', photo=photo(),
                 error=("La description est dans la base, mais l'écriture dans les fichiers a "
                        f'échoué : {exc}') if ask else str(exc))
            return
        tell(state='done', photo=photo())
    except Exception as exc:
        tell(state='error', error=str(exc))


def _batch_add(names):
    """queue for Claude the sequences of these photos that have no
    description yet; what was queued and what was left out"""
    if not _archive()['mounted']:
        return {'error': "Le disque d'archive n'est pas monté : Claude ne peut pas voir les "
                         'séquences.'}
    db = _db()
    seqs, described = [], 0
    for name in names[:5000]:
        row = db.execute('SELECT seq, described FROM photos WHERE name = ?', (str(name),)).fetchone()
        if row is None or not row['seq']:
            continue
        if row['described']:
            described += 1
        elif row['seq'] not in seqs:
            seqs.append(row['seq'])
    db.close()
    with _lock:
        if not (_batch['waiting'] or _batch['active']):
            # a new batch: the count of the last one is forgotten
            _batch.update(total=0, done=0, failed=0, note='', finished=[])
        new = [seq for seq in seqs if seq not in _batch['waiting'] and seq not in _batch['active']]
        _batch['waiting'] += new
        _batch['total'] += len(new)
        start = max(0, min(BATCH_JOBS, len(_batch['waiting'])) - _batch['workers'])
        _batch['workers'] += start
    for _ in range(start):
        threading.Thread(target=_batch_worker, daemon=True).start()
    return {'queued': len(new), 'described': described}


def _batch_worker():
    """take the sequences of the batch one after the other until none is
    left, the batch is stopped, or the calls of the session reach the budget"""
    while True:
        budget = float(_config_module.load().get('doc_budget') or 0)
        with _lock:
            if _batch['waiting'] and budget and _spent['total'] >= budget:
                _batch['note'] = (f"Plafond d'appels à Claude de cette session atteint : "
                                  f"{len(_batch['waiting'])} séquences laissées (config doc_budget ; "
                                  'relancez patchdoc gui pour repartir de zéro).')
                _batch['waiting'] = []
            if not _batch['waiting']:
                _batch['workers'] -= 1
                return
            seq = _batch['waiting'].pop(0)
            _batch['active'].append(seq)
        _work(seq, True)
        with _lock:
            job = _jobs.get(seq, {})
            _batch['active'].remove(seq)
            _batch['done' if job.get('state') == 'done' else 'failed'] += 1
            _batch['finished'].append({'seq': seq, 'photo': job.get('photo'),
                                       'error': job.get('error')})


def _batch_status(since=0):
    """how the batch goes, with what became of the sequences finished since
    the `since` first ones"""
    with _lock:
        return {'total': _batch['total'], 'done': _batch['done'], 'failed': _batch['failed'],
                'waiting': len(_batch['waiting']), 'active': len(_batch['active']),
                'running': bool(_batch['waiting'] or _batch['active']), 'note': _batch['note'],
                'finished': _batch['finished'][since:], 'next': len(_batch['finished'])}


def _resume(stop):
    """write into their files what the index learned of sequences without
    writing it yet: those a stopped server left waiting, and those whose
    people were just named; looks again every half minute, when the archive
    is there"""
    from .doc import pending_writes, write_sequence
    while not stop.is_set():
        busy = {seq for seq, job in list(_jobs.items()) if job.get('state') in ('running', 'writing')}
        for seq in pending_writes() if _archive()['mounted'] else []:
            if stop.is_set():
                return
            if seq not in busy:
                try:
                    write_sequence(seq)
                except Exception:
                    pass
        stop.wait(30)


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
            elif url.path == '/api/facets':
                db = _db()
                from .doc import _cost_guess
                cfg = _config_module.load()
                each = _cost_guess(cfg.get('keywords_model'), cfg.get('doc_effort'))[0]
                facets = dict(photoindex.facets(db), index=photoindex.index_dir(),
                              archive=_archive(), thumbs=dict(_store), raw_app=_raw_app(),
                              faces=__import__('patchnames.faces', fromlist=['x']).available(),
                              claude={'each': each, 'spent': _spent['total'],
                                      'budget': float(cfg.get('doc_budget') or 0)})
                db.close()
                facets['regions'] = [(state, country, country_name(country or ''), count)
                                     for state, country, count in facets['regions']]
                self._send(200, facets)
            elif url.path == '/api/search':
                db = _db()
                filters = {key: args[key] for key in FILTERS if args.get(key)}
                rows, totals = photoindex.search(
                    db, offset=int(args.get('offset') or 0),
                    limit=min(int(args.get('limit') or 60), 200),
                    listed=args.get('listed') == '1', **filters)
                db.close()
                self._send(200, {'photos': [_public(row) for row in rows], 'totals': totals})
            elif url.path == '/api/places':
                db = _db()
                filters = {key: args[key] for key in FILTERS if args.get(key)}
                self._send(200, photoindex.places(db, listed=args.get('listed') == '1', **filters))
                db.close()
            elif url.path == '/api/sequence':
                db = _db()
                self._send(200, photoindex.sequence(db, args.get('seq', '')))
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
                '/api/batch/stop', '/api/face'):
            self._send(403, {'error': 'refusé'})
            return
        try:
            body = json.loads(self.rfile.read(int(self.headers.get('Content-Length') or 0)))
            if self.path == '/api/batch':
                self._send(200, _batch_add(list(body.get('names') or [])))
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
                                     'people': photoindex.facets(db)['people']})
                db.close()
                return
            if self.path == '/api/batch/stop':
                with _lock:
                    left = len(_batch['waiting'])
                    _batch['total'] -= left
                    _batch['waiting'] = []
                    _batch['note'] = (f'Arrêt demandé : {left} séquences retirées de la file ; '
                                      'celles en cours se terminent.') if left else _batch['note']
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
    if fill:
        print('  vignettes : celles qui manquent sont faites en arrière-plan (--no-vignettes '
              'pour ne pas le faire)')
        threading.Thread(target=_fill, args=(stop,), daemon=True).start()
    if browser:
        threading.Timer(0.4, webbrowser.open, (url,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print()
    finally:
        stop.set()
        server.server_close()
