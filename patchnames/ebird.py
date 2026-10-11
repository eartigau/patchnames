"""
The eBird data of the photographer, as a prior on the photos: most bird
photos are taken while an eBird checklist is being filled, so the checklist
whose hours hold a photo gives its place and a short list of likely species.

The data: the "Download My Data" export of eBird
(https://ebird.org/downloadMyData), a zip of one MyEBirdData.csv, one row per
species and checklist. The newest export found on disk is used (config
'ebird_search', ~/Downloads by default) and kept as config 'ebird_dir'
/MyEBirdData.csv, dated from when eBird made it.

A new export, refresh(): eBird sends a link to it by email a few minutes
after it is asked for. The request is sent with the eBird account when its
password is in the macOS keychain (`patchdoc ebird --login`), and otherwise
the eBird page is opened in the browser, one click away. The link is then
read from the mailbox over IMAP (Proton Mail Bridge on this machine by
default, its password in the keychain too) and the zip downloaded; a zip
that lands in ~/Downloads by hand is picked up just as well.

The clock of the camera, EBird.offsets(): a camera left on the time of
another country, or on the wrong side of a daylight saving change, is off by
whole hours. For each day of photos, every offset from -20 to +20 h is scored
by the number of sequences it puts inside a checklist; the offsets of the
successive days are then chosen together, a change of offset costing SWITCH
sequences (leaving zero and coming back to it included), so that a day that
says little follows its neighbours. No offset is preferred when nothing says
otherwise. An offset also has to make sense (EBird._fits): the camera was
set somewhere, so with an offset of several hours it shows the time of a
place that far east or west of the checklist. A camera that records the
time zone it is set to (EXIF OffsetTimeOriginal) is off by the difference
between that zone and the zone of the checklist, read in the time zone
database of the system, and by nothing else: in Sweden on Swedish time it is
right, and photos taken twenty minutes after a list ended are not pulled
into it by an hour that happens to fit. A camera that does not say gets
more caution: the place has to be one of the checklists of the year before,
and a day counts for LONE sequences at most, so that one day alone never
moves the clock (an afternoon of photos away from any checklist, an
eclipse, would otherwise be pulled into the checklist of the morning).

A match, EBird.match(), is 'inside' (the corrected time is within the hours
of a checklist, MARGIN minutes around), 'near' (within NEAR minutes of one),
or nothing; the other checklists of the day always come along, since the
clock may still be wrong.
"""

import bisect
import csv
import glob
import json
import math
import os
import re
import subprocess
import sys
import time
import zipfile
from collections import namedtuple
from datetime import datetime, timedelta

from . import config as _config_module

CSV_NAME = 'MyEBirdData.csv'
DOWNLOAD_PAGE = 'https://ebird.org/downloadMyData'
#: the same page, opened in a browser on its "Request My Observations" button
BUTTON = DOWNLOAD_PAGE + '#:~:text=Request%20My-,Observations,-Personalized%20eBird%20Taxonomy'
#: the link of an export, as eBird emails it
LINK = re.compile(r'https://is-ebird-datadownload[\w-]*\.s3[\w.-]*\.amazonaws\.com'
                  r'/download_my_data/ebird_[\w-]+\.zip')
AGENT = ('Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 '
         '(KHTML, like Gecko) Version/17.0 Safari/605.1.15')

#: minutes before the start and after the end of a checklist still inside it
MARGIN = 10
#: minutes given to a checklist without a duration (incidental observations)
SHORT = 20
#: minutes around a checklist where a photo is said near it
NEAR = 60
#: the clock offsets tried, in hours (camera time + offset = time at the place)
OFFSETS = [float(k) for k in range(-20, 21)]
#: what a change of offset between two days of photos costs, in sequences
SWITCH = 2.0
#: the most a day can count for an offset, in sequences more than for no
#:  offset, when the camera does not say its time zone: less than leaving
#:  zero and coming back (2 SWITCH)
LONE = 3.5
#: the other checklists of the day given with a match, at most
OTHERS = 5

#: French names of the countries (ISO 3166 codes, from the Unicode CLDR)
COUNTRIES_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'countries_fr.json')

Match = namedtuple('Match', 'checklist kind offset local others')


def _cfg():
    return _config_module.load()


def _clean(text, limit=300):
    """a checklist comment without its HTML (weather widgets) and line breaks"""
    text = re.sub(r'<[^>]+>', ' ', text or '')
    text = ' '.join(text.split())
    return text if len(text) <= limit else text[:limit - 1] + '…'


