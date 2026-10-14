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
   eBird export is asked for when the photos are newer than the one on disk.
   A photo that carries a position of its own (a phone) keeps it, and its
   place is named from that position (geo.py), not from the checklist;
3. shows each sequence to Claude (the claude command line, in print mode and
   allowed to read files only): the middle frame, a crop at full resolution
   around the focus point and a contact sheet, with the checklist. Claude
   answers with a title, the main subject, a description, keywords, and for
   a bird the species, how sure it is, and what does not fit the checklist
   (the photographer may have been wrong, or have left a bird out). A bird
   photo also gets three notes from 1 to 5: the focus on the eye, how clear
   of branches the bird is, how close to eye level the photo is. Every
   frame of a burst gets its own, to find the best of the lot: the
   sharpness of each is measured where the camera focused, Claude sees the
   sharpest at full resolution and all of them cut around their focus point,
   and the best frame then stands for the sequence in the search page;
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
model. A run says what it expects to spend before it starts; with a limit
(config 'doc_budget', --budget; none by default) it stops there, the
sequences left staying in the directory for the next run; --model and
--effort pick a cheaper setting.

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
from . import faces
from . import geo
from . import pano
from . import photoindex
from . import remote
from .keywords import (IMAGE_PATTERNS, UNSURE, _groups, _image, _pictures, _tags, _when,
                       quarters, turned, turned_box, upright,
                       instructions_path, write_template)

#: the main subject of a photo, one of
TOPICS = ('oiseau', 'autre animal', 'plante', 'paysage', 'ciel', 'ville et architecture',
          'personnes', 'objet', 'document', 'autre')
#: keyword of a photo whose species is not on its eBird checklist
OFFLIST = 'absent de la liste eBird'
#: start of GPSAreaInformation in a file whose position is that of its eBird
#:  checklist, written here
GPS_MARK = 'eBird'
#: a burst of bird photos is noted frame by frame, in two calls. Claude first
#:  sees every frame cut to TILE_CUT pixels around its focus point and shown
#:  at TILE, TILES to a sheet (beyond RATED frames, the others take the notes
#:  of the nearest), and says where the eye is in each. It then sees the EYES
#:  frames whose eye is the sharpest at full resolution, a square of EYE
#:  pixels around the eye, EYE_SHEET to a sheet, to judge the focus on it
TILE_CUT, TILE, TILES, RATED = (1400, 933), (350, 233), 16, 96
EYE, EYE_SHEET, EYES = 512, 6, 12
#: frames of a burst read from the archive at the same time: a network drive
#:  answers slowly, and mostly waits; FRAMES are asked in one exiftool call
READS, FRAMES = 4, 3
#: a frame whose eye Claude did not see at full resolution: the note of one it
#:  saw, less this much each time the sharpness measured on the eye halves
SOFTER = 1.0
#: keyword of the best frame of a burst
BEST = 'meilleure vue'
#: keyword of an identification the photographer checked himself, and what a
#:  bird he could not name is called (its title, its keyword)
VERIFIED = 'identification vérifiée'
UNKNOWN_TITLE, UNKNOWN = 'Oiseau indéterminé', 'indéterminé'
#: the name, in a file, of the region where the eye of the bird is
EYE_NAME = 'œil'
#: what the frames of a sequence may each have of their own
OWN = ('eye', 'clear', 'level', 'best', 'eye_x', 'eye_y')
#: the notes of a bird photo, 1 to 5: column of the index, key of Claude's
#:  answer, name in the Lightroom hierarchy (Qualité|...), keyword
NOTES = (('eye', 'oeil_net', 'Œil net', 'œil net'), ('clear', 'degage', 'Dégagement', 'dégagé'),
         ('level', 'hauteur_oeil', "Hauteur d'œil", "hauteur d'œil"))

#: what exiftool is asked of each photo
FIELDS = ['-DateTimeOriginal', '-OffsetTimeOriginal', '-Model', '-LensModel', '-LensID',
          '-LensInfo', '-Lens', '-FocalLength#', '-Orientation#', '-GPSLatitude#', '-GPSLongitude#',
          '-GPSAreaInformation', '-AFAreaXPosition', '-AFAreaYPosition', '-AFImageWidth',
          '-AFImageHeight', '-XMP-dc:Title', '-XMP-dc:Description', '-XMP-dc:Subject',
          '-XMP-lr:HierarchicalSubject', '-XMP-iptcExt:Event', '-XMP-iptcExt:PersonInImage',
          '-ImageSize', '-XMP-mwg-rs:RegionName', '-XMP-mwg-rs:RegionAreaX#',
          '-XMP-mwg-rs:RegionAreaY#']

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
- "oeil_net" : pour un oiseau, la netteté de la mise au point sur son œil, de 1 à 5, jugée sur l'agrandissement à pleine résolution : 5 = œil parfaitement net (reflet et détail des plumes autour de l'œil), 3 = acceptable, 1 = flou ; null si l'œil ne se voit pas ou s'il n'y a pas d'oiseau ;
- "degage" : pour un oiseau, à quel point il est dégagé, de 1 à 5 : 5 = entier et sans rien devant lui, 3 = en partie masqué, 1 = presque caché par des branches ou des feuilles ; null sans oiseau ;
- "hauteur_oeil" : pour un oiseau, à quel point la photo est prise à la hauteur de son œil, de 1 à 5 : 5 = appareil à sa hauteur, 3 = angle modéré, 1 = forte plongée ou contre-plongée (oiseau haut dans un arbre ou dans le ciel, ou vu de dessus) ; null sans oiseau ;
- "oeil_x", "oeil_y" : pour un oiseau dont l'œil se voit, la position de cet œil dans la deuxième image (l'agrandissement à pleine résolution), en pourcentage de sa largeur depuis son bord gauche et de sa hauteur depuis son bord haut, de 0 à 100 ; s'il y a plusieurs oiseaux, l'œil de celui qui est le plus net ; null si aucun œil ne se voit dans cette image ou s'il n'y a pas d'oiseau ;
- "vues" : s'il y a des planches « vues », une entrée par vue numérotée, pour trouver la meilleure de la rafale : {{"numero": "...", "oeil_visible": true si l'œil de l'oiseau se voit dans cette vue, "oeil_x" et "oeil_y": la position de cet œil dans la vue (pas dans la planche), en pourcentage de sa largeur depuis son bord gauche et de sa hauteur depuis son bord haut, de 0 à 100 (null si l'œil ne se voit pas ; s'il y a plusieurs oiseaux, l'œil de celui qui est le plus net), "oeil_net": la netteté de l'œil de 1 à 5 jugée grossièrement sur cette vue réduite, 4 au plus (elle sera reprise à pleine résolution ; null si l'œil ne se voit pas), "degage": 1 à 5, "hauteur_oeil": 1 à 5}}, avec les mêmes échelles ; "degage" et "hauteur_oeil" valent null si l'oiseau n'est pas dans la vue. Les vues d'une rafale diffèrent (l'oiseau tourne la tête, passe derrière une branche, s'envole) : note chacune pour elle-même. [] sans ces planches ;
- "liste" : l'identifiant (S...) de la liste eBird à laquelle la séquence appartient, null s'il n'y en a pas ;
- "note_ebird" : une phrase seulement si ce que tu vois ne s'accorde pas avec la liste eBird (espèce absente de la liste, autre espèce plus probable que celle notée) ; une chaîne vide "" quand la photo s'accorde avec la liste ou qu'il n'y a pas de liste."""

VIEWS = ("La première est la vue du milieu de la séquence, réduite ; la deuxième est un "
         "agrandissement de cette vue à pleine résolution autour du point de mise au point de "
         "l'appareil (regarde-la attentivement : un sujet lointain n'est souvent visible que là)")
SHEETS = (" ; les suivantes sont des planches contact de toutes les vues de la séquence (le "
          "numéro de chaque vue est écrit dessus), pour vérifier qu'il s'agit du même sujet")

#: a burst of bird photos: its sharpest frame instead of the middle one, and
#:  sheets of every frame cut around its focus point
SHARPEST = ('du milieu', 'la plus nette')
BURST = (" ; viennent ensuite les planches « vues », où chaque vue de la séquence est recadrée "
         "autour de son point de mise au point et réduite quatre fois (son numéro est écrit "
         "dessus), pour vérifier qu'il s'agit du même sujet, noter chaque vue et dire où est "
         "l'œil de l'oiseau dans chacune. Le titre et la description parlent de ce que montre "
         "la séquence, jamais des planches, des numéros de vue ni de la netteté")

#: the second call for a burst: the eye of each frame at full resolution
EYES_SYSTEM = ("Tu juges la netteté de photos d'oiseaux. Tu lis les images indiquées avec l'outil "
               "Read, puis tu réponds uniquement par l'objet JSON demandé.")
EYES_PROMPT = """Tu notes la netteté de l'œil d'un oiseau ({subject}) sur plusieurs vues d'une même rafale, pour trouver la meilleure.

Lis ces images avec l'outil Read :
{images}

Chaque planche montre des vues numérotées, à pleine résolution, recadrées autour de l'œil de l'oiseau. Pour chaque vue, note la netteté de la mise au point sur l'œil, de 1 à 5 : 5 = œil parfaitement net (reflet net, détail des plumes autour de l'œil), 3 = acceptable, 1 = flou. Compare les vues entre elles : les notes doivent les départager. null si l'œil ne se voit pas dans le carré.

Un indice : la netteté mesurée dans chaque carré (finesse des détails), en pourcentage de celle du carré le plus net : {measured}. Elle départage bien deux vues du même cadrage ; un carré mal centré ou un flou de bougé sur l'œil seul la trompent, et c'est l'œil que tu juges.

