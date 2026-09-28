#!/usr/bin/python3
"""A small, native Linux performance monitor inspired by Windows Task Manager."""

from __future__ import annotations

import csv
import glob
import json
import math
import os
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
from collections import deque
from datetime import timedelta

import gi
gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, Gio, GLib, Gtk
import psutil


APP_DIR = os.path.dirname(os.path.realpath(__file__))
CONFIG_FILE = os.path.join(GLib.get_user_config_dir(), "sistem-performansi", "settings.json")
AUTOSTART_DIR = os.path.join(GLib.get_user_config_dir(), "autostart")
AUTOSTART_FILE = os.path.join(AUTOSTART_DIR, "sistem-performansi.desktop")
APPLICATION_ID = "io.github.sistemperformansi.Monitor"
ACCENT = "#2675c7"
BLUE = "#2675c7"
TEAL = "#16806b"
ORANGE = "#a85c08"
PURPLE = "#7055bf"
RED = "#b73c4a"
MUTED = "#687587"
GRAPH_PALETTE = {"background": "#f4f6f9", "grid": "#dce2e9", "axis": "#6f7b89", "spark": "#edf1f5"}


def is_autostart_enabled():
    return os.path.isfile(AUTOSTART_FILE)


def set_autostart_enabled(enabled):
    if not enabled:
        try:
            os.remove(AUTOSTART_FILE)
        except FileNotFoundError:
            pass
        return False

    source = os.path.join(APP_DIR, "Sistem Performansı.desktop")
    with open(source, encoding="utf-8") as launcher:
        contents = launcher.read().rstrip() + "\n"
    setting = "X-GNOME-Autostart-enabled=true"
    lines = contents.splitlines()
    for index, line in enumerate(lines):
        if line.startswith("X-GNOME-Autostart-enabled="):
            lines[index] = setting
            break
    else:
        lines.append(setting)

    os.makedirs(AUTOSTART_DIR, exist_ok=True)
    fd, temporary_path = tempfile.mkstemp(prefix=".sistem-performansi-", suffix=".desktop", dir=AUTOSTART_DIR)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as launcher:
            launcher.write("\n".join(lines) + "\n")
        os.chmod(temporary_path, 0o755)
        os.replace(temporary_path, AUTOSTART_FILE)
    except Exception:
        try:
            os.remove(temporary_path)
        except OSError:
            pass
        raise
    return True


def hex_rgb(value):
    return tuple(int(value[i:i + 2], 16) / 255 for i in (1, 3, 5))


def set_graph_palette(is_dark):
    global ACCENT, BLUE, TEAL, ORANGE, PURPLE, RED, MUTED, GRAPH_PALETTE
    if is_dark:
        ACCENT, BLUE, TEAL, ORANGE, PURPLE, RED = (
            "#68adf5", "#68adf5", "#53c5a4", "#f0ad59", "#b39af5", "#ef7884")
        MUTED = "#a4afbd"
        GRAPH_PALETTE = {"background": "#202a36", "grid": "#374555", "axis": "#a1aebc", "spark": "#202a36"}
    else:
        ACCENT, BLUE, TEAL, ORANGE, PURPLE, RED = (
            "#236fbd", "#236fbd", "#14745f", "#a85a05", "#6b51b7", "#b23c49")
        MUTED = "#687587"
        GRAPH_PALETTE = {"background": "#f4f6f9", "grid": "#dce2e9", "axis": "#6f7b89", "spark": "#edf1f5"}


def command(args, timeout=1.8):
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=timeout,
                              check=False).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return ""


def read_text(path, default=""):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read().strip()
    except OSError:
        return default


def human_bytes(value, suffix="B"):
    value = max(0, float(value or 0))
    units = ("B", "KB", "MB", "GB", "TB", "PB")
    index = 0
    while value >= 1024 and index < len(units) - 1:
        value /= 1024
        index += 1
    return f"{value:.1f} {units[index]}" if index else f"{value:.0f} {units[index]}"


def gib(value):
    return f"{float(value or 0) / (1024 ** 3):.1f} GB"


def mbps(value):
    return f"{float(value or 0) / 1_000_000:.2f} MB/s"


def safe_float(value):
    try:
        value = str(value).strip().replace("%", "")
        if value.lower() in {"n/a", "[n/a]", "not supported", "-", ""}:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def cpu_model():
    info = read_text("/proc/cpuinfo")
    for line in info.splitlines():
        if line.lower().startswith(("model name", "hardware", "processor")) and ":" in line:
            value = line.split(":", 1)[1].strip()
            if value and not value.isdigit():
                return value
    return socket.gethostname()


def cpu_topology():
    packages, cores = set(), set()
    for topology in glob.glob("/sys/devices/system/cpu/cpu[0-9]*/topology"):
        cpu = re.search(r"cpu(\d+)$", os.path.dirname(topology))
        if not cpu:
            continue
        package = read_text(os.path.join(topology, "physical_package_id"), "0")
        core = read_text(os.path.join(topology, "core_id"), cpu.group(1))
        packages.add(package)
        cores.add((package, core))
    return len(packages) or 1, len(cores) or (psutil.cpu_count(logical=False) or 1)


def cpu_caches():
    found = {}
    for path in glob.glob("/sys/devices/system/cpu/cpu0/cache/index*/"):
        level = read_text(os.path.join(path, "level"))
        size = read_text(os.path.join(path, "size"))
        if level and size:
            found[f"L{level}"] = size
    return found


def disk_roots():
    roots = {}
    for path in glob.glob("/sys/block/*"):
        name = os.path.basename(path)
        if name.startswith(("loop", "ram", "zram")):
            continue
        roots[name] = os.path.realpath(path)
    return roots


def physical_parents(device, roots, seen=None):
    """Resolve a mounted partition or mapper to visible non-pseudo block devices."""
    if seen is None:
        seen = set()
    base = os.path.basename(device or "")
    class_path = f"/sys/class/block/{base}"
    if not base or not os.path.exists(class_path):
        return set()
    real = os.path.realpath(class_path)
    matches = [name for name, root in roots.items() if real == root or real.startswith(root + "/")]
    if matches:
        name = max(matches, key=len)
        if name in seen:
            return set()
        # Device mapper and MD nodes can sit above the actual drives. Follow their slaves.
        slaves = glob.glob(os.path.join(roots[name], "slaves", "*"))
        if slaves:
            result = set()
            for slave in slaves:
                result |= physical_parents(os.path.basename(slave), roots, seen | {name})
            return result
        return {name}
    return set()