_countries = None
_zones = None


def zone_table():
    """{country code: [(latitude, longitude, time zone name)]}, the zones of
    each country and their reference towns, from the time zone database of
    the system (zone.tab); {} without one"""
    global _zones
    if _zones is not None:
        return _zones
    _zones = {}
    try:
        import zoneinfo
        places = list(zoneinfo.TZPATH) + ['/usr/share/zoneinfo']
    except ImportError:
        return _zones
    try:
        import tzdata
        places.append(os.path.join(os.path.dirname(tzdata.__file__), 'zoneinfo'))
    except ImportError:
        pass
    spot = re.compile(r'([+-])(\d\d)(\d\d)(\d\d)?([+-])(\d\d\d)(\d\d)(\d\d)?')
    for place in places:
        for name in ('zone.tab', 'zone1970.tab'):
            path = os.path.join(place, name)
            if _zones or not os.path.isfile(path):
                continue
            with open(path, encoding='utf-8') as handle:
                for line in handle:
                    parts = line.rstrip('\n').split('\t')
                    found = spot.fullmatch(parts[1]) if len(parts) > 2 else None
                    if line.startswith('#') or not found:
                        continue
                    g = found.groups()
                    lat = int(g[1]) + int(g[2]) / 60 + int(g[3] or 0) / 3600
                    lon = int(g[5]) + int(g[6]) / 60 + int(g[7] or 0) / 3600
                    for code in parts[0].split(','):
                        _zones.setdefault(code, []).append(
                            (lat if g[0] == '+' else -lat, lon if g[4] == '+' else -lon, parts[2]))
    return _zones


def country_name(code):
    """the French name of a country code ('SE' -> 'Suède'); the code if unknown"""
    global _countries
    if _countries is None:
        try:
            with open(COUNTRIES_PATH, encoding='utf-8') as handle:
                _countries = json.load(handle)
        except OSError:
            _countries = {}
    return _countries.get(code, code)


class Checklist:
    """one eBird checklist: when, where, and what was seen"""

    def __init__(self, row):
        self.sid = row['Submission ID']
        self.day = datetime.strptime(row['Date'], '%Y-%m-%d').date()
        self.start = None
        if row['Time']:
            self.start = datetime.strptime(f"{row['Date']} {row['Time']}", '%Y-%m-%d %I:%M %p')
        try:
            self.duration = int(float(row['Duration (Min)'])) or None
        except ValueError:
            self.duration = None
        self.location = row['Location']
        self.loc_id = row['Location ID']
        self.county = row['County']
        self.state = row['State/Province']
        self.country = self.state.split('-')[0]
        self.lat = float(row['Latitude']) if row['Latitude'] else None
        self.lon = float(row['Longitude']) if row['Longitude'] else None
        self.protocol = row['Protocol'].replace('eBird - ', '')
        self.comments = _clean(row['Checklist Comments'])
        self.species = []
        # the hours of the checklist, MARGIN minutes around
        self.lo = self.hi = None
        if self.start:
            self.lo = self.start - timedelta(minutes=MARGIN)
            self.hi = self.start + timedelta(minutes=(self.duration or SHORT) + MARGIN)

    def add(self, row):
        self.species.append({'francais': row['Common Name'], 'latin': row['Scientific Name'],
                             'count': row['Count'], 'details': _clean(row['Observation Details'], 200),
                             'order': float(row['Taxonomic Order'] or 0)})

    def gap(self, local):
        """minutes from a local time to the hours of the checklist (0 inside,
        None for a checklist without a time)"""
        if self.lo is None:
            return None
        if local < self.lo:
            return (self.lo - local).total_seconds() / 60
        if local > self.hi:
            return (local - self.hi).total_seconds() / 60
        return 0.0

    def label(self):
        """'Parc du Mont-Royal (CA-QC), 2026-05-17 06:40, 95 min'"""
        when = f'{self.start:%Y-%m-%d %H:%M}' if self.start else f'{self.day} (sans heure)'
        length = f', {self.duration} min' if self.duration else ''
        return f'{self.location} ({self.state}), {when}{length}'

    def latin_names(self):
        return {sp['latin'] for sp in self.species}


