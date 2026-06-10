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
"""Applet daemon for the SpacePilot Pro LCD, in the spirit of 3DxWare's
LCD applets on Windows.

The pages shown, their order and every visual option are configured with the
companion settings app (lcd_settings.py) and stored in
~/.config/spacepilot-lcd/config.json; the daemon hot-reloads the file when it
changes. Available applets: button mappings, digital/analog/dual clocks,
calendar, system monitor, 6DOF input test. Desktop notifications can be
mirrored on the screen as overlays.

Bezel keys (they report on the LCD USB interface, endpoint 0x81 — invisible
to spacenavd; every press gives on-screen feedback):
  Left / Right  previous / next page      Up / Down  backlight brightness
  Light         backlight on/off          Menu       page menu
  OK            confirm / refresh         Back       close / first page
  Settings      help overlay

Note: while the daemon runs it holds the LCD interface claimed; stop it
(systemctl --user stop spacepilot-lcd) before using spplcd.py manually.
"""

import signal
import sys
import time
from datetime import datetime

import usb.core

import applets
import lcdconfig
from notifications import NotificationListener
from spnav_client import SpnavClient
from spplcd import SpacePilotLCD
from sysstats import SystemStats

KEY_ENDPOINT = 0x81
KEY_SETTINGS, KEY_BACK, KEY_MENU, KEY_OK = 0x01, 0x02, 0x04, 0x08
KEY_RIGHT, KEY_LEFT, KEY_DOWN, KEY_UP = 0x10, 0x20, 0x40, 0x80
KEY_LIGHT = 0x200

HELP_SECONDS = 6


def main():
    running = True

    def stop(*_):
        nonlocal running
        running = False

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    cfg = lcdconfig.load()
    cfg_mtime = lcdconfig.mtime()
    stats = SystemStats()
    spnav = None
    notif_listener = None
    notif = None            # (app, summary, body, expiry)

    lcd = None
    page = 0
    brightness = int(cfg["brightness"])
    light_on = True
    pressed_mask = 0
    menu_open = False
    menu_sel = 0
    help_until = 0.0
    osd = None              # (text, fraction or None, expiry)
    dirty = True
    last_render = 0.0
    last_minute = None

    def pages():
        return cfg["pages"] or [lcdconfig.applet_with_defaults(
            {"type": "clock"})]

    def need_spnav():
        return any(p["type"] == "input" for p in pages())

    def apply_config():
        nonlocal spnav, notif_listener, page, dirty, brightness
        if need_spnav() and spnav is None:
            spnav = SpnavClient()
        if cfg["notifications"]["enabled"] and notif_listener is None:
            notif_listener = NotificationListener()
        page = min(page, len(pages()) - 1)
        dirty = True

    apply_config()

    def set_osd(text, fraction=None):
        nonlocal osd, dirty
        osd = (text, fraction, time.time() + float(cfg["osd_seconds"]))
        dirty = True

    def handle_key(bit):
        nonlocal page, brightness, light_on, menu_open, menu_sel
        nonlocal help_until, dirty
        titles = [p["title"] or p["type"] for p in pages()]
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
            page = (page - 1) % len(titles)
            menu_open = False
            set_osd(titles[page])
        elif bit == KEY_RIGHT:
            page = (page + 1) % len(titles)
            menu_open = False
            set_osd(titles[page])
        elif bit == KEY_MENU:
            menu_open = not menu_open
            menu_sel = page
            help_until = 0
            dirty = True
        elif bit == KEY_UP and menu_open:
            menu_sel = (menu_sel - 1) % len(titles)
            dirty = True
        elif bit == KEY_DOWN and menu_open:
            menu_sel = (menu_sel + 1) % len(titles)
            dirty = True
        elif bit == KEY_OK:
            if menu_open:
                page, menu_open = menu_sel, False
                set_osd(titles[page])
            else:
                set_osd("Refreshed")
        elif bit == KEY_BACK:
            if menu_open or help_until:
                menu_open, help_until = False, 0
            else:
                page = 0
                set_osd(titles[0])
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

            # Hot-reload the configuration when the settings app saves it.
            if lcdconfig.mtime() != cfg_mtime:
                cfg = lcdconfig.load()
                cfg_mtime = lcdconfig.mtime()
                brightness = int(cfg["brightness"])
                if light_on:
                    lcd.set_brightness(brightness)
                apply_config()
                set_osd("Settings applied")

            minute = datetime.now().strftime("%H:%M")
            if minute != last_minute:
                last_minute = minute
                dirty = True

            current = pages()[page]
            if now - last_render >= applets.refresh_interval(current):
                dirty = True

            if notif_listener is not None:
                try:
                    app, summary, body = notif_listener.queue.get_nowait()
                    notif = (app, summary, body,
                             now + float(cfg["notifications"]["seconds"]))
                    dirty = True
                except Exception:
                    pass
            if notif and now >= notif[3]:
                notif = None
                dirty = True
            if osd and now >= osd[2]:
                osd = None
                dirty = True
            if help_until and now >= help_until:
                help_until = 0
                dirty = True

            if dirty:
                ctx = {"stats": stats,
                       "spnav": spnav.state if spnav else None}
                img = applets.RENDERERS[current["type"]](current, ctx)
                if menu_open:
                    titles = [p["title"] or p["type"] for p in pages()]
                    img = applets.draw_menu(img, titles, menu_sel)
                elif help_until:
                    img = applets.draw_help(img)
                if notif:
                    img = applets.draw_notification(img, *notif[:3])
                if osd:
                    img = applets.draw_osd(img, osd[0], osd[1])
                lcd.send_image(img)
                last_render = now
                dirty = False

            # Poll the bezel keys; the timeout paces the loop, faster when
            # the current page wants frequent refreshes.
            timeout = min(500, int(applets.refresh_interval(current) * 1000))
            try:
                data = lcd.dev.read(KEY_ENDPOINT, 8, timeout=max(50, timeout))
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

    if spnav:
        spnav.stop()
    if notif_listener:
        notif_listener.stop()
    if lcd is not None:
        lcd.close()


if __name__ == "__main__":
    main()
