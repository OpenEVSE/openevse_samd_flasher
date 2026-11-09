# SAMD Flasher: application description

## Purpose

SAMD Flasher puts a bootloader and OpenEVSE firmware onto **OpenEVSE NXT** controller boards. These boards use the **ATSAMD21G18A** microcontroller, and the tool flashes them through a **Microchip/Atmel Atmel-ICE** programmer over SWD.

It is the SAMD counterpart of [OpenEVSE AVR Flasher](https://github.com/OpenEVSE/openevse_avr_flasher), which does the same job for ATmega328P boards with a USBasp. Like that tool, it is a single window aimed at people who don't use the command line, and it ships as a self-contained download for Windows, macOS and Linux.

## What it does

| Feature | Details |
|---|---|
| Flash bootloader + firmware | Full sequence: chip erase, unlock, write and verify the bootloader, write and verify the firmware, protect the bootloader, run |
| Firmware download | Lists **Stable** (numbered releases) and **Development** (rolling `latest` pre-release) builds from [OpenEVSE/open_evse releases](https://github.com/OpenEVSE/open_evse/releases) and downloads `samd.bin` |
| Bootloader | Uses the **D9.4.1** bootloader in `bootloader/` by default. It can use the release's `bootloader-OpenEVSE_NXT-*.bin` instead |
| Firmware-only update | Untick *Bootloader* and *Chip erase* to keep the existing bootloader |
| Read Chip Info | Checks wiring and reports the chip, flash size and bootloader protection |
| Programmer detection | Detects the Atmel-ICE (and EDBG / mEDBG / nEDBG / JTAGICE3) on all three OSes |
| Drivers | Linux: one-click udev rule install. Windows/macOS: no driver needed. Links to Microchip's tools |
| Safety checks | Warns if the bootloader is too big for its 8 KB space, or if a `.hex` firmware would overwrite the bootloader. Asks before erasing |
| Troubleshooting | Reads OpenOCD's output and suggests fixes (unpowered board, wrong port, missing udev rule, verify failure, secured chip) |
| CLI mode | Every GUI action is also available from the command line, for scripting or production use |

## Board profile

| Setting | Value | Why |
|---|---|---|
| Chip family | `samd21` (ATSAMD21G18A, 256 KB flash, 32 KB RAM) | OpenEVSE NXT controller |
| Bootloader size | 8192 bytes (`0x0000`–`0x1FFF`) | UF2 SAMD21 bootloader size; set in the BOOTPROT fuse |
| Firmware address | `0x2000` | OpenEVSE's `samd` build uses PlatformIO board `zeroUSB`, linked at `0x2000` |
| Release files | firmware `^samd\.bin$`, bootloader `^bootloader-OpenEVSE_NXT.*\.bin$` | Names produced by open_evse's `release.yml` workflow |

All of this lives in `profiles.json`, so it can be changed without touching code.

## How it works

```
┌──────────────────────┐   GitHub REST API    ┌──────────────────────────┐
│  samd_flasher.py     │ ───────────────────▶ │ OpenEVSE/open_evse       │
│  Tkinter GUI / CLI   │   samd.bin download  │ releases (stable / dev)  │
│                      │                      └──────────────────────────┘
│  builds an OpenOCD   │
│  Tcl script          │   runs                ┌──────────┐  USB  ┌───────────┐  SWD  ┌──────────────┐
│                      │ ────────────────────▶ │ OpenOCD  │ ────▶ │ Atmel-ICE │ ────▶ │ ATSAMD21G18A │
└──────────────────────┘                       └──────────┘       └───────────┘       └──────────────┘
```

1. **OpenOCD back end.** The app doesn't talk to USB itself. It writes a short OpenOCD script and runs [xPack OpenOCD](https://github.com/xpack-dev-tools/openocd-xpack) 0.12.0-7. Release packages include it in `tools/openocd/`; when running from source, the app downloads it and checks its SHA-256 checksum first. If no bundled copy exists, it falls back to a system `openocd`.
2. **Programmer.** OpenOCD's `cmsis-dap` driver, limited to Atmel/Microchip VID `03eb` with PIDs 2141, 2111, 2140, 2145 and 2175. The Atmel-ICE is a USB HID device, so Windows and macOS need no driver.
3. **Flash script** for a full flash:
   ```tcl
   at91samd chip-erase                 ;# also clears the security bit
   reset halt
   at91samd bootloader 0               ;# clear BOOTPROT
   reset halt                          ;# fuse takes effect after reset
   flash write_image erase {bootloader.bin} 0x0
   verify_image {bootloader.bin} 0x0
   flash write_image erase {samd.bin} 0x2000
   verify_image {samd.bin} 0x2000
   at91samd bootloader 8192            ;# protect bootloader
   reset run
   ```
   Each step echoes a `STEP:` marker. The GUI uses these markers to drive the progress bar and status line.
4. **Release channels.** GitHub releases marked *pre-release* are **Development**; that's open_evse's rolling `latest` "Development Build". All others are **Stable**. Only releases that contain `samd.bin` are listed, so older AVR-only releases are left out. Downloads go to `firmware/downloads/<channel>-<tag>/`, together with a `release.txt` holding the release notes.
5. **Threading.** OpenOCD runs, downloads and USB detection happen on worker threads. Log lines and UI updates go back to the Tk main loop through a queue, so the window never freezes.

## Files

```
samd_flasher.py        application (single file, standard library only)
profiles.json          board profile
bootloader/            D9.4.1 bootloader file goes here
firmware/              local firmware; downloads in firmware/downloads/
drivers/               99-atmel-ice.rules (Linux) + driver links (README.md)
tools/                 OpenOCD (bundled in releases, else downloaded on first use)
run.bat / run.sh       launchers when running from source
build.bat / build.sh   local build of a self-contained zip
samd_flasher.spec      PyInstaller spec
.github/workflows/release.yml   CI builds for Windows, macOS, Linux
```

## Building and releasing

GitHub Actions (`.github/workflows/release.yml`) runs on three runners: `windows-latest`, `macos-latest` (Apple Silicon) and `ubuntu-22.04`. For each one it:

1. Sets up Python 3.12 and PyInstaller.
2. Runs a CLI self-test (`--list-profiles`, then a `--dry-run` flash).
3. Downloads and checksum-verifies OpenOCD for that platform, then runs `openocd --version`.
4. Builds the one-file `SAMD_Flasher` executable.
5. Zips the executable with `tools/`, `drivers/`, `bootloader/`, `firmware/`, `profiles.json`, `README.md` and `APP.md`.

Every push uploads `SAMD_Flasher-windows.zip`, `-macos.zip` and `-linux.zip` as workflow artifacts. To publish a release:

```bash
git tag v1.0.0
git push origin v1.0.0
```

A `v*.*.*` tag also creates a GitHub Release with the three zips attached, using the same mechanism as openevse_avr_flasher.

The D9.4.1 bootloader is only included in the zips if it is committed to `bootloader/`.

## Platform notes

- **Windows:** unzip and double-click `SAMD_Flasher.exe`. SmartScreen may warn because the exe is unsigned; click *More info → Run anyway*.
- **macOS:** the build is Apple Silicon only. Binaries are unsigned, so after unzipping run `xattr -dr com.apple.quarantine <folder>`, or right-click → *Open* the first time. A Terminal window opens next to the GUI and shows the log.
- **Linux:** run `./SAMD_Flasher`, then click **Install udev rule** once so the Atmel-ICE can be used without root.

## Testing status and limitations

- **Tested** in a Linux container: script generation for every option, Intel HEX parsing, image overlap warnings, error hints, release filtering and download (against mocked GitHub data), the GUI under a virtual display (with a stand-in OpenOCD), and the frozen PyInstaller Linux executable.
- **Not yet tested:** flashing a real board with a real Atmel-ICE, the live GitHub API, and the Windows and macOS builds (the first CI run will build those).
- macOS Intel and 32-bit ARM Linux packages are not built. On those, run from source with a system OpenOCD.
- Anonymous GitHub API access is limited to 60 requests per hour per IP address. If the release list fails, download `samd.bin` by hand and use **Browse...**.
