from usblockbox.usbports import HubRaw, PortProps, build_ports, canonical_path, hub_key

ROOT = "PCIROOT(0)#PCI(1400)#USBROOT(0)"


def test_canonical_path_prefers_pci_and_drops_interface_part():
    raw = ["ACPI(_SB_)#ACPI(PCI0)#ACPI(XHC_)#ACPIROOT(0)#USB(3)", "PCIROOT(0)#PCI(1400)#USBROOT(0)#USB(3)#USBMI(0)"]
    assert canonical_path(raw) == "PCIROOT(0)#PCI(1400)#USBROOT(0)#USB(3)"
    assert canonical_path("A;B") == "A" and canonical_path("") == "" and canonical_path(None) == ""


def test_hub_key_normalises_interface_paths_and_symbolic_links():
    a = hub_key(r"\\?\usb#root_hub30#4&2d7a8bd&0&0#{f18a0e88-c30c-11d0-8815-00a0c906bed8}")
    b = hub_key(r"\??\USB#ROOT_HUB30#4&2D7A8BD&0&0#{F18A0E88-C30C-11D0-8815-00A0C906BED8}")
    assert a == b == "usb#root_hub30#4&2d7a8bd&0&0"


def test_only_user_connectable_root_ports_are_shown_and_companions_merge():
    # 8 bus ports; ports 1/5 and 2/6 are two USB-A connectors (USB2 + USB3 halves); 3, 4, 7, 8 are internal.
    props = {1: PortProps(True, 5), 5: PortProps(True, 1), 2: PortProps(True, 6), 6: PortProps(True, 2),
             3: PortProps(False), 4: PortProps(False), 7: PortProps(False), 8: PortProps(False)}
    ports, note = build_ports([HubRaw("usb#root_hub30#a", "x", ROOT, 8, props)], set())
    assert note == "" and len(ports) == 2
    assert all(p.where == "this computer" for p in ports)
    assert {len(p.paths) for p in ports} == {2}
    assert ports[0].paths == tuple(sorted([f"{ROOT}#USB(1)".lower(), f"{ROOT}#USB(5)".lower()]))


def test_external_hub_adds_its_ports_and_its_uplink_is_not_a_slot():
    root_props = {1: PortProps(True), 2: PortProps(True)}
    hub_loc = f"{ROOT}#USB(2)"
    hub = HubRaw("usb#vid_05e3&pid_0610#h", "USB\\VID_05E3\\H", hub_loc, 4, {n: PortProps(True) for n in range(1, 5)})
    ports, _ = build_ports([HubRaw("usb#root_hub30#a", "x", ROOT, 2, root_props), hub], set())
    assert [p.where for p in ports] == ["this computer", "hub 1", "hub 1", "hub 1", "hub 1"]
    assert not any(p.paths == (f"{ROOT}#USB(2)".lower(),) for p in ports)      # port 2 holds the hub


def test_usb3_hub_is_two_hubs_with_companion_ports_but_four_tiles():
    root = HubRaw("usb#root_hub30#a", "x", ROOT, 2, {1: PortProps(True, 2, ""), 2: PortProps(True, 1, "")})
    hs = HubRaw("hs", "i1", f"{ROOT}#USB(1)", 4, {n: PortProps(True, n, "ss") for n in range(1, 5)})
    ss = HubRaw("ss", "i2", f"{ROOT}#USB(2)", 4, {n: PortProps(True, n, "hs") for n in range(1, 5)})
    ports, _ = build_ports([root, hs, ss], set())
    assert [p.where for p in ports] == ["hub 1"] * 4          # root ports both hold hubs -> only the hub's four connectors
    assert all(len(p.paths) == 2 for p in ports)


def test_unknown_port_properties_fall_back_to_occupied_root_ports():
    hub = HubRaw("usb#root_hub30#a", "x", ROOT, 6, {})                       # properties could not be read
    used = f"{ROOT}#USB(4)".lower()
    ports, _ = build_ports([hub], {used})
    assert [p.paths for p in ports] == [(used,)]


def test_no_hub_data_shows_only_ports_with_drives_and_says_so():
    ports, note = build_ports([], {"pciroot(0)#pci(1400)#usbroot(0)#usb(3)"})
    assert len(ports) == 1 and ports[0].where == "detected" and "only ports with a drive" in note
    assert build_ports([], set())[0] == []


def test_drive_paths_match_ports_case_insensitively():
    hub = HubRaw("usb#root_hub30#a", "x", ROOT, 2, {1: PortProps(True), 2: PortProps(True)})
    ports, _ = build_ports([hub], set())
    drive_path = "PCIROOT(0)#PCI(1400)#USBROOT(0)#USB(2)".lower()
    assert any(drive_path in p.paths for p in ports)
