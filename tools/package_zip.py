"""After PyInstaller: zip dist\\NetMon into dist\\NetMon-<version>-win64.zip, ready to share. Run by build.bat."""
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from netmon import __version__  # noqa: E402

folder = ROOT / "dist" / "NetMon"
target = ROOT / "dist" / f"NetMon-{__version__}-win64.zip"
with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
    for path in sorted(folder.rglob("*")):
        if path.is_file() and "data" not in path.relative_to(folder).parts[:1]:  # never someone's data
            archive.write(path, Path("NetMon") / path.relative_to(folder))
print(f"[build] {target.relative_to(ROOT)}: {target.stat().st_size / 1024 / 1024:.1f} MB")
