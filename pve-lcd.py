#!/usr/bin/env python3
"""
pve-lcd - show useful Proxmox VE status on a Matrix Orbital character panel.

Talks the LCDproc widget language (protocol 0.4, LCDd 0.5.x) as a client on
127.0.0.1:13666. Five screens, each sized to the panel it is painting - the
size comes from LCDd's own banner, from PVELCD_SIZE/--size if you override it,
and --probe prints a ruler on the panel when the two disagree.  No `title`
widgets: a title bar would eat one row, and on a 4-row panel every row counts.

  node    node name + uptime, load average, cluster size / quorum, clock
  res     CPU %, full-width CPU bar, RAM used/total, full-width RAM bar
  guests  VM and container running counts, stopped ids, >90% full storage count
  disks   one overview line (how many volumes, how many over 90%) and then the
          fullest volumes with their fill %; with two or fewer volumes each one
          gets a full-width bar instead.  When there are more volumes than rows,
          the list is a window and the Up/Down keys move it.
  net     the interface this node routes through: link state, IPv4 address and
          prefix, gateway, measured rx/tx bandwidth, link speed and MTU.  This
          screen never calls pvesh: counters come from /proc/net/dev, the route
          from /proc/net/route, speed/MTU from /sys/class/net, the address by
          one socket ioctl.

Only the Python standard library is used. Reads /proc directly; uses `pvesh`
for VM/container and storage state, falling back to /etc/pve if pvesh fails.
CPU, RAM, uptime, load, the cluster check and the whole network screen never
touch pvesh - they come from /proc, /sys, /etc/pve and one ioctl.

Usage:
  pve-lcd.py                  run against LCDd (this is what the unit does)
  pve-lcd.py --test           print the screens to stdout instead (no LCDd)
  pve-lcd.py --probe          paint a column ruler on the real panel, then exit:
                              use it to measure how many columns the panel shows
  pve-lcd.py --size 17x4      draw for a fixed size instead of the size LCDd reports
  pve-lcd.py --screens node,guests
  pve-lcd.py --verbose        log every LCDd reply, including key events
  pve-lcd.py --interval 2     seconds between refreshes
  pve-lcd.py --hold 20        seconds to leave the ruler up (--probe)

Environment: PVELCD_HOST, PVELCD_PORT, PVELCD_REFRESH, PVELCD_VERBOSE=1,
             PVELCD_SIZE=20x4, and the polling rates below - a `pvesh` call is
             a Perl process that loads the whole Proxmox API, so these are the
             knobs for how much CPU the panel costs the machine:
               PVELCD_GUEST_TTL=30     seconds between guest polls (2 pvesh calls)
               PVELCD_STORAGE_TTL=180  seconds between storage polls (1 pvesh call)
               PVELCD_CLUSTER_TTL=60   seconds between /etc/pve reads (no pvesh)
               PVELCD_GUEST_SOURCE=files   read /etc/pve for the guest screen and
                                           never run pvesh for it
               PVELCD_RETRY_TTL=15     minimum wait after a collector failed
               PVELCD_SLOW_PVESH=1.5   log any pvesh call slower than this
                                       seconds (0 logs every call)
                PVELCD_NET_TTL=60       seconds between re-reading the interface
                                        details (bandwidth is sampled every refresh
                                        and costs nothing)
                PVELCD_NET_IFACE=vmbr0  use this interface instead of the one the
                                        default route uses
                PVELCD_KEYS="Up Down Left Right Enter"
                                        which button names this client reserves;
                                        they must match the KeyMap_ names written in
                                        /etc/LCDd.conf (empty string = no buttons)
                PVELCD_ROTATION=client  client = this program rotates the screens and
                                        the buttons are its own; lcdproc = leave
                                        rotation to LCDd (needs [server] AutoRotate=yes)
                PVELCD_NOTICE_SECONDS=2.5
                                        seconds the `| Hold` / `| Rotate` / `| Prev` /
                                        `| Next` word is written in the corner of the
                                        panel after a button changed the screen
                                        (0 never shows it)
"""

import argparse
import fcntl
import json
import os
import re
import select
import signal
import socket
import struct
import subprocess
import sys
import time
import traceback
from collections import deque

DEFAULT_HOST = os.environ.get("PVELCD_HOST", "127.0.0.1")
DEFAULT_PORT = int(os.environ.get("PVELCD_PORT", "13666"))
DEFAULT_REFRESH = float(os.environ.get("PVELCD_REFRESH", "2"))
CLIENT_NAME = "pve-lcd"
RECONNECT_DELAY = 5

def env_number(name, default, lowest=1.0):
    """Read a tunable number from the environment.

    Bad or missing input falls back to the default, silently: this runs while
    the module is still loading, so it must not depend on log().
    """
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return max(lowest, value)


# Durations are in eighths of a second (LCDd protocol), so 80 == 10 seconds.
# All five screens get 10 seconds; raise one to keep it on the panel longer.
DURATIONS = {"node": 80, "res": 80, "guests": 80, "disks": 80, "net": 80}

# Polling rates in seconds, i.e. how often the expensive data sources are
# touched.  A repaint every PVELCD_REFRESH seconds is cheap (it only writes
# strings to a socket); pvesh is not, and each call is a separate process, so
# these rates - not the refresh interval - decide what the panel costs the node.
GUEST_TTL = env_number("PVELCD_GUEST_TTL", 30.0)        # 2 pvesh calls (qemu, lxc)
STORAGE_TTL = env_number("PVELCD_STORAGE_TTL", 180.0)   # 1 pvesh call
CLUSTER_TTL = env_number("PVELCD_CLUSTER_TTL", 60.0)    # /etc/pve reads only
RETRY_TTL = env_number("PVELCD_RETRY_TTL", 15.0)        # after a collector failed
WARN_FILL = 90.0     # percentage that counts as "almost full"

# "files" reads the guest list out of /etc/pve instead of asking pvesh, which
# removes 2 of the 3 pvesh calls a otherwise-cold client makes.
GUEST_SOURCE = (os.environ.get("PVELCD_GUEST_SOURCE") or "pvesh").strip().lower()
GUEST_FROM_FILES = GUEST_SOURCE in ("files", "file", "etc-pve", "etc/pve", "no-pvesh", "off")

# A pvesh call slower than this is logged, so the cost is visible in the journal.
# Set PVELCD_SLOW_PVESH=0 to log every call.
SLOW_PVESH = env_number("PVELCD_SLOW_PVESH", 1.5, 0.0)

# The network screen reads /proc and /sys only, so its rate costs nothing; the
# interface details are re-read on this timer while the bandwidth counters are
# sampled on every refresh (a rate needs two samples, and both are free reads).
NET_TTL = env_number("PVELCD_NET_TTL", 60.0)
NET_IFACE = (os.environ.get("PVELCD_NET_IFACE") or "").strip()

# LCDd wrote a word in the bottom-right corner whenever a button changed what the panel
# shows: `| Hold` when the tick stopped the rotation, `| Rotate` when it let it run on,
# `| Prev` and `| Next` when the arrows moved it (input.c:155-170 -> server_msg(), four
# frames in render.c:180-186).  In client mode this client reserves those keys, so
# LCDd's handler never runs and that corner word disappeared: these are how long it
# stays here, in seconds.  PVELCD_NOTICE_SECONDS=0 turns the word off.
NOTICE_SECONDS = env_number("PVELCD_NOTICE_SECONDS", 2.5, 0.0)


