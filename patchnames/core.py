"""
Core photo/video file management logic.

Canonical filename format: YYMMDD_HHMMSS[_WHO]_FILENUM.ext
    260429_143022_EA_0234.nef
    260429_143022_0234.jpg
"""

import glob
import hashlib
import os
import shutil
import time

from . import config as _config_module

_cfg = None


def _cfg_get():
    global _cfg
    if _cfg is None:
        _cfg = _config_module.load()
    return _cfg


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def get_checksum(filename, hash_function='md5'):
    hash_function = hash_function.lower()
    with open(filename, 'rb') as f:
        data = f.read()
    if hash_function == 'md5':
        return hashlib.md5(data).hexdigest()
    elif hash_function == 'sha256':
        return hashlib.sha256(data).hexdigest()
    else:
        raise ValueError(f'{hash_function!r} is not supported. Use "md5" or "sha256".')


def who(hdr):
    if 'CAMERAMODELNAME' not in hdr:
        return ''
    model = hdr['CAMERAMODELNAME']
    camera_owners = _cfg_get()['camera_owners']
    for initials, models in camera_owners.items():
        for m in models:
            if m in model:
                return '_' + initials
    return ''


def get_exif(file, exif_folder=None):
    if exif_folder is None:
        exif_folder = os.path.expanduser(_cfg_get()['exif_cache_dir'])

    checksum = get_checksum(file)
    os.makedirs(exif_folder, exist_ok=True)
    exif_path = os.path.join(exif_folder, checksum + '.csv')

    if not os.path.isfile(exif_path):
        os.system(f'exiftool {file} > {exif_path}')
        if os.path.getsize(exif_path) < 100:
            time.sleep(0.02)

    hdr = {'CORR': False}
    with open(exif_path, 'rb') as ff:
        for raw_line in ff:
            line = str(raw_line).replace('\\n', '').replace("b'", '')
            parts = line.split(': ')
            if len(parts) < 2:
                continue
            if 'adobe' in parts[1].lower():
                hdr['CORR'] = True
            key = ''.join(parts[0].upper().split()).replace('/', '')
            val = ''.join(parts[1].split('\n')).replace("'", '')
            hdr[key] = val

    dates = []
    for key, val in hdr.items():
        if 'DATE' not in key:
            continue
        if '0000' in val or 'PROFILE' in key or ' ' not in val:
            continue
        dates.append(val)
    dates.sort()
    hdr['REFERENCE DATE'] = dates[0]
    hdr['DATE STRING'] = hdr['REFERENCE DATE'].split(' ')[0].replace(':', '')[2:]
    hdr['TIME STRING'] = hdr['REFERENCE DATE'].split(' ')[1].replace(':', '')[:6]

    if 'FILENUMBER' not in hdr:
        hdr['FILENUMBER'] = checksum[:8]
    elif '-' in hdr['FILENUMBER']:
        hdr['FILENUMBER'] = hdr['FILENUMBER'].split('-')[-1]

    who_suffix = who(hdr)
    ext = file.split('.')[-1].lower().replace('jpeg', 'jpg').replace('tiff', 'tif')
    hdr['REFERENCE NAME'] = (
        f"{hdr['DATE STRING']}_{hdr['TIME STRING']}{who_suffix}_{hdr['FILENUMBER']}.{ext}"
    )

    if 'LENSSPEC' in hdr and 'LENSINFO' not in hdr:
        hdr['LENSINFO'] = hdr['LENSSPEC']

    return hdr


# ---------------------------------------------------------------------------
# Workflows
# ---------------------------------------------------------------------------

def nef_to_dng():
    """Convert Nikon Z50 II NEF files in the current directory to DNG."""
    dng_converter = _cfg_get()['dng_converter']
    if not os.path.isfile(dng_converter):
        print('Adobe DNG Converter not found -- skipping NEF->DNG conversion.')
        return

    nef_files = glob.glob('*.NEF') + glob.glob('*.nef')
    for nef in nef_files:
        try:
            hdr = get_exif(nef)
        except Exception as exc:
            print(f'  Could not read EXIF for {nef}: {exc}')
            continue

        model = hdr.get('CAMERAMODELNAME', '')
        if not any(m in model for m in ['Z50_2', 'Z 50 II']):
            continue

        dng = os.path.splitext(nef)[0] + '.dng'
        if os.path.isfile(dng):
            print(f'  {dng} already exists -- skipping.')
            continue

        base = os.path.splitext(nef)[0]
        if os.path.isfile(base + '.jpg') or os.path.isfile(base + '.JPG'):
            print(f'  JPG companion exists for {nef} -- skipping DNG conversion.')
            continue

        cmd = f'"{dng_converter}" -d . -o "{dng}" "{nef}"'
        print(f'  Converting {nef} -> {dng}')
        os.system(cmd)


