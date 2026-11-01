"""
The faces of the photos: found, named by the photographer, found again.

Nothing here goes to Claude or online. The faces are found and compared on
this machine with two small models of OpenCV, downloaded once from the
OpenCV model zoo into the index folder: YuNet finds the faces of a picture,
SFace turns each into 128 numbers that are close for the same person.

How a person gets a name: the photographer names a face in the search page
(a square on the face). That face is then an example of the person, dated
by its photo. recognise() compares every face without a name with the
examples and proposes the person whose example is the most alike, when the
likeness reaches GUESS. A proposed name is shown with a question mark until
the photographer confirms or refuses it; only confirmed names are written
into the files. Each answer is kept with the likeness it was given to
(table face_answers), which says from what likeness a name can be trusted;
draw_check() draws faces at random to measure that (table face_check).
board() gives the faces to confirm in one go, those that help most first.

People change, children fast, and the photos of a family go back to before
the children were born. So an example only speaks for the years around its
photo: reach() gives how many, from one year for a baby to twelve for an
adult when the year of birth of the person is known, four when it is not,
and nobody is looked for before being born. A face recognised with a high
likeness (CHAIN) becomes an example in turn, without being shown as sure, so
that a name given at eight can walk back photo by photo towards the baby,
and forward towards the teenager. A few names given at different ages
anchor the walk; that is the way to help it.
"""

import collections
import os
import threading
import urllib.request
from datetime import datetime, timedelta

from . import photoindex

ZOO = 'https://github.com/opencv/opencv_zoo/raw/main/models/'
MODELS = {'find': 'face_detection_yunet/face_detection_yunet_2023mar.onnx',
          'tell': 'face_recognition_sface/face_recognition_sface_2021dec.onnx'}

#: a face is kept when the detector is this sure of it, and this many pixels
#:  wide in a picture of 1600
FACE, SMALL = 0.85, 36
#: likeness (cosine of the two vectors) from which a name is proposed, and
#:  from which a proposed face becomes an example for others; OpenCV gives
#:  0.363 as the limit of "the same person" for SFace
GUESS, CHAIN = 0.42, 0.52
#: and from which the page says a proposed name is very clear ("très
#:  probable"). Measured on 2026-10-24 on the 151 faces the photographer had
#:  named (13 people): the most alike face of another person reaches it for
#:  1 % of them, the most alike face of the same person for 42 %
CLEAR = 0.70
#: the faces drawn for the photographer to check: this likeness at least, this
#:  gap at least to the second name, this many. Replayed on 2026-10-31 on the
#:  294 faces confirmed and 12 refused, no name was wrong in 48 at 0.85 or
#:  more, 2 in 110 at 0.80 (the sisters, and faces of people not yet named):
#:  too few to say how far 0.85 can be trusted, which these answers will
CHECK = (0.85, 0.05, 150)
#: the faces of a board of confirmation, and how many for one person in one year
BOARD = (48, 6)
#: an answer goes for the faces of the same moment (this many seconds apart, or
#:  the same sequence) at least this alike the face answered. Faces of one
#:  moment share age and light: measured on 2026-11-01 on the 467 faces
#:  confirmed, of 324 pairs of different people of the same day none reached
#:  0.50, and 148 of 221 pairs of the same person reached 0.60
PUSH, MOMENT = 0.60, 12 * 3600

_lock, _nets, _early = threading.Lock(), {}, threading.Lock()


def available():
    """whether faces can be looked for here (OpenCV with its face models)"""
    try:
        import cv2
        return hasattr(cv2, 'FaceDetectorYN') and hasattr(cv2, 'FaceRecognizerSF')
    except ImportError:
        return False


