"""
Keywords of bird photos with Claude: the species (French and Latin names),
the habitat and a short description, written into the files themselves
(EXIF, IPTC and XMP).

How it works:

1. the photos of the current directory taken with a bird lens (config
   'bird_lenses'; --all-lenses for every photo) and not yet described (no
   XMP title; --force to redo them) are grouped into sequences: a new
   sequence after `gap` seconds without a shot;
2. for each sequence, a contact sheet of its frames (the frame numbers
   written on them) and its middle frame at 1400 px are made in a temporary
   folder;
3. the claude command line, in print mode and allowed to read files only,
   looks at them, follows the explanations of an MD file
   (~/.config/patchnames/keywords.md, yours to edit: region, names,
   conventions) and answers in JSON;
4. exiftool writes the answer into every frame of the sequence: title,
   keywords (species, Latin name, habitat, and 'identification à confirmer'
   when Claude is not sure), a Lightroom hierarchy (Espèce, Espèce latine,
   Habitat) and the description. exiftool keeps each original as
   <file>_original unless --no-backup.

One Claude call per sequence: a burst of a hundred frames costs the same as
one photo. The claude command must be logged in (it uses your Claude Code
account).
"""

import glob
import io
import json
import os
import subprocess
import tempfile
from datetime import datetime

from . import config as _config_module

#: the image files looked at
IMAGE_PATTERNS = ('*.nef', '*.dng', '*.jpg', '*.jpeg', '*.heic')
#: the frames of a contact sheet at most (sampled evenly beyond)
SHEET_MAX = 36
#: said when Claude is not sure of the species
UNSURE = 'identification à confirmer'

TEMPLATE = """# Explications pour les mots-clés des photos d'oiseaux

Ce fichier est lu par `patchnames keywords` et transmis tel quel à Claude,
qui décrit chaque séquence de photos. Modifiez-le à votre goût : tout ce qui
est écrit ici guide l'identification et la rédaction.

## Contexte

- Photographe : ornithologue amateur, surtout au Québec (sud du Québec,
  région de Montréal), avec des voyages occasionnels (par exemple le
  Mexique, Yucatán).
- Appareil : Nikon Z50 II, objectif 200-500 mm.

## Noms

- Noms français : ceux de la liste des oiseaux du Québec et de la
  Commission internationale des noms français des oiseaux (par exemple
  « Paruline masquée », « Bruant familier », « Petit Chevalier »).
- Noms latins : taxinomie Clements (eBird).
- Si seul le genre est sûr : le nom du genre en français (« grive ») et en
  latin (« Catharus sp. »), et les espèces candidates dans la description.

## Certitude

- Ne pas deviner : si l'identification n'est pas sûre, certitude
  « probable » ou « à confirmer », et nommer les candidats dans la
  description (par exemple : « le Grand Chevalier n'est pas exclu »).
- S'il n'y a pas d'oiseau identifiable, le dire (titre « aucun oiseau
  identifié »), sans inventer d'espèce.

## Habitat

- Un à trois mots simples : vasière, marais, milieu humide, eau peu profonde,
  lac, plan d'eau, rivière, lagune, friche, arbustes, sous-bois, forêt,
  canopée, champ, milieu urbain, clôture...

## Description

- Une ou deux phrases courtes en français (40 mots au plus) : ce que fait
  l'oiseau, le milieu, la lumière ou la météo, et les caractères qui ont
  servi à l'identifier.
- Ajouter « Migration automnale » ou « Migration printanière » quand la date
  et l'espèce s'y prêtent.

## Autres mots-clés

- Au plus trois, utiles pour retrouver les photos : comportement (en vol,
  alimentation), plumage (juvénile, nuptial), groupe (limicole, rapace).
"""

PROMPT = """Tu décris une séquence de photos d'oiseaux pour des mots-clés.

Lis ces images avec l'outil Read :
{images}

La première est la vue du milieu de la séquence, en grand ; les suivantes
sont des planches contact de toutes les vues de la séquence (le numéro de
chaque vue est écrit dessus), pour vérifier qu'il s'agit du même sujet.

La séquence : {count} vues, du {start} au {end}{lens}.

Suis ces explications du photographe :

---
{instructions}
---

Réponds uniquement par un objet JSON, sans texte autour :
{{"titre": "nom français (nom latin)",
  "especes": [{{"francais": "...", "latin": "..."}}],
  "habitat": ["..."],
  "description": "une ou deux phrases courtes en français, 40 mots au plus",
  "certitude": "sûre" ou "probable" ou "à confirmer",
  "autres_mots_cles": ["..."]}}
Mets "especes" à [] s'il n'y a aucun oiseau identifiable."""


def _cfg():
    return _config_module.load()


def instructions_path():
    """the MD file of the explanations (config 'keywords_instructions')"""
    return os.path.expanduser(_cfg()['keywords_instructions'])


