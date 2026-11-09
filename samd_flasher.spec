# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for SAMD Flasher.  Build: pyinstaller samd_flasher.spec
# Produces a single executable. OpenOCD, drivers, profiles.json, bootloader/
# and firmware/ are shipped NEXT TO it (the app looks beside its executable).
import sys

a = Analysis(['samd_flasher.py'], pathex=[], binaries=[], datas=[],
             hiddenimports=[], hookspath=[], runtime_hooks=[],
             excludes=['test', 'unittest'], noarchive=False)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, a.binaries, a.datas, [],
          name='SAMD_Flasher', debug=False, strip=False,
          upx=False,
          # Windows: no console window behind the GUI.
          # macOS: one-file + windowed is deprecated in PyInstaller, so keep a
          # console (Terminal opens alongside the GUI and shows the log).
          console=(sys.platform != 'win32'),
          icon=None)