class EBird:
    """the checklists of an export"""

    def __init__(self, path):
        self.path = path
        self.asof = datetime.fromtimestamp(os.path.getmtime(path))
        self.lists = {}
        with open(path, newline='', encoding='utf-8-sig') as handle:
            for row in csv.DictReader(handle):
                sid = row['Submission ID']
                if sid not in self.lists:
                    self.lists[sid] = Checklist(row)
                self.lists[sid].add(row)
        self.by_day = {}
        for checklist in self.lists.values():
            checklist.species.sort(key=lambda sp: sp['order'])
            self.by_day.setdefault(checklist.day, []).append(checklist)
        for lists in self.by_day.values():
            lists.sort(key=lambda c: c.start or datetime.combine(c.day, datetime.min.time()))
        self._places = None
        # day and solar time zone (longitude / 15) of every checklist, by day
        pairs = sorted((c.day, c.lon / 15) for c in self.lists.values() if c.lon is not None)
        self._days, self._solar, self._been = [d for d, _ in pairs], [z for _, z in pairs], {}
        self._utc = {}

    def utc_offset(self, checklist):
        """hours from UTC at the place and on the day of a checklist: the time
        zone of its country whose reference town is the closest (most
        countries have one zone; in a large one, a place by a zone border may
        get its neighbour's); None without a time zone database"""
        if checklist.sid not in self._utc:
            hours, zones = None, zone_table().get(checklist.country)
            if zones and checklist.lat is not None:
                shrink = math.cos(math.radians(checklist.lat))
                name = min(zones, key=lambda z: (z[0] - checklist.lat) ** 2
                           + ((z[1] - checklist.lon) * shrink) ** 2)[2]
                try:
                    import zoneinfo
                    noon = datetime(checklist.day.year, checklist.day.month, checklist.day.day, 12)
                    when = (checklist.start or noon).replace(tzinfo=zoneinfo.ZoneInfo(name))
                    hours = when.utcoffset().total_seconds() / 3600
                except Exception:
                    hours = None
            self._utc[checklist.sid] = hours
        return self._utc[checklist.sid]

    def _around(self, local):
        """the checklists of the day of a local time and of the days next to it
        (a checklist may cross midnight)"""
        day = local.date()
        for delta in (-1, 0, 1):
            yield from self.by_day.get(day + timedelta(days=delta), ())

    def _inside(self, local):
        return [c for c in self._around(local) if c.lo is not None and c.lo <= local <= c.hi]

    def _fits(self, checklist, zone, offset):
        """whether a clock offset makes sense for a checklist and a camera set
        to a time zone (hours from UTC; None if the photos do not say).

        No offset always does: the camera shows the time of the place. With
        a zone, the place would be in the zone `zone + offset`, which has to
        be its own (utc_offset); if the system cannot tell, a zone within
        reach of the solar time of its longitude, the zones of the world
        running from about 1.5 h behind it to 3 h ahead (Montréal -0.1 and
        +0.9 h, Madrid +2.2 h in summer). Without a zone, an hour is a
        daylight saving change; more is a camera still on the time of
        another place, which has to be one of the checklists of the 400
        days before, as far east or west as the offset says, within 1.5 h."""
        if not offset or checklist.lon is None:
            return True
        solar = checklist.lon / 15
        if zone is not None:
            there = self.utc_offset(checklist)
            if there is not None:
                return abs(zone + offset - there) < 0.01
            return -1.5 <= (zone + offset - solar + 12) % 24 - 12 <= 3.0
        if abs(offset) <= 1:
            return True
        if checklist.day not in self._been:
            first = bisect.bisect_left(self._days, checklist.day - timedelta(days=400))
            last = bisect.bisect_right(self._days, checklist.day)
            self._been[checklist.day] = {round(2 * z) / 2 for z in self._solar[first:last]}
        return any(abs(solar - past - offset) <= 1.5 for past in self._been[checklist.day])

    def offsets(self, moments, zones=None):
        """the clock offset of a camera for each day it took photos, {day: hours}
        (see the module); moments: the camera times of its sequences; zones:
        for each, the time zone the camera says it is set to, hours from UTC,
        or None"""
        by_day = {}
        for when, zone in zip(moments, zones or [None] * len(moments)):
            by_day.setdefault(when.date(), []).append((when, zone))
        days = sorted(by_day)
        if not days:
            return {}
        steps = [timedelta(hours=offset) for offset in OFFSETS]
        states = range(len(OFFSETS))
        zero = OFFSETS.index(0.0)
        gains = []
        for day in days:
            hits = [sum(1 for when, zone in by_day[day]
                        if any(self._fits(c, zone, offset) for c in self._inside(when + step)))
                    for offset, step in zip(OFFSETS, steps)]
            if all(zone is None for _, zone in by_day[day]):
                hits = [min(hit, hits[zero] + LONE) for hit in hits]
            # an offset other than zero has to earn its place: half a
            #   sequence, a little more when it is large
            gains.append([hit - (0.4 + 0.02 * abs(offset) if offset else 0.0)
                          for hit, offset in zip(hits, OFFSETS)])
        # the clock is taken as right before the first day and after the last
        ends = [SWITCH if offset else 0.0 for offset in OFFSETS]
        gains[0] = [gain - end for gain, end in zip(gains[0], ends)]
        gains[-1] = [gain - end for gain, end in zip(gains[-1], ends)]
        best, back = gains[0][:], []
        for gain in gains[1:]:
            top = max(states, key=best.__getitem__)
            came = [k if best[k] >= best[top] - SWITCH else top for k in states]
            best = [(best[k] if came[k] == k else best[top] - SWITCH) + gain[k] for k in states]
            back.append(came)
        k = max(states, key=best.__getitem__)
        path = [k]
        for came in reversed(back):
            k = came[k]
            path.append(k)
        path.reverse()
        return {day: OFFSETS[k] for day, k in zip(days, path)}

    def match(self, when, offset=0.0):
        """the checklist of a camera time under a clock offset (see the module)"""
        local = when + timedelta(hours=offset)
        best, kind = None, None
        inside = self._inside(local)
        if inside:
            # overlapping checklists: the one started last
            best, kind = max(inside, key=lambda c: c.start), 'inside'
        else:
            near = [(c.gap(local), c) for c in self._around(local) if c.lo is not None]
            near = [(gap, c) for gap, c in near if gap <= NEAR]
            if near:
                best, kind = min(near, key=lambda pair: pair[0])[1], 'near'
        # the other checklists of the day, on the clock as it is and as corrected
        days = {local.date(), when.date()}
        others = [c for day in sorted(days) for c in self.by_day.get(day, ()) if c is not best]
        others.sort(key=lambda c: c.gap(local) if c.lo is not None else float('inf'))
        return Match(best, kind, offset, local, others[:OTHERS])

    def nearest(self, lat, lon, within=3.0):
        """the checklist place closest to a position, as (checklist, km), when
        within `within` km: a name for a photo that has a GPS position only"""
        if self._places is None:
            self._places = {}
            for checklist in self.lists.values():
                if checklist.lat is not None:
                    self._places.setdefault(checklist.loc_id, checklist)
        best = None
        for checklist in self._places.values():
            dlat = math.radians(checklist.lat - lat)
            dlon = math.radians(checklist.lon - lon)
            a = (math.sin(dlat / 2) ** 2 + math.cos(math.radians(lat))
                 * math.cos(math.radians(checklist.lat)) * math.sin(dlon / 2) ** 2)
            km = 2 * 6371.0 * math.asin(math.sqrt(a))
            if km <= within and (best is None or km < best[1]):
                best = (checklist, km)
        return best


