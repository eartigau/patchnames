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

Only the photos of the current directory are ever shown to Claude: one call
per sequence, about a tenth of a dollar at API prices with the default
model. A run says what it expects to spend before it starts and stops at
config 'doc_budget' (--budget), the sequences left staying in the directory
for the next run; --model and --effort pick a cheaper setting.

What the program says is in French, like the descriptions.

`patchdoc index` adds the photos already in the archive to the index, from
their names alone (date, time, bird lens or not) and the eBird checklists:
enough to search them by place and date, and by the species of their
checklist. `patchdoc index --exif DIR` reads what the files of a folder say
of themselves (slow on a network drive). `patchdoc vignettes` makes ahead of
time the thumbnails the search page shows, so that browsing does not wait
for the drive, or need it. None of the three calls Claude.
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
import threading
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
#: start of GPSAreaInformation in a file whose position is that of its eBird
#:  checklist, written here
GPS_MARK = 'eBird'

#: what exiftool is asked of each photo
FIELDS = ['-DateTimeOriginal', '-OffsetTimeOriginal', '-Model', '-LensModel', '-LensID',
          '-LensInfo', '-Lens', '-FocalLength#', '-GPSLatitude#', '-GPSLongitude#',
          '-GPSAreaInformation', '-AFAreaXPosition', '-AFAreaYPosition', '-AFImageWidth',
          '-AFImageHeight', '-XMP-dc:Title', '-XMP-dc:Description', '-XMP-dc:Subject',
          '-XMP-lr:HierarchicalSubject', '-XMP-iptcExt:Event']

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
- "note_ebird" : une phrase seulement si ce que tu vois ne s'accorde pas avec la liste eBird (espèce absente de la liste, autre espèce plus probable que celle notée) ; une chaîne vide "" quand la photo s'accorde avec la liste ou qu'il n'y a pas de liste."""

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
    the others: [{'names', 'camera', 'bird', 'when', 'zone'}] in the order of
    time; 'when' is the camera time of the middle frame, 'zone' the time zone
    the camera says it is set to"""
    sets = {}
    for name in files:
        row = rows.get(name, {})
        sets.setdefault((str(row.get('Model') or ''), bool(is_bird(row))), []).append(name)
    out = []
    for (camera, bird), names in sets.items():
        for group in _groups(names, rows, gap):
            middle = rows.get(group[len(group) // 2], {})
            out.append({'names': group, 'camera': camera, 'bird': bird,
                        'when': _when(middle), 'zone': _zone(middle)})
    out.sort(key=lambda seq: (seq['when'] or datetime.max, seq['names'][0]))
    return out


def locate(seqs, data, rows=None, clock=None, before=None):
    """give each sequence its clock offset and its eBird match: the offsets
    come from the bird sequences of each camera and apply to all it took.

    clock: the offset in hours, when the photographer knows it. before:
    {day: hours}, offsets found earlier with more days around, kept for a
    camera that does not record its time zone (a few days alone would not
    move its clock)."""
    rows = rows or {}
    moments, zoned = {}, set()
    for seq in seqs:
        if seq['bird'] and seq['when']:
            moments.setdefault(seq['camera'], []).append((seq['when'], seq.get('zone')))
            if seq.get('zone') is not None:
                zoned.add(seq['camera'])
    offsets = {}
    if data and clock is None:
        offsets = {camera: data.offsets(*zip(*pairs)) for camera, pairs in moments.items()}
    for seq in seqs:
        seq['offset'], seq['match'], seq['gps'] = float(clock or 0.0), None, None
        if data is None:
            continue
        if seq['when']:
            day = seq['when'].date()
            if clock is not None:
                pass
            elif before and day in before and seq['camera'] not in zoned:
                seq['offset'] = before[day]
            else:
                seq['offset'] = offsets.get(seq['camera'], {}).get(day, 0.0)
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
    # a photo taken near a list, not during it, is not held against the list
    on_list = _on_list([sp.get('latin', '') for sp in species],
                       checklist if kind in ('inside', 'other') else None)
    # a note is for a photo that does not fit its list: none when every
    #   species named is on it (a small model writes one anyway)
    note = '' if on_list == 1 else (answer.get('note_ebird') or '').strip()
    sure.update({
        'title': answer['titre'], 'topic': answer.get('sujet') or '',
        'description': answer['description'],
        'species': photoindex.SEP.join(sp.get('francais', '') for sp in species),
        'latin': photoindex.SEP.join(sp.get('latin', '') for sp in species),
        'habitat': photoindex.SEP.join(answer.get('habitat') or []),
        'keywords': photoindex.SEP.join(answer.get('autres_mots_cles') or []),
        'certainty': answer.get('certitude') or '',
        'note': note,
        'on_checklist': on_list,
        'described': datetime.now().isoformat(' ', 'seconds'),
    })
    return sure, checklist


def own_position(row):
    """whether a photo carries a position of its own (a phone, a camera with
    a GPS), which is then left alone; one written here from an eBird
    checklist says so in GPSAreaInformation and may be written again"""
    return (row.get('GPSLatitude') not in (None, '')
            and not str(row.get('GPSAreaInformation') or '').startswith(GPS_MARK))


def place_tags(checklist, position=True):
    """the exiftool arguments of the place of a checklist: its names, the
    checklist as the IPTC event, and (position) its latitude and longitude,
    marked as those of the eBird place, not of the photographer"""
    country = _ebird.country_name(checklist.country)
    args = ['-charset', 'iptc=UTF8', '-codedcharacterset=utf8',
            f'-XMP-iptcExt:Event=eBird {checklist.sid}',
            f'-XMP-iptcCore:Location={checklist.location}',
            f'-IPTC:Sub-location={checklist.location[:32]}',
            f'-XMP-photoshop:City={checklist.county}', f'-IPTC:City={checklist.county[:32]}',
            f'-XMP-photoshop:State={checklist.state}',
            f'-IPTC:Province-State={checklist.state[:32]}',
            f'-XMP-photoshop:Country={country}',
            f'-IPTC:Country-PrimaryLocationName={country[:64]}',
            f'-XMP-iptcCore:CountryCode={checklist.country}']
    if position and checklist.lat is not None and checklist.lon is not None:
        # a signed number in a ...Ref tag gives its N/S or E/W
        args += [f'-GPSLatitude={abs(checklist.lat)}', f'-GPSLatitudeRef={checklist.lat}',
                 f'-GPSLongitude={abs(checklist.lon)}', f'-GPSLongitudeRef={checklist.lon}',
                 '-GPSMapDatum=WGS-84', f'-GPSAreaInformation={GPS_MARK} : {checklist.location}']
    return args


def file_tags(answer, kept, checklist, position=True):
    """the exiftool arguments of a sequence: Claude's answer (as `patchnames
    keywords` writes it; None for a sequence it did not describe), the place,
    its latitude and longitude, and the eBird checklist as the event"""
    args = []
    if answer:
        answer = dict(answer)
        if kept['note']:
            answer['description'] = f"{answer['description']} [eBird : {kept['note']}]"
        if kept['on_checklist'] == 0:
            answer['autres_mots_cles'] = list(answer.get('autres_mots_cles') or []) + [OFFLIST]
        args += _tags(answer)
    if checklist:
        args += place_tags(checklist, position)
    return args


def answer_of(row):
    """what the index says of a sequence, in the shape of an answer of
    Claude, to write it into the files; None for one never described"""
    if not row['title']:
        return None

    def parts(key):
        return [part for part in (row[key] or '').split(photoindex.SEP) if part]

    species = [{'francais': fr, 'latin': la}
               for fr, la in zip(parts('species'), parts('latin') + [''] * len(parts('species')))]
    return {'titre': row['title'], 'sujet': row['topic'] or '', 'especes': species,
            'habitat': parts('habitat'), 'description': row['description'] or row['title'],
            'certitude': row['certainty'] or 'sûre', 'autres_mots_cles': parts('keywords')}


def write_files(args, names, rows, backup=True):
    """write tags into files with exiftool, the latitude and longitude left
    out for those that carry a position of their own; raises RuntimeError"""
    position = [arg for arg in args if arg.startswith('-GPS')]
    for group, extra in (([n for n in names if not own_position(rows.get(n, {}))], position),
                         ([n for n in names if own_position(rows.get(n, {}))], [])):
        if not group:
            continue
        cmd = ['exiftool', '-q', '-m'] + ([] if backup else ['-overwrite_original'])
        res = subprocess.run(cmd + [arg for arg in args if arg not in position] + extra + group,
                             capture_output=True, text=True)
        if res.returncode != 0:
            raise RuntimeError(f'exiftool : {res.stderr.strip()[:300]}')


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
           'described': 'file', 'written': 'file'}
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
    during = kept['match'] == 'inside'
    kept.update(said)
    if said.get('checklist') and data and said['checklist'] in data.lists:
        # the checklist written in the file, rather than the one of its hours
        checklist, during = data.lists[said['checklist']], True
        kept.update(_place(checklist))
    if said:
        kept['on_checklist'] = _on_list(kept['latin'].split(photoindex.SEP),
                                        checklist if during else None)
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
                      focal=_focal(row) or None, zone=_zone(row), seq=first, seq_n=len(names),
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


def ask_claude(prompt, tmpdir, model=None, effort=None, tries=2):
    """Claude's answer to a prompt, as (dict, cost in dollars at API prices)"""
    cmd = ['claude', '-p', prompt, '--system-prompt', SYSTEM, '--tools', 'Read',
           '--allowedTools', 'Read', '--add-dir', tmpdir, '--output-format', 'json',
           '--json-schema', json.dumps(SCHEMA), '--no-session-persistence',
           '--strict-mcp-config', '--disable-slash-commands']
    if model:
        cmd += ['--model', model]
    if effort:
        cmd += ['--effort', effort]
    error = None
    for attempt in range(tries):
        if attempt:
            time.sleep(10)
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=900, cwd=tmpdir)
            if res.returncode != 0:
                raise RuntimeError(f'claude a échoué : {(res.stderr or res.stdout).strip()[:300]}')
            out = json.loads(res.stdout)
            answer = out.get('structured_output')
            if not isinstance(answer, dict):
                text = out.get('result') or ''
                start, end = text.find('{'), text.rfind('}')
                if start < 0 or end < start:
                    raise RuntimeError(f'pas de JSON dans la réponse : {text[:300]}')
                answer = json.loads(text[start:end + 1])
            for key in ('titre', 'description'):
                if not answer.get(key):
                    raise RuntimeError(f"la réponse n'a pas de {key} : {answer}")
            return answer, float(out.get('total_cost_usd') or 0.0)
        except (RuntimeError, ValueError, subprocess.TimeoutExpired) as exc:
            error = exc
    raise RuntimeError(str(error))


