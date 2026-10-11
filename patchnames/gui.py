"""
The search page of the photos (`patchdoc gui`): a small web server, for this
machine only, over the index of photoindex.py.

The page (gui/index.html, app.js) asks for:

    /api/facets              what the filters offer
    /api/search?...          the sequences that pass the filters
    /api/sequence?seq=       the frames of a sequence
    /api/checklist?sid=      an eBird checklist and its species
    /thumb/NAME /preview/NAME   the images of a photo
    /api/reveal, /api/open   (POST) show a photo in the Finder, open it

Thumbnails and previews come from the index folder; one that is missing is
made from the photo itself when its file can be reached (the archive drive
mounted), and kept.

The server listens on 127.0.0.1 only, answers only to its own address (so
that no other site can reach it through the browser), and the two actions
that touch the machine take a photo of the index, never a path.
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
           'flag', 'checklist', 'sort')
#: images made from the photos at the same time, at most (the archive may be
#:  a slow network drive)
_making = threading.Semaphore(4)


def _db():
    """a read-only connection to the index"""
    path = os.path.join(photoindex.index_dir(), 'photos.sqlite')
    db = sqlite3.connect(f'file:{path}?mode=ro', uri=True, timeout=30)
    db.row_factory = sqlite3.Row
    return db


def _public(row):
    """a photo as the page gets it"""
    out = {key: value for key, value in row.items() if key != 'place_norm'}
    out['country_name'] = country_name(row.get('country') or '') if row.get('country') else ''
    return out


def _image_file(name, kind):
    """the thumbnail or the preview of a photo, made if it is missing and the
    photo can be reached; None otherwise"""
    path = photoindex.thumb_path(name) if kind == 'thumb' else photoindex.preview_path(name)
    if os.path.isfile(path):
        return path
    db = _db()
    row = db.execute('SELECT path FROM photos WHERE name = ?', (name,)).fetchone()
    db.close()
    if row is None or not row['path'] or not os.path.isfile(row['path']):
        return None
    from .keywords import _image
    with _making:
        if not os.path.isfile(path):
            try:
                photoindex.save_images(_image(row['path'], big=kind == 'preview'), name,
                                       preview=kind == 'preview')
            except Exception:
                return None
    return path if os.path.isfile(path) else None


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
        self._send(403, {'error': 'not this server'})
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
                    self._send(404, {'error': 'no such file'})
            elif url.path.startswith(('/thumb/', '/preview/')):
                kind, name = url.path.strip('/').split('/', 1)
                name = os.path.basename(unquote(name))
                path = _image_file(name, kind) or (kind == 'preview' and _image_file(name, 'thumb'))
                if path:
                    self._file(path, cache=True)
                else:
                    self._send(404, {'error': 'no image'})
            elif url.path == '/api/facets':
                db = _db()
                facets = dict(photoindex.facets(db), index=photoindex.index_dir())
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
            elif url.path == '/api/sequence':
                db = _db()
                self._send(200, photoindex.sequence(db, args.get('seq', '')))
                db.close()
            elif url.path == '/api/checklist':
                db = _db()
                self._send(200, photoindex.checklist(db, args.get('sid', '')) or {})
                db.close()
            else:
                self._send(404, {'error': 'not found'})
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:
            self._send(500, {'error': str(exc)})

    def do_POST(self):
        if not self._local():
            return
        # a header no other site can send without asking first, and never gets
        if self.headers.get('X-Patchdoc') != '1' or self.path not in ('/api/reveal', '/api/open'):
            self._send(403, {'error': 'refused'})
            return
        try:
            body = json.loads(self.rfile.read(int(self.headers.get('Content-Length') or 0)))
            db = _db()
            row = db.execute('SELECT path FROM photos WHERE name = ?',
                             (str(body.get('name')),)).fetchone()
            db.close()
            if row is None or not row['path'] or not os.path.exists(row['path']):
                self._send(404, {'error': 'the file cannot be reached (the archive drive?)'})
                return
            if sys.platform == 'darwin':
                subprocess.run(['open'] + (['-R'] if self.path == '/api/reveal' else [])
                               + [row['path']], capture_output=True)
            self._send(200, {'ok': True})
        except Exception as exc:
            self._send(500, {'error': str(exc)})


def serve(port=None, browser=True):
    """run the search page until Ctrl-C"""
    port = int(port or _config_module.load()['gui_port'])
    photoindex.connect().close()        # make the index if there is none yet
    try:
        server = ThreadingHTTPServer(('127.0.0.1', port), Handler)
    except OSError as exc:
        print(f'Port {port} is taken ({exc}); try `patchdoc gui --port {port + 1}`.')
        return
    server.daemon_threads = True
    url = f'http://127.0.0.1:{port}/'
    print(f'patchdoc: the search page is at {url}  (Ctrl-C to stop)')
    print(f'  index: {photoindex.index_dir()}')
    if browser:
        threading.Timer(0.4, webbrowser.open, (url,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print()
    finally:
        server.server_close()
