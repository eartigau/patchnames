"""
exiftool where the photos are.

An archive mounted with sshfs answers slowly: every file exiftool opens
there is a round trip, and writing a tag brings the whole file here and
sends it back. When the server the archive is on has exiftool, patchdoc runs
it there instead, over ssh: the server reads and rewrites its own disk, and
only what was asked for travels (the tags, the picture inside a raw file).

run() is exiftool with a list of arguments and a list of files, run on the
server when every file is on one that has exiftool (config
'remote_exiftool': None to find out, False for never, or the path of
exiftool there), and here otherwise, or when the server does not answer.
The arguments go to the server on the standard input (exiftool -@ -), one
on a line, so that no shell there reads the titles and descriptions; the
files are named in what comes back as they are named here.
"""

import json
import os
import subprocess
import threading
import time

from . import config as _config_module
from . import sync

#: one connection is kept and shared by the calls that follow (ssh's own
#:  multiplexing): only the first one pays for the handshake
OPTIONS = ['-oBatchMode=yes', '-oConnectTimeout=15', '-oControlMaster=auto',
           '-oControlPath=~/.ssh/patchdoc-%C', '-oControlPersist=300', '-C']
#: a server is asked again whether it has exiftool after this long
AGAIN = 600

_lock = threading.Lock()
_tools, _points = {}, {'when': 0.0, 'found': {}}


def place(path):
    """(server, path there) of a file under an sshfs mount, as
    ("user@host", "/path"); None for a file of this machine"""
    with _lock:
        if time.time() - _points['when'] > 60:
            _points.update(when=time.time(), found=sync._mounts())
        found = dict(_points['found'])
    path = os.path.abspath(path)
    for point, source in found.items():
        if ':' in source and not source.startswith('/') and path.startswith(point + os.sep):
            host, _, base = source.partition(':')
            return host, base.rstrip('/') + path[len(point):]
    return None


def _ssh(host, command, data=None, timeout=900):
    """run a command on a server: (return code, standard output as bytes,
    standard error as text)"""
    try:
        res = subprocess.run(['ssh'] + OPTIONS + [host, command], input=data, capture_output=True,
                             timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 255, b'', str(exc)
    return res.returncode, res.stdout, res.stderr.decode('utf-8', 'replace')


def tool(host):
    """the exiftool of a server (its path there); None when it has none, or
    when config 'remote_exiftool' is False"""
    wanted = _config_module.load().get('remote_exiftool')
    if wanted is False:
        return None
    if isinstance(wanted, str) and wanted:
        return wanted
    with _lock:
        known = _tools.get(host)
        if known and time.time() - known[0] < AGAIN:
            return known[1]
    code, out, _ = _ssh(host, 'command -v exiftool', timeout=40)
    found = out.decode('utf-8', 'replace').strip().splitlines()[-1] if code == 0 and out.strip() else None
    with _lock:
        _tools[host] = (time.time(), found)
    return found


def status(ask=True):
    """what the page says of it: [{'host', 'tool', 'asked'}] for each server
    an archive root is mounted from; ask=False: only what is already known
    (asking a server may take a while)"""
    hosts = []
    for root in _config_module.archive_roots():
        where = place(os.path.join(root, 'x')) if os.path.isdir(root) else None
        if where and where[0] not in [host['host'] for host in hosts]:
            with _lock:
                known = _tools.get(where[0])
            hosts.append({'host': where[0], 'tool': tool(where[0]) if ask else known and known[1],
                          'asked': ask or bool(known)})
    return hosts


def run(args, files, timeout=900):
    """exiftool with these arguments on these files: (return code, standard
    output as bytes, standard error as text, where the files were for it:
    {path there: path here}, empty when it ran here)"""
    return run_many([(args, files)], timeout)


def run_many(blocks, timeout=900):
    """several exiftool commands in one run, [(arguments, files)], each
    with its own arguments (exiftool -execute between them): one start of
    exiftool, and one trip to the server, for files that do not get the same
    tags. Returns what run() does."""
    blocks = [([str(arg) for arg in args], [str(file) for file in files]) for args, files in blocks]
    files = [file for _, group in blocks for file in group]
    places = {file: place(file) for file in files}
    hosts = {where[0] if where else None for where in places.values()}
    host = hosts.pop() if len(hosts) == 1 else None
    there = tool(host) if host else None

    def lines(name):
        out = []
        for count, (args, group) in enumerate(blocks):
            out += ['-execute'] * bool(count) + args + [name(file) for file in group]
        return out

    if there:
        # one argument on a line: a line break inside one would cut it in two
        sent = [' '.join(line.splitlines()) for line in lines(lambda file: places[file][1])]
        code, out, err = _ssh(host, f'{there} -@ -', ('\n'.join(sent) + '\n').encode('utf-8'), timeout)
        if code != 255:
            return code, out, err, {places[file][1]: file for file in files}
        # (the server did not answer: do it here, and ask it again later)
        with _lock:
            _tools[host] = (time.time(), None)
    try:
        res = subprocess.run(['exiftool'] + lines(lambda file: file), capture_output=True,
                             timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        return 1, b'', str(exc), {}
    return res.returncode, res.stdout, res.stderr.decode('utf-8', 'replace'), {}


def rows(args, files, timeout=900):
    """exiftool -j with these arguments on these files: its rows, each named
    ('SourceFile') as the file is named here"""
    _, out, _, names = run(['-j', '-q'] + list(args), files, timeout)
    try:
        found = json.loads(out.decode('utf-8', 'replace')) if out.strip() else []
    except ValueError:
        return []
    for row in found:
        row['SourceFile'] = names.get(row.get('SourceFile'), row.get('SourceFile'))
    return found