# ---------------------------------------------------------------------------
# patchdoc
# ---------------------------------------------------------------------------

#: what a sequence costs when no run has measured it yet, dollars at API prices
COST_GUESS = 0.10


class _OverBudget(Exception):
    """a sequence left for another run: the budget of this one is spent"""


def _photos():
    return sorted({f for pattern in IMAGE_PATTERNS
                   for f in glob.glob(pattern) + glob.glob(pattern.upper()) if os.path.isfile(f)})


def _ebird_data(newest, refresh, dry_run):
    """the eBird data, asked anew when the photos are newer than it (refresh:
    True to ask in any case, False never, None as the config says)"""
    data = _ebird.load(quiet=False)
    stale = data is None or (newest is not None and data.asof < newest)
    if refresh is None:
        refresh = stale and _cfg()['ebird_refresh']
    if refresh and dry_run:
        print('eBird : les données sur disque sont plus anciennes que les photos '
              '(pas de mise à jour pendant un essai).')
    elif refresh:
        if stale and data is not None:
            print(f'eBird : les données sur disque ({data.asof:%Y-%m-%d %H:%M}) sont plus '
                  'anciennes que les photos.')
        if _ebird.refresh():
            data = _ebird.load(quiet=False)
    if data is None:
        print('Pas de données eBird : les photos sont décrites sans elles (voir `patchdoc ebird`).')
    else:
        print(f'Données eBird du {data.asof:%Y-%m-%d %H:%M}, {len(data.lists)} listes.')
    return data


