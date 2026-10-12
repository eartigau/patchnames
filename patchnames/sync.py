"""
The copies of the archive, and rsync between them.

The archive may live on several drives that are copies of each other (config
'photo_archive_root' and each of 'photo_archive_mounts'): a network drive
and an external one, say. One of them is used at a time; what changes there
(new photos, tags written into files) is brought to the other with rsync, in
one direction or the other. A file is copied only when the other side has
none or an older one, and nothing is ever deleted: doing it both ways leaves
each side with the newest of everything.

A copy that is on a server is reached by rsync itself over ssh, not through
its mount, which would have to be asked for every file: config
'photo_archive_remotes' says how ({root: "user@host:/path"}); a copy mounted
with sshfs is found without it while it is mounted.

plan() says what a copy would bring (a dry run), run() does it and tells how
far it is: the bytes and the files copied so far, and the pace.
"""

import os
import re
import shutil
import subprocess
import time

from . import config as _config_module

#: never copied: what the Finder leaves behind
EXCLUDE = ('.DS_Store', '._*')
FILES = re.compile(r'Number of (?:regular )?files transferred: ([\d,.\s]+)')
BYTES = re.compile(r'Total transferred file size: ([\d,.\s]+)')
#: a line of rsync --info=progress2: bytes so far, percent, pace, time, and
#:  the files copied so far
PROGRESS = re.compile(r'^\s*([\d,.]+)\s+\d+%\s+(\S+/s)\s+\d+:\d\d:\d\d(?:\s+\(xfr#(\d+),)?')


def name_of(root):
    """what a copy is called in the page: the drive its 'photo' folder is on
    (/Volumes/u5tb/photo -> u5tb)"""
    return os.path.basename(os.path.dirname(root)) or root


def _mounts():
    """{mount point: what is mounted there}, from the mount command"""
    try:
        out = subprocess.run(['mount'], capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.TimeoutExpired):
        return {}
    found = {}
    for line in out.splitlines():
        source, on, rest = line.partition(' on ')
        if on:
            found[rest.rsplit(' (', 1)[0].rstrip(os.sep)] = source
    return found


def remote_of(root, cfg=None):
    """how rsync reaches a copy that is on a server, "user@host:/path"; None
    for a copy on a drive of this machine"""
    cfg = cfg or _config_module.load()
    for place, remote in (cfg.get('photo_archive_remotes') or {}).items():
        if os.path.expanduser(place).rstrip(os.sep) == root and remote:
            return str(remote).rstrip('/')
    for point, source in _mounts().items():
        # an sshfs mount: user@host:/path on the mount point
        if ':' in source and not source.startswith('/') and (root + os.sep).startswith(point + os.sep):
            return (source.rstrip('/') + root[len(point):]).rstrip('/')
    return None


def copies(cfg=None):
    """the copies of the archive: [{'root', 'name', 'mounted', 'remote',
    'used'}], the one in use first"""
    cfg = cfg or _config_module.load()
    roots = _config_module.archive_roots(cfg)
    used = _config_module.archive_root(cfg)
    return [{'root': root, 'name': name_of(root), 'mounted': os.path.isdir(root),
             'remote': remote_of(root, cfg), 'used': root == used and os.path.isdir(root)}
            for root in roots]


def _rsync():
    """the rsync to run and whether it knows --info=progress2 (3.1 and
    later; the one macOS comes with does not)"""
    path = shutil.which('rsync')
    if not path:
        raise RuntimeError("rsync est introuvable sur cette machine")
    try:
        head = subprocess.run([path, '--version'], capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.TimeoutExpired):
        head = ''
    found = re.search(r'version (\d+)\.(\d+)', head)
    return path, bool(found) and (int(found.group(1)), int(found.group(2))) >= (3, 1)


def command(src, dst, dry=False, cfg=None):
    """the rsync command that brings to the copy `dst` what the copy `src`
    has that is newer or missing there (two archive roots)"""
    cfg = cfg or _config_module.load()
    path, modern = _rsync()
    ends = []
    for root in (src, dst):
        remote = remote_of(root, cfg)
        if not remote and not os.path.isdir(root):
            raise RuntimeError(f"{name_of(root)} n'est pas branché ({root})")
        ends.append((remote or root) + '/')
    far = [end for end, root in zip(ends, (src, dst)) if remote_of(root, cfg)]
    if len(far) == 2:
        raise RuntimeError('les deux copies sont sur des serveurs : rsync ne copie pas de serveur '
                           "à serveur ; branchez ou montez l'une des deux ici")
    # -r the folders, -t with their dates, -u never over a newer file; two
    #   seconds apart is the same date (an exFAT drive keeps no finer)
    cmd = [path, '-rtu', '--modify-window=2'] + [f'--exclude={pattern}' for pattern in EXCLUDE]
    if far:
        cmd += ['-z', '-e', 'ssh -oBatchMode=yes -oConnectTimeout=20']
    if dry:
        cmd += ['-n', '--stats']
    elif modern:
        cmd += ['--info=progress2', '--no-inc-recursive']
    else:
        cmd += ['-v']
    return cmd + ends


def _number(text):
    digits = re.sub(r'\D', '', text or '')
    return int(digits) if digits else 0


def plan(src, dst):
    """what a copy from `src` to `dst` would bring, without copying:
    {'files', 'bytes'}; raises RuntimeError"""
    res = subprocess.run(command(src, dst, dry=True), capture_output=True, text=True)
    if res.returncode != 0:
        raise RuntimeError((res.stderr or res.stdout).strip()[-400:] or 'rsync a échoué')
    files, size = FILES.search(res.stdout), BYTES.search(res.stdout)
    return {'files': _number(files.group(1)) if files else 0,
            'bytes': _number(size.group(1)) if size else 0}


def run(src, dst, tell=None, stop=None):
    """copy from `src` to `dst` what is newer or missing there; tell(bytes,
    files, pace) as it goes, stop: an Event that ends it. Returns 'done' or
    'stopped'; raises RuntimeError."""
    cmd = command(src, dst)
    named = '-v' in cmd         # an rsync without progress2: it names each file it copies
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    seen, files = b'', 0
    try:
        while True:
            if stop is not None and stop.is_set():
                proc.terminate()
                proc.wait(timeout=30)
                return 'stopped'
            chunk = proc.stdout.read1(65536)
            if not chunk:
                break
            seen += chunk
            # (progress2 rewrites its line with carriage returns)
            *lines, seen = re.split(rb'[\r\n]', seen)
            for line in lines:
                text = line.decode('utf-8', 'replace')
                found = PROGRESS.match(text)
                if found:
                    files = int(found.group(3) or files)
                    if tell:
                        tell(_number(found.group(1)), files, found.group(2))
                elif (named and text.strip() and not text.endswith('/') and not text.startswith(
                        ('building ', 'sending ', 'receiving ', 'sent ', 'total ', 'created '))):
                    files += 1
                    if tell:
                        tell(0, files, '')
        err = proc.stderr.read().decode('utf-8', 'replace')
        if proc.wait() != 0:
            raise RuntimeError(err.strip()[-400:] or 'rsync a échoué')
        return 'done'
    finally:
        if proc.poll() is None:
            proc.kill()