Réponds par un objet JSON : {{"yeux": [{{"numero": "...", "oeil_net": 1 à 5 ou null}}]}}, une entrée par vue."""
EYES_SCHEMA = {
    'type': 'object',
    'properties': {'yeux': {'type': 'array', 'items': {
        'type': 'object',
        'properties': {'numero': {'type': 'string'}, 'oeil_net': {'type': ['integer', 'null']}},
        'required': ['numero', 'oeil_net'], 'additionalProperties': False}}},
    'required': ['yeux'], 'additionalProperties': False,
}

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
        'oeil_net': {'type': ['integer', 'null']},
        'degage': {'type': ['integer', 'null']},
        'hauteur_oeil': {'type': ['integer', 'null']},
        'oeil_x': {'type': ['number', 'null']},
        'oeil_y': {'type': ['number', 'null']},
        'vues': {'type': 'array', 'items': {
            'type': 'object',
            'properties': {'numero': {'type': 'string'}, 'oeil_visible': {'type': 'boolean'},
                           'oeil_x': {'type': ['number', 'null']},
                           'oeil_y': {'type': ['number', 'null']},
                           'oeil_net': {'type': ['integer', 'null']},
                           'degage': {'type': ['integer', 'null']},
                           'hauteur_oeil': {'type': ['integer', 'null']}},
            'required': ['numero', 'oeil_visible', 'oeil_x', 'oeil_y', 'oeil_net', 'degage',
                         'hauteur_oeil'],
            'additionalProperties': False}},
        'liste': {'type': ['string', 'null']},
        'note_ebird': {'type': 'string'},
    },
    'required': ['titre', 'sujet', 'especes', 'habitat', 'description', 'certitude',
                 'autres_mots_cles', 'oeil_net', 'degage', 'hauteur_oeil', 'oeil_x', 'oeil_y',
                 'vues', 'liste', 'note_ebird'],
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
    found = remote.rows((['-fast2'] if fast else []) + FIELDS, list(paths))
    return {row['SourceFile']: row for row in found}


def read_frames(paths):
    """the tags of photos and the full-size picture inside each, {path:
    (dict, the bytes of a JPEG or None)}, in one exiftool call: on a network
    drive each visit to a file costs more than what it brings back (and the
    call is made on the server of the archive when it has exiftool)"""
    import base64
    found = {}
    for row in remote.rows(['-b', '-JpgFromRaw'] + FIELDS, list(paths)):
        raw = row.pop('JpgFromRaw', None)
        try:
            raw = base64.b64decode(raw[len('base64:'):])
        except (TypeError, ValueError):
            raw = None
        found[row['SourceFile']] = (row, raw)
    return found


def read_frame(path, found=None):
    """the tags of a photo and its picture at full size, (dict, PIL image);
    found: what read_frames brought for it, if it was asked with others"""
    import io
    from PIL import Image
    row, raw = found if found else read_frames([path]).get(path, ({}, None))
    try:
        # (the picture inside a raw file is seen as the file says: see _image)
        img = upright(Image.open(io.BytesIO(raw)), row.get('Orientation')).convert('RGB')
    except Exception:
        # not a raw file, or one without a full-size picture inside
        img = _image(path, big=True)
    return row, img


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


#: a camera named by the initials of its owner only (from a file name): the
#:  camera itself is not known
OWNER = re.compile(r'[A-Z]{2}')
#: a photo of another camera that carries its position is tied all the same
#:  to the checklist it was taken during, when it is this close to its place
NEAR_KM = 5.0


def anchored(camera, names, cameras=None):
    """whether photos are tied to the photographer's eBird checklists, for
    their time and their place: those of the cameras he goes birding with
    (config 'ebird_cameras', the name a file gives of its camera: a NEF, or
    a JPEG or a DNG made from one, which keeps it). Not those of a phone,
    which members of the family take too, nor of anything else. A photo
    whose camera is not known is one of them if it is a NEF or a DNG.
    cameras: that expression, compiled (read from the config without it)."""
    camera = '' if not camera or OWNER.fullmatch(camera) else camera
    if camera:
        cameras = cameras or re.compile(_cfg()['ebird_cameras'], re.I)
        return bool(cameras.search(camera))
    return any(os.path.splitext(name)[1].lower() in ('.nef', '.dng') for name in names)


def locate(seqs, data, rows=None, clock=None, before=None):
    """give each sequence its clock offset and its eBird match: the offsets
    come from the bird sequences of each camera and apply to all it took.
    Only the photos of the camera of the checklists are matched (anchored):
    the others keep their clock and get no checklist.

    clock: the offset in hours, when the photographer knows it. before:
    {day: hours}, offsets found earlier with more days around, kept for a
    camera that does not record its time zone (a few days alone would not
    move its clock). A photo of another camera that carries its own
    position, taken during a checklist (its clock as it is) within NEAR_KM
    of its place, is tied to it all the same: the photographer, or his
    family with him, on the outing. seq['position']: that position, when
    there are no rows (the index matched again)."""
    rows = rows or {}
    moments, zoned = {}, set()
    cameras = re.compile(_cfg()['ebird_cameras'], re.I)
    for seq in seqs:
        seq['anchored'] = anchored(seq['camera'], seq['names'], cameras)
        if seq['bird'] and seq['when'] and seq['anchored']:
            moments.setdefault(seq['camera'], []).append((seq['when'], seq.get('zone')))
            if seq.get('zone') is not None:
                zoned.add(seq['camera'])
    offsets = {}
    if data and clock is None:
        offsets = {camera: data.offsets(*zip(*pairs)) for camera, pairs in moments.items()}
    for seq in seqs:
        seq['offset'], seq['match'], seq['gps'] = float(clock or 0.0) * seq['anchored'], None, None
        if data is not None and seq['when'] and seq['anchored']:
            day = seq['when'].date()
            if clock is not None:
                pass
            elif before and day in before and seq['camera'] not in zoned:
                seq['offset'] = before[day]
            else:
                seq['offset'] = offsets.get(seq['camera'], {}).get(day, 0.0)
            seq['match'] = data.match(seq['when'], seq['offset'])
        row = rows.get(seq['names'][len(seq['names']) // 2], {})
        position = seq.get('position')
        if own_position(row):
            try:
                position = float(row['GPSLatitude']), float(row['GPSLongitude'])
            except (KeyError, TypeError, ValueError):
                position = None
        if data is not None and seq['when'] and not seq['anchored'] and position:
            # (another camera, where and when a checklist was: tied to it, and to it alone)
            match = data.match(seq['when'], 0.0)
            listed = match.checklist
            if listed and listed.lat is not None and geo.km(*position, listed.lat, listed.lon) <= NEAR_KM:
                seq['match'] = match._replace(others=[])
        if not own_position(row):
            continue
        # a position of its own (a phone): the photo keeps it and its place is
        #   the one of that position, asked online (geo.py), with or without
        #   a checklist
        try:
            seq['gps'] = geo.spot(float(row['GPSLatitude']), float(row['GPSLongitude']), data,
                                  seq['match'].checklist if seq['match'] else None)
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


def _note(value):
    """a note of Claude as the index keeps it: 1 to 5, or None"""
    try:
        return min(5, max(1, int(value)))
    except (TypeError, ValueError):
        return None


def settle(seq, answer, data):
    """what is kept of a sequence, from Claude's answer (None for a sequence
    it did not describe) and the eBird match: the columns its frames share,
    and its checklist (None without one). The place and the position are
    those of the checklist, unless the photos carry a position of their own
    (seq['gps']), which then gives both."""
    match = seq.get('match')
    checklist = match.checklist if match else None
    kind = match.kind if match else None
    if answer and answer.get('liste') and match and data:
        # Claude may prefer another checklist of the day
        named = data.lists.get(str(answer['liste']).strip())
        if named is not None and named in match.others:
            checklist, kind = named, 'other'
    sure = {'location': '', 'county': '', 'state': '', 'country': '', 'lat': None, 'lon': None,
            'own_position': int(bool(seq.get('gps'))),
            'checklist': checklist.sid if checklist else None, 'match': kind,
            'clock_offset': seq.get('offset') or 0.0, 'bird': int(seq['bird'])}
    if seq.get('gps'):
        # photos that carry their position: the checklist says what was seen,
        #   not where
        sure.update(_place(seq['gps']), match=kind or 'gps')
    elif checklist:
        sure.update(_place(checklist))
    if not answer:
        return sure, checklist
    species = answer.get('especes') or []
    # a photo taken near a list, not during it, is not held against the list
    on_list = _on_list([sp.get('latin', '') for sp in species],
                       checklist if kind in ('inside', 'other', 'manual') else None)
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
        **{column: _note(answer.get(key)) for column, key, _, _ in NOTES},
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
    marked as those of the eBird place, not of the photographer. A place
    found from the position of the photo itself (geo.Spot) has its names,
    and the checklist the photo was taken during as the event: the position
    the file carries is left as it is."""
    country = _ebird.country_name(checklist.country)
    position = position and not isinstance(checklist, geo.Spot)
    args = ['-charset', 'iptc=UTF8', '-codedcharacterset=utf8']
    args += [f'-XMP-iptcExt:Event=eBird {checklist.sid}'] if checklist.sid else []
    args += [f'-XMP-iptcCore:Location={checklist.location}',
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
    its latitude and longitude, and the eBird checklist as the event. Photos
    that carry their position (kept['own_position']) have the place kept for
    them, named from it, not the one of the checklist."""
    args = []
    if kept.get('own_position') and kept.get('location'):
        checklist = geo.Spot(kept['location'], kept.get('county') or '', kept.get('state') or '',
                             kept.get('country') or '', kept.get('lat'), kept.get('lon'),
                             kept.get('checklist'))
    people = [name for name in (kept.get('people') or '').split(photoindex.SEP) if name]
    if answer:
        answer = dict(answer)
        if kept['note']:
            answer['description'] = f"{answer['description']} [eBird : {kept['note']}]"
        words = list(answer.get('autres_mots_cles') or [])
        if kept['on_checklist'] == 0:
            words.append(OFFLIST)
        # the notes of a bird photo and the people named: keywords, and a
        #   place in the Lightroom hierarchy
        marks = [(label, stem, kept.get(column)) for column, _, label, stem in NOTES
                 if kept.get(column)]
        best = [BEST] if kept.get('best') else []
        if kept.get('verified'):
            words.append(VERIFIED)
        answer['autres_mots_cles'] = (words + [f'{stem} {n}/5' for _, stem, n in marks] + best
                                      + people)
        answer['hierarchie'] = ([f'Qualité|{label}|{n}' for label, _, n in marks]
                                + ['Qualité|Meilleure vue'] * len(best)
                                + [f'Personne|{name}' for name in people])
        args += _tags(answer)
    else:
        # no description to write: the people are added to the keywords the
        #   file has (taken off first, not to be there twice)
        for name in people:
            for tag, value in (('XMP-dc:Subject', name), ('IPTC:Keywords', name),
                               ('XMP-lr:HierarchicalSubject', f'Personne|{name}')):
                args += [f'-{tag}-={value}', f'-{tag}+={value}']
    if 'people' in kept:
        args += [f'-XMP-iptcExt:PersonInImage={name}' for name in people] or (
            ['-XMP-iptcExt:PersonInImage='])
    if (kept.get('eye_x') is not None and kept.get('eye_y') is not None
            and _cfg().get('doc_eye_region', True)):
        args.append(eye_region(kept['eye_x'], kept['eye_y'], kept.get('_size')))
    if checklist:
        args += place_tags(checklist, position)
    return args


def eye_region(x, y, size=None):
    """the exiftool argument that writes into a file where the eye of the
    bird is (fractions of the picture from its left and its top): a region
    of the Metadata Working Group schema, the one of the face tags of photo
    software, named "œil" and of type Focus. It is the square the loupe of
    the search page shows when the size of the picture is known."""
    wide, high = (EYE / size[0], EYE / size[1]) if size else (0.1, 0.15)
    dims = f'AppliedToDimensions={{W={size[0]},H={size[1]},Unit=pixel}},' if size else ''
    return ('-XMP-mwg-rs:RegionInfo={' + dims + 'RegionList=[{Area={'
            + f'X={x:.4f},Y={y:.4f},W={min(wide, 1):.4f},H={min(high, 1):.4f},Unit=normalized'
            + '},Name=' + EYE_NAME + ',Type=Focus}]}')


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


def write_groups(groups, rows, backup=True, most=40):
    """write tags into files with exiftool, [(arguments, files)], each group
    with its own tags, `most` groups to a run of exiftool (on the server of
    the archive when it has exiftool: the files are rewritten where they
    are, without coming here and going back). The latitude and longitude
    are left out for the files that carry a position of their own; raises
    RuntimeError."""
    head = ['-q', '-m'] + ([] if backup else ['-overwrite_original'])
    blocks = []
    for args, names in groups:
        position = [arg for arg in args if arg.startswith('-GPS')]
        rest = [arg for arg in args if arg not in position]
        for part, extra in (([n for n in names if not own_position(rows.get(n, {}))], position),
                            ([n for n in names if own_position(rows.get(n, {}))], [])):
            if part:
                blocks.append((head + rest + extra, part))
    for start in range(0, len(blocks), most):
        code, _, err, _ = remote.run_many(blocks[start:start + most])
        if code != 0:
            raise RuntimeError(f'exiftool : {err.strip()[:300]}')


def write_files(args, names, rows, backup=True):
    """write the same tags into files (see write_groups)"""
    write_groups([(args, names)], rows, backup)


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
    people = under('Personne') or _strings(row.get('PersonInImage'))
    notes = dict(item.rsplit('|', 1) for item in under('Qualité') if '|' in item)
    known = set(species + latin + habitat + people + [topic, UNSURE, OFFLIST, BEST, VERIFIED]
                + [f'{stem} {n}/5' for _, _, _, stem in NOTES for n in range(1, 6)])
    description, _, note = str(row.get('Description') or '').partition(' [eBird : ')
    event = str(row.get('Event') or '')
    out = {'title': str(row['Title']), 'topic': topic or ('oiseau' if species else ''),
           'description': description, 'note': note.rstrip(']'),
           'species': photoindex.SEP.join(species), 'latin': photoindex.SEP.join(latin),
           'habitat': photoindex.SEP.join(habitat),
           'keywords': photoindex.SEP.join(word for word in subject if word not in known),
           'certainty': ('à confirmer' if UNSURE in subject else 'sûre') if species else '',
           'people': photoindex.SEP.join(people),
           **{column: _note(notes.get(label)) for column, _, label, _ in NOTES},
           'best': int('Meilleure vue' in under('Qualité')),
           'described': 'file', 'written': 'file'}
    regions = _strings(row.get('RegionName'))
    if EYE_NAME in regions:
        at = regions.index(EYE_NAME)
        try:
            out['eye_x'] = float(_strings(row.get('RegionAreaX'))[at])
            out['eye_y'] = float(_strings(row.get('RegionAreaY'))[at])
        except (IndexError, ValueError):
            pass
    if VERIFIED in subject:
        # (what was said before the photographer checked is not in the file)
        out['verified'] = out['species'] or UNKNOWN
        out['certainty'] = 'sûre' if species else ''
    if event.startswith('eBird S'):
        out['checklist'] = event.split()[1]
    if OFFLIST in subject:
        out['on_checklist'] = 0
    return out


def said_of(seq, rows, data):
    """the columns of a sequence nobody asked Claude about: its eBird match,
    and what the frame that stands for it says of itself"""
    said = from_file(rows.get(stands_for(seq, own_notes(seq, rows)), {}))
    kept, checklist = settle(seq, None, data)
    during = kept['match'] == 'inside'
    kept.update(said)
    if said.get('checklist') and data and said['checklist'] in data.lists:
        # the checklist written in the file, rather than the one of its hours
        checklist, during = data.lists[said['checklist']], True
        if not seq.get('anchored', True) and kept['match'] in (None, 'gps'):
            kept['match'] = 'manual'        # (the matching would not have: it was tied by hand)
        if not seq.get('gps'):
            kept.update(_place(checklist))
    if said:
        kept['on_checklist'] = _on_list(kept['latin'].split(photoindex.SEP),
                                        checklist if during else None)
    return kept


def own_notes(seq, rows):
    """the notes each frame of a sequence holds in its file, {name: {'eye',
    'clear', 'level', 'best'}} (a sequence described before: every frame of
    a burst says its own)"""
    said = {name: from_file(rows.get(name, {})) for name in seq['names']}
    return {name: {key: mine.get(key) for key in OWN} for name, mine in said.items() if mine}


def stands_for(seq, notes):
    """the frame that stands for a sequence in the search page: the best of
    a burst, the middle one otherwise"""
    names = seq['names']
    return seq.get('rep') or next((name for name in names if (notes.get(name) or {}).get('best')),
                                  names[len(names) // 2])


def frame_records(seq, kept, rows, paths):
    """the records of the frames of a sequence for the index; paths: {name
    as found: path where the file now is}"""
    names = seq['names']
    final = [paths.get(name, os.path.abspath(name)) for name in names]
    first = os.path.splitext(os.path.basename(final[0]))[0]
    # the notes of each frame: those just given to a burst, or those the
    #   files hold when the sequence is indexed from what they say
    notes = seq.get('notes') or (own_notes(seq, rows) if kept.get('described') == 'file' else {})
    stands = stands_for(seq, notes)
    # what is said of the sequence goes for each frame, its notes aside
    kept = dict(kept, best=0)
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
                      rep=int(name == stands))
        record.update(notes.get(name) or notes.get(stands) or {})
        record['best'] = int(bool((notes.get(name) or {}).get('best')))
        records.append(record)
    return records


# ---------------------------------------------------------------------------
# A burst of bird photos, frame by frame: which is the best of the lot
# ---------------------------------------------------------------------------

def _focus(size, row):
    """the focus point of a frame in a picture of this size (the Nikon AF
    area; its centre without it)"""
    width, height = size
    try:
        # (the camera gives it on the sensor: it turns with the picture)
        x, y = turned(float(row['AFAreaXPosition']) / float(row['AFImageWidth']),
                      float(row['AFAreaYPosition']) / float(row['AFImageHeight']),
                      quarters(row.get('Orientation')))
        return x * width, y * height
    except (KeyError, TypeError, ValueError, ZeroDivisionError):
        return width / 2, height / 2


def _box(size, row, want):
    """the piece of `want` pixels of a frame around its focus point, in a
    picture of `size`: (left, top, width, height)"""
    (cx, cy), (width, height) = _focus(size, row), size
    w, h = min(want[0], width), min(want[1], height)
    return int(min(max(cx - w / 2, 0), width - w)), int(min(max(cy - h / 2, 0), height - h)), w, h


def _focus_box(size, row):
    """the piece keywords._focus_crop cuts: the second picture Claude is
    shown of a sequence that is not a burst"""
    (cx, cy), (width, height) = _focus(size, row), size
    half_w, half_h = min(700, width / 2), min(467, height / 2)
    cx, cy = min(max(cx, half_w), width - half_w), min(max(cy, half_h), height - half_h)
    left, top = int(cx - half_w), int(cy - half_h)
    return left, top, int(cx + half_w) - left, int(cy + half_h) - top


def _cut(img, row, size):
    """a piece of a frame of this size in pixels around its focus point"""
    left, top, w, h = _box(img.size, row, size)
    return img.crop((left, top, left + w, top + h))


def _around(picture, x, y, size=None):
    """the square of a picture around a point of it, EYE pixels wide"""
    size = min(size or EYE, picture.width, picture.height)
    left = int(min(max(x - size / 2, 0), picture.width - size))
    top = int(min(max(y - size / 2, 0), picture.height - size))
    return picture.crop((left, top, left + size, top + size))


def single_eye(answer, row, focus):
    """where the eye is in a bird photo that is not part of a burst, from
    what Claude says of the second picture it was shown (the crop at the
    focus point, file `focus`; row['_size']: the size of the photo): its
    place as fractions of the photo, {'eye_x', 'eye_y'}, and the square
    around it for the loupe; (None, None) when no eye is named"""
    from PIL import Image
    try:
        x, y, size = float(answer['oeil_x']), float(answer['oeil_y']), row['_size']
        crop = Image.open(focus)
    except (KeyError, TypeError, ValueError, OSError):
        return None, None
    left, top, _, _ = _focus_box(size, row)
    x, y = min(max(x, 0), 100) / 100 * crop.width, min(max(y, 0), 100) / 100 * crop.height
    return ({'eye_x': round((left + x) / size[0], 4), 'eye_y': round((top + y) / size[1], 4)},
            _around(crop, x, y))


def _detail(patch):
    """how sharp a piece of a picture at full resolution is: the variance of
    its Laplacian, the pixels binned by two against the noise. A number to
    compare pieces that show the same thing in the same light (the eye of
    the bird in the frames of a burst); it means nothing alone."""
    import numpy as np
    a = np.asarray(patch.convert('L'), dtype='float32')
    a = a[:a.shape[0] // 2 * 2, :a.shape[1] // 2 * 2]
    a = (a[0::2, 0::2] + a[1::2, 0::2] + a[0::2, 1::2] + a[1::2, 1::2]) / 4
    lap = 4 * a[1:-1, 1:-1] - a[:-2, 1:-1] - a[2:, 1:-1] - a[1:-1, :-2] - a[1:-1, 2:]
    return float(lap.var())


def sharpness(img, row):
    """how sharp a frame is where the camera focused (a 512 px square): what
    picks the frame of a burst shown large to Claude"""
    return _detail(_cut(img, row, (512, 512)))


def _number(name):
    """the number written on a frame in the sheets: the last part of its name"""
    return os.path.splitext(os.path.basename(name))[0].split('_')[-1]


def _sheet(pictures, columns, size):
    """a sheet of numbered pictures, [(name, PIL image of this size)]"""
    from PIL import Image, ImageDraw
    columns = min(columns, len(pictures))
    rows = (len(pictures) + columns - 1) // columns
    sheet = Image.new('RGB', (columns * size[0], rows * size[1]), 'white')
    draw = ImageDraw.Draw(sheet)
    for k, (name, picture) in enumerate(pictures):
        x, y = (k % columns) * size[0], (k // columns) * size[1]
        sheet.paste(picture, (x, y))
        draw.rectangle((x, y, x + 8 * len(_number(name)) + 8, y + 15), fill='black')
        draw.text((x + 4, y + 2), _number(name), fill='yellow')
    return sheet


def burst_pictures(names, load, rows, tmpdir, index, keep=None, progress=None, jobs=1):
    """the pictures of a burst of bird photos for Claude's first look: its
    sharpest frame at 1400 px and the crop of that frame at full resolution
    around its focus point, to say what it shows; and the sheets "vues",
    every frame cut around its own focus point and reduced, to note each and
    say where its eye is.

    Each frame is loaded once at full size (load(name) -> a PIL image; it
    may fill rows[name] on the way), `jobs` at a time; one that cannot be
    read is left out. Returns the paths and what eye_pictures and frame_notes
    need: {'sharp': {name: sharpness at the focus point}, 'shown': the
    sharpest frame, 'rated': the frames of the sheets, 'cuts': {name: file
    of its cut at full resolution}, 'boxes': {name: where that cut is in the
    frame, and the size of the frame}}; raises RuntimeError when no frame
    can be read."""
    sharp, tiles, cuts, boxes = {}, {}, {}, {}
    best, lock = {'name': None, 'img': None, 'size': None, 'read': 0}, threading.Lock()

    def measure(name):
        try:
            img = load(name)
            row = rows.get(name, {})
            value, cut = sharpness(img, row), _cut(img, row, TILE_CUT)
            tile = cut.copy()
            tile.thumbnail(TILE)
            file = os.path.join(tmpdir, f'seq{index:03d}_cut_{os.path.splitext(name)[0]}.jpg')
            cut.save(file, quality=95)
        except Exception:
            img = None
        with lock:
            # (a file read badly gives a smaller preview: its sharpness would
            #   not compare with the others')
            best['size'] = best['size'] or (sorted(img.size) if img is not None else None)
            if img is not None and sorted(img.size) == best['size']:
                sharp[name], tiles[name], cuts[name] = value, tile, file
                boxes[name] = _box(img.size, rows.get(name, {}), TILE_CUT) + img.size
                if best['name'] is None or value > sharp[best['name']]:
                    best['name'], best['img'] = name, img
            else:
                img = None
            best['read'] += 1
            if progress:
                progress(best['read'], len(names))
        if keep and img is not None:
            keep(name, img, False)

    with ThreadPoolExecutor(max_workers=max(1, jobs)) as pool:
        list(pool.map(measure, names))
    shown, full = best['name'], best['img']
    if shown is None:
        raise RuntimeError('aucune vue de la séquence ne peut être lue')
    if keep:
        keep(shown, full, True)
    paths = [os.path.join(tmpdir, f'seq{index:03d}_nette.jpg'),
             os.path.join(tmpdir, f'seq{index:03d}_focus.jpg')]
    small = full.copy()
    small.thumbnail((1400, 1400))
    small.save(paths[0], quality=88)
    _cut(full, rows.get(shown, {}), TILE_CUT).save(paths[1], quality=88)
    seen = [name for name in names if name in sharp]
    rated = seen if len(seen) <= RATED else [
        seen[round(i * (len(seen) - 1) / (RATED - 1))] for i in range(RATED)]
    for start in range(0, len(rated), TILES):
        paths.append(os.path.join(tmpdir, f'seq{index:03d}_vues{start // TILES}.jpg'))
        _sheet([(name, tiles[name]) for name in rated[start:start + TILES]], 4,
               TILE).save(paths[-1], quality=88)
    return paths, {'sharp': sharp, 'shown': shown, 'rated': rated, 'cuts': cuts, 'boxes': boxes}


def eye_pictures(burst, answer, tmpdir, index):
    """the sheets "yeux" of a burst, for Claude's second look: the frames
    whose eye it found, at full resolution, a square around the eye; the
    EYES whose eye is the sharpest when there are more. Returns the paths
    and the frames on them. For every frame that shows an eye, burst['eye']
    gets the sharpness measured on it, burst['spot'] where it is (fractions
    of the frame, from the left and from the top) and burst['squares'] the
    square around it, which the loupe of the search page shows."""
    from PIL import Image
    said = {str(view.get('numero', '')).strip(): view for view in answer.get('vues') or []}
    squares, burst['eye'], burst['spot'] = {}, {}, {}
    burst['squares'] = squares
    for name in burst['rated']:
        view = said.get(_number(name)) or {}
        if not view.get('oeil_visible') or view.get('oeil_x') is None or view.get('oeil_y') is None:
            continue
        try:
            cut = Image.open(burst['cuts'][name])
            x = min(max(float(view['oeil_x']), 0.0), 100.0) / 100 * cut.width
            y = min(max(float(view['oeil_y']), 0.0), 100.0) / 100 * cut.height
        except (OSError, TypeError, ValueError):
            continue

        # (the eye is near the middle of the square, not exactly there: the
        #   whole square is measured)
        squares[name] = _around(cut, x, y)
        burst['eye'][name] = _detail(squares[name])
        left, top, _, _, width, height = burst['boxes'][name]
        burst['spot'][name] = (round((left + x) / width, 4), round((top + y) / height, 4))
    eyes = sorted(sorted(squares, key=burst['eye'].get, reverse=True)[:EYES],
                  key=burst['rated'].index)
    paths = []
    for start in range(0, len(eyes), EYE_SHEET):
        paths.append(os.path.join(tmpdir, f'seq{index:03d}_yeux{start // EYE_SHEET}.jpg'))
        _sheet([(name, squares[name]) for name in eyes[start:start + EYE_SHEET]], 3,
               (EYE, EYE)).save(paths[-1], quality=90)
    return paths, eyes


def judge_eyes(seq, answer, tmpdir, index, model=None):
    """Claude's second look at a burst: the focus on the eye of each frame,
    seen at full resolution where its first look said the eye is. Fills
    seq['burst']['judged'] = {name: note}; the cost in dollars. A call that
    fails leaves the rough notes of the first look. It is a comparison of a
    few squares, asked at low effort (the notes came out the same as at the
    default one, for less) of config 'doc_eyes_model' or of `model`."""
    model, effort = _cfg().get('doc_eyes_model') or model, 'low'
    burst = seq['burst']
    burst['judged'] = {}
    paths, eyes = eye_pictures(burst, answer, tmpdir, index)
    if not eyes:
        return 0.0
    subject = ', '.join(sp.get('francais', '') for sp in answer.get('especes') or []) or 'oiseau'
    try:
        top = max(burst['eye'][name] for name in eyes) or 1.0
        measured = ', '.join(f"{_number(name)} : {100 * burst['eye'][name] / top:.0f} %"
                             for name in eyes)
        said, cost = ask_claude(EYES_PROMPT.format(subject=subject, images='\n'.join(paths),
                                                   measured=measured), tmpdir, model, effort,
                                schema=EYES_SCHEMA, system=EYES_SYSTEM, needs=('yeux',))
    except RuntimeError:
        return 0.0
    notes = {str(view.get('numero', '')).strip(): _note(view.get('oeil_net'))
             for view in said.get('yeux') or []}
    burst['judged'] = {name: notes.get(_number(name)) for name in eyes}
    return cost


def frame_notes(names, burst, answer):
    """the notes of each frame of a burst, {name: {'eye', 'clear', 'level',
    'sharp', 'best'}}, and its best frame.

    How clear the bird is, how close to eye level the photo is and whether
    the eye shows come from Claude's first look, frame by frame (a frame
    that was on no sheet takes the notes of the nearest that was). The
    focus on the eye comes from its second look, at full resolution around
    the eye (burst['judged']). A frame whose eye shows but was not among
    those gets the note of the judged frame nearest in sharpness measured on
    the eye, less SOFTER points each time its own is half of that one's;
    failing that, the rough note of the first look, 4 at most. A frame
    whose eye does not show has no note for it. 'sharp' is the sharpness
    measured on the eye, 1 for the sharpest of the burst. The best frame
    has the highest total, the focus on the eye counting double."""
    import math
    sharp, rated = burst['sharp'], burst['rated']
    eye_sharp = burst.get('eye') or {}
    place = {name: k for k, name in enumerate(names)}
    said = {str(view.get('numero', '')).strip(): view for view in answer.get('vues') or []}
    judged = {name: note for name, note in (burst.get('judged') or {}).items() if note}
    top = max(eye_sharp.values(), default=0) or 1.0

    def log(name):
        return math.log2(max(eye_sharp[name], 1e-6))

    notes = {}
    for name in (name for name in names if name in sharp):
        near = min(rated, key=lambda other: abs(place[other] - place[name]))
        # (a frame Claude said nothing of: the notes of the sequence)
        view = said.get(_number(near)) or {'degage': answer.get('degage'),
                                           'hauteur_oeil': answer.get('hauteur_oeil'),
                                           'oeil_net': answer.get('oeil_net'), 'oeil_visible': True}
        eye = judged.get(name)
        if eye is None and view.get('oeil_visible'):
            like = [other for other in judged if other in eye_sharp]
            if name in eye_sharp and like:
                like = min(like, key=lambda other: abs(log(other) - log(name)))
                eye = min(5, max(1, round(judged[like] + SOFTER * (log(name) - log(like)))))
            else:
                eye = min(_note(view.get('oeil_net')) or 0, 4) or None
        spot = (burst.get('spot') or {}).get(name) or (None, None)
        notes[name] = {'eye': eye, 'clear': _note(view.get('degage')),
                       'level': _note(view.get('hauteur_oeil')),
                       'sharp': round(eye_sharp[name] / top, 3) if name in eye_sharp else None,
                       'best': 0, 'eye_x': spot[0], 'eye_y': spot[1]}
    best = max(notes, key=lambda name: (2 * (notes[name]['eye'] or 0) + (notes[name]['clear'] or 0)
                                        + (notes[name]['level'] or 0) + (notes[name]['sharp'] or 0)))
    notes[best]['best'] = 1
    return notes, best


def _groups_of(names, notes):
    """the frames of a sequence by what they have of their own (their
    notes, the place of their eye): [(that, names)]"""
    groups = {}
    for name in names:
        mine = notes.get(name) or {}
        key = tuple((column, mine[column]) for column in OWN if column in mine)
        groups.setdefault(key, []).append(name)
    return [(dict(key), group) for key, group in groups.items()]


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
    lines = []
    if seq.get('gps'):
        spot = seq['gps']
        lines.append(f"La photo porte une position : elle a été prise à {spot.location} "
                     f"({', '.join(part for part in (spot.county, spot.state) if part)}, "
                     f"{_ebird.country_name(spot.country)}).")
        if match is None or not match.checklist:
            return lines[0] + ' Pas de liste eBird ("liste" : null).'
    if not seq.get('anchored', True) and not (match and match.checklist):
        return ("Cette photo vient d'un téléphone ou d'un autre appareil que celui avec lequel le "
                'photographe tient ses listes eBird (souvent un membre de sa famille) : identifie '
                'sans a priori ("liste" : null).')
    if match is None or not (match.checklist or match.others):
        return ("Le photographe n'a pas de liste eBird ce jour-là : identifie sans a priori "
                '("liste" : null).')
    if match.offset:
        lines.append(f"L'horloge de l'appareil semble décalée de {match.offset:+g} h : "
                     f"l'heure sur place serait {match.local:%Y-%m-%d %H:%M}.")
    if match.checklist:
        how = {'inside': 'dont les heures contiennent la séquence',
               'manual': 'à laquelle le photographe a lui-même rattaché la photo'}.get(
            match.kind, "la plus proche de la séquence (à moins d'une heure)")
        lines += [f'Liste eBird du photographe {how} :', _list_text(match.checklist, seq['bird'])]
    else:
        lines.append("Aucune liste eBird du photographe ne contient l'heure de la séquence.")
    if match.others:
        lines.append('Autres listes eBird du même jour (la séquence peut en venir si '
                     "l'horloge de l'appareil est décalée) :")
        lines += [_list_text(other, False) for other in match.others]
    if seq['bird']:
        lines += ['', HOW]
    elif seq.get('gps'):
        lines.append('Le lieu est celui de la position de la photo, pas celui des listes ; '
                     '"liste" : celle pendant laquelle elle a été prise, null si aucune.')
    else:
        lines.append('Ces listes disent surtout où la photo a été prise ; "liste" : celle '
                     "pendant laquelle elle a été prise, null si aucune.")
    return '\n'.join(lines)


def prompt_for(seq, images, rows, instructions):
    names = seq['names']
    times = [t for t in (_when(rows.get(name, {})) for name in names) if t]
    burst = seq.get('burst')
    row = rows.get(burst['shown'] if burst else names[len(names) // 2], {})
    focal = _focal(row)
    gear = ', '.join(part for part in (str(row.get('Model') or ''), _lens(row)) if part)
    if seq['bird']:
        kind = BIRD.format(focal=f'{focal:.0f}')
    else:
        kind = OTHER.format(focal=f' ({focal:.0f} mm)' if focal else '')
    if seq.get('given') is not None:
        # the photographer checked the identification in the search page
        kind += ("\nLe photographe a vérifié lui-même l'identification : "
                 + (', '.join(f'{fr} ({la})' if la else fr for fr, la in seq['given'])
                    or "l'oiseau ne peut pas être déterminé")
                 + ". Tiens-la pour acquise : ne propose aucune autre espèce, mets exactement "
                   'cela dans "especes" ([] pour un oiseau indéterminé) et décris la photo en '
                   'conséquence, sans décrire des caractères que tu ne vois pas.')
    if seq.get('people') or seq.get('guessed'):
        # the names come from faces.py, on this machine; Claude does not
        #   recognise anybody
        kind += ("\nPersonnes sur la photo, d'après les visages que le photographe a nommés : "
                 + (seq.get('people') or 'aucune de sûre')
                 + (f" ; peut-être aussi : {seq['guessed']}" if seq.get('guessed') else '')
                 + ". Nomme-les dans le titre ou la description, sans dire qui est qui si rien "
                   "ne le montre ; ne cherche à reconnaître personne d'autre.")
    return PROMPT.format(
        images='\n'.join(images),
        views=(VIEWS.replace(*SHARPEST) + BURST if burst
               else VIEWS + (SHEETS if len(images) > 2 else '')) + '.',
        count=len(names), start=f'{min(times):%Y-%m-%d %H:%M:%S}' if times else '?',
        end=f'{max(times):%Y-%m-%d %H:%M:%S}' if times else '?',
        gear=f' ({gear})' if gear else '', kind=kind, ebird=ebird_text(seq),
        instructions=instructions, topics=', '.join(f'"{topic}"' for topic in TOPICS))


def ask_claude(prompt, tmpdir, model=None, effort=None, tries=2, schema=None, system=None,
               needs=('titre', 'description')):
    """Claude's answer to a prompt, as (dict, cost in dollars at API prices);
    schema: that of the answer (the description of a sequence by default),
    needs: the keys it must not come back without"""
    cmd = ['claude', '-p', prompt, '--system-prompt', system or SYSTEM, '--tools', 'Read',
           '--allowedTools', 'Read', '--add-dir', tmpdir, '--output-format', 'json',
           '--json-schema', json.dumps(schema or SCHEMA), '--no-session-persistence',
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
            for key in needs:
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
    own = f'position de la photo : {seq["gps"].location}' if seq.get('gps') else ''
    if not seq.get('anchored', True) and not (match and match.checklist):
        return own or 'téléphone ou autre appareil : pas de liste eBird'
    if match and match.checklist:
        how = 'pendant' if match.kind == 'inside' else "à moins d'une heure de"
        return (f'{how} la liste eBird {match.checklist.sid}, {match.checklist.label()}{clock}'
                + (f'\n    {own}' if own else ''))
    return own or 'aucune liste eBird'


def panoramas(files, rows, gap, min_focal, dry_run):
    """stitch the sequences of the current directory that are panoramas
    (pano.py); the files made. In a dry run they are only said."""
    made = []
    if not pano.available():
        return made
    for seq in sequences(files, rows, gap, lambda row: _focal(row) > min_focal):
        names = seq['names']
        target = f'pano_{os.path.splitext(names[0])[0]}.jpg'
        if (seq['bird'] or not pano.FRAMES[0] <= len(names) <= pano.FRAMES[1]
                or os.path.exists(target)
                or any(pano.MARK in str(rows.get(name, {}).get('Model') or '') for name in names)):
            continue
        try:
            if not pano.sweep([_image(name, big=False) for name in names]):
                continue
            if dry_run:
                print(f'Panorama : {names[0]} .. {names[-1]} ({len(names)} vues), non assemblé '
                      'pendant un essai.')
                continue
            picture = pano.stitch([_image(name, big=True) for name in names],
                                  int(_cfg().get('pano_size') or 0))
        except Exception as exc:
            print(f'Panorama : {names[0]} .. {names[-1]} : {exc}')
            continue
        if picture is None:
            print(f"Panorama : {names[0]} .. {names[-1]} : les vues n'ont pas pu être assemblées.")
            continue
        made.append(pano.save(picture, names[0], '.', len(names)))
        print(f'Panorama : {names[0]} .. {names[-1]} ({len(names)} vues) assemblé en '
              f'{os.path.basename(made[-1])}, {picture.size[0]} x {picture.size[1]} px.')
    return made


def panorama_of(seq_id):
    """Stitch a sequence of the index where its files are (the button of the
    search page): the panorama is written next to its frames, under a
    canonical name, and added to the index; its row there, as a dict.
    Raises RuntimeError when the frames cannot be read or put together."""
    from .core import get_exif
    cfg = _cfg()
    db = photoindex.connect()
    frames = photoindex.sequence(db, seq_id)
    paths = [photoindex.resolve(frame['path']) for frame in frames]
    if not frames or not all(paths):
        raise RuntimeError("les fichiers sont hors d'atteinte (le disque d'archive est-il monté ?)")
    if not pano.FRAMES[0] <= len(paths) <= pano.FRAMES[1]:
        raise RuntimeError(f'un panorama a de {pano.FRAMES[0]} à {pano.FRAMES[1]} vues')
    pictures = [_image(path, big=True) for path in paths]
    if not pano.sweep(pictures):
        raise RuntimeError('ces vues ne se suivent pas comme celles d\'un panorama')
    picture = pano.stitch(pictures, int(cfg.get('pano_size') or 0))
    if picture is None:
        raise RuntimeError("les vues n'ont pas pu être assemblées")
    made = pano.save(picture, paths[0], os.path.dirname(paths[0]), len(paths))
    # its canonical name, as patchnames gives it
    final = os.path.join(os.path.dirname(made), get_exif(made)['REFERENCE NAME'])
    os.replace(made, final)
    name = os.path.basename(final)
    rows = {name: read_exif([final]).get(final, {})}
    data = _ebird.load()
    seqs = sequences([name], rows, cfg['keywords_gap'], lambda row: False)
    source = db.execute('SELECT clock_offset FROM photos WHERE seq = ? AND rep = 1',
                        (seq_id,)).fetchone()
    locate(seqs, data, rows, clock=(source['clock_offset'] if source else 0.0) or 0.0)
    kept = dict(said_of(seqs[0], rows, data), keywords=f'panorama{photoindex.SEP}{len(paths)} vues')
    photoindex.put(db, frame_records(seqs[0], kept, rows, {name: final}))
    photoindex.save_images(picture, name, preview=True)
    row = dict(db.execute('SELECT * FROM photos WHERE name = ?', (name,)).fetchone())
    db.close()
    return row


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
    if panoramas(files, rows, gap, min_focal, dry_run):
        # the panoramas just made are photos to describe like the others
        files = _photos()
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
            if big:
                # (the size of the photo, to say where its eye is: see single_eye)
                rows.setdefault(name, {})['_size'] = img.size
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
            if seq['bird'] and len(seq['names']) > 1:
                # every frame of a burst is measured and shown to Claude
                images, seq['burst'] = burst_pictures(
                    seq['names'], lambda name: _image(name, big=True), rows, tmpdir, index, keep)
            else:
                images = _pictures(seq['names'], tmpdir, index, rows, keep)
            middle = seq['names'][len(seq['names']) // 2]
            if (not seq['bird'] and not dry_run and faces.available()
                    and 'preview' in staged.get(middle, {})):
                # who is in the picture, from the faces named in the search page
                from PIL import Image
                when = _when(rows.get(middle, {}))
                seq['people'], seq['guessed'] = faces.early(
                    middle, Image.open(staged[middle]['preview']), when.isoformat(' ') if when else None)
            answer, cost = ask_claude(prompt_for(seq, images, rows, explanations), tmpdir,
                                      model, effort)
            if seq.get('burst'):
                # a second look, at the eye of each frame at full resolution
                cost += judge_eyes(seq, answer, tmpdir, index, model)
            with lock:
                spent['total'] += cost
            kept, checklist = settle(seq, answer, data)
            eyes = {}       # the square around the eye of each frame, for the loupe
            if seq.get('burst'):
                seq['notes'], seq['rep'] = frame_notes(seq['names'], seq['burst'], answer)
                eyes = seq['burst'].get('squares') or {}
                if seq['rep'] != seq['burst']['shown'] and not dry_run:
                    # the best frame stands for the sequence: its preview
                    keep(seq['rep'], _image(seq['rep'], big=True), True)
            elif seq['bird']:
                spot, square = single_eye(answer, rows.get(middle, {}), images[1])
                if spot:
                    seq['notes'], eyes = {middle: spot}, {middle: square}
            if not dry_run:
                for name, square in eyes.items():
                    out = os.path.join(tmpdir, f'eye_{name}.jpg')
                    square.save(out, quality=88)
                    staged.setdefault(name, {})['eye'] = out
            if not (dry_run or index_only):
                size = rows.get(seq['burst']['shown'] if seq.get('burst') else middle, {}).get('_size')
                write_groups([(file_tags(answer, dict(kept, _size=size, **notes), checklist), group)
                              for notes, group in _groups_of(seq['names'], seq.get('notes') or {})],
                             rows, backup)
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
                if seq.get('rep'):
                    mine = seq['notes'][seq['rep']]
                    flags.append(f"meilleure vue {_number(seq['rep'])} : "
                                 f"œil net {mine['eye'] or '?'}/5, dégagé {mine['clear'] or '?'}/5, "
                                 f"hauteur d'œil {mine['level'] or '?'}/5")
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
            middle = stands_for(seq, own_notes(seq, rows))
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
        if faces.available():
            faces.renamed(db, {name: os.path.basename(paths.get(name, name))
                               for index in answers for name in seqs[index]['names']})
        if described:
            photoindex.set_meta(db, _cost_key(model, effort), round(cost / len(described), 4))
        db.close()
        for index in answers:
            for name in seqs[index]['names']:
                final = os.path.basename(paths.get(name, name))
                for kind, target in (('thumb', photoindex.thumb_path(final)),
                                     ('preview', photoindex.preview_path(final)),
                                     ('eye', photoindex.eye_path(final))):
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
    data = _ebird.load(quiet=False)
    if data is None:
        print('Pas de données eBird : les photos sont indexées sans lieu (voir `patchdoc ebird`).')
    else:
        photoindex.sync_ebird(db, data)
    records, seqs = _from_names(found, known, gap, data)
    print(f"{len(found)} photos dans l'archive : {len(records)} nouvelles pour l'index, "
          f'{len(moved)} déplacées.')
    photoindex.put(db, moved + records)
    db.close()
    placed = sum(1 for seq in seqs if seq['match'] and seq['match'].checklist)
    print(f'{len(seqs)} séquences ajoutées, dont {placed} pendant une liste eBird.')


def _from_names(found, known, gap, data):
    """the records, for the index, of the photos of an archive it does not
    know yet, from their names alone: found, {name: (path, bird)}, known, the
    names it has. Returns (the records, their sequences)."""
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
    seqs = sequences(new, rows, gap, lambda row: row.get('bird'))
    locate(seqs, data)
    paths = {name: found[name][0] for name in new}
    records = []
    for seq in seqs:
        records += frame_records(seq, settle(seq, None, data)[0], rows, paths)
    for record in records:
        # the names say nothing of the camera
        for key in ('camera', 'lens', 'focal', 'zone'):
            record.pop(key, None)
    return records, seqs


def index_copy(root, gap=None):
    """Look at what a copy of the archive holds, for the search page to show
    it: every file of it is listed in one go (dupes.listing). The photos it
    has in the folders of the archive (20YY_MM/oiseaux or /autres) that the
    index did not know are added from their names, as `patchdoc index` does;
    the photos of the index it does not have are noted (table absent of the
    index). The paths the index has are left as they are. Returns {'files',
    'photos', 'new', 'absent', 'outside', 'read'}: its files, the photos of
    the index it holds, those just added, those it lacks, its pictures the
    index does not show (other folders, other names), and when it was read;
    the index keeps that too (meta 'copy:' and the root)."""
    from . import dupes
    gap = _cfg()['keywords_gap'] if gap is None else gap
    listed = dupes.listing(root, sizes=False) if os.path.isdir(root) else {}
    if not listed:
        # (a drive that went away must not make every photo absent)
        raise RuntimeError(f"{root} n'a donné aucun fichier (la copie est-elle branchée ?)")
    layout = re.compile(r'\d{4}_\d{2}/(oiseaux|autres)/[^/]+')
    db = photoindex.connect()
    try:
        known = photoindex.known(db)
        found = {}
        for path in listed:
            parsed, name = layout.fullmatch(path), os.path.basename(path)
            if parsed and name not in known and name.lower().endswith(_extensions()):
                found[name] = (os.path.join(root, path), parsed.group(1) == 'oiseaux')
        data = _ebird.load()
        if data:
            photoindex.sync_ebird(db, data)
        records, _ = _from_names(found, known, gap, data)
        photoindex.put(db, records)
        mine = ({path for path, _ in known.values()}
                | {photoindex.shorten(record['path']) for record in records})
        absent = [name for name, (path, _) in known.items()
                  if path and not os.path.isabs(path) and path not in listed]
        held = sum(1 for path, _ in known.values() if path and not os.path.isabs(path)) - len(absent)
        db.execute('DELETE FROM absent WHERE copy = ?', (root,))
        db.executemany('INSERT INTO absent VALUES (?, ?)', [(root, name) for name in absent])
        out = {'files': len(listed), 'photos': held + len(records), 'new': len(records),
               'absent': len(absent),
               'outside': sum(1 for path in listed
                              if path not in mine and path.lower().endswith(_extensions())),
               'read': datetime.now().isoformat(' ', 'minutes')}
        photoindex.set_meta(db, f'copy:{root}', json.dumps(out))
        return out
    finally:
        db.close()


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


def inbox_dirs(cfg=None):
    """the folders where photos wait before they are sent to the archive
    (config 'inbox_dirs')"""
    cfg = cfg or _cfg()
    return [os.path.abspath(os.path.expanduser(folder)) for folder in cfg.get('inbox_dirs') or []]


def inbox_files(cfg=None):
    """the photos that wait in those folders: the files directly in each
    (their own subfolders hold other things)"""
    found = []
    for folder in inbox_dirs(cfg):
        if os.path.isdir(folder):
            found += [os.path.join(folder, name) for name in sorted(os.listdir(folder))
                      if name.lower().endswith(_extensions()) and not name.startswith('.')
                      and os.path.isfile(os.path.join(folder, name))]
    return found


def index_inbox(tell=None, chunk=100):
    """Bring to the index the photos that wait to be sent to the archive,
    where they are, so that the search page can describe and note them
    before they go: what they say of themselves, their sequences and their
    eBird checklists, as `patchdoc index --exif` does for the archive.
    Nothing is renamed nor moved. The photos of those folders that are no
    longer there (sent to the archive since) leave the index; a photo whose
    name the index already has elsewhere is left out. tell(done, total) as
    the files are read. Returns {'files', 'new', 'gone', 'elsewhere'}."""
    cfg = _cfg()
    folders = tuple(folder + os.sep for folder in inbox_dirs(cfg))
    files = inbox_files(cfg)
    db = photoindex.connect()
    try:
        known = {row['name']: row['path'] or '' for row in db.execute('SELECT name, path FROM photos')}
        gone = [name for name, path in known.items()
                if path.startswith(folders) and not os.path.exists(path)]
        photoindex.forget(db, gone)
        elsewhere = [path for path in files
                     if known.get(os.path.basename(path)) not in (None, path)]
        todo = [path for path in files if os.path.basename(path) not in known]
        rows = {}
        for start in range(0, len(todo), chunk):
            rows.update({os.path.basename(src): row
                         for src, row in read_exif(todo[start:start + chunk]).items()})
            if tell:
                tell(min(start + chunk, len(todo)), len(todo))
        if rows:
            data = _ebird.load()
            if data:
                photoindex.sync_ebird(db, data)
            # the clock offset the index has for each day (the most common one last)
            before = {datetime.strptime(day, '%Y-%m-%d').date(): offset or 0.0
                      for day, offset, _ in db.execute(
                          'SELECT substr(taken, 1, 10), clock_offset, COUNT(*) FROM photos '
                          'WHERE bird = 1 AND taken IS NOT NULL GROUP BY 1, 2 ORDER BY 3')}
            paths = {os.path.basename(path): path for path in todo}
            seqs = sequences(sorted(rows), rows, cfg['keywords_gap'],
                             lambda row: _focal(row) > cfg['bird_focal_min'])
            locate(seqs, data, rows, before=before)
            records = []
            for seq in seqs:
                records += frame_records(seq, said_of(seq, rows, data), rows, paths)
            photoindex.put(db, records)
        return {'files': len(files), 'new': len(rows), 'gone': len(gone), 'elsewhere': len(elsewhere)}
    finally:
        db.close()


def rematch():
    """Match every sequence of the index with the eBird checklists again, from
    the index alone: after a new export (a list entered after its photos were
    indexed) or a change in the matching. What Claude said is kept, and so is
    a list Claude chose itself; photos placed by the position they carry
    may change checklist, not place. Only the photos of the camera of the
    checklists are tied to them (see anchored): the camera of the JPEG
    sequences the index was given from their names alone is read first
    from their files; those out of reach are left as they are. The files of
    the sequences whose checklist or place changed are to be written again."""
    data = _ebird.load(quiet=False)
    if data is None:
        print('Pas de données eBird (voir `patchdoc ebird`).')
        return
    db = photoindex.connect()
    photoindex.sync_ebird(db, data)
    learn_cameras(db)
    place = ('location', 'county', 'state', 'country', 'lat', 'lon')
    rows = db.execute('SELECT name, seq, taken, bird, camera, zone, latin, match, checklist, '
                      f'clock_offset, own_position, written, {", ".join(place)} FROM photos '
                      'WHERE rep = 1 AND taken IS NOT NULL').fetchall()
    seqs, unknown = [], 0
    for row in rows:
        parsed = NAME.match(row['name'])
        if row['camera'] is None and os.path.splitext(row['name'])[1].lower() in ('.jpg', '.jpeg'):
            unknown += 1        # (a JPEG whose camera is not known yet: left as it is)
            continue
        seqs.append({'names': [row['name']], 'bird': bool(row['bird']), 'zone': row['zone'],
                     'camera': row['camera'] or (parsed.group(3) if parsed else '') or '',
                     'when': datetime.fromisoformat(row['taken']), 'row': row,
                     # (the position the photo carries, which can tie it to a checklist)
                     'position': (row['lat'], row['lon']) if row['own_position']
                     and row['lat'] is not None else None})
    locate(seqs, data)
    changed, dropped = [], 0
    for seq in seqs:
        row = seq['row']
        kept, checklist = settle(seq, None, data)
        if (row['match'] == 'other' and seq['anchored']) or row['match'] in ('manual', 'detached'):
            continue        # (Claude's choice, or what the photographer did by hand)
        if row['own_position']:
            kept.update({key: row[key] for key in place}, match=kept['match'] or 'gps')
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
        if row['checklist'] and kept['checklist'] != row['checklist']:
            # (what Claude said of the photo against a checklist it is no longer tied to)
            db.execute("UPDATE photos SET note = '' WHERE seq = ?", (row['seq'],))
        if (kept['checklist'], kept['location']) != (row['checklist'], row['location'] or ''):
            # (what the files say of their checklist and their place is to be written again)
            db.execute("UPDATE photos SET written = 'pending' WHERE seq = ? AND written IS NOT NULL "
                       "AND written != 'pending'", (row['seq'],))
            dropped += bool(row['checklist']) and not seq['anchored']
        changed.append(row['name'])
    photoindex._derive(db, changed)
    db.commit()
    db.close()
    print(f"Liste eBird ou horloge changée pour {_count(len(changed), 'séquence')} "
          f'sur {len(seqs)}' + (f", dont {dropped} d'un autre appareil que celui des listes, "
                               'qui n\'y sont plus rattachées' if dropped else '') + '.'
          + (f" {_count(unknown, 'séquence')} JPEG dont l'appareil n'a pas pu être lu "
             '(disque non monté ?) : laissées telles quelles.' if unknown else ''))


def learn_cameras(db, chunk=400):
    """read from their files the camera of the JPEG sequences the index was
    given from their names alone (it tells the photos of the camera of the
    checklists from those of a phone: see anchored); '' for a file that
    names none. The files out of reach are left for another time."""
    rows = db.execute("SELECT name, path, seq FROM photos WHERE rep = 1 AND camera IS NULL "
                      "AND ext IN ('jpg', 'jpeg')").fetchall()
    if rows:
        print(f"Lecture de l'appareil de {_count(len(rows), 'séquence')} JPEG ...")
    done = 0
    for start in range(0, len(rows), chunk):
        part = {photoindex.resolve(row['path']): row for row in rows[start:start + chunk]}
        part.pop(None, None)
        if not part:
            continue
        for found in remote.rows(['-fast2', '-Model'], list(part)):
            row = part.get(found['SourceFile'])
            if row is not None:
                db.execute('UPDATE photos SET camera = ? WHERE seq = ? AND camera IS NULL',
                           (str(found.get('Model') or ''), row['seq']))
                done += 1
        db.commit()
        print(f'  {min(start + chunk, len(rows))}/{len(rows)}', end='\r', flush=True)
    if rows:
        print(f"\nAppareil lu pour {_count(done, 'séquence')} sur {len(rows)}.")


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
    row['_size'] = img.size         # (to say where the eye is: see single_eye)
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


def describe_sequence(seq_id, model=None, effort=None, sheet=12, progress=None):
    """Have Claude describe one sequence of the index, where its files are
    (the button of the search page): its row in the index, as a dict, and the
    cost in dollars at API prices. The answer goes to the index, and waits
    there for write_sequence to put it into the files. Every frame of a burst
    of bird photos is read, to note each one (progress(done, total) as they
    are, then progress(total, total, 'yeux') when Claude takes its second
    look, at the eyes); of another sequence, only the middle frame. Raises RuntimeError
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
    stored = db.execute('SELECT bird, clock_offset, verified, species, latin, match, checklist '
                        'FROM photos WHERE name = ?', (middle['name'],)).fetchone()
    bird = _focal(exif) > cfg['bird_focal_min'] if _focal(exif) else bool(stored['bird'])
    seq = {'names': names, 'camera': str(exif.get('Model') or ''), 'bird': bird,
           'when': _when(rows[middle['name']]), 'zone': _zone(exif)}
    if stored['verified'] is not None:
        # the species the photographer checked himself: Claude describes, it
        #   does not identify again
        seq['given'] = _pairs(stored['species'], stored['latin'])
    data = _ebird.load()
    # the clock offset the index found with the days around is kept
    locate([seq], data, rows, clock=stored['clock_offset'] or 0.0)
    if stored['match'] in ('manual', 'detached'):
        # (what the photographer said by hand stays)
        listed = data.lists.get(stored['checklist']) if data and stored['match'] == 'manual' else None
        seq['match'] = _ebird.Match(listed, 'manual', 0.0, seq['when'], []) if listed else None
    exifs = {middle['name']: exif}
    with tempfile.TemporaryDirectory() as tmpdir:
        if bird and len(frames) > 1:
            # a burst: the focus point and the full image of every frame
            folder = os.path.dirname(full)
            # (the frames next to the middle one are where it is: no need to
            #   ask the drive for each)
            where = {frame['name']: os.path.join(folder, os.path.basename(frame['path']))
                     if os.path.dirname(frame['path']) == os.path.dirname(middle['path'])
                     else photoindex.resolve(frame['path']) for frame in frames}

            # (asked FRAMES at a time, READS calls at once: each call costs
            #   a start of exiftool, each frame a trip of its picture)
            asked = ThreadPoolExecutor(max_workers=READS)
            parts = {}
            for start in range(0, len(names), FRAMES):
                part = [name for name in names[start:start + FRAMES] if where[name]]
                parts.update(dict.fromkeys(part, asked.submit(read_frames, [where[n] for n in part])))
            asked.shutdown(wait=False)

            def load(name):
                # (taken out of what the call brought: its picture is not kept twice)
                row, img = read_frame(where[name], parts[name].result().pop(where[name], None))
                exifs[name] = row
                rows[name] = dict(rows.get(name, {}), **row)
                return img

            if progress:
                progress(0, len(names))

            images, seq['burst'] = burst_pictures(
                names, load, rows, tmpdir, 0,
                lambda name, img, big: photoindex.save_images(img, name, preview=big), progress,
                jobs=READS)
        else:
            images = _archive_pictures(frames, full, tmpdir, rows[middle['name']], sheet)
        if not bird and faces.available():
            # who is in the picture, from the faces named in the search page
            from PIL import Image
            seq['people'], seq['guessed'] = faces.scan(
                db, middle['name'], Image.open(photoindex.preview_path(middle['name'])),
                middle['taken'])
        answer, cost = ask_claude(prompt_for(seq, images, rows, explanations), tmpdir,
                                  model or cfg.get('keywords_model'),
                                  effort or cfg.get('doc_effort'))
        if seq.get('given') is not None:
            answer.update(_named(seq['given']), note_ebird='')
            answer['autres_mots_cles'] = [word for word in answer.get('autres_mots_cles') or []
                                          if word != UNKNOWN] + [UNKNOWN] * (not seq['given'])
        if seq.get('burst'):
            # a second look, at the eye of each frame at full resolution
            if progress:
                progress(len(names), len(names), 'yeux')
            cost += judge_eyes(seq, answer, tmpdir, 0, model or cfg.get('keywords_model'))
        # where the eye is in a bird photo that is not part of a burst
        single = single_eye(answer, rows[middle['name']], images[1]) if bird and not seq.get(
            'burst') else (None, None)
    kept, _ = settle(seq, answer, data)
    notes, best = {}, None
    eyes = {middle['name']: single[1]} if single[0] else {}
    if seq.get('burst'):
        notes, best = frame_notes(names, seq['burst'], answer)
        eyes = seq['burst'].get('squares') or {}
        if best != seq['burst']['shown']:
            # the best frame stands for the sequence: the viewer opens on it
            try:
                photoindex.save_images(_image(where[best], big=True), best, preview=True)
            except Exception:
                pass
    for name, square in eyes.items():
        photoindex.save_eye(square, name)       # (what the loupe of the page shows)
    # 'pending': the files do not hold it yet (write_sequence)
    records = []
    for name in names:
        record = dict(kept, name=name, written='pending')
        if best:
            record.update(notes.get(name) or notes[best], rep=int(name == best),
                          best=int(name == best))
        elif bird:
            record.update(single[0] if single[0] and name == middle['name']
                          else {'eye_x': None, 'eye_y': None})
        if name in exifs:
            record.update(camera=str(exifs[name].get('Model') or ''), lens=_lens(exifs[name]),
                          focal=_focal(exifs[name]) or None, zone=_zone(exifs[name]))
        records.append(record)
    photoindex.put(db, records)
    row = dict(db.execute('SELECT * FROM photos WHERE seq = ? AND rep = 1', (seq_id,)).fetchone())
    db.close()
    return row, cost