def _model(kind):
    """the file of a model, downloaded the first time"""
    path = os.path.join(photoindex.index_dir(), 'models', os.path.basename(MODELS[kind]))
    if not os.path.isfile(path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        request = urllib.request.Request(ZOO + MODELS[kind], headers={'User-Agent': 'patchdoc'})
        with urllib.request.urlopen(request, timeout=300) as reply, open(path + '.tmp', 'wb') as out:
            while chunk := reply.read(1 << 20):
                out.write(chunk)
        os.replace(path + '.tmp', path)
    return path


def look(img):
    """the faces of a picture (a PIL image): [{'x', 'y', 'w', 'h'} as
    fractions of the picture, 'score', 'vector' (128 float32 of length 1)]"""
    import cv2
    import numpy as np
    picture = img.convert('RGB')
    picture.thumbnail((1600, 1600))
    pixels = np.asarray(picture)[:, :, ::-1].copy()
    height, width = pixels.shape[:2]
    out = []
    with _lock:
        if not _nets:
            _nets['find'] = cv2.FaceDetectorYN.create(_model('find'), '', (320, 320), FACE, 0.3, 60)
            _nets['tell'] = cv2.FaceRecognizerSF.create(_model('tell'), '')
        _nets['find'].setInputSize((width, height))
        found = _nets['find'].detect(pixels)[1]
        for row in found if found is not None else []:
            x, y, w, h = (float(v) for v in row[:4])
            if min(w, h) < SMALL:
                continue
            vector = _nets['tell'].feature(_nets['tell'].alignCrop(pixels, row)).flatten()
            vector = (vector / (np.linalg.norm(vector) or 1.0)).astype('float32')
            out.append({'x': max(0.0, x) / width, 'y': max(0.0, y) / height, 'w': w / width,
                        'h': h / height, 'score': float(row[-1]), 'vector': vector.tobytes()})
    return out


def store(db, name, found, taken):
    """keep the faces found in a photo (once: the names given later hang on
    them)"""
    if db.execute('SELECT 1 FROM faces WHERE photo = ? LIMIT 1', (name,)).fetchone() is None:
        db.executemany('INSERT INTO faces (photo, x, y, w, h, score, vector, taken) '
                       'VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                       [(name, f['x'], f['y'], f['w'], f['h'], f['score'], f['vector'], taken)
                        for f in found])
    db.execute('UPDATE photos SET faces = ? WHERE name = ?', (len(found), name))
    db.commit()


def reach(age):
    """how many years around its photo a face speaks for a person of this
    age (None: the year of birth is not known)"""
    if age is None:
        return 4.0
    return 1.0 if age < 3 else 2.0 if age < 8 else 3.0 if age < 16 else 6.0 if age < 25 else 12.0


def _year(taken):
    try:
        when = datetime.fromisoformat(taken)
        return when.year + (when.timetuple().tm_yday - 1) / 365.25
    except (TypeError, ValueError):
        return None


def recognise(db, photos=None):
    """propose a name for every face that has none (see the module); the
    photos whose proposals changed. photos: only the faces of these photos
    are looked at, the others keep their proposals and the examples they
    make (a second for a photo just named or looked at, where all the faces
    take about a minute: later() does those)"""
    import numpy as np
    rows = db.execute('SELECT id, photo, vector, taken, person, sure, banned, likeness '
                      'FROM faces').fetchall()
    born = {row['id']: _year(f"{row['born']}-07-01") if row['born'] else None
            for row in db.execute('SELECT id, born FROM people')}
    if not rows:
        return set()
    vectors = np.stack([np.frombuffer(row['vector'], dtype='float32') for row in rows])
    years = [_year(row['taken']) for row in rows]
    banned = [{int(p) for p in (row['banned'] or '').split(',') if p} for row in rows]
    named = [k for k, row in enumerate(rows) if row['sure'] == 1 and row['person']]
    unknown = [k for k, row in enumerate(rows) if row['sure'] != 1
               and (photos is None or row['photo'] in photos)]
    # (the proposals left as they are, and the examples they make)
    fixed = {} if photos is None else {
        k: (row['person'], row['likeness']) for k, row in enumerate(rows)
        if row['sure'] != 1 and row['photo'] not in photos and row['person']
        and row['likeness'] is not None}
    guess, last = dict(fixed), None     # index of a face -> (person, likeness)
    for _ in range(8):
        examples = named + [k for k, (_, likeness) in guess.items() if likeness >= CHAIN]
        who = [rows[k]['person'] if k in named else guess[k][0] for k in examples]
        if not examples or not unknown:
            break
        alike = vectors[unknown] @ vectors[examples].T
        fresh = {}
        for a, k in enumerate(unknown):
            best = None
            for b in np.argsort(-alike[a])[:40]:
                likeness, person, e = float(alike[a, b]), who[b], examples[b]
                if likeness < GUESS:
                    break
                if e == k or person in banned[k]:
                    continue
                if years[k] is not None and years[e] is not None:
                    start = born.get(person)
                    if start is not None and years[k] < start - 0.3:
                        continue        # not born yet
                    age = None if start is None else (years[k] + years[e]) / 2 - start
                    if abs(years[k] - years[e]) > reach(age):
                        continue        # an example of another age
                best = (person, likeness)
                break
            if best:
                fresh[k] = best
        # one person is once in a photo: the most alike keeps the name
        taken = {}
        for k, (person, likeness) in sorted(fresh.items(), key=lambda item: -item[1][1]):
            if (rows[k]['photo'], person) in taken:
                del fresh[k]
            else:
                taken[(rows[k]['photo'], person)] = k
        if fresh == last:
            break
        guess, last = {**fixed, **fresh}, fresh
    changed = set()
    for k in unknown:
        person, likeness = guess.get(k, (None, None))
        if person != rows[k]['person']:
            changed.add(rows[k]['photo'])
        # (a face the photographer named meanwhile keeps its name)
        db.execute('UPDATE faces SET person = ?, likeness = ?, sure = 0 WHERE id = ? AND sure != 1',
                   (person, likeness, rows[k]['id']))
    db.commit()
    return changed


