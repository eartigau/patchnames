import os as _os

# numpy's OpenBLAS on one thread, set before numpy is first loaded. The matrix
# products here are small (the faces of faces.py) and four times faster that
# way; and its pool of threads, called from a thread of the search page, was
# seen to spin for ever inside one of them with the lock of the interpreter
# held, which froze the whole page (2026-10-13, numpy 1.26.4, OpenBLAS 0.3.23).
_os.environ.setdefault('OPENBLAS_NUM_THREADS', '1')

from .core import patchnames, time_offset, patchdate, nef_to_dng, get_exif

__all__ = ['patchnames', 'time_offset', 'patchdate', 'nef_to_dng', 'get_exif']
