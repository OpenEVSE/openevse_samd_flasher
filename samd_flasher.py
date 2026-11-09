#!/usr/bin/env python3
"""
SAMD Bootloader + Firmware Flasher
==================================
Flashes a bootloader and application firmware onto Microchip/Atmel ATSAMD
(SAMD09/10/11/20/21, SAMR21, SAML21/22, SAMC20/21, SAMD51, SAME51/53/54)
boards with an Atmel-ICE (or any Atmel EDBG / CMSIS-DAP) programmer over SWD.

Back end: OpenOCD (xPack build, downloaded automatically into ./tools/).
Front end: Tkinter GUI, or a command line mode (--cli) for headless use.

Flash sequence (full flash):
    1. Chip erase (also clears the security bit on a locked chip)
    2. Clear BOOTPROT (bootloader protection fuse) and reset
    3. Write + verify bootloader at 0x00000000
    4. Write + verify firmware at the application offset (e.g. 0x2000)
    5. Set BOOTPROT to protect the bootloader
    6. Reset and run

Board settings come from profiles.json next to this script. The OpenEVSE NXT
profile can download Stable or Development firmware from
https://github.com/OpenEVSE/open_evse/releases.
"""

import argparse
import hashlib
import json
import os
import platform
import queue
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import urllib.error
import urllib.request
import zipfile

APP_NAME = "SAMD Flasher"
APP_VERSION = "1.0.0"

# ---------------------------------------------------------------------------
# Programmer / tool configuration
# ---------------------------------------------------------------------------
# Atmel / Microchip CMSIS-DAP debuggers OpenOCD will accept (VID, PID, name).
PROBES = [
    ("03eb", "2141", "Atmel-ICE"),
    ("03eb", "2111", "EDBG (Xplained boards)"),
    ("03eb", "2140", "JTAGICE3"),
    ("03eb", "2145", "mEDBG"),
    ("03eb", "2175", "nEDBG"),
]

OPENOCD_VERSION = "0.12.0-7"
OPENOCD_BASE = ("https://github.com/xpack-dev-tools/openocd-xpack/releases/"
                "download/v{v}/xpack-openocd-{v}-{plat}")
OPENOCD_ASSETS = {
    ("Windows", "x64"): "win32-x64.zip",
    ("Linux", "x64"): "linux-x64.tar.gz",
    ("Linux", "arm64"): "linux-arm64.tar.gz",
    ("Darwin", "x64"): "darwin-x64.tar.gz",
    ("Darwin", "arm64"): "darwin-arm64.tar.gz",
}

UDEV_RULE_PATH = "/etc/udev/rules.d/99-atmel-ice.rules"
GH_API = "https://api.github.com/repos/{repo}/releases"
USER_AGENT = "SAMD-Flasher/" + APP_VERSION

LINKS = {
    "Atmel-ICE product page":
        "https://www.microchip.com/en-us/development-tool/atatmel-ice",
    "Atmel-ICE user guide (pinouts)":
        "https://ww1.microchip.com/downloads/en/DeviceDoc/Atmel-ICE_UserGuide.pdf",
    "Microchip Studio (Windows drivers + GUI programmer)":
        "https://www.microchip.com/en-us/tools-resources/develop/microchip-studio",
    "MPLAB X IPE (alternative programmer, all OSes)":
        "https://www.microchip.com/en-us/tools-resources/production/mplab-integrated-programming-environment",
    "xPack OpenOCD (flashing back end)":
        "https://github.com/xpack-dev-tools/openocd-xpack/releases",
    "OpenOCD documentation":
        "https://openocd.org/doc/html/index.html",
}

# OpenOCD target config + flash driver command prefix for each chip family.
FAMILIES = {
    # family     : (target cfg,          driver prefix, default BL size, valid sizes)
    "samd09":  ("target/at91samdXX.cfg", "at91samd", 4096,  "pow2"),
    "samd10":  ("target/at91samdXX.cfg", "at91samd", 4096,  "pow2"),
    "samd11":  ("target/at91samdXX.cfg", "at91samd", 4096,  "pow2"),
    "samd20":  ("target/at91samdXX.cfg", "at91samd", 8192,  "pow2"),
    "samd21":  ("target/at91samdXX.cfg", "at91samd", 8192,  "pow2"),
    "samda1":  ("target/at91samdXX.cfg", "at91samd", 8192,  "pow2"),
    "samr21":  ("target/at91samdXX.cfg", "at91samd", 8192,  "pow2"),
    "saml21":  ("target/at91samdXX.cfg", "at91samd", 8192,  "pow2"),
    "saml22":  ("target/at91samdXX.cfg", "at91samd", 8192,  "pow2"),
    "samc20":  ("target/at91samdXX.cfg", "at91samd", 8192,  "pow2"),
    "samc21":  ("target/at91samdXX.cfg", "at91samd", 8192,  "pow2"),
    "samd51":  ("target/atsame5x.cfg",   "atsame5",  16384, "8k"),
    "same51":  ("target/atsame5x.cfg",   "atsame5",  16384, "8k"),
    "same53":  ("target/atsame5x.cfg",   "atsame5",  16384, "8k"),
    "same54":  ("target/atsame5x.cfg",   "atsame5",  16384, "8k"),
}

DEFAULT_PROFILES = {
    "boards": [
        {"name": "OpenEVSE NXT - D9.4.1 bootloader (ATSAMD21G18A)",
         "family": "samd21", "bootloader_size": 8192, "app_offset": "0x2000",
         "bootloader": "", "firmware": "",
         "release_repo": "OpenEVSE/open_evse",
         "firmware_asset": r"^samd\.bin$",
         "bootloader_asset": r"^bootloader-OpenEVSE_NXT.*\.bin$"},
    ]
}

CHANNELS = ("stable", "development")

FW_EXTS = (".bin", ".hex", ".elf")


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