def sync(db, photos):
    """bring to the index the names of the faces of these photos: for each
    sequence, 'people' (the names the photographer gave) and 'guessed' (the
    names proposed); the files of a sequence whose names changed wait to be
    written"""
    for name in photos:
        row = db.execute('SELECT seq, people FROM photos WHERE name = ?', (name,)).fetchone()
        if row is None:
            continue
        frames = [r['name'] for r in db.execute('SELECT name FROM photos WHERE seq = ?',
                                                (row['seq'],))] or [name]
        marks = ', '.join('?' * len(frames))
        found = db.execute(
            f'SELECT p.name, f.sure FROM faces f JOIN people p ON p.id = f.person '
            f'WHERE f.photo IN ({marks}) ORDER BY f.x', frames).fetchall()
        people = photoindex.SEP.join(dict.fromkeys(r['name'] for r in found if r['sure'] == 1))
        guessed = photoindex.SEP.join(dict.fromkeys(
            r['name'] for r in found if r['sure'] != 1 and r['name'] not in people))
        records = [{'name': frame, 'people': people, 'guessed': guessed} for frame in frames]
        if people != (row['people'] or ''):
            records = [dict(record, written='pending') for record in records]
        photoindex.put(db, records)


def name(db, face, person, born=None):
    """the photographer says who a face is (person: '' to take the name
    off, or to refuse the one proposed); the photos to show again"""
    return answers(db, [(face, person, born)])['changed']


def answers(db, said):
    """what the photographer said of faces, [(face, person, year of birth)],
    kept together and pushed to the faces of the same moment (_push), then
    the proposals of their photos made again: {'changed': the photos to show
    again, 'pushed': [{'face', 'person'}], person '' for a name refused}.
    The whole base follows in the background."""
    done = [(face, _answer(db, face, person, born)) for face, person, born in said]
    done = [(face, kept) for face, kept in done if kept]
    photos, pushed = {kept['photo'] for _, kept in done}, []
    for face, kept in done:
        pushed += _push(db, face, kept)
    db.commit()
    if not photos:
        return {'changed': set(), 'pushed': []}
    photos |= {row['photo'] for row in db.execute(
        f"SELECT photo FROM faces WHERE id IN ({', '.join('?' * len(pushed))})",
        [one['face'] for one in pushed])} if pushed else set()
    # (at once for the faces of these photos: one person is once in a photo;
    #   the others in the background)
    changed = recognise(db, photos) | photos
    sync(db, changed)
    later()
    return {'changed': changed, 'pushed': pushed}


