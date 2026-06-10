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
"""Status daemon for the SpacePilot Pro LCD, in the spirit of 3DxWare's
LCD applets on Windows.

Pages (cycle with the Left/Right bezel keys, or pick from the Menu):
  1. Button mappings  - spacenavd assignments from ~/.spnavrc or /etc/spnavrc
  2. Clock            - big clock with date
  3. System monitor   - live CPU / RAM / GPU usage, temperatures (2s refresh)

Bezel keys (these report on the LCD USB interface, endpoint 0x81 — they are
invisible to spacenavd; every press gives on-screen feedback):
  Left / Right  previous / next page
  Up / Down     backlight brightness (with on-screen bar)
  Light         backlight on/off
  Menu          open the page menu (Up/Down select, OK confirm, Back cancel)
  OK            confirm in menu; force refresh otherwise
  Back          close menu / return to first page
  Settings      help overlay with this key reference

Note: while the daemon runs it holds the LCD interface claimed; stop it
(systemctl --user stop spacepilot-lcd) before using spplcd.py manually.
"""

import glob
import os
import platform
import shutil
import signal
import subprocess
import sys
import time
from datetime import datetime

import usb.core
from PIL import Image, ImageDraw

from spplcd import WIDTH, HEIGHT, SpacePilotLCD, load_font

CONFIG_PATHS = [os.path.expanduser("~/.spnavrc"), "/etc/spnavrc"]
KEY_ENDPOINT = 0x81

# Bezel key bitmask (same codes as the Logitech G19 display keys).
KEY_SETTINGS, KEY_BACK, KEY_MENU, KEY_OK = 0x01, 0x02, 0x04, 0x08
KEY_RIGHT, KEY_LEFT, KEY_DOWN, KEY_UP = 0x10, 0x20, 0x40, 0x80
KEY_LIGHT = 0x200

PAGES = ["Button mappings", "Clock", "System monitor"]
SYSTEM_PAGE = 2
OSD_SECONDS = 1.8
HELP_SECONDS = 6

ACTION_LABELS = {
    "sensitivity-up": "Sensitivity +",
    "sensitivity-down": "Sensitivity -",
    "sensitivity-reset": "Sensitivity reset",
    "disable-rotation": "Pan/zoom only",
    "disable-translation": "Rotation only",
    "dominant-axis": "Dominant axis",
    "none": "(none)",
}

# Physical key names of the SpacePilot Pro, by spacenavd button number.
# Source: Blender's GHOST NDOF map for device 046d:c629 (31 buttons).
BUTTON_NAMES = {
    0: "Menu", 1: "Fit", 2: "Top", 3: "Left", 4: "Right", 5: "Front",
    6: "Bottom", 7: "Back", 8: "RollCW", 9: "RollCC", 10: "ISO1", 11: "ISO2",
    **{12 + n: str(n + 1) for n in range(10)},
    22: "Esc", 23: "Alt", 24: "Shift", 25: "Ctrl", 26: "Rot", 27: "Pan",
    28: "Dom", 29: "+", 30: "-",
}


# --------------------------------------------------------------------------
# spnavrc parsing
# --------------------------------------------------------------------------

def find_config():
    for path in CONFIG_PATHS:
        if os.path.isfile(path):
            return path
    return None


def parse_mappings(path):
    """Return a sorted list of (button_number, label) from an spnavrc file."""
    mappings = {}
    with open(path) as fp:
        for line in fp:
            line = line.split("#", 1)[0].strip()
            if "=" not in line:
                continue
            key, value = (part.strip() for part in line.split("=", 1))
            for prefix, fmt in (("bnact", lambda v: ACTION_LABELS.get(v, v)),
                                ("kbmap", lambda v: f"Key: {v}"),
                                ("bnmap", lambda v: f"-> button {v}")):
                if key.startswith(prefix) and key[len(prefix):].isdigit():
                    mappings[int(key[len(prefix):])] = fmt(value)
    return sorted(mappings.items())


# --------------------------------------------------------------------------
# System metrics (Linux: /proc + sysfs; AMD via amdgpu, NVIDIA via nvidia-smi)
# --------------------------------------------------------------------------

