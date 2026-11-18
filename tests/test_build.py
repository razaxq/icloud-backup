import io
from pathlib import Path
import subprocess
import sys
import tarfile
from tempfile import TemporaryDirectory
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import build


def tar_bytes(files):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as archive:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size, info.mode = len(data), 0o755
            archive.addfile(info, io.BytesIO(data))
    return buf.getvalue()


class BuildTests(unittest.TestCase):
    def test_optimized_python_still_verifies_vendor_integrity(self):
        files = dict.fromkeys(build.PACKAGE, b"")
        files["manifest"] = b"platform=x86\nversion=0.3.0\nchangelog=Changes\n"
        files["app.tgz"] = tar_bytes(dict.fromkeys(build.PAYLOAD, b""))
        with TemporaryDirectory() as tmp:
            fpk = Path(tmp) / "test.fpk"
            fpk.write_bytes(tar_bytes(files))
            result = subprocess.run([sys.executable, "-O", "-B", "-c",
                                     "import build,sys; build.verify(sys.argv[1],'amd64','0'*64,'0.3.0')",
                                     str(fpk)], cwd=build.ROOT, capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("vendor checksum mismatch", result.stderr)
