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

orphans() compares two copies file by file, and with the index, for what
rsync does not settle: a file that one copy has and the other does not
(not yet copied, or the old name of a photo renamed or moved on the other
copy, which a copy the other way would bring back), and a photo of the
index that neither copy has. It only says; nothing is moved or copied.
"""

import os
import re
import shlex
import shutil
import subprocess
import time

from . import config as _config_module

#: never copied: what the Finder leaves behind
EXCLUDE = ('.DS_Store', '._*')
#: the folder of an archive root where a copy of the index is left (backup())
BACKUP = 'patchdoc'
FILES = re.compile(r'Number of (?:regular )?files transferred: ([\d,.\s]+)')
BYTES = re.compile(r'Total transferred file size: ([\d,.\s]+)')
#: a line of rsync --info=progress2: bytes so far, percent, pace, time, and
#:  the files copied so far
PROGRESS = re.compile(r'^\s*([\d,.]+)\s+\d+%\s+(\S+/s)\s+\d+:\d\d:\d\d(?:\s+\(xfr#(\d+),)?')
#: the list of the orphans found last (orphans()), in the folder of the index
ORPHANS = 'orphelins.tsv'
#: what an orphan is, as that list says it
ORPHAN_KINDS = {
    'seul': "seulement sur cette copie, rien de semblable sur l'autre : une copie vers l'autre "
            "l'y apportera",
    'deplace': "sur l'autre copie aussi, sous le même nom, mais dans un autre dossier",
    'renomme': "l'autre copie a la même photo sous un autre nom (même numéro à moins de deux "
               "jours, ou même empreinte, même taille à 0,1 % près) : l'un des deux noms est "
               "probablement un ancien, qu'une copie vers l'autre ramènerait",
    'doublon': "mis de côté comme doublon sur l'autre copie (patchdoc doublons), encore en "
               "place ici : la prochaine copie le range aussi",
    'perdue': "dans la base, mais sur aucune des deux copies, sous aucun dossier",
}


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


def skip_file(paths):
    """a file that tells rsync to leave these paths alone (relative to the
    archive root): what was put aside as a duplicate on one copy and may
    still be in place on the other (see dupes.py); None without any"""
    if not paths:
        return None
    from . import photoindex
    name = os.path.join(photoindex.index_dir(), 'doublons_exclus.txt')
    with open(name, 'w') as handle:
        for path in paths:
            # (anchored at the root of the copy; the wildcards of rsync taken literally)
            handle.write('/' + re.sub(r'([*?\[\]\\])', r'\\\1', path) + '\n')
    return name


def command(src, dst, dry=False, cfg=None, skip=None):
    """the rsync command that brings to the copy `dst` what the copy `src`
    has that is newer or missing there (two archive roots); skip: a file of
    paths to leave alone (skip_file). The folder of the duplicates of each
    copy is never copied: each copy puts its own aside."""
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
    cmd += ['--exclude=/doublons/', f'--exclude=/{BACKUP}/'] + (
        [f'--exclude-from={skip}'] if skip else [])
    if far:
        cmd += ['-z', '-e', 'ssh -oBatchMode=yes -oConnectTimeout=20']
    if dry:
        # every file looked at is named, to say how far the comparison is: one
        #   it would copy or that is the same (-ii), one the other side has
        #   newer (skip1); an older rsync says all that with -vv
        cmd += ['-n', '--stats'] + (['-ii', '--info=skip1'] if modern else ['-vv'])
    elif modern:
        cmd += ['--info=progress2', '--no-inc-recursive']
    else:
        cmd += ['-v']
    return cmd + ends


def backup(roots, cfg=None):
    """leave on archive roots a copy of the index, which lives on this
    machine only: the file photos.sqlite in the folder 'patchdoc' of each
    root, the copy that was there kept as photos.avant.sqlite. The copy is
    taken with SQLite's own backup, safe while the index is in use; a root
    that already has the index as it is now is left alone. Returns the
    names of the copies that have it."""
    import json
    import sqlite3
    from . import photoindex
    cfg = cfg or _config_module.load()
    source = photoindex.db_path()
    if not os.path.isfile(source):
        return []
    stamp = max(os.path.getmtime(path) for path in (source, source + '-wal') if os.path.isfile(path))
    log = os.path.join(photoindex.index_dir(), 'sauvegardes.json')
    try:
        with open(log) as handle:
            done = json.load(handle)
    except (OSError, ValueError):
        done = {}
    todo = [root for root in roots if done.get(root) != stamp
            and (os.path.isdir(root) or remote_of(root, cfg))]
    if todo:
        copy = os.path.join(photoindex.index_dir(), 'photos.copie.sqlite')
        if os.path.exists(copy):
            os.remove(copy)
        live = sqlite3.connect(f'file:{source}?mode=ro', uri=True, timeout=60)
        fresh = sqlite3.connect(copy)
        with fresh:
            live.backup(fresh)
        # (one file that stands alone, without the write-ahead log of the live index)
        fresh.execute('PRAGMA journal_mode = DELETE')
        fresh.close()
        live.close()
        for root in todo:
            far = remote_of(root, cfg)
            try:
                if far:
                    host, _, there = far.partition(':')
                    folder = f'{there}/{BACKUP}'
                    keep = (f'mkdir -p {shlex.quote(folder)} && cd {shlex.quote(folder)} && '
                            'if [ -f photos.sqlite ]; then mv -f photos.sqlite photos.avant.sqlite; fi')
                    ssh = ['ssh', '-oBatchMode=yes', '-oConnectTimeout=20']
                    if subprocess.run(ssh + [host, keep], capture_output=True, timeout=120).returncode:
                        continue
                    sent = subprocess.run([_rsync()[0], '-tz', '-e', ' '.join(ssh), copy,
                                           f'{host}:{folder}/photos.sqlite'], capture_output=True)
                    if sent.returncode:
                        continue
                else:
                    folder = os.path.join(root, BACKUP)
                    os.makedirs(folder, exist_ok=True)
                    final = os.path.join(folder, 'photos.sqlite')
                    if os.path.isfile(final):
                        os.replace(final, os.path.join(folder, 'photos.avant.sqlite'))
                    shutil.copy2(copy, final + '.tmp')
                    os.replace(final + '.tmp', final)
                done[root] = stamp
            except (OSError, subprocess.TimeoutExpired):
                continue
        os.remove(copy)
        with open(log, 'w') as handle:
            json.dump(done, handle)
    return [name_of(root) for root in roots if done.get(root) == stamp]


def _number(text):
    digits = re.sub(r'\D', '', text or '')
    return int(digits) if digits else 0


def plan(src, dst, skip=None, tell=None, stop=None):
    """what a copy from `src` to `dst` would bring, without copying:
    {'files', 'bytes', 'seen'}, 'seen' being the files of `src` rsync looked
    at; tell(seen) as it goes, stop: an Event that ends it (None is then
    returned); raises RuntimeError"""
    proc = subprocess.Popen(command(src, dst, dry=True, skip=skip), stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE)
    seen, rest, said, last = 0, b'', [], 0.0
    try:
        while True:
            if stop is not None and stop.is_set():
                proc.terminate()
                proc.wait(timeout=30)
                return None
            chunk = proc.stdout.read1(1 << 16)
            if not chunk:
                break
            *lines, rest = (rest + chunk).split(b'\n')
            for line in lines:
                # a file rsync looked at: itemized ('>f++++++++++ path', '.f
                #   path'), or left alone ('path is newer', 'path is uptodate')
                code = line.split(b' ', 1)[0]
                if line.endswith((b' is newer', b' is uptodate')) or (
                        9 <= len(code) <= 13 and code[:1] in b'<>ch.*' and code[1:2] == b'f'):
                    seen += 1
                else:
                    said.append(line)       # (its statistics are at the end)
            if tell and time.time() - last > 0.3:
                last = time.time()
                tell(seen)
        if tell:
            tell(seen)
        err = proc.stderr.read().decode('utf-8', 'replace')
        text = b'\n'.join(said + [rest]).decode('utf-8', 'replace')
        if proc.wait() != 0:
            raise RuntimeError((err or text).strip()[-400:] or 'rsync a échoué')
    finally:
        if proc.poll() is None:
            proc.kill()
    files, size = FILES.search(text), BYTES.search(text)
    return {'files': _number(files.group(1)) if files else 0,
            'bytes': _number(size.group(1)) if size else 0, 'seen': seen}


def run(src, dst, tell=None, stop=None, skip=None):
    """copy from `src` to `dst` what is newer or missing there; tell(bytes,
    files, pace) as it goes, stop: an Event that ends it. Returns 'done' or
    'stopped'; raises RuntimeError."""
    cmd = command(src, dst, skip=skip)
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


def orphans(first, second, tell=None):
    """Compare two copies of the archive (two roots) file by file, and with
    the index (see the module). Each file that one copy has and the other
    does not, at the same place, is said to be:
    - 'doublon': put aside as a duplicate (patchdoc doublons) on the other;
    - 'deplace': on the other too, under the same name, in another folder;
    - 'renomme': the other has a file of the same camera number (or hash),
      the same maker's initials and kind, nearly the same size (dupes.NEAR),
      under another name, and for a camera number within dupes.WINDOW of
      its time: most likely the same photo, one of the two names an old one
      (the one the index knows is said, the closest in time is taken);
    - 'seul': nothing like it on the other.
    A photo of the index ('perdue') is one whose name is on neither copy,
    in any folder. The list goes to ORPHANS in the folder of the index,
    one line a file: kind, copy, path, its twin on the other copy, and the
    names of the two the index knows. tell(seen) as the copies are listed:
    the files listed so far. Returns {'copies': [name, name], 'files':
    {name: count}, 'counts': {kind: {name: count}}, 'folders': {name:
    [(folder, count)]} (the folders the 'seul' files of each copy are in, the
    fullest first), 'list', 'when'}; raises RuntimeError."""
    import collections
    import threading
    from datetime import datetime
    from . import dupes, photoindex
    roots = [first, second]
    for root in roots:
        if not os.path.isdir(root):
            raise RuntimeError(f"{name_of(root)} n'est pas branché ({root})")
    if name_of(first) == name_of(second):
        raise RuntimeError('deux copies du même nom')
    seen, files, failed = {first: 0, second: 0}, {}, []

    def listed(root):
        def counted(count):
            seen[root] = count
            if tell:
                tell(sum(seen.values()))
        try:
            found = dupes.listing(root, sizes=False, tell=counted)
            # (the copies of the index left on the drives are not photos)
            files[root] = {path: what for path, what in found.items()
                           if not path.startswith(BACKUP + '/')}
            seen[root] = len(files[root])
        except Exception as exc:
            failed.append(f'{name_of(root)} : {exc}')

    # (both at once: one is a drive of this machine, the other often a server)
    workers = [threading.Thread(target=listed, args=(root,)) for root in roots]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()
    if failed:
        raise RuntimeError(' ; '.join(failed))
    for root in roots:
        if not files[root]:
            raise RuntimeError(f"{name_of(root)} n'a donné aucun fichier (la copie est-elle branchée ?)")
    if tell:
        tell(sum(seen.values()))

    sizes = {}

    def size(root, path):
        """the size of a file of a copy: from its listing (a server gives
        them), else asked of the drive, once"""
        what = files[root].get(path)
        if what:
            return what[0]
        if (root, path) not in sizes:
            try:
                sizes[root, path] = os.path.getsize(os.path.join(root, path))
            except OSError:
                sizes[root, path] = None
        return sizes[root, path]

    def key(path):
        found = dupes.NAME.match(os.path.basename(path))
        return (found.group(3) or '', found.group(4), found.group(5).lower()) if found else None

    db = photoindex.connect()
    try:
        known = photoindex.known(db)
    finally:
        db.close()
    aside = set(dupes.excluded())
    by_name = {root: collections.defaultdict(list) for root in roots}
    by_key = {root: collections.defaultdict(list) for root in roots}
    for root in roots:
        for path in files[root]:
            by_name[root][os.path.basename(path)].append(path)
            found = key(path)
            if found:
                by_key[root][found].append(path)

    def twin(path, root, other):
        """the file of the other copy most likely the same photo under
        another name, None without one"""
        mine, found = size(root, path), key(path)
        when = dupes._named(path)
        if not found or not mine or not when:
            return None
        near = []
        for there in by_key[other].get(found, ()):
            then = dupes._named(there)
            if os.path.basename(there) == os.path.basename(path) or not then:
                continue
            gap = abs((then[0] - when[0]).total_seconds())
            # a camera number comes back every 10 000 photos, and the raw files
            #   of a camera are nearly all of a size: such a twin is believed
            #   within dupes.WINDOW only; a hash of the file in its name is
            #   believed at any time
            if found[1].isdigit() and gap > dupes.WINDOW:
                continue
            theirs = size(other, there)
            if theirs and abs(theirs - mine) <= dupes.NEAR * max(theirs, mine):
                near.append((gap, there))
        return min(near)[1] if near else None

    def told(*paths):
        names = [os.path.basename(path) for path in paths if path]
        return ' '.join(name for name in names if name in known)

    rows, counts = [], collections.defaultdict(lambda: collections.Counter())
    folders = {root: collections.Counter() for root in roots}
    for root, other in ((first, second), (second, first)):
        for path in sorted(set(files[root]) - set(files[other])):
            name, there = os.path.basename(path), None
            if path in aside:
                kind = 'doublon'
            elif by_name[other].get(name):
                kind, there = 'deplace', by_name[other][name][0]
            else:
                there = twin(path, root, other)
                kind = 'renomme' if there else 'seul'
            if kind == 'seul':
                folders[root][path.split('/', 1)[0] if '/' in path else '.'] += 1
            counts[kind][name_of(root)] += 1
            rows.append((kind, name_of(root), path, there or '', told(path, there)))
    everywhere = set(by_name[first]) | set(by_name[second])
    for name, (path, _) in sorted(known.items()):
        # (a photo of the archive: its path in the index is relative to the root)
        if path and not os.path.isabs(path) and name not in everywhere:
            counts['perdue']['base'] += 1
            rows.append(('perdue', 'base', path, '', name))
    report = os.path.join(photoindex.index_dir(), ORPHANS)
    with open(report + '.tmp', 'w') as handle:
        handle.write('# ' + datetime.now().isoformat(' ', 'minutes') + ' : '
                     + ' et '.join(name_of(root) for root in roots) + '\n')
        for kind, said in ORPHAN_KINDS.items():
            handle.write(f'# {kind} : {said}\n')
        handle.write('genre\tcopie\tchemin\tjumeau\tnoms connus de la base\n')
        for row in rows:
            handle.write('\t'.join(row) + '\n')
    os.replace(report + '.tmp', report)
    return {'copies': [name_of(root) for root in roots],
            'files': {name_of(root): len(files[root]) for root in roots},
            'counts': {kind: dict(found) for kind, found in counts.items()},
            'folders': {name_of(root): folders[root].most_common() for root in roots},
            'list': report, 'when': datetime.now().isoformat(' ', 'minutes')}
