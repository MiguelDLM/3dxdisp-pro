# This file is part of 3dxdisp-pro. License: GPL-3.0 (see LICENSE).
"""SpaceMouse navigation for 3D Slicer on Linux (via spacenavd).

Slicer has no 3D-mouse support on Linux. This scripted module connects to
spacenavd's Unix socket (the same one Blender uses through libspnav) and
drives whatever view is under the mouse cursor:

* Slice views (Red/Yellow/Green...): pan, zoom and scroll through slices.
* 3D views: orbit, pan and dolly the camera.

It starts automatically with Slicer; the module panel (Utilities >
SpaceMouse Navigation) only holds the settings and a live axis readout.

Wire format (spacenavd src/proto_unix.c): every event is 8 little-endian
int32. data[0] = 0 motion (data[1..6] = TX TY TZ RX RY RZ), 1 press,
2 release (data[1] = button number).
"""

import math
import socket
import struct
import time

import ctk
import qt
import slicer
import vtk
from slicer.ScriptedLoadableModule import (
    ScriptedLoadableModule,
    ScriptedLoadableModuleWidget,
)

SOCKET_PATH = "/var/run/spnav.sock"
EVENT = struct.Struct("<8i")
UEV_MOTION, UEV_PRESS, UEV_RELEASE = 0, 1, 2

AXES = ["TX", "TY", "TZ", "RX", "RY", "RZ"]
AXIS_FULL_SCALE = 350.0  # typical full deflection reported by spacenavd

# SpacePilot Pro button numbers (spacenavd / Blender GHOST numbering).
BTN_FIT, BTN_TOP, BTN_LEFT, BTN_RIGHT, BTN_FRONT = 1, 2, 3, 4, 5
BTN_BOTTOM, BTN_BACK, BTN_ROLL_CW, BTN_ROLL_CCW = 6, 7, 8, 9
BTN_ISO1, BTN_ISO2 = 10, 11

# action -> (label, default axis, default invert, default speed)
SLICE_ACTIONS = {
    "slice_pan_x": ("Pan horizontal", "TX", False, 1.0),
    "slice_pan_y": ("Pan vertical", "TZ", False, 1.0),
    "slice_zoom": ("Zoom", "TY", False, 1.0),
    "slice_scroll": ("Next / previous slice", "RY", False, 1.0),
}
VIEW3D_ACTIONS = {
    "3d_pan_x": ("Pan horizontal", "TX", False, 1.0),
    "3d_pan_y": ("Pan vertical", "TZ", False, 1.0),
    "3d_dolly": ("Zoom (dolly)", "TY", False, 1.0),
    "3d_tilt": ("Tilt (around screen X)", "RX", False, 1.0),
    "3d_spin": ("Spin (around screen Y)", "RY", False, 1.0),
    "3d_roll": ("Roll (around view axis)", "RZ", False, 1.0),
}
ALL_ACTIONS = {**SLICE_ACTIONS, **VIEW3D_ACTIONS}

# Rates at full deflection with speed 1.0.
PAN_FOV_PER_S = 1.0        # slice: one field of view per second
ZOOM_LOG_PER_S = 1.6       # e^1.6 ~ 5x per second
SLICES_PER_S = 30.0
ROT_DEG_PER_S = 150.0
PAN3D_DIST_PER_S = 0.8     # fraction of camera distance per second

SETTINGS_PREFIX = "SpaceMouseNav/"


def _setting(key, default):
    value = qt.QSettings().value(SETTINGS_PREFIX + key)
    if value is None:
        return default
    if isinstance(default, bool):
        return str(value).lower() in ("true", "1")
    if isinstance(default, float):
        try:
            return float(value)
        except (TypeError, ValueError):
            return default
    return str(value)


def _set_setting(key, value):
    qt.QSettings().setValue(SETTINGS_PREFIX + key, value)


