"""OS-backed lock: one writer for a gateway state directory."""

import contextlib
import os
from collections.abc import Iterator
from pathlib import Path

from .storage import private_directory


@contextlib.contextmanager
def instance_lock(directory: Path) -> Iterator[None]:
    private_directory(directory)
    path = directory / "gateway.lock"
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    acquired = False
    try:
        if os.name == "posix":
            import fcntl
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        elif os.name == "nt":
            import msvcrt
            os.write(descriptor, b"0")
            os.lseek(descriptor, 0, os.SEEK_SET)
            msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
        else:
            raise RuntimeError("This platform does not support the required state-directory lock")
        acquired = True
        yield
    except (BlockingIOError, PermissionError) as exc:
        if acquired:
            raise
        raise RuntimeError("Another gateway is using this state directory") from exc
    finally:
        if acquired:
            if os.name == "posix":
                import fcntl
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            elif os.name == "nt":
                import msvcrt
                os.lseek(descriptor, 0, os.SEEK_SET)
                msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
        os.close(descriptor)
