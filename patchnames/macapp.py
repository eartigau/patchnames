"""
An icon in the Dock for the search page: `patchdoc app`.

It makes patchdoc.app in ~/Applications, a small AppleScript application
whose only work is to run a shell script kept inside it: if the search page
is not running, `patchdoc gui` is started in the background (what it says
goes to ~/Library/Logs/patchdoc.log), then the page is opened in the
browser. A second click on the icon only opens the page. The application
is added to the Dock.

An application started from the Dock does not have the PATH of a terminal:
the script takes the one of the terminal `patchdoc app` was run in, so that
the page finds exiftool, claude, gh and rsync as it does there. Run
`patchdoc app` again after moving any of them, or patchdoc itself.

The page is stopped from its own footer ("Arrêter patchdoc"), since there
is no terminal to press Ctrl-C in.
"""

import os
import plistlib
import shlex
import shutil
import subprocess
import sys
import tempfile

from . import config as _config_module

NAME = 'patchdoc'
#: what the application does: run the script inside it, and say what went wrong
SCRIPT = '''try
    do shell script quoted form of ((POSIX path of (path to me)) & "Contents/Resources/lancer.sh")
on error what
    display dialog "patchdoc : " & what buttons {"D'accord"} default button 1 with icon caution
end try
'''
LAUNCHER = '''#!/bin/sh
# made by `patchdoc app`: start the search page of patchdoc if it is not
# running, then open it in the browser
export PATH=%(path)s
# (what the page says reaches the log as it says it)
export PYTHONUNBUFFERED=1
%(config)sURL="http://127.0.0.1:%(port)s/"
if ! curl -s -o /dev/null --max-time 2 "${URL}api/batch"; then
    mkdir -p "$HOME/Library/Logs"
    nohup %(patchdoc)s gui --no-browser >> "$HOME/Library/Logs/patchdoc.log" 2>&1 &
    tries=0
    until curl -s -o /dev/null --max-time 2 "${URL}api/batch"; do
        tries=$((tries + 1))
        if [ "$tries" -gt 40 ]; then
            echo "la page de recherche ne démarre pas (voir ~/Library/Logs/patchdoc.log)" >&2
            exit 1
        fi
        sleep 0.5
    done
fi
open "$URL"
'''