def _pairs(species, latin):
    """the species of a sequence as the index keeps them, [(French, Latin)]"""
    names = [part for part in (species or '').split(photoindex.SEP) if part]
    latins = [part for part in (latin or '').split(photoindex.SEP)]
    return list(zip(names, latins + [''] * len(names)))


def _named(pairs):
    """the part of an answer that names these species: title, species and
    how sure (the photographer's word: sure)"""
    return {'titre': ', '.join(f'{fr} ({la})' if la else fr for fr, la in pairs) or UNKNOWN_TITLE,
            'especes': [{'francais': fr, 'latin': la} for fr, la in pairs], 'certitude': 'sûre'}


def correct_species(seq_id, old, new):
    """The photographer sets an identification right, in the search page: in
    a sequence, the species `old` (its French name; '' to add one to a
    sequence that names none) becomes `new` (a French name; '' for a bird
    that cannot be named; the same as `old` to confirm it). The Latin name
    comes from the eBird checklist of the sequence, or from any other. The
    title is made again, a description written for another species gives
    way to a plain line, and the files wait for write_sequence. Returns the
    row of the sequence."""
    db = photoindex.connect()
    try:
        row = db.execute('SELECT * FROM photos WHERE seq = ? AND rep = 1', (seq_id,)).fetchone()
        if row is None:
            raise RuntimeError('séquence inconnue')
        pairs = _pairs(row['species'], row['latin'])
        old, new = ' '.join((old or '').split()), ' '.join((new or '').split())
        at = next((k for k, (fr, _) in enumerate(pairs)
                   if photoindex.fold(fr) == photoindex.fold(old)), None)
        was = pairs[at] if at is not None else None
        pair = None
        if new:
            found = (db.execute('SELECT francais, latin FROM observations WHERE sid = ? AND '
                                'francais = ? COLLATE NOCASE', (row['checklist'], new)).fetchone()
                     or db.execute('SELECT francais, latin FROM observations WHERE francais = ? '
                                   'COLLATE NOCASE LIMIT 1', (new,)).fetchone())
            pair = (found['francais'], found['latin'] or '') if found else (new, '')
        if at is None:
            pairs += [pair] if pair else []
        elif pair:
            pairs[at] = pair
        else:
            del pairs[at]
        pairs = list(dict.fromkeys(pairs))

        named = _named(pairs)
        # a description written for another bird argues for it: it gives way to
        #   a plain line, until Claude describes the sequence again knowing
        #   the species (a confirmation leaves it alone)
        description = row['description'] or ''
        if pair != was:
            description = (f"{named['titre']}. Identification corrigée par le photographe"
                           + (f' (proposée d\'abord : {was[0]})' if was else '') + '.')
        words = [word for word in (row['keywords'] or '').split(photoindex.SEP)
                 if word and word != UNKNOWN and photoindex.fold(word) not in
                 {photoindex.fold(name) for name in (was or ()) if name}]
        listed = None
        if row['checklist'] and row['match'] in ('inside', 'other', 'manual'):
            class Listed:
                names = [found['latin'] for found in db.execute(
                    'SELECT latin FROM observations WHERE sid = ?', (row['checklist'],))]

                def latin_names(self):
                    return self.names
            listed = Listed()
        changes = {
            'title': named['titre'], 'species': photoindex.SEP.join(fr for fr, _ in pairs),
            'latin': photoindex.SEP.join(la for _, la in pairs),
            'topic': row['topic'] or 'oiseau', 'description': description or named['titre'],
            'certainty': 'sûre' if pairs else '',
            'keywords': photoindex.SEP.join(words + [UNKNOWN] * (not pairs)),
            'on_checklist': _on_list([la for _, la in pairs], listed), 'note': '',
            # (what was said first stays, whatever is corrected afterwards)
            'verified': row['verified'] or (was[0] if was else UNKNOWN),
            'written': 'pending'}
        names = [frame['name'] for frame in photoindex.sequence(db, seq_id)]
        photoindex.put(db, [dict(changes, name=name) for name in names])
        return dict(db.execute('SELECT * FROM photos WHERE seq = ? AND rep = 1', (seq_id,)).fetchone())
    finally:
        db.close()