def time_offset(search_string, offset_h=None):
    """Shift EXIF timestamps for all files matching *search_string*.

    offset_h: signed offset string, e.g. '+=02:03:10' or '-=00:30:00'.
    If omitted, the user is prompted interactively.
    """
    fics = sorted(glob.glob(search_string))

    if offset_h is None:
        print('Enter the time offset.  Example: "+02:03:10" adds 2 h 3 min 10 s.')
        raw = input('Offset [+HH:MM:SS or -HH:MM:SS] : ')
        offset_h = raw.replace('-', '-=').replace('+', '+=')
    else:
        if not (offset_h.startswith('+=') or offset_h.startswith('-=')):
            print('Offset must start with "+=" or "-=".  Example: "+=02:03:10"')
            return

    os.makedirs('offset_files', exist_ok=True)

    for i, fic in enumerate(fics):
        print(f'[{i:5d}/{len(fics):5d}]  {fic}')
        cmd = (
            f'exiftool -EXIF:DateTimeOriginal{offset_h} '
            f'-EXIF:CreateDate{offset_h} '
            f'-EXIF:ModifyDate{offset_h} {fic}'
        )
        print(f'    {cmd}')
        os.system(cmd)

        hdr = get_exif(fic)
        target = os.path.join('offset_files', hdr['REFERENCE NAME'])

        if fic != hdr['REFERENCE NAME']:
            if not os.path.isfile(target):
                shutil.move(fic, target)
                print(f'    moved -> {target}')
            elif os.stat(fic).st_size == os.stat(target).st_size:
                os.remove(fic)
                print(f'    deleted duplicate: {fic}')
            else:
                print(f'    {hdr["REFERENCE NAME"]} already exists with different size -- skipped.')
        else:
            print('    filename unchanged.')

    os.makedirs('original_files', exist_ok=True)
    os.system('mv *_original original_files 2>/dev/null')


def patchdate(search_string):
    """Force a specific date onto files that carry no usable EXIF date."""
    fics = glob.glob(search_string)

    datestr = input('Date [YYMMDD] : ').strip()
    if len(datestr) != 6 or not datestr.isdigit():
        print('Invalid date.  Expected 6 digits, e.g. "260429".')
        return

    yr = '20' + datestr[0:2]
    mo = datestr[2:4]
    da = datestr[4:6]
    date_full = f'{yr}:{mo}:{da} 12:00:00'

    for fic in fics:
        exif = get_exif(fic)
        for key in exif:
            if 'DATE' in key and 'FILE' not in key:
                cmd = (
                    f'exiftool "-{key.lower()}={date_full}" '
                    f'"-CreateDate={date_full}" '
                    f'"-ModifyDate={date_full}" {fic}'
                )
                print(cmd)
                os.system(cmd)
                backup = fic + '_original'
                if os.path.isfile(backup):
                    os.remove(backup)


def patchnames(extensions=None):
    """Rename and archive all photo/video files in the current directory."""
    cfg = _cfg_get()
    if extensions is None:
        extensions = cfg['extensions']

    photo_archive_root = os.path.expanduser(cfg['photo_archive_root'])
    bird_lenses = cfg['bird_lenses']

    nef_to_dng()

    # Deduplicated case-insensitive patterns.
    seen = {}
    for e in extensions:
        seen[e.lower()] = None
    patterns = list(seen.keys()) + [e.upper() for e in seen]

    for pattern in patterns:
        fics = [f for f in glob.glob(pattern) if os.path.isfile(f)]
        if not fics:
            continue

        for i, fic in enumerate(fics):
            clean = (
                fic.replace(' ', '')
                   .replace('(', '')
                   .replace(')', '')
                   .replace("'", '_')
            )
            if fic != clean:
                os.rename(fic, clean)
                fics[i] = clean

        fics.sort()
        print(f'\nProcessing pattern: {pattern}  ({len(fics)} files)')

        for i, fic in enumerate(fics):
            print(f'  {i + 1:5d}/{len(fics):5d}  {fic}')
            try:
                hdr = get_exif(fic)

                suffix = 'autres'
                if 'LENSINFO' in hdr:
                    for lens_key in bird_lenses:
                        if lens_key in hdr['LENSINFO']:
                            suffix = 'oiseaux'
                            break

                target = hdr['REFERENCE NAME']
                if fic != target:
                    if not os.path.isfile(target):
                        shutil.move(fic, target)
                        print(f'    renamed -> {target}')
                    elif os.stat(fic).st_size == os.stat(target).st_size:
                        os.remove(fic)
                        print(f'    deleted duplicate: {fic}')
                    else:
                        print(f'    {target} already exists with different size -- skipped.')
                else:
                    print('    filename already canonical.')

                if os.path.isdir(photo_archive_root):
                    year_month = '20' + target[0:2] + '_' + target[2:4]
                    outdir = os.path.join(photo_archive_root, year_month, suffix)
                    os.makedirs(outdir, exist_ok=True)
                    os.system(f'mv -f "{target}" "{outdir}/"')
                    print(f'    archived -> {outdir}/')
                else:
                    print('    external drive not connected -- file kept locally.')

            except Exception as exc:
                print(f'    ERROR processing {fic}: {exc}')
                time.sleep(0.05)
