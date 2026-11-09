#!/usr/bin/env bash
# SAMD Flasher - macOS / Linux launcher.  Run:  bash run.sh   (extra args go to the app, e.g. --cli --help)
set -e
PYTHON=""
for c in python3 python; do
    if command -v "$c" &>/dev/null && [ "$("$c" -c 'import sys;print(sys.version_info.major)')" = "3" ]; then
        PYTHON="$c"; break
    fi
done
if [ -z "$PYTHON" ]; then
    echo "Python 3 not found."
    echo "  macOS:  brew install python python-tk   (or https://www.python.org)"
    echo "  Ubuntu: sudo apt install python3 python3-tk"
    echo "  Fedora: sudo dnf install python3 python3-tkinter"
    exit 1
fi
if [ $# -eq 0 ] && ! "$PYTHON" -c "import tkinter" 2>/dev/null; then
    echo "Tkinter missing (needed for the GUI). Install python3-tk, or use: bash run.sh --cli --help"
fi
if [ "$(uname)" = "Linux" ] && [ ! -f /etc/udev/rules.d/99-atmel-ice.rules ]; then
    echo "Note: Atmel-ICE udev rule not installed. Use the 'Install udev rule' button, or:"
    echo "  sudo cp drivers/99-atmel-ice.rules /etc/udev/rules.d/ && sudo udevadm control --reload-rules"
fi
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "$PYTHON" "$SCRIPT_DIR/samd_flasher.py" "$@"
