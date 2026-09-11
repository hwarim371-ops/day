"""Nonblocking process locks shared by desktop and Linux collection workers."""
import os

if os.name == "nt":
    import msvcrt
else:
    import fcntl


def acquire(file):
    file.seek(0)
    if os.name == "nt":
        msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        fcntl.flock(file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def release(file):
    file.seek(0)
    if os.name == "nt":
        msvcrt.locking(file.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        fcntl.flock(file.fileno(), fcntl.LOCK_UN)
