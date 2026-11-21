#!/usr/bin/env python3
"""Run one foreground writer under an exclusive checkout lock."""

import argparse
import fcntl
import json
import os
from pathlib import Path
import sys
import time


LOCK_PATH = Path(__file__).resolve().parents[1] / ".writer.lock"


def holder_pid(lock):
    # Linux can identify the owner even before it publishes its metadata.
    info = os.fstat(lock.fileno())
    try:
        with Path("/proc/locks").open(encoding="ascii") as entries:
            for entry in entries:
                fields = entry.split()
                if fields[1:4] == ["FLOCK", "ADVISORY", "WRITE"]:
                    major, minor, inode = fields[5].split(":")
                    if (os.makedev(int(major, 16), int(minor, 16)) == info.st_dev
                            and int(inode) == info.st_ino):
                        return int(fields[4])
    except OSError:
        pass

    try:
        return int(json.loads(lock.read(256))["pid"])
    except (ValueError, TypeError, KeyError):
        return None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", nargs=argparse.REMAINDER, help="-- COMMAND [ARG ...]")
    command = parser.parse_args().command
    if command[:1] == ["--"]:
        command = command[1:]
    if not command:
        parser.error("a writer command is required")

    try:
        fd = os.open(LOCK_PATH, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o644)
        with os.fdopen(fd, "r+b") as lock:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                pid = holder_pid(lock)
                holder = f"PID {pid}" if pid is not None else "a process publishing its PID"
                print(f"Writer lock {LOCK_PATH} is held by {holder}", file=sys.stderr)
                return 1

            # A free kernel lock makes any previous record stale, including a reused PID.
            metadata = json.dumps({"pid": os.getpid(), "timestamp": time.time()})
            lock.write((metadata + "\n").encode("ascii"))
            lock.truncate()
            lock.flush()

            # Preserve both the PID and the lock across exec. The kernel releases
            # the lock when its last descriptor closes, including on a crash.
            os.set_inheritable(fd, True)
            os.execvp(command[0], command)
    except OSError as exc:
        print(f"Writer lock {LOCK_PATH}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
