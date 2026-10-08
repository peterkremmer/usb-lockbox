"""Headless UI smoke test: python tools/ui_smoke.py [outdir]  (fake hardware + simulator, offscreen Qt)."""
import os, sys, time, tempfile
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from pathlib import Path
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QApplication
from usblockbox import policy
from usblockbox.backends.simulated import SimulatedBackend, SIM_PASSWORD_DEFAULT
from usblockbox.config import Settings
from usblockbox.models import SlotState
from usblockbox.ui.controller import Controller
from usblockbox.ui.main_window import MainWindow
from usblockbox.ui.theme import apply_tooltip_style

out = Path(sys.argv[1] if len(sys.argv) > 1 else tempfile.mkdtemp())
out.mkdir(parents=True, exist_ok=True)
os.environ["USBLOCKBOX_HOME"] = str(out / "home")      # keeps timings.json out of the real data folder
app = QApplication([])
apply_tooltip_style()
s = Settings(); s.csv_dir = str(out / "csv"); s.pdf_dir = str(out / "pdf"); s.set_fixed_password(SIM_PASSWORD_DEFAULT)

class FakeHardware(SimulatedBackend):
    """Stands in for the computer's own USB ports (3 of them) so the launch path can be checked."""
    name = "fake-hardware"

hw = FakeHardware(speed=4.0, port_count=3)
ctl = Controller(hw, s); win = MainWindow(ctl, s); win.show()
ctl.timer.setInterval(300)

def pump(sec, until=None):
    end = time.time() + sec
    while time.time() < end:
        app.processEvents(); time.sleep(0.02)
        if until and until(): return True
    return until() if until else True

# ---- launch: ports come from the computer, no simulator wording anywhere
assert pump(5, lambda: len(win.tiles) == 3), len(win.tiles)
text = win.banner.text()
assert "SIMULAT" not in text.upper() and "service" not in text.lower(), text
assert "DRY RUN" in text and not win.dock.isVisible() and not win.simulator_mode
assert "imulat" not in win.windowTitle() and "service" not in win.windowTitle().lower()
assert all(x.isVisible() for x in win.tiles) and getattr(win, "empty_label", None) is None
win.grab().save(str(out / "1_launch.png"))
# a USB hub is plugged in: the list grows live
hw.set_port_count(7)
assert pump(5, lambda: len(win.tiles) == 7), len(win.tiles)
hw.set_port_count(3)
assert pump(5, lambda: len(win.tiles) == 3), len(win.tiles)

# ---- Settings > Ports: hide one port, name another
from usblockbox.ui.settings_dialog import SettingsDialog
dlg = SettingsDialog(s, win, ports=ctl.available_ports())
assert dlg.port_table.rowCount() == 3
dlg.port_table.item(1, 0).setCheckState(Qt.Unchecked)
dlg.port_table.item(0, 1).setText("Front left")
from PySide6.QtWidgets import QTabWidget
tw = dlg.findChild(QTabWidget)
assert [tw.tabText(i) for i in range(tw.count())] == ["Ports", "Sanitization", "Encryption", "Passwords", "Records", "Safety", "Updates"]
for i in range(tw.count()):
    tw.setCurrentIndex(i); dlg.show(); pump(0.2)
    dlg.grab().save(str(out / f"1b_settings_{i}_{tw.tabText(i)}.png"))