def _push(db, face, kept):
    """what the photographer just said of a face goes for the faces of the
    same moment that are at least PUSH alike it and still have no answer. A
    name given is given to them too, if their photo does not have that person
    already and they do not look as much like somebody else confirmed in that
    moment; a name refused is refused for those it was proposed for. Only the
    answer pushes, not what it pushed: the faces reached are said to come from
    it (face_answers.pushed_by). What was pushed: [{'face', 'person'}]."""
    import numpy as np
    me = db.execute('SELECT f.vector, f.taken, ph.seq FROM faces f LEFT JOIN photos ph '
                    'ON ph.name = f.photo WHERE f.id = ?', (face,)).fetchone()
    given, refused = kept['given'], kept['proposed'] if not kept['given'] else None
    if not given and not refused:
        return []       # (a name taken off a confirmed face says nothing of the others)
    when = _moment(me['taken'])
    days = [] if when is None else [str((when + timedelta(days=by)).date()) for by in (-1, 0, 1)]
    marks = ', '.join('?' * len(days)) or "''"
    rows = db.execute(
        f'SELECT f.id, f.photo, f.vector, f.taken, f.person, f.sure, f.banned, f.likeness, p.name AS proposed '
        f'FROM faces f LEFT JOIN photos ph ON ph.name = f.photo LEFT JOIN people p ON p.id = f.person '
        f'WHERE f.id != ? AND (ph.seq = ? OR substr(f.taken, 1, 10) IN ({marks}))',
        [face, me['seq']] + days).fetchall()
    mine = np.frombuffer(me['vector'], dtype='float32')

    def vector(row):
        return np.frombuffer(row['vector'], dtype='float32')

    def close(row):
        there = _moment(row['taken'])
        return there is None or when is None or abs((there - when).total_seconds()) <= MOMENT

    rows = [row for row in rows if close(row)]
    who = None
    if given:
        who = db.execute('SELECT id FROM people WHERE name = ?', (given,)).fetchone()['id']
    held = {row['photo'] for row in rows if row['sure'] == 1 and row['person'] == who}
    others = [row for row in rows if row['sure'] == 1 and row['person'] not in (None, who)]
    found = {}
    for row in rows:
        likeness = float(vector(row) @ mine)
        if row['sure'] == 1 or likeness < PUSH or row['photo'] == kept['photo']:
            continue
        if given:
            if row['photo'] in held or str(who) in (row['banned'] or '').split(','):
                continue
            if any(float(vector(row) @ vector(other)) >= PUSH for other in others):
                continue
            # (one person is once in a photo: the most alike)
            if row['photo'] not in found or likeness > found[row['photo']][0]:
                found[row['photo']] = (likeness, row)
        elif row['person'] == refused:
            found[row['id']] = (likeness, row)
    now = datetime.now().isoformat(' ', 'seconds')
    pushed = []
    for _, row in found.values():
        db.execute('INSERT INTO face_answers (face, photo, proposed, likeness, given, answered, pushed_by) '
                   'VALUES (?, ?, ?, ?, ?, ?, ?)', (row['id'], row['photo'], row['proposed'],
                                                    row['likeness'], given or '', now, face))
        if given:
            db.execute('UPDATE faces SET person = ?, sure = 1, likeness = NULL WHERE id = ?', (who, row['id']))
        else:
            banned = ','.join(filter(None, [row['banned'] or '', str(refused)]))
            db.execute('UPDATE faces SET person = NULL, sure = 0, likeness = NULL, banned = ? WHERE id = ?',
                       (banned, row['id']))
        db.execute('UPDATE face_check SET answer = ?, answered = ? WHERE face = ? AND answer IS NULL',
                   (given or '', now, row['id']))
        pushed.append({'face': row['id'], 'person': given or ''})
    return pushed


def _moment(taken):
    try:
        return datetime.fromisoformat(taken)
    except (TypeError, ValueError):
        return None