def write_template(path=None, quiet=False):
    """write the default explanations if the file does not exist yet"""
    path = path or instructions_path()
    if os.path.exists(path):
        return path
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w') as handle:
        handle.write(TEMPLATE)
    if not quiet:
        print(f'Explanations for Claude written to {path} -- edit them to your taste.')
    return path


def _exif(files):
    """time, lens and title of each file (one exiftool call)"""
    out = subprocess.run(['exiftool', '-j', '-q', '-DateTimeOriginal', '-LensModel',
                          '-LensID', '-LensInfo', '-Lens', '-XMP-dc:Title'] + files,
                         capture_output=True, text=True).stdout
    rows = json.loads(out) if out.strip() else []
    return {os.path.basename(row['SourceFile']): row for row in rows}


def _when(row):
    try:
        return datetime.strptime(str(row.get('DateTimeOriginal', ''))[:19],
                                 '%Y:%m:%d %H:%M:%S')
    except ValueError:
        return None


def _is_bird_lens(row, bird_lenses):
    text = ' '.join(str(row.get(key, '')) for key in ('LensModel', 'LensID', 'LensInfo', 'Lens'))
    return any(lens in text for lens in bird_lenses)


def _groups(files, rows, gap):
    """the sequences: a new one after `gap` seconds without a shot"""
    dated = sorted((_when(rows.get(f, {})), f) for f in files if _when(rows.get(f, {})))
    undated = [f for f in files if not _when(rows.get(f, {}))]
    groups, current, last = [], [], None
    for when, name in dated:
        if current and (when - last).total_seconds() > gap:
            groups.append(current)
            current = []
        current.append(name)
        last = when
    if current:
        groups.append(current)
    # a file without a date is its own sequence
    return groups + [[name] for name in undated]


def _image(path, big):
    """the embedded preview of a raw (or the image itself), upright"""
    from PIL import Image, ImageOps
    if path.lower().endswith(('.jpg', '.jpeg')):
        return ImageOps.exif_transpose(Image.open(path)).convert('RGB')
    tags = ('-JpgFromRaw', '-PreviewImage') if big else ('-PreviewImage', '-JpgFromRaw')
    for tag in tags + ('-ThumbnailImage',):
        raw = subprocess.run(['exiftool', '-b', tag, path], capture_output=True).stdout
        try:
            return ImageOps.exif_transpose(Image.open(io.BytesIO(raw))).convert('RGB')
        except Exception:
            continue
    # HEIC and others: let macOS convert it
    out = os.path.join(tempfile.mkdtemp(), 'frame.jpg')
    subprocess.run(['sips', '-s', 'format', 'jpeg', path, '--out', out], capture_output=True)
    return Image.open(out).convert('RGB')


