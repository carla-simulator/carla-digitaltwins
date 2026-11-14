"""Non-blocking exclusive file locks on both POSIX (fcntl) and Windows (msvcrt)."""
import errno
from pathlib import Path

try:
    import fcntl
except ImportError:
    fcntl = None
    import msvcrt

_CONTENTION_ERRNOS = {errno.EACCES, errno.EAGAIN, getattr(errno, 'EDEADLK', -1)}
_CONTENTION_WINERRORS = {33, 36}  # ERROR_LOCK_VIOLATION, ERROR_SHARING_VIOLATION


def lock_exclusive(handle):
    """Take an exclusive lock, raising BlockingIOError when another writer holds it."""
    if fcntl is not None:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise
        except OSError as error:
            if error.errno in _CONTENTION_ERRNOS:
                raise BlockingIOError(str(error)) from error
            raise
        return
    handle.seek(0)
    try:
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    except BlockingIOError:
        raise
    except PermissionError as error:
        raise BlockingIOError(str(error)) from error
    except OSError as error:
        if error.errno in _CONTENTION_ERRNOS or getattr(error, 'winerror', None) in _CONTENTION_WINERRORS:
            raise BlockingIOError(str(error)) from error
        raise


def unlock(handle):
    """Release a lock taken by lock_exclusive."""
    if fcntl is not None:
        fcntl.flock(handle, fcntl.LOCK_UN)
        return
    handle.seek(0)
    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


def is_locked(path) -> bool:
    """Probe whether an existing lockfile is currently held by an exclusive writer."""
    target = Path(path)
    if not target.is_file():
        return False
    try:
        if fcntl is not None:
            with target.open('r', encoding='utf-8') as handle:
                try:
                    fcntl.flock(handle, fcntl.LOCK_SH | fcntl.LOCK_NB)
                except (BlockingIOError, PermissionError):
                    return True
                except OSError as error:
                    if error.errno in _CONTENTION_ERRNOS:
                        return True
                    raise
                finally:
                    try:
                        fcntl.flock(handle, fcntl.LOCK_UN)
                    except OSError:
                        pass
            return False
        with target.open('a', encoding='utf-8') as handle:
            try:
                lock_exclusive(handle)
            except BlockingIOError:
                return True
            else:
                unlock(handle)
                return False
    except FileNotFoundError:
        return False