def env_list(name, default):
    """Whitespace/comma separated list from the environment, with a default."""
    raw = (os.environ.get(name) or "").strip() or default
    return [item for item in raw.replace(",", " ").split() if item]


# Button names this client asks LCDd for.  These are the names the MtxOrb
# KeyMap_ lines produce (KeyMap_C=Up and so on), not the characters the panel
# sends, so install.sh keys-nav writes exactly this set.
KEY_NAMES = env_list("PVELCD_KEYS", "Up Down Left Right Enter")

# What decides which of this client's screens the panel shows.
#   client   this program changes the screens itself (default): the panel starts
#            rotating at boot without anyone pressing a button
#   lcdproc  every screen stays `info` and LCDd rotates them with its own
#            AutoRotate/duration settings
ROTATION_MODE = (os.environ.get("PVELCD_ROTATION") or "client").strip().lower()
if ROTATION_MODE not in ("client", "lcdproc"):
    ROTATION_MODE = "client"

# Left/Right move between screens, Up/Down move the row window on the screen that
# is showing, Enter holds the current screen or lets it rotate on.  Everything is
# matched case-insensitively because the name is whatever KeyMap_ wrote.
KEY_ACTIONS = {
    "left": "prev", "right": "next", "up": "up", "down": "down", "enter": "hold",
}

VERBOSE = os.environ.get("PVELCD_VERBOSE", "0") not in ("0", "", "no", "false")


def parse_size(text):
    """Parse a display size like '20x4'; empty or bad input returns None."""
    if not text:
        return None
    match = re.fullmatch(r"\s*(\d{1,2})\s*[xX]\s*(\d{1,2})\s*", str(text))
    if not match:
        return None
    cols, rows = int(match.group(1)), int(match.group(2))
    if cols < 8 or cols > 80 or rows < 1 or rows > 10:
        return None
    return (cols, rows)