class SystemStats:
    def __init__(self):
        self._prev_cpu = None
        self._gpu_dir = None
        for path in sorted(glob.glob("/sys/class/drm/card*/device/gpu_busy_percent")):
            self._gpu_dir = os.path.dirname(path)
            break
        self._nvidia = self._gpu_dir is None and shutil.which("nvidia-smi")
        self._cpu_temp_file = self._find_temp(("k10temp", "zenpower", "coretemp"))
        self._gpu_temp_file = self._find_temp(("amdgpu",))

    @staticmethod
    def _find_temp(names):
        for hwmon in glob.glob("/sys/class/hwmon/hwmon*"):
            try:
                with open(os.path.join(hwmon, "name")) as fp:
                    if fp.read().strip() in names:
                        candidate = os.path.join(hwmon, "temp1_input")
                        if os.path.isfile(candidate):
                            return candidate
            except OSError:
                continue
        return None

    @staticmethod
    def _read_int(path):
        try:
            with open(path) as fp:
                return int(fp.read().strip())
        except (OSError, ValueError):
            return None

    def cpu_percent(self):
        with open("/proc/stat") as fp:
            fields = [int(v) for v in fp.readline().split()[1:]]
        idle, total = fields[3] + fields[4], sum(fields)
        if self._prev_cpu is None:
            self._prev_cpu = (idle, total)
            return 0.0
        didle, dtotal = idle - self._prev_cpu[0], total - self._prev_cpu[1]
        self._prev_cpu = (idle, total)
        return 100.0 * (dtotal - didle) / dtotal if dtotal > 0 else 0.0

    def memory(self):
        total = avail = 0
        with open("/proc/meminfo") as fp:
            for line in fp:
                if line.startswith("MemTotal:"):
                    total = int(line.split()[1])
                elif line.startswith("MemAvailable:"):
                    avail = int(line.split()[1])
        return (total - avail) / 1048576, total / 1048576  # GiB

    def gpu(self):
        """Return (busy %, vram used GiB, vram total GiB, temp C) or None."""
        if self._gpu_dir:
            busy = self._read_int(os.path.join(self._gpu_dir, "gpu_busy_percent"))
            used = self._read_int(os.path.join(self._gpu_dir, "mem_info_vram_used"))
            total = self._read_int(os.path.join(self._gpu_dir, "mem_info_vram_total"))
            temp = self._read_int(self._gpu_temp_file) if self._gpu_temp_file else None
            return (busy or 0, (used or 0) / 2**30, (total or 0) / 2**30,
                    temp / 1000 if temp else None)
        if self._nvidia:
            try:
                out = subprocess.run(
                    ["nvidia-smi", "--query-gpu=utilization.gpu,memory.used,"
                     "memory.total,temperature.gpu", "--format=csv,noheader,nounits"],
                    capture_output=True, text=True, timeout=2).stdout.split(",")
                return (float(out[0]), float(out[1]) / 1024,
                        float(out[2]) / 1024, float(out[3]))
            except Exception:
                return None
        return None

    def cpu_temp(self):
        temp = self._read_int(self._cpu_temp_file) if self._cpu_temp_file else None
        return temp / 1000 if temp else None


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------

def page_base(title):
    img = Image.new("RGB", (WIDTH, HEIGHT), (10, 10, 30))
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, WIDTH - 1, 30], fill=(30, 30, 70))
    d.text((8, 15), title, fill=(255, 255, 255), anchor="lm", font=load_font(20))
    d.text((WIDTH - 8, 15), datetime.now().strftime("%H:%M"),
           fill=(0, 255, 255), anchor="rm", font=load_font(20))
    return img, d


