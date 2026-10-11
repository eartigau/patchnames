"""
Panoramas: a sequence shot by turning the camera, each frame overlapping
the next, is stitched into one picture.

sweep() tells such a sequence from a burst of the same view: between two
successive frames it looks for the points they share and for the move from
one to the other. A panorama moves the same way from frame to frame, by a
good part of the frame but not all of it (OVERLAP), and far enough in all
(TOTAL of a frame). stitch() hands the frames to OpenCV's stitcher, reduced
to config 'pano_size' pixels on their long side (3000; 0 for their full
size, slow and hungry), and trims the black edges the projection leaves.

The panorama is a JPEG next to its frames, dated like the first of them and
with the same lens and position; its camera model is followed by
"(panorama)", which makes it a sequence of its own and says what it is.
"""

import os
import subprocess

#: the move between two frames of a panorama, as a fraction of the frame
OVERLAP = (0.08, 0.92)
#: the move from the first frame to the last, at least
TOTAL = 0.35
#: a panorama has this many frames
FRAMES = (2, 40)
#: added to the camera model of a panorama
MARK = ' (panorama)'


def available():
    try:
        import cv2
        return hasattr(cv2, 'Stitcher') and hasattr(cv2, 'ORB_create')
    except ImportError:
        return False


def _gray(picture, size=640):
    import numpy as np
    small = picture.convert('L')
    small.thumbnail((size, size))
    return np.asarray(small)


def _move(a, b):
    """the move from a frame to the next, (dx, dy) as fractions of the
    frame; None when they do not show the same thing"""
    import cv2
    import numpy as np
    orb = cv2.ORB_create(2000)
    ka, da = orb.detectAndCompute(a, None)
    kb, db = orb.detectAndCompute(b, None)
    if da is None or db is None or len(ka) < 30 or len(kb) < 30:
        return None
    matches = cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=True).match(da, db)
    if len(matches) < 25:
        return None
    pa = np.float32([ka[m.queryIdx].pt for m in matches])
    pb = np.float32([kb[m.trainIdx].pt for m in matches])
    found, kept = cv2.estimateAffinePartial2D(pa, pb, method=cv2.RANSAC, ransacReprojThreshold=4.0)
    if found is None or kept is None or int(kept.sum()) < 15:
        return None
    scale = float(np.hypot(found[0, 0], found[1, 0]))
    if not 0.8 < scale < 1.25:
        return None
    return float(found[0, 2]) / a.shape[1], float(found[1, 2]) / a.shape[0]


def sweep(pictures):
    """whether these frames (PIL images, in order) are a panorama"""
    if not FRAMES[0] <= len(pictures) <= FRAMES[1]:
        return False
    grays = [_gray(picture) for picture in pictures]
    moves = [_move(a, b) for a, b in zip(grays, grays[1:])]
    if any(move is None for move in moves):
        return False
    axis = 0 if sum(abs(m[0]) for m in moves) >= sum(abs(m[1]) for m in moves) else 1
    steps = [move[axis] for move in moves]
    one_way = all(step > 0 for step in steps) or all(step < 0 for step in steps)
    return (one_way and all(OVERLAP[0] <= abs(step) <= OVERLAP[1] for step in steps)
            and abs(sum(steps)) >= TOTAL)


def stitch(pictures, size=3000):
    """the panorama of these frames, a PIL image; None when OpenCV cannot
    put them together"""
    import cv2
    import numpy as np
    from PIL import Image
    frames = []
    for picture in pictures:
        frame = picture.convert('RGB')
        if size:
            frame.thumbnail((size, size))
        frames.append(np.asarray(frame)[:, :, ::-1].copy())
    status, out = cv2.Stitcher.create(cv2.Stitcher_PANORAMA).stitch(frames)
    if status != 0 or out is None:
        return None
    # the rows and columns that are mostly picture, not black edge
    lit = out.max(axis=2) > 8
    rows = np.where(lit.mean(axis=1) > 0.85)[0]
    cols = np.where(lit.mean(axis=0) > 0.85)[0]
    if len(rows) and len(cols):
        out = out[rows[0]:rows[-1] + 1, cols[0]:cols[-1] + 1]
    return Image.fromarray(out[:, :, ::-1].copy())


def save(picture, first, folder, count):
    """write a panorama next to its frames, with the date, lens and position
    of the first of them (a file); its path"""
    stem = os.path.splitext(os.path.basename(first))[0]
    path = os.path.join(folder, f'pano_{stem}.jpg')
    picture.save(path, quality=92)
    model = subprocess.run(['exiftool', '-s3', '-Model', first], capture_output=True,
                           text=True).stdout.strip()
    subprocess.run(['exiftool', '-q', '-m', '-overwrite_original', '-tagsfromfile', first,
                    '-DateTimeOriginal', '-CreateDate', '-ModifyDate', '-OffsetTimeOriginal',
                    '-Make', '-LensModel', '-FocalLength', '-GPS:all',
                    f'-Model={model}{MARK}', f'-ImageDescription=Panorama de {count} vues',
                    path], capture_output=True)
    return path
