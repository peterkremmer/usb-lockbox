"""Partitions and volumes of a disk, read straight from Windows instead of through PowerShell.

Off unless the environment variable USBLOCKBOX_NATIVE_DISKS is 1, and meant to be switched on only after
`--compare-native` shows these answers matching PowerShell's on your hardware. Every failure raises, and the caller then
falls back to PowerShell for that read.

 * IOCTL_DISK_GET_DRIVE_LAYOUT_EX on \\\\.\\PhysicalDriveN gives the partition style and each partition.
 * Drive letters: IOCTL_STORAGE_GET_DEVICE_NUMBER on \\\\.\\X: says which disk a letter is on;
   GetVolumeInformationW and GetDiskFreeSpaceExW give file system, label, size and free space.

The parsing is plain functions on bytes, tested without Windows. The Windows calls are UNVERIFIED on real hardware.
"""
from __future__ import annotations

import struct
import sys
from typing import Optional

IOCTL_DISK_GET_DRIVE_LAYOUT_EX = 0x00070050
IOCTL_STORAGE_GET_DEVICE_NUMBER = 0x002D1080

STYLES = {0: "MBR", 1: "GPT", 2: "RAW"}
_GPT_TYPES = {                                                   # PartitionType GUIDs -> the names Get-Partition uses
    "ebd0a0a2-b9e5-4433-87c0-68b6b72699c7": "Basic",
    "e3c9e316-0b5c-4db8-817d-f92df00215ae": "Reserved",
    "c12a7328-f81f-11d2-ba4b-00a0c93ec93b": "System",
    "de94bba4-06d1-4d40-a16a-bfd50179d6ac": "Recovery",
}
_MBR_TYPES = {0x07: "IFS", 0x0B: "FAT32", 0x0C: "FAT32", 0x01: "FAT12", 0x04: "FAT16", 0x06: "FAT16", 0x0E: "FAT16",
              0x05: "Extended", 0x0F: "Extended", 0xEE: "GPT Protective", 0xEF: "System", 0x27: "Recovery"}
GPT_HIDDEN = 1 << 62
_PART_SIZE = 144                                                 # sizeof(PARTITION_INFORMATION_EX)
_FIRST_PART = 48                                                 # offset of PartitionEntry in DRIVE_LAYOUT_INFORMATION_EX


def _guid(b: bytes) -> str:
    d1, d2, d3 = struct.unpack("<IHH", b[:8])
    return "%08x-%04x-%04x-%s-%s" % (d1, d2, d3, b[8:10].hex(), b[10:16].hex())


def parse_layout(buf: bytes) -> tuple[str, list[dict]]:
    """(partition style, [{Number, Type, Size, Active, Hidden}]) from a DRIVE_LAYOUT_INFORMATION_EX answer. Entries that
    are not real partitions (length 0, or MBR container/empty slots) are left out."""
    if len(buf) < _FIRST_PART:
        raise ValueError("layout answer too short")
    style, count = struct.unpack_from("<II", buf, 0)
    parts: list[dict] = []
    for i in range(min(count, 128)):
        o = _FIRST_PART + i * _PART_SIZE
        if o + _PART_SIZE > len(buf):
            break
        pstyle = struct.unpack_from("<I", buf, o)[0]
        length, number = struct.unpack_from("<qI", buf, o + 16)[0], struct.unpack_from("<I", buf, o + 24)[0]
        if length <= 0 or number == 0:
            continue
        if pstyle == 1:                                          # GPT
            name = _GPT_TYPES.get(_guid(buf[o + 32:o + 48]), "Unknown")
            attrs = struct.unpack_from("<Q", buf, o + 64)[0]
            parts.append({"Number": number, "Type": name, "Size": length, "Active": False,
                          "Hidden": bool(attrs & GPT_HIDDEN)})
        else:                                                    # MBR
            ptype, boot = buf[o + 32], bool(buf[o + 33])
            parts.append({"Number": number, "Type": _MBR_TYPES.get(ptype, "Unknown"), "Size": length,
                          "Active": boot, "Hidden": False})
    return STYLES.get(style, "RAW"), parts


