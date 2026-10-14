"""
The index of the photos, for the search page: one SQLite file in config
'index_dir', next to the thumbnails (thumbs/, 360 px, every frame) and the
previews (previews/, 1600 px, the frame of each sequence Claude looked at),
so that searching and browsing need neither the archive drive nor the raws.

A photo is known by its canonical name (YYMMDD_HHMMSS[_WHO]_NUM.ext), which
stays the same wherever the file moves; its path is only where it was last
seen. The path of an archived photo is kept relative to the archive, which
may be an external drive or a network mount, at one place today and another
tomorrow: resolve() looks for it under config 'photo_archive_root' and each
of 'photo_archive_mounts'. The photos of a sequence share what Claude said
of it; the search works on sequences, through the frame Claude looked at
(rep = 1).

SQLite rather than DuckDB: the search page reads the index while patchdoc
writes to it, which one DuckDB file does not allow between two processes,
and its full-text index follows every insert. The columns are typed, so
DuckDB reads the file as it is for an analysis of one's own:
ATTACH 'photos.sqlite' AS idx (TYPE sqlite, READ_ONLY).

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

#: the columns of a photo (lists are joined with '; '); text unless in TYPES
COLUMNS = ('name', 'path', 'stem', 'ext',
           'taken',            # the clock of the camera, ISO
           'local',            # the time at the place: taken + clock_offset
           'clock_offset',     # hours
           'zone',             # the time zone the camera says it is set to, hours from UTC
           'camera', 'lens', 'focal',
           'bird',             # 1 when taken beyond config 'bird_focal_min'
           'seq', 'seq_n', 'rep',
           'title', 'topic', 'description', 'species', 'latin', 'habitat', 'keywords',
           'certainty',
           'verified',         # the photographer checked the identification himself: what was
                               #   said before he did; NULL if he never did
           'eye', 'clear', 'level',    # a bird photo, 1 to 5: the focus on the eye, how
                                       #   clear of branches the bird is, how close to eye level
           'sharp', 'best',            # in a burst: the sharpness measured on the eye (1 for
                                       #   the sharpest frame), and 1 for its best frame
           'eye_x', 'eye_y',           # where the eye of the bird is, from the left and from the
                                       #   top, as fractions of the picture
           'people',           # the people the photographer named in the photo
           'guessed',          # those faces.py proposes, not confirmed
           'faces',            # how many faces were found in it; NULL if nobody looked
           'note',             # what Claude says of the photo against the checklist
           'checklist',        # eBird submission ID
           'match',            # 'inside' or 'near' the hours of the checklist
           'on_checklist',     # 1: every species named is on the checklist, 0: one is not
           'location', 'county', 'state', 'country', 'lat', 'lon',
           'own_position',     # 1 when lat and lon are the position the photo carries itself
                               #   (a phone) and the place is named from it, not from the
                               #   checklist
           'turned',          # how many times the pictures kept of the photo were made again
                              #   another way up: the page then asks for them anew
           'upright',         # 1 once those pictures, and what is placed on them (eye,
                              #   faces), follow the Orientation of the file (doc.straighten)
           'described',       # when Claude described it, ISO; NULL if it never did
           'written',          # when that was written into the file; 'pending' while it waits
           'place_norm')

TYPES = {'clock_offset': 'REAL', 'zone': 'REAL', 'focal': 'REAL', 'lat': 'REAL', 'lon': 'REAL',
         'bird': 'INTEGER', 'seq_n': 'INTEGER', 'rep': 'INTEGER', 'on_checklist': 'INTEGER',
         'own_position': 'INTEGER', 'turned': 'INTEGER', 'upright': 'INTEGER',
         'eye':'INTEGER', 'clear': 'INTEGER', 'level': 'INTEGER', 'faces': 'INTEGER',
         'sharp': 'REAL', 'best': 'INTEGER', 'eye_x': 'REAL', 'eye_y': 'REAL'}

PHOTOS = ('(id INTEGER PRIMARY KEY, '
          + ', '.join(f'{column} {TYPES.get(column, "TEXT")}' for column in COLUMNS)
          + ', UNIQUE (name))')
TABLES = {
    'tags': '(name TEXT, kind TEXT, value TEXT, norm TEXT)',
    'checklists': '(sid TEXT PRIMARY KEY, day TEXT, start TEXT, duration INTEGER, protocol TEXT, '
                  'location TEXT, county TEXT, state TEXT, country TEXT, lat REAL, lon REAL, '
                  'comments TEXT)',
    'observations': '(sid TEXT, francais TEXT, latin TEXT, count TEXT, details TEXT, norm TEXT)',
    'meta': '(key TEXT PRIMARY KEY, value TEXT)',
}
SCHEMA = f"""
CREATE TABLE IF NOT EXISTS photos {PHOTOS};
CREATE INDEX IF NOT EXISTS photos_seq ON photos (seq);
CREATE INDEX IF NOT EXISTS photos_rep ON photos (rep, local);
CREATE INDEX IF NOT EXISTS photos_checklist ON photos (checklist);
CREATE TABLE IF NOT EXISTS tags {TABLES['tags']};
CREATE INDEX IF NOT EXISTS tags_name ON tags (name);
CREATE INDEX IF NOT EXISTS tags_kind ON tags (kind, value);
CREATE VIRTUAL TABLE IF NOT EXISTS search USING fts5(
    text, tokenize = 'unicode61 remove_diacritics 2');