# hover help must actually be readable: render one and check it has contrast
from PySide6.QtCore import QPoint
from PySide6.QtWidgets import QToolTip
from usblockbox.ui.settings_dialog import _tip, METHOD_INFO
tw.setCurrentIndex(2); dlg.show(); pump(0.2)
QToolTip.showText(dlg.mapToGlobal(QPoint(60, 60)), _tip(METHOD_INFO["XtsAes256"][1]), dlg.method)
pump(0.3)
tips = [w for w in QApplication.topLevelWidgets() if w.metaObject().className() == "QTipLabel" and w.isVisible()]
assert tips, "no tooltip window appeared"
img = tips[0].grab().toImage()
lumas = [QColor(img.pixel(x, y)).lightness() for x in range(0, img.width(), 3) for y in range(0, img.height(), 3)]
assert max(lumas) - min(lumas) > 120, ("tooltip has no contrast", min(lumas), max(lumas))
tips[0].grab().save(str(out / "1b_tooltip.png")); QToolTip.hideText()
# password tab: only the options that apply to the chosen mode are visible
tw.setCurrentIndex(3)
for mode, fixed_vis, glen_vis in (("generated", False, True), ("prompt", False, False), ("fixed", True, True)):
    dlg.pmode.setCurrentIndex(dlg.pmode.findData(mode)); pump(0.1)
    assert dlg.fixed_field.isVisibleTo(dlg) == fixed_vis and dlg.glen.isVisibleTo(dlg) == glen_vis, mode
assert dlg.fixed.text() == SIM_PASSWORD_DEFAULT                     # the fixed password is shown
assert "XTS-AES-256" in dlg.method.itemText(0) and dlg.method.itemText(2) == "AES-CBC-256"
dlg.dry.setChecked(True)
assert "operator" not in " ".join(tw.tabText(i).lower() for i in range(tw.count()))
assert "Operator:" in win.operator_label.text()
dlg._save(); ctl.apply_settings(s); pump(0.5)
assert len(win.tiles) == 2 and ctl.slots[0].title == "FRONT LEFT" and s.hidden_ports == ["sim-port-2"], (len(win.tiles), s.hidden_ports)
win.grab().save(str(out / "1c_hidden_port.png"))
dlg = SettingsDialog(s, win, ports=ctl.available_ports())          # reopen: port 2 must still be listed, unticked
assert dlg.port_table.rowCount() == 3 and dlg.port_table.item(1, 0).checkState() == Qt.Unchecked
dlg.port_table.item(1, 0).setCheckState(Qt.Checked); dlg.port_table.item(0, 1).setText("")
dlg._save(); ctl.apply_settings(s); pump(0.5)
assert len(win.tiles) == 3 and ctl.slots[0].title == "PORT 1" and not s.hidden_ports

# ---- processing on the hardware path (dry run first, then erase path)
for sc, port in (("used_files", 1), ("blank", 2), ("system_disk", 3)):
    hw.add_scenario(sc, port)
assert pump(10, lambda: all(x.state not in (SlotState.EMPTY, SlotState.SCANNING) for x in ctl.slots)), [x.state for x in ctl.slots]
assert [x.state for x in ctl.slots] == [SlotState.NEEDS_WORK, SlotState.NEEDS_WORK, SlotState.REJECTED]
win.grab().save(str(out / "2_scanned.png"))
s.dry_run = False; ctl.apply_settings(s); pump(0.3)
assert "WARNING" in win.banner.text() and "REAL MODE" not in win.banner.text() and win.windowTitle() == "USB Lockbox", (win.banner.text(), win.windowTitle())
ctl.process(0); ctl.process(1); ctl.process(2)           # slot 2 (system disk) must refuse
# unattended-batch messaging: clock times, time left, batch strip, stall warning
assert pump(10, lambda: ctl.slots[0].state == SlotState.PROCESSING and "Started" in win.tiles[0].timing.text()), win.tiles[0].timing.text()
assert win.tiles[0].timing.isVisibleTo(win) and "left" in win.tiles[0].timing.text() + "left" and "running" in win.tiles[0].timing.text()
win._update_batch()
assert win.batch_bar.isVisibleTo(win) and "working" in win.batch_bar.text(), win.batch_bar.text()
real_now = ctl._now
ctl._now = lambda: real_now() + 3600                       # pretend an hour went by with no progress
ctl.tick(); win._update_batch()
assert ctl.slots[0].attention and "No progress" in ctl.slots[0].attention, ctl.slots[0].attention
assert "NEEDS A LOOK" in win.batch_bar.text(), win.batch_bar.text()
assert "No progress" in win.tiles[0].reasons.text() and win.tiles[0].styleSheet().count("#b45309"), "tile not amber"
win.grab().save(str(out / "2b_needs_a_look.png"))
ctl._now = real_now; ctl.tick()
assert pump(40, lambda: ctl.slots[0].state == SlotState.DONE and ctl.slots[1].state == SlotState.DONE), [x.state for x in ctl.slots]
assert ctl.slots[2].state == SlotState.REJECTED
win.grab().save(str(out / "3_done.png"))
win._update_batch()
assert win.batch_bar.isVisibleTo(win) and "Batch finished" in win.batch_bar.text(), win.batch_bar.text()
assert "Finished" in win.tiles[0].timing.text() and "took" in win.tiles[0].timing.text(), win.tiles[0].timing.text()
s.dry_run = True; ctl.apply_settings(s); pump(0.3)

