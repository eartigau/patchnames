"""
The index of the photos, for the search page: one SQLite file in config
'index_dir', next to the thumbnails (thumbs/, 360 px, every frame) and the
previews (previews/, 1600 px, the frame of each sequence Claude looked at),
so that searching and browsing need neither the archive drive nor the raws.

A photo is known by its canonical name (YYMMDD_HHMMSS[_WHO]_NUM.ext), which
stays the same wherever the file moves; its path is only where it was last
seen. The photos of a sequence share what Claude said of it; the search works
on sequences, through the frame Claude looked at (rep = 1).

The files stay the truth: what `patchdoc` writes here is also written into
the photos (title, description, keywords, place, eBird checklist), and
`patchdoc index --exif` rebuilds the index from them.

The eBird checklists are copied here too, to show the list of a photo and to
find the photos taken during a checklist that holds a species.
"""

import os
import sqlite3
import unicodedata

from . import config as _config_module

#: the columns of a photo (lists are joined with '; ')
COLUMNS = ('name', 'path', 'stem', 'ext',
           'taken',            # the clock of the camera, ISO
           'local',            # the time at the place: taken + clock_offset
           'clock_offset',     # hours
           'camera', 'lens', 'focal',
           'bird',             # 1 when taken beyond config 'bird_focal_min'
           'seq', 'seq_n', 'rep',
           'title', 'topic', 'description', 'species', 'latin', 'habitat', 'keywords',
           'certainty',
           'note',             # what Claude says of the photo against the checklist
           'checklist',        # eBird submission ID
           'match',            # 'inside' or 'near' the hours of the checklist
           'on_checklist',     # 1: every species named is on the checklist, 0: one is not
           'location', 'county', 'state', 'country', 'lat', 'lon',
           'described',        # when Claude described it, ISO; NULL if it never did
           'place_norm')

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS photos (id INTEGER PRIMARY KEY, {', '.join(COLUMNS)}, UNIQUE (name));
CREATE INDEX IF NOT EXISTS photos_seq ON photos (seq);
CREATE INDEX IF NOT EXISTS photos_rep ON photos (rep, local);
CREATE INDEX IF NOT EXISTS photos_checklist ON photos (checklist);
CREATE TABLE IF NOT EXISTS tags (name, kind, value, norm);
CREATE INDEX IF NOT EXISTS tags_name ON tags (name);
CREATE INDEX IF NOT EXISTS tags_kind ON tags (kind, value);
CREATE VIRTUAL TABLE IF NOT EXISTS search USING fts5(
    text, tokenize = 'unicode61 remove_diacritics 2');
CREATE TABLE IF NOT EXISTS checklists (sid PRIMARY KEY, day, start, duration, protocol,
    location, county, state, country, lat, lon, comments);
