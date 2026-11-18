"""Launch the verified bundled downloader with fnOS's Python runtime."""
from pathlib import Path
import sys

if sys.implementation.name != "cpython" or sys.version_info[:2] != (3, 12):
    sys.exit("The iCloud downloader requires CPython 3.12.")

app = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(app / "vendor"), str(app)]
from worker.strict_cli import main  # noqa: E402

sys.exit(main())
