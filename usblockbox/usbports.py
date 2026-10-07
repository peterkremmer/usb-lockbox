"""USB port discovery: which USB connectors does this computer have, and is a hub plugged in?

Read-only. How it works (Windows):
 * Every USB hub (the computer's root hubs and any external hub) registers the device interface
   GUID_DEVINTERFACE_USB_HUB, so SetupAPI lists them all.
 * IOCTL_USB_GET_NODE_INFORMATION on a hub says how many ports it has.
 * IOCTL_USB_GET_PORT_CONNECTOR_PROPERTIES says, per port, whether the port is a user-visible connector and which
   "companion" port is the other half of the same connector (a USB 3 connector is a USB 2 port plus a USB 3 port).
 * PowerShell gives each hub's location path (DEVPKEY_Device_LocationPaths). Port n of a hub is
   "<hub location path>#USB(n)", which is exactly how a plugged-in drive's own location path ends.

The Windows part is UNTESTED on real hardware: run `python -m usblockbox --ports-dump` and compare. Everything that can
be tested without Windows (turning hub data into tiles) lives in build_ports(), and every failure falls back to
"show the ports that have a drive in them".
"""
from __future__ import annotations

import json
import re
import struct
import sys
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from . import diag
from .models import Port

HUB_INTERFACE_GUID = "F18A0E88-C30C-11D0-8815-00A0C906BED8"      # GUID_DEVINTERFACE_USB_HUB (usbiodef.h)
IOCTL_USB_GET_NODE_INFORMATION = 0x00220408                      # CTL_CODE(FILE_DEVICE_USB, 258, ...)
IOCTL_USB_GET_PORT_CONNECTOR_PROPERTIES = 0x00220458             # CTL_CODE(FILE_DEVICE_USB, 278, ...), Windows 8+

_ROOT_RE = re.compile(r"#USBROOT\(\d+\)$", re.I)


# ---------------------------------------------------------------- helpers (pure)
def norm(path) -> str:
    return (path or "").strip().lower()


def canonical_path(raw) -> str:
    """One location path from the several Windows reports for a device: the PCI one, minus a trailing interface part."""
    paths = list(raw) if isinstance(raw, (list, tuple)) else str(raw or "").split(";")
    paths = [p.strip() for p in paths if p and p.strip()]
    if not paths:
        return ""
    chosen = next((p for p in paths if p.upper().startswith("PCIROOT(")), paths[0])
    return re.sub(r"#USBMI\(\d+\)$", "", chosen, flags=re.I)


def hub_key(path: str) -> str:
    """Normalise a hub's interface path / symbolic link so a companion link can be matched to a hub."""
    p = (path or "").strip().lower()
    p = re.sub(r"^(\\\\[?.]\\|\\\?\?\\)", "", p)
    return re.sub(r"#\{[0-9a-f-]+\}$", "", p)


@dataclass
class PortProps:
    user_connectable: Optional[bool] = None      # None = could not be read
    companion_port: int = 0
    companion_hub: str = ""                      # hub_key of the hub that holds the companion port ("" = same hub)


@dataclass
class HubRaw:
    key: str
    instance_id: str = ""
    location: str = ""                           # canonical location path of the hub device
    num_ports: int = 0
    props: dict = field(default_factory=dict)    # port number -> PortProps

    @property
    def is_root(self) -> bool:
        return bool(_ROOT_RE.search(self.location))


