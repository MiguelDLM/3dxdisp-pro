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
"""Status daemon for the SpacePilot Pro LCD.

Replicates the useful part of what 3DxWare shows on Windows:

- Page 1: spacenavd button assignments, read from ~/.spnavrc or /etc/spnavrc
  (keys bnactN / kbmapN / bnmapN), refreshed when the file changes.
- Page 2: a big clock.
- Page 3: system status (load, memory, uptime).

It also brings the bezel keys around the screen back to life. Those keys
never reach spacenavd (they report on the LCD USB interface, interrupt
endpoint 0x81, as a 2-byte bitmask — same as the Logitech G19 menu pad):

    Left/Right  switch page          Up/Down  backlight brightness
    Light       toggle backlight     Menu     back to mappings page
    OK          force refresh

Note: while the daemon runs it holds the LCD interface claimed; stop it
(systemctl --user stop spacepilot-lcd) before using spplcd.py manually.
"""

import os
import platform
import signal
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

PAGES = ["mappings", "clock", "system"]

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


def page_base(title):
    img = Image.new("RGB", (WIDTH, HEIGHT), (10, 10, 30))
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, WIDTH - 1, 30], fill=(30, 30, 70))
    d.text((8, 15), title, fill=(255, 255, 255), anchor="lm", font=load_font(20))
    d.text((WIDTH - 8, 15), datetime.now().strftime("%H:%M"),
           fill=(0, 255, 255), anchor="rm", font=load_font(20))
    return img, d


def render_mappings(config_path, mappings):
    img, d = page_base("SpacePilot Pro")
    entry_font, small_font = load_font(15), load_font(12)
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


def render_system():
    img, d = page_base(platform.node() or "System")
    font = load_font(16)
    load1, load5, load15 = os.getloadavg()
    mem_total = mem_avail = 0
    with open("/proc/meminfo") as fp:
        for line in fp:
            if line.startswith("MemTotal:"):
                mem_total = int(line.split()[1])
            elif line.startswith("MemAvailable:"):
                mem_avail = int(line.split()[1])
    with open("/proc/uptime") as fp:
        up = float(fp.read().split()[0])
    rows = [
        ("Load", f"{load1:.2f}  {load5:.2f}  {load15:.2f}"),
        ("Memory", f"{(mem_total - mem_avail) / 1048576:.1f} / "
                   f"{mem_total / 1048576:.1f} GiB"),
        ("Uptime", f"{int(up // 86400)}d {int(up % 86400 // 3600)}h "
                   f"{int(up % 3600 // 60)}m"),
    ]
    for n, (key, value) in enumerate(rows):
        y = 60 + n * 34
        d.text((12, y), key, fill=(0, 255, 0), font=font)
        d.text((110, y), value, fill=(255, 255, 255), font=font)
    return img


def main():
    running = True

    def stop(*_):
        nonlocal running
        running = False

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    lcd = None
    page = 0
    brightness = 90
    light_on = True
    pressed_mask = 0
    last_state = None  # (page, minute, config mtime)

    def render():
        if PAGES[page] == "clock":
            return render_clock()
        if PAGES[page] == "system":
            return render_system()
        config_path = find_config()
        mappings = parse_mappings(config_path) if config_path else []
        return render_mappings(config_path, mappings)

    while running:
        if lcd is None:
            try:
                lcd = SpacePilotLCD()
                lcd.set_brightness(brightness if light_on else 0)
                last_state = None
            except (IOError, usb.core.USBError) as err:
                print(f"LCD unavailable, retrying: {err}", file=sys.stderr)
                time.sleep(10)
                continue
        try:
            config_path = find_config()
            state = (page, datetime.now().strftime("%H:%M"),
                     os.path.getmtime(config_path) if config_path else None)
            if state != last_state:
                lcd.send_image(render())
                last_state = state

            # Poll the bezel keys; this also paces the loop (~0.5s).
            try:
                data = lcd.dev.read(KEY_ENDPOINT, 8, timeout=500)
            except usb.core.USBTimeoutError:
                continue
            for i in range(0, len(data) - 1, 2):
                mask = data[i] | (data[i + 1] << 8)
                new = mask & ~pressed_mask
                pressed_mask = mask
                if new & KEY_LEFT:
                    page = (page - 1) % len(PAGES)
                if new & KEY_RIGHT:
                    page = (page + 1) % len(PAGES)
                if new & KEY_MENU:
                    page = 0
                if new & KEY_OK:
                    last_state = None
                if new & KEY_UP:
                    brightness = min(100, brightness + 15)
                    light_on = True
                    lcd.set_brightness(brightness)
                if new & KEY_DOWN:
                    brightness = max(5, brightness - 15)
                    light_on = True
                    lcd.set_brightness(brightness)
                if new & KEY_LIGHT:
                    light_on = not light_on
                    lcd.set_brightness(brightness if light_on else 0)
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
