"""The UI server started by another program: a free port it reports, and an end when that program goes."""

import os
import subprocess
import sys
from io import BytesIO
from threading import Thread
from urllib.request import Request

from ui_http import urlopen

from kumosql.ui import UIHandler, UIServer, stop_when_stdin_closes


def _start(tmp_path, *flags):
    env = {**os.environ, "KUMOSQL_HOME": str(tmp_path / "home")}  # nothing is written to the real data folder
    return subprocess.Popen(
        [sys.executable, "-m", "kumosql.ui", "--no-browser", "--port", "0", "--print-port", *flags],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, text=True,
    )


def test_port_zero_binds_a_free_port_and_prints_it_first(tmp_path):
    proc = _start(tmp_path, "--exit-on-stdin-close")
    try:
        first = proc.stdout.readline()
        assert first.startswith("PORT ") and first.split()[1].isdigit()
        port = int(first.split()[1])
        assert 0 < port <= 65535 and port != 8765
        with urlopen(Request(f"http://127.0.0.1:{port}/")) as response:  # the same address the banner names
            assert b"Original SQL" in response.read()
        assert proc.poll() is None  # stays up while the pipe is open
    finally:
        proc.stdin.close()  # the starter went away
        assert proc.wait(timeout=20) == 0
        proc.stdout.close()
        proc.stderr.close()


def test_without_the_flag_the_ui_ignores_a_closed_stdin(tmp_path):
    proc = _start(tmp_path)
    try:
        assert proc.stdout.readline().startswith("PORT ")
        proc.stdin.close()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass
        else:
            raise AssertionError("the UI ended on its own although --exit-on-stdin-close was not given")
    finally:
        proc.terminate()
        proc.wait(timeout=20)
        proc.stdout.close()
        proc.stderr.close()


def test_stop_when_stdin_closes_shuts_the_server_down_after_discarding_input():
    server = UIServer(("127.0.0.1", 0), UIHandler)
    serving = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    serving.start()
    try:
        watcher = stop_when_stdin_closes(server, BytesIO(b"ignored bytes"))
        watcher.join(timeout=5)
        serving.join(timeout=5)
        assert not watcher.is_alive() and not serving.is_alive()
    finally:
        server.server_close()


def test_no_stdin_means_nothing_to_watch():
    class NoStdin:
        buffer = None

    assert stop_when_stdin_closes(object(), NoStdin()) is None