def _pictures(names, tmpdir, index):
    """the middle frame at 1400 px and the contact sheets of a sequence"""
    from PIL import Image, ImageDraw
    paths = []
    middle = names[len(names) // 2]
    rep = _image(middle, big=True)
    rep.thumbnail((1400, 1400))
    path = os.path.join(tmpdir, f'seq{index:03d}_middle.jpg')
    rep.save(path, quality=88)
    paths.append(path)
    if len(names) < 2:
        return paths
    if len(names) <= 2 * SHEET_MAX:
        pick = list(names)
    else:
        pick = [names[round(i * (len(names) - 1) / (2 * SHEET_MAX - 1))]
                for i in range(2 * SHEET_MAX)]
    for isheet in range(0, len(pick), SHEET_MAX):
        chunk = pick[isheet:isheet + SHEET_MAX]
        sheet = Image.new('RGB', (1400, 930), 'white')
        draw = ImageDraw.Draw(sheet)
        for k, name in enumerate(chunk):
            img = _image(name, big=False)
            img.thumbnail((231, 153))
            x, y = (k % 6) * 233, (k // 6) * 155
            sheet.paste(img, (x, y))
            draw.text((x + 3, y + 2), os.path.splitext(name)[0].split('_')[-1], fill='yellow')
        path = os.path.join(tmpdir, f'seq{index:03d}_sheet{isheet // SHEET_MAX}.jpg')
        sheet.save(path, quality=85)
        paths.append(path)
    return paths


def _ask_claude(images, names, rows, instructions, tmpdir, model=None):
    """Claude's answer for one sequence, as a dict"""
    times = [t for t in (_when(rows.get(n, {})) for n in names) if t]
    lens = rows.get(names[0], {}).get('LensModel') or rows.get(names[0], {}).get('LensID')
    prompt = PROMPT.format(
        images='\n'.join(images), count=len(names),
        start=f'{min(times):%Y-%m-%d %H:%M:%S}' if times else '?',
        end=f'{max(times):%Y-%m-%d %H:%M:%S}' if times else '?',
        lens=f', objectif {lens}' if lens else '', instructions=instructions)
    cmd = ['claude', '-p', prompt, '--tools', 'Read', '--allowedTools', 'Read',
           '--add-dir', tmpdir, '--output-format', 'json', '--no-session-persistence']
    if model:
        cmd += ['--model', model]
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
    if res.returncode != 0:
        raise RuntimeError(f'claude failed: {res.stderr.strip()[:300]}')
    text = json.loads(res.stdout).get('result', '')
    start, end = text.find('{'), text.rfind('}')
    if start < 0 or end < start:
        raise RuntimeError(f'no JSON in the answer: {text[:300]}')
    answer = json.loads(text[start:end + 1])
    for key in ('titre', 'description'):
        if not answer.get(key):
            raise RuntimeError(f'the answer has no {key}: {answer}')
    return answer


def _tags(answer):
    """the exiftool arguments of an answer (lists set, not appended)"""
    species = answer.get('especes') or []
    habitat = answer.get('habitat') or []
    words = []
    for sp in species:
        words += [sp.get('francais', ''), sp.get('latin', '')]
    words += habitat + list(answer.get('autres_mots_cles') or [])
    if species and answer.get('certitude', 'sûre') != 'sûre':
        words.append(UNSURE)
    words = [w for w in dict.fromkeys(w.strip() for w in words) if w]
    hier = []
    for sp in species:
        if sp.get('francais'):
            hier.append(f'Espèce|{sp["francais"]}')
        if sp.get('latin'):
            hier.append(f'Espèce latine|{sp["latin"]}')
    hier += [f'Habitat|{h}' for h in habitat if h]
    title, desc = answer['titre'], answer['description']
    args = ['-charset', 'iptc=UTF8', '-codedcharacterset=utf8',
            f'-XMP-dc:Title={title}', f'-XMP-dc:Description={desc}',
            f'-IPTC:ObjectName={title[:64]}', f'-IPTC:Caption-Abstract={desc}',
            f'-EXIF:ImageDescription={desc}', f'-EXIF:UserComment={desc}',
            f'-EXIF:XPTitle={title}', f'-EXIF:XPComment={desc}',
            f'-EXIF:XPKeywords={";".join(words)}']
    # repeated '=' on a list tag in one command sets exactly that list ('+='
    #   would add to what the file already holds)
    for tag, values in (('-XMP-dc:Subject', words), ('-IPTC:Keywords', words),
                        ('-XMP-lr:HierarchicalSubject', hier)):
        args += [f'{tag}={value}' for value in values] or [f'{tag}=']
    return args


def keywords(gap=None, force=False, all_lenses=False, dry_run=False, backup=True,
             instructions=None, model=None):
    """Describe the bird photos of the current directory with Claude (see the module)."""
    cfg = _cfg()
    gap = cfg['keywords_gap'] if gap is None else gap
    model = model or cfg.get('keywords_model')
    path = instructions or instructions_path()
    if not instructions:
        write_template(path)
    with open(os.path.expanduser(path)) as handle:
        explanations = handle.read()

    files = sorted({f for pattern in IMAGE_PATTERNS
                    for f in glob.glob(pattern) + glob.glob(pattern.upper())
                    if os.path.isfile(f)})
    if not files:
        print('No photo in this directory.')
        return
    rows = _exif(files)
    if not all_lenses:
        skipped = [f for f in files if not _is_bird_lens(rows.get(f, {}), cfg['bird_lenses'])]
        files = [f for f in files if f not in skipped]
        if skipped:
            print(f'{len(skipped)} photos without a bird lens skipped (--all-lenses to include them).')
    if not force:
        done = [f for f in files if rows.get(f, {}).get('Title')]
        files = [f for f in files if f not in done]
        if done:
            print(f'{len(done)} photos already described skipped (--force to redo them).')
    if not files:
        print('Nothing to describe.')
        return
    groups = _groups(files, rows, gap)
    print(f'{len(files)} photos in {len(groups)} sequences.')

    with tempfile.TemporaryDirectory() as tmpdir:
        for index, names in enumerate(groups):
            print(f'\n[{index + 1}/{len(groups)}] {names[0]} .. {names[-1]}  ({len(names)} photos)')
            try:
                images = _pictures(names, tmpdir, index)
                answer = _ask_claude(images, names, rows, explanations, tmpdir, model)
            except Exception as exc:
                print(f'    ERROR: {exc}')
                continue
            print(f'    {answer["titre"]}  [{answer.get("certitude", "?")}]')
            print(f'    {answer["description"]}')
            if dry_run:
                continue
            cmd = ['exiftool', '-q'] + (['-overwrite_original'] if not backup else [])
            res = subprocess.run(cmd + _tags(answer) + names, capture_output=True, text=True)
            if res.returncode != 0:
                print(f'    ERROR writing: {res.stderr.strip()[:300]}')
            else:
                print(f'    written into {len(names)} files.')
