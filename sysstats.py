#!/usr/bin/env python3
# This file is part of spacepilot-pro-lcd. License: GPL-3.0 (see LICENSE).
"""System metrics for the system-monitor applet (Linux: /proc + sysfs;
AMD GPUs via amdgpu sysfs, NVIDIA via nvidia-smi)."""

import glob
import os
import shutil
import subprocess
import time


class SystemStats:
    def __init__(self):
        self._prev_cpu = None
        self._prev_net = None
        self._gpu_dir = None
        for path in sorted(glob.glob(
                "/sys/class/drm/card*/device/gpu_busy_percent")):
            self._gpu_dir = os.path.dirname(path)
            break
        self._nvidia = self._gpu_dir is None and shutil.which("nvidia-smi")
        self._cpu_temp_file = self._find_temp(("k10temp", "zenpower",
                                               "coretemp"))
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

    def net_rate(self):
        """Total (rx, tx) bytes/second across non-loopback interfaces."""
        rx = tx = 0
        with open("/proc/net/dev") as fp:
            for line in fp.readlines()[2:]:
                name, data = line.split(":", 1)
                if name.strip() == "lo":
                    continue
                fields = data.split()
                rx += int(fields[0])
                tx += int(fields[8])
        now = time.time()
        if self._prev_net is None:
            self._prev_net = (now, rx, tx)
            return 0.0, 0.0
        dt = now - self._prev_net[0]
        rate = ((rx - self._prev_net[1]) / dt if dt > 0 else 0,
                (tx - self._prev_net[2]) / dt if dt > 0 else 0)
        self._prev_net = (now, rx, tx)
        return rate

    def gpu(self):
        """Return (busy %, vram used GiB, vram total GiB, temp C) or None."""
        if self._gpu_dir:
            busy = self._read_int(os.path.join(self._gpu_dir,
                                               "gpu_busy_percent"))
            used = self._read_int(os.path.join(self._gpu_dir,
                                               "mem_info_vram_used"))
            total = self._read_int(os.path.join(self._gpu_dir,
                                                "mem_info_vram_total"))
            temp = (self._read_int(self._gpu_temp_file)
                    if self._gpu_temp_file else None)
            return (busy or 0, (used or 0) / 2**30, (total or 0) / 2**30,
                    temp / 1000 if temp else None)
        if self._nvidia:
            try:
                out = subprocess.run(
                    ["nvidia-smi", "--query-gpu=utilization.gpu,memory.used,"
                     "memory.total,temperature.gpu",
                     "--format=csv,noheader,nounits"],
                    capture_output=True, text=True, timeout=2).stdout.split(",")
                return (float(out[0]), float(out[1]) / 1024,
                        float(out[2]) / 1024, float(out[3]))
            except Exception:
                return None
        return None

    def cpu_temp(self):
        temp = (self._read_int(self._cpu_temp_file)
                if self._cpu_temp_file else None)
        return temp / 1000 if temp else None