def _answer(db, face, person, born=None):
    """keep what the photographer said of a face, without committing: {'photo',
    'given', 'proposed': the person proposed if a name was}, None for a face
    unknown"""
    row = db.execute('SELECT f.photo, f.person, f.sure, f.banned, f.likeness, p.name AS proposed '
                     'FROM faces f LEFT JOIN people p ON p.id = f.person WHERE f.id = ?',
                     (face,)).fetchone()
    if row is None:
        return None
    person = ' '.join((person or '').split())
    # what was proposed and what the photographer said: its likeness goes
    #   with the proposal, and this is the only place that remembers it
    when = datetime.now().isoformat(' ', 'seconds')
    db.execute('INSERT INTO face_answers (face, photo, proposed, likeness, given, answered) '
               'VALUES (?, ?, ?, ?, ?, ?)',
               (face, row['photo'], row['proposed'] if row['sure'] != 1 else None,
                row['likeness'] if row['sure'] != 1 else None, person, when))
    db.execute('UPDATE face_check SET answer = ?, answered = ? WHERE face = ? AND answer IS NULL',
               (person, when, face))
    if person:
        db.execute('INSERT OR IGNORE INTO people (name) VALUES (?)', (person,))
        who = db.execute('SELECT id FROM people WHERE name = ?', (person,)).fetchone()['id']
        if born:
            db.execute('UPDATE people SET born = ? WHERE id = ?', (str(born)[:4], who))
        db.execute('UPDATE faces SET person = ?, sure = 1, likeness = NULL WHERE id = ?', (who, face))
    else:
        # a proposal refused is not proposed again for this face
        refused = row['banned'] or ''
        if row['sure'] != 1 and row['person']:
            refused = ','.join(filter(None, [refused, str(row['person'])]))
        db.execute('UPDATE faces SET person = NULL, sure = 0, likeness = NULL, banned = ? '
                   'WHERE id = ?', (refused, face))
    return {'photo': row['photo'], 'given': person,
            'proposed': row['person'] if row['sure'] != 1 else None}


#: the background recognise() of every face: whether one runs, and whether
#:  another is asked for (a name given while it runs)
_later, _later_lock = {'thread': None, 'again': False}, threading.Lock()


def later():
    """recognise every face again in the background, then bring the new
    proposals to the index; asked several times while it runs, it runs once
    more after"""
    with _later_lock:
        _later['again'] = True
        if _later['thread'] is None:
            _later['thread'] = threading.Thread(target=_later_job, daemon=True)
            _later['thread'].start()


def busy():
    """whether later() is at work"""
    return _later['thread'] is not None


def _later_job():
    while True:
        with _later_lock:
            if not _later['again']:
                _later['thread'] = None
                return
            _later['again'] = False
        db = photoindex.connect()
        try:
            sync(db, recognise(db))
        except Exception:
            pass            # (the next name given asks again)
        finally:
            db.close()


def board(db, count=BOARD[0], person=''):
    """the proposed faces to confirm in one go, as the page shows them, one
    to a sequence: first those of the people and the years that have the
    fewest confirmed faces (where a confirmation helps most), the most alike
    first among them, BOARD[1] at most for a person in a year. person: only
    this one. Also how many names wait, in all and for that person."""
    rows = db.execute(
        'SELECT f.id AS face, f.person AS who, p.name AS person, f.likeness, f.photo, f.taken, ph.seq '
        'FROM faces f JOIN people p ON p.id = f.person LEFT JOIN photos ph ON ph.name = f.photo '
        # (a face already answered does not come back, under another name either)
        "WHERE f.sure != 1 AND COALESCE(f.banned, '') = ''").fetchall()
    known = collections.Counter()
    for row in db.execute('SELECT person, taken FROM faces WHERE sure = 1 AND person IS NOT NULL'):
        known[row['person'], (row['taken'] or '')[:4]] += 1

    def near(row):
        """the confirmed faces of that person in that year and the years beside it"""
        year = (row['taken'] or '')[:4]
        if not year.isdigit():
            return 99
        return sum(known[row['who'], str(int(year) + by)] for by in (-1, 0, 1))

    waiting = [row for row in rows if not person or row['person'] == person]
    # (one card for a person in a day: an answer goes for the rest of the moment)
    cards, seqs, years = [], set(), collections.Counter()
    for row in sorted(waiting, key=lambda row: (min(near(row), 6), -(row['likeness'] or 0))):
        seq, year = row['seq'] or row['photo'], (row['who'], (row['taken'] or '')[:4])
        day = (row['who'], (row['taken'] or '')[:10] or row['photo'])
        if seq not in seqs and day not in seqs and years[year] < BOARD[1]:
            seqs.update((seq, day))
            years[year] += 1
            cards.append({'face': row['face'], 'person': row['person'], 'likeness': row['likeness'],
                          'photo': row['photo'], 'year': year[1], 'near': near(row)})
            if len(cards) >= count:
                break
    said = db.execute('SELECT COUNT(*), SUM(proposed = given) FROM face_answers '
                      'WHERE proposed IS NOT NULL').fetchone()
    names = collections.Counter(row['person'] for row in rows)
    return {'cards': cards, 'waiting': len(rows), 'names': names.most_common(), 'answered': said[0],
            'right': said[1] or 0}