class Config:
    def __init__(self):
        self.load()

    def load(self):
        self.enabled = _setting("enabled", True)
        self.buttons = _setting("buttons", True)
        self.deadzone = _setting("deadzone", 0.06)
        self.curve = _setting("curve", 1.7)
        self.global_speed = _setting("global_speed", 1.0)
        self.mapping = {}
        for key, (_, axis, invert, speed) in ALL_ACTIONS.items():
            self.mapping[key] = {
                "axis": _setting(key + "/axis", axis),
                "invert": _setting(key + "/invert", invert),
                "speed": _setting(key + "/speed", speed),
            }

    def save(self):
        _set_setting("enabled", self.enabled)
        _set_setting("buttons", self.buttons)
        _set_setting("deadzone", self.deadzone)
        _set_setting("curve", self.curve)
        _set_setting("global_speed", self.global_speed)
        for key, m in self.mapping.items():
            _set_setting(key + "/axis", m["axis"])
            _set_setting(key + "/invert", m["invert"])
            _set_setting(key + "/speed", m["speed"])

    def reset(self):
        qt.QSettings().remove(SETTINGS_PREFIX.rstrip("/"))
        self.load()


class SpacenavReader:
    """Non-blocking spacenavd socket, polled from the Qt main thread."""

    def __init__(self):
        self.sock = None
        self.buf = b""
        self.axes = [0] * 6
        self.last_motion = 0.0
        self.next_retry = 0.0

    @property
    def connected(self):
        return self.sock is not None

    def _connect(self):
        now = time.monotonic()
        if now < self.next_retry:
            return
        self.next_retry = now + 2.0
        try:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.connect(SOCKET_PATH)
            s.setblocking(False)
            self.sock = s
            self.buf = b""
        except OSError:
            self.sock = None

    def close(self):
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass
        self.sock = None
        self.axes = [0] * 6

    def poll(self):
        """Read everything pending. Returns a list of (button, pressed)."""
        buttons = []
        if self.sock is None:
            self._connect()
            if self.sock is None:
                return buttons
        while True:
            try:
                chunk = self.sock.recv(65536)
            except BlockingIOError:
                break
            except OSError:
                self.close()
                return buttons
            if not chunk:  # spacenavd went away
                self.close()
                return buttons
            self.buf += chunk
        n = len(self.buf) // EVENT.size
        for i in range(n):
            values = EVENT.unpack_from(self.buf, i * EVENT.size)
            kind = values[0]
            if kind == UEV_MOTION:
                self.axes = list(values[1:7])
                self.last_motion = time.monotonic()
            elif kind in (UEV_PRESS, UEV_RELEASE):
                buttons.append((values[1], kind == UEV_PRESS))
        self.buf = self.buf[n * EVENT.size:]
        # Safety net: if the device stops reporting while deflected (unplug,
        # spacenavd hiccup), do not keep the view drifting forever.
        if any(self.axes) and time.monotonic() - self.last_motion > 1.0:
            self.axes = [0] * 6
        return buttons