# ---------------------------------------------------------------------------
# The export on disk
# ---------------------------------------------------------------------------

def data_dir():
    return os.path.expanduser(_cfg()['ebird_dir'])


def working_copy():
    return os.path.join(data_dir(), CSV_NAME)


def _made(path):
    """when eBird made an export: the date of the CSV inside a zip, the date
    of a loose CSV; None for a file that is not an export"""
    try:
        if path.lower().endswith('.zip'):
            with zipfile.ZipFile(path) as archive:
                return datetime(*archive.getinfo(CSV_NAME).date_time).timestamp()
        return os.path.getmtime(path)
    except (OSError, KeyError, zipfile.BadZipFile, ValueError):
        return None


def _import(path, made):
    """copy an export to the working copy, dated from when eBird made it"""
    os.makedirs(data_dir(), exist_ok=True)
    target, tmp = working_copy(), working_copy() + '.tmp'
    if path.lower().endswith('.zip'):
        # the one file wanted is read by its name: nothing else leaves the zip
        with zipfile.ZipFile(path) as archive, archive.open(CSV_NAME) as src, open(tmp, 'wb') as dst:
            while chunk := src.read(1 << 20):
                dst.write(chunk)
    else:
        with open(path, 'rb') as src, open(tmp, 'wb') as dst:
            while chunk := src.read(1 << 20):
                dst.write(chunk)
    os.utime(tmp, (made, made))
    os.replace(tmp, target)
    return target