def _rivals(db, faces):
    """the likeness of the second name of proposed faces, {face id: the best
    likeness with an example of somebody else, under the rules of recognise}"""
    import numpy as np
    rows = db.execute('SELECT id, photo, vector, taken, person, sure, likeness FROM faces '
                      'WHERE person IS NOT NULL AND (sure = 1 OR likeness >= ?)', (CHAIN,)).fetchall()
    born = {row['id']: _year(f"{row['born']}-07-01") if row['born'] else None
            for row in db.execute('SELECT id, born FROM people')}
    vectors = np.stack([np.frombuffer(row['vector'], dtype='float32') for row in rows])
    years = [_year(row['taken']) for row in rows]
    place = {row['id']: k for k, row in enumerate(rows)}
    out = {}
    for start in range(0, len(faces), 500):
        part = [face for face in faces[start:start + 500] if face in place]
        if not part:
            continue
        alike = vectors[[place[face] for face in part]] @ vectors.T
        for a, face in enumerate(part):
            k = place[face]
            out[face] = 0.0
            for e in np.argsort(-alike[a]):
                likeness, other = float(alike[a, e]), rows[int(e)]['person']
                if likeness < GUESS:
                    break
                if other == rows[k]['person'] or rows[int(e)]['photo'] == rows[k]['photo']:
                    continue
                if years[k] is not None and years[e] is not None:
                    begin = born.get(other)
                    if begin is not None and years[k] < begin - 0.3:
                        continue
                    age = None if begin is None else (years[k] + years[e]) / 2 - begin
                    if abs(years[k] - years[e]) > reach(age):
                        continue
                out[face] = likeness
                break
    return out


def draw_check(db, least=CHECK[0], gap=CHECK[1], count=CHECK[2]):
    """draw at random, once, the proposed faces the photographer will check:
    those that reach this likeness and this gap to the second name, one to a
    sequence (the frames of a burst would say the same thing); how many
    faces are drawn in all. The answers say how far such names can be trusted."""
    import random
    drawn = db.execute('SELECT COUNT(*) FROM face_check').fetchone()[0]
    if drawn:
        return drawn
    rows = db.execute('SELECT f.id, f.likeness, p.name, ph.seq, f.photo FROM faces f '
                      'JOIN people p ON p.id = f.person LEFT JOIN photos ph ON ph.name = f.photo '
                      'WHERE f.sure != 1 AND f.likeness >= ?', (least,)).fetchall()
    rivals = _rivals(db, [row['id'] for row in rows])
    rows = [row for row in rows if row['likeness'] - rivals.get(row['id'], 0.0) >= gap]
    random.shuffle(rows)
    when, seen, kept = datetime.now().isoformat(' ', 'seconds'), set(), []
    for row in rows:
        seq = row['seq'] or row['photo']
        if seq not in seen and len(kept) < count:
            seen.add(seq)
            kept.append((row['id'], when, row['name'], row['likeness'],
                         row['likeness'] - rivals.get(row['id'], 0.0)))
    db.executemany('INSERT INTO face_check (face, drawn, person, likeness, gap) VALUES (?, ?, ?, ?, ?)',
                   kept)
    db.commit()
    return len(kept)


def highest_error(wrong, count, level=0.95):
    """the highest share of wrong names these answers leave plausible (the
    one-sided bound of Clopper and Pearson); 1 without an answer"""
    from math import comb
    if not count:
        return 1.0

    def below(p):
        return sum(comb(count, k) * p ** k * (1 - p) ** (count - k) for k in range(wrong + 1))

    low, high = wrong / count, 1.0
    for _ in range(50):
        middle = (low + high) / 2
        low, high = (middle, high) if below(middle) > 1 - level else (low, middle)
    return high


def check_status(db):
    """the faces to check and what the answers say, for the page"""
    cards = [dict(row) for row in db.execute(
        'SELECT c.face, c.person, c.likeness, c.gap, c.answer, f.photo, f.x, f.y, f.w, f.h '
        'FROM face_check c JOIN faces f ON f.id = c.face ORDER BY c.answer IS NOT NULL, c.face')]
    said = [card for card in cards if card['answer'] is not None]
    wrong = sum(1 for card in said if card['answer'] != card['person'])
    return {'cards': cards, 'answered': len(said), 'wrong': wrong, 'least': CHECK[0], 'gap': CHECK[1],
            'highest': highest_error(wrong, len(said)),
            'kept': db.execute('SELECT COUNT(*) FROM face_answers').fetchone()[0]}


