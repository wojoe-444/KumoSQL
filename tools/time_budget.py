"""A wall-clock budget for code in the main thread, on every system: ``stop = arm(seconds, handler)``, then ``stop()``.

Where ``signal.setitimer`` exists (Linux, macOS) this is a ``SIGALRM`` timer that calls ``handler(signum, frame)`` after
``seconds`` and again every ``repeat`` seconds until stopped: an exception raised inside a destructor (z3's ``__del__``)
is swallowed, so one alarm can be lost. Windows has no ``SIGALRM``. There a timer thread raises a simulated ``SIGINT``
in the main thread (``_thread.interrupt_main``) on the same schedule, and the ``SIGINT`` handler installed for the
budget calls ``handler``; a real Ctrl+C during the budget still raises ``KeyboardInterrupt``. Either way the handler
runs between Python bytecodes, so a long call into C (a solver) finishes first.

The proof tools (``proof_admission``, ``proof_recheck``, ``proof_mutants``) use it, so a pair that hangs is cut off on
Windows as it is on Linux. Like ``signal.signal``, :func:`arm` raises ``ValueError`` outside the main thread.
"""

from __future__ import annotations

import _thread
import signal
import threading
from typing import Callable

Handler = Callable[[int, object], object]


def arm(seconds: float, handler: Handler, repeat: float = 5.0) -> Callable[[], None]:
    """Start the budget; returns the function that stops it and puts the previous signal handler back.

    ``seconds <= 0`` sets no budget (``setitimer`` treats 0 the same way)."""

    if hasattr(signal, "setitimer"):
        previous = signal.signal(signal.SIGALRM, handler)
        signal.setitimer(signal.ITIMER_REAL, seconds, repeat)

        def stop_alarm() -> None:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, previous)

        return stop_alarm
    return _arm_with_a_thread(seconds, handler, repeat)


def _arm_with_a_thread(seconds: float, handler: Handler, repeat: float) -> Callable[[], None]:
    fired, done = threading.Event(), threading.Event()

    def on_interrupt(signum, frame):
        if done.is_set():
            return None  # the budget ended as its timer fired: nothing is left to stop
        if fired.is_set():
            fired.clear()
            return handler(signum, frame)
        raise KeyboardInterrupt

    previous = signal.signal(signal.SIGINT, on_interrupt)
    if seconds <= 0:
        return lambda: signal.signal(signal.SIGINT, previous)

    def tick() -> None:
        wait = seconds
        while not done.wait(wait):
            fired.set()
            _thread.interrupt_main(signal.SIGINT)
            if repeat <= 0:
                return
            wait = repeat

    timer = threading.Thread(target=tick, name="time-budget", daemon=True)
    timer.start()

    def stop_thread() -> None:
        done.set()
        timer.join()
        # signal.signal runs any signal still pending first, with on_interrupt, which ignores it now that done is set.
        signal.signal(signal.SIGINT, previous)

    return stop_thread