CREATE TABLE IF NOT EXISTS observations (sid, francais, latin, count, details, norm);
CREATE INDEX IF NOT EXISTS observations_sid ON observations (sid);
CREATE TABLE IF NOT EXISTS meta (key PRIMARY KEY, value);
"""

#: the kinds of tags, and the column of a photo each comes from
TAGS = (('species', 'species'), ('latin', 'latin'), ('habitat', 'habitat'),
        ('keyword', 'keywords'), ('topic', 'topic'))
SEP = '; '


def fold(text):
    """lower case without accents, to compare what is typed with what is kept"""
    text = unicodedata.normalize('NFKD', text or '')
    return ''.join(ch for ch in text if not unicodedata.combining(ch)).lower()


def index_dir():
    return os.path.expanduser(_config_module.load()['index_dir'])


def connect():
    os.makedirs(index_dir(), exist_ok=True)
    db = sqlite3.connect(os.path.join(index_dir(), 'photos.sqlite'), timeout=30)
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)
    return db


def _image_path(kind, name):
    stem = os.path.splitext(name)[0]
    month = stem[:4] if stem[:6].isdigit() else 'misc'
    return os.path.join(index_dir(), kind, month, stem + '.jpg')


def thumb_path(name):
    return _image_path('thumbs', name)


def preview_path(name):
    return _image_path('previews', name)


def save_images(img, name, preview=False):
    """keep the thumbnail of a photo (a PIL image), and its preview if asked"""
    for path, size, wanted in ((thumb_path(name), 360, True), (preview_path(name), 1600, preview)):
        if not wanted:
            continue
        os.makedirs(os.path.dirname(path), exist_ok=True)
        small = img.copy()
        small.thumbnail((size, size))
        small.save(path, quality=82 if size > 400 else 78)


def put(db, records):
    """add photos or change what is known of them: each record is a dict of
    columns ('name' at least); the columns it does not hold are left alone"""
    names = [record['name'] for record in records]
    # the sequences these photos were in, and those they are in now
    touched = {record['seq'] for record in records if record.get('seq')}
    for start in range(0, len(names), 500):
        part = names[start:start + 500]
        touched.update(row[0] for row in db.execute(
            f'SELECT DISTINCT seq FROM photos WHERE name IN ({", ".join("?" * len(part))})', part))
    for record in records:
        if any(key in record for key in ('location', 'county', 'state', 'country')):
            from .ebird import country_name
            record = dict(record, place_norm=fold(' '.join(
                str(record.get(key) or '') for key in ('location', 'county', 'state'))
                + ' ' + country_name(record.get('country') or '')))
        keys = [key for key in COLUMNS if key in record]
        changes = ', '.join(f'{key} = excluded.{key}' for key in keys if key != 'name')
        db.execute(f'INSERT INTO photos ({", ".join(keys)}) VALUES ({", ".join("?" * len(keys))}) '
                   + (f'ON CONFLICT (name) DO UPDATE SET {changes}' if changes
                      else 'ON CONFLICT (name) DO NOTHING'),
                   [record[key] for key in keys])
    _derive(db, set(names) | _tidy(db, touched - {None}))
    db.commit()


def _tidy(db, seqs):
    """count the frames of sequences again and leave each one frame to stand
    for it (photos indexed twice, in other groups, may have moved between
    sequences); the photos that changed"""
    changed = set()
    for seq in seqs:
        rows = db.execute('SELECT name, rep, seq_n FROM photos WHERE seq = ? ORDER BY taken, name',
                          (seq,)).fetchall()
        if not rows:
            continue
        middle = len(rows) // 2
        reps = [k for k, row in enumerate(rows) if row['rep']]
        keep = min(reps, key=lambda k: abs(k - middle)) if reps else middle
        if reps != [keep] or any(row['seq_n'] != len(rows) for row in rows):
            db.execute('UPDATE photos SET seq_n = ?, rep = (name = ?) WHERE seq = ?',
                       (len(rows), rows[keep]['name'], seq))
            changed.update(row['name'] for row in rows)
    return changed


def _derive(db, names):
    """the tags and the search text (a row of `search` with the id of the
    photo) of photos, from their columns"""
    from .ebird import country_name
    for name in names:
        db.execute('DELETE FROM tags WHERE name = ?', (name,))
        row = db.execute('SELECT * FROM photos WHERE name = ?', (name,)).fetchone()
        if row is None:
            continue
        db.execute('DELETE FROM search WHERE rowid = ?', (row['id'],))
        if not row['rep']:
            continue
        for kind, column in TAGS:
            for value in (row[column] or '').split(SEP):
                if value.strip():
                    db.execute('INSERT INTO tags VALUES (?, ?, ?, ?)',
                               (name, kind, value.strip(), fold(value)))
        text = ' '.join(str(row[key] or '') for key in (
            'title', 'topic', 'description', 'species', 'latin', 'habitat', 'keywords', 'note',
            'location', 'county', 'state', 'checklist', 'camera', 'lens', 'name'))
        db.execute('INSERT INTO search (rowid, text) VALUES (?, ?)',
                   (row['id'], text + ' ' + country_name(row['country'] or '')))


def known(db):
    """{name: (path, described)} of every photo of the index"""
    return {row['name']: (row['path'], row['described'])
            for row in db.execute('SELECT name, path, described FROM photos')}


def sync_ebird(db, data):
    """copy the eBird checklists to the index when the export is a new one"""
    from .ebird import country_name
    asof = data.asof.isoformat()
    row = db.execute("SELECT value FROM meta WHERE key = 'ebird_asof'").fetchone()
    if row and row['value'] == asof:
        return
    db.execute('DELETE FROM checklists')
    db.execute('DELETE FROM observations')
    for c in data.lists.values():
        db.execute('INSERT INTO checklists VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                   (c.sid, c.day.isoformat(), c.start.isoformat(' ') if c.start else None,
                    c.duration, c.protocol, c.location, c.county, c.state, c.country,
                    c.lat, c.lon, c.comments))
        db.executemany('INSERT INTO observations VALUES (?, ?, ?, ?, ?, ?)',
                       [(c.sid, sp['francais'], sp['latin'], sp['count'], sp['details'],
                         fold(sp['francais'] + ' ' + sp['latin'])) for sp in c.species])
    db.execute("INSERT OR REPLACE INTO meta VALUES ('ebird_asof', ?)", (asof,))
    db.commit()


# ---------------------------------------------------------------------------
# Searching
# ---------------------------------------------------------------------------

def _fts(text):
    """free text as an FTS5 query: every word must be there, as a prefix"""
    words = [''.join(ch for ch in word if ch.isalnum() or ch in '_-') for word in text.split()]
    return ' '.join(f'"{word}"*' for word in words if word.strip('-_'))


def _where(q='', species='', listed=False, place='', region='', year='', since='', until='',
           topic='', kind='', flag='', checklist='', **_):
    where, args = ['p.rep = 1'], []
    if q and _fts(q):
        where.append('p.id IN (SELECT rowid FROM search WHERE search MATCH ?)')
        args.append(_fts(q))
    if species:
        like = f'%{fold(species)}%'
        clause = ("p.name IN (SELECT name FROM tags WHERE kind IN ('species', 'latin') "
                  "AND norm LIKE ?)")
        args.append(like)
        if listed:
            # also the photos without a species of their own, taken during a
            #   checklist that holds it
            clause = (f"({clause} OR (COALESCE(p.species, '') = '' AND p.checklist IN "
                      '(SELECT sid FROM observations WHERE norm LIKE ?)))')
            args.append(like)
        where.append(clause)
    if place:
        where.append('p.place_norm LIKE ?')
        args.append(f'%{fold(place)}%')
    if region:
        where.append('(p.state = ? OR p.country = ?)')
        args += [region, region]
    if year:
        where.append('substr(COALESCE(p.local, p.taken), 1, 4) = ?')
        args.append(str(year))
    if since:
        where.append('COALESCE(p.local, p.taken) >= ?')
        args.append(since)
    if until:
        where.append('substr(COALESCE(p.local, p.taken), 1, 10) <= ?')
        args.append(until)
    if topic:
        where.append('p.topic = ?')
        args.append(topic)
    if kind == 'birds':
        where.append('p.bird = 1')
    elif kind == 'others':
        where.append('COALESCE(p.bird, 0) = 0')
    if flag == 'unsure':
        where.append("p.species != '' AND p.certainty IS NOT NULL AND p.certainty != 'sûre'")
    elif flag == 'offlist':
        where.append('p.on_checklist = 0')
    elif flag == 'described':
        where.append('p.described IS NOT NULL')
    elif flag == 'undescribed':
        where.append('p.described IS NULL')
    elif flag == 'nolist':
        where.append('p.bird = 1 AND p.checklist IS NULL')
    if checklist:
        where.append('p.checklist = ?')
        args.append(checklist)
    return ' AND '.join(where), args


ORDERS = {'new': 'COALESCE(p.local, p.taken) DESC, p.name DESC',
          'old': 'COALESCE(p.local, p.taken), p.name',
          'title': "p.title IS NULL, p.title COLLATE NOCASE, COALESCE(p.local, p.taken) DESC"}


def search(db, sort='new', offset=0, limit=60, **filters):
    """the sequences that pass the filters (see _where), as dicts, and what
    they add up to: {'sequences', 'photos', 'species', 'places'}"""
    where, args = _where(**filters)
    rows = db.execute(f'SELECT p.* FROM photos p WHERE {where} '
                      f'ORDER BY {ORDERS.get(sort, ORDERS["new"])} LIMIT ? OFFSET ?',
                      args + [int(limit), int(offset)]).fetchall()
    totals = db.execute(
        f'SELECT COUNT(*) AS sequences, COALESCE(SUM(p.seq_n), 0) AS photos, '
        f"COUNT(DISTINCT NULLIF(p.location, '')) AS places FROM photos p WHERE {where}",
        args).fetchone()
    species = db.execute(
        f"SELECT COUNT(DISTINCT t.value) FROM tags t JOIN photos p ON p.name = t.name "
        f"WHERE t.kind = 'latin' AND {where}", args).fetchone()[0]
    return [dict(row) for row in rows], dict(totals, species=species)


def facets(db):
    """what the filters of the search page offer, with the number of sequences"""
    def count(sql, *args):
        return [tuple(row) for row in db.execute(sql, args)]
    return {
        'species': count("SELECT value, COUNT(*) FROM tags WHERE kind = 'species' "
                         'GROUP BY value ORDER BY value COLLATE NOCASE'),
        # the species of the checklists photos were taken during
        'listed': count('SELECT francais, COUNT(DISTINCT sid) FROM observations WHERE sid IN '
                        '(SELECT DISTINCT checklist FROM photos WHERE checklist IS NOT NULL) '
                        'GROUP BY francais ORDER BY francais COLLATE NOCASE'),
        'places': count("SELECT location, COUNT(*) FROM photos WHERE rep = 1 AND location != '' "
                        'GROUP BY location ORDER BY location COLLATE NOCASE'),
        'regions': count("SELECT state, country, COUNT(*) FROM photos WHERE rep = 1 "
                         "AND state != '' GROUP BY state ORDER BY state"),
        'years': count('SELECT substr(COALESCE(local, taken), 1, 4) AS y, COUNT(*) FROM photos '
                       'WHERE rep = 1 AND y IS NOT NULL GROUP BY y ORDER BY y DESC'),
        'topics': count("SELECT topic, COUNT(*) FROM photos WHERE rep = 1 AND topic != '' "
                        'GROUP BY topic ORDER BY COUNT(*) DESC'),
        'total': db.execute('SELECT COUNT(*) FROM photos').fetchone()[0],
        'described': db.execute('SELECT COUNT(*) FROM photos WHERE described IS NOT NULL').fetchone()[0],
        'ebird': (db.execute("SELECT value FROM meta WHERE key = 'ebird_asof'").fetchone() or [None])[0],
    }


def sequence(db, seq):
    """the frames of a sequence, in order"""
    return [dict(row) for row in db.execute(
        'SELECT name, path, taken, rep FROM photos WHERE seq = ? ORDER BY taken, name', (seq,))]


def checklist(db, sid):
    """a checklist and its species, None if the index does not have it"""
    row = db.execute('SELECT * FROM checklists WHERE sid = ?', (sid,)).fetchone()
    if row is None:
        return None
    species = db.execute('SELECT francais, latin, count, details FROM observations '
                         'WHERE sid = ? ORDER BY rowid', (sid,)).fetchall()
    return dict(row, species=[dict(sp) for sp in species])