def early(name, img, taken):
    """the names of the people of a picture that is not in the index yet (a
    photo patchdoc is describing, under the name it has for now): its faces
    are kept and compared; (given, proposed)"""
    with _early:
        db = photoindex.connect()
        if db.execute('SELECT 1 FROM faces WHERE photo = ? LIMIT 1', (name,)).fetchone() is None:
            store(db, name, look(img), taken)
            recognise(db, {name})
            later()
        rows = db.execute('SELECT p.name, f.sure FROM faces f JOIN people p ON p.id = f.person '
                          'WHERE f.photo = ? ORDER BY f.x', (name,)).fetchall()
        db.close()
    return (photoindex.SEP.join(r['name'] for r in rows if r['sure'] == 1),
            photoindex.SEP.join(r['name'] for r in rows if r['sure'] != 1))


def renamed(db, names):
    """the faces kept under the names photos had before they were renamed
    ({name before: name now}) follow them, and those of photos that never
    reached the index are dropped"""
    for before, now in names.items():
        db.execute('UPDATE faces SET photo = ? WHERE photo = ?', (now, before))
        count = db.execute('SELECT COUNT(*) FROM faces WHERE photo = ?', (now,)).fetchone()[0]
        db.execute('UPDATE photos SET faces = ? WHERE name = ?', (count, now))
    db.execute('DELETE FROM faces WHERE sure != 1 AND photo NOT IN (SELECT name FROM photos)')
    db.commit()
    sync(db, list(names.values()))


def of_photo(db, name):
    """the faces of a photo, from left to right, as the page gets them: a
    proposed name is 'clear' when its likeness reaches CLEAR"""
    return [dict(row, clear=row['sure'] != 1 and (row['likeness'] or 0) >= CLEAR)
            for row in db.execute(
                'SELECT f.id, f.x, f.y, f.w, f.h, f.sure, f.likeness, p.name AS person, p.born '
                'FROM faces f LEFT JOIN people p ON p.id = f.person WHERE f.photo = ? ORDER BY f.x',
                (name,))]


def of_sequence(db, name):
    """the people of the sequence of a photo, as the page shows them: one
    entry a person, {'person', 'sure': a face of it named or confirmed by
    the photographer, 'likeness': the best of its proposed faces (None when
    sure), 'clear': that likeness reaches CLEAR, 'frame': the photo of that
    face}, the sure first, then the most alike"""
    row = db.execute('SELECT seq FROM photos WHERE name = ?', (name,)).fetchone()
    frames = [r['name'] for r in db.execute('SELECT name FROM photos WHERE seq = ?',
                                            (row['seq'],))] if row and row['seq'] else [name]
    marks = ', '.join('?' * len(frames))
    found = {}
    for face in db.execute(f'SELECT p.name AS person, f.sure, f.likeness, f.photo FROM faces f '
                           f'JOIN people p ON p.id = f.person WHERE f.photo IN ({marks})', frames):
        sure, likeness = face['sure'] == 1, face['likeness'] or 0.0
        best = found.get(face['person'])
        if best is None or (sure, likeness) > (best['sure'], best['likeness'] or 0.0):
            found[face['person']] = {'person': face['person'], 'sure': sure,
                                     'likeness': None if sure else round(likeness, 3),
                                     'clear': not sure and likeness >= CLEAR, 'frame': face['photo']}
    return sorted(found.values(), key=lambda who: (not who['sure'], -(who['likeness'] or 0.0)))


def scan(db, name, img, taken):
    """look for the faces of a photo that was never looked at, keep them and
    propose their names; the names for Claude: (given, proposed)"""
    if db.execute('SELECT faces FROM photos WHERE name = ?', (name,)).fetchone()['faces'] is None:
        store(db, name, look(img), taken)
        sync(db, recognise(db, {name}) | {name})
        later()
    row = db.execute('SELECT people, guessed FROM photos WHERE name = ?', (name,)).fetchone()
    return row['people'] or '', row['guessed'] or ''
