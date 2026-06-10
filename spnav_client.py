#!/usr/bin/env python3
# This file is part of spacepilot-pro-lcd. License: GPL-3.0 (see LICENSE).
"""Minimal spacenavd client used by the 6DOF input-test applet.

Connects to spacenavd's Unix socket and keeps the latest device state.
Wire format (spacenavd src/proto_unix.c): every event is 8 little-endian
int32 values. data[0] is the type: 0 = motion (data[1..6] = TX TY TZ RX RY
RZ, data[7] = period), 1 = button press, 2 = button release (data[1] =
button number).
"""

import socket
import struct
import threading
import time

SOCKET_PATH = "/var/run/spnav.sock"
EVENT = struct.Struct("<8i")

UEV_MOTION, UEV_PRESS, UEV_RELEASE = 0, 1, 2


class State:
    def __init__(self):
        self.axes = [0] * 6
        self.buttons = set()
        self.connected = False
        self.last_event = 0.0


class SpnavClient:
    """Background reader thread; read .state for the latest values."""

    def __init__(self):
        self.state = State()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _run(self):
        while not self._stop.is_set():
            try:
                sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                sock.settimeout(1.0)
                sock.connect(SOCKET_PATH)
            except OSError:
                self.state.connected = False
                time.sleep(3)
                continue
            self.state.connected = True
            buf = b""
            try:
                while not self._stop.is_set():
                    try:
                        chunk = sock.recv(4096)
                    except socket.timeout:
                        # No events for a moment: axes spring back to zero.
                        if time.time() - self.state.last_event > 0.3:
                            self.state.axes = [0] * 6
                        continue
                    if not chunk:
                        break
                    buf += chunk
                    while len(buf) >= EVENT.size:
                        values = EVENT.unpack(buf[:EVENT.size])
                        buf = buf[EVENT.size:]
                        self._handle(values)
            except OSError:
                pass
            finally:
                sock.close()
                self.state.connected = False
                self.state.axes = [0] * 6
                self.state.buttons.clear()

    def _handle(self, values):
        kind = values[0]
        if kind == UEV_MOTION:
            self.state.axes = list(values[1:7])
        elif kind == UEV_PRESS:
            self.state.buttons.add(values[1])
        elif kind == UEV_RELEASE:
            self.state.buttons.discard(values[1])
        self.state.last_event = time.time()