def _say(seq):
    """one line on where and when a sequence is"""
    match = seq.get('match')
    clock = f' (horloge {seq["offset"]:+g} h)' if seq.get('offset') else ''
    if match and match.checklist:
        how = 'pendant' if match.kind == 'inside' else "à moins d'une heure de"
        return f'{how} la liste eBird {match.checklist.sid}, {match.checklist.label()}{clock}'
    if seq.get('gps'):
        return f'à {seq["gps"][1]:.1f} km de {seq["gps"][0].location}'
    return 'aucune liste eBird'


def _cost_key(model, effort):
    return f'cost_per_sequence:{model or "default"}:{effort or "default"}'


def _cost_guess(model, effort):
    """what a sequence costs in dollars at API prices, and whether a run
    with this model and effort measured it"""
    try:
        return float(photoindex.meta(_cost_key(model, effort))), True
    except (TypeError, ValueError):
        return COST_GUESS, False


def _count(number, word):
    """'1 photo', '6 photos'"""
    return f'{number} {word}{"s" if number > 1 else ""}'


def document(gap=None, force=False, dry_run=False, index_only=False, backup=True,
             instructions=None, model=None, effort=None, jobs=None, min_focal=None,
             refresh=None, clock=None, budget=None):
    """Describe, rename, archive and index the photos of the current directory
    (see the module)."""
    cfg = _cfg()
    gap = cfg['keywords_gap'] if gap is None else gap
    model = model or cfg.get('keywords_model')
    effort = effort or cfg.get('doc_effort')
    budget = float(cfg.get('doc_budget') or 0) if budget is None else budget
    jobs = int(jobs or cfg['doc_jobs'])
    min_focal = cfg['bird_focal_min'] if min_focal is None else min_focal
    path = instructions or instructions_path()
    if not instructions:
        write_template(path)
    with open(os.path.expanduser(path)) as handle:
        explanations = handle.read()

    files = _photos()
    if not files and (dry_run or index_only):
        print('Aucune photo dans ce dossier.')
        return
    rows = {os.path.basename(src): row for src, row in read_exif(files).items()}
    todo = [f for f in files if force or not rows.get(f, {}).get('Title')]
    seqs = sequences(todo, rows, gap, lambda row: _focal(row) > min_focal)
    # the photos already described are indexed from what they say of themselves
    done = sequences([f for f in files if f not in todo], rows, gap,
                     lambda row: _focal(row) > min_focal)
    if done:
        print(f'{sum(len(s["names"]) for s in done)} photos déjà décrites '
              '(--force pour les décrire de nouveau).')
    times = [seq['when'] for seq in seqs + done if seq['when']]
    data = _ebird_data(max(times) if times else None, refresh, dry_run) if files else None
    locate(seqs + done, data, rows, clock)
    shifted = sorted({seq['offset'] for seq in seqs + done if seq['offset']})
    if shifted:
        print("Horloge de l'appareil considérée comme décalée de "
              + ', '.join(f'{offset:+g} h' for offset in shifted)
              + (' (--clock).' if clock is not None else
                 ", d'après les listes eBird (--clock HEURES pour la fixer vous-même, "
                 '0 pour aucun décalage).'))

    birds = sum(1 for seq in seqs if seq['bird'])
    print(f'{_count(len(todo), "photo")} à décrire en {_count(len(seqs), "séquence")} '
          f'({birds} au-delà de {min_focal:g} mm), {jobs} à la fois.')
    if seqs:
        each, measured = _cost_guess(model, effort)
        print(f"Claude : environ {len(seqs) * each:.2f} $ US au prix de l'API ({each:.3f} $ par "
              + ('séquence au dernier passage)' if measured else 'séquence, estimation de départ)')
              + (f' ; arrêt à {budget:g} $ (--budget).' if budget and len(seqs) * each > budget
                 else '.'))

    failed, waiting, answers = [], [], {}
    spent, lock = {'total': 0.0}, threading.Lock()
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
            if budget and spent['total'] >= budget:
                raise _OverBudget()
            images = _pictures(seq['names'], tmpdir, index, rows, keep)
            answer, cost = ask_claude(prompt_for(seq, images, rows, explanations), tmpdir,
                                      model, effort)
            with lock:
                spent['total'] += cost
            kept, checklist = settle(seq, answer, data)
            if not (dry_run or index_only):
                write_files(file_tags(answer, kept, checklist), seq['names'], rows, backup)
                kept['written'] = kept['described']
            if not dry_run:
                thumbs(seq['names'])
            return answer, kept

        with ThreadPoolExecutor(max_workers=max(1, jobs)) as pool:
            futures = {pool.submit(describe, index, seq): index for index, seq in enumerate(seqs)}
            for count, future in enumerate(as_completed(futures), 1):
                index = futures[future]
                seq = seqs[index]
                names = seq['names']
                head = (f'\n[{count}/{len(seqs)}] {names[0]} .. {names[-1]}  '
                        f'({_count(len(names), "photo")})\n    {_say(seq)}')
                try:
                    answer, kept = future.result()
                except _OverBudget:
                    waiting.append(index)
                    continue
                except Exception as exc:
                    print(f'{head}\n    ERREUR : {exc}')
                    failed.append(index)
                    continue
                answers[index] = kept
                flags = [answer.get('certitude', '?')]
                if kept['on_checklist'] == 0:
                    flags.append('absente de la liste')
                elif kept['on_checklist']:
                    flags.append('sur la liste')
                print(f'{head}\n    {answer["titre"]}  [{", ".join(flags)}]'
                      f'\n    {answer["description"]}'
                      + (f'\n    eBird : {kept["note"]}' if kept['note'] else ''))

        cost = spent['total']
        if waiting:
            print(f'\nBudget de {budget:g} $ atteint : {_count(len(waiting), "séquence")} à décrire '
                  'au prochain passage (relancez patchdoc, ou --budget pour un autre plafond).')
        if dry_run:
            print(f"\nEssai : rien n'a été écrit. Claude : {cost:.2f} $ US au prix de l'API.")
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

        # rename and archive; the sequences not described are left where they are
        left = [name for index in failed + waiting for name in seqs[index]['names']]
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
        # a file whose name changed (one named after its content, described
        #   again) leaves the index under its old name
        photoindex.forget(db, [name for name, path in paths.items()
                               if os.path.basename(path) != name])
        photoindex.put(db, records)
        if described:
            photoindex.set_meta(db, _cost_key(model, effort), round(cost / len(described), 4))
        db.close()
        for index in answers:
            for name in seqs[index]['names']:
                final = os.path.basename(paths.get(name, name))
                for kind, target in (('thumb', photoindex.thumb_path(final)),
                                     ('preview', photoindex.preview_path(final))):
                    if kind in staged.get(name, {}):
                        os.makedirs(os.path.dirname(target), exist_ok=True)
                        shutil.move(staged[name][kind], target)

    print(f"\n{_count(len(described), 'séquence')} décrite{'s' if len(described) > 1 else ''}, "
          f"{_count(len(records), 'photo')} dans l'index ({photoindex.index_dir()}). "
          f"Claude : {cost:.2f} $ US au prix de l'API.")
    offlist = sum(1 for kept in described if kept['on_checklist'] == 0)
    unsure = sum(1 for kept in described if kept['species'] and kept['certainty'] != 'sûre')
    nolist = sum(1 for kept in described if kept['bird'] and not kept['checklist'])
    if offlist:
        print(f'  {offlist} avec une espèce absente de la liste eBird.')
    if unsure:
        print(f'  {unsure} avec une identification à confirmer.')
    if nolist:
        print(f"  {nolist} séquences d'oiseaux sans liste eBird.")
    if left:
        print(f'  {len(left)} photos non décrites sont laissées ici : relancez patchdoc.')
    print('`patchdoc gui` pour les chercher.')


