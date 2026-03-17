"""Thread-safe print wrapper shared across all modules.

Extracted into its own module to avoid the double-import problem that occurs
when run.py is executed as __main__ and also imported as 'run' by worker.py.
"""

import threading

_print_lock = threading.Lock()


def safe_print(*args, **kwargs):
    """Thread-safe print wrapper to prevent interleaved output from concurrent agents."""
    with _print_lock:
        print(*args, **kwargs)
