"""Process + thread safe lock for read-modify-write of the panel's JSON files."""
import fcntl
import os
import threading
from contextlib import contextmanager

_tlock = threading.RLock()


@contextmanager
def locked(path: str):
    lock_path = path + ".lock"
    os.makedirs(os.path.dirname(lock_path) or ".", exist_ok=True)
    with _tlock:
        with open(lock_path, "a") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fh, fcntl.LOCK_UN)