# ---- simulator mode: orange banner, flyout, own virtual ports
win.toggle_simulator_mode(True); pump(0.5)
assert win.simulator_mode and win.dock.isVisible()
t = win.banner.text()
assert "SIMULATOR MODE" in t and "service" not in t.lower() and "service" not in win.windowTitle().lower(), t
assert pump(5, lambda: len(win.tiles) == 4) and s.simulator_ports == 4, (len(win.tiles), s.simulator_ports)
win.grab().save(str(out / "4_simulator.png"))
win.sim_panel.count.setValue(6)
assert pump(6, lambda: len(win.tiles) == 6) and s.simulator_ports == 6, (len(win.tiles), s.simulator_ports)
combo = win.sim_panel.rows[2][1]; combo.setCurrentIndex(combo.findData("odd_boot"))
win.sim_panel._insert(3, combo)
assert pump(5, lambda: ctl.slots[2].drive is not None and ctl.slots[2].state == SlotState.NEEDS_WORK)
win.sim_panel._set_policy({"GPO": {"RDVConfigureBDE": 0}, "Intune/MDM": {}})
assert pump(5, lambda: "blocking" in win.policy_chip.text()), win.policy_chip.text()
assert pump(5, lambda: ctl.slots[2].state == SlotState.REJECTED), ctl.slots[2].state
win.sim_panel._set_policy(None)
assert pump(5, lambda: "blocking" not in win.policy_chip.text()), win.policy_chip.text()
win.grab().save(str(out / "5_simulator_policy.png"))

# ---- exit: back to the computer's real ports and normal colours
win.toggle_simulator_mode(False); pump(1)
assert not win.simulator_mode and not win.dock.isVisible() and win.styleSheet() == ""
assert "SIMULAT" not in win.banner.text().upper() and policy.SIM_RAW is None
assert pump(5, lambda: len(win.tiles) == 3), len(win.tiles)
win.grab().save(str(out / "6_normal_again.png"))
# ---- a slow first scan says "Scanning...", then "No USB ports found." when it ends empty-handed
from usblockbox.backends.empty import NoHardwareBackend
from usblockbox import diag

class SlowEmpty(NoHardwareBackend):
    ports_note = ""
    def list_usb_disks(self):
        time.sleep(2.5)
        return []

c2 = Controller(SlowEmpty(), s); w2 = MainWindow(c2, s); w2.show()
assert pump(1.5, lambda: getattr(w2, "empty_label", None) is not None and w2.empty_label.text().startswith("Scanning USB ports")), \
    getattr(w2, "empty_label", None) and w2.empty_label.text()
