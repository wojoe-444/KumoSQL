"""Saved files are replaced whole while other threads read them, on Windows as elsewhere (``resilience.replace_file``)."""

import threading
import time

import pytest

from kumosql import resilience


def test_a_file_open_by_a_shared_reader_is_replaced_at_once(tmp_path):
    target, new = tmp_path / "state.json", tmp_path / "new.tmp"
    target.write_bytes(b"old")
    new.write_bytes(b"new")
    with resilience.open_shared(target) as reader:
        started = time.monotonic()
        resilience.replace_file(new, target)
        assert time.monotonic() - started < 1  # not the retry window: the rename went through with the reader open
        assert reader.read() == b"old"  # the reader keeps the file it opened
    assert target.read_bytes() == b"new" and not new.exists()
    assert resilience.read_bytes_shared(target) == b"new"


def test_a_plain_reader_only_delays_the_replace(tmp_path):
    target, new = tmp_path / "state.json", tmp_path / "new.tmp"
    target.write_bytes(b"old")
    new.write_bytes(b"new")
    reader = open(target, "rb")  # without FILE_SHARE_DELETE on Windows: the rename must wait for it
    closer = threading.Timer(0.2, reader.close)
    closer.start()
    try:
        resilience.replace_file(new, target)
    finally:
        closer.join()
        reader.close()
    assert target.read_bytes() == b"new"


def test_a_reader_that_never_lets_go_fails_the_write_after_the_window(tmp_path, monkeypatch):
    if resilience.sys.platform != "win32":
        pytest.skip("a rename over an open file always succeeds off Windows")
    monkeypatch.setattr(resilience, "REPLACE_SECONDS", 0.1)
    target, new = tmp_path / "state.json", tmp_path / "new.tmp"
    target.write_bytes(b"old")
    new.write_bytes(b"new")
    with open(target, "rb"), pytest.raises(PermissionError):
        resilience.replace_file(new, target)
    assert target.read_bytes() == b"old" and new.read_bytes() == b"new"


def test_shared_reads_fail_like_ordinary_ones(tmp_path):
    with pytest.raises(FileNotFoundError):
        resilience.read_bytes_shared(tmp_path / "missing.json")
    with pytest.raises(OSError):
        resilience.read_bytes_shared(tmp_path)  # a folder
    (tmp_path / "a.json").write_bytes(b'{"a": 1}\r\n')
    assert resilience.read_bytes_shared(tmp_path / "a.json") == b'{"a": 1}\r\n'  # bytes as stored


def test_concurrent_replaces_and_shared_reads_always_see_a_whole_file(tmp_path):
    target = tmp_path / "state.json"
    target.write_bytes(b"0" * 1000)
    errors, stop = [], threading.Event()

    def writer(number):
        for round_ in range(20):
            temp = tmp_path / f"w{number}-{round_}.tmp"
            temp.write_bytes(str(number).encode() * 1000)
            try:
                resilience.replace_file(temp, target)
            except OSError as exc:
                errors.append(exc)

    def reader():
        while not stop.is_set():
            data = resilience.read_bytes_shared(target)
            if len(data) != 1000 or len(set(data)) != 1:
                errors.append(AssertionError(f"read {len(data)} bytes of {len(set(data))} writers"))

    readers = [threading.Thread(target=reader) for _ in range(3)]
    writers = [threading.Thread(target=writer, args=(n,)) for n in range(3)]
    for thread in (*readers, *writers):
        thread.start()
    for thread in writers:
        thread.join()
    stop.set()
    for thread in readers:
        thread.join()
    assert errors == []