def write_sequence(seq_id, progress=None, chunk=6):
    """Write into the files of a sequence what the index says of it: Claude's
    description if it has one, the place, the latitude and longitude of its
    eBird checklist, and the checklist as the event; photos that carry their
    position keep it, and get the place named from it. The files are rewritten
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
    said = dict(row, note=row['note'] or '')
    paths = {frame['name']: photoindex.resolve(frame['path']) for frame in frames}
    todo = [name for name in paths if paths[name]]
    if not todo:
        raise RuntimeError("les fichiers sont hors d'atteinte (le disque d'archive est-il monté ?)")
    # the frames of a burst each have their notes: they are written by groups
    #   of frames that share them
    notes = {frame['name']: {key: frame[key] for key in OWN} for frame in frames}
    # the frames of a sequence come from one camera: the middle one says
    #   whether they carry a position of their own, and how large they are
    middle = paths[todo[len(todo) // 2]]
    seen = read_exif([middle], fast=True).get(middle, {})
    own = own_position(seen)
    # a file tied by patchdoc to a checklist it is no longer tied to (the
    #   photo of a phone): the checklist, and the place and the position
    #   that came from it, are taken out of it
    clear = []
    if not row['checklist'] and str(seen.get('Event') or '').startswith('eBird S'):
        clear = ['-XMP-iptcExt:Event=']
        if not row['location']:
            clear += [f'-{tag}=' for tag in (
                'XMP-iptcCore:Location', 'IPTC:Sub-location', 'XMP-photoshop:City', 'IPTC:City',
                'XMP-photoshop:State', 'IPTC:Province-State', 'XMP-photoshop:Country',
                'IPTC:Country-PrimaryLocationName', 'XMP-iptcCore:CountryCode')]
        if str(seen.get('GPSAreaInformation') or '').startswith(GPS_MARK):
            clear += [f'-{tag}=' for tag in ('GPSLatitude', 'GPSLatitudeRef', 'GPSLongitude',
                                             'GPSLongitudeRef', 'GPSMapDatum', 'GPSAreaInformation')]
    if own and not row['own_position']:
        # photos indexed from their names, their position unknown until now:
        #   their place is named from it, here and in the index
        try:
            spot = geo.spot(float(seen['GPSLatitude']), float(seen['GPSLongitude']), data, checklist)
        except (KeyError, TypeError, ValueError):
            spot = None
        if spot:
            placed = dict(_place(spot), own_position=1, match=row['match'] or 'gps')
            photoindex.put(db, [dict(placed, name=frame['name']) for frame in frames])
            said.update(placed)
    if not file_tags(answer_of(row), said, checklist) and not clear:
        raise RuntimeError("la base n'a rien à écrire pour cette séquence (ni description, "
                           'ni liste eBird)')
    rows = {paths[name]: ({'GPSLatitude': 0} if own else {}) for name in todo}
    try:
        size = tuple(int(side) for side in str(seen.get('ImageSize') or '').split('x'))
        said['_size'] = size if len(size) == 2 and all(size) else None
    except ValueError:
        said['_size'] = None
    db.execute("UPDATE photos SET written = 'pending' WHERE seq = ?", (seq_id,))
    db.commit()
    done = 0
    for start in range(0, len(todo), chunk):
        # (each frame its own tags, a few frames to a run of exiftool)
        part = todo[start:start + chunk]
        # (what is taken out first: a place found since is written after it, and stays)
        write_groups([(clear + file_tags(answer_of(row), dict(said, **mine), checklist),
                       [paths[name] for name in group]) for mine, group in _groups_of(part, notes)],
                     rows, backup=bool(cfg.get('gui_backup')))
        db.execute(f'UPDATE photos SET written = ? WHERE name IN ({", ".join("?" * len(part))})',
                   [datetime.now().isoformat(' ', 'seconds')] + part)
        db.commit()
        done += len(part)
        if progress:
            progress(done, len(todo))
    db.close()
    return done


#: the Orientation of a file once its picture is turned a quarter turn
#:  clockwise; anticlockwise is the same table read the other way
CLOCKWISE = {1: 6, 6: 3, 3: 8, 8: 1, 2: 7, 7: 4, 4: 5, 5: 2}
#: the rotation a HEIC file keeps beside its Orientation (quarter turns
#:  anticlockwise), which is what macOS follows
HEIC_ROTATION = {1: 0, 8: 1, 3: 2, 6: 3}


def attach(seq_id, sid=None):
    """Tie a sequence to an eBird checklist by hand (sid), or untie it (None):
    what the photographer knows and the matching cannot (the photo of a
    phone taken on the outing, a JPEG that lost the name of its camera, a
    photo tied to the wrong list). It stays so whatever the matching says
    later (match 'manual' or 'detached'). The place is that of the checklist,
    unless the photos carry a position of their own; the clock is left as
    it is; the files wait to be written. Returns the row of the sequence."""
    db = photoindex.connect()
    try:
        row = db.execute('SELECT * FROM photos WHERE seq = ? AND rep = 1', (seq_id,)).fetchone()
        if row is None:
            raise RuntimeError('séquence inconnue')
        changes = {'note': '', 'written': 'pending'}
        if sid:
            listed = db.execute('SELECT * FROM checklists WHERE sid = ?', (sid,)).fetchone()
            if listed is None:
                raise RuntimeError('liste eBird inconnue')

            class Listed:
                names = [found['latin'] for found in db.execute(
                    'SELECT latin FROM observations WHERE sid = ?', (sid,))]

                def latin_names(self):
                    return self.names
            changes.update(checklist=sid, match='manual',
                           on_checklist=_on_list((row['latin'] or '').split(photoindex.SEP), Listed()))
            if not row['own_position']:
                changes.update({key: listed[key] for key in
                                ('location', 'county', 'state', 'country', 'lat', 'lon')})
        else:
            changes.update(checklist=None, match='detached', on_checklist=None)
            if not row['own_position']:
                changes.update(location='', county='', state='', country='', lat=None, lon=None)
        names = [frame['name'] for frame in photoindex.sequence(db, seq_id)]
        photoindex.put(db, [dict(changes, name=name) for name in names])
        return dict(db.execute('SELECT * FROM photos WHERE seq = ? AND rep = 1', (seq_id,)).fetchone())
    finally:
        db.close()


def _turn_kept(db, name, full, quarter):
    """make again, from its file, the pictures the index keeps of a photo
    whose file now says another way up, and turn with them (`quarter`
    quarter turns clockwise) what is placed on them: the eye of the bird and
    the faces. The page is told to ask for the pictures again."""
    big = os.path.isfile(photoindex.preview_path(name))
    photoindex.save_images(_image(full, big=big), name, preview=big)
    if os.path.isfile(photoindex.eye_path(name)):
        os.remove(photoindex.eye_path(name))        # (made again when it is asked for)
    row = db.execute('SELECT eye_x, eye_y, turned FROM photos WHERE name = ?', (name,)).fetchone()
    eye = (row['eye_x'], row['eye_y'])
    if quarter % 4 and None not in eye:
        eye = tuple(round(value, 4) for value in turned(eye[0], eye[1], quarter))
        # (the place of the eye written in the file is to be written again)
        db.execute("UPDATE photos SET written = 'pending' WHERE name = ? AND written IS NOT NULL",
                   (name,))
    db.execute('UPDATE photos SET turned = ?, upright = 1, eye_x = ?, eye_y = ? WHERE name = ?',
               ((row['turned'] or 0) + 1, eye[0], eye[1], name))
    if quarter % 4:
        for face in db.execute('SELECT id, x, y, w, h FROM faces WHERE photo = ?', (name,)).fetchall():
            db.execute('UPDATE faces SET x = ?, y = ?, w = ?, h = ? WHERE id = ?',
                       turned_box(face['x'], face['y'], face['w'], face['h'], quarter) + (face['id'],))
    db.commit()


def straighten(stop=None, tell=None, chunk=100):
    """Make upright the pictures the index keeps of photos whose file says
    they are to be turned. Until 2026-10-13 that was only followed for JPEG
    files: a NEF, a DNG or a HEIC taken with the camera on its side lay on
    its side in the page. The Orientation of each file whose thumbnail dates
    from before is read (the newest first, `chunk` files to a run of
    exiftool); the pictures of those that are to be turned are made again,
    and the eye and the faces placed on them turn with them. A run that is
    stopped goes on from there the next time; photos out of reach wait.
    tell(counts) as it goes. Returns {'checked', 'turned', 'left'}."""
    db = photoindex.connect()
    try:
        since = photoindex.meta('upright_since')
        if not since:
            since = str(time.time())
            photoindex.set_meta(db, 'upright_since', since)
        since = float(since)
        # (a JPEG was always seen as it says)
        db.execute("UPDATE photos SET upright = 1 WHERE upright IS NULL AND ext IN ('jpg', 'jpeg')")
        db.commit()
        rows = db.execute('SELECT name, path FROM photos WHERE upright IS NULL '
                          'ORDER BY COALESCE(local, taken) DESC').fetchall()
        fine, check = [], []
        for row in rows:
            thumb = photoindex.thumb_path(row['name'])
            old = os.path.isfile(thumb) and os.path.getmtime(thumb) < since
            (check if old else fine).append(row)
        # (no picture kept, or one made since: nothing to make again)
        db.executemany('UPDATE photos SET upright = 1 WHERE name = ?', [(row['name'],) for row in fine])
        db.commit()
        out = {'checked': 0, 'turned': 0, 'left': len(check)}
        for start in range(0, len(check), chunk):
            if stop is not None and stop.is_set():
                break
            fulls = {photoindex.resolve(row['path']): row['name'] for row in check[start:start + chunk]}
            fulls.pop(None, None)
            said = {row['SourceFile']: row.get('Orientation')
                    for row in remote.rows(['-fast2', '-Orientation#'], list(fulls))} if fulls else {}
            for full, name in fulls.items():
                if full not in said:
                    continue        # (not read this time)
                try:
                    orientation = int(said[full] or 1)
                except (TypeError, ValueError):
                    orientation = 1
                if orientation == 1:
                    db.execute('UPDATE photos SET upright = 1 WHERE name = ?', (name,))
                else:
                    try:
                        _turn_kept(db, name, full, quarters(orientation))
                    except Exception:
                        continue    # (a picture that cannot be made now waits)
                    out['turned'] += 1
                out['checked'] += 1
                out['left'] -= 1
            db.commit()
            if tell:
                tell(dict(out))
        return out
    finally:
        db.close()


def turn(name, way, whole=False):
    """Turn a photo a quarter turn, clockwise (way 'cw') or anticlockwise
    ('ccw'), or every frame of its sequence (whole). Nothing of the picture
    itself is rewritten: the Orientation of the file is, which says how it
    is to be seen (and, for a HEIC, the rotation of its container, which
    macOS follows). The pictures the index keeps, the eye and the faces
    follow. Returns the names of the photos turned; raises RuntimeError."""
    if way not in ('cw', 'ccw'):
        raise RuntimeError('sens inconnu')
    table = CLOCKWISE if way == 'cw' else {new: old for old, new in CLOCKWISE.items()}
    db = photoindex.connect()
    try:
        row = db.execute('SELECT seq FROM photos WHERE name = ?', (name,)).fetchone()
        if row is None:
            raise RuntimeError('photo inconnue')
        frames = [frame for frame in photoindex.sequence(db, row['seq'])
                  if whole or frame['name'] == name]
        fulls = {photoindex.resolve(frame['path']): frame['name'] for frame in frames}
        if None in fulls or not fulls:
            raise RuntimeError("le fichier est hors d'atteinte (le disque d'archive est-il monté ?)")
        now = {found['SourceFile']: found.get('Orientation')
               for found in remote.rows(['-fast2', '-Orientation#'], list(fulls))}
        head = ['-q', '-m', '-n'] + ([] if _cfg().get('gui_backup') else ['-overwrite_original'])
        wanted, blocks = {}, {}
        for full in fulls:
            try:
                wanted[full] = table[int(now.get(full) or 1)]
            except (KeyError, TypeError, ValueError):
                wanted[full] = table[1]
            args = [f'-Orientation={wanted[full]}']
            if full.lower().endswith('.heic') and wanted[full] in HEIC_ROTATION:
                args.append(f'-Rotation={HEIC_ROTATION[wanted[full]]}')
            blocks.setdefault(tuple(args), []).append(full)
        code, _, err, _ = remote.run_many([(head + list(args), files) for args, files in blocks.items()])
        after = {found['SourceFile']: found.get('Orientation')
                 for found in remote.rows(['-fast2', '-Orientation#'], list(fulls))}
        done = []
        for full, frame in fulls.items():
            if str(after.get(full)) != str(wanted[full]):
                continue
            _turn_kept(db, frame, full, 1 if way == 'cw' else 3)
            done.append(frame)
        if not done:
            raise RuntimeError(f"l'orientation n'a pas pu être écrite : {err.strip()[:200] or code}")
        return done
    finally:
        db.close()


#: the photos Claude is shown at once to tell those that lie on their side
ORIENT_SHEET = 16
ORIENT_PROMPT = """Voici une planche de {count} photos numérotées, chacune avec son numéro en jaune en haut à gauche de sa case. Lis-la avec l'outil Read : {sheet}