def app_dir():
    """Folder holding profiles.json, tools/, drivers/ (works when frozen)."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def tools_dir():
    return os.path.join(app_dir(), "tools")


def resolve(path):
    """Resolve a profile-relative path against the app folder."""
    if not path:
        return ""
    path = os.path.expanduser(path)
    return path if os.path.isabs(path) else os.path.join(app_dir(), path)


def first_file_in(folder):
    """First firmware-looking file in a folder (used when a profile leaves
    the file blank: drop your file into bootloader/ or firmware/)."""
    folder = resolve(folder)
    if not os.path.isdir(folder):
        return ""
    for ext in FW_EXTS:
        for name in sorted(os.listdir(folder)):
            if name.lower().endswith(ext):
                return os.path.join(folder, name)
    return ""


# ---------------------------------------------------------------------------
# Profiles
# ---------------------------------------------------------------------------

def load_profiles():
    p = os.path.join(app_dir(), "profiles.json")
    if not os.path.isfile(p):
        return DEFAULT_PROFILES["boards"]
    with open(p, "r", encoding="utf-8") as f:
        data = json.load(f)
    boards = data.get("boards", [])
    for b in boards:
        fam = b.get("family", "").lower()
        if fam not in FAMILIES:
            raise ValueError("Profile '%s': unknown family '%s'. Valid: %s"
                             % (b.get("name"), fam, ", ".join(FAMILIES)))
        b["family"] = fam
        b.setdefault("bootloader_size", FAMILIES[fam][2])
        b.setdefault("app_offset", hex(b["bootloader_size"]))
    return boards or DEFAULT_PROFILES["boards"]


def parse_int(v):
    if isinstance(v, int):
        return v
    return int(str(v).strip(), 0)


def validate_bootloader_size(family, size):
    kind = FAMILIES[family][3]
    if size == 0:
        return None
    if kind == "pow2":
        # SAMD2x/1x: 2^n * page size (64 B) from 512 B to 32 KB.
        if size in [512 << i for i in range(7)]:
            return None
        return "Bootloader size for %s must be 0 or 512..32768 (power of 2)" % family
    if size % 8192 == 0 and size <= 15 * 8192:
        return None
    return "Bootloader size for %s must be 0 or a multiple of 8192" % family


# ---------------------------------------------------------------------------
# Firmware file helpers
# ---------------------------------------------------------------------------

def hex_address_range(path):
    """Return (min_addr, max_addr) of an Intel HEX file, or None."""
    lo, hi, base = None, None, 0
    try:
        with open(path, "r", encoding="ascii", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line.startswith(":") or len(line) < 11:
                    continue
                n = int(line[1:3], 16)
                addr = int(line[3:7], 16)
                rtype = int(line[7:9], 16)
                if rtype == 0x00 and n:
                    a = base + addr
                    lo = a if lo is None else min(lo, a)
                    hi = a + n if hi is None else max(hi, a + n)
                elif rtype == 0x02:
                    base = int(line[9:13], 16) << 4
                elif rtype == 0x04:
                    base = int(line[9:13], 16) << 16
    except (OSError, ValueError):
        return None
    return (lo, hi) if lo is not None else None


def check_images(bootloader, firmware, bl_size, app_offset):
    """Sanity-check image placement. Returns list of warning strings."""
    warnings = []
    if bootloader:
        if bootloader.lower().endswith(".bin"):
            sz = os.path.getsize(bootloader)
            if bl_size and sz > bl_size:
                warnings.append("Bootloader is %d bytes, larger than the %d-byte "
                                "bootloader region." % (sz, bl_size))
        elif bootloader.lower().endswith(".hex"):
            r = hex_address_range(bootloader)
            if r and r[0] != 0:
                warnings.append("Bootloader HEX starts at 0x%X, expected 0x0." % r[0])
    if firmware and firmware.lower().endswith(".hex"):
        r = hex_address_range(firmware)
        if r and bootloader and r[0] < app_offset:
            warnings.append("Firmware HEX starts at 0x%X, which overlaps the "
                            "bootloader (app offset 0x%X). Use a firmware built "
                            "for the bootloader." % (r[0], app_offset))
    return warnings


def tcl_path(p):
    """Path quoted for an OpenOCD Tcl script."""
    p = os.path.abspath(p).replace("\\", "/")
    if any(c in p for c in "{}"):
        raise ValueError("File path may not contain { or }: " + p)
    return "{" + p + "}"


# ---------------------------------------------------------------------------
# OpenOCD script generation
# ---------------------------------------------------------------------------

def probe_lines(serial=None):
    pairs = " ".join("0x%s 0x%s" % (v, p) for v, p, _ in PROBES)
    lines = ["adapter driver cmsis-dap", "cmsis_dap_vid_pid " + pairs]
    if serial:
        lines.append("adapter serial " + serial)
    return lines


def build_flash_script(family, bootloader=None, firmware=None,
                       bl_size=8192, app_offset=0x2000,
                       erase=True, protect=True, run=True,
                       speed_khz=400, serial=None):
    cfg, drv, _, _ = FAMILIES[family]
    write_bl = bool(bootloader)
    s = probe_lines(serial)
    s += ["source [find %s]" % cfg,
          "adapter speed %d" % speed_khz,
          "init"]
    if erase:
        s += ['echo "STEP: Chip erase"',
              "%s chip-erase" % drv]
    s += ["reset halt"]
    if write_bl or erase:
        s += ['echo "STEP: Clearing bootloader protection (BOOTPROT)"',
              "%s bootloader 0" % drv,
              "reset halt"]
    if write_bl:
        addr = "" if bootloader.lower().endswith((".hex", ".elf")) else " 0x0"
        s += ['echo "STEP: Writing bootloader"',
              "flash write_image erase %s%s" % (tcl_path(bootloader), addr),
              'echo "STEP: Verifying bootloader"',
              "verify_image %s%s" % (tcl_path(bootloader), addr)]
    if firmware:
        addr = "" if firmware.lower().endswith((".hex", ".elf")) else " 0x%X" % app_offset
        s += ['echo "STEP: Writing firmware"',
              "flash write_image erase %s%s" % (tcl_path(firmware), addr),
              'echo "STEP: Verifying firmware"',
              "verify_image %s%s" % (tcl_path(firmware), addr)]
    if protect and bl_size and (write_bl or erase):
        s += ['echo "STEP: Protecting bootloader (BOOTPROT = %d bytes)"' % bl_size,
              "%s bootloader %d" % (drv, bl_size)]
    if run:
        s += ['echo "STEP: Reset and run"', "reset run"]
    s += ['echo "DONE: Flash complete"', "shutdown"]
    return "\n".join(s) + "\n"


def build_info_script(family, speed_khz=400, serial=None):
    cfg, drv, _, _ = FAMILIES[family]
    s = probe_lines(serial)
    s += ["source [find %s]" % cfg,
          "adapter speed %d" % speed_khz,
          "init",
          "reset halt",
          "flash probe 0",
          "flash info 0",
          'echo "Bootloader protection:"',
          "%s bootloader" % drv,
          "reset run",
          'echo "DONE: Info complete"',
          "shutdown"]
    return "\n".join(s) + "\n"


# ---------------------------------------------------------------------------
# OpenOCD discovery / download
# ---------------------------------------------------------------------------

def host_key():
    system = platform.system()
    m = platform.machine().lower()
    arch = "arm64" if m in ("arm64", "aarch64") else "x64" if m in ("x86_64", "amd64") else m
    return system, arch


def find_openocd():
    exe = "openocd.exe" if platform.system() == "Windows" else "openocd"
    td = tools_dir()
    if os.path.isdir(td):
        for root, _dirs, files in os.walk(td):
            if exe in files and os.path.basename(root) == "bin":
                return os.path.join(root, exe)
    return shutil.which("openocd")


def openocd_version(path):
    try:
        r = subprocess.run([path, "--version"], capture_output=True, text=True, timeout=10)
        out = (r.stdout + r.stderr).strip().splitlines()
        return out[0] if out else "unknown"
    except Exception as e:
        return "error: %s" % e


def _download(url, dest, log):
    log("Downloading %s" % url)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=60) as r, open(dest, "wb") as f:
        total = int(r.headers.get("Content-Length") or 0)
        done, last = 0, -10
        while True:
            chunk = r.read(65536)
            if not chunk:
                break
            f.write(chunk)
            done += len(chunk)
            if total:
                pct = done * 100 // total
                if pct >= last + 10:
                    log("  %d%%" % pct)
                    last = pct
    return dest


def _safe_extract(archive, dest):
    dest = os.path.abspath(dest)

    def check(name):
        target = os.path.abspath(os.path.join(dest, name))
        if not (target == dest or target.startswith(dest + os.sep)):
            raise RuntimeError("Unsafe path in archive: " + name)

    if archive.endswith(".zip"):
        with zipfile.ZipFile(archive) as z:
            for n in z.namelist():
                check(n)
            z.extractall(dest)
    else:
        with tarfile.open(archive, "r:gz") as t:
            for m in t.getmembers():
                check(m.name)
                if m.islnk() or m.issym():
                    check(os.path.join(os.path.dirname(m.name), m.linkname))
            try:
                t.extractall(dest, filter="tar")
            except TypeError:  # Python < 3.12
                t.extractall(dest)


def download_openocd(log=print):
    key = host_key()
    asset = OPENOCD_ASSETS.get(key)
    if not asset:
        raise RuntimeError("No prebuilt OpenOCD for %s/%s. Install OpenOCD with "
                           "your package manager (e.g. 'sudo apt install openocd')."
                           % key)
    url = OPENOCD_BASE.format(v=OPENOCD_VERSION, plat=asset)
    os.makedirs(tools_dir(), exist_ok=True)
    tmp = tempfile.mkdtemp(prefix="samdflasher_")
    archive = _download(url, os.path.join(tmp, os.path.basename(url)), log)

    # Verify the published SHA-256 checksum.
    sha_file = _download(url + ".sha", archive + ".sha", log)
    with open(sha_file, "r") as f:
        expected = f.read().split()[0].lower()
    h = hashlib.sha256()
    with open(archive, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    if h.hexdigest() != expected:
        raise RuntimeError("Checksum mismatch for OpenOCD download - aborting.")
    log("Checksum OK.")

    dest = os.path.join(tools_dir(), "openocd")
    if os.path.isdir(dest):
        shutil.rmtree(dest)
    _safe_extract(archive, dest)
    shutil.rmtree(tmp, ignore_errors=True)
    exe = find_openocd()
    if not exe:
        raise RuntimeError("OpenOCD extracted but binary not found.")
    if platform.system() != "Windows":
        os.chmod(exe, 0o755)
    log("OpenOCD installed: %s" % exe)
    return exe


# ---------------------------------------------------------------------------
# Programmer detection + drivers
# ---------------------------------------------------------------------------

def detect_probes():
    """Return list of detected probe names, or None if detection unavailable."""
    system = platform.system()
    found = []
    try:
        if system == "Linux":
            out = subprocess.check_output(["lsusb"], text=True, stderr=subprocess.DEVNULL).lower()
            found = [n for v, p, n in PROBES if ("%s:%s" % (v, p)) in out]
        elif system == "Windows":
            ps = ("Get-PnpDevice -PresentOnly | Where-Object { $_.InstanceId -match "
                  "'VID_03EB&PID_(2141|2111|2140|2145|2175)' } | "
                  "Select-Object -ExpandProperty InstanceId")
            out = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                                 capture_output=True, text=True, timeout=15,
                                 creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout.upper()
            found = [n for v, p, n in PROBES if ("PID_" + p.upper()) in out]
        elif system == "Darwin":
            out = subprocess.check_output(["system_profiler", "SPUSBDataType"],
                                          text=True, stderr=subprocess.DEVNULL).lower()
            for v, p, n in PROBES:
                if re.search(r"product id:\s*0x%s.*?\n\s*vendor id:\s*0x%s" % (p, v), out):
                    found.append(n)
            if not found and "atmel-ice" in out:
                found.append("Atmel-ICE")
        else:
            return None
    except Exception:
        return None
    return sorted(set(found))


def udev_rule_installed():
    return os.path.isfile(UDEV_RULE_PATH)


def install_udev_rule(log=print):
    src = os.path.join(app_dir(), "drivers", "99-atmel-ice.rules")
    if not os.path.isfile(src):
        raise RuntimeError("Missing " + src)
    cmd = ["sh", "-c", 'cp "$0" "%s" && udevadm control --reload-rules && udevadm trigger'
           % UDEV_RULE_PATH, src]
    elevate = ["pkexec"] if shutil.which("pkexec") and os.environ.get("DISPLAY") else ["sudo"]
    if os.geteuid() == 0:
        elevate = []
    log("Running: %s" % " ".join(elevate + cmd))
    r = subprocess.run(elevate + cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError("udev rule install failed: " + (r.stderr or r.stdout).strip())
    log("udev rule installed. Unplug and replug the Atmel-ICE.")


def driver_advice():
    system = platform.system()
    if system == "Windows":
        return ("Windows: the Atmel-ICE is a USB HID (CMSIS-DAP) device, so it works "
                "with Windows' built-in driver - no install needed. If it shows up with "
                "a warning in Device Manager, install Microchip Studio (link below), "
                "which ships the official Atmel/Microchip USB drivers.")
    if system == "Linux":
        return ("Linux: no driver needed, but non-root users need a udev rule. Click "
                "'Install udev rule' (or copy drivers/99-atmel-ice.rules to "
                "/etc/udev/rules.d/) and replug the programmer.")
    return "macOS: no driver needed."


# ---------------------------------------------------------------------------
# GitHub releases (optional per profile)
# ---------------------------------------------------------------------------

def fetch_releases(repo):
    req = urllib.request.Request(GH_API.format(repo=repo) + "?per_page=50",
                                 headers={"User-Agent": USER_AGENT,
                                          "Accept": "application/vnd.github+json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        if e.code in (403, 429):
            raise RuntimeError("GitHub refused the request (rate limit: 60 requests/hour "
                               "without login). Wait a while and try again, or download "
                               "the files from https://github.com/%s/releases and use "
                               "Browse..." % repo)
        raise


def pick_asset(release, pattern):
    if not pattern:
        return None
    rx = re.compile(pattern, re.I)
    for a in release.get("assets", []):
        if rx.search(a["name"]):
            return a
    return None


def release_channel(release):
    """Pre-releases (OpenEVSE's rolling 'latest' Development Build) are the
    development channel; numbered releases are stable."""
    return "development" if release.get("prerelease") else "stable"


def channel_releases(releases, channel, profile):
    """Releases of a channel that actually contain this board's firmware,
    newest first."""
    out = [r for r in releases
           if not r.get("draft") and release_channel(r) == channel
           and pick_asset(r, profile.get("firmware_asset"))]
    out.sort(key=lambda r: r.get("published_at") or "", reverse=True)
    return out


def release_label(release):
    date = (release.get("published_at") or "")[:10]
    title = release.get("name") or release["tag_name"]
    if title == release["tag_name"]:
        return "%s  (%s)" % (title, date)
    return "%s - %s  (%s)" % (release["tag_name"], title, date)


def download_release_files(profile, release, log=print, bootloader=False):
    """Download the firmware (and optionally bootloader) of a release into
    firmware/downloads/<channel>-<tag>/. Returns {'firmware': path, ...}."""
    tag = re.sub(r"[^\w.-]", "_", release["tag_name"])
    out_dir = os.path.join(app_dir(), "firmware", "downloads",
                           "%s-%s" % (release_channel(release), tag))
    os.makedirs(out_dir, exist_ok=True)
    keys = ["firmware"] + (["bootloader"] if bootloader else [])
    result = {}
    for key in keys:
        a = pick_asset(release, profile.get(key + "_asset"))
        if not a:
            log("WARNING: release %s has no %s file matching '%s'"
                % (release["tag_name"], key, profile.get(key + "_asset")))
            continue
        dest = os.path.join(out_dir, os.path.basename(a["name"]))
        _download(a["browser_download_url"], dest, log)
        if a.get("size") and os.path.getsize(dest) != a["size"]:
            raise RuntimeError("Download of %s is incomplete." % a["name"])
        result[key] = dest
        log("  %s -> %s" % (key, dest))
    with open(os.path.join(out_dir, "release.txt"), "w", encoding="utf-8") as f:
        f.write("%s\n%s\n%s\n\n%s\n" % (release_label(release), release.get("html_url", ""),
                                        release.get("target_commitish", ""),
                                        release.get("body") or ""))
    return result


# ---------------------------------------------------------------------------
# Running OpenOCD
# ---------------------------------------------------------------------------

def run_openocd(openocd, script, log=print, line_cb=None):
    """Write script to a temp file, run OpenOCD, stream output. Returns rc."""
    fd, path = tempfile.mkstemp(prefix="samdflash_", suffix=".cfg")
    with os.fdopen(fd, "w") as f:
        f.write(script)
    try:
        cmd = [openocd, "-f", path]
        log("$ " + " ".join(cmd))
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, bufsize=1,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        for line in proc.stdout:
            line = line.rstrip()
            log(line)
            if line_cb:
                line_cb(line)
        return proc.wait()
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


TROUBLESHOOT = [
    (r"unable to find a matching CMSIS-DAP device|No CMSIS-DAP device found|unable to open",
     "Programmer not found. Check the USB cable; on Linux install the udev rule."),
    (r"Error connecting DP|Could not initialize the debug port|DAP init failed",
     "Programmer found but no response from the chip. Check: cable is in the "
     "Atmel-ICE 'SAM' port, the target board is powered, VTref is connected to 3.3V, "
     "SWDIO/SWCLK/GND/RESET wiring."),
    (r"Invalid bootloader size",
     "The bootloader size in the profile is not valid for this chip family."),
    (r"verify|checksum mismatch|contents differ",
     "Verify failed. Try a lower adapter speed (e.g. 100 kHz) and a shorter cable."),
    (r"locked|security",
     "The chip may be secured. Enable 'Chip erase' to unlock it."),
]


def diagnose(log_text):
    hints = []
    for rx, hint in TROUBLESHOOT:
        if re.search(rx, log_text, re.I) and hint not in hints:
            hints.append(hint)
    return hints


# ---------------------------------------------------------------------------
# Command line mode
# ---------------------------------------------------------------------------

def cli(argv):
    ap = argparse.ArgumentParser(description="%s %s (CLI mode)" % (APP_NAME, APP_VERSION))
    ap.add_argument("--cli", action="store_true", help="run without GUI")
    ap.add_argument("--list-profiles", action="store_true")
    ap.add_argument("--profile", help="board profile name (or index) from profiles.json")
    ap.add_argument("--family", choices=sorted(FAMILIES), help="override chip family")
    ap.add_argument("--bootloader", help="bootloader .bin/.hex/.elf ('none' to skip)")
    ap.add_argument("--firmware", help="firmware .bin/.hex/.elf ('none' to skip)")
    ap.add_argument("--bootloader-size", help="override BOOTPROT size in bytes")
    ap.add_argument("--app-offset", help="override firmware address for .bin, e.g. 0x2000")
    ap.add_argument("--no-erase", action="store_true", help="skip chip erase")
    ap.add_argument("--no-protect", action="store_true", help="do not set BOOTPROT")
    ap.add_argument("--speed", type=int, default=400, help="SWD clock kHz (default 400)")
    ap.add_argument("--serial", help="Atmel-ICE serial number (if several attached)")
    ap.add_argument("--info", action="store_true", help="read chip info only")
    ap.add_argument("--dry-run", action="store_true", help="print the OpenOCD script only")
    ap.add_argument("--download-openocd", action="store_true")
    ap.add_argument("--install-udev", action="store_true")
    ap.add_argument("--links", action="store_true", help="print driver/software links")
    ap.add_argument("--channel", choices=CHANNELS,
                    help="download firmware from GitHub releases: stable or development")
    ap.add_argument("--release", help="release tag within the channel (default: newest)")
    ap.add_argument("--release-bootloader", action="store_true",
                    help="also use the bootloader from the release instead of bootloader/")
    ap.add_argument("--list-releases", action="store_true",
                    help="list stable and development releases with firmware")
    a = ap.parse_args(argv)

    if a.links:
        print(driver_advice() + "\n")
        for k, v in LINKS.items():
            print("%-52s %s" % (k, v))
        return 0
    if a.install_udev:
        install_udev_rule()
        return 0
    if a.download_openocd:
        download_openocd()
        return 0

    profiles = load_profiles()
    if a.list_profiles:
        for i, p in enumerate(profiles):
            print("%d: %s  [%s, BL %s, app %s]" % (i, p["name"], p["family"],
                                                  p["bootloader_size"], p["app_offset"]))
        return 0

    prof = profiles[0]
    if a.profile is not None:
        match = [p for p in profiles if p["name"] == a.profile]
        if not match and a.profile.isdigit() and int(a.profile) < len(profiles):
            match = [profiles[int(a.profile)]]
        if not match:
            print("Unknown profile. Use --list-profiles.")
            return 2
        prof = match[0]

    family = a.family or prof["family"]
    bl_size = parse_int(a.bootloader_size if a.bootloader_size else prof["bootloader_size"])
    app_off = parse_int(a.app_offset if a.app_offset else prof["app_offset"])
    err = validate_bootloader_size(family, bl_size)
    if err:
        print("ERROR: " + err)
        return 2

    def pick(arg, key, folder):
        if arg and arg.lower() == "none":
            return None
        p = resolve(arg) if arg else (resolve(prof.get(key)) or first_file_in(folder))
        return p or None

    bl = pick(a.bootloader, "bootloader", "bootloader")
    fw = pick(a.firmware, "firmware", "firmware")

    if a.list_releases or a.channel:
        repo = prof.get("release_repo")
        if not repo:
            print("ERROR: profile has no release_repo.")
            return 2
        print("Fetching releases from github.com/%s ..." % repo)
        releases = fetch_releases(repo)
        if a.list_releases:
            for ch in CHANNELS:
                print("\n%s:" % ch.capitalize())
                for r in channel_releases(releases, ch, prof) or []:
                    print("  " + release_label(r))
            return 0
        rels = channel_releases(releases, a.channel, prof)
        if a.release:
            rels = [r for r in rels if r["tag_name"] == a.release]
        if not rels:
            print("ERROR: no %s release%s with firmware found."
                  % (a.channel, (" '%s'" % a.release) if a.release else ""))
            return 2
        print("Using %s release: %s" % (a.channel, release_label(rels[0])))
        files = download_release_files(prof, rels[0], bootloader=a.release_bootloader)
        if "firmware" not in files:
            return 2
        if not (a.firmware and a.firmware.lower() == "none"):
            fw = files["firmware"]
        if a.release_bootloader and "bootloader" in files:
            bl = files["bootloader"]

    print("Profile:    %s" % prof["name"])
    print("Family:     %s" % family)
    if a.info:
        script = build_info_script(family, a.speed, a.serial)
    else:
        for label, p in (("Bootloader", bl), ("Firmware", fw)):
            print("%-11s %s" % (label + ":", p or "(skip)"))
            if p and not os.path.isfile(p) and not a.dry_run:
                print("ERROR: file not found: " + p)
                return 2
        if not bl and not fw:
            print("ERROR: nothing to flash (no bootloader or firmware file).")
            return 2
        if not a.dry_run:
            for w in check_images(bl, fw, bl_size, app_off):
                print("WARNING: " + w)
        script = build_flash_script(family, bl, fw, bl_size, app_off,
                                    erase=not a.no_erase, protect=not a.no_protect,
                                    speed_khz=a.speed, serial=a.serial)
    if a.dry_run:
        print("\n--- OpenOCD script ---\n" + script)
        return 0

    ocd = find_openocd()
    if not ocd:
        print("OpenOCD not found - downloading...")
        ocd = download_openocd()
    buf = []
    rc = run_openocd(ocd, script, log=lambda s: (print(s), buf.append(s)))
    if rc != 0:
        print("\nFAILED (exit code %d)" % rc)
        for h in diagnose("\n".join(buf)):
            print("  hint: " + h)
        return rc
    print("\nSuccess.")
    return 0


# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------

def gui():
    import tkinter as tk
    from tkinter import ttk, scrolledtext, messagebox, filedialog
    import webbrowser

    BLUE, BG, GREEN, ORANGE, RED = "#1565C0", "#F5F5F5", "#2E7D32", "#E65100", "#C62828"
    CONS_BG, CONS_FG = "#1e1e1e", "#d4d4d4"

    class App(tk.Tk):
        def __init__(self):
            super().__init__()
            self.title("%s %s" % (APP_NAME, APP_VERSION))
            self.geometry("800x820")
            self.minsize(680, 620)
            self.configure(bg=BG)
            self.q = queue.Queue()
            self.busy = False
            self.releases = []
            try:
                self.profiles = load_profiles()
            except Exception as e:
                messagebox.showerror(APP_NAME, "profiles.json error:\n%s" % e)
                self.profiles = DEFAULT_PROFILES["boards"]

            self.v_profile = tk.StringVar()
            self.v_family = tk.StringVar()
            self.v_blsize = tk.StringVar()
            self.v_offset = tk.StringVar()
            self.v_bl = tk.StringVar()
            self.v_fw = tk.StringVar()
            self.v_speed = tk.StringVar(value="400")
            self.v_release = tk.StringVar()
            self.v_channel = tk.StringVar(value="stable")
            self.use_rel_bl = tk.BooleanVar(value=False)
            self.rel_map = {}
            self.do_erase = tk.BooleanVar(value=True)
            self.do_bl = tk.BooleanVar(value=True)
            self.do_fw = tk.BooleanVar(value=True)
            self.do_protect = tk.BooleanVar(value=True)

            self._style()
            self._header()
            self._programmer_panel()
            self._board_panel()
            self._files_panel()
            self._actions()
            self._console()
            self._statusbar()
            self.after(50, self._pump)
            self.after(150, self._startup)

        # --- layout -----------------------------------------------------
        def _style(self):
            s = ttk.Style(self)
            try:
                s.theme_use("clam")
            except Exception:
                pass
            for w in ("TFrame", "TLabel", "TLabelframe", "TCheckbutton"):
                s.configure(w, background=BG)
            s.configure("TLabelframe.Label", background=BG, font=("Helvetica", 9, "bold"))
            s.configure("Big.TButton", font=("Helvetica", 11, "bold"), padding=6)

        def _header(self):
            h = tk.Frame(self, bg=BLUE, pady=10)
            h.pack(fill="x")
            tk.Label(h, text="SAMD Flasher", font=("Helvetica", 18, "bold"),
                     bg=BLUE, fg="white").pack()
            tk.Label(h, text="Bootloader + firmware via Atmel-ICE (SWD / OpenOCD)",
                     font=("Helvetica", 9), bg=BLUE, fg="#BBDEFB").pack()

        def _programmer_panel(self):
            f = ttk.LabelFrame(self, text=" Programmer ", padding=8)
            f.pack(fill="x", padx=10, pady=(8, 4))
            self.lbl_probe = tk.Label(f, text="Checking...", bg=BG, anchor="w")
            self.lbl_probe.grid(row=0, column=0, sticky="w")
            self.lbl_ocd = tk.Label(f, text="", bg=BG, anchor="w")
            self.lbl_ocd.grid(row=1, column=0, sticky="w")
            b = ttk.Frame(f)
            b.grid(row=2, column=0, sticky="w", pady=(6, 0))
            f.columnconfigure(0, weight=1)
            ttk.Button(b, text="Refresh", command=self._startup).pack(side="left", padx=2)
            self.btn_ocd = ttk.Button(b, text="Download OpenOCD", command=self._get_openocd)
            self.btn_ocd.pack(side="left", padx=2)
            if platform.system() == "Linux":
                ttk.Button(b, text="Install udev rule",
                           command=self._udev).pack(side="left", padx=2)
            ttk.Button(b, text="Drivers & links", command=self._links).pack(side="left", padx=2)

        def _board_panel(self):
            f = ttk.LabelFrame(self, text=" Board ", padding=8)
            f.pack(fill="x", padx=10, pady=4)
            ttk.Label(f, text="Profile:").grid(row=0, column=0, sticky="w")
            cb = ttk.Combobox(f, textvariable=self.v_profile, state="readonly",
                              values=[p["name"] for p in self.profiles])
            cb.grid(row=0, column=1, columnspan=5, sticky="ew", padx=4)
            cb.bind("<<ComboboxSelected>>", lambda e: self._apply_profile())
            ttk.Label(f, text="Family:").grid(row=1, column=0, sticky="w", pady=(6, 0))
            ttk.Combobox(f, textvariable=self.v_family, values=sorted(FAMILIES),
                         state="readonly", width=9).grid(row=1, column=1, sticky="w", padx=4, pady=(6, 0))
            ttk.Label(f, text="Bootloader size:").grid(row=1, column=2, sticky="e", pady=(6, 0))
            ttk.Entry(f, textvariable=self.v_blsize, width=8).grid(row=1, column=3, sticky="w", padx=4, pady=(6, 0))
            ttk.Label(f, text="App offset:").grid(row=1, column=4, sticky="e", pady=(6, 0))
            ttk.Entry(f, textvariable=self.v_offset, width=8).grid(row=1, column=5, sticky="w", padx=4, pady=(6, 0))
            ttk.Label(f, text="SWD kHz:").grid(row=1, column=6, sticky="e", pady=(6, 0))
            ttk.Entry(f, textvariable=self.v_speed, width=6).grid(row=1, column=7, sticky="w", padx=4, pady=(6, 0))
            f.columnconfigure(1, weight=1)

        def _files_panel(self):
            f = ttk.LabelFrame(self, text=" Files ", padding=8)
            f.pack(fill="x", padx=10, pady=4)
            # GitHub release download: Stable / Development channel.
            rr = self.rel_row = ttk.Frame(f)
            ttk.Label(rr, text="Download:").grid(row=0, column=0, sticky="w")
            for i, ch in enumerate(CHANNELS):
                ttk.Radiobutton(rr, text=ch.capitalize(), value=ch, variable=self.v_channel,
                                command=self._fill_releases).grid(row=0, column=1 + i, padx=(4, 0))
            self.cb_rel = ttk.Combobox(rr, textvariable=self.v_release, state="readonly", width=38)
            self.cb_rel.grid(row=0, column=3, sticky="ew", padx=6)
            self.btn_dl = ttk.Button(rr, text="Download", command=self._dl_release)
            self.btn_dl.grid(row=0, column=4)
            ttk.Button(rr, text="↻", width=3,
                       command=lambda: self._bg(self._load_releases)).grid(row=0, column=5, padx=(4, 0))
            ttk.Checkbutton(rr, text="Also use the bootloader from this release "
                            "(instead of the D9.4.1 file in bootloader/)",
                            variable=self.use_rel_bl).grid(row=1, column=1, columnspan=5,
                                                           sticky="w", pady=(2, 0))
            rr.columnconfigure(3, weight=1)
            for r, (lbl, var, chk) in enumerate((("Bootloader:", self.v_bl, self.do_bl),
                                                 ("Firmware:", self.v_fw, self.do_fw)), start=1):
                ttk.Checkbutton(f, text=lbl, variable=chk).grid(row=r, column=0, sticky="w")
                ttk.Entry(f, textvariable=var).grid(row=r, column=1, sticky="ew", padx=4, pady=2)
                ttk.Button(f, text="Browse...",
                           command=lambda v=var: self._browse(v)).grid(row=r, column=2)
            f.columnconfigure(1, weight=1)
            o = ttk.Frame(f)
            o.grid(row=3, column=0, columnspan=3, sticky="w", pady=(6, 0))
            ttk.Checkbutton(o, text="Chip erase (unlocks secured chips)",
                            variable=self.do_erase).pack(side="left", padx=(0, 12))
            ttk.Checkbutton(o, text="Protect bootloader (set BOOTPROT)",
                            variable=self.do_protect).pack(side="left")
            self.files_frame = f

        def _actions(self):
            f = ttk.Frame(self)
            f.pack(fill="x", padx=10, pady=6)
            self.btn_flash = ttk.Button(f, text="Flash Bootloader + Firmware",
                                        style="Big.TButton", command=self._flash)
            self.btn_flash.pack(side="left", fill="x", expand=True)
            self.btn_info = ttk.Button(f, text="Read Chip Info", command=self._info)
            self.btn_info.pack(side="left", padx=(6, 0))
            ttk.Button(f, text="Show Script", command=self._show_script).pack(side="left", padx=(6, 0))
            self.pb = ttk.Progressbar(self, mode="determinate", maximum=100)
            self.pb.pack(fill="x", padx=10)

        def _console(self):
            self.con = scrolledtext.ScrolledText(self, bg=CONS_BG, fg=CONS_FG,
                                                 font=("Courier", 9), state="disabled",
                                                 relief="flat", height=14)
            self.con.pack(fill="both", expand=True, padx=10, pady=6)
            for tag, col in (("ok", "#81C784"), ("err", "#E57373"), ("step", "#64B5F6"),
                             ("warn", "#FFB74D")):
                self.con.tag_config(tag, foreground=col)

        def _statusbar(self):
            self.status = tk.Label(self, text="Ready", anchor="w", bg="#E0E0E0", padx=8)
            self.status.pack(fill="x", side="bottom")

        # --- logging (thread-safe via queue) ----------------------------
        def log(self, msg, tag=None):
            self.q.put(("log", msg, tag))

        def _pump(self):
            try:
                while True:
                    kind, *rest = self.q.get_nowait()
                    if kind == "log":
                        msg, tag = rest
                        if tag is None:
                            low = msg.lower()
                            tag = ("step" if msg.startswith("STEP:") else
                                   "ok" if msg.startswith("DONE:") or "success" in low else
                                   "err" if low.startswith("error") or "failed" in low else
                                   "warn" if low.startswith("warn") else None)
                        self.con.configure(state="normal")
                        self.con.insert("end", msg + "\n", tag or ())
                        self.con.see("end")
                        self.con.configure(state="disabled")
                    elif kind == "call":
                        rest[0]()
            except queue.Empty:
                pass
            self.after(50, self._pump)

        def ui(self, fn):
            self.q.put(("call", fn))

        # --- startup / status ------------------------------------------
        def _startup(self):
            if not self.v_profile.get() and self.profiles:
                self.v_profile.set(self.profiles[0]["name"])
                self._apply_profile()
            self._bg(self._check_status)

        def _check_status(self):
            probes = detect_probes()
            ocd = find_openocd()
            ver = openocd_version(ocd) if ocd else None

            def upd():
                if probes is None:
                    self.lbl_probe.config(text="Programmer: auto-detect unavailable "
                                          "(OpenOCD will still try)", fg=ORANGE)
                elif probes:
                    self.lbl_probe.config(text="Programmer: %s detected" % ", ".join(probes), fg=GREEN)
                else:
                    self.lbl_probe.config(text="Programmer: Atmel-ICE NOT detected - plug it in", fg=RED)
                if platform.system() == "Linux" and not udev_rule_installed():
                    self.lbl_probe.config(text=self.lbl_probe.cget("text") +
                                          "   (udev rule missing)", fg=ORANGE)
                if ocd:
                    self.lbl_ocd.config(text="OpenOCD: %s" % ver, fg=GREEN)
                else:
                    self.lbl_ocd.config(text="OpenOCD: not installed - click 'Download OpenOCD'", fg=ORANGE)
            self.ui(upd)

        def _apply_profile(self):
            p = self._profile()
            self.v_family.set(p["family"])
            self.v_blsize.set(str(p["bootloader_size"]))
            self.v_offset.set(str(p["app_offset"]))
            self.v_bl.set(resolve(p.get("bootloader")) or first_file_in("bootloader"))
            self.v_fw.set(resolve(p.get("firmware")) or first_file_in("firmware"))
            if p.get("release_repo"):
                self.rel_row.grid(row=0, column=0, columnspan=3, sticky="ew", pady=(0, 8))
                self._bg(self._load_releases)
            else:
                self.rel_row.grid_forget()

        def _profile(self):
            for p in self.profiles:
                if p["name"] == self.v_profile.get():
                    return p
            return self.profiles[0]

        # --- releases ---------------------------------------------------
        def _load_releases(self):
            repo = self._profile()["release_repo"]
            self.log("Fetching releases from github.com/%s ..." % repo)
            try:
                rels = fetch_releases(repo)
            except Exception as e:
                self.log("ERROR fetching releases: %s" % e)
                self.ui(self._fill_releases)
                return
            self.releases = rels
            prof = self._profile()
            counts = {ch: len(channel_releases(rels, ch, prof)) for ch in CHANNELS}
            self.log("Found %d stable and %d development build(s) with SAMD firmware."
                     % (counts["stable"], counts["development"]))
            self.ui(self._fill_releases)

        def _fill_releases(self):
            rels = channel_releases(self.releases, self.v_channel.get(), self._profile())
            self.rel_map = {release_label(r): r for r in rels}
            labels = list(self.rel_map)
            self.cb_rel["values"] = labels
            self.v_release.set(labels[0] if labels else
                               ("(no %s builds found)" % self.v_channel.get()
                                if self.releases else "(press ↻ to load releases)"))

        def _dl_release(self):
            rel = getattr(self, "rel_map", {}).get(self.v_release.get())
            if not rel:
                messagebox.showinfo(APP_NAME, "No release selected. Press ↻ to load "
                                    "the release list (needs internet).")
                return
            prof = self._profile()
            want_bl = self.use_rel_bl.get()

            def work():
                self.log("Downloading %s build %s ..."
                         % (release_channel(rel), rel["tag_name"]), "step")
                try:
                    files = download_release_files(prof, rel, self.log, bootloader=want_bl)
                except Exception as e:
                    self.log("ERROR: %s" % e)
                    return

                def upd():
                    if "bootloader" in files:
                        self.v_bl.set(files["bootloader"])
                    if "firmware" in files:
                        self.v_fw.set(files["firmware"])
                        self.do_fw.set(True)
                        self.log("Ready to flash %s firmware %s."
                                 % (release_channel(rel), rel["tag_name"]), "ok")
                self.ui(upd)
            self._bg(work)

        # --- buttons ----------------------------------------------------
        def _browse(self, var):
            p = filedialog.askopenfilename(
                initialdir=os.path.dirname(var.get()) or app_dir(),
                filetypes=[("Firmware", "*.bin *.hex *.elf"), ("All files", "*.*")])
            if p:
                var.set(p)

        def _links(self):
            w = tk.Toplevel(self)
            w.title("Drivers & software")
            w.configure(bg=BG, padx=14, pady=12)
            tk.Label(w, text=driver_advice(), wraplength=520, justify="left",
                     bg=BG).pack(anchor="w", pady=(0, 10))
            for name, url in LINKS.items():
                l = tk.Label(w, text=name, fg=BLUE, bg=BG, cursor="hand2",
                             font=("Helvetica", 10, "underline"))
                l.pack(anchor="w", pady=1)
                l.bind("<Button-1>", lambda e, u=url: webbrowser.open(u))
            tk.Label(w, text="\nWiring: Atmel-ICE 'SAM' port -> board SWD header\n"
                     "  VTref->3V3, GND->GND, SWDIO->SWDIO, SWCLK->SWCLK, nRESET->RESET\n"
                     "  The Atmel-ICE does NOT power the board - power it separately.",
                     justify="left", bg=BG, font=("Courier", 9)).pack(anchor="w")
            ttk.Button(w, text="Close", command=w.destroy).pack(pady=(10, 0))

        def _udev(self):
            def work():
                try:
                    install_udev_rule(self.log)
                except Exception as e:
                    self.log("ERROR: %s" % e)
                self._check_status()
            self._bg(work)

        def _get_openocd(self):
            def work():
                try:
                    download_openocd(self.log)
                except Exception as e:
                    self.log("ERROR: %s" % e)
                self._check_status()
            self._bg(work, lock=True)

        def _collect(self):
            fam = self.v_family.get()
            try:
                bl_size = parse_int(self.v_blsize.get())
                offset = parse_int(self.v_offset.get())
                speed = int(self.v_speed.get())
            except ValueError:
                raise ValueError("Bootloader size, app offset and SWD kHz must be numbers.")
            err = validate_bootloader_size(fam, bl_size)
            if err:
                raise ValueError(err)
            bl = self.v_bl.get().strip() if self.do_bl.get() else ""
            fw = self.v_fw.get().strip() if self.do_fw.get() else ""
            for p in (bl, fw):
                if p and not os.path.isfile(p):
                    raise ValueError("File not found:\n" + p)
            if not bl and not fw:
                raise ValueError("Select a bootloader and/or firmware file.")
            return fam, bl or None, fw or None, bl_size, offset, speed

        def _show_script(self):
            try:
                fam, bl, fw, size, off, speed = self._collect()
                self.log(build_flash_script(fam, bl, fw, size, off, self.do_erase.get(),
                                            self.do_protect.get(), True, speed))
            except ValueError as e:
                messagebox.showerror(APP_NAME, str(e))

        def _flash(self):
            try:
                fam, bl, fw, size, off, speed = self._collect()
            except ValueError as e:
                messagebox.showerror(APP_NAME, str(e))
                return
            warns = check_images(bl, fw, size, off)
            if warns and not messagebox.askyesno(APP_NAME, "\n\n".join(warns) + "\n\nFlash anyway?"):
                return
            if self.do_erase.get() and not messagebox.askyesno(
                    APP_NAME, "This will ERASE the entire chip and write:\n\n"
                    "Bootloader: %s\nFirmware: %s\n\nContinue?"
                    % (bl or "(skip)", fw or "(skip)")):
                return
            script = build_flash_script(fam, bl, fw, size, off, self.do_erase.get(),
                                        self.do_protect.get(), True, speed)
            self._run(script, "Flashing")

        def _info(self):
            try:
                speed = int(self.v_speed.get())
            except ValueError:
                speed = 400
            self._run(build_info_script(self.v_family.get(), speed), "Reading chip info")

        def _run(self, script, what):
            steps = max(1, script.count('echo "STEP:'))
            state = {"n": 0, "buf": []}

            def on_line(line):
                state["buf"].append(line)
                if line.startswith("STEP:"):
                    state["n"] += 1
                    pct = state["n"] * 100 // (steps + 1)
                    self.ui(lambda: (self.pb.config(value=pct),
                                     self.status.config(text=line[5:].strip())))

            def work():
                ocd = find_openocd()
                if not ocd:
                    self.log("OpenOCD not found - downloading...")
                    try:
                        ocd = download_openocd(self.log)
                    except Exception as e:
                        self.log("ERROR: %s" % e)
                        return
                self.log("--- %s ---" % what, "step")
                try:
                    rc = run_openocd(ocd, script, self.log, on_line)
                except Exception as e:
                    rc = -1
                    self.log("ERROR: %s" % e)
                if rc == 0:
                    self.log("Success! Unplug/replug the board.", "ok")
                    self.ui(lambda: (self.pb.config(value=100),
                                     self.status.config(text="Done", fg=GREEN)))
                else:
                    self.log("FAILED (OpenOCD exit code %s)" % rc, "err")
                    for h in diagnose("\n".join(state["buf"])):
                        self.log("Hint: " + h, "warn")
                    self.ui(lambda: self.status.config(text="Failed", fg=RED))
            self.pb.config(value=0)
            self.status.config(text=what + "...", fg="black")
            self._bg(work, lock=True)

        def _bg(self, fn, lock=False):
            if lock:
                if self.busy:
                    messagebox.showinfo(APP_NAME, "Busy - please wait.")
                    return
                self.busy = True
                for b in (self.btn_flash, self.btn_info, self.btn_ocd):
                    b.state(["disabled"])

            def wrapper():
                try:
                    fn()
                finally:
                    if lock:
                        def unlock():
                            self.busy = False
                            for b in (self.btn_flash, self.btn_info, self.btn_ocd):
                                b.state(["!disabled"])
                        self.ui(unlock)
            threading.Thread(target=wrapper, daemon=True).start()

    App().mainloop()


# ---------------------------------------------------------------------------

def main():
    argv = sys.argv[1:]
    if argv:
        return cli(argv)
    try:
        import tkinter  # noqa: F401
    except ImportError:
        print("Tkinter not available - running in CLI mode. Use --help for options.\n"
              "(Ubuntu/Debian: sudo apt install python3-tk for the GUI)\n")
        return cli(["--help"])
    gui()
    return 0


if __name__ == "__main__":
    sys.exit(main())