# ---------------------------------------------------------------------------
# The photos already in the archive: patchdoc index, patchdoc vignettes
# ---------------------------------------------------------------------------

def _extensions():
    return tuple('.' + pattern.rsplit('.', 1)[1] for pattern in IMAGE_PATTERNS)


def _archive_files(root):
    """the photos of the archive, {name: (path, bird)}: root/20YY_MM/oiseaux
    and /autres"""
    found = {}
    for month in sorted(os.listdir(root)):
        if not re.fullmatch(r'\d{4}_\d{2}', month):
            continue
        for kind in ('oiseaux', 'autres'):
            folder = os.path.join(root, month, kind)
            if not os.path.isdir(folder):
                continue
            for name in os.listdir(folder):
                if name.lower().endswith(_extensions()) and not name.startswith('.'):
                    found[name] = (os.path.join(folder, name), kind == 'oiseaux')
        print(f'  {month} : {len(found)} photos', end='\r', flush=True)
    print()
    return found


def index_archive(root=None, gap=None):
    """Add the photos of the archive to the index from their names alone:
    date and time, bird lens or not (the folder), and the eBird checklist
    they were taken during. Photos the index already has only get their
    path brought up to date."""
    cfg = _cfg()
    root = os.path.expanduser(root) if root else _config_module.archive_root(cfg)
    gap = cfg['keywords_gap'] if gap is None else gap
    if not os.path.isdir(root):
        print(f"{root} est introuvable (le disque d'archive est-il monté ?).")
        return
    print(f'Lecture des dossiers de {root} ...')
    found = _archive_files(root)
    db = photoindex.connect()
    known = photoindex.known(db)
    moved = [{'name': name, 'path': path} for name, (path, _) in found.items()
             if name in known and known[name][0] != photoindex.shorten(path)]
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
    print(f"{len(found)} photos dans l'archive : {len(new)} nouvelles pour l'index, "
          f'{len(moved)} déplacées.')
    data = _ebird.load(quiet=False)
    if data is None:
        print('Pas de données eBird : les photos sont indexées sans lieu (voir `patchdoc ebird`).')
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
        for key in ('camera', 'lens', 'focal', 'zone'):
            record.pop(key, None)
    photoindex.put(db, records)
    db.close()
    placed = sum(1 for seq in seqs if seq['match'] and seq['match'].checklist)
    print(f'{len(seqs)} séquences ajoutées, dont {placed} pendant une liste eBird.')