def enabled() -> bool:
    import os
    return os.environ.get("USBLOCKBOX_NATIVE_DISKS", "") == "1"


# ---------------------------------------------------------------- Windows calls
def _k32():
    import ctypes
    from ctypes import wintypes as w
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.CreateFileW.restype = ctypes.c_void_p
    k.CreateFileW.argtypes = [w.LPCWSTR, w.DWORD, w.DWORD, ctypes.c_void_p, w.DWORD, w.DWORD, ctypes.c_void_p]
    k.DeviceIoControl.argtypes = [ctypes.c_void_p, w.DWORD, ctypes.c_void_p, w.DWORD, ctypes.c_void_p, w.DWORD,
                                  ctypes.POINTER(w.DWORD), ctypes.c_void_p]
    k.DeviceIoControl.restype = w.BOOL
    k.CloseHandle.argtypes = [ctypes.c_void_p]
    return k


def _ioctl(path: str, code: int, size: int) -> bytes:
    import ctypes
    from ctypes import wintypes as w
    k = _k32()
    h = k.CreateFileW(path, 0, 0x3, None, 0x3, 0, None)          # no access needed for a query; share read+write
    if h in (None, ctypes.c_void_p(-1).value):
        raise OSError("cannot open %s (%d)" % (path, ctypes.get_last_error()))
    try:
        b = ctypes.create_string_buffer(size)
        got = w.DWORD(0)
        if not k.DeviceIoControl(h, code, None, 0, b, size, ctypes.byref(got), None):
            raise OSError("ioctl %#x failed on %s (%d)" % (code, path, ctypes.get_last_error()))
        return b.raw[:got.value]
    finally:
        k.CloseHandle(h)


def native_layout(disk_number: int) -> tuple[str, list[dict]]:
    if sys.platform != "win32":
        raise OSError("Windows only")
    return parse_layout(_ioctl(r"\\.\PhysicalDrive%d" % int(disk_number), IOCTL_DISK_GET_DRIVE_LAYOUT_EX,
                               _FIRST_PART + 128 * _PART_SIZE))


def native_volumes(disk_number: int) -> list[dict]:
    """[{Letter, FS, Label, Size, Free}] for the drive letters that live on this disk."""
    import ctypes
    from ctypes import wintypes as w
    if sys.platform != "win32":
        raise OSError("Windows only")
    k = _k32()
    k.GetLogicalDrives.restype = w.DWORD
    k.GetDriveTypeW.argtypes = [w.LPCWSTR]
    out: list[dict] = []
    mask = k.GetLogicalDrives()
    for i in range(2, 26):                                       # C: to Z:
        if not mask & (1 << i):
            continue
        letter = chr(65 + i)
        if k.GetDriveTypeW(letter + ":\\") not in (2, 3):         # removable or fixed only: never wake a CD or network drive
            continue
        try:
            dev = struct.unpack("<III", _ioctl(r"\\.\%s:" % letter, IOCTL_STORAGE_GET_DEVICE_NUMBER, 12))
        except OSError:
            continue
        if dev[1] != int(disk_number):
            continue
        fs, label = ctypes.create_unicode_buffer(64), ctypes.create_unicode_buffer(262)
        ok = k.GetVolumeInformationW(letter + ":\\", label, 262, None, None, None, fs, 64)
        total, free = ctypes.c_ulonglong(0), ctypes.c_ulonglong(0)
        sized = k.GetDiskFreeSpaceExW(letter + ":\\", None, ctypes.byref(total), ctypes.byref(free))
        out.append({"Letter": letter, "FS": fs.value if ok else "", "Label": label.value if ok else "",
                    "Size": total.value if sized else 0, "Free": free.value if sized else 0})
    return out


def native_parts_and_volumes(disk_number: int) -> tuple[str, list[dict], list[dict]]:
    style, parts = native_layout(disk_number)
    vols = native_volumes(disk_number)
    return style, parts, vols
