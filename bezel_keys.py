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
"""Diagnostic tool: capture the LCD bezel keys of the SpacePilot Pro.

The keys around the screen (the G19-heritage menu/navigation keys) do NOT go
through the HID interface, so spacenavd never sees them. They report on the
LCD interface (0) via interrupt endpoint 0x81 (2-byte packets), like the
Logitech G19 menu keys.

Run it, press the keys around the screen, and note the hex codes printed.

    python3 bezel_keys.py [seconds]   (default 30)
"""

import sys
import time

import usb.core
import usb.util

from spplcd import VENDOR_ID, PRODUCT_ID, LCD_INTERFACE

KEY_ENDPOINT = 0x81


def main():
    duration = float(sys.argv[1]) if len(sys.argv) > 1 else 30
    dev = usb.core.find(idVendor=VENDOR_ID, idProduct=PRODUCT_ID)
    if dev is None:
        sys.exit("SpacePilot Pro (046d:c629) not found")
    if dev.is_kernel_driver_active(LCD_INTERFACE):
        dev.detach_kernel_driver(LCD_INTERFACE)
    usb.util.claim_interface(dev, LCD_INTERFACE)

    print(f"Listening on EP 0x81 for {duration:.0f}s - press the keys around "
          "the screen...", flush=True)
    deadline = time.time() + duration
    try:
        while time.time() < deadline:
            try:
                data = dev.read(KEY_ENDPOINT, 8, timeout=1000)
            except usb.core.USBTimeoutError:
                continue
            stamp = time.strftime("%H:%M:%S")
            print(f"{stamp}  {' '.join(f'{b:02X}' for b in data)}", flush=True)
    finally:
        usb.util.release_interface(dev, LCD_INTERFACE)
        usb.util.dispose_resources(dev)
    print("Done.")


if __name__ == "__main__":
    main()
