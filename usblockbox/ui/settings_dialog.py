"""Settings dialog."""
from __future__ import annotations

import html
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFileDialog,
                               QFormLayout, QFrame, QHBoxLayout, QLabel, QLineEdit, QMessageBox,
                               QPushButton, QSpinBox, QTableWidget, QTableWidgetItem, QTabWidget,
                               QToolButton, QVBoxLayout, QWidget)

from .. import updater
from ..appinfo import UPDATE_REPO
from ..config import (ENCRYPTION_METHODS, FILESYSTEMS, IS_WINDOWS, PARTITION_STYLES, DEFAULT_VOLUME_LABEL,
                      DATA_DIR_FALLBACK, Settings, default_csv_dir, default_pdf_dir, fips_mode_enabled,
                      method_label)
from ..passwords import generate_passphrase

# ---- plain-language descriptions shown under the Encryption choices (and as hover help)
# Sources: Microsoft's Enable-BitLocker and BitLocker policy documentation (default XTS-AES-128, XTS-AES-256 for
# stronger security, AES-128/256 for removable media used on Windows 8.1 / Server 2012 R2), the Microsoft blog on
# BitLocker XTS (introduced in Windows 10 build 1511; CBC before), NIST SP 800-38E (XTS-AES approved for storage).
METHOD_INFO = {
    "XtsAes256": ("Highest strength",
                  "Microsoft recommends XTS-AES for all drives, with the 256-bit key when the hardware is fast enough. "
                  "The drive can only be opened on Windows 10 version 1511 or later."),
    "XtsAes128": ("Strong, Microsoft's default",
                  "The same mode with a 128-bit key: slightly faster and still considered strong. "
                  "Opens on Windows 10 version 1511 or later."),
    "Aes256": ("Older mode, for compatibility",
               "AES in CBC mode, the only mode before Windows 10 1511. Choose it only if the drive must open on "
               "Windows 8.1 / Server 2012 R2 or earlier; Microsoft says removable drives for those versions must use "
               "AES-128 or AES-256 (the CBC modes). Otherwise prefer XTS-AES."),
    "Aes128": ("Older mode, the lowest of the four",
               "AES-CBC with a 128-bit key. Compatibility with old Windows only. Prefer XTS-AES-256 unless you need this."),
}
FS_INFO = {
    "exFAT": ("Recommended for mixed use",
              "Opens on Windows, current macOS and most Linux, and has no 4 GB file-size limit. Some older devices "
              "(older TVs, cameras, printers) cannot read it."),
    "NTFS": ("Windows first",
             "Best on Windows (permissions, large files). Macs can read it but not write without extra software, and "
             "many TVs, printers and embedded devices cannot read it."),
    "FAT32": ("Most compatible, with limits",
              "Works almost everywhere, but one file cannot exceed 4 GB, and Windows' own tools will not format a FAT32 "
              "volume larger than 32 GB, so larger drives will fail to format."),
}
PSTYLE_INFO = {
    "GPT": ("Modern, needed above 2 TB",
            "Works on Windows 7 and later, macOS and Linux. Some old devices (older TVs, car stereos, embedded gear) "
            "and Windows XP cannot read GPT drives."),
    "MBR": ("Old, widely readable, 2 TB limit",
            "Readable almost everywhere, but anything beyond 2 TB on the drive is unusable. Choose it only for "
            "old devices that cannot read GPT."),
}
FIPS_NOTE = ("FIPS: AES is an approved algorithm (FIPS 197); XTS mode is approved by NIST for storage devices "
             "(SP 800-38E) and CBC mode by SP 800-38A. An approved algorithm is not the same as a validated product: "
             "FIPS 140 validation covers Windows' cryptographic module, not this app, so check Microsoft's validation "
             "list for your Windows version.")


def _tip(text: str) -> str:
    return f"<div style='width:340px'>{html.escape(text)}</div>"


def _label(text: str, tip: str = "") -> QWidget:
    """A form label. With a tip it gets an (i) button: hover shows the help, click opens it in a window."""
    if not tip:
        return QLabel(text)
    w = QWidget()
    h = QHBoxLayout(w); h.setContentsMargins(0, 0, 0, 0); h.setSpacing(6)
    lab = QLabel(text)
    lab.setToolTip(_tip(tip))
    b = QToolButton(); b.setText("ⓘ"); b.setAutoRaise(True); b.setCursor(Qt.PointingHandCursor)
    b.setToolTip(_tip(tip))

    def show_help():
        m = QMessageBox(w.window())
        m.setWindowTitle(text); m.setTextFormat(Qt.PlainText); m.setText(tip); m.setIcon(QMessageBox.Information)
        m.exec()
    b.clicked.connect(show_help)
    h.addWidget(lab); h.addWidget(b); h.addStretch(1)
    return w