class NavigationController:
    """Applies spacenavd input to the Slicer view under the cursor."""

    def __init__(self):
        self.config = Config()
        self.reader = SpacenavReader()
        self.timer = qt.QTimer()
        self.timer.setInterval(16)
        self.timer.timeout.connect(self._tick)
        self.last_tick = time.monotonic()
        self.scroll_accum = 0.0
        self.pinned = None          # (kind, name) chosen with ISO2
        self.last_target = None
        self.listeners = []         # callables(axes, connected, target)

    # ---- lifecycle -------------------------------------------------------
    def start(self):
        self.last_tick = time.monotonic()
        self.timer.start()

    def stop(self):
        self.timer.stop()
        self.reader.close()

    # ---- target view -----------------------------------------------------
    def _views(self):
        """Visible views as (kind, name, widget, view)."""
        lm = slicer.app.layoutManager()
        if lm is None:
            return []
        out = []
        for name in lm.sliceViewNames():
            w = lm.sliceWidget(name)
            if w is not None and w.sliceView().isVisible():
                out.append(("slice", name, w, w.sliceView()))
        for i in range(lm.threeDViewCount):
            w = lm.threeDWidget(i)
            if w is not None and w.threeDView().isVisible():
                out.append(("3d", w.mrmlViewNode().GetName(), w, w.threeDView()))
        return out

    def _target(self):
        views = self._views()
        if self.pinned is not None:
            for v in views:
                if (v[0], v[1]) == self.pinned:
                    return v
            self.pinned = None
        pos = qt.QCursor.pos()
        for v in views:
            view = v[3]
            if view.rect.contains(view.mapFromGlobal(pos)):
                self.last_target = (v[0], v[1])
                return v
        if self.last_target is not None:
            for v in views:
                if (v[0], v[1]) == self.last_target:
                    return v
        return None

    # ---- input shaping ---------------------------------------------------
    def _shape(self, raw):
        """Normalize, apply dead zone and response curve -> [-1, 1]."""
        v = max(-1.0, min(1.0, raw / AXIS_FULL_SCALE))
        dz = self.config.deadzone
        a = abs(v)
        if a <= dz:
            return 0.0
        a = (a - dz) / (1.0 - dz)
        return math.copysign(a ** self.config.curve, v)

    def _value(self, shaped, action):
        m = self.config.mapping[action]
        if m["axis"] not in AXES:
            return 0.0
        v = shaped[AXES.index(m["axis"])]
        if m["invert"]:
            v = -v
        return v * m["speed"] * self.config.global_speed

    # ---- main loop -------------------------------------------------------
    def _tick(self):
        now = time.monotonic()
        dt = min(now - self.last_tick, 0.05)
        self.last_tick = now
        buttons = self.reader.poll()
        active = self.config.enabled and slicer.app.activeWindow() is not None
        target = self._target() if active else None
        for cb in list(self.listeners):
            try:
                cb(self.reader.axes, self.reader.connected, target)
            except Exception:
                pass
        if not active or target is None:
            self.scroll_accum = 0.0
            return
        if self.config.buttons:
            for number, pressed in buttons:
                if pressed:
                    try:
                        self._button(number, target)
                    except Exception as exc:  # never kill the timer
                        print("SpaceMouseNav button error:", exc)
        shaped = [self._shape(a) for a in self.reader.axes]
        if not any(shaped):
            self.scroll_accum = 0.0
            return
        try:
            if target[0] == "slice":
                self._move_slice(target, shaped, dt)
            else:
                self._move_3d(target, shaped, dt)
        except Exception as exc:
            print("SpaceMouseNav motion error:", exc)

    # ---- slice views -----------------------------------------------------
    def _move_slice(self, target, shaped, dt):
        widget = target[2]
        logic = widget.sliceLogic()
        node = logic.GetSliceNode()
        fov = node.GetFieldOfView()

        pan_x = self._value(shaped, "slice_pan_x")
        pan_y = self._value(shaped, "slice_pan_y")
        if pan_x or pan_y:
            # The view follows the hand: sliding right moves the view centre
            # (XYZOrigin) right, so the image slides left.
            scale = PAN_FOV_PER_S * dt * max(fov[0], fov[1])
            origin = list(node.GetXYZOrigin())
            origin[0] += pan_x * scale
            origin[1] += pan_y * scale
            logic.StartSliceNodeInteraction(slicer.vtkMRMLSliceNode.XYZOriginFlag)
            node.SetXYZOrigin(*origin)
            logic.EndSliceNodeInteraction()

        zoom = self._value(shaped, "slice_zoom")
        if zoom:
            # Push down (negative TY) zooms in.
            f = math.exp(zoom * ZOOM_LOG_PER_S * dt)
            logic.StartSliceNodeInteraction(slicer.vtkMRMLSliceNode.FieldOfViewFlag)
            node.SetFieldOfView(fov[0] * f, fov[1] * f, fov[2])
            logic.EndSliceNodeInteraction()

        scroll = self._value(shaped, "slice_scroll")
        if scroll:
            self.scroll_accum += scroll * SLICES_PER_S * dt
            steps = int(self.scroll_accum)
            if steps:
                self.scroll_accum -= steps
                self._step_slice(logic, steps)
        else:
            self.scroll_accum = 0.0

    @staticmethod
    def _step_slice(logic, steps):
        spacing = logic.GetLowestVolumeSliceSpacing()[2] or 1.0
        logic.StartSliceOffsetInteraction()
        logic.SetSliceOffset(logic.GetSliceOffset() + steps * spacing)
        logic.EndSliceOffsetInteraction()

    # ---- 3D views --------------------------------------------------------
    def _move_3d(self, target, shaped, dt):
        view = target[3]
        cam = view.cameraNode().GetCamera()
        pos = list(cam.GetPosition())
        focal = list(cam.GetFocalPoint())
        up = list(cam.GetViewUp())
        direction = [focal[i] - pos[i] for i in range(3)]
        dist = vtk.vtkMath.Norm(direction)
        vtk.vtkMath.Normalize(direction)
        right = [0.0, 0.0, 0.0]
        vtk.vtkMath.Cross(direction, up, right)
        vtk.vtkMath.Normalize(right)
        vtk.vtkMath.Cross(right, direction, up)

        # Object mode: the scene follows the hand, so the camera moves the
        # opposite way.
        tilt = self._value(shaped, "3d_tilt")
        spin = self._value(shaped, "3d_spin")
        roll = self._value(shaped, "3d_roll")
        if tilt or spin or roll:
            k = ROT_DEG_PER_S * dt
            t = vtk.vtkTransform()
            t.PostMultiply()
            t.Translate(-focal[0], -focal[1], -focal[2])
            if tilt:
                t.RotateWXYZ(-tilt * k, right)
            if spin:
                t.RotateWXYZ(-spin * k, up)
            if roll:
                t.RotateWXYZ(-roll * k, direction)
            t.Translate(focal)
            pos = list(t.TransformPoint(pos))
            up = list(t.TransformVector(up))

        pan_x = self._value(shaped, "3d_pan_x")
        pan_y = self._value(shaped, "3d_pan_y")
        if pan_x or pan_y:
            scale = PAN3D_DIST_PER_S * dt * (
                cam.GetParallelScale() * 2 if cam.GetParallelProjection() else dist)
            # Same convention as the slice views: the camera follows the hand.
            delta = [(right[i] * pan_x + up[i] * pan_y) * scale for i in range(3)]
            pos = [pos[i] + delta[i] for i in range(3)]
            focal = [focal[i] + delta[i] for i in range(3)]

        cam.SetPosition(pos)
        cam.SetFocalPoint(focal)
        cam.SetViewUp(up)
        cam.OrthogonalizeViewUp()

        dolly = self._value(shaped, "3d_dolly")
        if dolly:
            # Push down (negative TY) zooms in, as in the slice views.
            f = math.exp(-dolly * ZOOM_LOG_PER_S * dt)
            if cam.GetParallelProjection():
                cam.SetParallelScale(cam.GetParallelScale() / f)
            else:
                cam.Dolly(f)

        renderer = view.renderWindow().GetRenderers().GetFirstRenderer()
        if renderer is not None:
            renderer.ResetCameraClippingRange()
        view.scheduleRender()

    # ---- buttons ---------------------------------------------------------
    def _button(self, number, target):
        kind, name, widget, view = target
        if number == BTN_ISO2:
            self._cycle_pin()
            return
        if number == BTN_ISO1:
            self._toggle_maximize(widget)
            return
        if kind == "slice":
            logic = widget.sliceLogic()
            node = logic.GetSliceNode()
            if number == BTN_FIT:
                logic.FitSliceToAll()
                logic.SnapSliceOffsetToIJK()
            elif number == BTN_TOP:
                node.SetOrientation("Axial")
            elif number == BTN_FRONT:
                node.SetOrientation("Coronal")
            elif number in (BTN_RIGHT, BTN_LEFT):
                node.SetOrientation("Sagittal")
            elif number == BTN_ROLL_CW:
                self._step_slice(logic, 1)
            elif number == BTN_ROLL_CCW:
                self._step_slice(logic, -1)
        else:
            axes = {
                BTN_TOP: ctk.ctkAxesWidget.Superior,
                BTN_BOTTOM: ctk.ctkAxesWidget.Inferior,
                BTN_FRONT: ctk.ctkAxesWidget.Anterior,
                BTN_BACK: ctk.ctkAxesWidget.Posterior,
                BTN_RIGHT: ctk.ctkAxesWidget.Right,
                BTN_LEFT: ctk.ctkAxesWidget.Left,
            }
            if number == BTN_FIT:
                view.resetFocalPoint()
            elif number in axes:
                view.lookFromAxis(axes[number])
            elif number in (BTN_ROLL_CW, BTN_ROLL_CCW):
                cam = view.cameraNode().GetCamera()
                cam.Roll(-90 if number == BTN_ROLL_CW else 90)
                view.scheduleRender()

    def _cycle_pin(self):
        """ISO2: lock navigation to one view, cycling through all of them."""
        keys = [(v[0], v[1]) for v in self._views()]
        if not keys:
            return
        if self.pinned in keys:
            i = keys.index(self.pinned) + 1
            self.pinned = keys[i] if i < len(keys) else None
        else:
            self.pinned = keys[0]
        msg = ("SpaceMouse: following the mouse cursor" if self.pinned is None
               else "SpaceMouse: locked to view %s" % self.pinned[1])
        slicer.util.showStatusMessage(msg, 2500)

    @staticmethod
    def _toggle_maximize(widget):
        """ISO1: maximize the target view, or restore the layout."""
        lm = slicer.app.layoutManager()
        layout_node = lm.layoutLogic().GetLayoutNode()
        view_node = widget.mrmlAbstractViewNode()
        if layout_node.GetNumberOfMaximizedViewNodes() > 0:
            lm.removeAllMaximizedViewNodes()
        else:
            lm.addMaximizedViewNode(view_node)