def build_ports(hubs: list[HubRaw], occupied: set[str]) -> tuple[list[Port], str]:
    """Turn raw hub data into the list of connectors to show. Returns (ports, note)."""
    occ = {norm(p) for p in occupied if p}
    usable = [h for h in hubs if h.location and h.num_ports > 0]

    def fallback(note: str) -> tuple[list[Port], str]:
        ports = [Port(o, (o,), "detected") for o in sorted(occ)]
        return ports, note

    if not usable:
        return fallback("Could not read the USB hub layout, so only ports with a drive in them are shown.")

    by_key = {h.key: h for h in usable}
    uplinks = {norm(h.location) for h in usable if not h.is_root}          # a port that holds a hub is not a drive slot
    node: dict[str, tuple[HubRaw, int]] = {}
    for h in usable:
        for n in range(1, h.num_ports + 1):
            node[norm(f"{h.location}#USB({n})")] = (h, n)

    parent = {p: p for p in node}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for path, (h, n) in node.items():                                      # merge companion ports into one connector
        pr = h.props.get(n)
        if pr and pr.companion_port:
            other_hub = by_key.get(pr.companion_hub) if pr.companion_hub else h
            if other_hub is not None:
                other = norm(f"{other_hub.location}#USB({pr.companion_port})")
                if other in node:
                    parent[find(path)] = find(other)
    groups: dict[str, list[str]] = {}
    for p in node:
        groups.setdefault(find(p), []).append(p)

    chosen: list[list[str]] = []
    for members in groups.values():
        if any(m in uplinks for m in members):
            continue
        visible = False
        for m in members:
            h, n = node[m]
            pr = h.props.get(n)
            uc = pr.user_connectable if pr else None
            if uc is True or (uc is None and (not h.is_root or m in occ)):
                visible = True
        if visible:
            chosen.append(members)

    def rep(members: list[str]) -> str:
        return min(members, key=lambda m: (not node[m][0].is_root, norm(node[m][0].location), node[m][1]))

    chosen.sort(key=lambda g: (not node[rep(g)][0].is_root, norm(node[rep(g)][0].location), node[rep(g)][1]))
    hub_no: dict[str, int] = {}
    ports: list[Port] = []
    for members in chosen:
        hubs_here = {node[m][0].key: node[m][0] for m in members}
        roots = all(h.is_root for h in hubs_here.values())
        if roots:
            where = "this computer"
        else:
            numbers = {hub_no[k] for k in hubs_here if k in hub_no}
            number = min(numbers) if numbers else len(set(hub_no.values())) + 1
            for k in hubs_here:
                hub_no[k] = number
            where = f"hub {number}"
        ports.append(Port(rep(members), tuple(sorted(members)), where))
    note = "" if ports else "No user-accessible USB ports were found."
    return ports, note


# ---------------------------------------------------------------- Windows (untested)
class PortScanner:
    """Reads the hub layout. The (slow) hub details are cached and re-read only when the set of hubs changes."""

    def __init__(self, run_ps: Callable[..., str]):
        self._run_ps = run_ps
        self._ids: Optional[tuple] = None
        self._hubs: list[HubRaw] = []
        self.last_error = ""

    def hubs(self) -> list[HubRaw]:
        with diag.timed("find USB hubs"):
            found = _enumerate_hub_interfaces()
        ids = tuple(sorted(i for i, _p in found))
        if ids != self._ids:
            with diag.timed("read %d USB hub(s)" % len(found)):
                self._hubs = self._read(found)
            self._ids = ids
        return self._hubs

    def scan(self, occupied: set[str]) -> tuple[list[Port], str]:
        try:
            hubs = self.hubs()
            self.last_error = ""
        except Exception as e:   # noqa: BLE001 - discovery must never break the app
            self.last_error = f"{type(e).__name__}: {e}"
            hubs = []
        ports, note = build_ports(hubs, occupied)
        if self.last_error and not note:
            note = f"USB hub scan failed ({self.last_error})."
        return ports, note

    def _read(self, found: list[tuple[str, str]]) -> list[HubRaw]:
        hubs: list[HubRaw] = []
        slowest = (0.0, "")
        for instance_id, path in found:
            t0 = time.perf_counter()
            try:
                n, props = _query_hub(path)
            except Exception as e:   # noqa: BLE001 - skip a hub we cannot open
                diag.log.info("Could not open hub %s: %s", instance_id, e)
                continue
            finally:
                slowest = max(slowest, (time.perf_counter() - t0, instance_id))
            hubs.append(HubRaw(hub_key(path), instance_id, "", n, props))
        if found:
            diag.note("slowest single hub (%s)" % slowest[1], slowest[0])
        if hubs:
            paths = self._locations([h.instance_id for h in hubs])
            for h in hubs:
                h.location = paths.get(h.instance_id.lower(), "")
        return hubs

    def _locations(self, instance_ids: list[str]) -> dict[str, str]:
        """{lower-case instance id: canonical location path}: one PowerShell call for all hubs."""
        out = self._run_ps(
            "$ErrorActionPreference='SilentlyContinue'\n"
            "$ids = $env:USBLOCKBOX_IDS | ConvertFrom-Json\n"
            "$out=@(); foreach($id in @($ids)){\n"
            "  $p=(Get-PnpDeviceProperty -InstanceId $id -KeyName 'DEVPKEY_Device_LocationPaths').Data\n"
            "  $out+=[pscustomobject]@{Id=[string]$id;Paths=@($p)} }\n"
            "ConvertTo-Json -InputObject @($out) -Depth 4",
            {"USBLOCKBOX_IDS": json.dumps(instance_ids)}, 60)
        rows = json.loads(out) if out.strip() else []
        return {str(r.get("Id", "")).lower(): canonical_path(r.get("Paths") or []) for r in rows if isinstance(r, dict)}


