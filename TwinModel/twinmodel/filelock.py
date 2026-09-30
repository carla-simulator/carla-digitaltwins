"""Non-blocking exclusive file locks on both POSIX (fcntl) and Windows (msvcrt)."""
try:
    import fcntl
except ImportError:
    fcntl = None
    import msvcrt


def lock_exclusive(handle):
    """Take an exclusive lock, raising BlockingIOError when another writer holds it."""
    if fcntl is not None:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return
    handle.seek(0)
    try:
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    except BlockingIOError:
        raise
    except OSError as error:
        raise BlockingIOError(str(error)) from error


def unlock(handle):
    """Release a lock taken by lock_exclusive."""
    if fcntl is not None:
        fcntl.flock(handle, fcntl.LOCK_UN)
        return
    handle.seek(0)
    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
