#!/usr/bin/env python3
# This file is part of spacepilot-pro-lcd. License: GPL-3.0 (see LICENSE).
"""Desktop notification listener for the LCD daemon.

Watches the session bus for org.freedesktop.Notifications.Notify calls (the
same notifications GNOME/KDE display) by running `dbus-monitor` as a
subprocess and parsing its output — no Python DBus bindings required.
"""

import queue
import re
import subprocess
import threading

_STRING_RE = re.compile(r'^\s*string "(.*)"$')


class NotificationListener:
    """Background listener; pop notifications from .queue as (app, summary,
    body) tuples."""

    def __init__(self):
        self.queue = queue.Queue()
        self._proc = None
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        if self._proc:
            self._proc.terminate()

    def _run(self):
        try:
            self._proc = subprocess.Popen(
                ["dbus-monitor",
                 "interface='org.freedesktop.Notifications',member='Notify'"],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        except OSError:
            return
        strings = None
        for line in self._proc.stdout:
            if "member=Notify" in line:
                strings = []
                continue
            if strings is None:
                continue
            m = _STRING_RE.match(line)
            if m:
                strings.append(m.group(1))
                # Notify(app_name, replaces_id, icon, summary, body, ...):
                # the string args arrive as app, icon, summary, body.
                if len(strings) == 4:
                    app, _icon, summary, body = strings
                    self.queue.put((app, summary, body))
                    strings = None