def index_exif(folders, gap=None, force=False, chunk=200):
    """Bring to the index what the photos of folders say of themselves:
    camera, lens, focal length, and the title, description and keywords
    written by patchdoc or `patchnames keywords`. Slow on a network drive:
    the photos are read and kept day by day, so that a run that is stopped
    is not lost, and those already read are skipped unless force."""
    cfg = _cfg()
    gap = cfg['keywords_gap'] if gap is None else gap
    data = _ebird.load(quiet=False)
    db = photoindex.connect()
    if data:
        photoindex.sync_ebird(db, data)
    read = {row['name'] for row in db.execute('SELECT name FROM photos WHERE camera IS NOT NULL')}
    # the clock offset the index has for each day (the most common one last)
    before = {datetime.strptime(day, '%Y-%m-%d').date(): offset or 0.0
              for day, offset, _ in db.execute(
                  'SELECT substr(taken, 1, 10), clock_offset, COUNT(*) FROM photos '
                  'WHERE bird = 1 AND taken IS NOT NULL GROUP BY 1, 2 ORDER BY 3')}
    for folder in folders:
        folder = os.path.abspath(os.path.expanduser(folder))
        files = sorted(os.path.join(base, name) for base, _, names in os.walk(folder)
                       for name in names if name.lower().endswith(_extensions())
                       and not name.startswith('.'))
        files = [path for path in files if force or os.path.basename(path) not in read]
        # a day of a folder at a time: the date at the start of a canonical name
        days = {}
        for path in files:
            parsed = NAME.match(os.path.basename(path))
            days.setdefault((os.path.dirname(path), parsed.group(1) if parsed else ''),
                            []).append(path)
        print(f'{folder} : {len(files)} photos à lire.')
        count = 0
        for (base, day), part in days.items():
            rows = {}
            for start in range(0, len(part), chunk):
                rows.update({os.path.basename(src): row for src, row
                             in read_exif(part[start:start + chunk], fast=True).items()})
                print(f'  {count + min(start + chunk, len(part))}/{len(files)}',
                      end='\r', flush=True)
            count += len(part)
            if not rows:
                continue
            paths = {os.path.basename(path): path for path in part}
            seqs = sequences(sorted(rows), rows, gap,
                             lambda row: _focal(row) > cfg['bird_focal_min'])
            locate(seqs, data, rows, before=before)
            records = []
            for seq in seqs:
                records += frame_records(seq, said_of(seq, rows, data), rows, paths)
            photoindex.put(db, records)
            told = sum(1 for record in records if record.get('title') and record['rep'])
            print(f'  {os.path.relpath(base, folder)} {day} : {len(part)} photos, '
                  f'{len(seqs)} séquences, {told} décrites.')
    db.close()


def rematch():
    """Match every sequence of the index with the eBird checklists again, from
    the index alone: after a new export (a list entered after its photos were
    indexed) or a change in the matching. What Claude said is kept, and so is
    a list Claude chose itself or a place taken from a GPS position."""
    data = _ebird.load(quiet=False)
    if data is None:
        print('Pas de données eBird (voir `patchdoc ebird`).')
        return
    db = photoindex.connect()
    photoindex.sync_ebird(db, data)
    rows = db.execute('SELECT name, seq, taken, bird, camera, zone, latin, match, checklist, '
                      'clock_offset FROM photos WHERE rep = 1 AND taken IS NOT NULL').fetchall()
    seqs = []
    for row in rows:
        parsed = NAME.match(row['name'])
        seqs.append({'names': [row['name']], 'bird': bool(row['bird']), 'zone': row['zone'],
                     'camera': row['camera'] or (parsed.group(3) if parsed else '') or '',
                     'when': datetime.fromisoformat(row['taken']), 'row': row})
    locate(seqs, data)
    changed = []
    for seq in seqs:
        row = seq['row']
        kept, checklist = settle(seq, None, data)
        if row['match'] == 'other' or (row['match'] == 'gps' and not checklist):
            continue
        if (kept['checklist'], kept['match'], kept['clock_offset']) == (
                row['checklist'], row['match'], row['clock_offset'] or 0.0):
            continue
        on_list = _on_list((row['latin'] or '').split(photoindex.SEP),
                           checklist if kept['match'] == 'inside' else None)
        db.execute(
            'UPDATE photos SET checklist = ?, match = ?, clock_offset = ?, on_checklist = ?, '
            'location = ?, county = ?, state = ?, country = ?, lat = ?, lon = ?, place_norm = ?, '
            "local = strftime('%Y-%m-%d %H:%M:%S', taken, ?) WHERE seq = ?",
            (kept['checklist'], kept['match'], kept['clock_offset'], on_list, kept['location'],
             kept['county'], kept['state'], kept['country'], kept['lat'], kept['lon'],
             photoindex.place_text(kept), f"{kept['clock_offset']:+g} hours", row['seq']))
        changed.append(row['name'])
    photoindex._derive(db, changed)
    db.commit()
    db.close()
    print(f"Liste eBird ou horloge changée pour {_count(len(changed), 'séquence')} "
          f'sur {len(seqs)}.')


