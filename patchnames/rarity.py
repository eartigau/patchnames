"""
Whether a bird is rare where and when it was seen: the label "oiseau rare"
goes to a sequence when one of its species is reported on less than RARE
(0.5 %) of the eBird checklists of the sub-national region of its checklist
(CA-QC, MX-YUC...), at that time of the year, all years together (the
status of a species changes little).

The frequencies are the bar charts of eBird
(https://ebird.org/barchartData?r=CA-QC&bmo=1&emo=12&byr=1900&eyr=2026&fmt=tsv):
48 periods a year (the 1st to the 7th, 8th to 14th, 15th to 21st and 22nd
to the end of each month), the share of the checklists of the region that
report each species, and how many checklists each period has. eBird gives
them only to someone signed in: they are asked through Safari, which is,
in a window of its own closed afterwards, and the file it downloads is taken
from ~/Downloads (a file of a bar chart already there is taken as well).
Each region is asked for once, and kept in the folder of the index,
ebird/histogrammes, which the backup on GitHub takes with the index
(backup.py).

The species of a bar chart go by the French names of eBird (those of the
account, fr_CA); the taxonomy of eBird, which needs no account, ties them to
the Latin names the index has (kept in ebird/ too), and a Latin name eBird
no longer uses (Bubulcus ibis, now Ardea ibis) is found by its French name,
accents and the ligature œ aside. A species counts with
its subspecies and forms (the most frequent of them), and one that eBird
knows but the region never had is rare. A genus or a pair ('sp.', '/', a
hybrid), a domestic form (few birders report one) and a Latin name eBird
does not know leave the question open.

A period with few checklists says little: the periods around it are taken
with it, weighted by their checklists, until they hold SAMPLE checklists,
MOST periods on each side at most; a region and time of the year without
that many leave the question open (None).
"""

import csv
import glob
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from datetime import date

from . import config as _config_module
from . import photoindex

#: rare: reported on less than this share of the checklists (config 'rare_share')
RARE = 0.005
#: the label of a rare bird, a keyword of its files
LABEL = 'oiseau rare'
#: the checklists a share is computed on, at least, and how many periods on
#:  each side of the one of the photo may be taken to reach them
SAMPLE, MOST = 200, 4
#: the periods on each side of the one of the photo always taken with it
#:  (config 'rare_width'): with the years all together, a rare bird seen by
#:  hundreds of birders raises the share of its own week (the European Robin
#:  of Montréal, 11 January 2026: 0.66 % of the checklists of its week in
#:  Québec, 0.30 % with the weeks on each side)
WIDTH = 1
#: the bar chart of a region, all its years, all its months
ADDRESS = ('https://ebird.org/barchartData?r={region}&bmo=1&emo=12&byr=1900&eyr={year}'
           '&fmt=tsv')
#: the taxonomy of eBird, with the French names of Canada
TAXONOMY = 'https://api.ebird.org/v2/ref/taxonomy/ebird?fmt=csv&locale=fr_CA'
#: how long the file Safari downloads is waited for, in seconds
WAIT = 60

#: the bar charts and the taxonomy read so far (_french: {French name of
#:  eBird, folded: the Latin name of its species})
_charts, _taxa, _french = {}, {}, {}


def stats_dir():
    """the folder of the statistics of eBird, in the folder of the index"""
    return os.path.join(photoindex.index_dir(), 'ebird')


def _dir():
    return os.path.join(stats_dir(), 'histogrammes')


def chart_path(region):
    return os.path.join(_dir(), f'{region}.tsv')


def _is_chart(path):
    """whether a file is a bar chart of eBird (not a page asking to sign in)"""
    try:
        with open(path, encoding='utf-8', errors='replace') as handle:
            return 'Sample Size:' in handle.read(4000)
    except OSError:
        return False


def _downloaded(region, since=0.0):
    """a bar chart of the region in ~/Downloads, the newest, made after `since`"""
    found = [path for path in glob.glob(os.path.expanduser(f'~/Downloads/ebird_{region}__*barchart*.txt'))
             if os.path.getmtime(path) >= since and _is_chart(path)]
    return max(found, key=os.path.getmtime) if found else None


def _osascript(script):
    done = subprocess.run(['osascript', '-e', script], capture_output=True, text=True, timeout=60)
    if done.returncode != 0:
        raise RuntimeError(f'Safari : {done.stderr.strip()[:200]}')
    return done.stdout.strip()


def fetch(region, wait=WAIT):
    """the bar chart of a region on disk, asked through Safari if it is not
    there yet (see the module); raises RuntimeError when Safari does not
    bring it (not signed in to eBird, no Safari)"""
    path = chart_path(region)
    if os.path.isfile(path):
        return path
    os.makedirs(_dir(), exist_ok=True)
    found = _downloaded(region)
    if not found:
        if sys.platform != 'darwin':
            raise RuntimeError(f"pas de Safari ici : téléchargez {ADDRESS.format(region=region, year=date.today().year)} "
                               'dans ~/Downloads')
        start = time.time() - 1
        window = _osascript('tell application "Safari"\n'
                            f'make new document with properties {{URL:"{ADDRESS.format(region=region, year=date.today().year)}"}}\n'
                            'return id of front window\nend tell')
        try:
            while time.time() - start < wait:
                time.sleep(1)
                found = _downloaded(region, start)
                if found:
                    # (the name is given once the download is over: let it settle)
                    size = os.path.getsize(found)
                    time.sleep(0.5)
                    if os.path.getsize(found) == size:
                        break
                    found = None
        finally:
            try:
                _osascript(f'tell application "Safari" to close (every window whose id is {int(window)})')
            except (RuntimeError, ValueError):
                pass
        if not found:
            raise RuntimeError(f"Safari n'a pas téléchargé l'histogramme de {region} : est-il connecté à eBird ?")
    shutil.move(found, path)
    return path