class Collector:
    def __init__(self):
        self.prev_net = {}
        self.prev_disk = {}
        self.prev_time = time.monotonic()
        self.boot = psutil.boot_time()
        self.cores = psutil.cpu_count(logical=True) or 1
        self.packages, self.physical_cores = cpu_topology()
        self.model = cpu_model()
        self.caches = cpu_caches()
        self.roots = disk_roots()
        self.disk_meta = self._load_disk_meta()
        # Prime psutil's interval based CPU counters before the first visible sample.
        psutil.cpu_percent(interval=None, percpu=True)

    def _load_disk_meta(self):
        meta = {}
        for name, path in self.roots.items():
            model = read_text(os.path.join(path, "device", "model"), "")
            if not model:
                model = read_text(os.path.join(path, "device", "name"), "")
            if not model:
                model = name
            size_sectors = safe_float(read_text(os.path.join(path, "size"), "0")) or 0
            rotational = read_text(os.path.join(path, "queue", "rotational"), "")
            kind = "HDD" if rotational == "1" else "SSD"
            if name.startswith("nvme"):
                kind = "NVMe SSD"
            size = size_sectors * 512
            meta[name] = {"model": model.strip(), "kind": kind, "size": size}
        return meta

    def _cpu(self):
        usage = psutil.cpu_percent(interval=None, percpu=True)
        avg = sum(usage) / max(1, len(usage))
        freqs = psutil.cpu_freq(percpu=True) or []
        mhz = [f.current for f in freqs if f and f.current]
        if not mhz:
            mhz = [safe_float(read_text(p)) / 1000 for p in glob.glob("/sys/devices/system/cpu/cpu[0-9]*/cpufreq/scaling_cur_freq") if safe_float(read_text(p))]
        current = sum(mhz) / len(mhz) if mhz else 0
        max_freq = max((f.max for f in freqs if f and f.max), default=0)
        min_freq = min((f.min for f in freqs if f and f.min), default=0)
        temps = []
        try:
            for group in psutil.sensors_temperatures().values():
                for sensor in group:
                    if sensor.current and sensor.current < 120:
                        temps.append(sensor.current)
        except (AttributeError, OSError):
            pass
        process_count = len(psutil.pids())
        thread_count = 0
        for proc in psutil.process_iter(attrs=["num_threads"]):
            try:
                thread_count += int(proc.info.get("num_threads") or 0)
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                continue
        file_counts = read_text("/proc/sys/fs/file-nr").split()
        open_files = int(file_counts[0]) if file_counts else 0
        open_files_limit = int(file_counts[2]) if len(file_counts) > 2 else 0
        base_khz = safe_float(read_text("/sys/devices/system/cpu/cpu0/cpufreq/base_frequency"))
        if not base_khz:
            base_khz = safe_float(read_text("/sys/devices/system/cpu/cpu0/cpufreq/cpuinfo_max_freq"))
        flags = read_text("/proc/cpuinfo").lower()
        return {
            "usage": avg, "per_core": usage, "mhz": current, "max_mhz": max_freq,
            "base_mhz": base_khz / 1000 if base_khz else 0,
            "min_mhz": min_freq, "model": self.model, "logical": self.cores,
            "cores": self.physical_cores, "packages": self.packages,
            "virtualization": "Evet" if re.search(r"\b(vmx|svm)\b", flags) else "Hayır",
            "caches": self.caches, "processes": process_count, "threads": thread_count,
            "open_files": open_files, "open_files_limit": open_files_limit,
            "uptime": max(0, time.time() - self.boot), "temp": max(temps) if temps else None,
        }

    def _memory(self):
        mem = psutil.virtual_memory()
        swap = psutil.swap_memory()
        meminfo = {}
        for line in read_text("/proc/meminfo").splitlines():
            m = re.match(r"([^:]+):\s+(\d+)", line)
            if m:
                meminfo[m.group(1)] = int(m.group(2)) * 1024
        return {
            "total": mem.total, "used": mem.total - mem.available, "available": mem.available,
            "percent": (mem.total - mem.available) / mem.total * 100 if mem.total else 0,
            "swap_total": swap.total, "swap_used": swap.used, "swap_percent": swap.percent,
            "commit": meminfo.get("Committed_AS", 0), "commit_limit": meminfo.get("CommitLimit", 0),
            "cached": meminfo.get("Cached", mem.cached), "buffers": meminfo.get("Buffers", mem.buffers),
            "shared": meminfo.get("Shmem", mem.shared), "slab": meminfo.get("Slab", mem.slab),
            "reclaimable_slab": meminfo.get("SReclaimable", 0),
            "unreclaimable_slab": meminfo.get("SUnreclaim", 0),
            "active": mem.active, "inactive": mem.inactive,
        }

    def _disks(self, elapsed):
        counters = psutil.disk_io_counters(perdisk=True, nowrap=True) or {}
        partitions = psutil.disk_partitions(all=False)
        mounts = {name: [] for name in self.roots}
        for part in partitions:
            if part.mountpoint.startswith(("/proc", "/sys", "/dev", "/run/user")):
                continue
            parents = physical_parents(part.device, self.roots)
            for name in parents:
                try:
                    usage = psutil.disk_usage(part.mountpoint)
                except (PermissionError, OSError):
                    continue
                record = {"mount": part.mountpoint, "device": part.device, "fstype": part.fstype,
                          "total": usage.total, "used": usage.used, "free": usage.free,
                          "percent": usage.percent}
                # Multiple Btrfs subvolume mounts share one capacity pool; show that pool once.
                match = next((v for v in mounts[name] if v["device"] == record["device"] and v["total"] == record["total"] and v["used"] == record["used"]), None)
                if match:
                    match["mounts"].append(part.mountpoint)
                else:
                    record["mounts"] = [part.mountpoint]
                    mounts[name].append(record)

        result = {}
        now_stats = {}
        for name, meta in self.disk_meta.items():
            stat = counters.get(name)
            if not stat:
                continue
            now_stats[name] = (stat.read_bytes, stat.write_bytes, stat.busy_time,
                               stat.read_time, stat.write_time, stat.read_count, stat.write_count)
            previous = self.prev_disk.get(name)
            read_rate = write_rate = active = 0.0
            latency = None
            if previous and elapsed > 0:
                read_rate = max(0, stat.read_bytes - previous[0]) / elapsed
                write_rate = max(0, stat.write_bytes - previous[1]) / elapsed
                active = min(100, max(0, (stat.busy_time - previous[2]) / (elapsed * 1000) * 100))
                operations = max(0, stat.read_count - previous[5]) + max(0, stat.write_count - previous[6])
                if operations:
                    latency = (max(0, stat.read_time - previous[3]) + max(0, stat.write_time - previous[4])) / operations
            result[name] = {**meta, "name": name, "read_bps": read_rate, "write_bps": write_rate,
                            "active": active, "volumes": mounts.get(name, []),
                            "latency_ms": latency,
                            "read_bytes": stat.read_bytes, "write_bytes": stat.write_bytes}
        self.prev_disk = now_stats
        return result

    def _networks(self, elapsed):
        counters = psutil.net_io_counters(pernic=True, nowrap=True) or {}
        addrs = psutil.net_if_addrs()
        stats = psutil.net_if_stats()
        wifi = {}
        for line in read_text("/proc/net/wireless").splitlines()[2:]:
            if ":" in line:
                name, values = line.split(":", 1)
                try:
                    # /proc/net/wireless columns after status are link quality and signal level.
                    wifi[name.strip()] = float(values.split()[2].rstrip("."))
                except (IndexError, ValueError):
                    pass
        result, current = {}, {}
        for name, stat in counters.items():
            if name == "lo":
                continue
            current[name] = (stat.bytes_recv, stat.bytes_sent)
            before = self.prev_net.get(name)
            down = up = 0.0
            if before and elapsed > 0:
                down = max(0, stat.bytes_recv - before[0]) / elapsed
                up = max(0, stat.bytes_sent - before[1]) / elapsed
            addresses = []
            for addr in addrs.get(name, []):
                if addr.family in (socket.AF_INET, socket.AF_INET6):
                    addresses.append(("IPv4" if addr.family == socket.AF_INET else "IPv6", addr.address.split("%", 1)[0]))
            info = stats.get(name)
            wireless = os.path.isdir(f"/sys/class/net/{name}/wireless")
            result[name] = {
                "name": name, "up": bool(info and info.isup), "speed_mbps": info.speed if info else 0,
                "mtu": info.mtu if info else 0, "duplex": getattr(info.duplex, "name", str(info.duplex)).replace("NIC_DUPLEX_", "").lower() if info else "unknown",
                "down_bps": down, "up_bps": up, "received": stat.bytes_recv, "sent": stat.bytes_sent,
                "addresses": addresses, "wireless": wireless, "signal": wifi.get(name),
                "ssid": command(["iwgetid", "-r", name], timeout=0.5) if wireless else "",
            }
        self.prev_net = current
        return result

    def _gpus(self):
        gpus = []
        query = command(["nvidia-smi", "--query-gpu=uuid,name,utilization.gpu,utilization.memory,utilization.encoder,utilization.decoder,memory.used,memory.total,temperature.gpu,driver_version,clocks.gr,power.draw,power.limit", "--format=csv,noheader,nounits"], timeout=2.2)
        if query and "failed" not in query.lower() and "not found" not in query.lower():
            for fields in csv.reader(query.splitlines(), skipinitialspace=True):
                if len(fields) < 13:
                    continue
                ident, name = fields[0].strip(), fields[1].strip()
                used_mb, total_mb = safe_float(fields[6]), safe_float(fields[7])
                gpus.append({
                    "id": ident, "name": name, "usage": safe_float(fields[2]), "engine_memory": safe_float(fields[3]),
                    "encoder": safe_float(fields[4]), "decoder": safe_float(fields[5]),
                    "used": used_mb * 1024 ** 2 if used_mb is not None else None,
                    "total": total_mb * 1024 ** 2 if total_mb is not None else None,
                    "temperature": safe_float(fields[8]), "driver": fields[9].strip(),
                    "clock_mhz": safe_float(fields[10]), "power": safe_float(fields[11]), "power_limit": safe_float(fields[12]),
                    "source": "NVIDIA sürücüsü", "shared": None,
                })
        if gpus:
            return gpus
        for card in sorted(glob.glob("/sys/class/drm/card[0-9]*")):
            if re.search(r"-\w+-\d+$", card):
                continue
            dev = os.path.join(card, "device")
            vendor = read_text(os.path.join(dev, "vendor"))
            if not vendor or vendor == "0x0000":
                continue
            bus = os.path.basename(os.path.realpath(dev))
            name = command(["lspci", "-s", bus, "-mm"], timeout=0.7)
            name = name.split('"')[-2] if '"' in name else f"Grafik aygıtı ({vendor})"
            used = safe_float(read_text(os.path.join(dev, "mem_info_vram_used")))
            total = safe_float(read_text(os.path.join(dev, "mem_info_vram_total")))
            usage = safe_float(read_text(os.path.join(dev, "gpu_busy_percent")))
            gpus.append({"id": bus, "name": name, "usage": usage,
                         "used": used, "total": total, "engine_memory": None,
                         "encoder": None, "decoder": None,
                         "temperature": None, "driver": read_text(os.path.join(dev, "driver/module/version"), ""),
                         "clock_mhz": None, "power": None, "power_limit": None,
                         "source": "DRM/sysfs", "shared": None})
        return gpus

    def _bluetooth(self):
        adapter_text = command(["bluetoothctl", "show"], timeout=1.5)
        devices_text = command(["bluetoothctl", "devices", "Connected"], timeout=1.5)
        powered = None
        alias = ""
        for line in adapter_text.splitlines():
            if "Powered:" in line:
                powered = line.rsplit(" ", 1)[-1].lower() == "yes"
            elif "Name:" in line:
                alias = line.split("Name:", 1)[-1].strip()
        devices = []
        for line in devices_text.splitlines():
            match = re.match(r"Device\s+([0-9A-Fa-f:]{17})\s+(.+)", line.strip())
            if not match:
                continue
            mac, name = match.groups()
            info = command(["bluetoothctl", "info", mac], timeout=1.2)
            battery = None
            for info_line in info.splitlines():
                if "Battery Percentage:" in info_line:
                    m = re.search(r"\((\d+)\)", info_line)
                    battery = int(m.group(1)) if m else None
            devices.append({"name": name, "address": mac, "battery": battery})
        return {"powered": powered, "adapter": alias, "devices": devices,
                "available": bool(adapter_text)}

    def sample(self):
        now = time.monotonic()
        elapsed = max(0.05, now - self.prev_time)
        self.prev_time = now
        return {"timestamp": time.time(), "cpu": self._cpu(), "memory": self._memory(),
                "disks": self._disks(elapsed), "networks": self._networks(elapsed),
                "gpus": self._gpus(), "bluetooth": self._bluetooth()}


class Sampler:
    def __init__(self, callback):
        self.callback = callback
        self.interval = 1
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._loop, daemon=True, name="performance-sampler")

    def start(self):
        self.thread.start()

    def set_interval(self, seconds):
        self.interval = int(seconds)
        self.stop_event.set()
        self.stop_event.clear()

    def stop(self):
        self.stop_event.set()

    def _loop(self):
        collector = Collector()
        while not self.stop_event.is_set():
            started = time.monotonic()
            try:
                snapshot = collector.sample()
                GLib.idle_add(self.callback, snapshot)
            except Exception as exc:  # Keep a transient device disappearing from stopping the monitor.
                print(f"Ölçüm alınamadı: {exc}", flush=True)
            wait = max(0.05, self.interval - (time.monotonic() - started))
            self.stop_event.wait(wait)


class LineChart(Gtk.DrawingArea):
    def __init__(self, history_getter, fixed_max=None):
        super().__init__()
        self.history_getter = history_getter
        self.fixed_max = fixed_max
        self.set_size_request(200, 208)
        self.connect("draw", self._draw)

    def _draw(self, _widget, cr):
        alloc = self.get_allocation()
        width, height = alloc.width, alloc.height
        left, right, top, bottom = 42, 10, 10, 24
        graph_w, graph_h = max(10, width - left - right), max(10, height - top - bottom)
        cr.set_source_rgb(*hex_rgb(GRAPH_PALETTE["background"]))
        cr.rectangle(left, top, graph_w, graph_h); cr.fill()
        cr.set_source_rgb(*hex_rgb(GRAPH_PALETTE["grid"]))
        cr.set_line_width(.8)
        for i in range(5):
            y = top + graph_h * i / 4
            cr.move_to(left, y); cr.line_to(width - right, y); cr.stroke()
        for i in range(1, 7):
            x = left + graph_w * i / 7
            cr.move_to(x, top); cr.line_to(x, top + graph_h); cr.stroke()
        series = self.history_getter() or []
        values = [v for _, _, data in series for v in data if v is not None and math.isfinite(v)]
        max_val = self.fixed_max or max(1.0, (max(values) * 1.18 if values else 1.0))
        for i, label_value in enumerate((max_val, max_val * .75, max_val * .5, max_val * .25, 0)):
            cr.set_source_rgb(*hex_rgb(GRAPH_PALETTE["axis"]))
            cr.select_font_face("Sans", 0, 0); cr.set_font_size(9)
            cr.move_to(2, top + graph_h * i / 4 + 3)
            label = f"{label_value:.0f}%" if self.fixed_max == 100 else (f"{label_value:.1f}" if max_val < 10 else f"{label_value:.0f}")
            cr.show_text(label)
        for name, color, data in series:
            if len(data) < 2:
                continue
            rgb = hex_rgb(color)
            n = len(data)
            points = []
            for idx, val in enumerate(data):
                x = left + graph_w * idx / max(1, n - 1)
                y = top + graph_h * (1 - max(0, min(max_val, val or 0)) / max_val)
                points.append((x, y))
            cr.new_path(); cr.move_to(points[0][0], top + graph_h); cr.line_to(*points[0])
            for x, y in points[1:]: cr.line_to(x, y)
            cr.line_to(points[-1][0], top + graph_h); cr.close_path()
            cr.set_source_rgba(*rgb, .12); cr.fill_preserve()
            cr.set_source_rgb(*rgb); cr.set_line_width(2.2); cr.stroke()
        return False


