# Atmel-ICE drivers

| OS | What you need |
|----|---------------|
| **Windows 10/11** | Nothing. The Atmel-ICE is a USB HID (CMSIS-DAP) device and uses Windows' built-in HID driver. If Device Manager shows it with a warning, install **Microchip Studio** (below), which includes the official Atmel/Microchip USB drivers. |
| **Linux** | No driver. Install the udev rule in this folder so non-root users can open it: `sudo cp 99-atmel-ice.rules /etc/udev/rules.d/ && sudo udevadm control --reload-rules && sudo udevadm trigger`, then replug. Or click **Install udev rule** in the app. |
| **macOS** | Nothing. |

## Downloads / links

- Atmel-ICE product page: https://www.microchip.com/en-us/development-tool/atatmel-ice
- Atmel-ICE user guide (connector pinouts): https://ww1.microchip.com/downloads/en/DeviceDoc/Atmel-ICE_UserGuide.pdf
- Microchip Studio (Windows drivers, GUI programmer, Atmel-ICE firmware updates): https://www.microchip.com/en-us/tools-resources/develop/microchip-studio
- MPLAB X IPE (alternative GUI programmer, Windows/macOS/Linux): https://www.microchip.com/en-us/tools-resources/production/mplab-integrated-programming-environment
- xPack OpenOCD (the flashing back end this app uses, downloaded automatically into `tools/`): https://github.com/xpack-dev-tools/openocd-xpack/releases

## Firmware on the Atmel-ICE

If OpenOCD can't open the programmer even though it's plugged in, its firmware may be old. Plug it into Microchip Studio or MPLAB X once and accept the firmware upgrade.