def find_export(quiet=True):
    """the working copy of the newest export on disk, None without any: an
    export of the search places newer than the working copy replaces it"""
    target = working_copy()
    newest = (_made(target) if os.path.isfile(target) else None, target)
    for pattern in _cfg()['ebird_search']:
        for path in glob.glob(os.path.expanduser(pattern)):
            made = _made(path)
            if made and (newest[0] is None or made > newest[0] + 1):
                newest = (made, path)
    if newest[0] is None:
        return None
    if newest[1] != target:
        if not quiet:
            print(f'Données eBird : {newest[1]} '
                  f'({datetime.fromtimestamp(newest[0]):%Y-%m-%d %H:%M}) copié vers {target}')
        _import(newest[1], newest[0])
    return target


def load(quiet=True):
    """the eBird data on disk, None without any"""
    path = find_export(quiet=quiet)
    return EBird(path) if path else None


# ---------------------------------------------------------------------------
# Passwords: the macOS keychain (or the environment), never a file
# ---------------------------------------------------------------------------

def _service(name):
    return f'patchnames-{name}'


def secret(name):
    """(account, password) of 'ebird' or 'imap': the environment
    (PATCHNAMES_EBIRD_USER and _PASSWORD, PATCHNAMES_IMAP_USER and _PASSWORD),
    else the macOS keychain; (None, None) without any"""
    user = os.environ.get(f'PATCHNAMES_{name.upper()}_USER')
    password = os.environ.get(f'PATCHNAMES_{name.upper()}_PASSWORD')
    if user and password:
        return user, password
    if sys.platform != 'darwin':
        return None, None
    found = subprocess.run(['security', 'find-generic-password', '-s', _service(name)],
                           capture_output=True, text=True)
    account = re.search(r'"acct"<blob>="(.*)"', found.stdout)
    if found.returncode != 0 or not account:
        return None, None
    password = subprocess.run(['security', 'find-generic-password', '-s', _service(name), '-w'],
                              capture_output=True, text=True)
    if password.returncode != 0:
        return None, None
    return account.group(1), password.stdout.rstrip('\n')


def login():
    """ask for the eBird and mailbox accounts and keep them in the macOS
    keychain; the passwords are typed into the `security` command itself"""
    if sys.platform != 'darwin':
        print('Pas de trousseau ici : définissez plutôt PATCHNAMES_EBIRD_USER, '
              'PATCHNAMES_EBIRD_PASSWORD, PATCHNAMES_IMAP_USER et PATCHNAMES_IMAP_PASSWORD.')
        return
    cfg = _cfg()
    asked = (('ebird', "Nom d'utilisateur eBird", 'le mot de passe eBird'),
             ('imap', f'Nom d\'utilisateur de la boîte courriel (IMAP à {cfg["imap_host"]}:'
                      f'{cfg["imap_port"]} ; pour Proton Mail Bridge, celui de ses '
                      '« Mailbox details »)',
              'le mot de passe IMAP (pour Proton Mail Bridge, celui du Bridge, pas celui de Proton)'))
    for name, question, what in asked:
        current = secret(name)[0]
        user = input(f'{question}{f" [{current}]" if current else ""} (vide pour passer) : ').strip()
        if not user:
            continue
        print(f'Maintenant {what}, demandé deux fois par le trousseau :')
        subprocess.run(['security', 'delete-generic-password', '-s', _service(name)],
                       capture_output=True)
        # -w last and without a value: `security` asks for the password itself
        done = subprocess.run(['security', 'add-generic-password', '-s', _service(name),
                               '-a', user, '-l', f'patchnames ({name})', '-w'])
        print('  gardé dans le trousseau.' if done.returncode == 0
              else '  PAS gardé (voir ci-dessus).')


# ---------------------------------------------------------------------------
# A new export: the request, the email, the download
# ---------------------------------------------------------------------------

