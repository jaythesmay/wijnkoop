"""Which process does a job, and is it still alive."""
import os


def _start_ticks(pid: int) -> str | None:
    """When a process started (clock ticks since boot), so a reused process number is not
    mistaken for the run that had it before a restart."""
    try:
        with open(f"/proc/{pid}/stat", "rb") as f:
            stat = f.read().decode(errors="replace")
        return stat.rsplit(")", 1)[1].split()[19]
    except (OSError, IndexError):
        return None


def process_token(pid: int | None = None) -> str:
    pid = pid or os.getpid()
    ticks = _start_ticks(pid)
    return f"{pid}:{ticks}" if ticks else str(pid)


def is_alive(token: str | None) -> bool:
    if not token:
        return False
    pid_s, _, ticks = token.partition(":")
    try:
        pid = int(pid_s)
        os.kill(pid, 0)
    except (ValueError, ProcessLookupError):
        return False
    except PermissionError:
        pass  # alive, owned by someone else
    if ticks and _start_ticks(pid) not in (None, ticks):
        return False
    try:
        with open(f"/proc/{pid}/stat", "rb") as f:
            if f.read().rsplit(b")", 1)[1].split()[0] == b"Z":  # ended, waiting to be cleared away
                return False
    except (OSError, IndexError):
        pass
    return True
