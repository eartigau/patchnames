"""
A backup of the index on GitHub: `patchdoc sauvegarde`, and the button of the
search page.

The index (photos.sqlite: every description, note, place, name and face)
lives on this machine only; the copy between the drives of the archive
leaves one on each (sync.backup). This puts one more out of the house: a
consistent copy of the database, without the thumbnails, compressed with
gzip, encrypted with a password, and sent as a file of a release of a
private GitHub repository (config 'backup_repo'; '<your account>/
patchdoc-sauvegarde' by default, made if it is not there).

It is encrypted here, before anything leaves: GitHub only ever holds what
cannot be read without the password, and a repository that is not private is
refused all the same. The password is in the macOS keychain (`patchdoc
sauvegarde --mot-de-passe` asks for it; it is typed into the `security`
command itself), never in a file. Lose it and the backups cannot be read.

The encryption is that of the openssl command (AES-256-CBC, the key drawn
from the password by PBKDF2, 600 000 rounds of SHA-256), so that a backup is
read back with nothing but openssl and gunzip, on any machine:

    gh release download sauvegardes -R ACCOUNT/patchdoc-sauvegarde
    openssl enc -d -aes-256-cbc -pbkdf2 -iter 600000 -md sha256 \\
        -in photos-AAAAMMJJ-HHMMSS.sqlite.gz.enc | gunzip > photos.sqlite

(`patchdoc sauvegarde --restaurer FILE` does the second step.) Each backup
is read back before it is sent, the last ones are kept (config
'backup_keep', 5) and the older ones removed: files of a release, not of the
repository itself, which would grow by one whole backup at each of them.
"""

import gzip
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from datetime import datetime

from . import config as _config_module
from . import photoindex

#: the release the backups are files of
TAG = 'sauvegardes'
#: how openssl encrypts them (see the module)
CIPHER = ['-aes-256-cbc', '-pbkdf2', '-iter', '600000', '-md', 'sha256']
#: the item of the macOS keychain that holds the password
SERVICE = 'patchnames-backup'

NOTES = """Sauvegardes chiffrées de la base de recherche de patchdoc (https://github.com/eartigau/patchnames).

Chaque fichier est la base `photos.sqlite`, sans les vignettes, compressée avec gzip puis chiffrée avec openssl (AES-256-CBC, clé tirée du mot de passe par PBKDF2). Pour en relire une, avec le mot de passe :

    openssl enc -d -aes-256-cbc -pbkdf2 -iter 600000 -md sha256 -in photos-AAAAMMJJ-HHMMSS.sqlite.gz.enc | gunzip > photos.sqlite

ou `patchdoc sauvegarde --restaurer FICHIER`."""


def _cfg():
    return _config_module.load()


def has_password():
    """whether a password is there (nothing is asked of the keychain but that)"""
    if os.environ.get('PATCHNAMES_BACKUP_PASSWORD'):
        return True
    return sys.platform == 'darwin' and subprocess.run(
        ['security', 'find-generic-password', '-s', SERVICE], capture_output=True).returncode == 0


def password():
    """the password of the backups: the environment (PATCHNAMES_BACKUP_PASSWORD),
    else the macOS keychain; None without one"""
    found = os.environ.get('PATCHNAMES_BACKUP_PASSWORD')
    if found or sys.platform != 'darwin':
        return found or None
    asked = subprocess.run(['security', 'find-generic-password', '-s', SERVICE, '-w'],
                           capture_output=True, text=True)
    return asked.stdout.rstrip('\n') or None if asked.returncode == 0 else None


def set_password():
    """ask for the password of the backups and keep it in the macOS keychain;
    it is typed into the `security` command itself"""
    if sys.platform != 'darwin':
        print('Pas de trousseau ici : définissez plutôt PATCHNAMES_BACKUP_PASSWORD.')
        return
    if has_password():
        print('Un mot de passe est déjà dans le trousseau : les sauvegardes déjà faites ne se '
              'lisent qu\'avec lui.')
        if input('Le remplacer ? [o/N] ').strip().lower() not in ('o', 'oui'):
            return
    print('Le mot de passe des sauvegardes, demandé deux fois par le trousseau. Notez-le aussi '
          'ailleurs : sans lui, aucune sauvegarde ne peut être relue.')
    subprocess.run(['security', 'delete-generic-password', '-s', SERVICE], capture_output=True)
    # -w last and without a value: `security` asks for the password itself
    done = subprocess.run(['security', 'add-generic-password', '-s', SERVICE, '-a', 'patchdoc',
                           '-l', 'patchnames (sauvegarde de la base)', '-w'])
    print('  gardé dans le trousseau.' if done.returncode == 0 else '  PAS gardé (voir ci-dessus).')