def _read(region):
    """(the checklists of each period, {French name: the share of each period})"""
    if region not in _charts:
        sizes, shares = None, {}
        with open(fetch(region), encoding='utf-8') as handle:
            for row in csv.reader(handle, delimiter='\t'):
                if not row or len(row) < 49:
                    continue
                try:
                    values = [float(value) for value in row[1:49]]
                except ValueError:
                    continue
                if row[0].startswith('Sample Size'):
                    sizes = values
                elif row[0].strip():
                    shares[row[0].strip()] = values
        _charts[region] = (sizes or [0.0] * 48, shares)
    return _charts[region]


def taxonomy():
    """{Latin name: [the French names of eBird of it and of its subspecies
    and forms]}, downloaded once"""
    if not _taxa:
        path = os.path.join(stats_dir(), 'taxonomie_fr_CA.csv')
        if not os.path.isfile(path):
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with urllib.request.urlopen(TAXONOMY, timeout=120) as reply, open(path + '.tmp', 'wb') as out:
                out.write(reply.read())
            os.replace(path + '.tmp', path)
        with open(path, encoding='utf-8') as handle:
            for row in csv.DictReader(handle):
                latin, name = row['SCIENTIFIC_NAME'].strip(), row['COMMON_NAME'].strip()
                if row['CATEGORY'] in ('spuh', 'slash', 'hybrid'):
                    continue
                # (a subspecies or a form counts with its species)
                species = ' '.join(latin.replace('(', ' ').split()[:2])
                _taxa.setdefault(species, []).append(name)
                _french.setdefault(_fold(name), species)
    return _taxa


def _fold(name):
    """a French name to compare: without accents nor capitals, œ as oe"""
    name = (name or '').replace('œ', 'oe').replace('Œ', 'Oe').replace('æ', 'ae')
    return photoindex.fold(' '.join(name.split()))


def wild(pairs):
    """these species, [(French, Latin)], less their domestic forms ("Canard
    musqué (forme domestique)", "Anser anser (Domestic type)"; the Moineau
    domestique is wild)"""
    return [(french, latin) for french, latin in pairs
            if latin and '(forme domestique)' not in (french or '').lower()
            and 'domestic type' not in latin.lower()]


def period(day):
    """the period of the bar charts a day falls in, 0 to 47"""
    return (day.month - 1) * 4 + min(3, (day.day - 1) // 7)


def share(region, latin, day, width=None, french=''):
    """the share of the checklists of the region that report the species
    around that day (the period of the day and `width` periods on each side,
    WIDTH by default); None when it cannot be told (see the module). french:
    its French name, should eBird not know its Latin name"""
    cfg = _config_module.load()
    width = cfg.get('rare_width', WIDTH) if width is None else width
    latin = ' '.join((latin or '').split())
    if not region or not latin or 'sp.' in latin or '/' in latin or ' x ' in latin:
        return None
    names = taxonomy().get(' '.join(latin.split()[:2]))
    if not names and french:
        names = taxonomy().get(_french.get(_fold(french), ''))
    if not names:
        return None
    sizes, shares = _read(region)
    rows = [shares[name] for name in names if name in shares]
    at, total, seen = period(day), 0.0, 0.0
    for reach in range(MOST + 1):
        for k in {(at - reach) % 48, (at + reach) % 48}:
            total += sizes[k]
            seen += sizes[k] * max((row[k] for row in rows), default=0.0)
        if reach >= width and total >= SAMPLE:
            return seen / total
    return None


def assess(region, pairs, day, width=None):
    """(1 when one of these species, [(French, Latin)], is rare in the
    region around that day, 0 when none is, None when that cannot be told
    for any of them; the lowest of their shares, None without any)"""
    found = [share(region, latin, day, width, french) for french, latin in pairs if latin]
    known = [value for value in found if value is not None]
    if not known:
        return None, None
    return int(min(known) < _config_module.load().get('rare_share', RARE)), min(known)


def rare(region, pairs, day):
    """assess, the first half"""
    return assess(region, pairs, day)[0]


def fetch_all(regions, tell=None):
    """the bar charts of these regions on disk, asked through Safari one
    after the other; {region: error} for those it did not bring"""
    failed = {}
    for count, region in enumerate(regions, 1):
        try:
            fetch(region)
        except (RuntimeError, OSError, subprocess.TimeoutExpired) as exc:
            failed[region] = str(exc)
            if 'connecté' in str(exc) and len(failed) >= 2:
                break       # (Safari is not signed in: the others would fail too)
        if tell:
            tell(count, len(regions), region)
    return failed
