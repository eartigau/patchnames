"""
patchdoc: patchnames, with every photo described on the way.

In the directory of the new photos, `patchdoc`:

1. groups the photos into sequences (a new one after `gap` seconds without a
   shot), the bird photos, taken beyond config 'bird_focal_min' (200 mm),
   apart from the others;
2. finds the eBird checklist each sequence was taken during (ebird.py): its
   place, and the species noted there, a strong prior on what the photo
   shows. The clock of the camera is corrected first if the checklists say
   it is off by whole hours (daylight saving, another time zone); a new
   eBird export is asked for when the photos are newer than the one on disk;
3. shows each sequence to Claude (the claude command line, in print mode and
   allowed to read files only): the middle frame, a crop at full resolution
   around the focus point and a contact sheet, with the checklist. Claude
   answers with a title, the main subject, a description, keywords, and for
   a bird the species, how sure it is, and what does not fit the checklist
   (the photographer may have been wrong, or have left a bird out);
4. writes the answer into every frame of the sequence with exiftool (title,
   description, keywords, a Lightroom hierarchy, the place, and the eBird
   checklist as the event), like `patchnames keywords`;
5. renames and archives the files like `patchnames`;
6. adds them to the index of the search page (photoindex.py, `patchdoc gui`).

A sequence Claude could not describe stays in the directory, to run
`patchdoc` again. --dry-run shows the answers and changes nothing;
--index-only describes and indexes without touching the files (to try it in
a folder of the archive).

`patchdoc index` adds the photos already in the archive to the index, from
their names alone (date, time, bird lens or not) and the eBird checklists:
enough to search them by place and date, and by the species of their
checklist. `patchdoc index --exif DIR` reads what the files of a folder say
of themselves (slow on a network drive).
"""

import argparse
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta

from . import config as _config_module
from . import ebird as _ebird
from . import photoindex
from .keywords import (IMAGE_PATTERNS, UNSURE, _groups, _image, _pictures, _tags, _when,
                       instructions_path, write_template)

#: the main subject of a photo, one of
TOPICS = ('oiseau', 'autre animal', 'plante', 'paysage', 'ciel', 'ville et architecture',
          'personnes', 'objet', 'document', 'autre')
#: keyword of a photo whose species is not on its eBird checklist
OFFLIST = 'absent de la liste eBird'
#: a position within this many km of an eBird place takes its name
GPS_KM = 2.0

#: what exiftool is asked of each photo
FIELDS = ['-DateTimeOriginal', '-OffsetTimeOriginal', '-Model', '-LensModel', '-LensID',
          '-LensInfo', '-Lens', '-FocalLength#', '-GPSLatitude#', '-GPSLongitude#', '-AFAreaXPosition',
          '-AFAreaYPosition', '-AFImageWidth', '-AFImageHeight', '-XMP-dc:Title',
          '-XMP-dc:Description', '-XMP-dc:Subject', '-XMP-lr:HierarchicalSubject',
          '-XMP-iptcExt:Event']

#: a canonical name: YYMMDD_HHMMSS[_WHO]_NUM.ext
NAME = re.compile(r'^(\d{6})_(\d{6})(?:_([A-Z]{2}))?_[^.]+\.(\w+)$')

SYSTEM = ("Tu décris des photos pour l'archive d'un photographe. Tu lis les images indiquées "
          "avec l'outil Read, puis tu réponds uniquement par l'objet JSON demandé.")

PROMPT = """Tu décris une séquence de photos pour l'archive d'un photographe : un titre, le sujet, une description, des mots-clés, et l'espèce quand il s'agit d'un oiseau.

Lis ces images avec l'outil Read :
{images}

{views}

La séquence : {count} vues, du {start} au {end} à l'horloge de l'appareil{gear}.
{kind}

{ebird}

Suis ces explications du photographe (elles parlent surtout des oiseaux ; pour un autre sujet, gardes-en l'esprit : noms français, phrases courtes) :

---
{instructions}
---

Réponds par un objet JSON :
- "titre" : pour un oiseau, « nom français (nom latin) » ; sinon un titre court, 8 mots au plus ;
- "sujet" : le sujet principal, un de : {topics} ;
- "especes" : les espèces identifiées, [{{"francais": "...", "latin": "..."}}] ; [] s'il n'y en a pas ;
- "habitat" : un à trois mots pour le milieu ; [] si cela n'a pas de sens ici ;
- "description" : une ou deux phrases courtes en français, 40 mots au plus ;
- "certitude" : celle de l'identification, "sûre", "probable" ou "à confirmer" ("sûre" s'il n'y a rien à identifier) ;
- "autres_mots_cles" : au plus cinq mots-clés utiles pour retrouver la photo ;
- "liste" : l'identifiant (S...) de la liste eBird à laquelle la séquence appartient, null s'il n'y en a pas ;
- "note_ebird" : une phrase si ce que tu vois ne s'accorde pas avec la liste eBird (espèce absente de la liste, autre espèce plus probable que celle notée), sinon ""."""