def _icon(folder):
    """the icon of the application, a lens on the dark blue of the page, as
    an .icns file; None when it cannot be made (the application then keeps
    the icon of a script)"""
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        return None
    size = 1024
    # the rounded square of a macOS icon, with the banner's gradient
    shade = Image.new('RGB', (size, size))
    draw = ImageDraw.Draw(shade)
    for y in range(size):
        t = y / (size - 1)
        draw.line([(0, y), (size, y)], fill=(int(13 + 15 * t), int(26 + 32 * t), int(46 + 48 * t)))
    mask = Image.new('L', (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle((100, 100, 924, 924), radius=185, fill=255)
    icon = Image.new('RGBA', (size, size), (0, 0, 0, 0))
    icon.paste(shade, (0, 0), mask)
    draw = ImageDraw.Draw(icon)
    # a camera seen from the front: its body, its lens, a light on the lens
    draw.rounded_rectangle((250, 330, 774, 720), radius=70, outline=(98, 194, 255, 255), width=34)
    draw.rounded_rectangle((400, 262, 624, 345), radius=30, fill=(98, 194, 255, 255))
    draw.ellipse((382, 395, 642, 655), outline=(232, 238, 248, 255), width=34)
    draw.ellipse((462, 475, 562, 575), fill=(108, 226, 173, 255))
    try:
        iconset = os.path.join(folder, f'{NAME}.iconset')
        os.makedirs(iconset)
        for side in (16, 32, 128, 256, 512):
            icon.resize((side, side), Image.LANCZOS).save(os.path.join(iconset, f'icon_{side}x{side}.png'))
            icon.resize((2 * side, 2 * side), Image.LANCZOS).save(
                os.path.join(iconset, f'icon_{side}x{side}@2x.png'))
        made = os.path.join(folder, f'{NAME}.icns')
        done = subprocess.run(['iconutil', '-c', 'icns', iconset, '-o', made], capture_output=True)
        return made if done.returncode == 0 else None
    except OSError:
        return None


def in_dock(app):
    """whether the Dock has an application"""
    said = subprocess.run(['defaults', 'read', 'com.apple.dock', 'persistent-apps'],
                          capture_output=True, text=True).stdout
    return os.path.basename(app) in said


def to_dock(app):
    """add an application to the Dock (which starts again to show it)"""
    entry = ('<dict><key>tile-data</key><dict><key>file-data</key><dict><key>_CFURLString</key>'
             f'<string>{app}</string><key>_CFURLStringType</key><integer>0</integer></dict></dict></dict>')
    done = subprocess.run(['defaults', 'write', 'com.apple.dock', 'persistent-apps', '-array-add', entry],
                          capture_output=True)
    if done.returncode == 0:
        subprocess.run(['killall', 'Dock'], capture_output=True)
    return done.returncode == 0


def install(dock=True):
    """`patchdoc app`: make the application and put it in the Dock"""
    if sys.platform != 'darwin':
        print("L'application et le Dock sont ceux de macOS : rien à faire ici.")
        return
    patchdoc = shutil.which('patchdoc') or os.path.join(os.path.dirname(sys.executable), 'patchdoc')
    if not os.path.isfile(patchdoc):
        print("La commande patchdoc est introuvable : l'application ne saurait pas quoi lancer.")
        return
    cfg = _config_module.load()
    app = os.path.join(os.path.expanduser('~/Applications'), f'{NAME}.app')
    os.makedirs(os.path.dirname(app), exist_ok=True)
    with tempfile.TemporaryDirectory() as folder:
        source = os.path.join(folder, 'patchdoc.applescript')
        with open(source, 'w') as handle:
            handle.write(SCRIPT)
        fresh = os.path.join(folder, f'{NAME}.app')
        made = subprocess.run(['osacompile', '-o', fresh, source], capture_output=True, text=True)
        if made.returncode != 0:
            print(f"L'application n'a pas pu être faite : {made.stderr.strip()}")
            return
        other = os.environ.get('PATCHNAMES_CONFIG')
        launcher = os.path.join(fresh, 'Contents', 'Resources', 'lancer.sh')
        with open(launcher, 'w') as handle:
            handle.write(LAUNCHER % {
                'path': shlex.quote(os.environ.get('PATH', '/usr/bin:/bin')),
                'config': f'export PATCHNAMES_CONFIG={shlex.quote(other)}\n' if other else '',
                'port': int(cfg['gui_port']), 'patchdoc': shlex.quote(patchdoc)})
        os.chmod(launcher, 0o755)
        plist = os.path.join(fresh, 'Contents', 'Info.plist')
        with open(plist, 'rb') as handle:
            info = plistlib.load(handle)
        info.update(CFBundleName=NAME, CFBundleDisplayName=NAME,
                    CFBundleIdentifier='io.github.eartigau.patchdoc')
        icon = _icon(folder)
        if icon:
            shutil.copyfile(icon, os.path.join(fresh, 'Contents', 'Resources', 'applet.icns'))
            # (the icon of a script, which the application would show instead)
            info.pop('CFBundleIconName', None)
            catalog = os.path.join(fresh, 'Contents', 'Resources', 'Assets.car')
            if os.path.isfile(catalog):
                os.remove(catalog)
        with open(plist, 'wb') as handle:
            plistlib.dump(info, handle)
        # (what was changed inside is signed again, for this machine only)
        subprocess.run(['codesign', '--force', '--deep', '-s', '-', fresh], capture_output=True)
        if os.path.isdir(app):
            # (the one made here before gives way; any other is left alone)
            if not os.path.isfile(os.path.join(app, 'Contents', 'Resources', 'lancer.sh')):
                print(f"{app} existe et n'a pas été faite par patchdoc : elle est laissée telle "
                      'quelle, rien n\'est fait.')
                return
            shutil.rmtree(app)
        shutil.move(fresh, app)
    print(f"Application faite : {app}. Un clic démarre la page de recherche si elle ne tourne pas, "
          'puis l\'ouvre ; ce que dit le serveur va dans ~/Library/Logs/patchdoc.log.')
    if dock and in_dock(app):
        print('Elle est déjà dans le Dock.')
    elif dock:
        print('Elle est ajoutée au Dock (qui redémarre pour la montrer).' if to_dock(app)
              else f'Elle n\'a pas pu être ajoutée au Dock : glissez-y {app}.')
