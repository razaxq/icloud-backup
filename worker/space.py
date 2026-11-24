"""Headroom required on the archive volume during a backup."""
import shutil
import time


RESERVE = 1024 ** 3


class LowSpace(Exception):
    def __init__(self, free, required):
        self.free = free
        self.required = required
        super().__init__("low archive space")


def check(path, additional=0):
    free = shutil.disk_usage(path).free
    required = RESERVE + additional
    if free <= required:
        raise LowSpace(free, required)
    return free


class SpaceMonitor:
    def __init__(self, root, on_event):
        self.root = root
        self.on_event = on_event
        self.next_check = 0

    def poll(self):
        now = time.monotonic()
        if now >= self.next_check:
            self.next_check = now + 2
            self.on_event("space", check(self.root))