_controller = None


def controller():
    global _controller
    if _controller is None:
        _controller = NavigationController()
    return _controller


def _start_controller():
    controller().start()


#
# Module
#
class SpaceMouseNav(ScriptedLoadableModule):
    def __init__(self, parent):
        ScriptedLoadableModule.__init__(self, parent)
        parent.title = "SpaceMouse Navigation"
        parent.categories = ["Utilities"]
        parent.contributors = ["3dxdisp-pro"]
        parent.helpText = (
            "Navigate slice and 3D views with a 3Dconnexion device through "
            "spacenavd. Works on the view under the mouse cursor; it runs in "
            "the background from startup, this panel only holds settings.")
        parent.acknowledgementText = "Part of 3dxdisp-pro (GPL-3.0)."
        if not slicer.app.commandOptions().noMainWindow:
            slicer.app.connect("startupCompleted()", _start_controller)


#
# Settings panel
#
class SpaceMouseNavWidget(ScriptedLoadableModuleWidget):
    def setup(self):
        ScriptedLoadableModuleWidget.setup(self)
        self.ctrl = controller()
        cfg = self.ctrl.config

        # -- status
        status = ctk.ctkCollapsibleButton()
        status.text = "Status"
        self.layout.addWidget(status)
        form = qt.QFormLayout(status)
        self.enabledBox = qt.QCheckBox("Navigate views with the 3D mouse")
        self.enabledBox.checked = cfg.enabled
        self.enabledBox.toggled.connect(self._changed)
        form.addRow(self.enabledBox)
        self.buttonsBox = qt.QCheckBox(
            "Use view keys (Fit, Top/Front/Right, Roll, ISO1, ISO2)")
        self.buttonsBox.checked = cfg.buttons
        self.buttonsBox.toggled.connect(self._changed)
        form.addRow(self.buttonsBox)
        self.connLabel = qt.QLabel()
        form.addRow("spacenavd:", self.connLabel)
        self.targetLabel = qt.QLabel()
        form.addRow("Target view:", self.targetLabel)
        self.axisBars = []
        for name in AXES:
            bar = qt.QProgressBar()
            bar.minimum, bar.maximum = -int(AXIS_FULL_SCALE), int(AXIS_FULL_SCALE)
            bar.textVisible = True
            bar.format = "%v"
            self.axisBars.append(bar)
            form.addRow(name + ":", bar)

        # -- response
        resp = ctk.ctkCollapsibleButton()
        resp.text = "Response"
        self.layout.addWidget(resp)
        form = qt.QFormLayout(resp)
        self.speed = self._slider(0.1, 4.0, 0.05, cfg.global_speed)
        form.addRow("Global speed:", self.speed)
        self.deadzone = self._slider(0.0, 0.4, 0.01, cfg.deadzone)
        form.addRow("Dead zone:", self.deadzone)
        self.curve = self._slider(1.0, 3.0, 0.1, cfg.curve)
        self.curve.toolTip = ("1 = linear. Higher values give finer control "
                              "near the centre and full speed at the edge.")
        form.addRow("Response curve:", self.curve)

        # -- mappings
        self.mapWidgets = {}
        for title, actions in (("Slice views", SLICE_ACTIONS),
                               ("3D views", VIEW3D_ACTIONS)):
            box = ctk.ctkCollapsibleButton()
            box.text = title + " mapping"
            self.layout.addWidget(box)
            grid = qt.QGridLayout(box)
            for col, head in enumerate(("Action", "Axis", "Invert", "Speed")):
                grid.addWidget(qt.QLabel("<b>%s</b>" % head), 0, col)
            for row, (key, (label, *_)) in enumerate(actions.items(), start=1):
                m = cfg.mapping[key]
                combo = qt.QComboBox()
                combo.addItems(AXES + ["(none)"])
                combo.currentText = m["axis"] if m["axis"] in AXES else "(none)"
                inv = qt.QCheckBox()
                inv.checked = m["invert"]
                spd = self._slider(0.1, 4.0, 0.05, m["speed"])
                combo.currentIndexChanged.connect(self._changed)
                inv.toggled.connect(self._changed)
                grid.addWidget(qt.QLabel(label), row, 0)
                grid.addWidget(combo, row, 1)
                grid.addWidget(inv, row, 2)
                grid.addWidget(spd, row, 3)
                self.mapWidgets[key] = (combo, inv, spd)

        reset = qt.QPushButton("Restore defaults")
        reset.clicked.connect(self._reset)
        self.layout.addWidget(reset)
        help_ = qt.QLabel(
            "<small>Navigation applies to the view under the mouse cursor "
            "while Slicer is the active window. <b>ISO1</b> maximizes / "
            "restores that view, <b>ISO2</b> locks navigation to one view "
            "(press again to cycle, then back to follow-the-cursor). "
            "<b>Fit</b> resets the view, <b>Roll</b> keys step one slice, "
            "<b>Top / Front / Right</b> set Axial / Coronal / Sagittal in a "
            "slice view or the camera direction in 3D. The <b>+ / -</b>, "
            "<b>Rotation</b>, <b>Pan Zoom</b> and <b>Dominant</b> keys keep "
            "working through spacenavd.</small>")
        help_.wordWrap = True
        self.layout.addWidget(help_)
        self.layout.addStretch(1)

        self.ctrl.listeners.append(self._live)

    def _slider(self, lo, hi, step, value):
        s = ctk.ctkSliderWidget()
        s.minimum, s.maximum, s.singleStep = lo, hi, step
        s.decimals = 2
        s.value = value
        s.valueChanged.connect(self._changed)
        return s

    def _changed(self, *_):
        cfg = self.ctrl.config
        cfg.enabled = self.enabledBox.checked
        cfg.buttons = self.buttonsBox.checked
        cfg.global_speed = self.speed.value
        cfg.deadzone = self.deadzone.value
        cfg.curve = self.curve.value
        for key, (combo, inv, spd) in self.mapWidgets.items():
            cfg.mapping[key] = {"axis": combo.currentText,
                                "invert": inv.checked, "speed": spd.value}
        cfg.save()

    def _reset(self):
        self.ctrl.config.reset()
        cfg = self.ctrl.config
        for w in [self.enabledBox, self.buttonsBox, self.speed, self.deadzone,
                  self.curve] + [x for t in self.mapWidgets.values() for x in t]:
            w.blockSignals(True)
        self.enabledBox.checked = cfg.enabled
        self.buttonsBox.checked = cfg.buttons
        self.speed.value = cfg.global_speed
        self.deadzone.value = cfg.deadzone
        self.curve.value = cfg.curve
        for key, (combo, inv, spd) in self.mapWidgets.items():
            m = cfg.mapping[key]
            combo.currentText = m["axis"]
            inv.checked = m["invert"]
            spd.value = m["speed"]
        for w in [self.enabledBox, self.buttonsBox, self.speed, self.deadzone,
                  self.curve] + [x for t in self.mapWidgets.values() for x in t]:
            w.blockSignals(False)

    def _live(self, axes, connected, target):
        if not self.parent.isVisible():
            return
        self.connLabel.text = "connected" if connected else \
            "not connected (is spacenavd running?)"
        if target is None:
            self.targetLabel.text = "-"
        else:
            lock = " (locked)" if self.ctrl.pinned else ""
            self.targetLabel.text = "%s view %s%s" % (target[0], target[1], lock)
        for bar, value in zip(self.axisBars, axes):
            bar.value = max(-int(AXIS_FULL_SCALE), min(int(AXIS_FULL_SCALE), value))

    def cleanup(self):
        if self._live in self.ctrl.listeners:
            self.ctrl.listeners.remove(self._live)
