#!/usr/bin/env python3
"""Offline stand-in for icloudpd 1.33.0 with the upstream prompts and log format.

Behaviour is read on every run from the JSON file named by FAKE_ICLOUDPD_CONTROL,
so a test can change it without restarting the service. Each invocation's
arguments are appended to FAKE_ICLOUDPD_CALLS when that variable is set.
"""
import json
import os
import pathlib
import signal
import sys
import time

args = sys.argv[1:]


def arg(name, default=None):
    return args[args.index(name) + 1] if name in args else default


def log(text, level="INFO"):
    print(time.strftime("%Y-%m-%d %H:%M:%S ") + f"{level:<8} " + text, flush=True)


control_file = os.environ.get("FAKE_ICLOUDPD_CONTROL")
control = json.loads(pathlib.Path(control_file).read_text()) if control_file and os.path.exists(control_file) else {}
if os.environ.get("FAKE_ICLOUDPD_CALLS"):
    with open(os.environ["FAKE_ICLOUDPD_CALLS"], "a") as calls:
        calls.write(json.dumps(args) + "\n")
if control.get("pidfile"):
    pathlib.Path(control["pidfile"]).write_text(str(os.getpid()))
if control.get("ignore_term"):
    signal.signal(signal.SIGTERM, signal.SIG_IGN)

assert "--delete-after-download" not in args and "--auto-delete" not in args
assert "--skip-live-photos" not in args

session = pathlib.Path(arg("--cookie-directory")) / "session"
if not session.exists():
    # getpass writes its prompt without a newline, like input().
    sys.stdout.write(f"iCloud Password for {arg('--username')}:")
    sys.stdout.flush()
    if sys.stdin.readline().rstrip("\n") != control.get("password", "test-secret"):
        log("Invalid email/password combination.", "ERROR")
        sys.exit(1)
    if control.get("sms"):
        print("  a: (•••) •••-••12", flush=True)
        code = input("Please enter two-factor authentication code or device index (a) to send SMS with a code: ")
        if code == "a":
            code = input("Please enter two-factor authentication code that you received over SMS: ")
    else:
        code = input("Please enter two-factor authentication code: ")
    if code != control.get("code", "987654"):
        log("Failed to verify two-factor authentication code", "ERROR")
        sys.exit(1)
    session.write_text("fixture")

if control.get("denied") and "--adp-web-access" not in args:
    log("Photo request failed: ACCESS_DENIED", "ERROR")
    sys.exit(1)

assert "--fn-cleanup-apply" not in args

if "--list-libraries" in args:
    print("PrimarySync")
    for library in control.get("libraries", []):
        print(library)
    print("FNARCHIVE/1 checked", flush=True)
    sys.exit(0)

library = arg("--library", "PrimarySync")
hidden = arg("--album") == "Hidden"
if hidden and library != "PrimarySync" and control.get("fail_hidden_shared"):
    log("Unknown album: Hidden", "ERROR")
    sys.exit(1)
if not hidden and library == "PrimarySync" and control.get("fail_primary"):
    log("Photo library request failed", "ERROR")
    sys.exit(1)


folder = pathlib.Path(arg("--directory")) / "2026" / "01"
folder.mkdir(parents=True, exist_ok=True)
count = 1 if hidden else control.get("files", 1)
total = "???" if "--until-found" in args else str(count * 2)
log(f"Downloading {total} original,alternative,adjusted photos and videos to {folder} ...")
if control.get("xmp_error") and not hidden:
    log("Could not write XMP sidecar for IMG_0001.HEIC", "ERROR")
if control.get("short_listing") and not hidden:
    log("iCloud listed only 1 of 100 items in the library", "ERROR")
if control.get("missing_master") and not hidden:
    log("Skipping asset 123: its master record abc was not returned", "WARNING")
for index in range(count):
    for ext in ("HEIC", "MOV"):
        if ext == "MOV" and not hidden and control.get("missing_live"):
            print('FNARCHIVE/1 ["live", "test.HEIC"]', flush=True)
            continue
        path = folder / f"{'hidden-' if hidden else 'test-'}{index}.{ext}"
        if not path.exists():
            time.sleep(control.get("delay", 0))
            path.write_bytes(b"fixture-" + ext.encode())
            log("Downloaded " + str(path))
log("All photos and videos have been downloaded")
if not control.get("unverified"):
    print("FNARCHIVE/1 checked", flush=True)
