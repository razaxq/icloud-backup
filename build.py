"""Build the two fnOS installation packages on Linux using the official fnpack."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import io
from pathlib import Path, PurePosixPath
import platform
import secrets
import shutil
import subprocess
import sys
import tarfile
import tempfile
from urllib.request import Request, urlopen
import zipfile

ROOT = Path(__file__).resolve().parent
RELEASE = "1.33.0"
SOURCE = (f"https://codeload.github.com/icloud-photos-downloader/icloud_photos_downloader/tar.gz/refs/tags/v{RELEASE}",
          "465d4db04b842e67336c857447b9a3e10c930f4874b81dea4edb4261a2aef55a")
# SHA-256 over the complete installed vendor tree, including wheel metadata and licenses.
TARGETS = {
    "amd64": ("x86", "9933e7849ca3707854a6ed1b5ee57966b5b86e92d50e15b062f3cf0cec34e426"),
    "arm64": ("arm", "56b6a8c41c38a648eb2dc108524a35649014d583edd583da9f1dac35d58f15ba"),
}
FNPACK = {
    "x86_64": ("https://static2.fnnas.com/fnpack/fnpack-1.2.3-linux-amd64",
               "54b97fa7b70968c4d05c79840f5daeff508957d0bb2062fdb0376d00d9615c93"),
}
PAYLOAD = {"web/server.py", "web/archive.py", "web/notifier.py", "web/static/index.html", "web/static/app.js",
           "web/static/style.css", "web/static/inter.woff2",
           "worker/__init__.py", "worker/engine.py", "worker/strict_cli.py", "bin/icloudpd", "bin/run.py",
           "vendor/icloudpd/base.py", "vendor/pyicloud_ipd/services/photos.py", "ui/config", "ui/images/icon_64.png",
           "ui/images/icon_256.png", "licenses/icloudpd-LICENSE.md", "licenses/Inter-OFL.txt"}
COMMANDS = {path.name for path in (ROOT / "package/cmd").iterdir() if path.is_file()}
PACKAGE = {"manifest", "ICON.PNG", "ICON_256.PNG", "config/privilege", "config/resource",
           "wizard/install", "wizard/uninstall", "app.tgz", *("cmd/" + name for name in COMMANDS)}


def digest_stream(stream):
    # hashlib.file_digest needs Python 3.11; keep the build usable on 3.10 hosts.
    digest = hashlib.sha256()
    for chunk in iter(lambda: stream.read(1 << 20), b""):
        digest.update(chunk)
    return digest.hexdigest()


def sha256(path):
    with path.open("rb") as stream:
        return digest_stream(stream)


def retrieve(cache, url, digest, filename=None):
    target = cache / (filename or url.split("/")[-1])
    if target.is_file() and sha256(target) == digest:
        return target
    with tempfile.NamedTemporaryFile(dir=cache, delete=False) as tmp:
        partial = Path(tmp.name)
    try:
        with urlopen(Request(url, headers={"User-Agent": "fn-icloud-photos-builder"}), timeout=90) as response, \
                partial.open("wb") as out:
            shutil.copyfileobj(response, out)
        if sha256(partial) != digest:
            raise ValueError(f"SHA-256 mismatch for {target.name}")
        partial.replace(target)
        return target
    finally:
        partial.unlink(missing_ok=True)


def members(archive):
    return {str(PurePosixPath(entry.name)): entry for entry in archive.getmembers()}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def tree_digest(hashes):
    digest = hashlib.sha256()
    for name, checksum in sorted(hashes.items()):
        digest.update(name.encode("utf-8") + b"\0" + bytes.fromhex(checksum))
    return digest.hexdigest()


def install_vendor(cache, arch, vendor):
    source = retrieve(cache, *SOURCE, f"icloudpd-source-{RELEASE}.tar.gz")
    prefix = f"icloud_photos_downloader-{RELEASE}/src/"
    with tarfile.open(source) as archive:
        for entry in archive:
            if not entry.isfile() or not entry.name.startswith(prefix):
                continue
            path = PurePosixPath(entry.name.removeprefix(prefix))
            if path.parts[0] not in ("icloudpd", "pyicloud_ipd", "foundation"):
                continue
            require(not path.is_absolute() and ".." not in path.parts, "invalid source path")
            dest = vendor / path
            dest.parent.mkdir(parents=True, exist_ok=True)
            with archive.extractfile(entry) as src, dest.open("xb") as out:
                shutil.copyfileobj(src, out)
    # Mirror scripts/patch_version from the pinned release. Source archives
    # otherwise report the development placeholder when asked for --version.
    metadata = vendor / "foundation/__init__.py"
    content = metadata.read_bytes().replace(b"0.0.1", RELEASE.encode("ascii"))
    metadata.write_bytes(content.replace(b"abcdefgh", b"3caeae5").replace(b"1234567890", b"1793741327"))

    downloads = []
    for line in (ROOT / "wheels.lock").read_text(encoding="ascii").splitlines():
        if line and not line.startswith("#"):
            target, checksum, url = line.split()
            if target in ("all", arch):
                downloads.append((url, checksum))
    with ThreadPoolExecutor(max_workers=6) as pool:
        wheels = list(pool.map(lambda item: retrieve(cache, *item), downloads))
    for wheel in wheels:
        with zipfile.ZipFile(wheel) as archive:
            for entry in archive.infolist():
                if entry.is_dir():
                    continue
                path = PurePosixPath(entry.filename)
                require(not path.is_absolute() and ".." not in path.parts and not path.parts[0].endswith(".data"),
                        f"unsupported wheel layout in {wheel.name}: {path}")
                dest = vendor / path
                dest.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(entry) as src, dest.open("xb") as out:
                    shutil.copyfileobj(src, out)


def verify(fpk, arch, expected_digest, version):
    with tarfile.open(fpk) as outer:
        files = members(outer)
        missing = PACKAGE - files.keys()
        require(not missing, f"package is missing {sorted(missing)}")
        manifest = outer.extractfile(files["manifest"]).read().decode()
        fields = dict(tuple(part.strip() for part in line.split("=", 1)) for line in manifest.splitlines() if "=" in line)
        require(fields.get("platform") == TARGETS[arch][0], "wrong platform in manifest")
        require(fields.get("version") == version, "wrong version in manifest")
        require(fields.get("changelog"), "manifest has no changelog")
        require(all(files[f"cmd/{name}"].isfile() and files[f"cmd/{name}"].mode & 0o111 for name in COMMANDS),
                "cmd scripts must be executable")
        with tarfile.open(fileobj=io.BytesIO(outer.extractfile(files["app.tgz"]).read())) as payload:
            entries = members(payload)
            missing = PAYLOAD - entries.keys()
            require(not missing, f"payload is missing {sorted(missing)}")
            require(not any("__pycache__" in name or name.startswith("tests/") for name in entries),
                    "payload contains development files")
            engine = entries["bin/icloudpd"]
            require(engine.isfile() and engine.mode & 0o111, "icloudpd must be executable")
            hashes = {name.removeprefix("vendor/"): digest_stream(payload.extractfile(entry))
                      for name, entry in entries.items() if name.startswith("vendor/") and entry.isfile()}
            require(tree_digest(hashes) == expected_digest, "vendor checksum mismatch")


def build(arch, fnpack, cache, output):
    fn_arch, digest = TARGETS[arch]
    with tempfile.TemporaryDirectory(prefix="fn-icloud-build-") as tmp:
        work = Path(tmp)
        stage = work / "package"
        shutil.copytree(ROOT / "package", stage)
        app = stage / "app"
        for folder in ("web", "worker"):
            shutil.copytree(ROOT / folder, app / folder, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        shutil.copytree(ROOT / "licenses", app / "licenses")
        install_vendor(cache, arch, app / "vendor")
        manifest = stage / "manifest"
        lines = manifest.read_text(encoding="utf-8").splitlines()
        version = next(line.split("=", 1)[1] for line in lines if line.startswith("version="))
        manifest.write_text("\n".join("platform=" + fn_arch if line.startswith("platform=") else line
                                      for line in lines) + "\n", encoding="utf-8")
        for path in stage.rglob("*"):
            executable = path.is_dir() or path.parent == stage / "cmd" or path == app / "bin" / "icloudpd"
            path.chmod(0o755 if executable else 0o644)
        subprocess.run([str(fnpack), "build", "--directory", str(stage)], cwd=work, check=True)
        artifact, = work.rglob("*.fpk")
        verify(artifact, arch, digest, version)
        final = output / f"icloud-photos-{version}-{arch}.fpk"
        partial = output / f".{final.name}.{secrets.token_hex(10)}.tmp"
        try:
            with partial.open("xb") as out, artifact.open("rb") as src:
                shutil.copyfileobj(src, out)
            partial.replace(final)
        finally:
            partial.unlink(missing_ok=True)
        print(f"{sha256(final)}  {final.name}", flush=True)
        return final


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--arch", choices=["both", *TARGETS], default="both")
    parser.add_argument("--fnpack", type=Path, default=None, help="use a local fnpack instead of the pinned download")
    parser.add_argument("--cache", type=Path, default=Path.home() / ".cache/fn-icloud-photos")
    parser.add_argument("--output", type=Path, default=ROOT / "dist")
    args = parser.parse_args()
    if sys.platform != "linux":
        parser.error("Run on Linux or WSL so FPK file permissions are preserved.")
    args.cache.mkdir(parents=True, exist_ok=True)
    args.output.mkdir(parents=True, exist_ok=True)
    if args.fnpack:
        fnpack = args.fnpack.resolve()
    elif platform.machine() in FNPACK:
        fnpack = retrieve(args.cache, *FNPACK[platform.machine()])
        fnpack.chmod(0o755)
    else:
        parser.error("Specify the native official fnpack executable using --fnpack.")
    for arch in TARGETS if args.arch == "both" else [args.arch]:
        build(arch, fnpack, args.cache, args.output)


if __name__ == "__main__":
    main()