VIEWS = ("La première est la vue du milieu de la séquence, réduite ; la deuxième est un "
         "agrandissement de cette vue à pleine résolution autour du point de mise au point de "
         "l'appareil (regarde-la attentivement : un sujet lointain n'est souvent visible que là)")
SHEETS = (" ; les suivantes sont des planches contact de toutes les vues de la séquence (le "
          "numéro de chaque vue est écrit dessus), pour vérifier qu'il s'agit du même sujet")

BIRD = ("Elle a été prise au téléobjectif ({focal} mm) : le sujet est presque toujours un "
        "oiseau, parfois un autre animal.")
OTHER = ("Elle n'a pas été prise au téléobjectif{focal} : paysage, lieu, personnes, objet... "
         "Décris simplement ce qu'elle montre.")

HOW = """Comment te servir des listes eBird. Le photographe remplit presque toujours une liste eBird pendant qu'il photographie : c'est un a priori fort sur l'espèce, pas une certitude (il a pu se tromper d'espèce, photographier un oiseau qu'il n'a pas noté, et l'horloge de l'appareil peut être décalée).
- Regarde d'abord la photo ; la liste sert à départager des espèces semblables et à écarter une espèce improbable à cet endroit.
- Un oiseau qui correspond à une espèce de la liste est très probablement cette espèce.
- Si ce que tu vois n'est clairement pas sur la liste, nomme quand même ce que tu vois, baisse la certitude s'il le faut et dis-le en une phrase dans "note_ebird".
- "liste" : en général la première liste donnée ; une autre du même jour si ses espèces correspondent mieux à la photo."""

SCHEMA = {
    'type': 'object',
    'properties': {
        'titre': {'type': 'string'},
        'sujet': {'type': 'string', 'enum': list(TOPICS)},
        'especes': {'type': 'array', 'items': {
            'type': 'object',
            'properties': {'francais': {'type': 'string'}, 'latin': {'type': 'string'}},
            'required': ['francais', 'latin'], 'additionalProperties': False}},
        'habitat': {'type': 'array', 'items': {'type': 'string'}},
        'description': {'type': 'string'},
        'certitude': {'type': 'string', 'enum': ['sûre', 'probable', 'à confirmer']},
        'autres_mots_cles': {'type': 'array', 'items': {'type': 'string'}},
        'liste': {'type': ['string', 'null']},
        'note_ebird': {'type': 'string'},
    },
    'required': ['titre', 'sujet', 'especes', 'habitat', 'description', 'certitude',
                 'autres_mots_cles', 'liste', 'note_ebird'],
    'additionalProperties': False,
}


def _cfg():
    return _config_module.load()


# ---------------------------------------------------------------------------
# The photos: what they say of themselves, their sequences, their checklists
# ---------------------------------------------------------------------------

def read_exif(paths, fast=False):
    """{path: tags} of files (one exiftool call); fast: without the maker
    notes (no focus point), for a network drive"""
    if not paths:
        return {}
    cmd = ['exiftool', '-j', '-q'] + (['-fast2'] if fast else []) + FIELDS
    out = subprocess.run(cmd + list(paths), capture_output=True, text=True).stdout
    rows = json.loads(out) if out.strip() else []
    return {row['SourceFile']: row for row in rows}


def _strings(value):
    """a list tag of exiftool as a list of strings"""
    if value in (None, ''):
        return []
    return [str(item) for item in (value if isinstance(value, list) else [value])]


def _focal(row):
    try:
        return float(row.get('FocalLength') or 0)
    except (TypeError, ValueError):
        return 0.0


def _lens(row):
    return str(row.get('LensModel') or row.get('LensID') or row.get('Lens') or '')


def _zone(row):
    """the time zone the camera says it is set to, hours from UTC ('+02:00'
    -> 2.0); None if the photo does not say"""
    found = re.fullmatch(r'([+-])(\d\d):(\d\d)',
                         str(row.get('OffsetTimeOriginal') or '').strip())
    if not found:
        return None
    hours = int(found.group(2)) + int(found.group(3)) / 60
    return hours if found.group(1) == '+' else -hours


