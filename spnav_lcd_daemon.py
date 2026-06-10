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

Replicates the most useful part of what 3DxWare shows on Windows: the current
button assignments, read from the spacenavd configuration (~/.spnavrc or
/etc/spnavrc), plus a clock. The screen refreshes once a minute and whenever
the configuration file changes.

Recognized spnavrc keys (see spacenavd's example-spnavrc):
    bnactN = <built-in action>   e.g. bnact16 = sensitivity-up
    kbmapN = <X11 keysym>        e.g. kbmap0  = Escape
    bnmapN = M                   button remapping (shown as-is)

Run it in the foreground (it is a simple loop); use the provided systemd user
unit to keep it running in the background.
"""

import os
import signal
import sys
import time
from datetime import datetime

import usb.core
from PIL import Image, ImageDraw

from spplcd import WIDTH, HEIGHT, SpacePilotLCD, load_font

CONFIG_PATHS = [os.path.expanduser("~/.spnavrc"), "/etc/spnavrc"]
REFRESH_SECONDS = 60

ACTION_LABELS = {
    "sensitivity-up": "Sensitivity +",
    "sensitivity-down": "Sensitivity -",
    "sensitivity-reset": "Sensitivity reset",
    "disable-rotation": "Toggle rotation",
    "disable-translation": "Toggle translation",
    "dominant-axis": "Dominant axis",
    "none": "(none)",
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


def render(config_path, mappings):
    img = Image.new("RGB", (WIDTH, HEIGHT), (10, 10, 30))
    d = ImageDraw.Draw(img)
    title_font, entry_font, small_font = load_font(20), load_font(15), load_font(12)

    d.rectangle([0, 0, WIDTH - 1, 30], fill=(30, 30, 70))
    d.text((8, 15), "SpacePilot Pro", fill=(255, 255, 255),
           anchor="lm", font=title_font)
    d.text((WIDTH - 8, 15), datetime.now().strftime("%H:%M"),
           fill=(0, 255, 255), anchor="rm", font=title_font)

    if not mappings:
        d.text((WIDTH // 2, HEIGHT // 2),
               "No button mappings in spnavrc", fill=(255, 255, 0),
               anchor="mm", font=entry_font)
        d.text((WIDTH // 2, HEIGHT // 2 + 24),
               "(spacenavd defaults active)", fill=(160, 160, 160),
               anchor="mm", font=small_font)
    else:
        # Two columns of up to 9 entries each; anything beyond is summarized.
        per_col, top, line_h = 9, 40, 21
        for n, (button, label) in enumerate(mappings[:per_col * 2]):
            col, row = divmod(n, per_col)
            x, y = 8 + col * (WIDTH // 2), top + row * line_h
            d.text((x, y), f"B{button:02d}", fill=(0, 255, 0), font=entry_font)
            d.text((x + 38, y), label[:18], fill=(255, 255, 255), font=entry_font)
        if len(mappings) > per_col * 2:
            d.text((WIDTH - 8, HEIGHT - 14), f"+{len(mappings) - per_col * 2} more",
                   fill=(160, 160, 160), anchor="rm", font=small_font)

    source = os.path.basename(config_path) if config_path else "no spnavrc found"
    d.text((8, HEIGHT - 14), source, fill=(100, 100, 140),
           anchor="lm", font=small_font)
    return img


def main():
    running = True

    def stop(*_):
        nonlocal running
        running = False

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    last_mtime = None
    last_minute = None
    while running:
        config_path = find_config()
        mtime = os.path.getmtime(config_path) if config_path else None
        minute = datetime.now().strftime("%H:%M")

        if mtime != last_mtime or minute != last_minute:
            mappings = parse_mappings(config_path) if config_path else []
            try:
                with SpacePilotLCD() as lcd:
                    lcd.send_image(render(config_path, mappings))
                last_mtime, last_minute = mtime, minute
            except (IOError, usb.core.USBError) as err:
                # Device unplugged or busy: keep retrying quietly.
                print(f"LCD unavailable, retrying: {err}", file=sys.stderr)
                time.sleep(10)
                continue
        time.sleep(2)


if __name__ == "__main__":
    main()
