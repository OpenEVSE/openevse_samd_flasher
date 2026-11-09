#!/usr/bin/env bash
# Builds a self-contained release zip: SAMD_Flasher-<platform>.zip
# Contains the executable, OpenOCD, drivers, profiles and your bootloader/firmware files.
set -e
cd "$(dirname "${BASH_SOURCE[0]}")"
case "$(uname -s)" in Darwin) PLAT=macos;; Linux) PLAT=linux;; *) echo "Unsupported OS"; exit 1;; esac
PY=$(command -v python3 || command -v python)
"$PY" -m pip install --upgrade pyinstaller --quiet
rm -rf dist build release
[ -n "$(find tools -name openocd -path '*/bin/*' 2>/dev/null)" ] || "$PY" samd_flasher.py --download-openocd
"$PY" -m PyInstaller samd_flasher.spec
mkdir -p release
cp dist/SAMD_Flasher release/ && chmod +x release/SAMD_Flasher
cp -R tools drivers bootloader firmware profiles.json README.md APP.md release/
ZIP="SAMD_Flasher-${PLAT}.zip"
(cd release && zip -qry "../$ZIP" .)
echo "Done: $ZIP"
