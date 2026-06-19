import argparse
import sys

from .core import patchnames, time_offset, patchdate, nef_to_dng
from . import config as _config


def main():
    parser = argparse.ArgumentParser(
        prog='patchnames',
        description='Rename and archive photo/video files using EXIF metadata.',
    )
    sub = parser.add_subparsers(dest='cmd')

    sub.add_parser('nef-to-dng', help='Convert Nikon Z50 II NEF files to DNG.')

    p_offset = sub.add_parser('time-offset', help='Shift EXIF timestamps by a fixed offset.')
    p_offset.add_argument('glob', help='Glob pattern, e.g. "*.NEF"')
    p_offset.add_argument(
        'offset', nargs='?',
        help='Signed offset: "+=HH:MM:SS" or "-=HH:MM:SS". Prompted if omitted.',
    )

    p_date = sub.add_parser('patchdate', help='Force a date onto files with no EXIF date.')
    p_date.add_argument('glob', help='Glob pattern, e.g. "*.jpg"')

    sub.add_parser('init-config', help='Create a default config at ~/.config/patchnames/config.json.')

    args = parser.parse_args()

    if args.cmd is None:
        patchnames()
    elif args.cmd == 'nef-to-dng':
        nef_to_dng()
    elif args.cmd == 'time-offset':
        time_offset(args.glob, args.offset)
    elif args.cmd == 'patchdate':
        patchdate(args.glob)
    elif args.cmd == 'init-config':
        _config.init()
    else:
        parser.print_help()
        sys.exit(1)