def _note(text: str, warn: bool = False) -> QLabel:
    lab = QLabel(text)
    lab.setWordWrap(True)
    if warn:
        lab.setStyleSheet("color:#b45309;")
    return lab


def _form() -> QFormLayout:
    f = QFormLayout()
    f.setLabelAlignment(Qt.AlignLeft | Qt.AlignVCenter)
    f.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
    f.setHorizontalSpacing(18)
    return f


def _page(f: QFormLayout) -> QWidget:
    w = QWidget()
    w.setLayout(f)
    return w


def _check(f: QFormLayout, text: str, box: QCheckBox, tip: str = "") -> QCheckBox:
    """Text on the left, the check box on the right, like every other row."""
    f.addRow(_label(text, tip), box)
    if tip:
        box.setToolTip(_tip(tip))
    return box


def _folder_row(edit: QLineEdit, default: str) -> QWidget:
    w = QWidget()
    h = QHBoxLayout(w); h.setContentsMargins(0, 0, 0, 0)
    b = QPushButton("Browse...")
    r = QPushButton("Default")
    r.setToolTip(f"Use the default: {default}")

    def pick():
        d = QFileDialog.getExistingDirectory(w, "Choose folder", edit.text() or default)
        if d:
            edit.setText(d)
    b.clicked.connect(pick)
    r.clicked.connect(lambda: edit.setText(default))
    h.addWidget(edit, 1); h.addWidget(b); h.addWidget(r)
    return w


def _combo(items, current, tips=None) -> QComboBox:
    """items: list of (text, value). Hover text per item when tips is given."""
    c = QComboBox()
    for i, (text, value) in enumerate(items):
        c.addItem(text, value)
        if tips and value in tips:
            c.setItemData(i, _tip(tips[value]), Qt.ToolTipRole)
    c.setCurrentIndex(max(0, c.findData(current)))
    return c


