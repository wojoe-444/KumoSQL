"""Shared rules for using Z3: one lock around its default context, and a deterministic work cap per check.

Z3 terms, sorts and solvers built without an explicit context share one global context, and Z3 objects
must not be used from two threads at once: concurrent first calls fail with ``Context mismatch`` and can
leave the context unusable for the rest of the process. The UI serves requests on threads and analyses
pipelines in the background, so each public entry point that builds Z3 terms runs under this lock.
It is re-entrant, so a prover that calls another (or a rule that uses Z3) does not deadlock.
"""

from __future__ import annotations

import functools
import gc
import threading

try:
    import z3
except ImportError:  # pragma: no cover - z3-solver is optional
    z3 = None

SOLVER_LOCK = threading.RLock()

# What every entry point answers without the optional solver (the extra is ``smt``; the package name differs between
# the product and the split-out prover, so the hint names the dependency itself).
Z3_MISSING = "z3-solver is not installed (pip install z3-solver)"


def serialized(function):
    """Run ``function`` while holding :data:`SOLVER_LOCK`."""

    @functools.wraps(function)
    def locked(*args, **kwargs):
        with SOLVER_LOCK:
            # Cyclic Z3 terms from a completed proof can otherwise be collected
            # by a different Python thread while this thread uses the context.
            restore_gc = gc.isenabled()
            if restore_gc:
                gc.disable()
            try:
                return function(*args, **kwargs)
            finally:
                if restore_gc:
                    try:
                        if gc.get_count()[0] >= gc.get_threshold()[0]:
                            gc.collect()
                    finally:
                        gc.enable()

    return locked


# Each solver check also gets a deterministic work cap (Z3's rlimit) of ``timeout_ms * WORK_PER_MS`` units.
# A check that runs out of it stops at the same point on any machine and under any load; the wall-clock
# ``timeout_ms`` still stops checks that do little counted work. Measured on the QED pairs, checks do 150 to
# 1,000,000 units per millisecond and the largest finished check used 4.3 million units, so the cap
# (100 million at the default 5000 ms) never binds before a check that finishes today would.
WORK_PER_MS = 20000


def bounded_solver(timeout_ms: int):
    """A ``z3.Solver`` whose checks stop at a deterministic work cap or at ``timeout_ms`` (see ``WORK_PER_MS``).

    Z3 reports ``reason_unknown()`` ``"canceled"`` for the cap and ``"timeout"`` for the wall clock.
    """

    return bound(z3.Solver(), timeout_ms)


def bound(solver, timeout_ms: int):
    """Apply the work cap and the wall-clock limit to ``solver``; ``Solver.translate`` drops both."""

    solver.set("rlimit", timeout_ms * WORK_PER_MS)
    solver.set("timeout", timeout_ms)
    return solver
