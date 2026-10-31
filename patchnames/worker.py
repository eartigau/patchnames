"""
The batch of the archive on the machine that holds the archive.

A laptop goes to sleep, and every photo has to cross the network to it. The
server the archive is mounted from does neither, so it can take the batch of
the whole archive over: a copy of patchnames, of the index, of the eBird data
and of the explanations run there, under its own Claude login, with a search
page nobody looks at (it listens on that machine only), and the files are
written where they are.

    python -m patchnames.worker status
    python -m patchnames.worker start [--until 11:00] [--jobs 6]
    python -m patchnames.worker fetch
    python -m patchnames.worker stop

start stops the batch of this machine (a sequence is never asked twice),
sends a copy of the index and starts the batch there. fetch brings back here
what it described: the rows of the sequences whose files it has finished
writing, and their images. The index of this machine stays the one that is
searched: a sequence described here since, or whose species the photographer
checked here, is left as it is, and what this machine alone knows (the faces
and the people) is not touched.
"""

import argparse
import json
import os
import shlex
import sqlite3
import subprocess
import sys
import time
import urllib.request
from datetime import datetime

from . import config as _config_module
from . import photoindex
from . import sync

#: where things are on the worker, under its home
CODE, INDEX, PORT = 'patchnames', 'patchdoc', 8765
ENV = 'export PATH="$HOME/.local/bin:$PATH"; '
SSH = ['ssh', '-oBatchMode=yes', '-oConnectTimeout=20']
#: what a row of the worker may not change here
KEEP = ('id', 'faces', 'people', 'guessed')
#: read on the worker: the sequences described since a date, as JSON
EXPORT = """
import json, os, sqlite3, sys
db = sqlite3.connect('file:' + os.path.expanduser('~/patchdoc/photos.sqlite') + '?mode=ro', uri=True, timeout=30)
db.row_factory = sqlite3.Row
rows = db.execute("SELECT * FROM photos WHERE seq IN (SELECT seq FROM photos WHERE described > ? "
                  "AND described != 'file')", (sys.argv[1],)).fetchall()
json.dump([dict(row) for row in rows], sys.stdout)
"""


def host_of():
    """user@host of the server the archive is mounted from"""
    for root in _config_module.archive_roots():
        far = sync.remote_of(root) if os.path.isdir(root) else None
        if far:
            return far.partition(':')[0]
    raise RuntimeError("aucune copie de l'archive n'est montée depuis un serveur")


def ssh(host, command, data=None, timeout=600):
    return subprocess.run(SSH + [host, ENV + command], input=data, capture_output=True, timeout=timeout)


def logged_in(host):
    out = ssh(host, 'claude auth status', timeout=60).stdout
    try:
        return bool(json.loads(out).get('loggedIn'))
    except ValueError:
        return False


def _running(host):
    """how the batch goes on the worker; None when its server does not answer"""
    out = ssh(host, f'curl -s -m 20 http://127.0.0.1:{PORT}/api/batch', timeout=60).stdout
    try:
        return json.loads(out)
    except ValueError:
        return None


def _post(host, path, body='{}'):
    return ssh(host, f"curl -s -m 120 -X POST -H 'X-Patchdoc: 1' -H 'Content-Type: application/json' "
                     f"-d {shlex.quote(body)} http://127.0.0.1:{PORT}{path}", timeout=180).stdout


def _stop_here():
    """stop the batch of the search page of this machine, and wait for the
    sequences under way; False if it has no server running"""
    base = f"http://127.0.0.1:{_config_module.load()['gui_port']}"
    try:
        request = urllib.request.Request(base + '/api/batch/stop', b'{}', {
            'Content-Type': 'application/json', 'X-Patchdoc': '1'})
        urllib.request.urlopen(request, timeout=60).read()
    except OSError:
        return False
    for _ in range(100):
        with urllib.request.urlopen(base + '/api/batch', timeout=60) as reply:
            status = json.load(reply)
        print(f"  ici : {status['active']} en cours", end='\r', flush=True)
        if not status['active']:
            break
        time.sleep(6)
    print()
    return True


def _send(host):
    """the code, the explanations, the eBird data and a copy of the index"""
    root = os.path.dirname(os.path.abspath(__file__))
    cfg = _config_module.load()
    rsync = [sync._rsync()[0], '-rtz', '-e', ' '.join(SSH)]
    subprocess.run(rsync + ['--delete', '--exclude=__pycache__', root, f'{host}:{CODE}/'], check=True,
                   capture_output=True)
    explanations = os.path.expanduser(cfg['keywords_instructions'])
    subprocess.run(rsync + [explanations, f'{host}:.config/patchnames/keywords.md'], check=True,
                   capture_output=True)
    subprocess.run(rsync + [os.path.expanduser(cfg['ebird_dir']) + '/', f'{host}:.config/patchnames/ebird/'],
                   check=True, capture_output=True)
    copy = os.path.join(photoindex.index_dir(), 'photos.pour_serveur.sqlite')
    if os.path.exists(copy):
        os.remove(copy)
    live = sqlite3.connect(f'file:{photoindex.db_path()}?mode=ro', uri=True, timeout=60)
    fresh = sqlite3.connect(copy)
    with fresh:
        live.backup(fresh)
    live.close()
    # (what still waits to be written here is written from here)
    fresh.execute("UPDATE photos SET written = 'ici' WHERE written = 'pending'")
    fresh.execute("DELETE FROM meta WHERE key = 'batch_archive'")
    fresh.commit()
    fresh.execute('PRAGMA journal_mode = DELETE')
    fresh.close()
    ssh(host, f'mkdir -p {INDEX} && rm -f {INDEX}/photos.sqlite-wal {INDEX}/photos.sqlite-shm')
    subprocess.run(rsync + [copy, f'{host}:{INDEX}/photos.sqlite'], check=True, capture_output=True)
    os.remove(copy)


