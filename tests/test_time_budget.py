"""The proof tools' wall-clock budget stops a runaway check on every system (``tools/time_budget.py``)."""

import _thread
import signal
import sys
import time
from pathlib import Path

import pytest

TOOLS = str(Path(__file__).resolve().parent.parent / "tools")
sys.path.insert(0, TOOLS)
import time_budget  # noqa: E402

sys.path.remove(TOOLS)


class OutOfTime(Exception):
    pass


def out_of_time(_signum, _frame):
    raise OutOfTime


def spin(seconds):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        pass


def test_python_code_that_runs_past_its_budget_is_stopped():
    previous = signal.getsignal(signal.SIGINT)
    started = time.monotonic()
    stop = time_budget.arm(0.2, out_of_time)
    try:
        with pytest.raises(OutOfTime):
            spin(20)
    finally:
        stop()
    assert time.monotonic() - started < 10
    assert signal.getsignal(signal.SIGINT) is previous  # the handlers are put back


def test_the_budget_repeats_when_the_first_stop_is_swallowed():
    stop = time_budget.arm(0.1, out_of_time, repeat=0.1)
    try:
        try:
            spin(20)
        except OutOfTime:
            pass  # as a destructor would swallow it
        with pytest.raises(OutOfTime):
            spin(20)
    finally:
        stop()


def test_a_budget_stopped_in_time_raises_nothing_later():
    stop = time_budget.arm(0.3, out_of_time)
    stop()
    spin(0.6)


def test_no_budget_for_zero_seconds():
    stop = time_budget.arm(0, out_of_time)
    try:
        spin(0.3)
    finally:
        stop()


def test_ctrl_c_during_a_budget_is_still_a_keyboard_interrupt():
    stop = time_budget.arm(30, out_of_time)
    try:
        with pytest.raises(KeyboardInterrupt):
            _thread.interrupt_main(signal.SIGINT)
            spin(10)
    finally:
        stop()
