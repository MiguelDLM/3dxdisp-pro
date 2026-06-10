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
"""Library and CLI to drive the LCD of the 3Dconnexion SpacePilot Pro on Linux.

The SpacePilot Pro (USB 046d:c629) embeds the same 320x240 color LCD as the
Logitech G19 keyboard and speaks the same protocol (reverse-engineered in
libg19, https://github.com/jgeboski/libg19): a single USB bulk transfer to
endpoint 0x02 carrying a 512-byte header followed by a 320*240*2-byte RGB565
little-endian framebuffer.

The LCD lives on USB interface 0 (vendor-specific, no kernel driver), fully
independent from interface 1 (HID, the 6DOF axes used by spacenavd), so it can
be driven without disturbing motion input.

The physical panel is a 240x320 portrait module mounted sideways with a
mirrored horizontal scan: landscape images must be flipped left-right and
rotated 90 degrees before being written to the framebuffer (determined
empirically; see README).

CLI usage:
    spplcd.py                   -> test pattern
    spplcd.py image.png         -> display an image (rescaled to 320x240)
    spplcd.py --text "hello"    -> display centered text
"""

import sys

import usb.core
import usb.util
from PIL import Image, ImageDraw, ImageFont

VENDOR_ID = 0x046D
PRODUCT_ID = 0xC629
WIDTH, HEIGHT = 320, 240
HEADER_SIZE = 512
LCD_INTERFACE = 0
LCD_ENDPOINT = 0x02

# Panel orientation relative to the landscape image (see module docstring).
TRANSFORMS = [Image.FLIP_LEFT_RIGHT, Image.ROTATE_90]


def _build_header():
    """G19 protocol header from libg19: 15 magic bytes + incrementing filler."""
    hdr = bytearray([0x10, 0x0F, 0x00, 0x58, 0x02, 0x00, 0x00, 0x00,
                     0x00, 0x00, 0x00, 0x3F, 0x01, 0xEF, 0x00])
    value = 0x0F
    while len(hdr) < HEADER_SIZE:
        hdr.append(value)
        value = (value + 1) & 0xFF
    return bytes(hdr)


HEADER = _build_header()


def image_to_framebuffer(img):
    """Convert a 320x240 landscape PIL image to the panel's RGB565 buffer."""
    if img.size != (WIDTH, HEIGHT):
        img = img.resize((WIDTH, HEIGHT))
    img = img.convert("RGB")
    for t in TRANSFORMS:
        img = img.transpose(t)
    fb = bytearray(WIDTH * HEIGHT * 2)
    px = img.load()
    w, h = img.size
    i = 0
    for y in range(h):
        for x in range(w):
            r, g, b = px[x, y]
            c = ((r >> 3) << 11) | ((g >> 2) << 5) | (b >> 3)
            fb[i] = c & 0xFF
            fb[i + 1] = c >> 8
            i += 2
    return fb


class SpacePilotLCD:
    """Handle to the SpacePilot Pro LCD. Use as a context manager."""

    def __init__(self):
        self.dev = usb.core.find(idVendor=VENDOR_ID, idProduct=PRODUCT_ID)
        if self.dev is None:
            raise IOError("SpacePilot Pro (046d:c629) not found")
        if self.dev.is_kernel_driver_active(LCD_INTERFACE):
            self.dev.detach_kernel_driver(LCD_INTERFACE)
        usb.util.claim_interface(self.dev, LCD_INTERFACE)

    def send_image(self, img):
        payload = HEADER + bytes(image_to_framebuffer(img))
        return self.dev.write(LCD_ENDPOINT, payload, timeout=5000)

    def close(self):
        try:
            usb.util.release_interface(self.dev, LCD_INTERFACE)
            usb.util.dispose_resources(self.dev)
        except usb.core.USBError:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def load_font(size):
    for path in ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
                 "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf"):
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default()


def test_image():
    img = Image.new("RGB", (WIDTH, HEIGHT), (0, 0, 0))
    d = ImageDraw.Draw(img)
    bars = [(255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 0),
            (255, 0, 255), (0, 255, 255), (255, 255, 255), (64, 64, 64)]
    for n, c in enumerate(bars):
        d.rectangle([n * 40, 0, n * 40 + 39, 60], fill=c)
    d.rectangle([0, 0, WIDTH - 1, HEIGHT - 1], outline=(255, 255, 255))
    d.text((WIDTH // 2, 120), "SpacePilot Pro", fill=(255, 255, 255),
           anchor="mm", font=load_font(28))
    d.text((WIDTH // 2, 160), "LCD OK - Linux", fill=(0, 255, 0),
           anchor="mm", font=load_font(22))
    return img


def main(argv):
    if len(argv) >= 3 and argv[1] == "--text":
        img = Image.new("RGB", (WIDTH, HEIGHT), (0, 0, 40))
        d = ImageDraw.Draw(img)
        d.text((WIDTH // 2, HEIGHT // 2), " ".join(argv[2:]),
               fill=(255, 255, 255), anchor="mm", font=load_font(24))
    elif len(argv) >= 2:
        img = Image.open(argv[1])
    else:
        img = test_image()

    with SpacePilotLCD() as lcd:
        written = lcd.send_image(img)
    print(f"OK: {written} bytes sent to the LCD")


if __name__ == "__main__":
    main(sys.argv)