def sequences(files, rows, gap, is_bird):
    """the sequences of photos, camera by camera, the bird photos apart from
    the others: [{'names', 'camera', 'bird', 'when'}] in the order of time;
    'when' is the camera time of the middle frame"""
    sets = {}
    for name in files:
        row = rows.get(name, {})
        sets.setdefault((str(row.get('Model') or ''), bool(is_bird(row))), []).append(name)
    out = []
    for (camera, bird), names in sets.items():
        for group in _groups(names, rows, gap):
            out.append({'names': group, 'camera': camera, 'bird': bird,
                        'when': _when(rows.get(group[len(group) // 2], {}))})
    out.sort(key=lambda seq: (seq['when'] or datetime.max, seq['names'][0]))
    return out


def locate(seqs, data, rows=None, clock=None):
    """give each sequence its clock offset and its eBird match: the offsets
    come from the bird sequences of each camera and apply to all it took
    (clock: the offset in hours, when the photographer knows it)"""
    rows = rows or {}
    moments = {}
    for seq in seqs:
        if seq['bird'] and seq['when']:
            zone = _zone(rows.get(seq['names'][len(seq['names']) // 2], {}))
            moments.setdefault(seq['camera'], []).append((seq['when'], zone))
    offsets = {}
    if data and clock is None:
        offsets = {camera: data.offsets(*zip(*pairs)) for camera, pairs in moments.items()}
    for seq in seqs:
        seq['offset'], seq['match'], seq['gps'] = float(clock or 0.0), None, None
        if data is None:
            continue
        if seq['when']:
            if clock is None:
                seq['offset'] = offsets.get(seq['camera'], {}).get(seq['when'].date(), 0.0)
            seq['match'] = data.match(seq['when'], seq['offset'])
        row = rows.get(seq['names'][len(seq['names']) // 2], {})
        if seq['match'] and seq['match'].checklist:
            continue
        # no checklist, but maybe a position: the eBird place next to it names it
        try:
            seq['gps'] = data.nearest(float(row['GPSLatitude']), float(row['GPSLongitude']), GPS_KM)
        except (KeyError, TypeError, ValueError):
            pass


def _place(checklist):
    return {'location': checklist.location, 'county': checklist.county, 'state': checklist.state,
            'country': checklist.country, 'lat': checklist.lat, 'lon': checklist.lon}


def _binomial(latin):
    """'Junco hyemalis hyemalis/carolinensis' -> 'junco hyemalis'; None for a
    name that is not one species (sp., a slash, a hybrid)"""
    words = latin.lower().replace('(', ' ').split()
    if len(words) < 2 or 'sp.' in words or 'x' in words or '/' in ''.join(words[:2]):
        return None
    return ' '.join(words[:2])


def _on_list(latins, checklist):
    """1 when every species named is on the checklist, 0 when one is not,
    None when there is nothing to compare"""
    named = [name for name in map(_binomial, latins) if name]
    if not checklist or not named:
        return None
    listed = {_binomial(latin) for latin in checklist.latin_names()}
    return int(all(name in listed for name in named))


def settle(seq, answer, data):
    """what is kept of a sequence, from Claude's answer (None for a sequence
    it did not describe) and the eBird match: the columns its frames share,
    and its checklist (None without one)"""
    match = seq.get('match')
    checklist = match.checklist if match else None
    kind = match.kind if match else None
    if answer and answer.get('liste') and match and data:
        # Claude may prefer another checklist of the day
        named = data.lists.get(str(answer['liste']).strip())
        if named is not None and named in match.others:
            checklist, kind = named, 'other'
    sure = {'location': '', 'county': '', 'state': '', 'country': '', 'lat': None, 'lon': None,
            'checklist': checklist.sid if checklist else None, 'match': kind,
            'clock_offset': seq.get('offset') or 0.0, 'bird': int(seq['bird'])}
    if checklist:
        sure.update(_place(checklist))
    elif seq.get('gps'):
        sure.update(_place(seq['gps'][0]), match='gps')
    if not answer:
        return sure, checklist
    species = answer.get('especes') or []
    sure.update({
        'title': answer['titre'], 'topic': answer.get('sujet') or '',
        'description': answer['description'],
        'species': photoindex.SEP.join(sp.get('francais', '') for sp in species),
        'latin': photoindex.SEP.join(sp.get('latin', '') for sp in species),
        'habitat': photoindex.SEP.join(answer.get('habitat') or []),
        'keywords': photoindex.SEP.join(answer.get('autres_mots_cles') or []),
        'certainty': answer.get('certitude') or '',
        'note': (answer.get('note_ebird') or '').strip(),
        'on_checklist': _on_list([sp.get('latin', '') for sp in species], checklist),
        'described': datetime.now().isoformat(' ', 'seconds'),
    })
    return sure, checklist


def file_tags(answer, kept, checklist):
    """the exiftool arguments of a sequence: Claude's answer (as `patchnames
    keywords` writes it), the place, and the eBird checklist as the event"""
    answer = dict(answer)
    if kept['note']:
        answer['description'] = f"{answer['description']} [eBird : {kept['note']}]"
    if kept['on_checklist'] == 0:
        answer['autres_mots_cles'] = list(answer.get('autres_mots_cles') or []) + [OFFLIST]
    args = _tags(answer)
    if checklist:
        country = _ebird.country_name(checklist.country)
        args += [f'-XMP-iptcExt:Event=eBird {checklist.sid}',
                 f'-XMP-iptcCore:Location={checklist.location}',
                 f'-IPTC:Sub-location={checklist.location[:32]}',
                 f'-XMP-photoshop:City={checklist.county}', f'-IPTC:City={checklist.county[:32]}',
                 f'-XMP-photoshop:State={checklist.state}',
                 f'-IPTC:Province-State={checklist.state[:32]}',
                 f'-XMP-photoshop:Country={country}',
                 f'-IPTC:Country-PrimaryLocationName={country[:64]}',
                 f'-XMP-iptcCore:CountryCode={checklist.country}']
    return args


def from_file(row):
    """what a photo says of itself, as columns: the title, description and
    keywords written by patchdoc or by `patchnames keywords`"""
    if not row.get('Title'):
        return {}
    subject = _strings(row.get('Subject'))
    hier = _strings(row.get('HierarchicalSubject'))

    def under(head):
        return [item.split('|', 1)[1] for item in hier if item.startswith(head + '|')]

    species, latin, habitat = under('Espèce'), under('Espèce latine'), under('Habitat')
    topic = (under('Sujet') or [''])[0]
    known = set(species + latin + habitat + [topic, UNSURE, OFFLIST])
    description, _, note = str(row.get('Description') or '').partition(' [eBird : ')
    event = str(row.get('Event') or '')
    out = {'title': str(row['Title']), 'topic': topic or ('oiseau' if species else ''),
           'description': description, 'note': note.rstrip(']'),
           'species': photoindex.SEP.join(species), 'latin': photoindex.SEP.join(latin),
           'habitat': photoindex.SEP.join(habitat),
           'keywords': photoindex.SEP.join(word for word in subject if word not in known),
           'certainty': ('à confirmer' if UNSURE in subject else 'sûre') if species else '',
           'described': 'file'}
    if event.startswith('eBird S'):
        out['checklist'] = event.split()[1]
    if OFFLIST in subject:
        out['on_checklist'] = 0
    return out


def said_of(seq, rows, data):
    """the columns of a sequence nobody asked Claude about: its eBird match,
    and what its middle frame says of itself"""
    said = from_file(rows.get(seq['names'][len(seq['names']) // 2], {}))
    kept, checklist = settle(seq, None, data)
    kept.update(said)
    if said.get('checklist') and data and said['checklist'] in data.lists:
        # the checklist written in the file, rather than the one of its hours
        checklist = data.lists[said['checklist']]
        kept.update(_place(checklist))
    if said:
        kept['on_checklist'] = _on_list(kept['latin'].split(photoindex.SEP), checklist)
    return kept


def frame_records(seq, kept, rows, paths):
    """the records of the frames of a sequence for the index; paths: {name
    as found: path where the file now is}"""
    names = seq['names']
    final = [paths.get(name, os.path.abspath(name)) for name in names]
    first = os.path.splitext(os.path.basename(final[0]))[0]
    records = []
    for k, (name, path) in enumerate(zip(names, final)):
        row = rows.get(name, {})
        when = _when(row)
        base = os.path.basename(path)
        record = dict(kept, name=base, path=path, stem=os.path.splitext(base)[0],
                      ext=os.path.splitext(base)[1].lstrip('.').lower(),
                      taken=when.isoformat(' ') if when else None,
                      local=(when + timedelta(hours=kept['clock_offset'])).isoformat(' ')
                      if when else None,
                      camera=str(row.get('Model') or ''), lens=_lens(row),
                      focal=_focal(row) or None, seq=first, seq_n=len(names),
                      rep=int(k == len(names) // 2))
        records.append(record)
    return records


# ---------------------------------------------------------------------------
# Claude
# ---------------------------------------------------------------------------

def _list_text(checklist, full):
    """a checklist for the prompt: every species with its count (full), or
    the names only"""
    county = f'{checklist.county}, ' if checklist.county else ''
    when = f'{checklist.start:%Y-%m-%d %H:%M}' if checklist.start else f'{checklist.day}, sans heure'
    length = f', {checklist.duration} min' if checklist.duration else ''
    head = (f'{checklist.sid} : {checklist.location} ({county}{checklist.state}, '
            f'{_ebird.country_name(checklist.country)}), {when}{length}, '
            f'{len(checklist.species)} espèces')
    if not full:
        return head + ' : ' + ', '.join(f"{sp['francais']} ({sp['latin']})" for sp in checklist.species)
    lines = [head]
    for sp in checklist.species:
        details = f' ; note du photographe : « {sp["details"]} »' if sp['details'] else ''
        lines.append(f"  - {sp['francais']} ({sp['latin']}), {sp['count']}{details}")
    if checklist.comments:
        lines.append(f'  Commentaire de la liste : {checklist.comments}')
    return '\n'.join(lines)


def ebird_text(seq):
    """what the prompt says of the eBird checklists of a sequence"""
    match = seq.get('match')
    if match is None or not (match.checklist or match.others):
        return ("Le photographe n'a pas de liste eBird ce jour-là : identifie sans a priori "
                '("liste" : null).')
    lines = []
    if match.offset:
        lines.append(f"L'horloge de l'appareil semble décalée de {match.offset:+g} h : "
                     f"l'heure sur place serait {match.local:%Y-%m-%d %H:%M}.")
    if match.checklist:
        how = ('dont les heures contiennent la séquence' if match.kind == 'inside'
               else "la plus proche de la séquence (à moins d'une heure)")
        lines += [f'Liste eBird du photographe {how} :', _list_text(match.checklist, seq['bird'])]
    else:
        lines.append("Aucune liste eBird du photographe ne contient l'heure de la séquence.")
    if match.others:
        lines.append('Autres listes eBird du même jour (la séquence peut en venir si '
                     "l'horloge de l'appareil est décalée) :")
        lines += [_list_text(other, False) for other in match.others]
    if seq['bird']:
        lines += ['', HOW]
    else:
        lines.append('Ces listes disent surtout où la photo a été prise ; "liste" : celle '
                     "pendant laquelle elle a été prise, null si aucune.")
    return '\n'.join(lines)


def prompt_for(seq, images, rows, instructions):
    names = seq['names']
    times = [t for t in (_when(rows.get(name, {})) for name in names) if t]
    row = rows.get(names[len(names) // 2], {})
    focal = _focal(row)
    gear = ', '.join(part for part in (str(row.get('Model') or ''), _lens(row)) if part)
    if seq['bird']:
        kind = BIRD.format(focal=f'{focal:.0f}')
    else:
        kind = OTHER.format(focal=f' ({focal:.0f} mm)' if focal else '')
    return PROMPT.format(
        images='\n'.join(images), views=VIEWS + (SHEETS if len(images) > 2 else '') + '.',
        count=len(names), start=f'{min(times):%Y-%m-%d %H:%M:%S}' if times else '?',
        end=f'{max(times):%Y-%m-%d %H:%M:%S}' if times else '?',
        gear=f' ({gear})' if gear else '', kind=kind, ebird=ebird_text(seq),
        instructions=instructions, topics=', '.join(f'"{topic}"' for topic in TOPICS))


def ask_claude(prompt, tmpdir, model=None, tries=2):
    """Claude's answer to a prompt, as (dict, cost in dollars)"""
    cmd = ['claude', '-p', prompt, '--system-prompt', SYSTEM, '--tools', 'Read',
           '--allowedTools', 'Read', '--add-dir', tmpdir, '--output-format', 'json',
           '--json-schema', json.dumps(SCHEMA), '--no-session-persistence',
           '--strict-mcp-config', '--disable-slash-commands']
    if model:
        cmd += ['--model', model]
    error = None
    for attempt in range(tries):
        if attempt:
            time.sleep(10)
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=900, cwd=tmpdir)
            if res.returncode != 0:
                raise RuntimeError(f'claude failed: {(res.stderr or res.stdout).strip()[:300]}')
            out = json.loads(res.stdout)
            answer = out.get('structured_output')
            if not isinstance(answer, dict):
                text = out.get('result') or ''
                start, end = text.find('{'), text.rfind('}')
                if start < 0 or end < start:
                    raise RuntimeError(f'no JSON in the answer: {text[:300]}')
                answer = json.loads(text[start:end + 1])
            for key in ('titre', 'description'):
                if not answer.get(key):
                    raise RuntimeError(f'the answer has no {key}: {answer}')
            return answer, float(out.get('total_cost_usd') or 0.0)
        except (RuntimeError, ValueError, subprocess.TimeoutExpired) as exc:
            error = exc
    raise RuntimeError(str(error))


# ---------------------------------------------------------------------------
# patchdoc
# ---------------------------------------------------------------------------

def _photos():
    return sorted({f for pattern in IMAGE_PATTERNS
                   for f in glob.glob(pattern) + glob.glob(pattern.upper()) if os.path.isfile(f)})


def _ebird_data(newest, refresh, quiet_dry):
    """the eBird data, asked anew when the photos are newer than it (refresh:
    True to ask in any case, False never, None as the config says)"""
    data = _ebird.load(quiet=False)
    stale = data is None or (newest is not None and data.asof < newest)
    if refresh is None:
        refresh = stale and _cfg()['ebird_refresh']
    if refresh and quiet_dry:
        print('eBird: the data on disk is older than the photos (not refreshed in a dry run).')
    elif refresh:
        if stale and data is not None:
            print(f'eBird: the data on disk ({data.asof:%Y-%m-%d %H:%M}) is older than the photos.')
        if _ebird.refresh():
            data = _ebird.load(quiet=False)
    if data is None:
        print('No eBird data: the photos are described without it (see `patchdoc ebird`).')
    else:
        print(f'eBird data of {data.asof:%Y-%m-%d %H:%M}, {len(data.lists)} checklists.')
    return data


def _say(seq):
    """one line on where and when a sequence is"""
    match = seq.get('match')
    clock = f' (clock {seq["offset"]:+g} h)' if seq.get('offset') else ''
    if match and match.checklist:
        return f'{match.kind} eBird {match.checklist.sid}, {match.checklist.label()}{clock}'
    if seq.get('gps'):
        return f'{seq["gps"][1]:.1f} km from {seq["gps"][0].location}'
    return 'no eBird checklist'


def document(gap=None, force=False, dry_run=False, index_only=False, backup=True,
             instructions=None, model=None, jobs=None, min_focal=None, refresh=None,
             clock=None):
    """Describe, rename, archive and index the photos of the current directory
    (see the module)."""
    cfg = _cfg()
    gap = cfg['keywords_gap'] if gap is None else gap
    model = model or cfg.get('keywords_model')
    jobs = int(jobs or cfg['doc_jobs'])
    min_focal = cfg['bird_focal_min'] if min_focal is None else min_focal
    path = instructions or instructions_path()
    if not instructions:
        write_template(path)
    with open(os.path.expanduser(path)) as handle:
        explanations = handle.read()

    files = _photos()
    if not files and (dry_run or index_only):
        print('No photo in this directory.')
        return
    rows = {os.path.basename(src): row for src, row in read_exif(files).items()}
    todo = [f for f in files if force or not rows.get(f, {}).get('Title')]
    seqs = sequences(todo, rows, gap, lambda row: _focal(row) > min_focal)
    # the photos already described are indexed from what they say of themselves
    done = sequences([f for f in files if f not in todo], rows, gap,
                     lambda row: _focal(row) > min_focal)
    if done:
        print(f'{sum(len(s["names"]) for s in done)} photos already described '
              '(--force to describe them again).')
    times = [seq['when'] for seq in seqs + done if seq['when']]
    data = _ebird_data(max(times) if times else None, refresh, dry_run) if files else None
    locate(seqs + done, data, rows, clock)
    shifted = sorted({seq['offset'] for seq in seqs + done if seq['offset']})
    if shifted:
        print('Camera clock taken as off by ' + ', '.join(f'{offset:+g} h' for offset in shifted)
              + (' (--clock).' if clock is not None else
                 ', from the eBird checklists (--clock HOURS to set it yourself, 0 for none).'))

    birds = sum(1 for seq in seqs if seq['bird'])
    print(f'{len(todo)} photos to describe in {len(seqs)} sequences '
          f'({birds} beyond {min_focal:g} mm), {jobs} at a time.')

    failed, answers, cost = [], {}, 0.0
    with tempfile.TemporaryDirectory() as tmpdir:
        staged = {}     # name as found -> {'thumb': path, 'preview': path}

        def keep(name, img, big):
            if dry_run:
                return
            for kind, size in (('thumb', 360),) + ((('preview', 1600),) if big else ()):
                if kind in staged.setdefault(name, {}):
                    continue
                small = img.copy()
                small.thumbnail((size, size))
                out = os.path.join(tmpdir, f'{kind}_{name}.jpg')
                small.save(out, quality=82 if big else 78)
                staged[name][kind] = out

        def thumbs(names):
            """the thumbnails of the frames no contact sheet showed"""
            for name in names:
                if 'thumb' not in staged.get(name, {}):
                    try:
                        keep(name, _image(name, big=False), False)
                    except Exception:
                        pass

        def describe(index, seq):
            images = _pictures(seq['names'], tmpdir, index, rows, keep)
            answer, spent = ask_claude(prompt_for(seq, images, rows, explanations), tmpdir, model)
            kept, checklist = settle(seq, answer, data)
            if not (dry_run or index_only):
                cmd = ['exiftool', '-q'] + ([] if backup else ['-overwrite_original'])
                res = subprocess.run(cmd + file_tags(answer, kept, checklist) + seq['names'],
                                     capture_output=True, text=True)
                if res.returncode != 0:
                    raise RuntimeError(f'exiftool: {res.stderr.strip()[:300]}')
            if not dry_run:
                thumbs(seq['names'])
            return answer, kept, spent

        with ThreadPoolExecutor(max_workers=max(1, jobs)) as pool:
            futures = {pool.submit(describe, index, seq): index for index, seq in enumerate(seqs)}
            for count, future in enumerate(as_completed(futures), 1):
                index = futures[future]
                seq = seqs[index]
                names = seq['names']
                head = (f'\n[{count}/{len(seqs)}] {names[0]} .. {names[-1]}  ({len(names)} photos)'
                        f'\n    {_say(seq)}')
                try:
                    answer, kept, spent = future.result()
                except Exception as exc:
                    print(f'{head}\n    ERROR: {exc}')
                    failed.append(index)
                    continue
                answers[index] = kept
                cost += spent
                flags = [answer.get('certitude', '?')]
                if kept['on_checklist'] == 0:
                    flags.append('not on the checklist')
                elif kept['on_checklist']:
                    flags.append('on the checklist')
                print(f'{head}\n    {answer["titre"]}  [{", ".join(flags)}]'
                      f'\n    {answer["description"]}'
                      + (f'\n    eBird: {kept["note"]}' if kept['note'] else ''))

        if dry_run:
            print(f'\nDry run: nothing written. Claude: ${cost:.2f} at API prices.')
            return

        # the photos already described, from what they say of themselves
        described = list(answers.values())
        for seq in done:
            seqs.append(seq)
            answers[len(seqs) - 1] = said_of(seq, rows, data)
            middle = seq['names'][len(seq['names']) // 2]
            for name in seq['names']:
                try:
                    keep(name, _image(name, big=name == middle), name == middle)
                except Exception:
                    pass

        # rename and archive, the sequences that failed left where they are
        left = [name for index in failed for name in seqs[index]['names']]
        paths = {}
        if not index_only:
            from .core import patchnames
            paths = patchnames(skip=left)

        db = photoindex.connect()
        if data:
            photoindex.sync_ebird(db, data)
        records = []
        for index, kept in answers.items():
            records += frame_records(seqs[index], kept, rows, paths)
        photoindex.put(db, records)
        db.close()
        for index in answers:
            for name in seqs[index]['names']:
                final = os.path.basename(paths.get(name, name))
                for kind, target in (('thumb', photoindex.thumb_path(final)),
                                     ('preview', photoindex.preview_path(final))):
                    if kind in staged.get(name, {}):
                        os.makedirs(os.path.dirname(target), exist_ok=True)
                        shutil.move(staged[name][kind], target)

    print(f'\n{len(described)} sequences described, {len(records)} photos in the index '
          f'({photoindex.index_dir()}). Claude: ${cost:.2f} at API prices.')
    offlist = sum(1 for kept in described if kept['on_checklist'] == 0)
    unsure = sum(1 for kept in described if kept['species'] and kept['certainty'] != 'sûre')
    nolist = sum(1 for kept in described if kept['bird'] and not kept['checklist'])
    if offlist:
        print(f'  {offlist} with a species that is not on the eBird checklist.')
    if unsure:
        print(f'  {unsure} with an identification to confirm.')
    if nolist:
        print(f'  {nolist} bird sequences without an eBird checklist.')
    if left:
        print(f'  {len(left)} photos not described are left here: run patchdoc again.')
    print('`patchdoc gui` to search them.')


# ---------------------------------------------------------------------------
# patchdoc index: the photos already in the archive
# ---------------------------------------------------------------------------

def _archive_files(root):
    """the photos of the archive, {name: (path, bird)}: root/20YY_MM/oiseaux
    and /autres"""
    extensions = tuple('.' + pattern.rsplit('.', 1)[1] for pattern in IMAGE_PATTERNS)
    found = {}
    for month in sorted(os.listdir(root)):
        if not re.fullmatch(r'\d{4}_\d{2}', month):
            continue
        for kind in ('oiseaux', 'autres'):
            folder = os.path.join(root, month, kind)
            if not os.path.isdir(folder):
                continue
            for name in os.listdir(folder):
                if name.lower().endswith(extensions) and not name.startswith('.'):
                    found[name] = (os.path.join(folder, name), kind == 'oiseaux')
        print(f'  {month}: {len(found)} photos', end='\r', flush=True)
    print()
    return found


def index_archive(root=None, gap=None):
    """Add the photos of the archive to the index from their names alone:
    date and time, bird lens or not (the folder), and the eBird checklist
    they were taken during. Photos the index already has only get their
    path brought up to date."""
    cfg = _cfg()
    root = os.path.expanduser(root or cfg['photo_archive_root'])
    gap = cfg['keywords_gap'] if gap is None else gap
    if not os.path.isdir(root):
        print(f'{root} is not there (the archive drive?).')
        return
    print(f'Listing {root} ...')
    found = _archive_files(root)
    db = photoindex.connect()
    known = photoindex.known(db)
    moved = [{'name': name, 'path': path} for name, (path, _) in found.items()
             if name in known and known[name][0] != path]
    rows, new = {}, []
    for name, (path, bird) in found.items():
        parsed = NAME.match(name)
        if name in known or not parsed:
            continue
        date, clock, who, _ = parsed.groups()
        # the year of the folder: the archive files 79 under 2079
        year = os.path.basename(os.path.dirname(os.path.dirname(path)))[:4]
        try:
            when = datetime.strptime(year + date[2:] + clock, '%Y%m%d%H%M%S')
        except ValueError:
            continue
        rows[name] = {'DateTimeOriginal': when.strftime('%Y:%m:%d %H:%M:%S'), 'Model': who or '',
                      'bird': bird}
        new.append(name)
    print(f'{len(found)} photos in the archive: {len(new)} new to the index, '
          f'{len(moved)} that moved.')
    data = _ebird.load(quiet=False)
    if data is None:
        print('No eBird data: the photos are indexed without a place (see `patchdoc ebird`).')
    else:
        photoindex.sync_ebird(db, data)
    seqs = sequences(new, rows, gap, lambda row: row.get('bird'))
    locate(seqs, data)
    paths = {name: found[name][0] for name in new}
    records = list(moved)
    for seq in seqs:
        records += frame_records(seq, settle(seq, None, data)[0], rows, paths)
    for record in records:
        # the names say nothing of the camera
        record.pop('camera', None), record.pop('lens', None), record.pop('focal', None)
    photoindex.put(db, records)
    db.close()
    placed = sum(1 for seq in seqs if seq['match'] and seq['match'].checklist)
    print(f'{len(seqs)} sequences added, {placed} of them during an eBird checklist.')


def index_exif(folders, gap=None, force=False, chunk=200):
    """Bring to the index what the photos of folders say of themselves:
    camera, lens, focal length, and the title, description and keywords
    written by patchdoc or `patchnames keywords`. Slow on a network drive;
    the photos already read are skipped unless force."""
    cfg = _cfg()
    gap = cfg['keywords_gap'] if gap is None else gap
    extensions = tuple('.' + pattern.rsplit('.', 1)[1] for pattern in IMAGE_PATTERNS)
    data = _ebird.load(quiet=False)
    db = photoindex.connect()
    if data:
        photoindex.sync_ebird(db, data)
    read = {row['name'] for row in db.execute('SELECT name FROM photos WHERE camera IS NOT NULL')}
    for folder in folders:
        folder = os.path.abspath(os.path.expanduser(folder))
        files = sorted(os.path.join(base, name) for base, _, names in os.walk(folder)
                       for name in names if name.lower().endswith(extensions)
                       and not name.startswith('.'))
        files = [path for path in files if force or os.path.basename(path) not in read]
        print(f'{folder}: {len(files)} photos to read.')
        rows = {}
        for start in range(0, len(files), chunk):
            part = files[start:start + chunk]
            rows.update({os.path.basename(src): row
                         for src, row in read_exif(part, fast=True).items()})
            print(f'  {min(start + chunk, len(files))}/{len(files)}', end='\r', flush=True)
        if not rows:
            continue
        print()
        paths = {os.path.basename(path): path for path in files}
        seqs = sequences(sorted(rows), rows, gap,
                         lambda row: _focal(row) > cfg['bird_focal_min'])
        locate(seqs, data, rows)
        records = []
        for seq in seqs:
            records += frame_records(seq, said_of(seq, rows, data), rows, paths)
        photoindex.put(db, records)
        print(f'  {len(seqs)} sequences, '
              f'{sum(1 for record in records if record.get("title") and record["rep"])} described.')
    db.close()


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        prog='patchdoc',
        description='patchnames, with every photo described by Claude on the way: the birds '
                    '(photos taken beyond 200 mm) identified with the eBird checklists as a '
                    'prior, the files renamed, archived, and indexed for the search page.')
    parser.add_argument('--dry-run', action='store_true',
                        help="Show Claude's answers; write, move and index nothing.")
    parser.add_argument('--index-only', action='store_true',
                        help='Describe and index, but leave the files untouched where they are.')
    parser.add_argument('--force', action='store_true',
                        help='Describe again the photos that already have a title.')
    parser.add_argument('--gap', type=float, default=None,
                        help='Seconds without a shot that start a new sequence (config, 30).')
    parser.add_argument('--min-focal', type=float, default=None,
                        help='A photo taken beyond this focal length (mm) is a bird photo '
                             '(config bird_focal_min, 200).')
    parser.add_argument('--clock', type=float, default=None, metavar='HOURS',
                        help='Hours to add to the camera clock to get the time at the place '
                             '(6 for a camera left on Montreal time in Sweden). Found from the '
                             'eBird checklists when not given.')
    parser.add_argument('--jobs', type=int, default=None,
                        help='Sequences described at the same time (config doc_jobs, 3).')
    parser.add_argument('--no-backup', action='store_true',
                        help='Do not keep the originals as <file>_original.')
    parser.add_argument('--instructions', default=None,
                        help='The MD file of explanations (~/.config/patchnames/keywords.md).')
    parser.add_argument('--model', default=None, help='The Claude model (default of claude).')
    group = parser.add_mutually_exclusive_group()
    group.add_argument('--refresh', dest='refresh', action='store_true', default=None,
                       help='Ask eBird for a new export first, whatever the one on disk.')
    group.add_argument('--no-refresh', dest='refresh', action='store_false',
                       help='Use the eBird data on disk even if the photos are newer.')
    sub = parser.add_subparsers(dest='cmd')

    p_gui = sub.add_parser('gui', help='The search page, in the browser.')
    p_gui.add_argument('--port', type=int, default=None, help='Port (config gui_port, 8765).')
    p_gui.add_argument('--no-browser', action='store_true', help='Do not open the browser.')

    p_index = sub.add_parser(
        'index', help='Add the photos already in the archive to the index, from their names '
                      'and the eBird checklists.')
    p_index.add_argument('--exif', nargs='+', metavar='DIR', default=None,
                         help='Read instead what the photos of these folders say of themselves '
                              '(camera, title, description, keywords); slow on a network drive.')
    p_index.add_argument('--force', action='store_true',
                         help='With --exif: read again the photos already read.')

    p_ebird = sub.add_parser('ebird', help='The eBird data: what is on disk, a new export.')
    p_ebird.add_argument('--refresh', action='store_true',
                         help='Ask eBird for a new export and wait for its email.')
    p_ebird.add_argument('--login', action='store_true',
                         help='Keep the eBird and mailbox accounts in the macOS keychain.')

    args = parser.parse_args()
    if args.cmd == 'gui':
        from .gui import serve
        serve(port=args.port, browser=not args.no_browser)
    elif args.cmd == 'index':
        if args.exif:
            index_exif(args.exif, force=args.force)
        else:
            index_archive()
    elif args.cmd == 'ebird':
        if args.login:
            _ebird.login()
        if args.refresh:
            _ebird.refresh()
        _ebird.status()
    else:
        document(gap=args.gap, force=args.force, dry_run=args.dry_run,
                 index_only=args.index_only, backup=not args.no_backup,
                 instructions=args.instructions, model=args.model, jobs=args.jobs,
                 min_focal=args.min_focal, refresh=args.refresh, clock=args.clock)


if __name__ == '__main__':
    sys.exit(main())
