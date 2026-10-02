"""Plain-language names for the system folders and files operating systems leave on removable drives."""
from __future__ import annotations

import re

_EXACT = {
    "$recycle.bin": "Windows Recycle Bin",
    "recycler": "Windows Recycle Bin (old style)",
    "recycled": "Windows Recycle Bin (old style)",
    "system volume information": "Windows system folder",
    ".spotlight-v100": "Mac Spotlight search index",
    ".fseventsd": "Mac file-change log",
    ".trashes": "Mac Trash",
    ".temporaryitems": "Mac temporary items",
    ".documentrevisions-v100": "Mac document versions",
    ".apdisk": "Mac Time Machine marker",
    "lost+found": "Linux file-system recovery folder",
    ".ds_store": "Mac Finder settings",
}
_PATTERNS = [
    (re.compile(r"^found\.\d{3}$"), "Windows disk-check (chkdsk) recovery folder"),
    (re.compile(r"^\.trash-\d+$"), "Linux Trash"),
    (re.compile(r"^\._"), "Mac metadata file"),
]


def junk_label(name: str):
    """A friendly name if `name` is a well-known system folder/file, else None."""
    n = name.lower()
    if n in _EXACT:
        return _EXACT[n]
    for rx, label in _PATTERNS:
        if rx.search(n):
            return label
    return None