def _gh(*args, check=True, timeout=1800):
    try:
        done = subprocess.run(['gh', *args], capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        raise RuntimeError("la commande gh (GitHub) n'est pas installée : brew install gh, puis "
                           'gh auth login') from None
    if check and done.returncode != 0:
        raise RuntimeError(f"gh {' '.join(args[:2])} : {(done.stderr or done.stdout).strip()[:300]}")
    return done


def repo(cfg=None):
    """the repository the backups go to, 'account/name': config 'backup_repo',
    or patchdoc-sauvegarde of the account gh is logged in to"""
    name = (cfg or _cfg()).get('backup_repo')
    return name or _gh('api', 'user', '-q', '.login', timeout=60).stdout.strip() + '/patchdoc-sauvegarde'


def _ready(name):
    """make sure the repository is there and private, with the release the
    backups are files of"""
    seen = _gh('repo', 'view', name, '--json', 'isPrivate', '-q', '.isPrivate', check=False,
               timeout=60)
    if seen.returncode != 0:
        _gh('repo', 'create', name, '--private', '--add-readme', '--description',
            'Sauvegardes chiffrées de la base de recherche de patchdoc')
    elif seen.stdout.strip() != 'true':
        raise RuntimeError(f"le dépôt {name} n'est pas privé : les sauvegardes ne vont que dans "
                           'un dépôt privé')
    if _gh('release', 'view', TAG, '-R', name, check=False, timeout=60).returncode != 0:
        for attempt in range(4):
            # (a repository just made takes a moment to have its first commit)
            made = _gh('release', 'create', TAG, '-R', name, '--title',
                       'Sauvegardes de la base de patchdoc', '--notes', NOTES, check=attempt == 3)
            if made.returncode == 0:
                break
            time.sleep(3)


def _crypt(source, target, secret, decrypt=False):
    """encrypt a file with openssl, or decrypt it; raises RuntimeError"""
    tool = shutil.which('openssl') or '/usr/bin/openssl'
    done = subprocess.run([tool, 'enc'] + (['-d'] if decrypt else ['-salt']) + CIPHER
                          + ['-pass', 'stdin', '-in', source, '-out', target],
                          input=(secret + '\n').encode(), capture_output=True)
    if done.returncode != 0:
        raise RuntimeError('le mot de passe ne convient pas, ou le fichier est abîmé' if decrypt
                           else f"openssl : {done.stderr.decode('utf-8', 'replace').strip()[:200]}")


def _sum(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as handle:
        for block in iter(lambda: handle.read(1 << 20), b''):
            digest.update(block)
    return digest.hexdigest()


def make(folder, secret, tell=None):
    """a backup of the index in a folder, ready to send: a consistent copy,
    compacted, compressed, encrypted, and read back to make sure it can be.
    tell(stage) as it goes. Returns its path."""
    tell = tell or (lambda stage: None)
    tell('copie')
    plain = os.path.join(folder, 'photos.sqlite')
    source = sqlite3.connect(f'file:{photoindex.db_path()}?mode=ro', uri=True, timeout=60)
    copy = sqlite3.connect(plain)
    source.backup(copy)
    source.close()
    copy.execute('PRAGMA journal_mode = DELETE')        # one file, with nothing beside it
    copy.execute('VACUUM')
    copy.close()
    tell('compression')
    packed = plain + '.gz'
    with open(plain, 'rb') as handle, gzip.open(packed, 'wb', compresslevel=6) as out:
        shutil.copyfileobj(handle, out, 1 << 20)
    os.remove(plain)
    tell('chiffrement')
    final = os.path.join(folder, f'photos-{datetime.now():%Y%m%d-%H%M%S}.sqlite.gz.enc')
    _crypt(packed, final, secret)
    back = packed + '.relu'
    _crypt(final, back, secret, decrypt=True)
    if _sum(back) != _sum(packed):
        raise RuntimeError('la sauvegarde chiffrée ne se relit pas à l\'identique : rien n\'est envoyé')
    os.remove(back)
    os.remove(packed)
    return final


def run(tell=None):
    """Back the index up to GitHub (see the module). Raises RuntimeError.
    Returns {'when', 'name', 'bytes', 'repo', 'kept'}, which the index keeps
    too (meta 'backup_last')."""
    tell = tell or (lambda stage: None)
    cfg = _cfg()
    secret = password()
    if not secret:
        raise RuntimeError('pas de mot de passe pour les sauvegardes : `patchdoc sauvegarde '
                           '--mot-de-passe` en demande un et le garde dans le trousseau')
    if not os.path.isfile(photoindex.db_path()):
        raise RuntimeError("il n'y a pas encore de base à sauvegarder")
    with tempfile.TemporaryDirectory() as folder:
        final = make(folder, secret, tell)
        tell('envoi')
        name = repo(cfg)
        _ready(name)
        _gh('release', 'upload', TAG, final, '-R', name, '--clobber')
        out = {'when': datetime.now().isoformat(' ', 'minutes'), 'name': os.path.basename(final),
               'bytes': os.path.getsize(final), 'repo': name}
    # the last ones are kept, the older ones removed
    assets = sorted(line for line in _gh('release', 'view', TAG, '-R', name, '--json', 'assets',
                                         '-q', '.assets[].name', timeout=60).stdout.split()
                    if line.startswith('photos-') and line.endswith('.sqlite.gz.enc'))
    keep = max(1, int(cfg.get('backup_keep') or 5))
    for old in assets[:-keep]:
        _gh('release', 'delete-asset', TAG, old, '-R', name, '-y', check=False, timeout=120)
    out['kept'] = min(len(assets), keep)
    db = photoindex.connect()
    photoindex.set_meta(db, 'backup_last', json.dumps(out))
    db.close()
    return out


def last():
    """what the last backup was, {} if there was none"""
    try:
        return dict(json.loads(photoindex.meta('backup_last') or '{}'))
    except ValueError:
        return {}


def restore(path, target=None):
    """Read a backup back into a database file (photos.restauree.sqlite here,
    unless told otherwise): never over a file that is there, and never over
    the index itself. Returns the path of the file made."""
    secret = password()
    if not secret:
        raise RuntimeError('pas de mot de passe dans le trousseau (`patchdoc sauvegarde '
                           '--mot-de-passe`)')
    target = os.path.abspath(target or 'photos.restauree.sqlite')
    if os.path.exists(target):
        raise RuntimeError(f'{target} existe déjà : rien n\'est écrasé')
    with tempfile.TemporaryDirectory() as folder:
        packed = os.path.join(folder, 'photos.sqlite.gz')
        _crypt(path, packed, secret, decrypt=True)
        try:
            with gzip.open(packed, 'rb') as handle, open(target, 'wb') as out:
                shutil.copyfileobj(handle, out, 1 << 20)
        except (OSError, EOFError) as exc:
            if os.path.exists(target):
                os.remove(target)
            raise RuntimeError(f'le mot de passe ne convient pas, ou le fichier est abîmé ({exc})')
    return target


def main(set_it=False, back=None):
    """`patchdoc sauvegarde`: ask for the password, read a backup back, or
    make one and send it"""
    try:
        if set_it:
            set_password()
        elif back:
            made = restore(back)
            db = sqlite3.connect(f'file:{made}?mode=ro', uri=True)
            count = db.execute('SELECT COUNT(*), SUM(described IS NOT NULL) FROM photos').fetchone()
            db.close()
            print(f'Base relue dans {made} : {count[0]} photos, dont {count[1] or 0} décrites.\n'
                  f"Pour qu'elle devienne la base de recherche, arrêtez `patchdoc gui` et copiez-la "
                  f'vers {photoindex.db_path()}.')
        else:
            said = {'copie': 'Copie de la base...', 'compression': 'Compression...',
                    'chiffrement': 'Chiffrement, et relecture pour vérifier...',
                    'envoi': 'Envoi vers GitHub...'}
            out = run(lambda stage: print(said[stage], flush=True))
            many = 's' if out['kept'] > 1 else ''
            print(f"Base sauvegardée : {out['name']} ({out['bytes'] / 1e6:.1f} Mo) dans le dépôt "
                  f"privé {out['repo']} ; {out['kept']} sauvegarde{many} gardée{many}.")
    except RuntimeError as exc:
        print(f'Sauvegarde : {exc}.')