def measure_rows(cols, rows):
    """Ruler rows for --probe: read the last digit/letter on the panel to count columns.

    Column numbers are printed 1-based, so if the panel shows 17 columns the digit
    row ends in 7 and the letter row ends in Q (A=1 ... Q=17).  The '+' row marks
    columns 1, 10, 20, ... so a missing '+' at the right edge means the panel is
    narrower than LCDd believes.
    """
    span = max(20, cols)
    digits = "".join(str(n % 10) for n in range(1, span + 1))
    marks = "".join("+" if (n == 1 or n % 10 == 0) else "-" for n in range(1, span + 1))
    letters = ("ABCDEFGHIJ" * (span // 10 + 2))[:span]
    out = [fit(digits, cols), fit(marks, cols), fit(letters, cols)]
    out.append(fit(f"LCDd says {cols}x{rows}", cols))
    while len(out) < rows:
        out.append(fit(digits, cols))
    return out[:rows]


def log(message):
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    print(f"[pve-lcd {stamp}] {message}", file=sys.stderr, flush=True)


def quote(text):
    """Quote a string for the LCDd widget language."""
    escaped = str(text).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def fit(text, width):
    """Force text to fit exactly `width` columns; '>' marks a truncation."""
    text = str(text)
    if len(text) <= width:
        return text.ljust(width)
    return text[: width - 1] + ">"


def fit_lr(left, right, width):
    """Left-align `left`, right-align `right`, together exactly `width` wide."""
    right = str(right)
    left = str(left)
    if len(left) + len(right) > width:
        left = left[: max(0, width - len(right) - 1)]
        if width - len(right) - 1 > 0:
            left += ">"
    return (left + " " * max(0, width - len(left) - len(right)) + right).ljust(width)


def corner(text, word, cols):
    """Write `| word` over the end of a row, the way LCDd's own messages were drawn.

    The row keeps whatever fits in front of the word; a `>` marks where the word
    covered it.  LCDd did exactly this when its key handlers called `server_msg()`:
    render.c paints `| Hold` at `width - strlen + 1` on the last row for a few frames,
    on top of whatever the screen had there.
    """
    if not word:
        return str(text)
    right = "| " + word
    if len(right) >= cols:
        return fit(right, cols)
    room = cols - len(right) - 1          # one blank column keeps the two apart
    left = str(text)
    if len(left) > room:
        left = left[:room - 1] + ">" if room > 1 else ""
    return (left + " " * max(0, cols - len(left) - len(right)) + right).ljust(cols)


def bar(fraction, width):
    """A full-width text bar, e.g. [##########----------] (width including brackets)."""
    inner = max(0, width - 2)
    if fraction is None:
        return "[" + "?" * inner + "]"
    filled = int(round(max(0.0, min(1.0, fraction)) * inner))
    return "[" + "#" * filled + "-" * (inner - filled) + "]"


def gib(number, decimals=1):
    """Bytes -> human GiB string."""
    if number is None:
        return "?"
    value = number / (1024 ** 3)
    if value >= 100:
        decimals = 0
    return f"{value:.{decimals}f}G"


def pct_text(fraction):
    if fraction is None:
        return " --%"
    return f"{100.0 * fraction:5.1f}%"


def quorum_text(quorate):
    """Cluster status wording that fits on one 20 column row."""
    if quorate is None:
        return "no cluster"
    return "cluster quorate" if quorate else "cluster NO QUORUM"


def pick(candidates, cols):
    """First candidate short enough for the panel, so a narrow display loses wording
    instead of losing characters off the right-hand edge."""
    for text in candidates:
        if len(text) <= cols:
            return text
    return candidates[-1]


def pair(lefts, rights, cols, gap=1):
    """Left/right aligned line built from candidate labels.

    Tries the most detailed left label against the most detailed right label and
    steps down until the pair fits, so nothing is chopped with a '>' marker.  `gap`
    columns of air between the two halves are part of the budget: without it a header
    like `1 storage >90%1/2` reads as one garbled word.
    """
    lefts = [lefts] if isinstance(lefts, str) else list(lefts)
    rights = [rights] if isinstance(rights, str) else list(rights)
    budget = cols - gap
    for left in lefts:
        for right in rights:
            if len(left) + len(right) <= budget:
                return fit_lr(left, right, cols)
    return fit_lr(lefts[-1], rights[-1], cols)


# --------------------------------------------------------------------------
# small TTL cache so pvesh is not spammed every refresh
# --------------------------------------------------------------------------

class Cache:
    """One value per key, refreshed at most once per ttl seconds.

    A failed producer is retried, but never on every repaint: the attempt is
    stamped too.  Without that, a collector that raises would be tried again on
    the next cycle - every PVELCD_REFRESH seconds, forever - which is exactly
    how a small status client ends up hammering pvesh and eating a core.
    """

    def __init__(self):
        self._store = {}

    def get(self, key, ttl, producer):
        now = time.monotonic()
        value, stamp, failed = self._store.get(key, (None, None, False))
        wait = min(ttl, RETRY_TTL) if failed else ttl
        # On a freshly booted machine monotonic() is only a few seconds old, so
        # "no stamp yet" has to be detected explicitly, not by comparing to 0.
        if stamp is None or now - stamp >= wait:
            try:
                value = producer()
                self._store[key] = (value, now, False)
            except Exception as exc:                       # keep the last value
                log(f"collect {key}: {exc} (retrying in at least {RETRY_TTL:.0f}s)")
                self._store[key] = (value, now, True)
        return value


# --------------------------------------------------------------------------
# data collection
# --------------------------------------------------------------------------

def pvesh(path, timeout=10):
    """Run one pvesh call, timed.

    pvesh is not free: it starts a Perl interpreter, loads the Proxmox API,
    then answers.  That process runs inside this client's cgroup, so its CPU is
    charged to pve-lcd.service - which is why the journal can show hours of CPU
    for a client that only paints four rows.  Timing the call here makes the
    cost measurable: PVELCD_SLOW_PVESH logs the slow ones, --verbose logs all.
    """
    cmd = ["pvesh", "get", path, "--output-format", "json"]
    started = time.monotonic()
    result = None
    problem = None
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if proc.returncode != 0:
            problem = f"exit {proc.returncode}: {proc.stderr.strip()[:160]}"
        else:
            try:
                result = json.loads(proc.stdout)
            except json.JSONDecodeError as exc:
                problem = f"unparsable output: {exc}"
    except (subprocess.TimeoutExpired, OSError) as exc:
        problem = str(exc)
    cost = time.monotonic() - started
    if problem is not None:
        log(f"pvesh {path}: {problem}  (after {cost:.1f}s)")
    elif VERBOSE or cost >= SLOW_PVESH:
        log(f"pvesh {path} took {cost:.1f}s")
    return result


def read_cpu_line():
    with open("/proc/stat", "r", encoding="utf-8") as handle:
        fields = [int(x) for x in handle.readline().split()[1:]]
    idle = fields[3] + (fields[4] if len(fields) > 4 else 0)
    return sum(fields), idle


def cpu_fraction(state):
    try:
        total, idle = read_cpu_line()
    except OSError as exc:
        log(f"/proc/stat: {exc}")
        return None
    previous = getattr(state, "cpu_sample", None)
    state.cpu_sample = (total, idle)
    prev_total, prev_idle = previous if previous else (None, None)
    if prev_total is None:
        return None
    d_total, d_idle = total - prev_total, idle - prev_idle
    if d_total <= 0:
        return None
    return (d_total - d_idle) / d_total


def read_meminfo():
    values = {}
    with open("/proc/meminfo", "r", encoding="utf-8") as handle:
        for line in handle:
            key, _, rest = line.partition(":")
            if not rest.strip():
                continue
            values[key.strip()] = int(rest.split()[0]) * 1024
    return values


def read_uptime_and_load():
    uptime = None
    load = None
    try:
        with open("/proc/uptime", "r", encoding="utf-8") as handle:
            uptime = float(handle.read().split()[0])
    except OSError as exc:
        log(f"/proc/uptime: {exc}")
    try:
        with open("/proc/loadavg", "r", encoding="utf-8") as handle:
            load = tuple(handle.read().split()[:3])
    except OSError as exc:
        log(f"/proc/loadavg: {exc}")
    return uptime, load


def format_uptime(seconds):
    if seconds is None:
        return "?"
    seconds = int(seconds)
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60
    if days:
        return f"{days}d {hours:02d}:{minutes:02d}"
    if hours:
        return f"{hours}:{minutes:02d}"
    return f"{minutes}m"


def collect_cluster():
    """Return (node_count, quorate_or_None). None means 'not clustered'."""
    nodes = []
    try:
        nodes = [n for n in os.listdir("/etc/pve/nodes") if not n.startswith(".")]
    except Exception as exc:
        log(f"/etc/pve/nodes: {exc}")
    clustered = False
    for candidate in ("/etc/pve/corosync.conf", "/etc/pve/cluster.conf"):
        if os.path.exists(candidate):
            clustered = True
            quorate = None
            try:
                with open(candidate, "r", encoding="utf-8", errors="replace") as handle:
                    match = re.search(r"quorate\s*:\s*([01])", handle.read())
                if match:
                    return max(len(nodes), 1), match.group(1) == "1"
            except Exception as exc:
                log(f"{candidate}: {exc}")
            return max(len(nodes), 1), None
    return max(len(nodes), 1), None


def collect_guests_from_files():
    """Guest list read out of the Proxmox cluster filesystem - no pvesh at all.

    The status written in these .conf files is what pvedaemon last recorded, so
    it can lag a few seconds behind reality; pvesh is the authoritative source.
    This is the fallback when pvesh fails, and what PVELCD_GUEST_SOURCE=files
    asks for on purpose when the pvesh calls are the thing costing CPU.
    """
    fallback = {}
    for kind, directory in (("qemu", "/etc/pve/qemu-server"), ("lxc", "/etc/pve/lxc")):
        if not os.path.isdir(directory):
            return None
        items = []
        for name in sorted(os.listdir(directory)):
            if not name.endswith(".conf"):
                continue
            vmid = name[: -len(".conf")]
            status = "stopped"
            try:
                with open(os.path.join(directory, name), "r", encoding="utf-8",
                          errors="replace") as handle:
                    for line in handle:
                        if line.startswith("status:"):
                            status = line.split(":", 1)[1].strip()
                            break
            except OSError:
                pass
            items.append((vmid, status))
        fallback[kind] = items
    fallback["source"] = "files"
    return fallback


def collect_guests(node):
    """Return {'qemu': [(id, status)], 'lxc': [...], 'source': 'pvesh'|'files'}."""
    if GUEST_FROM_FILES:
        chosen = collect_guests_from_files()
        if chosen is not None:
            chosen["why"] = "chosen"
        return chosen

    result = {}
    ok = True
    for kind in ("qemu", "lxc"):
        entries = pvesh(f"/nodes/{node}/{kind}")
        if not isinstance(entries, list):
            ok = False
            break
        result[kind] = [
            (str(item.get("vmid", "?")), str(item.get("status", "?")))
            for item in entries
        ]
    if ok:
        result["source"] = "pvesh"
        return result

    fallback = collect_guests_from_files()
    if fallback is not None:
        fallback["why"] = "pvesh failed"
    return fallback


def collect_storage(node):
    """Return [(storage_id, total_bytes, used_bytes)] for every enabled storage.

    pvesh spells the free space `avail`, not `available`, and it also reports the
    share in use directly as `used_fraction`.  Reading a key that does not exist
    makes every volume look 100% full, so the fraction Proxmox calculated itself
    comes first, then `avail`, then `used`.
    """
    data = pvesh(f"/nodes/{node}/storage")
    rows = []
    if isinstance(data, list):
        for item in data:
            total = item.get("total") or 0
            if not total:
                continue
            if item.get("enabled") in (0, False):
                continue
            used = None
            fraction = item.get("used_fraction")
            if isinstance(fraction, (int, float)):
                used = total * fraction
            else:
                for key in ("avail", "used"):
                    value = item.get(key)
                    if isinstance(value, (int, float)):
                        used = (total - value) if key == "avail" else value
                        break
            if used is None:
                continue
            used = max(0.0, min(float(used), float(total)))
            rows.append((str(item.get("storage", item.get("id", "?"))), total, used))
    if rows:
        return rows

    # Fallback: at least report the root filesystem.
    try:
        info = os.statvfs("/")
        total = info.f_blocks * info.f_frsize
        used = (info.f_blocks - info.f_bfree) * info.f_frsize
        return [("rootfs", total, used)]
    except Exception as exc:
        log(f"storage fallback failed: {exc}")
        return []


# --------------------------------------------------------------------------
# network: /proc/net/dev counters, /proc/net/route, /sys/class/net, one ioctl
# --------------------------------------------------------------------------

# The numbers the kernel uses when a socket ioctl asks an interface for its
# address or its netmask (the values every Linux language uses for this).
SIOCGIFADDR = 0x8915
SIOCGIFNETMASK = 0x891b


def hex_to_ip(value):
    """/proc/net/route writes an IPv4 address as little-endian hex."""
    try:
        return socket.inet_ntoa(bytes.fromhex(value)[::-1])
    except ValueError:
        return None


def read_net_counters():
    """{interface: (bytes received, bytes sent)} out of /proc/net/dev."""
    counters = {}
    try:
        with open("/proc/net/dev", "r", encoding="utf-8") as handle:
            lines = handle.readlines()[2:]          # two header lines first
    except OSError as exc:
        log(f"/proc/net/dev: {exc}")
        return counters
    for line in lines:
        name, _, rest = line.partition(":")
        fields = rest.split()
        if len(fields) < 9:                          # need both byte columns
            continue
        try:
            counters[name.strip()] = (int(fields[0]), int(fields[8]))
        except ValueError:
            continue
    return counters


def default_route():
    """(interface, gateway) of the default route; the lowest metric wins."""
    try:
        with open("/proc/net/route", "r", encoding="utf-8") as handle:
            lines = handle.readlines()[1:]
    except OSError as exc:
        log(f"/proc/net/route: {exc}")
        return None, None
    best = None
    for line in lines:
        fields = line.split()
        if len(fields) < 8 or fields[1] != "00000000":
            continue                                 # not the default route
        try:
            metric = int(fields[6])
        except ValueError:
            metric = 0
        if best is None or metric < best[0]:
            best = (metric, fields[0], hex_to_ip(fields[2]))
    if best is None:
        return None, None
    return best[1], best[2]


def busiest_iface(counters):
    """Fallback when there is no default route: the interface that moved the most."""
    best = None
    for name, (received, _) in counters.items():
        if name == "lo":
            continue
        if best is None or received > best[1]:
            best = (name, received)
    return best[0] if best else None


def iface_ipv4(name, request=SIOCGIFADDR):
    """One interface's IPv4 address (or netmask), asked from the kernel directly.

    A single ioctl on a datagram socket: no `ip`, no `ifconfig`, no subprocess.
    """
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    except OSError:
        return None
    try:
        answer = fcntl.ioctl(probe.fileno(), request,
                             struct.pack("256s", name.encode("utf-8")[:15]))
    except OSError:
        return None
    finally:
        probe.close()
    try:
        return socket.inet_ntoa(answer[20:24])
    except OSError:
        return None


def prefix_len(netmask):
    if not netmask:
        return None
    try:
        packed = socket.inet_aton(netmask)
    except OSError:
        return None
    return sum(bin(byte).count("1") for byte in packed)


def sysfs_net(name, key):
    try:
        with open(f"/sys/class/net/{name}/{key}", "r", encoding="utf-8") as handle:
            return handle.read().strip()
    except OSError:
        return None


def net_number(text):
    """A /sys/class/net value as an int; 0 and -1 (no carrier, virtual link) mean None."""
    try:
        value = int(text)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def collect_net():
    """Details of the interface this node routes through.  Never calls pvesh."""
    if NET_IFACE:
        iface, gateway = NET_IFACE, None
    else:
        iface, gateway = default_route()
    if not iface:
        iface = busiest_iface(read_net_counters())
    if not iface:
        return None
    netmask = iface_ipv4(iface, SIOCGIFNETMASK)
    return {
        "iface": iface,
        "chosen": "PVELCD_NET_IFACE" if NET_IFACE else "default route",
        "ipv4": iface_ipv4(iface),
        "netmask": netmask,
        "prefix": prefix_len(netmask),
        "gateway": gateway,
        "speed": net_number(sysfs_net(iface, "speed")),          # Mb/s
        "mtu": net_number(sysfs_net(iface, "mtu")),
        "operstate": (sysfs_net(iface, "operstate") or "").lower() or None,
    }


def net_rates(state, iface):
    """(rx, tx) bytes per second for one interface, measured between two samples.

    Counters are cumulative since boot, so a rate needs the previous sample; the
    sample itself is one small /proc read per refresh, which is why this screen
    is free where the guest and storage screens are not.
    """
    counters = read_net_counters()
    now = time.monotonic()
    previous = state.net_sample
    state.net_sample = (now, counters)
    if not counters or previous is None:
        return None, None
    elapsed = now - previous[0]
    before, after = previous[1].get(iface), counters.get(iface)
    if elapsed <= 0 or not before or not after:
        return None, None
    return (max(0, after[0] - before[0]) / elapsed,
            max(0, after[1] - before[1]) / elapsed)


def rate_text(bytes_per_second):
    """A rate as 12.3M / 870k / 45: bits per second, the way links are quoted."""
    if bytes_per_second is None:
        return "?"
    bits = bytes_per_second * 8.0
    if bits >= 1e9:
        return f"{bits / 1e9:.2f}G"
    if bits >= 1e6:
        return f"{bits / 1e6:.1f}M"
    if bits >= 1e3:
        return f"{bits / 1e3:.0f}k"
    return f"{bits:.0f}"


def link_text(megabits):
    """A link speed from /sys, which writes Mb/s: 1000 becomes 1G, 10000 becomes 10G."""
    if not megabits:
        return "?"
    if megabits >= 1000:
        return f"{megabits / 1000.0:.1f}G".replace(".0G", "G")
    return f"{megabits}M"


def fill_fraction(row):
    _, total, used = row
    return (used / total) if total else 0.0


def storage_by_need(rows):
    """Fullest first, largest first as the tie-break.

    The volume that needs attention belongs on a 4-row panel, not the one with the
    most raw space.  Sorted the other way round, a 1.7 TiB pool sitting at 4% would
    push a 64 GiB volume at 70% off the screen.
    """
    return sorted(rows, key=lambda row: (-fill_fraction(row), -row[1]))


def count_almost_full(storage_rows):
    return sum(1 for _, total, used in storage_rows
               if total and (used / total) * 100.0 >= WARN_FILL)


def cluster_info(state):
    """(node count, quorate) - falls back to a sensible default if the cache is empty."""
    cached = state.cache.get("cluster", CLUSTER_TTL, collect_cluster)
    return cached if cached else (1, None)


# --------------------------------------------------------------------------
# screens: each returns a list of rows, one string per display row
# --------------------------------------------------------------------------

def screen_node(state, cols, rows):
    uptime, load = read_uptime_and_load()
    node_count, quorate = cluster_info(state)
    now = time.localtime()

    out = []
    out.append(fit_lr(state.node, format_uptime(uptime), cols))
    if load:
        out.append(fit(pick([f"load {load[0]} {load[1]} {load[2]}",
                             f"load {load[0]} {load[1]}",
                             f"load {load[0]}"], cols), cols))
    else:
        out.append(fit("load ?", cols))
    if quorate is None:
        label = "node" if node_count == 1 else "nodes"
        out.append(fit(pick([f"no cluster ({node_count} {label})",
                             "single node", "1 node"], cols), cols))
    else:
        health = "quorate" if quorate else "NO QUORUM"
        label = "node" if node_count == 1 else "nodes"
        out.append(fit(pick([f"{node_count} {label}  {health}",
                             f"{node_count} {label} {health}",
                             health], cols), cols))
    out.append(fit(pick([time.strftime("%H:%M:%S  %d %b", now),
                         time.strftime("%H:%M:%S", now)], cols), cols))
    return out[:rows]


def screen_resources(state, cols, rows):
    fraction = cpu_fraction(state)
    try:
        mem = read_meminfo()
    except OSError as exc:
        log(f"/proc/meminfo: {exc}")
        mem = {}
    total = mem.get("MemTotal") or 0
    available = mem.get("MemAvailable")
    if available is None:
        available = mem.get("MemFree", 0) + mem.get("Buffers", 0) + mem.get("Cached", 0)
    used = total - available
    ram_fraction = (used / total) if total else None

    cores = os.cpu_count() or 0
    out = [
        pair([f"CPU {pct_text(fraction).strip()}", f"CPU {pct_text(fraction).strip()[:4]}"],
             [f"{cores} cores", f"{cores} cpu{'s' if cores != 1 else ''}", f"{cores}c"],
             cols),
        bar(fraction, cols),
        pair([f"RAM {gib(used)}/{gib(total)}",
              f"RAM {gib(used, 0)}/{gib(total, 0)}",
              "RAM"],
             [pct_text(ram_fraction).strip()], cols),
        bar(ram_fraction, cols),
    ]
    return out[:rows]


def screen_guests(state, cols, rows):
    guests = state.cache.get("guests", GUEST_TTL, lambda: collect_guests(state.node))
    storage = state.cache.get("storage", STORAGE_TTL, lambda: collect_storage(state.node))

    if not guests:
        out = [fit("no guest data", cols), fit("pvesh failed", cols)]
        return out[:rows]

    out = []
    for kind, label in (("qemu", "VM"), ("lxc", "CT")):
        entries = guests.get(kind, [])
        running = sum(1 for _, status in entries if status == "running")
        out.append(fit(pick([f"{label}  {running}/{len(entries)} running",
                             f"{label}  {running}/{len(entries)} up",
                             f"{label} {running}/{len(entries)}"], cols), cols))
    stopped = sorted(vmid for kind in ("qemu", "lxc") for vmid, status in guests.get(kind, [])
                     if status != "running")
    if stopped:
        ids = " ".join(stopped)
        out.append(fit(pick([f"not running: {ids}", f"stopped: {ids}",
                             f"{len(stopped)} guest(s) stopped"], cols), cols))
    else:
        out.append(fit(pick(["all guests running", "all guests up", "all running"], cols), cols))
    if guests.get("source") == "files" and guests.get("why") != "chosen":
        out.append(fit(pick(["pvesh failed, read /etc/pve", "source: /etc/pve",
                             "/etc/pve"], cols), cols))
    else:
        almost = count_almost_full(storage or [])
        out.append(fit(pick([f"{almost} storage >{int(WARN_FILL)}%", "storage ok"], cols)
                      if almost else fit("storage ok", cols), cols))
    return out[:rows]


def screen_disks(state, cols, rows):
    storage = storage_by_need(
        state.cache.get("storage", STORAGE_TTL, lambda: collect_storage(state.node)))
    if not storage:
        return [fit("no storage info", cols)] * min(2, rows)

    almost = count_almost_full(storage)

    if len(storage) <= rows // 2:
        # Only a couple of volumes: there is room for a full-width bar under each.
        out = []
        for name, total, used in storage:
            fraction = (used / total) if total else None
            out.append(fit_lr(name, pct_text(fraction).strip(), cols))
            out.append(bar(fraction, cols))
        if len(out) < rows:
            _, quorate = cluster_info(state)
            _, total, used = storage[0]
            free = max(0.0, total - used)
            out.append(fit(pick([f"{gib(used)} used {gib(free)} free",
                                 f"{gib(used, 0)} used {gib(free, 0)} free",
                                 f"{gib(used, 0)} of {gib(total, 0)}"], cols), cols))
            out.append(fit(quorum_text(quorate), cols))
        return out[:rows]

    # More volumes than bars fit: spend one row on the overview and name the rest,
    # fullest volume first (see storage_by_need).  Only `rows - 1` volumes fit, so
    # the list is a window and the Up/Down keys move it; the header says which
    # slice of the list is on the panel right now.
    slots = max(1, rows - 1)
    offset, window, pages = state.window_for("disks", storage, slots)
    if almost:
        left = [f"{almost} of {len(storage)} over {int(WARN_FILL)}%",
                f"{almost} storage >{int(WARN_FILL)}%",
                f"{almost} nearly full",
                f"over {int(WARN_FILL)}%: {almost}"]
    else:
        left = [f"{len(storage)} storage  all <{int(WARN_FILL)}%",
                f"{len(storage)} storage  all ok",
                f"{len(storage)} storage",
                "storage"]
    right = [f"{offset + 1}-{offset + len(window)}/{len(storage)}",
             f"{offset + 1}/{pages}"] if pages > 1 else [""]
    out = [pair(left, right, cols)]
    for name, total, used in window:
        out.append(fit_lr(name, pct_text((used / total) if total else None).strip(), cols))
    return out[:rows]


def screen_net(state, cols, rows):
    info = state.cache.get("net", NET_TTL, collect_net)
    if not info:
        return [fit("no network info", cols), fit("no interface found", cols)][:rows]
    rx, tx = net_rates(state, info["iface"])
    iface = info["iface"]
    link = link_text(info["speed"])
    state_txt = info["operstate"] or "link ?"

    out = []
    # Row one: which interface the panel is talking about, and whether its link is up.
    # The negotiated speed rides along on the same row when there is room for it.
    rights = ([f"{link} {state_txt}"] if link != "?" else []) + [state_txt, "link ?"]
    out.append(pair([iface, f"net {iface}"], rights, cols))

    # Address and gateway get a row each.  Two dotted IPv4 addresses side by side do not
    # fit a 17 or 20 column panel ("192.168.1.42/24" is 15 columns on its own, and
    # "gw 192.168.1.1" is 14), so pairing them would end in a chopped '>' fragment.
    addrs = []
    if info["ipv4"]:
        prefix = info["prefix"]
        if prefix is not None:
            addrs.append(f"ip {info['ipv4']}/{prefix}")
            addrs.append(f"{info['ipv4']}/{prefix}")
        addrs.append(f"ip {info['ipv4']}")
        addrs.append(info["ipv4"])
    out.append(fit(pick(addrs + ["no address"], cols), cols))

    gateway = info["gateway"]
    routes = [f"gw {gateway}", gateway] if gateway else []
    out.append(fit(pick(routes + ["no gateway"], cols), cols))

    out.append(fit(pick([f"rx {rate_text(rx)}  tx {rate_text(tx)}",
                         f"rx {rate_text(rx)} tx {rate_text(tx)}",
                         f"{rate_text(rx)} / {rate_text(tx)}",
                         "bandwidth ?"], cols), cols))

    # A taller panel has room for the static link details too; a four row panel does not,
    # and the live numbers matter more than an MTU that never changes.
    if rows > len(out):
        out.append(pair([f"{link} link", link, "link ?"],
                        [f"mtu {info['mtu']}" if info["mtu"] else "", ""], cols))
    return out[:rows]


SCREENS = {
    "node": ("Node", screen_node),
    "res": ("Resources", screen_resources),
    "guests": ("Guests", screen_guests),
    "disks": ("Storage", screen_disks),
    "net": ("Network", screen_net),
}


class State:
    """Shared state: node name, cache, the CPU and bandwidth sampling baselines,
    and the row windows that the Up/Down keys move."""

    def __init__(self, node):
        self.node = node
        self.cache = Cache()
        self.cpu_sample = None
        self.net_sample = None            # (sampled at, {iface: (rx, tx)})
        self.offsets = {}                 # screen id -> first row of its window
        self.pages = {}                   # screen id -> window positions available

    def window_for(self, sid, items, slots):
        """(offset, window, pages): the slice of `items` that fits in `slots` rows.

        Registering how many positions exist is what lets a Up/Down key press move
        the window; an offset past the end wraps to the other side.
        """
        pages = max(1, len(items) - slots + 1)
        offset = self.offsets.get(sid, 0) % pages
        self.offsets[sid] = offset
        self.pages[sid] = pages
        return offset, items[offset:offset + slots], pages

    def scroll(self, sid, delta):
        """Move one screen's window by `delta` rows.

        Returns False when that screen has nothing to scroll, so the caller can tell
        the user the key did something on this screen or not.
        """
        pages = self.pages.get(sid)
        if not pages or pages <= 1:
            return False
        self.offsets[sid] = (self.offsets.get(sid, 0) + delta) % pages
        return True


# --------------------------------------------------------------------------
# LCDd connection
# --------------------------------------------------------------------------

def reservable_keys():
    """The button names to ask LCDd for, minus the ones a client cannot reserve.

    `Escape` is held exclusively by LCDd's own menu for the internal client, so a
    client asking for it always fails - and 0.5.9 reports that failure on its own
    console while still answering `success`, which is a silent nothing.  In lcdproc
    rotation mode Left/Right/Enter are left with LCDd so its own PrevScreenKey,
    NextScreenKey and ToggleRotateKey keep working, and only Up/Down are taken: 0.5.9
    does nothing with those two by itself (its ScrollUpKey/ScrollDownKey handlers are
    empty), so scrolling is only ever done here.
    """
    names = [name for name in KEY_NAMES if name.lower() != "escape"]
    dropped = [name for name in KEY_NAMES if name.lower() == "escape"]
    if dropped:
        log("not reserving " + ", ".join(dropped) + ": LCDd's menu holds that key itself")
    if not names:
        return names
    if ROTATION_MODE != "client":
        kept = [name for name in names if name.lower() in ("up", "down")]
        log("rotation left to LCDd: reserving only " + (", ".join(kept) or "nothing") +
            ", Left/Right/Enter stay LCDd's own screen keys")
        return kept
    return names


class Rotation:
    """Which of this client's screens is on the panel, and whether it moves on.

    Rotation is driven here rather than left to LCDd, because LCDd 0.5.9 only advances
    screens when its [server] AutoRotate is switched on and no client command can
    switch it on: a client that wants the panel to start changing screens at boot has
    to change them itself.  Pinning the current screen at `foreground` and keeping the
    rest at `info` makes LCDd snap to the pinned one (it preempts whenever the first
    screen in its sorted list has a higher class than the one showing) and keeps its
    own rotation from wandering off, because rotation only walks inside one priority
    class.

    PVELCD_ROTATION=lcdproc keeps every screen at `info` and lets LCDd rotate them.
    """

    def __init__(self, screens, mode=ROTATION_MODE):
        self.screens = screens
        self.mode = mode
        self.index = 0
        self.held = False
        self.deadline = time.monotonic() + self.seconds(screens[0]["id"])

    def seconds(self, sid):
        """How long this screen stays up: the duration we asked for, in seconds."""
        return max(2.0, DURATIONS.get(sid, 80) / 8.0)

    def current(self):
        return self.screens[self.index]

    def apply(self, conn):
        """Pin the current screen and re-arm the timer for it."""
        if self.mode != "client":
            return
        for index, screen in enumerate(self.screens):
            wanted = "foreground" if index == self.index else "info"
            if screen.get("priority") != wanted:
                conn.send(f"screen_set {screen['id']} -priority {wanted}")
                screen["priority"] = wanted
        self.deadline = time.monotonic() + self.seconds(self.current()["id"])

    def goto(self, conn, delta):
        """Move to the previous/next screen of this client."""
        if self.mode != "client":
            return None
        self.index = (self.index + delta) % len(self.screens)
        self.apply(conn)
        return self.current()

    def toggle_hold(self, conn):
        """Enter: keep this screen on the panel, or let it rotate again."""
        if self.mode != "client":
            return None
        self.held = not self.held
        if not self.held:
            self.deadline = time.monotonic() + self.seconds(self.current()["id"])
        return self.held

    def tick(self, conn):
        """Advance if this screen has been up long enough."""
        if self.mode != "client" or self.held:
            return None
        if time.monotonic() < self.deadline:
            return None
        return self.goto(conn, 1)


class Notice:
    """The word LCDd used to write in the corner of the panel when a button changed it.

    0.5.9's own key handler prints `| Hold` when the rotation is stopped and `| Rotate`
    when it is let run on, `| Prev` and `| Next` when the arrows move the panel
    (input.c:155-170 hands the word to server_msg(), render.c:180-186 draws it over the
    end of the last row for four frames).  Those handlers only run for keys LCDd handles
    itself: in client mode this client reserves Enter and the two arrows, so nothing
    else writes that corner and the panel stops saying anything when a button is
    pressed.  This writes the same words back in the same place.
    """

    def __init__(self, seconds=NOTICE_SECONDS):
        self.seconds = max(0.0, seconds)
        self.text = None
        self.until = 0.0

    def write(self, word):
        """Show `word` in the corner for a while; 0 seconds means never."""
        if self.seconds <= 0.0:
            self.text = None
            self.until = 0.0
            return
        self.text = word
        self.until = time.monotonic() + self.seconds

    def word(self):
        """The word to draw right now, or None when the corner is empty."""
        if self.text is not None and time.monotonic() < self.until:
            return self.text
        return None

    def remaining(self):
        """Seconds until the word goes away, 0 when the corner is already empty.

        The main loop wakes up on this so the row the word covered comes back on time
        instead of waiting for the next refresh.
        """
        if self.text is None:
            return 0.0
        return max(0.0, self.until - time.monotonic())


def handle_key(conn, state, rotation, event, verbose, notice=None):
    """Act on one button press.  Returns True when the panel needs repainting now.

    Left/Right change screen, Up/Down move the row window on the screen that is
    showing (only a screen that has a window answers), Enter holds the screen or lets
    it rotate on.  A screen change is only a priority change, which LCDd applies on its
    own, and scrolling only moves rows inside one screen; both repaint immediately
    anyway because the corner word has to be drawn or cleared (see `Notice`).
    """
    parts = event.split()
    if len(parts) < 2 or parts[0] != "key":
        return False
    name = parts[1].strip().strip('"{}')
    action = KEY_ACTIONS.get(name.lower())
    if action is None:
        if verbose:
            log(f"key {name}: this client does not use that button")
        return False

    if action in ("prev", "next"):
        screen = rotation.goto(conn, -1 if action == "prev" else 1)
        if screen is None:
            log(f"{name}: rotation is left to LCDd (PVELCD_ROTATION=lcdproc), "
                "so this button is LCDd's own screen key")
            return False
        if notice is not None:
            notice.write("Prev" if action == "prev" else "Next")
        log(f"{name}: showing the {SCREENS[screen['id']][0]} screen")
        return True

    if action == "hold":
        held = rotation.toggle_hold(conn)
        if held is None:
            log(f"{name}: rotation is LCDd's own here (its ToggleRotateKey), "
                "not this client's")
            return False
        if notice is not None:
            notice.write("Hold" if held else "Rotate")
        log(f"{name}: screen held on the panel" if held else f"{name}: screens rotate again")
        return True

    delta = -1 if action == "up" else 1
    if rotation.mode != "client":
        # Without a pinned screen this client cannot know which screen the panel is
        # showing, so every screen that has a window is moved together.
        moved = [sid for sid in list(state.pages) if state.scroll(sid, delta)]
        if not moved:
            log(f"{name}: nothing on this panel has more rows than fit on it")
            return False
        return True

    sid = rotation.current()["id"]
    if not state.scroll(sid, delta):
        log(f"{name}: the {SCREENS[sid][0]} screen shows everything it has, "
            "there is nothing to scroll")
        return False
    return True


class LcdConnection:
    def __init__(self, host, port, timeout=5):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.sock = None
        self.buffer = b""
        self.size = None          # (cols, rows) as reported by LCDd
        self.recent = deque(maxlen=10)   # last commands, for error reporting

    def connect(self):
        self.sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        self.buffer = b""
        self.send("hello")
        while True:
            line = self.read_line(2.0)
            if line is None:
                raise ConnectionError("LCDd sent no 'connect' banner")
            if line.startswith("connect"):
                self.size = self.parse_banner(line)
                return
            if VERBOSE:
                log(f"pre-connect line: {line}")

    @staticmethod
    def parse_banner(line):
        tokens = line.split()[1:]
        pairs = {}
        index = 0
        while index < len(tokens):
            if tokens[index] == "lcd":
                index += 1
                continue
            if index + 1 < len(tokens):
                pairs[tokens[index].lower()] = tokens[index + 1]
                index += 2
            else:
                index += 1
        try:
            return int(pairs["wid"]), int(pairs["hgt"])
        except (KeyError, ValueError):
            return None

    def send(self, command):
        self.recent.append(command)
        self.sock.sendall((command + "\n").encode("utf-8"))

    def read_line(self, timeout):
        while b"\n" not in self.buffer:
            readable, _, _ = select.select([self.sock], [], [], timeout)
            if not readable:
                return None
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ConnectionError("LCDd closed the connection")
            self.buffer += chunk
        line, _, self.buffer = self.buffer.partition(b"\n")
        return line.decode("utf-8", "replace").strip()

    def drain(self):
        """Read everything already waiting. Not reading these messages stalls LCDd."""
        events = []
        while True:
            line = self.read_line(0.0)
            if line is None:
                return events
            events.append(line)

    def close(self):
        if self.sock is not None:
            try:
                self.sock.close()
            finally:
                self.sock = None


def register_screens(conn, screen_ids, cols, rows):
    conn.send(f"client_set -name {quote(CLIENT_NAME)}")
    keys = reservable_keys()
    if keys:
        # Shared, not exclusive: 0.5.9's menu holds Up/Down/Left/Right/Enter shared
        # for itself, so an exclusive reservation of any of them is refused outright.
        # Shared means they reach this client while one of its screens is showing, and
        # reach the menu while the menu is showing - the panel can still open it.
        conn.send("client_add_key -shared " + " ".join(keys))
    screens = []
    for sid in screen_ids:
        name, _ = SCREENS[sid]
        conn.send(f"screen_add {sid}")
        conn.send(
            f"screen_set {sid} -name {quote(name)} -priority info "
            f"-heartbeat off -duration {DURATIONS.get(sid, 24)}"
        )
        for row in range(rows):
            conn.send(f"widget_add {sid} r{row} string")
        screens.append({"id": sid, "last": [None] * rows, "priority": "info"})
    for event in conn.drain():
        if event.startswith("huh?"):
            log(f"registration rejected: {event}   commands just before: {'  '.join(conn.recent)}")
    if keys:
        log("buttons this client handles: " + ", ".join(keys))
    return screens


def paint(conn, screen, lines):
    """Write every row: rows the builder did not fill are cleared, not left stale."""
    for index in range(len(screen["last"])):
        text = lines[index] if index < len(lines) else ""
        if screen["last"][index] == text:
            continue
        conn.send(f"widget_set {screen['id']} r{index} 1 {index + 1} {quote(text)}")
        screen["last"][index] = text


def handle_events(conn, verbose, on_key=None):
    """Read everything LCDd has sent.  Returns True when a key handler asked for a repaint.

    `key <name>` lines are the interesting ones: the buttons, arriving with the name
    the KeyMap_ line gave them.  Not reading them is not harmless - a server whose
    output queue fills up stops accepting commands - so they are drained here even
    when nothing wants them.
    """
    repaint = False
    for event in conn.drain():
        if event.startswith("huh?"):
            log(f"rejected by LCDd: {event}   commands just before: {'  '.join(conn.recent)}")
        elif event.startswith("key"):
            if on_key is not None and on_key(event):
                repaint = True
            elif verbose:
                log(f"key event: {event}   <- use this to learn your button letters")
        elif verbose:
            log(f"<- {event}")
    return repaint


def sleep_and_drain(conn, seconds, on_key=None):
    """Wait between refreshes, reading as we go.  True when a button wants a repaint now."""
    deadline = time.monotonic() + seconds
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        if handle_events(conn, VERBOSE, on_key):
            return True
        time.sleep(min(0.25, remaining))


def run_forever(args):
    state = State(socket.gethostname())
    screen_ids = [sid for sid in SCREENS if sid in args.screens]
    if not screen_ids:
        screen_ids = list(SCREENS)

    while True:
        conn = LcdConnection(args.host, args.port)
        try:
            conn.connect()
            cols, rows = conn.size or (20, 4)
            log(f"connected to LCDd at {args.host}:{args.port}, display is {cols}x{rows}")
            if args.size and args.size != (cols, rows):
                cols, rows = args.size
                log(f"drawing for {cols}x{rows} because --size/PVELCD_SIZE overrides what "
                    f"LCDd reported; the right-hand columns of the panel will stay blank")
            elif (cols, rows) != (20, 4) and not args.size:
                log(f"WARNING: LCDd reports {cols}x{rows}, not 20x4. Count the columns on the "
                    "panel (install.sh measure); if the panel is narrower than LCDd thinks, "
                    "install.sh size <WxH> fixes the [MtxOrb] Size setting.")
            screens = register_screens(conn, screen_ids, cols, rows)
            log(f"registered {len(screens)} screen(s): {', '.join(screen_ids)}")
            rotation = Rotation(screens)
            if rotation.mode == "client":
                rotation.apply(conn)
                log(f"screen rotation is driven by this client: it starts on the "
                    f"{SCREENS[rotation.current()['id']][0]} screen now and moves on "
                    f"its own, {int(rotation.seconds(rotation.current()['id']))}s per "
                    "screen; Left/Right change screen, Up/Down scroll a list, "
                    "Enter holds the screen or lets it rotate again")
            else:
                log("screen rotation is left to LCDd: [server] AutoRotate=yes and the "
                    "-duration values above decide what the panel shows and for how long")

            notice = Notice()

            def on_key(event):
                return handle_key(conn, state, rotation, event, VERBOSE, notice)

            while True:
                rotation.tick(conn)
                # The word LCDd used to put in the corner for its own keys: only the
                # screen actually on the panel gets it, and only while it is live.
                word = notice.word() if rotation.mode == "client" else None
                showing = rotation.current()["id"] if rotation.mode == "client" else None
                for screen in screens:
                    builder = SCREENS[screen["id"]][1]
                    try:
                        lines = builder(state, cols, rows)
                    except Exception:
                        lines = [fit(f"screen {screen['id']} failed", cols)] * rows
                        log("screen " + screen["id"] + " failed:\n" + traceback.format_exc())
                    lines = (list(lines[:rows]) + [""] * rows)[:rows]
                    if word and screen["id"] == showing:
                        lines[rows - 1] = corner(lines[rows - 1], word, cols)
                    paint(conn, screen, lines)
                conn.send("noop")
                seconds = args.interval
                left = notice.remaining()
                if 0.0 < left < seconds:
                    seconds = max(0.25, left)   # wake on time to give the row back
                if sleep_and_drain(conn, seconds, on_key):
                    continue        # a button moved something: repaint without waiting
        except KeyboardInterrupt:
            raise
        except SystemExit:
            raise
        except Exception as exc:
            log(f"link problem: {exc} - retrying in {RECONNECT_DELAY}s")
        finally:
            conn.close()
        time.sleep(RECONNECT_DELAY)


def run_probe(args):
    """Paint a ruler on the real panel so the physical size can be counted by eye."""
    conn = LcdConnection(args.host, args.port)
    conn.connect()
    cols, rows = conn.size or (20, 4)
    log(f"LCDd reports the display as {cols}x{rows}")

    conn.send(f"client_set -name {quote(CLIENT_NAME + '-measure')}")
    conn.send("screen_add ruler")
    conn.send(f"screen_set ruler -name {quote('measure')} -priority info "
              "-heartbeat off -duration 160")
    screen = {"id": "ruler", "last": [None] * rows}
    for index in range(rows):
        conn.send(f"widget_add ruler r{index} string")
    for event in conn.drain():
        if event.startswith("huh?"):
            log(f"probe rejected: {event}   commands just before: {'  '.join(conn.recent)}")

    lines = measure_rows(cols, rows)
    log("the panel is now showing a ruler. Count what you can actually see:")
    log("  digit row: the last digit is the last column (17 columns ends in 7)")
    log("  letter row: A is column 1, so a last letter of Q means 17 columns")
    log("  plus row: '+' marks columns 1, 10, 20 - a missing '+' on the right means")
    log(f"          the panel is narrower than the {cols} columns LCDd is writing")
    log(f"then press Enter here (or wait {int(args.hold)}s) to finish")

    deadline = time.monotonic() + args.hold
    while time.monotonic() < deadline:
        paint(conn, screen, lines)
        conn.send("noop")
        handle_events(conn, VERBOSE)
        if sys.stdin.isatty():
            readable, _, _ = select.select([sys.stdin], [], [], 1.5)
            if readable:
                sys.stdin.readline()
                break
            continue
        time.sleep(1.5)
    conn.close()
    log("probe finished; the panel goes back to the normal screens when the client restarts")


def run_test(args):
    state = State(socket.gethostname())
    cols, rows = args.size or (20, 4)
    cpu_fraction(state)                      # first sample primes the CPU baseline
    time.sleep(1.0)
    for sid in [s for s in SCREENS if s in args.screens] or list(SCREENS):
        name, builder = SCREENS[sid]
        try:
            lines = builder(state, cols, rows)
        except Exception:
            lines = ["screen build failed"]
            log("screen " + sid + " failed:\n" + traceback.format_exc())
        print(f"+{'-' * cols}+  {name} (duration {DURATIONS[sid] / 8:.0f}s)")
        for line in lines:
            print(f"|{line[:cols].ljust(cols)}|")
        print()


def parse_screens(value):
    wanted = [item.strip().lower() for item in value.split(",") if item.strip()]
    unknown = [item for item in wanted if item not in SCREENS]
    if unknown:
        raise SystemExit(f"unknown screen(s): {', '.join(unknown)}; "
                         f"valid: {', '.join(SCREENS)}")
    return wanted


def parse_size_arg(value):
    size = parse_size(value)
    if size is None:
        raise SystemExit(f"bad --size {value!r}; expected something like 20x4 or 17x4")
    return size


def main():
    global VERBOSE
    parser = argparse.ArgumentParser(description="Proxmox status screens on an LCDproc display")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--interval", type=float, default=DEFAULT_REFRESH)
    parser.add_argument("--screens", default=",".join(SCREENS), type=parse_screens)
    parser.add_argument("--verbose", action="store_true", default=VERBOSE)
    parser.add_argument("--size", default=parse_size(os.environ.get("PVELCD_SIZE")),
                        type=parse_size_arg,
                        help="draw for this size instead of the size LCDd reports")
    parser.add_argument("--hold", type=float, default=120.0,
                        help="seconds to keep the ruler on the panel (--probe)")
    parser.add_argument("--test", action="store_true",
                        help="print the screens to stdout instead of talking to LCDd")
    parser.add_argument("--probe", action="store_true",
                        help="paint a ruler on the panel so you can count its real columns")
    args = parser.parse_args()

    VERBOSE = args.verbose

    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    signal.signal(signal.SIGINT, lambda *_: sys.exit(0))

    if args.probe:
        run_probe(args)
        return
    if args.test:
        run_test(args)
        return
    log(f"starting for node {socket.gethostname()}")
    run_forever(args)


if __name__ == "__main__":
    main()