def _enumerate_hub_interfaces() -> list[tuple[str, str]]:
    """[(device instance id, device interface path)] for every present USB hub. Windows only."""
    import ctypes
    from ctypes import wintypes as w

    class GUID(ctypes.Structure):
        _fields_ = [("Data1", w.DWORD), ("Data2", w.WORD), ("Data3", w.WORD), ("Data4", ctypes.c_ubyte * 8)]

    class SP_DEVICE_INTERFACE_DATA(ctypes.Structure):
        _fields_ = [("cbSize", w.DWORD), ("InterfaceClassGuid", GUID), ("Flags", w.DWORD), ("Reserved", ctypes.c_void_p)]

    class SP_DEVINFO_DATA(ctypes.Structure):
        _fields_ = [("cbSize", w.DWORD), ("ClassGuid", GUID), ("DevInst", w.DWORD), ("Reserved", ctypes.c_void_p)]

    import uuid
    guid = GUID.from_buffer_copy(uuid.UUID(HUB_INTERFACE_GUID).bytes_le)
    setupapi = ctypes.WinDLL("setupapi", use_last_error=True)
    cfgmgr = ctypes.WinDLL("cfgmgr32", use_last_error=True)
    setupapi.SetupDiGetClassDevsW.restype = ctypes.c_void_p
    setupapi.SetupDiGetClassDevsW.argtypes = [ctypes.POINTER(GUID), w.LPCWSTR, w.HWND, w.DWORD]
    setupapi.SetupDiEnumDeviceInterfaces.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(GUID), w.DWORD,
                                                     ctypes.POINTER(SP_DEVICE_INTERFACE_DATA)]
    setupapi.SetupDiEnumDeviceInterfaces.restype = w.BOOL
    setupapi.SetupDiGetDeviceInterfaceDetailW.argtypes = [ctypes.c_void_p, ctypes.POINTER(SP_DEVICE_INTERFACE_DATA),
                                                          ctypes.c_void_p, w.DWORD, ctypes.POINTER(w.DWORD),
                                                          ctypes.POINTER(SP_DEVINFO_DATA)]
    setupapi.SetupDiGetDeviceInterfaceDetailW.restype = w.BOOL
    setupapi.SetupDiDestroyDeviceInfoList.argtypes = [ctypes.c_void_p]
    cfgmgr.CM_Get_Device_IDW.argtypes = [w.DWORD, w.LPWSTR, w.ULONG, w.ULONG]
    cfgmgr.CM_Get_Device_IDW.restype = w.DWORD

    DIGCF_PRESENT, DIGCF_DEVICEINTERFACE = 0x2, 0x10
    invalid = ctypes.c_void_p(-1).value
    hdev = setupapi.SetupDiGetClassDevsW(ctypes.byref(guid), None, None, DIGCF_PRESENT | DIGCF_DEVICEINTERFACE)
    if hdev in (None, invalid):
        raise OSError(f"SetupDiGetClassDevs failed ({ctypes.get_last_error()})")
    found: list[tuple[str, str]] = []
    try:
        index = 0
        while True:
            ifd = SP_DEVICE_INTERFACE_DATA()
            ifd.cbSize = ctypes.sizeof(ifd)
            if not setupapi.SetupDiEnumDeviceInterfaces(hdev, None, ctypes.byref(guid), index, ctypes.byref(ifd)):
                break                                                  # ERROR_NO_MORE_ITEMS
            index += 1
            need = w.DWORD(0)
            setupapi.SetupDiGetDeviceInterfaceDetailW(hdev, ctypes.byref(ifd), None, 0, ctypes.byref(need), None)
            buf = ctypes.create_string_buffer(max(need.value, 8))
            ctypes.c_uint32.from_buffer(buf).value = 8 if sys.maxsize > 2 ** 32 else 6    # cbSize of the detail struct
            info = SP_DEVINFO_DATA()
            info.cbSize = ctypes.sizeof(info)
            if not setupapi.SetupDiGetDeviceInterfaceDetailW(hdev, ctypes.byref(ifd), buf, need.value,
                                                             ctypes.byref(need), ctypes.byref(info)):
                continue
            path = ctypes.wstring_at(ctypes.addressof(buf) + 4)
            idbuf = ctypes.create_unicode_buffer(512)
            if cfgmgr.CM_Get_Device_IDW(info.DevInst, idbuf, 512, 0) != 0:
                continue
            found.append((idbuf.value, path))
    finally:
        setupapi.SetupDiDestroyDeviceInfoList(hdev)
    return found


