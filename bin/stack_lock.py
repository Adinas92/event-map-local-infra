#!/usr/bin/env python3
"""Serialize local stack operations and test runs across workspace checkouts."""
from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile

KEY = 'EVENT_MAP_STACK_LOCK_FD'


def lock_path():
    root = Path('/tmp').resolve() / ('event-map-stack-lock-' + str(os.getuid()))
    root.mkdir(mode=0o700, exist_ok=True)
    info = root.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise ValueError('invalid stack lock directory')
    return root / 'stack.lock'


def inherited_fd():
    value = os.environ.get(KEY)
    if value is None:
        return None
    fd = int(value)
    info = os.fstat(fd)
    expected = lock_path().lstat()
    if (fd < 3 or not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600
            or (info.st_dev, info.st_ino) != (expected.st_dev, expected.st_ino)):
        raise ValueError('invalid inherited stack lock')
    return fd


@contextmanager
def stack_lock():
    previous = os.environ.get(KEY)
    inherited = inherited_fd()
    fd = os.dup(inherited) if inherited is not None else os.open(
        lock_path(), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
            raise ValueError('invalid stack lock file')
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        os.environ[KEY] = str(fd)
        yield fd
    finally:
        if previous is None:
            os.environ.pop(KEY, None)
        else:
            os.environ[KEY] = previous
        # Never explicitly unlock a shared open-file description inherited by a child.
        os.close(fd)


def main():
    try:
        with stack_lock() as fd:
            return subprocess.run(sys.argv[1:], pass_fds=(fd,)).returncode
    except (OSError, ValueError):
        print('BLOCKED: local stack is busy or its lock is invalid.', file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