def _forms(html):
    """the forms and the links of a page: [{'action', 'method', 'fields',
    'text'}], [(href, text)]"""
    from html.parser import HTMLParser

    class Page(HTMLParser):
        def __init__(self):
            super().__init__()
            self.forms, self.links, self.form, self.link = [], [], None, None

        def handle_starttag(self, tag, attrs):
            attrs = dict(attrs)
            if tag == 'form':
                self.form = {'action': attrs.get('action') or '', 'fields': {}, 'text': '',
                             'method': (attrs.get('method') or 'get').lower()}
                self.forms.append(self.form)
            elif tag in ('input', 'button') and self.form is not None:
                kind = (attrs.get('type') or 'text').lower()
                if tag == 'input' and kind == 'checkbox' and 'checked' not in attrs:
                    return
                if attrs.get('name') and kind not in ('submit', 'button', 'image', 'reset'):
                    self.form['fields'][attrs['name']] = attrs.get('value') or ''
                if kind == 'submit':
                    self.form['text'] += ' ' + (attrs.get('value') or '')
            elif tag == 'a' and attrs.get('href'):
                self.link = [attrs['href'], '']

        def handle_endtag(self, tag):
            if tag == 'form':
                self.form = None
            elif tag == 'a' and self.link:
                self.links.append(tuple(self.link))
                self.link = None

        def handle_data(self, data):
            if self.form is not None:
                self.form['text'] += ' ' + data
            if self.link:
                self.link[1] += data

    page = Page()
    page.feed(html)
    return page.forms, page.links


def request_export():
    """ask eBird for a new export of the observations with the eBird account
    of the keychain; False without an account. Raises RuntimeError when eBird
    refuses the login or its page has changed."""
    import http.cookiejar
    import urllib.parse
    import urllib.request
    user, password = secret('ebird')
    if not user:
        return False
    opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    opener.addheaders = [('User-Agent', AGENT)]

    def get(url, fields=None):
        data = urllib.parse.urlencode(fields).encode() if fields is not None else None
        with opener.open(url, data, timeout=60) as reply:
            return reply.geturl(), reply.read().decode('utf-8', 'replace')

    def send(url, form, fields):
        target = urllib.parse.urljoin(url, form['action'] or url)
        if form['method'] == 'post':
            return get(target, fields)
        return get(target + ('&' if '?' in target else '?') + urllib.parse.urlencode(fields))

    def signin(html):
        return next((f for f in _forms(html)[0] if 'password' in f['fields']), None)

    # the page of the exports sends to the Cornell Lab login first
    url, html = get(DOWNLOAD_PAGE)
    form = signin(html)
    if form:
        url, html = send(url, form, dict(form['fields'], username=user, password=password))
        if signin(html):
            raise RuntimeError("eBird a refusé la connexion (nom d'utilisateur ou mot de passe ; "
                               '`patchdoc ebird --login` pour les redonner)')
    if 'downloadmydata' not in url.lower():
        url, html = get(DOWNLOAD_PAGE)
    # the request of the observations, not of the taxonomy next to it
    forms, links = _forms(html)
    wanted = [f for f in forms if re.search('observations', f['text'], re.I)
              and not re.search('taxonom', f['text'] + f['action'], re.I)]
    wanted = wanted or [f for f in forms if 'downloadmydata' in f['action'].lower()
                        and not re.search('taxonom', f['text'] + f['action'], re.I)]
    if wanted:
        send(url, wanted[0], wanted[0]['fields'])
        return True
    for href, text in links:
        if re.search('my observations', text, re.I):
            get(urllib.parse.urljoin(url, href))
            return True
    raise RuntimeError('la demande des observations est introuvable sur la page eBird')


def _mail_texts(since):
    """the texts of the messages received since a time, the newest first"""
    import email
    import imaplib
    import ssl
    cfg = _cfg()
    user, password = secret('imap')
    if not user:
        return
    host, port = cfg['imap_host'], int(cfg['imap_port'])
    if port == 993:
        box = imaplib.IMAP4_SSL(host, port, timeout=30)
    else:
        box = imaplib.IMAP4(host, port, timeout=30)
        context = ssl.create_default_context()
        if host in ('127.0.0.1', 'localhost', '::1'):
            # Proton Mail Bridge, on this machine, signs its own certificate
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
        box.starttls(context)
    try:
        box.login(user, password)
        day = (datetime.fromtimestamp(since) - timedelta(days=1)).strftime('%d-%b-%Y')
        for folder in cfg['imap_folders']:
            if box.select(f'"{folder}"', readonly=True)[0] != 'OK':
                continue
            found = box.search(None, 'SINCE', day)
            numbers = found[1][0].split() if found[0] == 'OK' and found[1] else []
            for number in reversed(numbers[-60:]):
                fetched = box.fetch(number, '(INTERNALDATE RFC822)')
                if fetched[0] != 'OK' or not fetched[1] or not isinstance(fetched[1][0], tuple):
                    continue
                received = imaplib.Internaldate2tuple(fetched[1][0][0])
                if received and time.mktime(received) < since - 60:
                    break
                message = email.message_from_bytes(fetched[1][0][1])
                for part in message.walk():
                    if part.get_content_maintype() == 'text':
                        body = part.get_payload(decode=True) or b''
                        yield body.decode(part.get_content_charset() or 'utf-8', 'replace')
    finally:
        try:
            box.logout()
        except Exception:
            pass


