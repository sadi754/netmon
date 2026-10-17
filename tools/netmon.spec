# PyInstaller recipe for NetMon: a portable folder, dist\NetMon, with NetMon.exe in it.
# Run build.bat rather than this directly (it makes the icon and version info first).
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

ROOT = Path(SPECPATH).parent  # noqa: F821 - SPECPATH is set by PyInstaller

a = Analysis(
    [str(ROOT / "tools" / "netmon_entry.py")],
    pathex=[str(ROOT)],
    datas=[
        (str(ROOT / "web"), "web"),  # the window's UI
        (str(ROOT / "relay" / "router" / "netmon-feed.rsc.template"), "relay/router"),  # Router tab script maker
    ],
    # uvicorn picks its event loop and protocols by name at run time; pystray its Windows backend.
    hiddenimports=collect_submodules("uvicorn") + ["pystray._win32"],
    excludes=["tkinter"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="NetMon",
    icon=str(ROOT / "build" / "netmon.ico"),
    version=str(ROOT / "build" / "version_info.txt"),
    console=False,  # a tray app: no console window
    upx=False,      # compressed executables upset antivirus programs more often
)
coll = COLLECT(exe, a.binaries, a.datas, name="NetMon", upx=False)
