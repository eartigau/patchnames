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

    p_nef = sub.add_parser('nef-to-dng', help='Convert Nikon Z50 II NEF files to DNG.')
    p_nef.add_argument(
        '--convert-without-jpg', action='store_true',
        help='Also convert NEF files that have no JPG companion (off by default).',
    )

    p_offset = sub.add_parser('time-offset', help='Shift EXIF timestamps by a fixed offset.')
    p_offset.add_argument('glob', help='Glob pattern, e.g. "*.NEF"')
    p_offset.add_argument(
        'offset', nargs='?',
        help='Signed offset: "+=HH:MM:SS" or "-=HH:MM:SS". Prompted if omitted.',
    )

    p_date = sub.add_parser('patchdate', help='Force a date onto files with no EXIF date.')
    p_date.add_argument('glob', help='Glob pattern, e.g. "*.jpg"')

    sub.add_parser('init-config', help='Create a default config at ~/.config/patchnames/config.json.')

    argv = sys.argv[1:]
    if len(argv) >= 2 and argv[0] == 'time-offset' and argv[1] not in ('-h', '--help'):
        # The offset value ("+=HH:MM:SS" / "-=HH:MM:SS") starts with a dash,
        # which argparse would otherwise mistake for an option flag.
        argv = [argv[0], '--'] + argv[1:]
    args = parser.parse_args(argv)

    if args.cmd is None:
        patchnames()
    elif args.cmd == 'nef-to-dng':
        nef_to_dng(convert_without_jpg=args.convert_without_jpg)
    elif args.cmd == 'time-offset':
        time_offset(args.glob, args.offset)
    elif args.cmd == 'patchdate':
        patchdate(args.glob)
    elif args.cmd == 'init-config':
        _config.init()
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == '__main__':
    main()