def find_link(since):
    """the link of an export in a message received since a time, None without"""
    for text in _mail_texts(since) or ():
        link = LINK.search(text)
        if link:
            return link.group(0)
    return None


def download(link):
    """download the zip of an export and make it the working copy"""
    import urllib.request
    if not LINK.fullmatch(link):
        raise ValueError(f"ce n'est pas le lien d'un export eBird : {link}")
    os.makedirs(data_dir(), exist_ok=True)
    path = os.path.join(data_dir(), link.rsplit('/', 1)[-1])
    request = urllib.request.Request(link, headers={'User-Agent': AGENT})
    with urllib.request.urlopen(request, timeout=300) as reply, open(path, 'wb') as handle:
        while chunk := reply.read(1 << 20):
            handle.write(chunk)
    made = _made(path)
    if made is None:
        raise RuntimeError(f"{path} n'est pas un export eBird")
    _import(path, made)
    os.remove(path)
    return working_copy()


def refresh(wait=900, quiet=False):
    """ask eBird for a new export and wait for it, `wait` seconds at most
    (see the module); the working copy when it came, None otherwise"""
    say = (lambda *args: None) if quiet else print
    since = time.time()
    try:
        sent = request_export()
    except Exception as exc:
        say(f'eBird : {exc}')
        sent = False
    if sent:
        say('eBird : un nouvel export est demandé ; son lien arrive par courriel dans quelques '
            'minutes.')
    else:
        say(f'eBird : cliquez sur « Request My Observations » sur {DOWNLOAD_PAGE} (ouverte dans '
            'le navigateur).')
        if sys.platform == 'darwin':
            subprocess.run(['open', BUTTON], capture_output=True)
    mailbox = secret('imap')[0]
    if not mailbox:
        say('eBird : pas de boîte courriel dans le trousseau (`patchdoc ebird --login`) : ouvrez '
            "vous-même le lien du courriel ; le zip est pris dès qu'il arrive dans ~/Downloads.")
    before = _made(working_copy()) if os.path.isfile(working_copy()) else 0
    try:
        while time.time() - since < wait:
            time.sleep(15)
            if mailbox:
                try:
                    link = find_link(since)
                except Exception as exc:
                    say(f'eBird : la boîte courriel est illisible ({exc}).')
                    mailbox, link = None, None
                if link:
                    say('eBird : le lien est arrivé ; téléchargement.')
                    return download(link)
            path = find_export(quiet=True)
            if path and (_made(path) or 0) > max(before or 0, since - 3600):
                return path
            if not quiet:
                print(f"  attente de l'export, {int(time.time() - since)} s "
                      '(Ctrl-C pour continuer avec les données sur disque)', end='\r', flush=True)
    except KeyboardInterrupt:
        say("\neBird : fin de l'attente ; les données sur disque sont utilisées.")
        return None
    say(f'\neBird : pas de nouvel export après {wait} s ; les données sur disque sont utilisées.')
    return None


def status():
    """say what eBird data is on disk and which accounts are known"""
    data = load(quiet=False)
    if data is None:
        print(f'Aucun export eBird trouvé ({", ".join(_cfg()["ebird_search"])}).')
    else:
        days = sorted(data.by_day)
        print(f'Données eBird : {data.path}')
        print(f'  faites par eBird le {data.asof:%Y-%m-%d %H:%M} ; {len(data.lists)} listes, '
              f'du {days[0]} au {days[-1]}')
    for name, what in (('ebird', 'Compte eBird'), ('imap', 'Boîte courriel (IMAP)')):
        user = secret(name)[0]
        print(f'{what} : {user or "non défini (patchdoc ebird --login)"}')