Pour chaque photo, dis si elle est à l'endroit. La plupart le sont. Une photo est couchée quand le haut de la scène est sur un côté (un horizon vertical, des personnes, des arbres ou des bâtiments couchés, un texte qui se lit de bas en haut ou de haut en bas), ou à l'envers. Pour une photo de document, de page ou d'écran, l'endroit est celui où le texte se lit normalement. Regarde chaque case une à une : un texte, une étiquette, un livre ou un écran dont les lettres sont verticales trahit une photo couchée. Un oiseau photographié en vol ou la tête en bas, un écureuil qui descend un tronc, une vue prise vers le ciel ou vers le sol ne sont pas couchés. Le photographe préfère quelques photos tournées à tort à des photos couchées oubliées : dans le doute, signale-la.

Réponds par la liste des photos à tourner ("a_tourner", vide s'il n'y en a pas), chacune avec le sens dans lequel la tourner pour la remettre à l'endroit : "horaire" (un quart de tour dans le sens des aiguilles d'une montre), "antihoraire" (un quart de tour dans l'autre sens) ou "demi-tour"."""
ORIENT_SCHEMA = {'type': 'object', 'required': ['a_tourner'], 'properties': {'a_tourner': {
    'type': 'array', 'items': {'type': 'object', 'required': ['n', 'sens'], 'properties': {
        'n': {'type': 'integer'},
        'sens': {'type': 'string', 'enum': ['horaire', 'antihoraire', 'demi-tour']}}}}}}


def _orient_sheet(names, path):
    """the thumbnails of photos on one numbered sheet, for Claude to look at
    (each in a square, as it is, without being turned to fill it)"""
    from PIL import Image, ImageDraw
    cell, columns = 330, 4
    rows = (len(names) + columns - 1) // columns
    page = Image.new('RGB', (cell * columns, cell * rows), (40, 40, 40))
    draw = ImageDraw.Draw(page)
    for k, name in enumerate(names):
        thumb = Image.open(photoindex.thumb_path(name)).convert('RGB')
        thumb.thumbnail((cell - 14, cell - 14))
        x, y = (k % columns) * cell, (k // columns) * cell
        page.paste(thumb, (x + (cell - thumb.width) // 2, y + (cell - thumb.height) // 2))
        draw.rectangle((x + 2, y + 2, x + 34, y + 24), fill=(0, 0, 0))
        draw.text((x + 7, y + 6), str(k + 1), fill='yellow')
    page.save(path, quality=88)


def orient_left(db):
    """how many sequences orient has yet to look at, now"""
    return db.execute("SELECT COUNT(*) FROM photos WHERE rep = 1 AND oriented IS NULL AND "
                      "(upright = 1 OR ext IN ('jpg', 'jpeg'))").fetchone()[0]


def orient(stop=None, tell=None, model=None, effort=None, most=None, dry=False, hold=None):
    """Have Claude find the photos that lie on their side or upside down
    (an old camera without a sensor of its position, a document photographed
    flat) and turn them as the arrows of the page do: only the Orientation
    of their files is rewritten (see turn), for every frame of the sequence.
    Claude sees the thumbnails the index keeps, ORIENT_SHEET to a sheet, the
    newest first; a photo whose thumbnail is not yet as its file says (see
    straighten) waits. What Claude found is kept (column oriented): 'ok', or
    'claude' for a photo it turned, which the page finds again. most: at most
    that many photos; dry: say what would be turned, change nothing; hold(seq):
    see inbox._free. tell(counts) after each sheet. Returns {'looked',
    'turned', 'left', 'cost', 'found'}, found: [(name, way)]."""
    from contextlib import nullcontext
    hold = hold or (lambda seq: nullcontext(True))
    model = model or _cfg().get('orient_model')
    effort = effort or _cfg().get('orient_effort')
    db = photoindex.connect()
    try:
        rows = [row for row in db.execute(
            "SELECT name, seq FROM photos WHERE rep = 1 AND oriented IS NULL AND "
            "(upright = 1 OR ext IN ('jpg', 'jpeg')) ORDER BY COALESCE(local, taken) DESC")
            if os.path.isfile(photoindex.thumb_path(row['name']))]
        rows = rows[:most] if most else rows
        out = {'looked': 0, 'turned': 0, 'left': len(rows), 'cost': 0.0, 'found': []}
        ways = {'horaire': ['cw'], 'antihoraire': ['ccw'], 'demi-tour': ['cw', 'cw']}
        with tempfile.TemporaryDirectory() as tmpdir:
            for start in range(0, len(rows), ORIENT_SHEET):
                if stop is not None and stop.is_set():
                    break
                part = rows[start:start + ORIENT_SHEET]
                sheet = os.path.join(tmpdir, f'planche_{start}.jpg')
                _orient_sheet([row['name'] for row in part], sheet)
                answer, cost = ask_claude(ORIENT_PROMPT.format(count=len(part), sheet=sheet), tmpdir,
                                          model, effort, schema=ORIENT_SCHEMA, needs=())
                out['cost'] += cost
                turned = {}
                for item in answer.get('a_tourner') or []:
                    try:
                        turned[part[int(item['n']) - 1]['seq']] = ways[item['sens']]
                    except (KeyError, IndexError, TypeError, ValueError):
                        continue
                states = {}
                for row in part:
                    if row['seq'] in turned:
                        out['found'].append((row['name'], ' '.join(turned[row['seq']])))
                    if dry:
                        continue
                    states[row['seq']] = 'ok'
                    if row['seq'] in turned:
                        # (turned before anything is written here: turn writes with its own
                        #   connection to the index)
                        with hold(row['seq']) as mine:
                            if not mine:
                                del states[row['seq']]
                                continue
                            try:
                                for way in turned[row['seq']]:
                                    turn(row['name'], way, whole=True)
                                states[row['seq']] = 'claude'
                                out['turned'] += 1
                            except RuntimeError:
                                del states[row['seq']]      # (its files out of reach: it waits)
                db.executemany('UPDATE photos SET oriented = ? WHERE seq = ?',
                               [(state, seq) for seq, state in states.items()])
                db.commit()
                out['looked'] += len(part)
                out['left'] -= len(part)
                if tell:
                    tell(dict(out, found=len(out['found'])))
        return out
    finally:
        db.close()


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
                             '(config doc_budget ; sans plafond par défaut).')
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

    p_dupes = sub.add_parser(
        'doublons', help="Les images présentes deux fois dans l'archive (la même image, une "
                         "autre heure) : dire laquelle est la bonne d'après les listes eBird, et "
                         'ranger les autres dans un dossier doublons.')
    p_dupes.add_argument('dossiers', nargs='*', metavar='DOSSIER',
                         help="Ne regarder que les doublons qui touchent ces dossiers de l'archive "
                              '(2026_07, mexico2026...) ; toute l\'archive sans cela.')
    p_dupes.add_argument('--deplacer', action='store_true',
                         help="Ranger les fichiers en trop dans le dossier doublons de l'archive "
                              "(rien n'est effacé) et mettre les JPG et DNG à l'heure de leur NEF ; "
                              'sans cela, seulement dire ce qui serait fait.')

    p_backup = sub.add_parser(
        'sauvegarde', help='Sauvegarder la base de recherche sur GitHub, dans un dépôt privé : '
                           'compressée et chiffrée avec un mot de passe, sans les vignettes.')
    p_backup.add_argument('--mot-de-passe', dest='password', action='store_true',
                          help='Demander le mot de passe des sauvegardes et le garder dans le '
                               'trousseau de macOS (à faire une fois).')
    p_backup.add_argument('--restaurer', metavar='FICHIER', default=None,
                          help='Relire une sauvegarde téléchargée, dans photos.restauree.sqlite '
                               '(le mot de passe est demandé, avec son indice, si le trousseau '
                               'ne l\'a pas).')
    p_backup.add_argument('--indice', metavar='TEXTE', default=None,
                          help='L\'indice du mot de passe, montré quand il est demandé : gardé '
                               'dans votre config et dans les notes du dépôt privé ("" le retire).')

    p_orient = sub.add_parser(
        'redresser', help='Faire trouver par Claude les photos couchées ou à l\'envers, et les '
                          'tourner (seule l\'étiquette Orientation du fichier est récrite).')
    p_orient.add_argument('--max', type=int, default=None, help='Au plus ce nombre de photos.')
    p_orient.add_argument('--essai', action='store_true',
                          help='Dire seulement ce qui serait tourné, sans rien changer.')
    p_orient.add_argument('--modele', default=None,
                          help='Le modèle de Claude (config orient_model, sonnet).')

    p_app = sub.add_parser(
        'app', help="Faire l'application patchdoc et la mettre dans le Dock : un clic démarre "
                    "la page de recherche et l'ouvre.")
    p_app.add_argument('--sans-dock', action='store_true',
                       help="Faire l'application dans ~/Applications sans toucher au Dock.")

    args = parser.parse_args()
    if args.cmd == 'redresser':
        said = lambda out: print(f"  {out['looked']} regardées, {out['turned']} tournées, "
                                 f"{out['left']} à regarder", flush=True)
        out = orient(tell=said, model=args.modele, most=args.max, dry=args.essai)
        for name, way in out['found']:
            print(f"  {'à tourner' if args.essai else 'tournée'} : {name} ({way})")
        print(f"Claude : {out['cost']:.2f} $ US au prix de l'API.")
        return
    if args.cmd == 'app':
        from . import macapp
        macapp.install(dock=not args.sans_dock)
        return
    if args.cmd == 'sauvegarde':
        from . import backup
        backup.main(set_it=args.password, back=args.restaurer, said=args.indice)
        return
    if args.cmd == 'doublons':
        from . import dupes
        dupes.main(folders=args.dossiers, move=args.deplacer)
        return
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
