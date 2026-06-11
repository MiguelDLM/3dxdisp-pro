#!/usr/bin/env python3
# This file is part of spacepilot-pro-lcd. License: GPL-3.0 (see LICENSE).
"""Virtual keyboard for the profiles feature.

Injects keyboard shortcuts at kernel level via uinput (python-evdev), so it
works on Wayland and X11 alike. Requires write access to /dev/uinput — see
99-spacepilot-uinput.rules.

Key combos are written as 'ctrl+shift+z', 'tab', 'f12', 'g'... Tokens are
*physical* kernel key codes: letters, digits, F-keys, navigation and
modifiers are layout-independent in practice; for arithmetic symbols prefer
the numpad names (kp_plus, kp_minus...) which do not depend on the layout.
"""

import time

try:
    from evdev import UInput, ecodes as e
    HAVE_EVDEV = True
except ImportError:
    HAVE_EVDEV = False

MODIFIERS = {
    "ctrl": "KEY_LEFTCTRL", "control": "KEY_LEFTCTRL",
    "shift": "KEY_LEFTSHIFT",
    "alt": "KEY_LEFTALT",
    "altgr": "KEY_RIGHTALT",
    "super": "KEY_LEFTMETA", "meta": "KEY_LEFTMETA", "win": "KEY_LEFTMETA",
}

ALIASES = {
    "esc": "KEY_ESC", "escape": "KEY_ESC",
    "enter": "KEY_ENTER", "return": "KEY_ENTER",
    "space": "KEY_SPACE", "tab": "KEY_TAB",
    "backspace": "KEY_BACKSPACE", "delete": "KEY_DELETE", "del": "KEY_DELETE",
    "insert": "KEY_INSERT", "home": "KEY_HOME", "end": "KEY_END",
    "pageup": "KEY_PAGEUP", "pagedown": "KEY_PAGEDOWN",
    "up": "KEY_UP", "down": "KEY_DOWN", "left": "KEY_LEFT",
    "right": "KEY_RIGHT",
    "minus": "KEY_MINUS", "equal": "KEY_EQUAL",
    "comma": "KEY_COMMA", "period": "KEY_DOT", "dot": "KEY_DOT",
    "slash": "KEY_SLASH", "backslash": "KEY_BACKSLASH",
    "semicolon": "KEY_SEMICOLON", "apostrophe": "KEY_APOSTROPHE",
    "grave": "KEY_GRAVE", "bracketleft": "KEY_LEFTBRACE",
    "bracketright": "KEY_RIGHTBRACE",
    "kp_plus": "KEY_KPPLUS", "kp_minus": "KEY_KPMINUS",
    "kp_multiply": "KEY_KPASTERISK", "kp_divide": "KEY_KPSLASH",
    "kp_enter": "KEY_KPENTER", "kp_dot": "KEY_KPDOT",
    **{f"kp{n}": f"KEY_KP{n}" for n in range(10)},
    "printscreen": "KEY_SYSRQ", "menu": "KEY_COMPOSE",
}


def _token_code(token):
    token = token.strip().lower()
    if not HAVE_EVDEV:
        raise RuntimeError("python-evdev is not installed")
    name = None
    if token in MODIFIERS:
        name = MODIFIERS[token]
    elif token in ALIASES:
        name = ALIASES[token]
    elif len(token) == 1 and (token.isalpha() or token.isdigit()):
        name = f"KEY_{token.upper()}"
    elif token.startswith("f") and token[1:].isdigit() and \
            1 <= int(token[1:]) <= 24:
        name = f"KEY_{token.upper()}"
    code = getattr(e, name, None) if name else None
    if code is None:
        raise ValueError(f"unknown key token: {token!r}")
    return code


def parse_combo(combo):
    """'ctrl+shift+z' -> ordered list of key codes (modifiers first)."""
    tokens = [t for t in combo.split("+") if t.strip()]
    if not tokens:
        raise ValueError("empty key combo")
    mods = [t for t in tokens if t.strip().lower() in MODIFIERS]
    keys = [t for t in tokens if t.strip().lower() not in MODIFIERS]
    return [_token_code(t) for t in mods + keys]


def validate_combo(combo):
    """Return None if the combo is valid, else an error message."""
    try:
        parse_combo(combo)
        return None
    except (ValueError, RuntimeError) as err:
        return str(err)


class KeyInjector:
    """Owns the uinput virtual keyboard. Create once, inject many."""

    def __init__(self):
        if not HAVE_EVDEV:
            raise RuntimeError("python-evdev is not installed")
        keys = sorted({code for name in
                       list(MODIFIERS.values()) + list(ALIASES.values())
                       for code in [getattr(e, name)]} |
                      {getattr(e, f"KEY_{c}") for c in
                       "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"} |
                      {getattr(e, f"KEY_F{n}") for n in range(1, 25)})
        self._ui = UInput({e.EV_KEY: keys}, name="SpacePilot LCD Profiles")

    def press_combo(self, combo):
        codes = parse_combo(combo)
        for code in codes:
            self._ui.write(e.EV_KEY, code, 1)
            self._ui.syn()
            time.sleep(0.005)
        for code in reversed(codes):
            self._ui.write(e.EV_KEY, code, 0)
            self._ui.syn()
            time.sleep(0.005)

    def close(self):
        self._ui.close()