def _query_hub(path: str) -> tuple[int, dict]:
    """(port count, {port: PortProps}) for the hub at this device interface path. Windows only."""
    import ctypes
    from ctypes import wintypes as w

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateFileW.restype = ctypes.c_void_p
    k32.CreateFileW.argtypes = [w.LPCWSTR, w.DWORD, w.DWORD, ctypes.c_void_p, w.DWORD, w.DWORD, ctypes.c_void_p]
    k32.DeviceIoControl.argtypes = [ctypes.c_void_p, w.DWORD, ctypes.c_void_p, w.DWORD, ctypes.c_void_p, w.DWORD,
                                    ctypes.POINTER(w.DWORD), ctypes.c_void_p]
    k32.DeviceIoControl.restype = w.BOOL
    k32.CloseHandle.argtypes = [ctypes.c_void_p]
    GENERIC_WRITE, FILE_SHARE_WRITE, OPEN_EXISTING = 0x40000000, 0x2, 0x3
    h = k32.CreateFileW(path, GENERIC_WRITE, FILE_SHARE_WRITE, None, OPEN_EXISTING, 0, None)
    if h in (None, ctypes.c_void_p(-1).value):
        raise OSError(f"cannot open hub ({ctypes.get_last_error()})")

    def ioctl(code: int, buf) -> bool:
        got = w.DWORD(0)
        return bool(k32.DeviceIoControl(h, code, buf, len(buf), buf, len(buf), ctypes.byref(got), None))

    try:
        node = ctypes.create_string_buffer(76)                          # sizeof(USB_NODE_INFORMATION), packed
        if not ioctl(IOCTL_USB_GET_NODE_INFORMATION, node):
            raise OSError(f"node information failed ({ctypes.get_last_error()})")
        ports = node.raw[6]                                             # NodeType(4) + bLength + bType, then bNumberOfPorts
        props: dict[int, PortProps] = {}
        for n in range(1, ports + 1):
            b = ctypes.create_string_buffer(512)
            struct.pack_into("<I", b, 0, n)                             # ConnectionIndex
            if not ioctl(IOCTL_USB_GET_PORT_CONNECTOR_PROPERTIES, b):
                props[n] = PortProps()
                continue
            actual, flags = struct.unpack_from("<II", b, 4)
            _idx, comp_port = struct.unpack_from("<HH", b, 12)
            chars = max(0, min((actual - 16) // 2, 240))
            name = b.raw[16:16 + chars * 2].decode("utf-16-le", "ignore").split("\x00")[0]
            props[n] = PortProps(bool(flags & 1), comp_port, hub_key(name) if name else "")
        return ports, props
    finally:
        k32.CloseHandle(h)


def dump(run_ps: Callable[..., str], occupied: set[str]) -> str:
    """Human-readable diagnostics for --ports-dump."""
    sc = PortScanner(run_ps)
    lines = []
    try:
        hubs = sc.hubs()
    except Exception as e:   # noqa: BLE001
        return f"Hub enumeration failed: {type(e).__name__}: {e}"
    for h in hubs:
        lines.append(f"HUB {h.instance_id}\n  key={h.key}\n  location={h.location or '(unknown)'}  root={h.is_root}  ports={h.num_ports}")
        for n, pr in sorted(h.props.items()):
            lines.append(f"    port {n}: user_connectable={pr.user_connectable} companion={pr.companion_port}"
                         f"{' on ' + pr.companion_hub if pr.companion_hub else ''}")
    ports, note = build_ports(hubs, occupied)
    lines.append(f"\nDRIVES' LOCATION PATHS: {sorted(occupied) or 'none'}")
    lines.append(f"RESULT: {len(ports)} port(s)" + (f"  NOTE: {note}" if note else ""))
    for i, p in enumerate(ports, 1):
        lines.append(f"  PORT {i} ({p.where}): {', '.join(p.paths)}")
    return "\n".join(lines)