class SettingsDialog(QDialog):
    def __init__(self, settings: Settings, parent=None, erase_blocked_reason: str = "", ports=()):
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.resize(760, 660)
        self.s = settings
        self.erase_blocked_reason = erase_blocked_reason
        tabs = QTabWidget()

        # ---- Ports
        self.ports = list(ports)
        pt = QWidget(); pv = QVBoxLayout(pt)
        pv.addWidget(_note("These are the USB ports Windows reports for this computer (and any hub on it). Windows also "
                           "lists connectors that are inside the machine, so untick the ones you do not use. A hidden port "
                           "is ignored: a drive plugged into it is never touched. To find a port, plug a drive into it "
                           "and see which row it appears in on the main screen (rename it here)."))
        self.port_table = QTableWidget(len(self.ports), 3)
        self.port_table.setHorizontalHeaderLabels(["Show", "Name (optional)", "Where"])
        hidden = set(settings.hidden_ports)
        for r, p in enumerate(self.ports):
            show = QTableWidgetItem(); show.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled)
            show.setCheckState(Qt.Unchecked if p.key in hidden else Qt.Checked)
            self.port_table.setItem(r, 0, show)
            self.port_table.setItem(r, 1, QTableWidgetItem(settings.port_names.get(p.key, "")))
            where = QTableWidgetItem(f"{p.where}  ·  {p.key}"); where.setFlags(Qt.ItemIsEnabled)
            self.port_table.setItem(r, 2, where)
        self.port_table.setColumnWidth(0, 50); self.port_table.setColumnWidth(1, 180)
        self.port_table.horizontalHeader().setStretchLastSection(True)
        self.port_table.verticalHeader().setVisible(False)
        pv.addWidget(self.port_table)
        if not self.ports:
            pv.addWidget(_note("No ports to show right now (Simulator mode is on, or none were detected)."))
        tabs.addTab(pt, "Ports")

        # ---- Sanitization
        f = _form()
        self.passes = QSpinBox(); self.passes.setRange(0, 7); self.passes.setValue(settings.overwrite_passes)
        self.edge = QSpinBox(); self.edge.setRange(1, 64); self.edge.setValue(settings.zero_edge_mb)
        self.verify = QCheckBox(); self.verify.setChecked(settings.verify_readback)
        f.addRow(_label("Overwrite passes (0 = none)",
                        "How many times the whole drive is overwritten with random data before encryption. "
                        "0 only removes the partition tables and the first and last megabytes. More passes take longer "
                        "and, on flash drives, do not guarantee more."), self.passes)
        f.addRow(_label("Zero first/last N MB",
                        "Overwrites the start and end of the drive with zeros, where partition tables, boot code and "
                        "backup headers live."), self.edge)
        _check(f, "Verify the final pass by reading back", self.verify,
               "Reads the drive back after the last pass to confirm the data was written. Slower, safer.")
        self.estimate = _note("")
        f.addRow(self.estimate)
        f.addRow(_note("Overwrite on flash media is NIST 800-88 'Clear', not 'Purge'."))
        self.passes.valueChanged.connect(self._update_estimate); self._update_estimate()
        tabs.addTab(_page(f), "Sanitization")

        # ---- Encryption
        f = _form()
        self.method = _combo([(method_label(m), m) for m in ENCRYPTION_METHODS],
                             settings.encryption_method, {m: f"{METHOD_INFO[m][0]}. {METHOD_INFO[m][1]}" for m in METHOD_INFO})
        self.fs = _combo([(x, x) for x in FILESYSTEMS], settings.filesystem, {k: f"{v[0]}. {v[1]}" for k, v in FS_INFO.items()})
        self.pstyle = _combo([(x, x) for x in PARTITION_STYLES], settings.partition_style,
                             {k: f"{v[0]}. {v[1]}" for k, v in PSTYLE_INFO.items()})
        self.label = QLineEdit(settings.volume_label); self.label.setMaxLength(11)
        self.full = QCheckBox(); self.full.setChecked(settings.full_volume_encryption)
        self.conflict = _combo([("Refuse and report the conflict", "block"), ("Use the policy's method", "use_policy")],
                               settings.on_policy_method_conflict)
        f.addRow(_label("Encryption method", "The BitLocker cipher. Hover over the choices for what each means; "
                                              "the notes below explain the choice you have selected."), self.method)
        f.addRow(_label("Filesystem", "How the drive is formatted. It decides which devices can read the drive "
                                      "and how large a file can be."), self.fs)
        f.addRow(_label("Partition style", "How the drive's partition table is laid out. It decides which devices "
                                           "can read the drive and how much of a large drive is usable."), self.pstyle)
        f.addRow(_label("Volume label", "The name the drive shows in Explorer. Letters, digits, space, _ and - only; "
                                        "11 characters at most."), self.label)
        _check(f, "Encrypt the full volume", self.full,
               "Encrypts every sector, including free space, so nothing left over from earlier use can be read. "
               "Slower than used-space-only, which encrypts only the sectors that currently hold files and leaves "
               "old deleted data in free space readable.")
        f.addRow(_label("If group policy forces another method",
                        "Your organisation's BitLocker policy can force one cipher. 'Refuse' stops and reports the "
                        "conflict; 'Use the policy's method' encrypts with the policy's cipher instead of the one chosen here."),
                 self.conflict)
        self.enc_info = QLabel()
        self.enc_info.setWordWrap(True)
        self.enc_info.setTextFormat(Qt.RichText)
        self.enc_info.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        self.enc_info.setMargin(10)
        box = QFrame(); box.setFrameShape(QFrame.StyledPanel)
        bl = QVBoxLayout(box); bl.addWidget(self.enc_info)
        f.addRow(box)
        for c in (self.method, self.fs, self.pstyle):
            c.currentIndexChanged.connect(self._update_enc_info)
        self._update_enc_info()
        tabs.addTab(_page(f), "Encryption")

        # ---- Passwords
        f = _form()
        self.pmode = _combo([("Same password for every drive", "fixed"), ("New random password per drive", "generated"),
                             ("Ask me for each batch", "prompt")], settings.password_mode)
        saved = settings.get_fixed_password()
        self.had_password = bool(settings.fixed_password_enc)
        self.fixed = QLineEdit(saved)                # shown in clear: it is handed to everyone who receives a drive
        self.fixed.setPlaceholderText("could not read the saved password (saved by another Windows account); type it again"
                                      if self.had_password and not saved else "type the password to use on every drive")
        self.gen = QPushButton("Generate")
        self.gen.clicked.connect(self._generate_fixed)
        self.fixed_field = QWidget(); h = QHBoxLayout(self.fixed_field); h.setContentsMargins(0, 0, 0, 0)
        h.addWidget(self.fixed, 1); h.addWidget(self.gen)
        self.glen = QSpinBox(); self.glen.setRange(12, 64); self.glen.setValue(settings.generated_length)
        self.minlen = QSpinBox(); self.minlen.setRange(8, 64); self.minlen.setValue(settings.min_password_length)
        f.addRow(_label("Password mode", "Where each drive's password comes from."), self.pmode)
        self.fixed_label = _label("Password for all drives",
                                  "Shown here in clear text on purpose: every drive gets this same password and the "
                                  "people who receive the drives are told it. One leak opens every drive.")
        f.addRow(self.fixed_label, self.fixed_field)
        self.glen_label = _label("Length of generated passwords",
                                 "Number of characters. Used by the Generate button, or for every drive in "
                                 "'New random password per drive' mode.")
        f.addRow(self.glen_label, self.glen)
        f.addRow(_label("Minimum length (app floor)", "Passwords shorter than this are refused. Your BitLocker "
                                                      "policy may require more."), self.minlen)
        self.mode_note = _note("")
        f.addRow(self.mode_note)
        f.addRow(_note("The saved password is encrypted for this Windows account only."))
        self.pmode.currentIndexChanged.connect(self._sync_password_mode); self._sync_password_mode()
        tabs.addTab(_page(f), "Passwords")

        # ---- Records
        f = _form()
        self.csvdir = QLineEdit(settings.resolved_csv_dir()); self.pdfdir = QLineEdit(settings.resolved_pdf_dir())
        self.sec_csv = QCheckBox(); self.sec_csv.setChecked(settings.include_secrets_csv)
        self.sec_pdf = QCheckBox(); self.sec_pdf.setChecked(settings.include_secrets_pdf)
        f.addRow(_label("CSV folder"), _folder_row(self.csvdir, default_csv_dir()))
        f.addRow(_label("PDF folder"), _folder_row(self.pdfdir, default_pdf_dir()))
        _check(f, "Put passwords in the CSV", self.sec_csv,
               "The CSV is one running log for all drives. With this on, it becomes a list of every password.")
        _check(f, "Put passwords in each drive's PDF", self.sec_pdf,
               "Each drive gets its own PDF, so its recovery key can travel with that drive's paperwork.")
        self.rec_warn = _note("", warn=True)
        f.addRow(self.rec_warn)
        f.addRow(_note("Defaults live in the 'data' folder next to the app. Move the folders anywhere you like, "
                       "but keep them on a restricted, backed-up location and never on a drive this station wipes."))
        if DATA_DIR_FALLBACK:
            f.addRow(_note("The app folder is not writable, so the per-user profile folder is used for data.", warn=True))
        self.sec_csv.toggled.connect(self._sync_record_warning); self.sec_pdf.toggled.connect(self._sync_record_warning)
        self._sync_record_warning()
        tabs.addTab(_page(f), "Records")

        # ---- Safety
        f = _form()
        self.dry = QCheckBox(); self.dry.setChecked(settings.dry_run)
        self.fixeddisk = QCheckBox(); self.fixeddisk.setChecked(settings.allow_fixed_disks)
        self.minsz = QDoubleSpinBox(); self.minsz.setRange(0.1, 4096); self.minsz.setValue(settings.min_size_gb)
        self.maxsz = QDoubleSpinBox(); self.maxsz.setRange(1, 8192); self.maxsz.setValue(settings.max_size_gb)
        self.confirm = QCheckBox(); self.confirm.setChecked(settings.require_confirm_if_content)
        self.stall = QSpinBox(); self.stall.setRange(3, 240); self.stall.setValue(settings.stall_minutes)
        self.reuse = QCheckBox(); self.reuse.setChecked(settings.reuse_compliant_drives)
        _check(f, "Dry run: scan and plan only, never erase", self.dry,
               "While this is on, drives are scanned and the plan is shown, but nothing is ever written. "
               "Turning it off lets the app permanently erase the drives you process; the banner turns red as a reminder.")
        _check(f, "Allow USB-attached fixed disks", self.fixeddisk,
               "Off by default. Some USB hard disks and SSDs report themselves as fixed disks. "
               "Turn this on only if you mean to provision those.")
        f.addRow(_label("Minimum drive size (GB)", "Smaller drives are rejected."), self.minsz)
        f.addRow(_label("Maximum drive size (GB)", "Larger drives are rejected. A guard against picking the wrong disk."), self.maxsz)
        f.addRow(_label("Warn if a drive stalls (minutes)",
                        "A drive that makes no progress for this long is flagged on its tile and in the batch bar, "
                        "with a taskbar flash. Nothing is stopped. Large, slow drives can pause for a while; "
                        "15 minutes is a sensible start."), self.stall)
        _check(f, "Confirm before erasing a drive that has files", self.confirm,
               "Shows a confirmation with the file count before a drive that holds files is erased.")
        _check(f, "Skip drives already compliant and in the records", self.reuse,
               "Off by default: an empty drive can still hold recoverable deleted data.")
        tabs.addTab(_page(f), "Safety")

        # ---- Updates
        f = _form()
        self.check_updates = QCheckBox(); self.check_updates.setChecked(settings.check_updates)
        _check(f, "Check for updates at start-up", self.check_updates,
               "Asks GitHub for the latest release. Nothing but a normal web request is sent.")
        configured = updater.enabled()
        f.addRow(_note(f"Source: github.com/{UPDATE_REPO}" if configured else
                       "Update source not configured yet (UPDATE_REPO in appinfo.py), so checks are off.", warn=not configured))
        btn = QPushButton("Check now"); btn.setEnabled(configured)
        btn.clicked.connect(self._check_now)
        f.addRow(btn)
        f.addRow(_note("Updates are never installed without your confirmation, and are verified against a "
                       "published SHA-256 first."))
        tabs.addTab(_page(f), "Updates")

        bb = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        bb.accepted.connect(self._save); bb.rejected.connect(self.reject)
        v = QVBoxLayout(self); v.addWidget(tabs); v.addWidget(bb)

    # ------------------------------------------------------------ helpers
    def _update_estimate(self) -> None:
        n = self.passes.value()
        if n == 0:
            self.estimate.setText("No overwrite pass: only partitions and the first/last MB are cleared.")
        else:
            hours = n * 64 * 1024 / 30 / 3600
            self.estimate.setText(f"{n} pass(es) over a 64 GB drive at about 30 MB/s takes roughly {hours:.1f} hours.")

    def _update_enc_info(self) -> None:
        m, fs, ps = self.method.currentData(), self.fs.currentData(), self.pstyle.currentData()
        mt, md = METHOD_INFO[m]
        ft, fd = FS_INFO[fs]
        pt, pd = PSTYLE_INFO[ps]
        parts = [f"<b>{method_label(m)}</b> &ndash; {html.escape(mt)}. {html.escape(md)}",
                 f"<b>{fs}</b> &ndash; {html.escape(ft)}. {html.escape(fd)}",
                 f"<b>{ps}</b> &ndash; {html.escape(pt)}. {html.escape(pd)}"]
        warn = []
        if m in ("Aes128", "Aes256"):
            warn.append("Older cipher selected: drives will not be the strongest this app can make. Use it only for "
                        "compatibility with old Windows.")
        if ps == "MBR":
            warn.append("MBR cannot use more than 2 TB of a drive.")
        if fs == "FAT32":
            warn.append("FAT32 cannot hold files over 4 GB and will not format above 32 GB with Windows' tools.")
        parts.append(html.escape(FIPS_NOTE))
        fips = fips_mode_enabled()
        if fips is True:
            warn.append("Windows' FIPS policy ('Use FIPS compliant algorithms') is ON on this PC. In that mode Windows "
                        "will not create a BitLocker recovery password (Microsoft KB), which this app adds to every drive, "
                        "so encryption can fail here.")
        elif fips is False:
            parts.append("The Windows FIPS policy is off on this PC.")
        text = "<br><br>".join(parts)
        if warn:
            text += "<br><br><span style='color:#d97706'>" + "<br>".join("&#9888; " + html.escape(w) for w in warn) + "</span>"
        self.enc_info.setText(text)

    def _sync_password_mode(self) -> None:
        mode = self.pmode.currentData()
        for w in (self.fixed_label, self.fixed_field):
            w.setVisible(mode == "fixed")
        for w in (self.glen_label, self.glen):
            w.setVisible(mode in ("fixed", "generated"))
        self.mode_note.setText({
            "fixed": "Every drive gets the same password, so one leak opens every drive shipped with it.",
            "generated": "Each drive gets its own random password, written into that drive's record and PDF.",
            "prompt": "You type one password at the start of each batch; nothing is stored.",
        }[mode])

    def _generate_fixed(self) -> None:
        self.fixed.setText(generate_passphrase(self.glen.value()))

    def _sync_record_warning(self) -> None:
        if self.sec_csv.isChecked():
            self.rec_warn.setText("The CSV will hold every password and recovery key in clear text: a master key list. "
                                  "Restrict access to its folder.")
        elif self.sec_pdf.isChecked():
            self.rec_warn.setText("Each drive's PDF will contain its password and recovery key. Store the PDF folder securely.")
        else:
            self.rec_warn.setText("Neither record will contain passwords or recovery keys. Make sure you store them elsewhere, "
                                  "or a lost password means a lost drive.")

    def _check_now(self) -> None:
        p = self.parent()
        if p is not None and hasattr(p, "check_updates_now"):
            p.check_updates_now()

    # ------------------------------------------------------------ save
    def _fail(self, title: str, text: str) -> None:
        QMessageBox.warning(self, title, text)

    def _save(self) -> None:
        s = self.s
        if not self.dry.isChecked() and s.dry_run:
            if not IS_WINDOWS:
                return self._fail("Not available", "Erasing drives requires Windows.")
            if self.erase_blocked_reason:
                return self._fail("Erasing is not available", self.erase_blocked_reason)
        mode, new_pw = self.pmode.currentData(), self.fixed.text()
        if mode == "fixed" and not new_pw:
            return self._fail("Password needed", "'Same password for every drive' needs a password.")
        if mode == "fixed" and len(new_pw) < self.minlen.value():
            return self._fail("Password too short", f"Minimum length is {self.minlen.value()}.")
        if self.minsz.value() > self.maxsz.value():
            return self._fail("Size limits", "The minimum size is larger than the maximum size.")
        csv_dir = self.csvdir.text().strip() or default_csv_dir()
        pdf_dir = self.pdfdir.text().strip() or default_pdf_dir()
        for d in (csv_dir, pdf_dir):
            try:
                Path(d).mkdir(parents=True, exist_ok=True)
                probe = Path(d) / ".write_test"; probe.write_text("x"); probe.unlink()
            except OSError as e:
                return self._fail("Folder not writable", f"{d}\n{e}")
        keys = {p.key for p in self.ports}
        s.hidden_ports = [k for k in s.hidden_ports if k not in keys]           # keep ones not currently present
        s.port_names = {k: v for k, v in s.port_names.items() if k not in keys}
        for r, p in enumerate(self.ports):
            if self.port_table.item(r, 0).checkState() != Qt.Checked:
                s.hidden_ports.append(p.key)
            nm = self.port_table.item(r, 1).text().strip()
            if nm:
                s.port_names[p.key] = nm
        s.overwrite_passes = self.passes.value(); s.zero_edge_mb = self.edge.value(); s.stall_minutes = self.stall.value()
        s.verify_readback = self.verify.isChecked()
        s.encryption_method = self.method.currentData(); s.filesystem = self.fs.currentData()
        s.partition_style = self.pstyle.currentData(); s.volume_label = self.label.text() or DEFAULT_VOLUME_LABEL
        s.full_volume_encryption = self.full.isChecked(); s.on_policy_method_conflict = self.conflict.currentData()
        s.password_mode = mode; s.generated_length = self.glen.value()
        s.min_password_length = self.minlen.value()
        if mode == "fixed" and new_pw != s.get_fixed_password():
            s.set_fixed_password(new_pw)
        s.csv_dir = csv_dir; s.pdf_dir = pdf_dir
        s.include_secrets_csv = self.sec_csv.isChecked(); s.include_secrets_pdf = self.sec_pdf.isChecked()
        s.dry_run = self.dry.isChecked()
        s.allow_fixed_disks = self.fixeddisk.isChecked(); s.min_size_gb = self.minsz.value(); s.max_size_gb = self.maxsz.value()
        s.require_confirm_if_content = self.confirm.isChecked(); s.reuse_compliant_drives = self.reuse.isChecked()
        s.check_updates = self.check_updates.isChecked()
        s.validate()
        s.save()
        self.accept()