def start(until=None, jobs=None):
    host = host_of()
    if not logged_in(host):
        print(f"Claude n'est pas connecté sur {host} : ssh -t {host} '~/.local/bin/claude auth login'")
        return 1
    if _running(host) is not None:
        print(f'Le serveur de {host} tourne déjà.')
    else:
        print('Arrêt de la file de cette machine...')
        _stop_here()
        print('Ce qui a déjà été fait là-bas revient ici...')
        fetch(host)
        print(f"Copie du code, des données eBird et de la base vers {host}...")
        _send(host)
        # (only what is described from now on comes back)
        db = photoindex.connect()
        photoindex.set_meta(db, 'worker_since', datetime.now().isoformat(' ', 'seconds'))
        db.close()
        # (cd first, not 'cd && ... &': the shell around a list would keep ssh open)
        ssh(host, f'cd {CODE}; PYTHONPATH=. setsid nohup python3 -m patchnames.doc gui --port {PORT} '
                  f'--no-browser --no-vignettes > ~/{INDEX}/serveur.log 2>&1 < /dev/null & sleep 8')
    if jobs:
        ssh(host, f"cd {CODE} && PYTHONPATH=. python3 -c \"from patchnames import config; "
                  f"config.save({{'doc_batch_jobs': {int(jobs)}}})\"")
    print('File :', _post(host, '/api/batch', '{"all": true}').decode()[:200])
    if until:
        end = datetime.combine(datetime.now().date(), datetime.strptime(until, '%H:%M').time())
        wait = int((end - datetime.now()).total_seconds())
        ssh(host, f"setsid nohup sh -c 'sleep {max(wait, 1)}; curl -s -m 120 -X POST -H \"X-Patchdoc: 1\" "
                  f"-H \"Content-Type: application/json\" -d {{}} http://127.0.0.1:{PORT}/api/batch/stop' "
                  f'> ~/{INDEX}/arret.log 2>&1 < /dev/null &')
        print(f'Arrêt prévu à {until} (dans {wait // 60} min).')
    return status(host)


def stop(host=None):
    host = host or host_of()
    print(_post(host, '/api/batch/stop').decode()[:200])
    return 0


def status(host=None):
    host = host or host_of()
    print(f"{host} : Claude {'connecté' if logged_in(host) else 'pas connecté'}")
    state = _running(host)
    if state is None:
        print('Pas de serveur en marche là-bas.')
        return 0
    writes = state.get('writes') or {}
    print(f"{state['done']} séquences faites, {state['failed']} échecs, {state['active']} en cours, "
          f"{state['waiting']} en attente, {writes.get('waiting', 0)} à écrire"
          + (f" ; en pause : {state['why']}" if state.get('hold') else ''))
    return 0


def fetch(host=None):
    """bring here what the worker described; the number of sequences"""
    host = host or host_of()
    db = photoindex.connect()
    since = photoindex.meta('worker_since') or '2000'
    found = ssh(host, f'test -f {INDEX}/photos.sqlite && python3 -c {shlex.quote(EXPORT)} {shlex.quote(since)}')
    rows = json.loads(found.stdout) if found.stdout.strip() else []
    seqs = {}
    for row in rows:
        seqs.setdefault(row['seq'], []).append(row)
    ready = {seq: group for seq, group in seqs.items()
             if not any(row['written'] == 'pending' for row in group)}
    records, kept = [], 0
    for seq, group in ready.items():
        here = db.execute('SELECT MAX(described) AS described, MAX(verified) AS verified FROM photos '
                          'WHERE seq = ?', (seq,)).fetchone()
        when = max(str(row['described'] or '') for row in group)
        if here['verified'] or (here['described'] and here['described'] != 'file'
                                and here['described'] >= when):
            kept += 1
            continue
        records += [{key: value for key, value in row.items() if key not in KEEP} for row in group]
    if records:
        photoindex.put(db, records)
    waiting = [str(row['described']) for seq, group in seqs.items() if seq not in ready for row in group
               if row['described'] and row['described'] != 'file']
    done = [str(row['described']) for group in ready.values() for row in group
            if row['described'] and row['described'] != 'file']
    # (the next time from the oldest that is still being written)
    limit = min(waiting)[:19] if waiting else max(done)[:19] if done else since
    photoindex.set_meta(db, 'worker_since', min(limit, datetime.now().isoformat(' ', 'seconds')))
    db.close()
    rsync = [sync._rsync()[0], '-rtuz', '-e', ' '.join(SSH)]
    for kind in ('thumbs', 'previews', 'eyes'):
        subprocess.run(rsync + [f'{host}:{INDEX}/{kind}/', os.path.join(photoindex.index_dir(), kind) + '/'],
                       capture_output=True)
    print(f'{len(ready) - kept} séquences reviennent du serveur ({kept} laissées telles quelles ici, '
          f'{len(seqs) - len(ready)} encore en cours d\'écriture).')
    return 0


def main():
    parser = argparse.ArgumentParser(prog='python -m patchnames.worker',
                                     description="La file de l'archive, sur le serveur de l'archive.")
    parser.add_argument('what', choices=('status', 'start', 'fetch', 'stop'))
    parser.add_argument('--until', metavar='HH:MM', help="Arrêter la file à cette heure aujourd'hui.")
    parser.add_argument('--jobs', type=int, help='Séquences en même temps là-bas.')
    args = parser.parse_args()
    if args.what == 'start':
        return start(args.until, args.jobs)
    return {'status': status, 'fetch': fetch, 'stop': stop}[args.what]()


if __name__ == '__main__':
    sys.exit(main())