def _archive_pictures(frames, full, tmpdir, row, sheet):
    """the pictures of a sequence of the archive for Claude, as
    keywords._pictures makes them for local files: the middle frame, the crop
    at its focus point, and one contact sheet of `sheet` frames at most, from
    the thumbnails the index has, so that a long burst on a network drive is
    not read whole. The thumbnail and the preview of what is read are kept."""
    from PIL import Image, ImageDraw
    from .keywords import _focus_crop
    middle = frames[len(frames) // 2]
    img = _image(full, big=True)
    photoindex.save_images(img, middle['name'], preview=True)
    paths = [os.path.join(tmpdir, 'middle.jpg'), os.path.join(tmpdir, 'focus.jpg')]
    small = img.copy()
    small.thumbnail((1400, 1400))
    small.save(paths[0], quality=88)
    _focus_crop(img, row).save(paths[1], quality=88)
    if len(frames) < 2:
        return paths
    count = min(sheet, len(frames))
    pick = [frames[round(k * (len(frames) - 1) / max(1, count - 1))] for k in range(count)]
    page = Image.new('RGB', (1400, 155 * ((count + 5) // 6)), 'white')
    draw = ImageDraw.Draw(page)
    for k, frame in enumerate(pick):
        thumb = photoindex.thumb_path(frame['name'])
        try:
            if not os.path.isfile(thumb):
                photoindex.save_images(_image(photoindex.resolve(frame['path']), big=False),
                                       frame['name'])
            cell = Image.open(thumb).convert('RGB')
        except Exception:
            continue
        cell.thumbnail((231, 153))
        x, y = (k % 6) * 233, (k // 6) * 155
        page.paste(cell, (x, y))
        draw.text((x + 3, y + 2), os.path.splitext(frame['name'])[0].split('_')[-1], fill='yellow')
    paths.append(os.path.join(tmpdir, 'sheet.jpg'))
    page.save(paths[2], quality=85)
    return paths


def describe_sequence(seq_id, model=None, effort=None, sheet=12):
    """Have Claude describe one sequence of the index, where its files are
    (the button of the search page): its row in the index, as a dict, and the
    cost in dollars at API prices. The answer goes to the index, and waits
    there for write_sequence to put it into the files. Raises RuntimeError
    when the sequence cannot be read (the archive drive is not mounted)."""
    cfg = _cfg()
    path = write_template(instructions_path(), quiet=True)
    with open(os.path.expanduser(path)) as handle:
        explanations = handle.read()
    db = photoindex.connect()
    frames = photoindex.sequence(db, seq_id)
    if not frames:
        raise RuntimeError('séquence inconnue')
    names = [frame['name'] for frame in frames]
    middle = frames[len(frames) // 2]
    full = photoindex.resolve(middle['path'])
    if not full:
        raise RuntimeError("le fichier est hors d'atteinte (le disque d'archive est-il monté ?)")
    # the times of the index for every frame, the EXIF of the middle one only
    rows = {frame['name']: {'DateTimeOriginal': frame['taken'][:10].replace('-', ':')
                            + frame['taken'][10:19]} for frame in frames if frame['taken']}
    exif = read_exif([full]).get(full, {})
    rows[middle['name']] = dict(rows.get(middle['name'], {}), **exif)
    stored = db.execute('SELECT bird, clock_offset FROM photos WHERE name = ?',
                        (middle['name'],)).fetchone()
    bird = _focal(exif) > cfg['bird_focal_min'] if _focal(exif) else bool(stored['bird'])
    seq = {'names': names, 'camera': str(exif.get('Model') or ''), 'bird': bird,
           'when': _when(rows[middle['name']]), 'zone': _zone(exif)}
    data = _ebird.load()
    # the clock offset the index found with the days around is kept
    locate([seq], data, rows, clock=stored['clock_offset'] or 0.0)
    with tempfile.TemporaryDirectory() as tmpdir:
        images = _archive_pictures(frames, full, tmpdir, rows[middle['name']], sheet)
        answer, cost = ask_claude(prompt_for(seq, images, rows, explanations), tmpdir,
                                  model or cfg.get('keywords_model'),
                                  effort or cfg.get('doc_effort'))
    kept, _ = settle(seq, answer, data)
    # 'pending': the files do not hold it yet (write_sequence)
    photoindex.put(db, [dict(kept, name=name, written='pending') for name in names]
                   + [{'name': middle['name'], 'camera': seq['camera'], 'lens': _lens(exif),
                       'focal': _focal(exif) or None, 'zone': seq['zone']}])
    row = dict(db.execute('SELECT * FROM photos WHERE seq = ? AND rep = 1', (seq_id,)).fetchone())
    db.close()
    return row, cost


def write_sequence(seq_id, progress=None, chunk=6):
    """Write into the files of a sequence what the index says of it: Claude's
    description if it has one, the place, the latitude and longitude of its
    eBird checklist, and the checklist as the event. The files are rewritten
    where they are, a few at a time (progress(done, total) after each), and
    marked in the index as they go, so that a write cut short is finished
    later; no <file>_original copy is left in the archive unless config
    'gui_backup'. Returns the number of files written; raises RuntimeError."""
    cfg = _cfg()
    db = photoindex.connect()
    frames = photoindex.sequence(db, seq_id)
    row = db.execute('SELECT * FROM photos WHERE seq = ? AND rep = 1', (seq_id,)).fetchone()
    if not frames or row is None:
        raise RuntimeError('séquence inconnue')
    data = _ebird.load()
    checklist = data.lists.get(row['checklist']) if data and row['checklist'] else None
    args = file_tags(answer_of(row), dict(row, note=row['note'] or ''), checklist)
    if not args:
        raise RuntimeError("la base n'a rien à écrire pour cette séquence (ni description, "
                           'ni liste eBird)')
    paths = {frame['name']: photoindex.resolve(frame['path']) for frame in frames}
    todo = [name for name in paths if paths[name]]
    if not todo:
        raise RuntimeError("les fichiers sont hors d'atteinte (le disque d'archive est-il monté ?)")
    # the frames of a sequence come from one camera: the middle one says
    #   whether they carry a position of their own
    middle = paths[todo[len(todo) // 2]]
    own = own_position(read_exif([middle], fast=True).get(middle, {}))
    rows = {paths[name]: ({'GPSLatitude': 0} if own else {}) for name in todo}
    db.execute("UPDATE photos SET written = 'pending' WHERE seq = ?", (seq_id,))
    db.commit()
    done = 0
    for start in range(0, len(todo), chunk):
        part = todo[start:start + chunk]
        write_files(args, [paths[name] for name in part], rows, backup=bool(cfg.get('gui_backup')))
        db.execute(f'UPDATE photos SET written = ? WHERE name IN ({", ".join("?" * len(part))})',
                   [datetime.now().isoformat(' ', 'seconds')] + part)
        db.commit()
        done += len(part)
        if progress:
            progress(done, len(todo))
    db.close()
    return done


def pending_writes():
    """the sequences whose files still wait for what the index says of them"""
    db = photoindex.connect()
    seqs = [row[0] for row in db.execute(
        "SELECT DISTINCT seq FROM photos WHERE written = 'pending' AND seq IS NOT NULL")]
    db.close()
    return seqs


def thumbnails(year=None, every=False, previews=False, limit=None, jobs=4):
    """Make ahead of time the thumbnails the search page shows: the frame
    that stands for each sequence, the newest first (every: all the frames;
    previews: also the 1600 px image of the viewer; year: only that year).
    The photos are read from the archive, so the drive has to be mounted;
    what is made is kept and a run that is stopped goes on from there."""
    if not os.path.isdir(_config_module.archive_root()):
        print(f"{_config_module.archive_root()} est introuvable (le disque d'archive est-il "
              'monté ?) : seules les photos hors archive peuvent être lues.')
    db = photoindex.connect()
    where = '1' if every else 'rep = 1'
    args = []
    if year:
        where += ' AND substr(COALESCE(local, taken), 1, 4) = ?'
        args.append(str(year))
    rows = db.execute(f'SELECT name, path, rep FROM photos WHERE {where} '
                      'ORDER BY COALESCE(local, taken) DESC', args).fetchall()
    db.close()
    todo = [(row['name'], row['path'], previews and row['rep']) for row in rows
            if not os.path.isfile(photoindex.thumb_path(row['name']))
            or (previews and row['rep'] and not os.path.isfile(photoindex.preview_path(row['name'])))]
    todo = todo[:limit] if limit else todo
    print(f"{_count(len(todo), 'vignette')} à faire sur {len(rows)} (les plus récentes d'abord).")

    def make(item):
        name, path, big = item
        full = photoindex.resolve(path)
        if not full:
            return False
        try:
            photoindex.save_images(_image(full, big=bool(big)), name, preview=bool(big))
            return True
        except Exception:
            return False

    made = missed = 0
    try:
        with ThreadPoolExecutor(max_workers=max(1, jobs)) as pool:
            for count, ok in enumerate(pool.map(make, todo), 1):
                made, missed = made + bool(ok), missed + (not ok)
                print(f'  {count}/{len(todo)}', end='\r', flush=True)
    except KeyboardInterrupt:
        print('\nArrêt demandé : ce qui est fait est gardé.')
    print(f"\n{_count(made, 'vignette')} faite{'s' if made > 1 else ''}"
          + (f', {_count(missed, "photo")} hors d\'atteinte ou illisible{"s" if missed > 1 else ""}.'
             if missed else '.'))


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        prog='patchdoc', add_help=False,
        description="patchnames, avec chaque photo décrite par Claude au passage : les oiseaux "
                    '(photos prises au-delà de 200 mm) sont identifiés avec vos listes eBird '
                    'comme a priori, les fichiers renommés, archivés, et indexés pour la page '
                    'de recherche.')
    parser.add_argument('-h', '--help', action='help', help='Afficher cette aide.')
    parser.add_argument('--dry-run', action='store_true',
                        help="Essai : montrer les réponses de Claude ; ne rien écrire, déplacer "
                             'ni indexer.')
    parser.add_argument('--index-only', action='store_true',
                        help='Décrire et indexer, sans toucher aux fichiers ni les déplacer.')
    parser.add_argument('--force', action='store_true',
                        help='Décrire de nouveau les photos qui ont déjà un titre.')
    parser.add_argument('--gap', type=float, default=None,
                        help='Secondes sans prise de vue qui commencent une nouvelle séquence '
                             '(config, 30).')
    parser.add_argument('--min-focal', type=float, default=None,
                        help="Une photo prise au-delà de cette focale (mm) est une photo d'oiseau "
                             '(config bird_focal_min, 200).')
    parser.add_argument('--clock', type=float, default=None, metavar='HEURES',
                        help="Heures à ajouter à l'horloge de l'appareil pour avoir l'heure sur "
                             "place (6 pour un appareil resté à l'heure de Montréal en Suède). "
                             "Trouvées d'après les listes eBird si l'option est absente.")
    parser.add_argument('--jobs', type=int, default=None,
                        help='Séquences décrites en même temps (config doc_jobs, 3).')
    parser.add_argument('--budget', type=float, default=None, metavar='DOLLARS',
                        help="Dépense maximale de ce passage, en dollars US au prix de l'API ; "
                             'les séquences restantes attendent le passage suivant '
                             '(config doc_budget, 10 ; 0 : sans plafond).')
    parser.add_argument('--model', default=None,
                        help='Le modèle de Claude : opus, sonnet (environ la moitié du coût), '
                             'haiku (environ 3 %% du coût, moins sûr de lui). Par défaut : celui '
                             'de claude (config keywords_model).')
    parser.add_argument('--effort', default=None, choices=['low', 'medium', 'high', 'xhigh', 'max'],
                        help="L'effort de réflexion de Claude (config doc_effort ; par défaut "
                             'celui de claude).')
    parser.add_argument('--no-backup', action='store_true',
                        help='Ne pas garder les originaux sous <fichier>_original.')
    parser.add_argument('--instructions', default=None,
                        help="Le fichier MD d'explications (~/.config/patchnames/keywords.md).")
    group = parser.add_mutually_exclusive_group()
    group.add_argument('--refresh', dest='refresh', action='store_true', default=None,
                       help="Demander d'abord un nouvel export à eBird, quel que soit celui sur "
                            'disque.')
    group.add_argument('--no-refresh', dest='refresh', action='store_false',
                       help='Utiliser les données eBird sur disque même si les photos sont plus '
                            'récentes.')
    sub = parser.add_subparsers(dest='cmd', title='commandes')

    p_gui = sub.add_parser('gui', help='La page de recherche, dans le navigateur.')
    p_gui.add_argument('--port', type=int, default=None, help='Port (config gui_port, 8765).')
    p_gui.add_argument('--no-browser', action='store_true', help='Ne pas ouvrir le navigateur.')
    p_gui.add_argument('--no-vignettes', action='store_true',
                       help="Ne pas faire en arrière-plan les vignettes qui manquent.")

    p_index = sub.add_parser(
        'index', help="Ajouter à l'index les photos déjà dans l'archive, d'après leurs noms et "
                      'les listes eBird (sans Claude).')
    p_index.add_argument('--exif', nargs='+', metavar='DOSSIER', default=None,
                         help="Lire plutôt ce que les photos de ces dossiers disent d'elles-mêmes "
                              '(appareil, titre, description, mots-clés) ; lent sur un disque '
                              'réseau.')
    p_index.add_argument('--force', action='store_true',
                         help='Avec --exif : relire les photos déjà lues.')
    p_index.add_argument('--rematch', action='store_true',
                         help="Refaire, d'après l'index seul, l'accord de toutes les séquences "
                              'avec les listes eBird (après un nouvel export).')

    p_thumbs = sub.add_parser(
        'vignettes', help="Faire d'avance les vignettes de la page de recherche (disque "
                          "d'archive monté ; sans Claude).")
    p_thumbs.add_argument('--year', default=None, metavar='ANNEE', help='Seulement cette année.')
    p_thumbs.add_argument('--all', action='store_true',
                          help='Toutes les vues, pas seulement celle qui représente chaque '
                               'séquence.')
    p_thumbs.add_argument('--previews', action='store_true',
                          help="Aussi l'image de 1600 px de la visionneuse.")
    p_thumbs.add_argument('--limit', type=int, default=None, help='Au plus ce nombre de vignettes.')

    p_ebird = sub.add_parser('ebird', help='Les données eBird : ce qui est sur disque, un '
                                           'nouvel export.')
    p_ebird.add_argument('--refresh', action='store_true',
                         help='Demander un nouvel export à eBird et attendre son courriel.')
    p_ebird.add_argument('--login', action='store_true',
                         help='Garder les comptes eBird et courriel dans le trousseau de macOS.')

    args = parser.parse_args()
    if args.cmd == 'gui':
        from .gui import serve
        serve(port=args.port, browser=not args.no_browser,
              fill=False if args.no_vignettes else None)
    elif args.cmd == 'index':
        if args.exif:
            index_exif(args.exif, force=args.force)
        elif args.rematch:
            rematch()
        else:
            index_archive()
    elif args.cmd == 'vignettes':
        thumbnails(year=args.year, every=args.all, previews=args.previews, limit=args.limit)
    elif args.cmd == 'ebird':
        if args.login:
            _ebird.login()
        if args.refresh:
            _ebird.refresh()
        _ebird.status()
    else:
        document(gap=args.gap, force=args.force, dry_run=args.dry_run,
                 index_only=args.index_only, backup=not args.no_backup,
                 instructions=args.instructions, model=args.model, effort=args.effort,
                 jobs=args.jobs, min_focal=args.min_focal, refresh=args.refresh,
                 clock=args.clock, budget=args.budget)


if __name__ == '__main__':
    sys.exit(main())
