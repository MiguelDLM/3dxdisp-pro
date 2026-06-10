#!/usr/bin/env python3
# This file is part of spacepilot-pro-lcd.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
"""Settings application for the SpacePilot Pro LCD daemon (Qt / PySide6).

Configure which pages the LCD shows and in which order, every visual option
(clock styles, fonts, colors, time zones, dual clocks, calendar, system
monitor rows, 6DOF test), plus brightness, OSD and notification mirroring.
The live preview on the right is rendered with the daemon's own applet code,
so it is pixel-exact. Saving writes ~/.config/spacepilot-lcd/config.json and
the running daemon applies it immediately — no restart needed.
"""

import copy
import sys

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor, QImage, QPixmap
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QColorDialog, QComboBox, QDoubleSpinBox,
    QFormLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit, QListWidget,
    QListWidgetItem, QMainWindow, QMenu, QMessageBox, QPushButton,
    QScrollArea, QSlider, QSpinBox, QVBoxLayout, QWidget)

import applets
import lcdconfig
from spnav_client import State
from spplcd import WIDTH, HEIGHT
from sysstats import SystemStats

try:
    from zoneinfo import available_timezones
    TIMEZONES = [""] + sorted(available_timezones())
except Exception:
    TIMEZONES = ["", "UTC", "Europe/Madrid", "America/Mexico_City",
                 "America/New_York", "Asia/Tokyo"]

APPLET_LABELS = {
    "mappings": "Button mappings",
    "clock": "Clock",
    "calendar": "Calendar",
    "system": "System monitor",
    "input": "6DOF input test",
}

FIELD_LABELS = {
    "title": "Title",
    "style": "Style",
    "use_24h": "24-hour format",
    "show_seconds": "Show seconds",
    "show_date": "Show date",
    "font": "Font",
    "size": "Digit size",
    "color": "Color",
    "background": "Background",
    "highlight": "Highlight color",
    "timezone": "Time zone (empty = local)",
    "label": "Label",
    "second_timezone": "2nd time zone (dual clock)",
    "second_label": "2nd label",
    "week_starts_monday": "Week starts on Monday",
    "show_cpu": "Show CPU",
    "show_ram": "Show RAM",
    "show_gpu": "Show GPU",
    "show_vram": "Show VRAM",
    "show_net": "Show network rate",
    "refresh_seconds": "Refresh (seconds)",
    "axis_range": "Axis range (deflection)",
}


class ColorButton(QPushButton):
    def __init__(self, value, on_change):
        super().__init__()
        self._on_change = on_change
        self.set_value(value)
        self.clicked.connect(self._pick)

    def set_value(self, value):
        self._value = value
        self.setStyleSheet(
            f"background-color: {value}; border: 1px solid #888;"
            " min-height: 22px;")
        self.setText(value)

    def _pick(self):
        color = QColorDialog.getColor(QColor(self._value), self)
        if color.isValid():
            self.set_value(color.name())
            self._on_change(color.name())


def demo_spnav_state():
    state = State()
    state.connected = True
    state.axes = [120, -80, 230, -160, 60, -30]
    state.buttons = {1, 5, 29}
    return state


class SettingsWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("SpacePilot Pro LCD Settings")
        self.cfg = lcdconfig.load()
        self.stats = SystemStats()
        self.stats.cpu_percent()  # prime the delta
        self.demo_state = demo_spnav_state()

        root = QWidget()
        self.setCentralWidget(root)
        layout = QHBoxLayout(root)

        # ----- left column: page list + ordering ---------------------------
        left = QVBoxLayout()
        left.addWidget(QLabel("<b>Pages (display order)</b>"))
        self.page_list = QListWidget()
        self.page_list.currentRowChanged.connect(self._page_selected)
        left.addWidget(self.page_list, 1)

        row = QHBoxLayout()
        add_btn = QPushButton("Add")
        menu = QMenu(add_btn)
        for ptype, label in APPLET_LABELS.items():
            menu.addAction(label, lambda t=ptype: self._add_page(t))
        add_btn.setMenu(menu)
        remove_btn = QPushButton("Remove")
        remove_btn.clicked.connect(self._remove_page)
        up_btn = QPushButton("Up")
        up_btn.clicked.connect(lambda: self._move_page(-1))
        down_btn = QPushButton("Down")
        down_btn.clicked.connect(lambda: self._move_page(1))
        for b in (add_btn, remove_btn, up_btn, down_btn):
            row.addWidget(b)
        left.addLayout(row)

        # global settings
        glob = QGroupBox("General")
        gform = QFormLayout(glob)
        self.brightness = QSlider(Qt.Horizontal)
        self.brightness.setRange(5, 100)
        self.brightness.setValue(int(self.cfg["brightness"]))
        self.brightness.valueChanged.connect(
            lambda v: self.cfg.__setitem__("brightness", v))
        gform.addRow("Brightness", self.brightness)
        self.osd_secs = QDoubleSpinBox()
        self.osd_secs.setRange(0.5, 10)
        self.osd_secs.setSingleStep(0.5)
        self.osd_secs.setValue(float(self.cfg["osd_seconds"]))
        self.osd_secs.valueChanged.connect(
            lambda v: self.cfg.__setitem__("osd_seconds", v))
        gform.addRow("OSD duration (s)", self.osd_secs)
        self.notif_enable = QCheckBox("Mirror desktop notifications")
        self.notif_enable.setChecked(self.cfg["notifications"]["enabled"])
        self.notif_enable.toggled.connect(
            lambda v: self.cfg["notifications"].__setitem__("enabled", v))
        gform.addRow(self.notif_enable)
        self.notif_secs = QSpinBox()
        self.notif_secs.setRange(2, 30)
        self.notif_secs.setValue(int(self.cfg["notifications"]["seconds"]))
        self.notif_secs.valueChanged.connect(
            lambda v: self.cfg["notifications"].__setitem__("seconds", v))
        gform.addRow("Notification time (s)", self.notif_secs)
        left.addWidget(glob)

        layout.addLayout(left, 1)

        # ----- middle column: options for the selected page ----------------
        mid = QVBoxLayout()
        mid.addWidget(QLabel("<b>Page options</b>"))
        self.options_area = QScrollArea()
        self.options_area.setWidgetResizable(True)
        mid.addWidget(self.options_area, 1)
        layout.addLayout(mid, 1)

        # ----- right column: live preview + actions ------------------------
        right = QVBoxLayout()
        right.addWidget(QLabel("<b>Live preview</b>"))
        self.preview = QLabel()
        self.preview.setFixedSize(WIDTH * 2, HEIGHT * 2)
        self.preview.setStyleSheet("border: 2px solid #444;")
        right.addWidget(self.preview)
        right.addStretch(1)
        apply_btn = QPushButton("Apply (daemon reloads instantly)")
        apply_btn.clicked.connect(self._apply)
        right.addWidget(apply_btn)
        defaults_btn = QPushButton("Restore defaults")
        defaults_btn.clicked.connect(self._restore_defaults)
        right.addWidget(defaults_btn)
        layout.addLayout(right, 0)

        self._reload_page_list()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._update_preview)
        self.timer.start(1000)
        self._update_preview()

    # ----- page list management --------------------------------------------

    def _reload_page_list(self, select=0):
        self.page_list.blockSignals(True)
        self.page_list.clear()
        for p in self.cfg["pages"]:
            label = APPLET_LABELS.get(p["type"], p["type"])
            title = p.get("title") or ""
            item = QListWidgetItem(
                f"{label}" + (f"  -  {title}" if title != label else ""))
            self.page_list.addItem(item)
        self.page_list.blockSignals(False)
        if self.cfg["pages"]:
            select = max(0, min(select, len(self.cfg["pages"]) - 1))
            self.page_list.setCurrentRow(select)
        else:
            self._build_options(None)

    def _current_page(self):
        row = self.page_list.currentRow()
        if 0 <= row < len(self.cfg["pages"]):
            return self.cfg["pages"][row]
        return None

    def _page_selected(self, _row):
        self._build_options(self._current_page())
        self._update_preview()

    def _add_page(self, ptype):
        self.cfg["pages"].append(
            lcdconfig.applet_with_defaults({"type": ptype}))
        self._reload_page_list(len(self.cfg["pages"]) - 1)

    def _remove_page(self):
        row = self.page_list.currentRow()
        if 0 <= row < len(self.cfg["pages"]):
            del self.cfg["pages"][row]
            self._reload_page_list(row)

    def _move_page(self, delta):
        row = self.page_list.currentRow()
        new = row + delta
        if 0 <= row < len(self.cfg["pages"]) and 0 <= new < len(
                self.cfg["pages"]):
            pages = self.cfg["pages"]
            pages[row], pages[new] = pages[new], pages[row]
            self._reload_page_list(new)

    # ----- option form ------------------------------------------------------

    def _build_options(self, page):
        form_host = QWidget()
        form = QFormLayout(form_host)
        if page is None:
            form.addRow(QLabel("Add a page to configure it."))
        else:
            for key, default in lcdconfig.APPLET_DEFAULTS[
                    page["type"]].items():
                page.setdefault(key, default)
                form.addRow(FIELD_LABELS.get(key, key),
                            self._editor(page, key, page[key]))
        self.options_area.setWidget(form_host)

    def _editor(self, page, key, value):
        def setter(v):
            page[key] = v
            if key == "title":
                self._reload_page_list(self.page_list.currentRow())
            self._update_preview()

        if key in ("color", "background", "highlight"):
            return ColorButton(value, setter)
        if key == "font":
            combo = QComboBox()
            fonts = list(lcdconfig.available_fonts())
            combo.addItems(fonts)
            if value in fonts:
                combo.setCurrentText(value)
            combo.currentTextChanged.connect(setter)
            return combo
        if key in ("timezone", "second_timezone"):
            combo = QComboBox()
            combo.setEditable(True)
            combo.addItems(TIMEZONES)
            combo.setCurrentText(value)
            combo.currentTextChanged.connect(setter)
            return combo
        if key == "style":
            combo = QComboBox()
            combo.addItems(["digital", "analog"])
            combo.setCurrentText(value)
            combo.currentTextChanged.connect(setter)
            return combo
        if isinstance(value, bool):
            box = QCheckBox()
            box.setChecked(value)
            box.toggled.connect(setter)
            return box
        if isinstance(value, int):
            spin = QSpinBox()
            spin.setRange(1, 1000)
            spin.setValue(value)
            spin.valueChanged.connect(setter)
            return spin
        edit = QLineEdit(str(value))
        edit.textChanged.connect(setter)
        return edit

    # ----- preview / apply ---------------------------------------------------

    def _update_preview(self):
        page = self._current_page()
        if page is None:
            self.preview.clear()
            return
        ctx = {"stats": self.stats, "spnav": self.demo_state}
        try:
            img = applets.RENDERERS[page["type"]](page, ctx)
        except Exception as err:
            self.preview.setText(f"Preview error:\n{err}")
            return
        data = img.tobytes("raw", "RGB")
        qimg = QImage(data, img.width, img.height, img.width * 3,
                      QImage.Format_RGB888)
        self.preview.setPixmap(QPixmap.fromImage(qimg).scaled(
            WIDTH * 2, HEIGHT * 2, Qt.KeepAspectRatio))

    def _apply(self):
        lcdconfig.save(self.cfg)
        self.statusBar().showMessage(
            "Saved - the daemon applies it within a second.", 4000)

    def _restore_defaults(self):
        if QMessageBox.question(
                self, "Restore defaults",
                "Discard the current configuration and restore defaults?") \
                == QMessageBox.Yes:
            self.cfg = copy.deepcopy(lcdconfig.DEFAULT_CONFIG)
            self.cfg["pages"] = [lcdconfig.applet_with_defaults(p)
                                 for p in self.cfg["pages"]]
            self._reload_page_list()


def main():
    app = QApplication(sys.argv)
    win = SettingsWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
