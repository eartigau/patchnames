import argparse
import sys

from .core import patchnames, time_offset, patchdate, nef_to_dng
from . import config as _config
from .keywords import keywords


def main():
    parser = argparse.ArgumentParser(
        prog='patchnames',
        description='Rename and archive photo/video files using EXIF metadata.',
    )
    parser.add_argument(
        '--keywords', action='store_true',
        help='Describe the bird photos with Claude before renaming them '
             '(or set "keywords_auto": true in the config).',
    )
    sub = parser.add_subparsers(dest='cmd')

    p_kw = sub.add_parser(
        'keywords',
        help='Describe the bird photos with Claude: species (French, Latin), habitat, '
             'description, written into the files.',
    )
    p_kw.add_argument('--gap', type=float, default=None,
                      help='Seconds without a shot that start a new sequence (config, 30).')
    p_kw.add_argument('--force', action='store_true',
                      help='Describe again the photos that already have a title.')
    p_kw.add_argument('--all-lenses', action='store_true',
                      help='Every photo, not only those taken with a bird lens.')
    p_kw.add_argument('--dry-run', action='store_true',
                      help="Show Claude's answers without writing anything.")
    p_kw.add_argument('--no-backup', action='store_true',
                      help='Do not keep the originals as <file>_original.')
    p_kw.add_argument('--instructions', default=None,
                      help='The MD file of explanations (~/.config/patchnames/keywords.md).')
    p_kw.add_argument('--model', default=None, help='The Claude model (default of claude).')

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
        if args.keywords or _config.load().get('keywords_auto'):
            keywords()
        patchnames()
    elif args.cmd == 'keywords':
        keywords(gap=args.gap, force=args.force, all_lenses=args.all_lenses,
                 dry_run=args.dry_run, backup=not args.no_backup,
                 instructions=args.instructions, model=args.model)
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