CREATE TABLE IF NOT EXISTS checklists {TABLES['checklists']};
CREATE TABLE IF NOT EXISTS observations {TABLES['observations']};
CREATE INDEX IF NOT EXISTS observations_sid ON observations (sid);
CREATE TABLE IF NOT EXISTS meta {TABLES['meta']};
CREATE TABLE IF NOT EXISTS geocode (cell TEXT PRIMARY KEY, answer TEXT);
CREATE TABLE IF NOT EXISTS absent (copy TEXT, name TEXT, PRIMARY KEY (copy, name)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS people (id INTEGER PRIMARY KEY, name TEXT UNIQUE, born TEXT);
CREATE TABLE IF NOT EXISTS faces (id INTEGER PRIMARY KEY, photo TEXT, x REAL, y REAL, w REAL,
    h REAL, score REAL, vector BLOB, taken TEXT, person INTEGER, sure INTEGER DEFAULT 0,
    likeness REAL, banned TEXT);
CREATE INDEX IF NOT EXISTS faces_photo ON faces (photo);
"""

#: the kinds of tags, and the column of a photo each comes from
TAGS = (('species', 'species'), ('latin', 'latin'), ('habitat', 'habitat'),
        ('keyword', 'keywords'), ('topic', 'topic'), ('person', 'people'), ('maybe', 'guessed'))
#: the sections of the search page: bird photos, photos with people, the rest
# (a column that is NULL would make "NOT ..." NULL, and lose the row)
BIRDS = "(COALESCE(p.bird, 0) = 1 OR COALESCE(p.topic, '') = 'oiseau')"
FAMILY = ("(COALESCE(p.topic, '') = 'personnes' OR COALESCE(p.people, '') != '' "
          "OR COALESCE(p.guessed, '') != '' OR COALESCE(p.faces, 0) > 0)")
SEP = '; '


def fold(text):
    """lower case without accents, to compare what is typed with what is kept"""
    text = unicodedata.normalize('NFKD', text or '')
    return ''.join(ch for ch in text if not unicodedata.combining(ch)).lower()


def _compare(one, other):
    one, other = fold(one), fold(other)
    return (one > other) - (one < other)


def collate(db):
    """give a connection the order of names as French has it, whatever their
    accents and capitals (ORDER BY ... COLLATE FOLDED); the connection"""
    db.create_collation('FOLDED', _compare)
    return db


def index_dir():
    return os.path.expanduser(_config_module.load()['index_dir'])


def db_path():
    return os.path.join(index_dir(), 'photos.sqlite')


def connect():
    os.makedirs(index_dir(), exist_ok=True)
    db = collate(sqlite3.connect(db_path(), timeout=60))
    db.row_factory = sqlite3.Row
    _shared(db)
    _retype(db)
    db.executescript(SCHEMA)
    # a column added since the index was made
    made = {row['name'] for row in db.execute('PRAGMA table_info(photos)')}
    for column in COLUMNS:
        if column not in made:
            db.execute(f'ALTER TABLE photos ADD COLUMN {column} {TYPES.get(column, "TEXT")}')
    if 'own_position' not in made:
        # an index made before the column: the photos placed by their position
        #   were those without a checklist
        with db:
            db.execute("UPDATE photos SET own_position = 1 WHERE match = 'gps'")
    _relative(db)
    return db


def _shared(db):
    """let the index be read while it is written: the search page, patchdoc
    in a terminal and DuckDB may all have it open. With the write-ahead log
    of SQLite a reader never makes a writer wait, nor a writer a reader;
    with its default journal, a reader that lingers stops a write, and a
    write stopped half-way locks every reader out. An index on a network
    drive, where that log does not work, keeps the journal it has."""
    try:
        if db.execute('PRAGMA journal_mode').fetchone()[0].lower() != 'wal':
            db.execute('PRAGMA journal_mode = WAL')
        db.execute('PRAGMA synchronous = NORMAL')
    except sqlite3.OperationalError:
        pass


def _retype(db):
    """give their types to the columns of an index made before they had any
    (DuckDB and pandas read an untyped column as bytes)"""
    made = {row['name']: row['type'] for row in db.execute('PRAGMA table_info(photos)')}
    if not made or all(made.values()):
        return
    tables = dict(TABLES, photos=PHOTOS)
    with db:
        for table, columns in tables.items():
            if not db.execute('SELECT 1 FROM sqlite_master WHERE name = ?', (table,)).fetchone():
                continue
            names = ', '.join(row['name'] for row in db.execute(f'PRAGMA table_info({table})'))
            db.execute(f'ALTER TABLE {table} RENAME TO {table}_untyped')
            db.execute(f'CREATE TABLE {table} {columns}')
            db.execute(f'INSERT INTO {table} ({names}) SELECT {names} FROM {table}_untyped')
            db.execute(f'DROP TABLE {table}_untyped')
    db.executescript(SCHEMA)        # the search indexes went with the old tables
    db.execute('VACUUM')            # and the room they took is given back


def _relative(db):
    """make relative the paths of archived photos kept in full by an older
    index (see roots())"""
    for root in roots():
        head = root + os.sep
        if db.execute('SELECT 1 FROM photos WHERE substr(path, 1, ?) = ? LIMIT 1',
                      (len(head), head)).fetchone():
            with db:
                db.execute('UPDATE photos SET path = substr(path, ?) WHERE substr(path, 1, ?) = ?',
                           (len(head) + 1, len(head), head))


def meta(key):
    """a value the index keeps under a name, None without one (or without an
    index: nothing is made to look)"""
    if not os.path.isfile(db_path()):
        return None
    db = sqlite3.connect(f'file:{db_path()}?mode=ro', uri=True, timeout=60)
    try:
        row = db.execute('SELECT value FROM meta WHERE key = ?', (key,)).fetchone()
    except sqlite3.OperationalError:
        row = None
    db.close()
    return row[0] if row else None


def set_meta(db, key, value):
    db.execute('INSERT OR REPLACE INTO meta VALUES (?, ?)', (key, str(value)))
    db.commit()


# ---------------------------------------------------------------------------
# Where the photos are: an archive that is not always at the same place
# ---------------------------------------------------------------------------

def roots():
    """the places the archive may be mounted at, the usual one first"""
    return _config_module.archive_roots()


def shorten(path):
    """the path of a photo as the index keeps it: relative to the archive
    when the photo is in it, unchanged otherwise"""
    for root in roots():
        if path and path.startswith(root + os.sep):
            return path[len(root) + 1:]
    return path


def resolve(path):
    """where a photo of the index can be read now: under the first archive
    root that has it; None when it cannot be reached (drive not mounted)"""
    if not path:
        return None
    if os.path.isabs(path):
        if os.path.exists(path):
            return path
        path = shorten(path)
        if os.path.isabs(path):
            return None
    for root in roots():
        full = os.path.join(root, path)
        if os.path.exists(full):
            return full
    return None


def _image_path(kind, name):
    stem = os.path.splitext(name)[0]
    month = stem[:4] if stem[:6].isdigit() else 'misc'
    return os.path.join(index_dir(), kind, month, stem + '.jpg')


def thumb_path(name):
    return _image_path('thumbs', name)


def preview_path(name):
    return _image_path('previews', name)


def eye_path(name):
    """the eye of the bird of a photo at full resolution, for the loupe"""
    return _image_path('eyes', name)


def save_eye(img, name):
    """keep the square around the eye of a photo (a PIL image)"""
    path = eye_path(name)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    img.save(path, quality=88)


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
        if record.get('path'):
            record = dict(record, path=shorten(record['path']))
        if any(key in record for key in ('location', 'county', 'state', 'country')):
            record = dict(record, place_norm=place_text(record))
        keys = [key for key in COLUMNS if key in record]
        changes = ', '.join(f'{key} = excluded.{key}' for key in keys if key != 'name')
        db.execute(f'INSERT INTO photos ({", ".join(keys)}) VALUES ({", ".join("?" * len(keys))}) '
                   + (f'ON CONFLICT (name) DO UPDATE SET {changes}' if changes
                      else 'ON CONFLICT (name) DO NOTHING'),
                   [record[key] for key in keys])
    _derive(db, set(names) | _tidy(db, touched - {None}))
    db.commit()


def rename(db, moved):
    """follow photos whose files changed name, place, or both: {name in the
    index: path now}. Their rows keep all they hold; a sequence named after
    a frame that changed name takes its new name, and the tags and the
    images kept (thumbnail, preview, eye) follow. A photo that would take a
    name the index already has is left as it is. Returns {name before: name
    now} of the photos that changed name (their faces are for the caller:
    faces.renamed)."""
    done = {}
    for old, path in moved.items():
        new = os.path.basename(path)
        row = db.execute('SELECT id, seq FROM photos WHERE name = ?', (old,)).fetchone()
        if row is None or (new != old and db.execute('SELECT 1 FROM photos WHERE name = ?',
                                                     (new,)).fetchone()):
            continue
        stem, before = os.path.splitext(new)[0], os.path.splitext(old)[0]
        db.execute('UPDATE photos SET name = ?, stem = ?, ext = ?, path = ? WHERE id = ?',
                   (new, stem, os.path.splitext(new)[1].lstrip('.').lower(), shorten(path),
                    row['id']))
        if new == old:
            continue
        db.execute('UPDATE tags SET name = ? WHERE name = ?', (new, old))
        if row['seq'] == before:
            db.execute('UPDATE photos SET seq = ? WHERE seq = ?', (stem, before))
        for kept in (thumb_path, preview_path, eye_path):
            if os.path.isfile(kept(old)) and not os.path.exists(kept(new)):
                os.makedirs(os.path.dirname(kept(new)), exist_ok=True)
                os.replace(kept(old), kept(new))
        done[old] = new
    _derive(db, done.values())
    db.commit()
    return done


def forget(db, names):
    """take photos out of the index (a file renamed takes its new name)"""
    names = list(names)
    seqs = set()
    for name in names:
        row = db.execute('SELECT id, seq FROM photos WHERE name = ?', (name,)).fetchone()
        if row is None:
            continue
        seqs.add(row['seq'])
        db.execute('DELETE FROM search WHERE rowid = ?', (row['id'],))
        db.execute('DELETE FROM tags WHERE name = ?', (name,))
        db.execute('DELETE FROM photos WHERE name = ?', (name,))
    _derive(db, _tidy(db, seqs - {None}))
    db.commit()


def place_text(record):
    """the place of a photo as it is searched: its names, folded"""
    from .ebird import country_name
    return fold(' '.join(str(record.get(key) or '') for key in ('location', 'county', 'state'))
                + ' ' + country_name(record.get('country') or ''))


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
            'people', 'guessed',
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
           topic='', kind='', flag='', checklist='', section='', person='', eye='', clear='',
           level='', place_of='', **_):
    where, args = ['p.rep = 1'], []
    # a photo of the archive has a path relative to it; one that waits to be
    #   sent there, where it is
    if place_of == 'attente':
        where.append("p.path LIKE '/%'")
    elif place_of == 'archive':
        where.append("p.path NOT LIKE '/%'")
    elif place_of.startswith('/'):
        # what one copy of the archive holds (its root): the photos of the
        #   archive, less those it was found not to have (doc.index_copy)
        where.append("p.path NOT LIKE '/%' AND p.name NOT IN "
                     '(SELECT name FROM absent WHERE copy = ?)')
        args.append(place_of)
    if section == 'oiseaux':
        where.append(BIRDS)
    elif section == 'famille':
        where.append(f'{FAMILY} AND NOT {BIRDS}')
    elif section == 'autre':
        where.append(f'NOT {FAMILY} AND NOT {BIRDS}')
    if person:
        where.append("p.name IN (SELECT name FROM tags WHERE kind IN ('person', 'maybe') "
                     'AND norm LIKE ?)')
        args.append(f'%{fold(person)}%')
    for column, least in (('eye', eye), ('clear', clear), ('level', level)):
        if str(least).isdigit():
            where.append(f'p.{column} >= ?')
            args.append(int(least))
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


#: the orders of the search page. The last three bring first what they are
#:  named after (the sequences with a species, by its name; the panoramas;
#:  the photos with people: those named, those proposed, then the faces
#:  nobody named yet), and the rest after, the newest first
#:  (a photo without a date, or dated after today by a camera whose clock was
#:  wrong, comes last in either direction; names are compared as French
#:  does, É with E: the collation FOLDED of collate())
_WHEN = 'COALESCE(p.local, p.taken)'
_UNDATED = f"({_WHEN} IS NULL OR {_WHEN} = '' OR {_WHEN} > datetime('now', '+2 days'))"
_NEWEST = f'{_UNDATED}, {_WHEN} DESC, p.name DESC'
ORDERS = {'new': _NEWEST,
          'old': f'{_UNDATED}, {_WHEN}, p.name',
          'title': f'p.title IS NULL, p.title COLLATE FOLDED, {_NEWEST}',
          'species': f"COALESCE(p.species, '') = '', NULLIF(p.species, '') COLLATE FOLDED, {_NEWEST}",
          'pano': f"COALESCE(p.camera LIKE '% (panorama)', 0) DESC, {_NEWEST}",
          'people': "CASE WHEN COALESCE(p.people, '') != '' THEN 0 WHEN COALESCE(p.guessed, '') != '' "
                    'THEN 1 WHEN COALESCE(p.faces, 0) > 0 THEN 2 ELSE 3 END, '
                    f"COALESCE(NULLIF(p.people, ''), NULLIF(p.guessed, '')) COLLATE FOLDED, {_NEWEST}"}


def search(db, sort='new', offset=0, limit=60, counts=True, **filters):
    """the sequences that pass the filters (see _where), as dicts, and what
    they add up to: {'sequences', 'photos', 'species', 'places'} (None when
    counts is False: a page after the first)"""
    where, args = _where(**filters)
    rows = db.execute(f'SELECT p.* FROM photos p WHERE {where} '
                      f'ORDER BY {ORDERS.get(sort, ORDERS["new"])} LIMIT ? OFFSET ?',
                      args + [int(limit), int(offset)]).fetchall()
    if not counts:
        return [dict(row) for row in rows], None
    totals = db.execute(
        f'SELECT COUNT(*) AS sequences, COALESCE(SUM(p.seq_n), 0) AS photos, '
        f"COUNT(DISTINCT NULLIF(p.location, '')) AS places FROM photos p WHERE {where}",
        args).fetchone()
    species = db.execute(
        f"SELECT COUNT(DISTINCT t.value) FROM tags t JOIN photos p ON p.name = t.name "
        f"WHERE t.kind = 'latin' AND {where}", args).fetchone()[0]
    return [dict(row) for row in rows], dict(totals, species=species)


def places(db, **filters):
    """the places of the sequences that pass the filters, whatever the place
    asked for, for the map: [(place, latitude, longitude, sequences, photos)]"""
    where, args = _where(**dict(filters, place=''))
    return [tuple(row) for row in db.execute(
        f'SELECT p.location, AVG(p.lat), AVG(p.lon), COUNT(*), SUM(p.seq_n) FROM photos p '
        f"WHERE {where} AND p.lat IS NOT NULL AND p.lon IS NOT NULL AND p.location != '' "
        'GROUP BY p.location', args)]


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
        'people': count('SELECT p.name, p.born, COUNT(f.id) FROM people p '
                        'LEFT JOIN faces f ON f.person = p.id GROUP BY p.id '
                        'ORDER BY p.name COLLATE NOCASE'),
        'total': db.execute('SELECT COUNT(*) FROM photos').fetchone()[0],
        'described': db.execute('SELECT COUNT(*) FROM photos WHERE described IS NOT NULL').fetchone()[0],
        'ebird': (db.execute("SELECT value FROM meta WHERE key = 'ebird_asof'").fetchone() or [None])[0],
    }


def sequence(db, seq):
    """the frames of a sequence, in order"""
    return [dict(row) for row in db.execute(
        'SELECT name, path, taken, rep, eye, clear, level, sharp, best, eye_x, eye_y, turned '
        'FROM photos '
        'WHERE seq = ? ORDER BY taken, name', (seq,))]


def checklist(db, sid):
    """a checklist and its species, None if the index does not have it"""
    row = db.execute('SELECT * FROM checklists WHERE sid = ?', (sid,)).fetchone()
    if row is None:
        return None
    species = db.execute('SELECT francais, latin, count, details FROM observations '
                         'WHERE sid = ? ORDER BY rowid', (sid,)).fetchall()
    return dict(row, species=[dict(sp) for sp in species])