class ResponsiveGrid(Gtk.Grid):
    """Reflow children only when the available width crosses a column breakpoint."""
    def __init__(self, max_columns, min_cell_width, column_spacing, row_spacing):
        super().__init__()
        self.max_columns = max_columns
        self.min_cell_width = min_cell_width
        self.set_column_spacing(column_spacing)
        self.set_row_spacing(row_spacing)
        self.set_column_homogeneous(True)
        self.set_hexpand(True)
        self.set_valign(Gtk.Align.START)
        self.cells = []
        self.columns = 1
        self.pending_columns = 1
        self.reflow_idle = 0
        self.connect("size-allocate", self._on_size_allocate)

    def add_cell(self, cell):
        index = len(self.cells)
        self.cells.append(cell)
        self.attach(cell, index % self.columns, index // self.columns, 1, 1)

    def do_get_preferred_width(self):
        # Gtk.Grid normally treats the current number of columns as a hard
        # minimum. Since this grid reflows, its minimum must be one cell wide.
        child_widths = [cell.get_preferred_width() for cell in self.cells if cell.get_visible()]
        if not child_widths:
            return 0, 0
        minimum = max(widths[0] for widths in child_widths)
        columns = min(self.max_columns, len(child_widths))
        natural = max(widths[1] for widths in child_widths) * columns
        natural += self.get_column_spacing() * max(0, columns - 1)
        return minimum, max(minimum, natural)

    def _on_size_allocate(self, _grid, allocation):
        spacing = self.get_column_spacing()
        columns = max(1, min(self.max_columns,
                             (allocation.width + spacing) // (self.min_cell_width + spacing)))
        if columns != self.columns:
            self.pending_columns = columns
            if not self.reflow_idle:
                self.reflow_idle = GLib.idle_add(self._reflow)

    def _reflow(self):
        self.reflow_idle = 0
        columns = self.pending_columns
        if columns == self.columns:
            return False
        self.columns = columns
        for child in self.get_children():
            self.remove(child)
        for index, cell in enumerate(self.cells):
            self.attach(cell, index % columns, index // columns, 1, 1)
        return False


class CenteredMaxWidth(Gtk.Bin):
    """Allocate a centered child without changing its size request during resize."""
    def __init__(self, max_width):
        super().__init__()
        self.max_width = max_width
        self.set_hexpand(True)
        self.set_valign(Gtk.Align.START)

    def do_get_preferred_width(self):
        child = self.get_child()
        if child and child.get_visible():
            minimum, natural = child.get_preferred_width()
            return minimum, min(natural, self.max_width)
        return 0, 0

    def do_get_preferred_height(self):
        child = self.get_child()
        return child.get_preferred_height() if child and child.get_visible() else (0, 0)

    def do_get_preferred_height_for_width(self, width):
        child = self.get_child()
        if not child or not child.get_visible():
            return 0, 0
        child_width = min(max(1, width), self.max_width)
        return child.get_preferred_height_for_width(child_width)

    def do_size_allocate(self, allocation):
        self.set_allocation(allocation)
        child = self.get_child()
        if child and child.get_visible():
            width = min(max(1, allocation.width), self.max_width)
            child_allocation = Gdk.Rectangle()
            child_allocation.x = allocation.x + (allocation.width - width) // 2
            child_allocation.y = allocation.y
            child_allocation.width = width
            child_allocation.height = max(1, allocation.height)
            child.size_allocate(child_allocation)


def install_css():
    css = """
    window, .main-bg { background-color: @theme_bg_color; color: @theme_fg_color; }
    headerbar {
        background-color: @theme_bg_color;
        color: @theme_fg_color;
        border: none;
        border-bottom: 1px solid alpha(@theme_fg_color, 0.13);
        box-shadow: none;
        min-height: 56px;
        padding: 0 8px;
    }
    headerbar .title { color: @theme_fg_color; font-size: 15px; font-weight: 700; }
    headerbar .subtitle { color: alpha(@theme_fg_color, 0.62); font-size: 11px; }
    .sidebar { background-color: shade(@theme_bg_color, 0.975); }
    .sidebar-paned > separator {
        min-width: 8px;
        background-color: alpha(@theme_fg_color, 0.055);
    }
    .sidebar-paned > separator:hover,
    .sidebar-paned > separator:active { background-color: alpha(@theme_selected_bg_color, 0.58); }
    .side-title { padding: 17px 15px 9px; }
    .side-heading { color: @theme_fg_color; font-size: 13px; font-weight: 700; }
    .live-indicator { color: #19966e; font-size: 10px; font-weight: 700; }
    .nav-button {
        background: transparent;
        border: 1px solid transparent;
        border-left: 3px solid transparent;
        border-radius: 5px;
        color: @theme_fg_color;
        padding: 7px 7px;
        margin: 1px 6px;
    }
    .nav-button:hover { background: alpha(@theme_fg_color, 0.055); }
    .nav-button.nav-selected {
        background: alpha(@theme_selected_bg_color, 0.13);
        border-left-color: @theme_selected_bg_color;
    }
    .nav-button.nav-selected .nav-name { color: @theme_selected_bg_color; }
    .nav-name { color: @theme_fg_color; font-size: 12px; font-weight: 600; }
    .nav-value { color: alpha(@theme_fg_color, 0.67); font-size: 10px; }
    .page-title { font-size: 24px; font-weight: 700; color: @theme_fg_color; }
    .page-description { font-size: 12px; color: alpha(@theme_fg_color, 0.64); }
    .page-icon { background-color: alpha(@theme_selected_bg_color, 0.13); border-radius: 8px; padding: 9px; }
    .section-title { font-size: 12px; font-weight: 700; color: @theme_fg_color; }
    .chart-title { font-size: 13px; font-weight: 700; color: @theme_fg_color; }
    .chart-subtitle { font-size: 11px; color: alpha(@theme_fg_color, 0.62); }
    .legend-item { font-size: 10px; }
    .card {
        background-color: @theme_base_color;
        border: 1px solid alpha(@theme_fg_color, 0.14);
        border-radius: 5px;
        box-shadow: 0 1px 2px alpha(@theme_fg_color, 0.045);
    }
    .graph-card { border-top: 2px solid alpha(@theme_selected_bg_color, 0.48); }
    .metric-block {
        background-color: alpha(@theme_fg_color, 0.035);
        border-bottom: 2px solid alpha(@theme_selected_bg_color, 0.48);
        border-radius: 4px;
        padding: 9px 11px;
    }
    .metric-value { font-size: 20px; font-weight: 700; color: @theme_text_color; }
    .metric-label { font-size: 11px; color: alpha(@theme_fg_color, 0.62); }
    .body-label { color: @theme_text_color; font-size: 12px; }
    .muted { color: alpha(@theme_fg_color, 0.64); font-size: 11px; }
    .accent { color: @theme_selected_bg_color; }
    .progress trough { background: alpha(@theme_fg_color, 0.12); border-radius: 4px; min-height: 6px; }
    .progress progress { background: @theme_selected_bg_color; border-radius: 4px; min-height: 6px; }
    separator { background: alpha(@theme_fg_color, 0.12); min-height: 1px; }

    .app-dark, .app-dark .main-bg { background-color: #171f29; color: #e6edf5; }
    .app-dark headerbar {
        background-color: #1d2733;
        color: #e6edf5;
        border-bottom: 1px solid #3a4757;
    }
    .app-dark headerbar .title, .app-dark .side-heading,
    .app-dark .page-title, .app-dark .section-title,
    .app-dark .chart-title { color: #edf3fa; }
    .app-dark headerbar .subtitle, .app-dark .page-description,
    .app-dark .chart-subtitle, .app-dark .metric-label,
    .app-dark .muted { color: #aab7c6; }
    .app-dark .sidebar { background-color: #202a36; }
    .app-dark .sidebar-paned > separator { background-color: #344252; }
    .app-dark .sidebar-paned > separator:hover,
    .app-dark .sidebar-paned > separator:active { background-color: #75b9ff; }
    .app-dark .nav-button { color: #d9e2ed; }
    .app-dark .nav-button:hover { background-color: #293746; }
    .app-dark .nav-button.nav-selected { background-color: #2b3f52; border-left-color: #75b9ff; }
    .app-dark .nav-button.nav-selected .nav-name { color: #8cc5ff; }
    .app-dark .nav-name { color: #e1e9f2; }
    .app-dark .nav-value { color: #a8b5c4; }
    .app-dark .page-icon { background-color: #2b3f52; }
    .app-dark .card { background-color: #222d39; border-color: #3b4959; box-shadow: 0 1px 2px alpha(#000000, 0.22); }
    .app-dark .graph-card { border-top-color: #75b9ff; }
    .app-dark .metric-block { background-color: #242f3b; border-bottom-color: #75b9ff; }
    .app-dark .metric-value, .app-dark .body-label { color: #edf3fa; }
    .app-dark .accent { color: #8cc5ff; }
    .app-dark headerbar combobox button {
        background-color: #273341;
        color: #e4ecf5;
        border-color: #465667;
    }
    .app-dark headerbar combobox button:hover { background-color: #334354; }
    .app-dark headerbar combobox arrow { color: #ccd7e3; }
    .app-dark popover, .app-dark menu { background-color: #222d39; color: #e6edf5; }
    .app-dark popover modelbutton, .app-dark menu menuitem { color: #e6edf5; }
    .app-dark popover modelbutton:hover, .app-dark menu menuitem:hover { background-color: #334354; }
    .app-dark .progress trough { background-color: #394757; }
    .app-dark .progress progress { background-color: #75b9ff; }
    .app-dark separator { background-color: #3a4757; }
    .app-dark scrollbar trough { background-color: #202a36; }
    .app-dark scrollbar slider { background-color: #536274; }
    """
    provider = Gtk.CssProvider()
    provider.load_from_data(css.encode("utf-8"))
    Gtk.StyleContext.add_provider_for_screen(Gdk.Screen.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
    return provider


class SystemAppearance:
    """Follow the desktop's xdg-desktop-portal color-scheme and GTK theme changes."""
    PORTAL = "org.freedesktop.portal.Desktop"
    PORTAL_PATH = "/org/freedesktop/portal/desktop"
    SETTINGS_IFACE = "org.freedesktop.portal.Settings"

    def __init__(self, app):
        self.app = app
        self.settings = Gtk.Settings.get_default()
        self.bus = None
        self.subscription = 0
        try:
            self.bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
            self.subscription = self.bus.signal_subscribe(
                self.PORTAL, self.SETTINGS_IFACE, "SettingChanged", self.PORTAL_PATH,
                None, Gio.DBusSignalFlags.NONE, self._on_setting_changed)
        except GLib.Error:
            pass
        if self.settings:
            self.settings.connect("notify::gtk-theme-name", self._on_gtk_theme_changed)
            self.settings.connect("notify::gtk-application-prefer-dark-theme", self._on_gtk_theme_changed)
        self.refresh()

    def _fallback(self):
        if self.settings and self.settings.get_property("gtk-application-prefer-dark-theme"):
            return True
        theme = self.settings.get_property("gtk-theme-name") if self.settings else ""
        return "dark" in str(theme).lower()

    def refresh(self):
        mode = None
        if self.bus is not None:
            try:
                reply = self.bus.call_sync(
                    self.PORTAL, self.PORTAL_PATH, self.SETTINGS_IFACE, "Read",
                    GLib.Variant("(ss)", ("org.freedesktop.appearance", "color-scheme")),
                    GLib.VariantType.new("(v)"), Gio.DBusCallFlags.NONE, 1200, None)
                value = reply.unpack()[0]
                if int(value) == 1: mode = True
                elif int(value) == 2: mode = False
            except (GLib.Error, TypeError, ValueError):
                pass
        self.app.set_appearance(self._fallback() if mode is None else mode)

    def _on_setting_changed(self, _bus, _sender, _path, _interface, _signal, parameters):
        try:
            namespace, key, value = parameters.unpack()
            if namespace == "org.freedesktop.appearance" and key == "color-scheme":
                if int(value) == 1: GLib.idle_add(self.app.set_appearance, True)
                elif int(value) == 2: GLib.idle_add(self.app.set_appearance, False)
                else: GLib.idle_add(self.refresh)
        except (TypeError, ValueError):
            pass

    def _on_gtk_theme_changed(self, *_args):
        GLib.idle_add(self.refresh)


class SystemMonitorApplication(Gtk.Application):
    def __init__(self):
        super().__init__(application_id=APPLICATION_ID, flags=Gio.ApplicationFlags.FLAGS_NONE)
        self.monitor = None

    def do_activate(self):
        if self.monitor is None:
            self.monitor = PerformanceApp(self)
        self.monitor.show_window()

    def do_shutdown(self):
        if self.monitor is not None:
            self.monitor.shutdown()
        Gtk.Application.do_shutdown(self)


class PerformanceApp:
    def __init__(self, application):
        self.application = application
        self._shutdown = False
        self.css_provider = install_css()
        self.data = None
        self.selected = "cpu"
        self.dark_mode = None
        self.interval_seconds = 1
        try:
            with open(CONFIG_FILE, encoding="utf-8") as settings:
                value = int(json.load(settings).get("interval", 1))
                if value in (1, 2, 3, 5, 10): self.interval_seconds = value
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            pass
        self.histories = {}
        self.nav = {}
        self.current_page = None
        self.suppress_delete = False
        self.appearance = SystemAppearance(self)
        self.window = Gtk.ApplicationWindow(application=application, title="Sistem Performansı")
        self.window.set_default_size(1200, 780)
        self.window.set_size_request(320, 480)
        self.window.connect("delete-event", self.on_delete)
        self._header()
        self._layout()
        self.set_appearance(self.dark_mode)
        self._start_tray()
        self.sampler = Sampler(self.on_sample)
        self.sampler.start()

    def set_appearance(self, is_dark):
        is_dark = bool(is_dark)
        changed = self.dark_mode != is_dark
        self.dark_mode = is_dark
        set_graph_palette(is_dark)
        window = getattr(self, "window", None)
        if window:
            context = window.get_style_context()
            if is_dark:
                context.add_class("app-dark")
            else:
                context.remove_class("app-dark")
        settings = Gtk.Settings.get_default()
        if settings and bool(settings.get_property("gtk-application-prefer-dark-theme")) != is_dark:
            settings.set_property("gtk-application-prefer-dark-theme", is_dark)
        for ident, item in getattr(self, "nav", {}).items():
            color = self._resource_color(ident)
            item["color"] = color
            item["spark"].color = color
            item["spark"].queue_draw()
        if changed and self.data:
            self._update_sidebar()
            self._build_page()

    def _resource_color(self, ident):
        if ident == "cpu": return BLUE
        if ident == "memory": return PURPLE
        if ident.startswith("disk:"): return TEAL
        if ident.startswith("net:"): return ORANGE
        if ident == "bluetooth": return MUTED
        return BLUE

    def _resource_icon_name(self, ident):
        if ident == "cpu": names = ("cpu-symbolic", "utilities-system-monitor-symbolic", "utilities-system-monitor")
        elif ident == "memory": names = ("media-floppy-symbolic", "drive-harddisk-symbolic", "drive-harddisk")
        elif ident.startswith("disk:"): names = ("drive-harddisk-symbolic", "drive-harddisk")
        elif ident.startswith("net:"): names = ("network-wired-symbolic", "network-wired")
        elif ident == "bluetooth": names = ("bluetooth-symbolic", "bluetooth")
        else: names = ("video-display-symbolic", "video-display")
        theme = Gtk.IconTheme.get_default()
        return next((name for name in names if theme.has_icon(name)), "utilities-system-monitor")

    def _header(self):
        header = Gtk.HeaderBar()
        header.set_show_close_button(True)
        title_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
        title = Gtk.Label(label="Sistem Performansı", xalign=0)
        title.get_style_context().add_class("title")
        title.set_ellipsize(3); title.set_max_width_chars(28)
        self.header_title = title
        subtitle = Gtk.Label(label="Donanım kaynaklarının canlı görünümü", xalign=0)
        subtitle.get_style_context().add_class("subtitle")
        subtitle.set_ellipsize(3); subtitle.set_max_width_chars(32)
        self.header_subtitle = subtitle
        title_box.pack_start(title, False, False, 0); title_box.pack_start(subtitle, False, False, 0)
        header.set_custom_title(title_box)
        interval_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.interval_box = interval_box
        label = Gtk.Label(label="Örnek aralığı")
        label.get_style_context().add_class("muted")
        self.interval_combo = Gtk.ComboBoxText()
        for sec in (1, 2, 3, 5, 10): self.interval_combo.append(str(sec), f"{sec} saniye")
        self.interval_combo.set_active_id(str(self.interval_seconds))
        self.interval_combo.connect("changed", self.on_interval_changed)
        interval_box.pack_start(label, False, False, 0); interval_box.pack_start(self.interval_combo, False, False, 0)
        header.pack_end(interval_box)
        self.window.set_titlebar(header)

    def _layout(self):
        self.root = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        self.root.set_wide_handle(True)
        self.root.get_style_context().add_class("main-bg")
        self.root.get_style_context().add_class("sidebar-paned")
        self.side_column = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.side_column.set_size_request(160, -1); self.side_column.get_style_context().add_class("sidebar")
        self.side_head = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.side_head.set_margin_start(15); self.side_head.set_margin_end(14)
        self.side_head.set_margin_top(16); self.side_head.set_margin_bottom(10)
        side_title = Gtk.Label(label="Kaynaklar", xalign=0)
        side_title.get_style_context().add_class("side-heading")
        live = Gtk.Label(label="●  CANLI", xalign=1)
        live.get_style_context().add_class("live-indicator")
        self.side_head.pack_start(side_title, True, True, 0); self.side_head.pack_end(live, False, False, 0)
        self.side_column.pack_start(self.side_head, False, False, 0)
        self.nav_scroll = Gtk.ScrolledWindow()
        self.nav_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.resource_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        self.nav_scroll.add(self.resource_box)
        self.side_column.pack_start(self.nav_scroll, True, True, 0)
        self.root.pack1(self.side_column, False, False)
        self.main_scroll = Gtk.ScrolledWindow()
        self.main_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.main_host = CenteredMaxWidth(1180)
        self.main_content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.main_content.set_margin_start(24); self.main_content.set_margin_end(24)
        self.main_content.set_margin_top(20); self.main_content.set_margin_bottom(24)
        self.main_content.set_hexpand(True)
        self.main_content.set_halign(Gtk.Align.FILL); self.main_content.set_valign(Gtk.Align.START)
        self.main_host.add(self.main_content)
        self.main_scroll.add(self.main_host)
        self.root.pack2(self.main_scroll, True, True)
        self.sidebar_width = 236
        self._updating_sidebar_position = False
        self.root.set_position(self.sidebar_width)
        self.window.add(self.root)
        self.root.connect("size-allocate", self._on_window_resize)
        self.root.connect("notify::position", self._on_sidebar_position_changed)
        self.compact_nav = False

    def _on_window_resize(self, _widget, allocation):
        width = allocation.width
        compact = self.compact_nav
        if compact and width > 580:
            compact = False
        elif not compact and width < 540:
            compact = True
        if compact != self.compact_nav:
            if compact:
                position = self.root.get_position()
                if position >= 160:
                    self.sidebar_width = position
            self.compact_nav = compact
            self._apply_navigation_density()
            self.header_title.set_text("Performans" if compact else "Sistem Performansı")
            self.header_subtitle.set_visible(not compact)
            self.interval_box.set_visible(not compact)
            self.side_head.set_visible(not compact)
            margin = 8 if compact else 24
            self.main_content.set_margin_start(margin)
            self.main_content.set_margin_end(margin)
            self._set_sidebar_compact(compact, width)

    def _set_sidebar_compact(self, compact, width):
        self.side_column.set_size_request(64 if compact else 160, -1)
        position = 64 if compact else self.sidebar_width
        if not compact:
            main_minimum = self.main_scroll.get_preferred_width()[0]
            position = min(position, max(160, width - main_minimum - 12))
            self.sidebar_width = position
        self._set_sidebar_position(position)

    def _set_sidebar_position(self, position):
        self._updating_sidebar_position = True
        try:
            self.root.set_position(position)
        finally:
            self._updating_sidebar_position = False

    def _on_sidebar_position_changed(self, paned, _property):
        if self._updating_sidebar_position:
            return
        if self.compact_nav:
            self._set_sidebar_position(64)
            return
        width = paned.get_allocation().width
        main_minimum = self.main_scroll.get_preferred_width()[0]
        maximum = max(160, min(480, width - main_minimum - 12))
        position = max(160, min(maximum, paned.get_position()))
        if position != paned.get_position():
            self._set_sidebar_position(position)
        self.sidebar_width = position

    def _apply_navigation_density(self):
        for item in self.nav.values():
            if self.compact_nav:
                item["icon"].show()
                item["spark"].hide()
                item["labels"].hide()
            else:
                item["icon"].hide()
                item["spark"].show()
                item["labels"].show_all()

    def _start_tray(self):
        self.sni = StatusNotifier(self)
        self.sni.start()

    def on_delete(self, *_):
        self.window.hide()
        return True

    def show_window(self):
        self.window.show_all()
        self.window.present()

    def on_interval_changed(self, combo):
        if hasattr(self, "sampler") and combo.get_active_id():
            self.interval_seconds = int(combo.get_active_id())
            self.sampler.set_interval(self.interval_seconds)
            try:
                os.makedirs(os.path.dirname(CONFIG_FILE), exist_ok=True)
                with open(CONFIG_FILE, "w", encoding="utf-8") as settings:
                    json.dump({"interval": self.interval_seconds}, settings)
            except OSError as exc:
                print(f"Güncelleme aralığı kaydedilemedi: {exc}", flush=True)

    def quit(self):
        self.shutdown()
        self.application.quit()

    def shutdown(self):
        if self._shutdown:
            return
        self._shutdown = True
        if hasattr(self, "sampler"):
            self.sampler.stop()
        if hasattr(self, "sni"):
            self.sni.stop()

    def toggle_autostart(self):
        enabled = not is_autostart_enabled()
        try:
            set_autostart_enabled(enabled)
        except OSError as exc:
            print(f"Sistemle birlikte başlatma ayarı kaydedilemedi: {exc}", flush=True)
            return
        self.sni.refresh_menu()

    def on_sample(self, snapshot):
        self.data = snapshot
        self._add_histories(snapshot)
        self._rebuild_sidebar_if_needed()
        self._update_sidebar()
        self._refresh_page()
        return False

    def _add_histories(self, data):
        def add(key, value):
            self.histories.setdefault(key, deque(maxlen=90)).append(float(value or 0))
        add("cpu", data["cpu"]["usage"])
        for index, value in enumerate(data["cpu"]["per_core"]): add(f"core:{index}", value)
        add("memory", data["memory"]["percent"])
        add("swap", data["memory"]["swap_percent"])
        for name, disk in data["disks"].items():
            add(f"disk-read:{name}", disk["read_bps"] / 1_000_000)
            add(f"disk-write:{name}", disk["write_bps"] / 1_000_000)
            add(f"disk-active:{name}", disk["active"])
        for name, net in data["networks"].items():
            add(f"net-down:{name}", net["down_bps"] / 1_000_000)
            add(f"net-up:{name}", net["up_bps"] / 1_000_000)
        for gpu in data["gpus"]:
            add(f"gpu:{gpu['id']}", gpu["usage"] or 0)
            add(f"gpu-memory-controller:{gpu['id']}", gpu["engine_memory"] or 0)
            add(f"gpu-encoder:{gpu['id']}", gpu["encoder"] or 0)
            add(f"gpu-decoder:{gpu['id']}", gpu["decoder"] or 0)
            percent = (gpu["used"] / gpu["total"] * 100) if gpu["used"] is not None and gpu["total"] else 0
            add(f"gpu-mem:{gpu['id']}", percent)

    def _resource_list(self):
        if not self.data: return []
        d = self.data
        resources = [
            ("cpu", "CPU", f"{d['cpu']['usage']:.0f}%  ·  {d['cpu']['mhz'] / 1000:.2f} GHz", BLUE),
            ("memory", "Bellek", f"{gib(d['memory']['used'])} / {gib(d['memory']['total'])}  ·  {d['memory']['percent']:.0f}%", PURPLE),
        ]
        for name, disk in d["disks"].items():
            resources.append((f"disk:{name}", f"Disk · {name}", f"{disk['active']:.0f}% etkin  ·  ↓ {mbps(disk['read_bps'])}", TEAL))
        for name, net in d["networks"].items():
            resources.append((f"net:{name}", name, f"↓ {mbps(net['down_bps'])}   ↑ {mbps(net['up_bps'])}", ORANGE))
        bt = d["bluetooth"]
        resources.append(("bluetooth", "Bluetooth", f"{len(bt['devices'])} bağlı cihaz" if bt["devices"] else ("Kapalı" if bt["powered"] is False else "Bağlı değil"), MUTED))
        for i, gpu in enumerate(d["gpus"]):
            usage = f"{gpu['usage']:.0f}%" if gpu["usage"] is not None else "Ölçüm yok"
            resources.append((f"gpu:{gpu['id']}", f"GPU {i} · {gpu['name']}", usage, BLUE))
        return resources

    def _rebuild_sidebar_if_needed(self):
        resources = self._resource_list()
        signature = tuple((r[0], r[1]) for r in resources)
        if getattr(self, "nav_signature", None) == signature:
            return
        self.nav_signature = signature
        for child in self.resource_box.get_children(): self.resource_box.remove(child)
        self.nav = {}
        for ident, name, value, color in resources:
            button = Gtk.Button(); button.set_relief(Gtk.ReliefStyle.NONE); button.get_style_context().add_class("nav-button")
            button.set_tooltip_text(name)
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
            icon = Gtk.Image.new_from_icon_name(self._resource_icon_name(ident), Gtk.IconSize.LARGE_TOOLBAR)
            icon.set_pixel_size(20)
            row.pack_start(icon, False, False, 0)
            spark = MiniChart(color); row.pack_start(spark, False, False, 0)
            labels = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            name_label = Gtk.Label(label=name, xalign=0); name_label.set_ellipsize(3); name_label.set_max_width_chars(22); name_label.get_style_context().add_class("nav-name")
            value_label = Gtk.Label(label=value, xalign=0); value_label.set_ellipsize(3); value_label.set_max_width_chars(24)
            value_label.get_style_context().add_class("nav-value")
            labels.pack_start(name_label, False, False, 0); labels.pack_start(value_label, False, False, 0)
            row.pack_start(labels, True, True, 0); button.add(row)
            button.connect("clicked", self._select, ident)
            self.resource_box.pack_start(button, False, False, 0)
            self.nav[ident] = {"button": button, "value": value_label, "spark": spark,
                               "labels": labels, "icon": icon, "color": color}
        self.resource_box.show_all()
        self._apply_navigation_density()
        if self.selected not in self.nav:
            self.selected = "cpu"
        self._select(None, self.selected)

    def _update_sidebar(self):
        if not self.data: return
        for ident, name, value, _color in self._resource_list():
            item = self.nav.get(ident)
            if item:
                item["value"].set_text(value)
                history_key = ident
                if ident == "cpu": history_key = "cpu"
                elif ident == "memory": history_key = "memory"
                elif ident.startswith("disk:"): history_key = f"disk-active:{ident.split(':',1)[1]}"
                elif ident.startswith("net:"):
                    iface = ident.split(":", 1)[1]
                    history_key = f"net-down:{iface}"
                elif ident.startswith("gpu:"): history_key = ident
                item["spark"].values = list(self.histories.get(history_key, []))
                item["spark"].queue_draw()

    def _select(self, _button, ident):
        if ident not in self.nav: return
        self.selected = ident
        for key, item in self.nav.items():
            ctx = item["button"].get_style_context()
            ctx.add_class("nav-selected") if key == ident else ctx.remove_class("nav-selected")
        self._build_page()

    def _build_page(self):
        if not self.data: return
        for child in self.main_content.get_children(): self.main_content.remove(child)
        self.metric_value_refs = {}
        self.metric_hint_refs = {}
        self.info_value_refs = {}
        self.chart_widgets = []
        self.cpu_core_refs = []
        self.disk_volume_refs = []
        self.bluetooth_battery_refs = {}
        ident = self.selected
        if ident == "cpu": page = self._cpu_page()
        elif ident == "memory": page = self._memory_page()
        elif ident.startswith("disk:"): page = self._disk_page(ident.split(":", 1)[1])
        elif ident.startswith("net:"): page = self._network_page(ident.split(":", 1)[1])
        elif ident == "bluetooth": page = self._bluetooth_page()
        elif ident.startswith("gpu:"): page = self._gpu_page(ident.split(":", 1)[1])
        else: page = Gtk.Label(label="Kaynak bulunamadı")
        self.current_page = page
        self.page_structure_signature = self._page_structure_signature()
        self.main_content.pack_start(page, False, True, 0)
        self.main_content.show_all()

    def _refresh_page(self):
        if not self.data or not self.current_page: return
        if self._page_structure_signature() != self.page_structure_signature:
            self._build_page()
            return
        values, hints, info = self._live_text()
        for title, label in self.metric_value_refs.items():
            if title in values: label.set_text(values[title])
        for title, label in self.metric_hint_refs.items():
            if title in hints: label.set_text(hints[title])
        for key, label in self.info_value_refs.items():
            if key in info: label.set_text(info[key])
        if self.selected == "cpu":
            for index, (label, bar) in enumerate(self.cpu_core_refs):
                if index < len(self.data["cpu"]["per_core"]):
                    usage = self.data["cpu"]["per_core"][index]
                    label.set_text(f"CPU {index}  ·  {usage:.0f}%")
                    bar.set_fraction(max(0, min(1, usage / 100)))
        if self.selected.startswith("disk:"):
            name = self.selected.split(":", 1)[1]
            disk = self.data["disks"].get(name, {})
            volume_map = {(v["device"], tuple(v["mounts"])): v for v in disk.get("volumes", [])}
            for key, percent, bar, detail in self.disk_volume_refs:
                volume = volume_map.get(key)
                if volume:
                    percent.set_text(f"{volume['percent']:.1f}% dolu")
                    bar.set_fraction(max(0, min(1, volume["percent"] / 100)))
                    detail.set_text(f"{gib(volume['used'])} dolu   ·   {gib(volume['free'])} boş   ·   {gib(volume['total'])} toplam")
        if self.selected == "bluetooth":
            by_address = {d["address"]: d for d in self.data["bluetooth"]["devices"]}
            for address, label in self.bluetooth_battery_refs.items():
                device = by_address.get(address)
                if device and device["battery"] is not None:
                    label.set_text(f"Pil {device['battery']}%")
        for chart in self.chart_widgets: chart.queue_draw()

    def _page_structure_signature(self):
        if not self.data: return None
        if self.selected.startswith("disk:"):
            disk = self.data["disks"].get(self.selected.split(":", 1)[1], {})
            return tuple((v["device"], v["fstype"], tuple(v["mounts"])) for v in disk.get("volumes", []))
        if self.selected.startswith("net:"):
            net = self.data["networks"].get(self.selected.split(":", 1)[1], {})
            return (tuple(net.get("addresses", [])), net.get("wireless"), net.get("signal") is not None,
                    bool(net.get("ssid")))
        if self.selected == "bluetooth":
            bt = self.data["bluetooth"]
            return (tuple((d["address"], d["name"], d["battery"] is not None) for d in bt["devices"]), bt["available"])
        if self.selected == "cpu":
            c = self.data["cpu"]
            return (len(c["per_core"]), tuple(c["caches"].items()), bool(c["max_mhz"]), bool(c["base_mhz"]), c["temp"] is not None)
        if self.selected == "memory": return True
        if self.selected.startswith("gpu:"):
            gpu = next((g for g in self.data["gpus"] if g["id"] == self.selected.split(":", 1)[1]), {})
            return (gpu.get("used") is not None, gpu.get("total") is not None, gpu.get("engine_memory") is not None,
                    gpu.get("encoder") is not None, gpu.get("decoder") is not None,
                    gpu.get("temperature") is not None, gpu.get("clock_mhz") is not None,
                    gpu.get("power") is not None, gpu.get("power_limit") is not None)
        return None

    def _live_text(self):
        d = self.data
        values, hints, info = {}, {}, {}
        if self.selected == "cpu":
            c = d["cpu"]
            values = {"Kullanım": f"{c['usage']:.1f}%", "Anlık hız": f"{c['mhz']/1000:.2f} GHz" if c["mhz"] else "—",
                      "İşlem": str(c["processes"]), "İş parçacığı": str(c["threads"]),
                      "Açık tanıtıcı": f"{c['open_files']:,}", "Çekirdek": str(c["cores"]),
                      "Mantıksal işlemci": str(c["logical"]), "Soket": str(c["packages"]),
                      "Sanallaştırma": c["virtualization"], "Çalışma süresi": str(timedelta(seconds=int(c["uptime"]))) }
            hints = {"Açık tanıtıcı": f"Linux dosya tablosu · sistem sınırı {c['open_files_limit']:,}"}
            if c["max_mhz"]: values["Raporlanan üst hız"] = f"{c['max_mhz']/1000:.2f} GHz"
            if c["base_mhz"]: values["Temel hız"] = f"{c['base_mhz']/1000:.2f} GHz"
            if c["temp"] is not None: values["En yüksek CPU sıcaklığı"] = f"{c['temp']:.0f} °C"
        elif self.selected == "memory":
            m = d["memory"]
            values = {"Kullanılan RAM": f"{gib(m['used'])}  ·  {m['percent']:.1f}%", "Kullanılabilir": gib(m["available"]),
                      "Swap kullanımı": f"{gib(m['swap_used'])}  ·  {m['swap_percent']:.1f}%", "Commit": gib(m["commit"]),
                      "Önbellek": gib(m["cached"]), "Bellek baskısı": f"{m['percent']:.0f}%"}
            hints = {"Kullanılan RAM": f"Toplam {gib(m['total'])}", "Kullanılabilir": "Linux bellek baskısı olmadan kullanılabilir",
                     "Swap kullanımı": f"Toplam {gib(m['swap_total'])}", "Commit": f"Sınır {gib(m['commit_limit'])}"}
            info = {"Etkin bellek": gib(m["active"]), "Etkin olmayan bellek": gib(m["inactive"]),
                    "Çekirdek arabellekleri": gib(m["buffers"]), "Paylaşılan bellek": gib(m["shared"]),
                    "Çekirdek slab alanı": gib(m["slab"]), "Geri alınabilir slab": gib(m["reclaimable_slab"]),
                    "Geri alınamayan slab": gib(m["unreclaimable_slab"])}
        elif self.selected.startswith("disk:"):
            name = self.selected.split(":", 1)[1]; disk = d["disks"].get(name, {})
            values = {"Etkin süre": f"{disk.get('active',0):.1f}%", "Okuma hızı": mbps(disk.get("read_bps",0)),
                      "Yazma hızı": mbps(disk.get("write_bps",0)), "Okunan toplam": human_bytes(disk.get("read_bytes",0)),
                      "Yazılan toplam": human_bytes(disk.get("write_bytes",0)),
                      "Ortalama yanıt": f"{disk['latency_ms']:.1f} ms" if disk.get("latency_ms") is not None else "—",
                      "Aygıt kapasitesi": gib(disk.get("size",0)),
                      "Sistem diski": "Evet" if any("/" in v["mounts"] for v in disk.get("volumes", [])) else "Hayır"}
        elif self.selected.startswith("net:"):
            name = self.selected.split(":", 1)[1]; net = d["networks"].get(name, {})
            values = {"İndirme": mbps(net.get("down_bps",0)), "Yükleme": mbps(net.get("up_bps",0)),
                      "Bağlantı hızı": f"{net['speed_mbps']} Mbit/s" if net.get("speed_mbps") else "Bilinmiyor",
                      "Durum": "Bağlı" if net.get("up") else "Bağlı değil", "Alınan toplam": human_bytes(net.get("received",0)),
                      "Gönderilen toplam": human_bytes(net.get("sent",0))}
            if net.get("wireless") and net.get("signal") is not None: values["Wi-Fi sinyali"] = f"{net['signal']:.0f} dBm"
            if net.get("wireless") and net.get("ssid"): values["Kablosuz ağ"] = net["ssid"]
        elif self.selected == "bluetooth":
            bt = d["bluetooth"]
            values = {"Radyo durumu": "Açık" if bt["powered"] else "Kapalı" if bt["powered"] is False else "Durum alınamadı",
                      "Bağlı cihaz": str(len(bt["devices"])),
                      "Bluetooth servisi": "Çalışıyor" if bt["available"] else "Erişilemiyor"}
            hints = {"Radyo durumu": bt["adapter"] or "Bluetooth adaptörü", "Bağlı cihaz": "Şu an bağlı aygıtlar",
                     "Bluetooth servisi": "Sistem BlueZ servisi"}
        elif self.selected.startswith("gpu:"):
            gpu = next((g for g in d["gpus"] if g["id"] == self.selected.split(":", 1)[1]), {})
            used, total = gpu.get("used"), gpu.get("total")
            mem_percent = used / total * 100 if used is not None and total else None
            values = {"GPU kullanımı": f"{gpu['usage']:.1f}%" if gpu.get("usage") is not None else "Veri sunulmuyor",
                      "VRAM kullanımı": gib(used) if used is not None else "—",
                      "VRAM doluluğu": f"{mem_percent:.1f}%" if mem_percent is not None else "—",
                      "GPU sıcaklığı": f"{gpu['temperature']:.0f} °C" if gpu.get("temperature") is not None else "—",
                      "Grafik saati": f"{gpu['clock_mhz']:.0f} MHz" if gpu.get("clock_mhz") is not None else "—",
                      "Paylaşılan GPU belleği": "Sürücü verisi yok"}
            hints = {"GPU kullanımı": gpu.get("source", ""), "VRAM kullanımı": f"{gib(used)} / {gib(total)}" if used is not None and total else "VRAM verisi yok",
                     "VRAM doluluğu": f"Toplam {gib(total)}" if total else "Toplam kapasite sürücüden alınamadı",
                     "GPU sıcaklığı": "Aygıt sıcaklık sensörü", "Grafik saati": "Anlık grafik frekansı",
                     "Paylaşılan GPU belleği": "Bu Linux aygıtında sayaç sunulmuyor"}
            info = {"Bellek denetleyicisi": f"{gpu['engine_memory']:.0f}%" if gpu.get("engine_memory") is not None else "",
                    "Güç tüketimi": f"{gpu['power']:.1f} W" if gpu.get("power") is not None else "",
                    "Güç sınırı": f"{gpu['power_limit']:.1f} W" if gpu.get("power_limit") is not None else ""}
        return values, hints, info

    def _series(self, *items):
        return [(label, color, list(self.histories.get(key, []))) for label, key, color in items]

    def _chart(self, title, subtitle, series, fixed_max=None, legend=True):
        frame, box = self._card()
        frame.get_style_context().add_class("graph-card")
        header = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        labels = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
        title_label = Gtk.Label(label=title, xalign=0); title_label.get_style_context().add_class("chart-title")
        sub = Gtk.Label(label=subtitle, xalign=0); sub.get_style_context().add_class("chart-subtitle")
        title_label.set_ellipsize(3); title_label.set_max_width_chars(28)
        sub.set_ellipsize(3); sub.set_max_width_chars(42)
        labels.pack_start(title_label, False, False, 0); labels.pack_start(sub, False, False, 0)
        header.pack_start(labels, False, False, 0)
        if legend:
            legend_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
            for label, _key, color in series:
                key = Gtk.Label(xalign=0)
                key.set_markup(f'<span foreground="{color}">●</span>  {GLib.markup_escape_text(label)}')
                key.set_ellipsize(3); key.set_max_width_chars(24)
                key.get_style_context().add_class("legend-item")
                legend_row.pack_start(key, False, False, 0)
            header.pack_start(legend_row, False, False, 0)
        box.pack_start(header, False, False, 0)
        chart = LineChart(lambda: self._series(*series), fixed_max)
        self.chart_widgets.append(chart)
        box.pack_start(chart, False, True, 8)
        return frame

    def _card(self, padding=16):
        frame = Gtk.Frame(); frame.set_shadow_type(Gtk.ShadowType.NONE); frame.get_style_context().add_class("card")
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        box.set_margin_start(padding); box.set_margin_end(padding); box.set_margin_top(padding); box.set_margin_bottom(padding)
        frame.add(box)
        return frame, box

    def _page_shell(self, title, description):
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=15)
        heading = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=13)
        icon_names = {
            "CPU": ("cpu-symbolic", "utilities-system-monitor-symbolic", "utilities-system-monitor"),
            "Bellek": ("media-floppy-symbolic", "drive-harddisk-symbolic", "drive-harddisk"),
            "Bluetooth": ("bluetooth-symbolic", "bluetooth"),
        }
        if title.startswith("Disk"): names = ("drive-harddisk-symbolic", "drive-harddisk")
        elif title.startswith("GPU"): names = ("video-display-symbolic", "video-display")
        elif title == "enp9s0" or title.startswith(("Ethernet", "Wi-Fi", "wlan", "en")):
            names = ("network-wired-symbolic", "network-wired", "network-workgroup")
        else: names = icon_names.get(title, ("network-wired-symbolic", "network-wired"))
        theme = Gtk.IconTheme.get_default()
        icon_name = next((name for name in names if theme.has_icon(name)), "utilities-system-monitor")
        icon = Gtk.Image.new_from_icon_name(icon_name, Gtk.IconSize.DIALOG)
        icon.set_pixel_size(25)
        icon_frame = Gtk.Box(); icon_frame.get_style_context().add_class("page-icon")
        icon_frame.set_valign(Gtk.Align.CENTER); icon_frame.add(icon)
        heading.pack_start(icon_frame, False, False, 0)
        title_stack = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        h = Gtk.Label(label=title, xalign=0); h.get_style_context().add_class("page-title")
        h.set_ellipsize(3); h.set_max_width_chars(26)
        d = Gtk.Label(label=description, xalign=0); d.get_style_context().add_class("page-description")
        d.set_ellipsize(3); d.set_max_width_chars(30)
        title_stack.pack_start(h, False, False, 0); title_stack.pack_start(d, False, False, 0)
        heading.pack_start(title_stack, True, True, 0)
        page.pack_start(heading, False, True, 4)
        return page

    def _tiles(self, values, columns=3):
        grid = ResponsiveGrid(columns, 184, 10, 10)
        for title, value, detail in values:
            cell = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=5)
            cell.get_style_context().add_class("metric-block")
            cell.set_size_request(184, -1)
            cell.set_hexpand(True)
            label = Gtk.Label(label=title, xalign=0); label.get_style_context().add_class("metric-label")
            label.set_ellipsize(3); label.set_max_width_chars(24)
            val = Gtk.Label(label=str(value), xalign=0); val.get_style_context().add_class("metric-value")
            val.set_ellipsize(3); val.set_max_width_chars(22)
            hint = Gtk.Label(label=str(detail), xalign=0); hint.get_style_context().add_class("muted"); hint.set_ellipsize(3)
            self.metric_value_refs[title] = val
            self.metric_hint_refs[title] = hint
            cell.pack_start(label, False, False, 0); cell.pack_start(val, False, False, 0); cell.pack_start(hint, False, False, 0)
            grid.add_cell(cell)
        return grid

    def _info_rows(self, rows, title=None):
        frame, box = self._card(16)
        if title:
            label = Gtk.Label(label=title, xalign=0); label.get_style_context().add_class("section-title")
            box.pack_start(label, False, False, 0)
        for index, (key, value) in enumerate(rows):
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=16)
            k = Gtk.Label(label=key, xalign=0); k.get_style_context().add_class("muted")
            k.set_ellipsize(3); k.set_max_width_chars(26)
            v = Gtk.Label(label=str(value), xalign=1); v.get_style_context().add_class("body-label"); v.set_selectable(True)
            v.set_ellipsize(3); v.set_max_width_chars(28)
            self.info_value_refs[key] = v
            row.pack_start(k, True, True, 0); row.pack_end(v, False, False, 0)
            box.pack_start(row, False, False, 0)
            if index < len(rows) - 1:
                sep = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL); box.pack_start(sep, False, False, 2)
        return frame

    def _cpu_page(self):
        c = self.data["cpu"]
        page = self._page_shell("CPU", c["model"])
        page.pack_start(self._chart("İşlemci etkinliği", "Son 90 örnek · toplam kullanım", [("Kullanım", "cpu", BLUE)], 100, False), False, True, 0)
        freq = f"{c['mhz']/1000:.2f} GHz" if c["mhz"] else "—"
        rows = [("Kullanım", f"{c['usage']:.1f}%", "Tüm mantıksal işlemcilerin ortalaması"),
                ("Anlık hız", freq, "Çekirdeklerin ölçülen ortalama frekansı"),
                ("İşlem", str(c["processes"]), "Çalışan süreç sayısı"),
                ("İş parçacığı", str(c["threads"]), "Süreçler genelindeki Linux thread sayısı"),
                ("Açık tanıtıcı", f"{c['open_files']:,}", f"Linux dosya tablosu · sistem sınırı {c['open_files_limit']:,}"),
                ("Çekirdek", str(c["cores"]), "Fiziksel çekirdek"),
                ("Mantıksal işlemci", str(c["logical"]), "İşletim sisteminin gördüğü iş parçacığı"),
                ("Soket", str(c["packages"]), "Fiziksel işlemci paketi"),
                ("Sanallaştırma", c["virtualization"], "CPU sanallaştırma uzantısı"),
                ("Çalışma süresi", str(timedelta(seconds=int(c["uptime"]))), "Sistemin son açılışından beri")]
        if c["max_mhz"]: rows.append(("Raporlanan üst hız", f"{c['max_mhz']/1000:.2f} GHz", "Sürücünün bildirdiği frekans sınırı"))
        if c["base_mhz"]: rows.append(("Temel hız", f"{c['base_mhz']/1000:.2f} GHz", "CPU frekans sürücüsünün bildirdiği temel hız"))
        if c["temp"] is not None: rows.append(("En yüksek CPU sıcaklığı", f"{c['temp']:.0f} °C", "Erişilebilen sıcaklık sensörleri"))
        for level, size in sorted(c["caches"].items()): rows.append((f"{level} önbellek", size, "CPU sysfs topoloji bilgisi"))
        page.pack_start(self._tiles(rows, 4), False, True, 0)
        if c["per_core"]:
            frame, box = self._card()
            label = Gtk.Label(label="Mantıksal işlemci kullanımı", xalign=0); label.get_style_context().add_class("section-title")
            box.pack_start(label, False, False, 0)
            grid = ResponsiveGrid(4, 156, 12, 10)
            for index, percent in enumerate(c["per_core"]):
                cell = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=5)
                cell.set_size_request(156, -1)
                hdr = Gtk.Label(label=f"CPU {index}  ·  {percent:.0f}%", xalign=0); hdr.get_style_context().add_class("muted")
                bar = Gtk.ProgressBar(); bar.get_style_context().add_class("progress"); bar.set_fraction(max(0, min(1, percent / 100)))
                self.cpu_core_refs.append((hdr, bar))
                cell.pack_start(hdr, False, False, 0); cell.pack_start(bar, False, False, 0)
                grid.add_cell(cell)
            box.pack_start(grid, False, True, 0); page.pack_start(frame, False, True, 0)
        return page

    def _memory_page(self):
        m = self.data["memory"]
        page = self._page_shell("Bellek", "Fiziksel RAM ve takas alanı kullanımı")
        page.pack_start(self._chart("Bellek kullanımı", "RAM ve swap doluluğu · son 90 örnek",
                                    [("RAM", "memory", PURPLE), ("Swap", "swap", ORANGE)], 100, True), False, True, 0)
        vals = [("Kullanılan RAM", f"{gib(m['used'])}  ·  {m['percent']:.1f}%", f"Toplam {gib(m['total'])}"),
                ("Kullanılabilir", gib(m["available"]), "Linux bellek baskısı olmadan kullanılabilir"),
                ("Swap kullanımı", f"{gib(m['swap_used'])}  ·  {m['swap_percent']:.1f}%", f"Toplam {gib(m['swap_total'])}"),
                ("Commit", gib(m["commit"]), f"Sınır {gib(m['commit_limit'])}"),
                ("Önbellek", gib(m["cached"]), "Linux sayfa önbelleği"),
                ("Bellek baskısı", f"{m['percent']:.0f}%", "Kullanılan ve kullanılabilir RAM oranı")]
        page.pack_start(self._tiles(vals, 3), False, True, 0)
        page.pack_start(self._info_rows([("Etkin bellek", gib(m["active"])), ("Etkin olmayan bellek", gib(m["inactive"])),
                                         ("Çekirdek arabellekleri", gib(m["buffers"])), ("Paylaşılan bellek", gib(m["shared"])),
                                         ("Çekirdek slab alanı", gib(m["slab"])),
                                         ("Geri alınabilir slab", gib(m["reclaimable_slab"])),
                                         ("Geri alınamayan slab", gib(m["unreclaimable_slab"]))], "Bellek ayrıntıları"), False, True, 0)
        return page

    def _disk_page(self, name):
        disk = self.data["disks"].get(name)
        if not disk: return self._page_shell("Disk", "Disk aygıtı artık bağlı değil")
        page = self._page_shell(f"Disk · {name}", f"{disk['model']} · {disk['kind']} · bütün hızlar MB/s")
        page.pack_start(self._chart("Disk etkinliği", "Okuma ve yazma aktarımı · son 90 örnek",
                                    [("Okuma", f"disk-read:{name}", TEAL), ("Yazma", f"disk-write:{name}", ORANGE)], None, True), False, True, 0)
        vals = [("Etkin süre", f"{disk['active']:.1f}%", "Örnek aralığında aygıtın meşgul kaldığı süre"),
                ("Okuma hızı", mbps(disk["read_bps"]), "Anlık disk aktarımı"),
                ("Yazma hızı", mbps(disk["write_bps"]), "Anlık disk aktarımı"),
                ("Okunan toplam", human_bytes(disk["read_bytes"]), "Sistem açıldığından beri"),
                ("Yazılan toplam", human_bytes(disk["write_bytes"]), "Sistem açıldığından beri"),
                ("Ortalama yanıt", f"{disk['latency_ms']:.1f} ms" if disk["latency_ms"] is not None else "—", "Örnek aralığındaki I/O işlemleri"),
                ("Aygıt kapasitesi", gib(disk["size"]), "Blok aygıtı kapasitesi"),
                ("Sistem diski", "Evet" if any("/" in v["mounts"] for v in disk["volumes"]) else "Hayır", "Kök dosya sistemini barındırıyor")]
        page.pack_start(self._tiles(vals, 4), False, True, 0)
        if disk["volumes"]:
            for vol in disk["volumes"]:
                mounts = ", ".join(vol["mounts"])
                label = f"{vol['device']}   ·   {vol['fstype']}   ·   {mounts}"
                frame, box = self._card(15)
                top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
                t = Gtk.Label(label=label, xalign=0); t.get_style_context().add_class("section-title")
                t.set_ellipsize(3); t.set_max_width_chars(36)
                percent = Gtk.Label(label=f"{vol['percent']:.1f}% dolu", xalign=1); percent.get_style_context().add_class("body-label")
                top.pack_start(t, True, True, 0); top.pack_end(percent, False, False, 0)
                bar = Gtk.ProgressBar(); bar.get_style_context().add_class("progress"); bar.set_fraction(vol["percent"] / 100)
                detail = Gtk.Label(label=f"{gib(vol['used'])} dolu   ·   {gib(vol['free'])} boş   ·   {gib(vol['total'])} toplam", xalign=0); detail.get_style_context().add_class("muted")
                detail.set_ellipsize(3); detail.set_max_width_chars(36)
                self.disk_volume_refs.append(((vol["device"], tuple(vol["mounts"])), percent, bar, detail))
                box.pack_start(top, False, False, 0); box.pack_start(bar, False, False, 0); box.pack_start(detail, False, False, 0)
                page.pack_start(frame, False, True, 0)
        else:
            page.pack_start(self._info_rows([("Bağlı dosya sistemleri", "Bu diskte bağlı bir mount noktası görünmüyor")]), False, True, 0)
        return page

    def _network_page(self, name):
        net = self.data["networks"].get(name)
        if not net: return self._page_shell(name, "Ağ arayüzü artık mevcut değil")
        page = self._page_shell(name, ("Kablosuz ağ" if net["wireless"] else "Ağ arayüzü") + (" · bağlı" if net["up"] else " · bağlantı yok"))
        page.pack_start(self._chart("Ağ trafiği", "Gelen ve giden veri · son 90 örnek · MB/s",
                                    [("İndirilen", f"net-down:{name}", BLUE), ("Gönderilen", f"net-up:{name}", ORANGE)], None, True), False, True, 0)
        vals = [("İndirme", mbps(net["down_bps"]), "Anlık gelen trafik"), ("Yükleme", mbps(net["up_bps"]), "Anlık giden trafik"),
                ("Bağlantı hızı", f"{net['speed_mbps']} Mbit/s" if net["speed_mbps"] else "Bilinmiyor", "Arayüzün raporladığı bağlantı hızı"),
                ("Durum", "Bağlı" if net["up"] else "Bağlı değil", f"MTU {net['mtu']}"),
                ("Alınan toplam", human_bytes(net["received"]), "Arayüz sayacı"),
                ("Gönderilen toplam", human_bytes(net["sent"]), "Arayüz sayacı")]
        if net["wireless"] and net["signal"] is not None: vals.append(("Wi-Fi sinyali", f"{net['signal']:.0f} dBm", "Çekirdek kablosuz arayüz sayacı"))
        if net["wireless"] and net["ssid"]: vals.append(("Kablosuz ağ", net["ssid"], "SSID"))
        page.pack_start(self._tiles(vals), False, True, 0)
        rows = [(family, address) for family, address in net["addresses"]]
        rows.insert(0, ("MAC adresi", read_text(f"/sys/class/net/{name}/address", "—")))
        rows.insert(1, ("Çift yön", net["duplex"].upper()))
        page.pack_start(self._info_rows(rows, "Arayüz ayrıntıları"), False, True, 0)
        return page

    def _bluetooth_page(self):
        bt = self.data["bluetooth"]
        page = self._page_shell("Bluetooth", "BlueZ üzerinden bağlı Bluetooth aygıtları")
        status = "Açık" if bt["powered"] else "Kapalı" if bt["powered"] is False else "Durum alınamadı"
        vals = [("Radyo durumu", status, bt["adapter"] or "Bluetooth adaptörü"),
                ("Bağlı cihaz", str(len(bt["devices"])), "Şu an bağlı aygıtlar"),
                ("Bluetooth servisi", "Çalışıyor" if bt["available"] else "Erişilemiyor", "Sistem BlueZ servisi")]
        page.pack_start(self._tiles(vals, 3), False, True, 0)
        frame, box = self._card(18)
        header = Gtk.Label(label="Bağlı cihazlar", xalign=0); header.get_style_context().add_class("section-title")
        box.pack_start(header, False, False, 0)
        if bt["devices"]:
            for device in bt["devices"]:
                row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
                icon = Gtk.Label(label="●"); icon.get_style_context().add_class("accent")
                text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
                name = Gtk.Label(label=device["name"], xalign=0); name.get_style_context().add_class("body-label")
                addr = Gtk.Label(label=device["address"], xalign=0); addr.get_style_context().add_class("muted")
                name.set_ellipsize(3); name.set_max_width_chars(24)
                addr.set_ellipsize(3); addr.set_max_width_chars(28)
                text.pack_start(name, False, False, 0); text.pack_start(addr, False, False, 0)
                row.pack_start(icon, False, False, 0); row.pack_start(text, True, True, 0)
                if device["battery"] is not None:
                    battery = Gtk.Label(label=f"Pil {device['battery']}%"); battery.get_style_context().add_class("body-label"); row.pack_end(battery, False, False, 0)
                    self.bluetooth_battery_refs[device["address"]] = battery
                box.pack_start(row, False, False, 5)
        else:
            empty = Gtk.Label(label="Şu anda bağlı Bluetooth cihazı yok. Cihaz bağlandığında burada görünür.", xalign=0)
            empty.get_style_context().add_class("muted"); empty.set_line_wrap(True)
            box.pack_start(empty, False, False, 4)
        page.pack_start(frame, False, True, 0)
        return page

    def _gpu_page(self, ident):
        gpu = next((g for g in self.data["gpus"] if g["id"] == ident), None)
        if not gpu: return self._page_shell("GPU", "Grafik aygıtı artık bağlı değil")
        page = self._page_shell(f"GPU · {gpu['name']}", "Grafik işlemci ve video belleği")
        used, total = gpu["used"], gpu["total"]
        mem_percent = used / total * 100 if used is not None and total else None
        gpu_usage = gpu["usage"]
        gpu_series = [("Genel kullanım", f"gpu:{ident}", BLUE)]
        if gpu["engine_memory"] is not None: gpu_series.append(("Bellek denetleyicisi", f"gpu-memory-controller:{ident}", PURPLE))
        if gpu["encoder"] is not None: gpu_series.append(("Kodlama", f"gpu-encoder:{ident}", TEAL))
        if gpu["decoder"] is not None: gpu_series.append(("Kod çözme", f"gpu-decoder:{ident}", ORANGE))
        page.pack_start(self._chart("GPU etkinliği", "Genel kullanım ve sürücünün sağladığı motor sayaçları · son 90 örnek", gpu_series, 100, True), False, True, 0)
        usage_text = f"{gpu_usage:.1f}%" if gpu_usage is not None else "Veri sunulmuyor"
        memory_text = f"{gib(used)} / {gib(total)}" if used is not None and total else "VRAM verisi yok"
        vals = [("GPU kullanımı", usage_text, gpu["source"]), ("VRAM kullanımı", f"{gib(used)}" if used is not None else "—", memory_text),
                ("VRAM doluluğu", f"{mem_percent:.1f}%" if mem_percent is not None else "—", f"Toplam {gib(total)}" if total else "Toplam kapasite sürücüden alınamadı"),
                ("GPU sıcaklığı", f"{gpu['temperature']:.0f} °C" if gpu["temperature"] is not None else "—", "Aygıt sıcaklık sensörü"),
                ("Grafik saati", f"{gpu['clock_mhz']:.0f} MHz" if gpu["clock_mhz"] is not None else "—", "Anlık grafik frekansı"),
                ("Paylaşılan GPU belleği", "Sürücü verisi yok", "Bu Linux aygıtında sayaç sunulmuyor")]
        page.pack_start(self._tiles(vals, 3), False, True, 0)
        if mem_percent is not None:
            page.pack_start(self._chart("VRAM doluluğu", "Ayrılmış video belleği · toplam kapasiteye oranı",
                                        [("VRAM", f"gpu-mem:{ident}", PURPLE)], 100, False), False, True, 0)
        rows = [("GPU modeli", gpu["name"]), ("Ölçüm kaynağı", gpu["source"]), ("Sürücü sürümü", gpu["driver"] or "Bilinmiyor")]
        if gpu["engine_memory"] is not None: rows.append(("Bellek denetleyicisi", f"{gpu['engine_memory']:.0f}%"))
        if gpu["power"] is not None: rows.append(("Güç tüketimi", f"{gpu['power']:.1f} W"))
        if gpu["power_limit"] is not None: rows.append(("Güç sınırı", f"{gpu['power_limit']:.1f} W"))
        page.pack_start(self._info_rows(rows, "Grafik aygıtı ayrıntıları"), False, True, 0)
        return page