def render_mappings():
    img, d = page_base("SpacePilot Pro")
    entry_font, small_font = load_font(15), load_font(12)
    config_path = find_config()
    mappings = parse_mappings(config_path) if config_path else []
    if not mappings:
        d.text((WIDTH // 2, HEIGHT // 2), "No button mappings in spnavrc",
               fill=(255, 255, 0), anchor="mm", font=entry_font)
        d.text((WIDTH // 2, HEIGHT // 2 + 24), "(spacenavd defaults active)",
               fill=(160, 160, 160), anchor="mm", font=small_font)
    else:
        per_col, top, line_h = 9, 40, 21
        for n, (button, label) in enumerate(mappings[:per_col * 2]):
            col, row = divmod(n, per_col)
            x, y = 8 + col * (WIDTH // 2), top + row * line_h
            name = BUTTON_NAMES.get(button, f"B{button}")
            d.text((x, y), f"[{name}]", fill=(0, 255, 0), font=entry_font)
            d.text((x + 56, y), label[:15], fill=(255, 255, 255), font=entry_font)
        if len(mappings) > per_col * 2:
            d.text((WIDTH - 8, HEIGHT - 14), f"+{len(mappings) - per_col * 2} more",
                   fill=(160, 160, 160), anchor="rm", font=small_font)
    source = os.path.basename(config_path) if config_path else "no spnavrc found"
    d.text((8, HEIGHT - 14), source, fill=(100, 100, 140),
           anchor="lm", font=small_font)
    return img


def render_clock():
    img, d = page_base("Clock")
    now = datetime.now()
    d.text((WIDTH // 2, 110), now.strftime("%H:%M"), fill=(0, 255, 255),
           anchor="mm", font=load_font(72))
    d.text((WIDTH // 2, 180), now.strftime("%A %d %B %Y"),
           fill=(255, 255, 255), anchor="mm", font=load_font(17))
    return img


def _bar(d, x, y, w, h, fraction, color):
    fraction = max(0.0, min(1.0, fraction))
    d.rectangle([x, y, x + w, y + h], outline=(90, 90, 120))
    if fraction > 0:
        d.rectangle([x + 1, y + 1, x + 1 + int((w - 2) * fraction), y + h - 1],
                    fill=color)


def _usage_color(fraction):
    if fraction < 0.6:
        return (0, 200, 80)
    if fraction < 0.85:
        return (240, 200, 0)
    return (230, 40, 40)


def render_system(stats):
    img, d = page_base(platform.node() or "System")
    font, small = load_font(16), load_font(13)

    cpu = stats.cpu_percent()
    cpu_temp = stats.cpu_temp()
    mem_used, mem_total = stats.memory()
    gpu = stats.gpu()

    def row(y, label, fraction, text, temp):
        d.text((10, y), label, fill=(255, 255, 255), font=font)
        _bar(d, 60, y + 1, 150, 14, fraction, _usage_color(fraction))
        d.text((218, y), text, fill=(200, 200, 200), font=small)
        if temp is not None:
            d.text((WIDTH - 8, y), f"{temp:.0f}C", fill=(0, 255, 255),
                   anchor="ra", font=small)

    row(48, "CPU", cpu / 100, f"{cpu:3.0f}%", cpu_temp)
    row(86, "RAM", mem_used / mem_total if mem_total else 0,
        f"{mem_used:.1f}/{mem_total:.0f}G", None)
    if gpu is not None:
        busy, vused, vtotal, gtemp = gpu
        row(124, "GPU", busy / 100, f"{busy:3.0f}%", gtemp)
        row(162, "VRAM", vused / vtotal if vtotal else 0,
            f"{vused:.1f}/{vtotal:.0f}G", None)
    else:
        d.text((10, 124), "GPU: n/a", fill=(120, 120, 120), font=font)

    load1, load5, load15 = os.getloadavg()
    d.text((10, HEIGHT - 18), f"load {load1:.2f} {load5:.2f} {load15:.2f}",
           fill=(100, 100, 140), font=small)
    return img


def draw_menu(img, selection):
    d = ImageDraw.Draw(img)
    font = load_font(17)
    box_w, line_h = 220, 30
    box_h = line_h * len(PAGES) + 44
    x0, y0 = (WIDTH - box_w) // 2, (HEIGHT - box_h) // 2
    d.rectangle([x0, y0, x0 + box_w, y0 + box_h], fill=(20, 20, 50),
                outline=(120, 120, 200), width=2)
    d.text((x0 + box_w // 2, y0 + 16), "Menu", fill=(255, 255, 255),
           anchor="mm", font=load_font(18))
    for n, name in enumerate(PAGES):
        y = y0 + 34 + n * line_h
        if n == selection:
            d.rectangle([x0 + 6, y - 2, x0 + box_w - 6, y + line_h - 8],
                        fill=(60, 60, 140))
        d.text((x0 + 16, y + line_h // 2 - 5), name, fill=(255, 255, 255),
               anchor="lm", font=font)
    return img


def draw_help(img):
    d = ImageDraw.Draw(img)
    font = load_font(14)
    d.rectangle([10, 36, WIDTH - 10, HEIGHT - 10], fill=(20, 20, 50),
                outline=(120, 120, 200), width=2)
    lines = [
        ("Left/Right", "previous / next page"),
        ("Up/Down", "backlight brightness"),
        ("Light", "backlight on/off"),
        ("Menu", "page menu (OK confirm)"),
        ("OK", "refresh page"),
        ("Back", "close / first page"),
        ("Settings", "this help"),
    ]
    for n, (key, desc) in enumerate(lines):
        y = 46 + n * 22
        d.text((22, y), key, fill=(0, 255, 0), font=font)
        d.text((120, y), desc, fill=(255, 255, 255), font=font)
    return img


def draw_osd(img, text, fraction=None):
    d = ImageDraw.Draw(img)
    d.rectangle([20, HEIGHT - 48, WIDTH - 20, HEIGHT - 14], fill=(20, 20, 50),
                outline=(120, 120, 200))
    if fraction is None:
        d.text((WIDTH // 2, HEIGHT - 31), text, fill=(255, 255, 255),
               anchor="mm", font=load_font(15))
    else:
        d.text((30, HEIGHT - 31), text, fill=(255, 255, 255), anchor="lm",
               font=load_font(15))
        _bar(d, 150, HEIGHT - 38, 150, 14, fraction, (0, 200, 255))
    return img


# --------------------------------------------------------------------------
# Main loop
# --------------------------------------------------------------------------

def main():
    running = True

    def stop(*_):
        nonlocal running
        running = False

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    stats = SystemStats()
    lcd = None
    page = 0
    brightness = 90
    light_on = True
    pressed_mask = 0
    menu_open = False
    menu_sel = 0
    help_until = 0.0
    osd = None          # (text, fraction or None, expiry)
    dirty = True
    last_render = 0.0
    last_minute = None
    last_mtime = None

    def set_osd(text, fraction=None, seconds=OSD_SECONDS):
        nonlocal osd, dirty
        osd = (text, fraction, time.time() + seconds)
        dirty = True

    def handle_key(bit):
        nonlocal page, brightness, light_on, menu_open, menu_sel
        nonlocal help_until, dirty
        if bit == KEY_LIGHT:
            light_on = not light_on
            lcd.set_brightness(brightness if light_on else 0)
            if light_on:
                set_osd("Backlight on")
            return
        if bit == KEY_UP and not menu_open:
            brightness = min(100, brightness + 10)
            light_on = True
            lcd.set_brightness(brightness)
            set_osd("Brightness", brightness / 100)
        elif bit == KEY_DOWN and not menu_open:
            brightness = max(5, brightness - 10)
            light_on = True
            lcd.set_brightness(brightness)
            set_osd("Brightness", brightness / 100)
        elif bit == KEY_LEFT:
            page = (page - 1) % len(PAGES)
            menu_open = False
            set_osd(PAGES[page])
        elif bit == KEY_RIGHT:
            page = (page + 1) % len(PAGES)
            menu_open = False
            set_osd(PAGES[page])
        elif bit == KEY_MENU:
            menu_open = not menu_open
            menu_sel = page
            help_until = 0
            dirty = True
        elif bit == KEY_UP and menu_open:
            menu_sel = (menu_sel - 1) % len(PAGES)
            dirty = True
        elif bit == KEY_DOWN and menu_open:
            menu_sel = (menu_sel + 1) % len(PAGES)
            dirty = True
        elif bit == KEY_OK:
            if menu_open:
                page, menu_open = menu_sel, False
                set_osd(PAGES[page])
            else:
                set_osd("Refreshed")
        elif bit == KEY_BACK:
            if menu_open or help_until:
                menu_open, help_until = False, 0
            else:
                page = 0
                set_osd(PAGES[0])
            dirty = True
        elif bit == KEY_SETTINGS:
            help_until = time.time() + HELP_SECONDS
            menu_open = False
            dirty = True

    while running:
        if lcd is None:
            try:
                lcd = SpacePilotLCD()
                lcd.set_brightness(brightness if light_on else 0)
                dirty = True
            except (IOError, usb.core.USBError) as err:
                print(f"LCD unavailable, retrying: {err}", file=sys.stderr)
                time.sleep(10)
                continue
        try:
            now = time.time()
            minute = datetime.now().strftime("%H:%M")
            config = find_config()
            mtime = os.path.getmtime(config) if config else None
            if minute != last_minute or mtime != last_mtime:
                last_minute, last_mtime = minute, mtime
                dirty = True
            if page == SYSTEM_PAGE and now - last_render >= 2:
                dirty = True
            if osd and now >= osd[2]:
                osd = None
                dirty = True
            if help_until and now >= help_until:
                help_until = 0
                dirty = True

            if dirty:
                if page == SYSTEM_PAGE:
                    img = render_system(stats)
                elif PAGES[page] == "Clock":
                    img = render_clock()
                else:
                    img = render_mappings()
                if menu_open:
                    img = draw_menu(img, menu_sel)
                elif help_until:
                    img = draw_help(img)
                if osd:
                    img = draw_osd(img, osd[0], osd[1])
                lcd.send_image(img)
                last_render = now
                dirty = False

            # Poll the bezel keys; this also paces the loop (~0.5s).
            try:
                data = lcd.dev.read(KEY_ENDPOINT, 8, timeout=500)
            except usb.core.USBTimeoutError:
                continue
            for i in range(0, len(data) - 1, 2):
                mask = data[i] | (data[i + 1] << 8)
                new_bits = mask & ~pressed_mask
                pressed_mask = mask
                for shift in range(16):
                    bit = 1 << shift
                    if new_bits & bit:
                        handle_key(bit)
        except usb.core.USBError as err:
            print(f"USB error, reconnecting: {err}", file=sys.stderr)
            try:
                lcd.close()
            except Exception:
                pass
            lcd = None
            time.sleep(5)

    if lcd is not None:
        lcd.close()


if __name__ == "__main__":
    main()