assert not c2.first_scan_done and "NOT FINISHED" in w2._diagnostic_summary(), w2._diagnostic_summary()
w2.grab().save(str(out / "7_scanning.png"))
assert pump(10, lambda: c2.first_scan_done)
assert pump(3, lambda: w2.empty_label.text().startswith("No USB ports found")), w2.empty_label.text()
assert "First scan:" in w2._diagnostic_summary() and "NOT FINISHED" not in w2._diagnostic_summary()
w2.grab().save(str(out / "8_no_ports_found.png"))
# ---- switching to the simulator while a slow real scan is still running must not wait for it
c3 = Controller(SlowEmpty(), s); w3 = MainWindow(c3, s); w3.show(); pump(0.3)
assert c3._polling and not c3.first_scan_done
t0 = time.monotonic()
c3.set_backend(SimulatedBackend(speed=4.0, port_count=4), simulator=True)
assert pump(1.0, lambda: len(w3.tiles) == 4), (len(w3.tiles), "simulator ports waited for the old scan")
assert time.monotonic() - t0 < 1.5
pump(3)                                              # the old scan finishes now; its answer must be ignored
assert len(w3.tiles) == 4 and c3.first_scan_done
# ---- ports first: the tiles appear while the drives are still being read, and say so
class SlowDrives(SimulatedBackend):
    name = "slow-drives"
    def list_usb_disks(self):
        time.sleep(2.5)
        return super().list_usb_disks()

c4 = Controller(SlowDrives(speed=4.0, port_count=3), s); w4 = MainWindow(c4, s); w4.show()
assert pump(1.5, lambda: len(w4.tiles) == 3), len(w4.tiles)
assert not c4.first_scan_done and w4.tiles[0].big.text() == "READING DRIVES", w4.tiles[0].big.text()
assert getattr(w4, "empty_label", None) is None
w4.grab().save(str(out / "9_reading_drives.png"))
assert pump(10, lambda: c4.first_scan_done)
assert pump(2, lambda: w4.tiles[0].big.text() == "EMPTY"), w4.tiles[0].big.text()
# ---- drives are reported as they are read: the first is checked while the others are still being read
class Streaming(SimulatedBackend):
    name = "streaming"
    streams_drives = True
    def list_usb_disks(self, on_ready=None):
        drives = super().list_usb_disks()
        if on_ready and drives:
            on_ready(drives[:1])
            time.sleep(2.0)                               # the second drive is slow to read
        return drives

st = Streaming(speed=4.0, port_count=3)
st.add_scenario("blank", 1); st.add_scenario("used_files", 2)
c5 = Controller(st, s); w5 = MainWindow(c5, s); w5.show()
assert pump(1.5, lambda: len(w5.tiles) == 3)
assert pump(1.5, lambda: c5.slots[0].state != SlotState.EMPTY and not c5.first_scan_done), (c5.slots[0].state, c5.first_scan_done)
assert c5.slots[1].state == SlotState.EMPTY and c5.slots[1].reading        # not "empty": still being read
assert pump(10, lambda: c5.first_scan_done)
assert pump(3, lambda: c5.slots[1].state != SlotState.EMPTY and not c5.slots[1].reading)
# ---- a drive that will not answer: its tile says so instead of looking empty, and recovers
class Stuck(SimulatedBackend):
    name = "stuck"
    bad = True
    def unreadable_drives(self):
        return [("sim-port-1", "x")] if self.bad else []

stuck = Stuck(speed=4.0, port_count=3)
c6 = Controller(stuck, s); w6 = MainWindow(c6, s); w6.show()
assert pump(10, lambda: c6.first_scan_done)
assert pump(3, lambda: w6.tiles[0].big.text() == "NOT RESPONDING"), w6.tiles[0].big.text()
assert w6.tiles[1].big.text() == "EMPTY"
stuck.bad = False
assert pump(10, lambda: w6.tiles[0].big.text() == "EMPTY"), w6.tiles[0].big.text()
# ---- the application icon is there and loads
from usblockbox.ui.icon import app_icon, icon_file
assert icon_file() is not None and not app_icon().isNull() and not win.windowIcon().isNull()
assert 256 in [sz.width() for sz in app_icon().availableSizes()] or app_icon().availableSizes() == []
print("pdfs:", sorted(p.name for p in (out / "pdf").glob("*.pdf")))
print("SMOKE OK")