class MiniChart(Gtk.DrawingArea):
    def __init__(self, color):
        super().__init__()
        self.color = color
        self.values = []
        self.set_size_request(54, 34)
        self.connect("draw", self._draw)

    def _draw(self, widget, cr):
        width = widget.get_allocated_width(); height = widget.get_allocated_height()
        cr.set_source_rgb(*hex_rgb(GRAPH_PALETTE["spark"])); cr.rectangle(0, 0, width, height); cr.fill()
        if len(self.values) < 2: return False
        rgb = hex_rgb(self.color)
        maximum = max(1, max(self.values))
        points = []
        for i, val in enumerate(self.values):
            x = width * i / max(1, len(self.values) - 1)
            y = height - 3 - (height - 6) * min(1, val / maximum)
            points.append((x, y))
        cr.move_to(points[0][0], height); cr.line_to(*points[0])
        for point in points[1:]: cr.line_to(*point)
        cr.line_to(points[-1][0], height); cr.close_path()
        cr.set_source_rgba(*rgb, .12); cr.fill_preserve()
        cr.set_source_rgb(*rgb); cr.set_line_width(1.7); cr.stroke()
        return False


class StatusNotifier:
    """StatusNotifierItem plus a minimal DBusMenu for native Wayland/KDE trays."""
    SNI_XML = """<node><interface name='org.kde.StatusNotifierItem'>
      <method name='Activate'><arg type='i' direction='in'/><arg type='i' direction='in'/></method>
      <method name='SecondaryActivate'><arg type='i' direction='in'/><arg type='i' direction='in'/></method>
      <method name='ContextMenu'><arg type='i' direction='in'/><arg type='i' direction='in'/></method>
      <method name='Scroll'><arg type='i' direction='in'/><arg type='s' direction='in'/></method>
      <property name='Category' type='s' access='read'/><property name='Id' type='s' access='read'/>
      <property name='Title' type='s' access='read'/><property name='Status' type='s' access='read'/>
      <property name='WindowId' type='u' access='read'/><property name='IconName' type='s' access='read'/>
      <property name='IconPixmap' type='a(iiay)' access='read'/><property name='OverlayIconName' type='s' access='read'/>
      <property name='OverlayIconPixmap' type='a(iiay)' access='read'/><property name='AttentionIconName' type='s' access='read'/>
      <property name='AttentionIconPixmap' type='a(iiay)' access='read'/><property name='AttentionMovieName' type='s' access='read'/>
      <property name='ToolTip' type='(sa(iiay)ss)' access='read'/><property name='ItemIsMenu' type='b' access='read'/>
      <property name='Menu' type='o' access='read'/>
    </interface></node>"""
    MENU_XML = """<node><interface name='com.canonical.dbusmenu'>
      <method name='GetLayout'><arg type='i' direction='in'/><arg type='i' direction='in'/><arg type='as' direction='in'/><arg type='u' direction='out'/><arg type='(ia{sv}av)' direction='out'/></method>
      <method name='GetGroupProperties'><arg type='ai' direction='in'/><arg type='as' direction='in'/><arg type='a(ia{sv})' direction='out'/></method>
      <method name='GetProperty'><arg type='i' direction='in'/><arg type='s' direction='in'/><arg type='v' direction='out'/></method>
      <method name='Event'><arg type='i' direction='in'/><arg type='s' direction='in'/><arg type='v' direction='in'/><arg type='u' direction='in'/></method>
      <method name='EventGroup'><arg type='a(isvu)' direction='in'/><arg type='a(is)' direction='out'/></method>
      <method name='AboutToShow'><arg type='i' direction='in'/><arg type='b' direction='out'/></method>
      <method name='AboutToShowGroup'><arg type='ai' direction='in'/><arg type='ai' direction='out'/><arg type='ai' direction='out'/></method>
      <property name='Version' type='u' access='read'/><property name='TextDirection' type='s' access='read'/><property name='Status' type='s' access='read'/><property name='IconThemePath' type='as' access='read'/>
      <signal name='LayoutUpdated'><arg type='u'/><arg type='i'/></signal>
      <signal name='ItemsPropertiesUpdated'><arg type='a(ia{sv})'/><arg type='a(ias)'/></signal>
    </interface></node>"""

    def __init__(self, app):
        self.app = app
        self.connection = None
        self.registrations = []
        self.bus_name = f"org.kde.StatusNotifierItem-{os.getpid()}-1"
        self.menu_revision = 1

    def start(self):
        try:
            self.connection = Gio.bus_get_sync(Gio.BusType.SESSION, None)
            sni_info = Gio.DBusNodeInfo.new_for_xml(self.SNI_XML).interfaces[0]
            menu_info = Gio.DBusNodeInfo.new_for_xml(self.MENU_XML).interfaces[0]
            self.registrations.append(self.connection.register_object("/StatusNotifierItem", sni_info, self._sni_method, self._sni_get, None))
            self.registrations.append(self.connection.register_object("/Menu", menu_info, self._menu_method, self._menu_get, None))
            self.connection.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus", "RequestName",
                                      GLib.Variant("(su)", (self.bus_name, 0)), GLib.VariantType.new("(u)"), Gio.DBusCallFlags.NONE, 1500, None)
            self.connection.call_sync("org.kde.StatusNotifierWatcher", "/StatusNotifierWatcher", "org.kde.StatusNotifierWatcher", "RegisterStatusNotifierItem",
                                      GLib.Variant("(s)", (self.bus_name,)), None, Gio.DBusCallFlags.NONE, 1500, None)
        except Exception as exc:
            # GTK's legacy tray icon remains active as a fallback on desktops with XEmbed support.
            print(f"Sistem tepsisi kaydı yapılamadı: {exc}", flush=True)

    def stop(self):
        if self.connection:
            for registration in self.registrations:
                try: self.connection.unregister_object(registration)
                except Exception: pass
            try:
                self.connection.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus", "ReleaseName",
                                          GLib.Variant("(s)", (self.bus_name,)), GLib.VariantType.new("(u)"), Gio.DBusCallFlags.NONE, 1000, None)
            except Exception: pass

    def _sni_get(self, _connection, _sender, _path, _interface, name):
        props = {"Category": GLib.Variant("s", "SystemServices"), "Id": GLib.Variant("s", "sistem-performansi"),
                 "Title": GLib.Variant("s", "Sistem Performansı"), "Status": GLib.Variant("s", "Active"),
                 "WindowId": GLib.Variant("u", 0), "IconName": GLib.Variant("s", "utilities-system-monitor"),
                 "IconPixmap": GLib.Variant("a(iiay)", []), "OverlayIconName": GLib.Variant("s", ""),
                 "OverlayIconPixmap": GLib.Variant("a(iiay)", []), "AttentionIconName": GLib.Variant("s", ""),
                 "AttentionIconPixmap": GLib.Variant("a(iiay)", []), "AttentionMovieName": GLib.Variant("s", ""),
                 "ToolTip": GLib.Variant("(sa(iiay)ss)", ("utilities-system-monitor", [], "Sistem Performansı", "Canlı sistem kaynakları")),
                 "ItemIsMenu": GLib.Variant("b", False), "Menu": GLib.Variant("o", "/Menu")}
        return props.get(name)

    def _sni_method(self, _connection, _sender, _path, _interface, method, _params, invocation):
        if method in ("Activate", "SecondaryActivate"):
            GLib.idle_add(self.app.show_window)
        invocation.return_value(None)

    def _menu_get(self, _connection, _sender, _path, _interface, name):
        props = {"Version": GLib.Variant("u", 3), "TextDirection": GLib.Variant("s", "ltr"),
                 "Status": GLib.Variant("s", "normal"), "IconThemePath": GLib.Variant("as", [])}
        return props.get(name)

    def _menu_item_properties(self, ident):
        if ident in (2, 4):
            return {"type": GLib.Variant("s", "separator"),
                    "enabled": GLib.Variant("b", False), "visible": GLib.Variant("b", True)}
        labels = {1: "Uygulamayı göster", 3: "Sistemle birlikte başlat", 5: "Tamamen kapat"}
        if ident not in labels:
            return None
        values = {"label": GLib.Variant("s", labels[ident]),
                  "enabled": GLib.Variant("b", True), "visible": GLib.Variant("b", True)}
        if ident == 3:
            values["toggle-type"] = GLib.Variant("s", "checkmark")
            values["toggle-state"] = GLib.Variant("i", int(is_autostart_enabled()))
        return values

    def _layout(self):
        children = [GLib.Variant("(ia{sv}av)", (ident, self._menu_item_properties(ident), []))
                    for ident in (1, 2, 3, 4, 5)]
        return GLib.Variant("(ia{sv}av)", (0, {}, children))

    def refresh_menu(self):
        self.menu_revision += 1
        if self.connection:
            try:
                self.connection.emit_signal(None, "/Menu", "com.canonical.dbusmenu", "LayoutUpdated",
                                            GLib.Variant("(ui)", (self.menu_revision, 0)))
            except Exception as exc:
                print(f"Tepsi menüsü güncellenemedi: {exc}", flush=True)

    def _menu_method(self, _connection, _sender, _path, _interface, method, params, invocation):
        if method == "GetLayout":
            invocation.return_value(GLib.Variant.new_tuple(GLib.Variant("u", self.menu_revision), self._layout()))
        elif method == "GetGroupProperties":
            ids = params.unpack()[0]
            props = []
            for ident in ids:
                values = self._menu_item_properties(ident)
                if values is not None:
                    props.append((ident, values))
            invocation.return_value(GLib.Variant("(a(ia{sv}))", (props,)))
        elif method == "GetProperty":
            ident, prop = params.unpack()
            values = self._menu_item_properties(ident) or {}
            value = values.get(prop)
            if value is None:
                invocation.return_dbus_error("com.canonical.dbusmenu.Error", "Unknown menu property")
                return
            invocation.return_value(GLib.Variant("(v)", (value,)))
        elif method == "Event":
            ident, event, _data, _timestamp = params.unpack()
            if event == "clicked": self._activate_menu_item(ident)
            invocation.return_value(None)
        elif method == "EventGroup":
            events = params.unpack()[0]
            errors = []
            for ident, event, _data, _timestamp in events:
                if event == "clicked": self._activate_menu_item(ident)
            invocation.return_value(GLib.Variant("(a(is))", (errors,)))
        elif method == "AboutToShow":
            invocation.return_value(GLib.Variant("(b)", (False,)))
        elif method == "AboutToShowGroup":
            invocation.return_value(GLib.Variant("(ai ai)", ([], [])))
        else:
            invocation.return_value(None)

    def _activate_menu_item(self, ident):
        callbacks = {1: self.app.show_window, 3: self.app.toggle_autostart, 5: self.app.quit}
        callback = callbacks.get(ident)
        if callback:
            GLib.idle_add(callback)


def main():
    application = SystemMonitorApplication()
    return application.run(sys.argv)


if __name__ == "__main__":
    main()
