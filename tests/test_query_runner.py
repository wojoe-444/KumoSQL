"""``python -m kumosql q run``: one read-only query, the same output in every shell, a distinct exit code per failure.

End-to-end runs use a subprocess and the local DuckDB backend; the BigQuery paths use a fake transport, so nothing here
needs the network or credentials. Every test works in its own ``tmp_path``.
"""

from __future__ import annotations

import base64
import codecs
import io
import json
import os
import shutil
import subprocess
import sys
import urllib.error
from pathlib import Path

import pytest

from kumosql import query_runner
from kumosql.query_runner import BigQueryRunner, run

ROOT = Path(__file__).resolve().parents[1]
SUMMARY_KEYS = [
    "status", "exit_code", "error", "backend", "rows", "total_rows", "truncated", "columns",
    "bytes_estimated", "bytes_accuracy", "within_cap", "max_bytes_billed", "bytes_processed", "bytes_billed",
    "cache_hit", "job_id", "output", "format", "checked", "not_checked", "warnings",
]
MULTILINE = "-- the first line is only a comment\nSELECT id,\n       name\nFROM orders\nWHERE amount > 3\nORDER BY id\n"

duckdb = pytest.importorskip("duckdb")


@pytest.fixture
def home(tmp_path, monkeypatch):
    folder = tmp_path / "home"
    folder.mkdir()
    monkeypatch.setenv("KUMOSQL_HOME", str(folder))
    return folder


@pytest.fixture
def database(tmp_path):
    path = tmp_path / "shop.duckdb"
    connection = duckdb.connect(str(path))
    connection.execute(
        "CREATE TABLE orders AS SELECT * FROM (VALUES (1, 'ann', 2.5, DATE '2024-01-01'), (2, 'bob', 4.0, DATE '2024-02-01'),"
        " (3, 'ann', 7.25, NULL), (4, NULL, NULL, DATE '2024-03-01')) t(id, name, amount, day)"
    )
    connection.close()
    return path


def _env(tmp_path: Path) -> dict:
    temp = tmp_path / "temp"
    temp.mkdir(exist_ok=True)
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(filter(None, [str(ROOT / "src"), os.environ.get("PYTHONPATH")])),
           "KUMOSQL_HOME": str(tmp_path / "home"), "TMP": str(temp), "TEMP": str(temp), "TMPDIR": str(temp)}
    env.pop("PYTHONWARNINGS", None)
    return env


def _cli(tmp_path: Path, *args: str, stdin: bytes | None = None, command: str = "q") -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-m", "kumosql", command, "run", *args], input=stdin, capture_output=True,
                          env=_env(tmp_path), cwd=tmp_path, timeout=120)


def _summary(stdout: bytes) -> dict:
    lines = stdout.split(b"\n")
    assert lines[-1] == b"" and len(lines) == 2, stdout  # exactly one line, and nothing else
    lines[0].decode("ascii")  # ASCII, so every console code page shows the same bytes
    return json.loads(lines[0])


# ------------------------------------------------------------------ end to end


def test_multiline_sql_through_stdin_runs_whole_with_one_summary_line_and_empty_stderr(tmp_path, database):
    out = tmp_path / "result.json"
    done = _cli(tmp_path, "-", "--duckdb", str(database), "-o", str(out), stdin=MULTILINE.encode("utf-8"))
    assert done.returncode == 0, done.stdout
    assert done.stderr == b""
    summary = _summary(done.stdout)
    assert list(summary) == SUMMARY_KEYS
    assert summary["status"] == "ok" and summary["rows"] == 2 and summary["truncated"] is False  # the WHERE on line 5 applied
    assert summary["output"] == str(out.resolve()) and summary["format"] == "json"
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["rows"] == [[2, "bob"], [3, "ann"]]


def test_multiline_sql_file_with_windows_line_ends_and_a_bom(tmp_path, database):
    sql = tmp_path / "query.sql"
    sql.write_bytes(codecs.BOM_UTF8 + MULTILINE.replace("\n", "\r\n").encode("utf-8"))
    out = tmp_path / "result.csv"
    done = _cli(tmp_path, str(sql), "--duckdb", str(database), "-o", str(out))
    assert done.returncode == 0 and done.stderr == b"", done.stdout
    summary = _summary(done.stdout)
    assert summary["rows"] == 2 and summary["format"] == "csv"
    assert out.read_bytes() == b"id,name\r\n2,bob\r\n3,ann\r\n"


def test_query_run_is_an_alias_of_q_run(tmp_path):
    done = _cli(tmp_path, "-", "--duckdb", ":memory:", "--dry-run", stdin=b"SELECT 1 AS x", command="query")
    assert done.returncode == 0 and done.stderr == b""
    assert _summary(done.stdout)["status"] == "dry_run"


def _shells(sql_file: Path, out: Path) -> dict[str, list[str] | str]:
    python = sys.executable
    args = ["-m", "kumosql", "q", "run", str(sql_file), "--duckdb", ":memory:", "-o", str(out)]
    found: dict[str, list[str] | str] = {}
    if sys.platform == "win32":
        if shutil.which("cmd"):  # cmd /c "<line>" drops the outer quotes and runs the line as typed
            found["cmd"] = f'cmd /d /c "{subprocess.list2cmdline([python, *args])}"'
        for name in ("powershell", "pwsh"):
            if shutil.which(name):
                script = "& " + " ".join("'" + part.replace("'", "''") + "'" for part in [python, *args])
                encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
                found[name] = [name, "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded]
                break
    else:
        for name in ("bash", "sh"):
            if shutil.which(name):
                import shlex

                found[name] = [name, "-c", shlex.join([python, *args])]
    return found


@pytest.mark.slow  # three shells start Python three times (about 12 s): the periodic full run and `pytest -m slow` run it
def test_the_same_command_prints_the_same_summary_in_every_shell(tmp_path):
    sql_file = tmp_path / "query.sql"
    sql_file.write_text("SELECT 1 AS one,\n       'two' AS two\n", encoding="utf-8")
    out = tmp_path / "out.json"
    shells = _shells(sql_file, out)
    if not shells:
        pytest.skip("no shell to compare")
    direct = subprocess.run([sys.executable, "-m", "kumosql", "q", "run", str(sql_file), "--duckdb", ":memory:", "-o", str(out)],
                            capture_output=True, env=_env(tmp_path), timeout=120)
    expected = _summary(direct.stdout)
    assert direct.returncode == 0 and expected["rows"] == 1
    for name, command in shells.items():
        done = subprocess.run(command, capture_output=True, env=_env(tmp_path), timeout=120)
        assert done.returncode == 0, (name, done.stdout, done.stderr)
        assert done.stderr == b"", name
        assert done.stdout.decode("ascii").splitlines() == direct.stdout.decode("ascii").splitlines(), name


# ------------------------------------------------------------------- in process


def _run(*argv: str, stdin: bytes = b"", bigquery=None) -> tuple[int, dict, bytes]:
    out, err = io.BytesIO(), io.BytesIO()
    code = run(list(argv), stdin=io.BytesIO(stdin), stdout=out, stderr=err, bigquery=bigquery)
    summary = _summary(out.getvalue())
    assert summary["exit_code"] == code
    if code == 0:
        assert err.getvalue() == b""
    else:
        lines = err.getvalue().decode("ascii").splitlines()
        assert len(lines) == 1 and lines[0].startswith("q: "), lines  # one short line, never a traceback
    return code, summary, err.getvalue()


FIELDS = [
    {"name": "id", "type": "INTEGER", "mode": "NULLABLE"},
    {"name": "name", "type": "STRING", "mode": "NULLABLE"},
    {"name": "at", "type": "TIMESTAMP", "mode": "NULLABLE"},
    {"name": "tags", "type": "STRING", "mode": "REPEATED"},
    {"name": "place", "type": "RECORD", "mode": "NULLABLE", "fields": [{"name": "city", "type": "STRING"}, {"name": "ok", "type": "BOOLEAN"}]},
    {"name": "price", "type": "NUMERIC", "mode": "NULLABLE"},
]


def _bq_row(i: int) -> dict:
    return {"f": [{"v": str(i)}, {"v": f"name {i}"}, {"v": "1700000000123456"}, {"v": [{"v": "a"}, {"v": "b"}]},
                  {"v": {"f": [{"v": "Oslo"}, {"v": "true"}]}}, {"v": "1.10"}]}


def _dry(bytes_processed: int = 1234, accuracy: str | None = "PRECISE", statement: str = "SELECT") -> tuple[int, dict]:
    query = {"totalBytesProcessed": str(bytes_processed), "statementType": statement, "schema": {"fields": FIELDS}}
    if accuracy:
        query["totalBytesProcessedAccuracy"] = accuracy
    return 200, {"statistics": {"totalBytesProcessed": str(bytes_processed), "query": query}}


def _done(rows: int = 2, total: int | None = None, token: str | None = None) -> tuple[int, dict]:
    payload = {"jobReference": {"jobId": "job_123", "location": "EU"}, "jobComplete": True, "totalRows": str(total or rows),
               "totalBytesProcessed": "1234", "totalBytesBilled": "10485760", "cacheHit": False,
               "schema": {"fields": FIELDS}, "rows": [_bq_row(i) for i in range(1, rows + 1)]}
    if token:
        payload["pageToken"] = token
    return 200, payload


def _error(status: int, reason: str, message: str) -> tuple[int, dict]:
    return status, {"error": {"code": status, "message": message, "errors": [{"reason": reason, "message": message}]}}


class FakeBigQuery:
    """Scripted BigQuery REST answers. ``dry``, ``query`` and each of ``pages`` is ``(status, payload)`` or an exception."""

    def __init__(self, dry=None, query=None, pages=()):
        self.dry, self.query, self.pages = dry or _dry(), query or _done(), list(pages)
        self.calls: list[tuple[str, str, dict | None]] = []

    @staticmethod
    def _answer(item):
        if isinstance(item, BaseException):
            raise item
        return item

    def post(self, url, headers, body):
        payload = json.loads(body)
        assert headers["Authorization"] == "Bearer test-token"
        self.calls.append(("POST", url, payload))
        return self._answer(self.dry if payload.get("configuration", {}).get("dryRun") else self.query)

    def get(self, url, headers):
        self.calls.append(("GET", url, None))
        return self._answer(self.pages.pop(0))

    def runner(self, project, **options):
        return BigQueryRunner(project, post=self.post, get=self.get, token=lambda: "test-token", clock=self.clock, **options)

    ticks = 0.0

    def clock(self) -> float:
        self.ticks += 100.0  # every reading is 100 seconds later, so a deadline passes without waiting
        return self.ticks

    @property
    def jobs(self) -> list:
        return [call for call in self.calls if not (call[2] or {}).get("configuration", {}).get("dryRun") and call[0] == "POST"]


BQ = ("--project", "billing-project", "--location", "EU", "--max-bytes", "1GB")


def test_bigquery_success_writes_typed_json_and_a_fixed_summary(tmp_path, home):
    fake = FakeBigQuery()
    out = tmp_path / "r.json"
    code, summary, _ = _run("run", "-", *BQ, "-o", str(out), stdin=b"SELECT *\nFROM sales.orders", bigquery=fake.runner)
    assert code == 0 and list(summary) == SUMMARY_KEYS
    assert summary["status"] == "ok" and summary["rows"] == 2 and summary["job_id"] == "job_123"
    assert summary["bytes_estimated"] == 1234 and summary["bytes_accuracy"] == "exact" and summary["within_cap"] is True
    assert summary["bytes_billed"] == 10485760 and summary["max_bytes_billed"] == 1_000_000_000
    assert summary["checked"] == ["read_only", "static", "dry_run"]
    job = fake.jobs[0][2]
    assert job["maximumBytesBilled"] == "1000000000" and job["useLegacySql"] is False and job["location"] == "EU"
    assert job["query"] == "SELECT *\nFROM sales.orders"
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["columns"][3] == {"name": "tags", "type": "ARRAY<STRING>"}
    assert data["rows"][0] == [1, "name 1", "2023-11-14T22:13:20.123456Z", ["a", "b"], {"city": "Oslo", "ok": True}, "1.10"]


def test_bigquery_csv_output_flattens_nested_values(tmp_path, home):
    out = tmp_path / "r.csv"
    code, summary, _ = _run("run", "-", *BQ, "-o", str(out), stdin=b"SELECT 1", bigquery=FakeBigQuery().runner)
    assert code == 0 and summary["format"] == "csv"
    lines = out.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "id,name,at,tags,place,price"
    assert lines[1] == '1,name 1,2023-11-14T22:13:20.123456Z,"[""a"",""b""]","{""city"":""Oslo"",""ok"":true}",1.10'


def test_rows_past_the_cap_are_counted_not_fetched(tmp_path, home):
    fake = FakeBigQuery(query=_done(rows=2, total=5, token="next"), pages=[_done(rows=2, total=5, token="more")])
    out = tmp_path / "r.json"
    code, summary, _ = _run("run", "-", *BQ, "-o", str(out), "--max-rows", "3", stdin=b"SELECT 1", bigquery=fake.runner)
    assert code == 0 and summary["rows"] == 3 and summary["total_rows"] == 5 and summary["truncated"] is True
    assert len([c for c in fake.calls if c[0] == "GET"]) == 1  # the third row came from the second page; no third page
    assert "maxResults=1" in fake.calls[-1][1] and "pageToken=next" in fake.calls[-1][1]
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["truncated"] is True and data["row_count"] == 3 and data["max_rows"] == 3


@pytest.mark.parametrize("accuracy, expected", [("PRECISE", "exact"), ("UPPER_BOUND", "upper_bound"),
                                                ("LOWER_BOUND", "lower_bound"), (None, "unknown")])
def test_dry_run_says_whether_the_bytes_are_exact_or_a_bound(tmp_path, home, accuracy, expected):
    fake = FakeBigQuery(dry=_dry(5_000_000, accuracy))
    code, summary, _ = _run("run", "-", *BQ, "--dry-run", stdin=b"SELECT id FROM sales.orders", bigquery=fake.runner)
    assert code == 0 and summary["status"] == "dry_run"
    assert summary["bytes_estimated"] == 5_000_000 and summary["bytes_accuracy"] == expected
    assert summary["output"] is None and fake.jobs == []  # planned only: no job, no file
    assert list(tmp_path.glob("*.json")) == []


def test_dry_run_over_the_cap_still_reports_and_a_real_run_is_refused(tmp_path, home):
    fake = FakeBigQuery(dry=_dry(50_000_000_000, "UPPER_BOUND"))
    code, summary, _ = _run("run", "-", *BQ, "--dry-run", stdin=b"SELECT 1", bigquery=fake.runner)
    assert code == 0 and summary["within_cap"] is False and summary["bytes_accuracy"] == "upper_bound"
    code, summary, err = _run("run", "-", *BQ, "-o", str(tmp_path / "r.json"), stdin=b"SELECT 1", bigquery=fake.runner)
    assert code == query_runner.EXIT_BYTE_CAP and summary["error"]["kind"] == "byte_cap"
    assert "at most 50.0 GB" in summary["error"]["message"] and "--max-bytes" in summary["error"]["message"]
    assert fake.jobs == []


def test_raising_the_cap_lets_the_query_run(tmp_path, home):
    fake = FakeBigQuery(dry=_dry(50_000_000_000, "UPPER_BOUND"))
    code, summary, _ = _run("run", "-", "--project", "p", "--location", "EU", "--max-bytes", "100GB",
                            "-o", str(tmp_path / "r.json"), stdin=b"SELECT 1", bigquery=fake.runner)
    assert code == 0 and fake.jobs[0][2]["maximumBytesBilled"] == "100000000000"


@pytest.mark.parametrize("answer, code, kind", [
    (_error(400, "invalidQuery", "Unrecognized name: nope at [1:8]"), 2, "sql"),
    (_error(404, "notFound", "Not found: Table p:sales.orders"), 2, "sql"),
    (_error(401, "authError", "Request had invalid authentication credentials."), 3, "auth"),
    (_error(403, "accessDenied", "Access Denied: Table p:sales.orders"), 3, "auth"),
    (_error(503, "backendError", "Backend error"), 4, "network"),
    (_error(403, "rateLimitExceeded", "Exceeded rate limits"), 4, "network"),
    (urllib.error.URLError("getaddrinfo failed"), 4, "network"),
    (TimeoutError("timed out"), 4, "network"),
])
def test_each_dry_run_failure_has_its_exit_code(tmp_path, home, answer, code, kind):
    fake = FakeBigQuery(dry=answer)
    got, summary, err = _run("run", "-", *BQ, "-o", str(tmp_path / "r.json"), stdin=b"SELECT nope FROM sales.orders", bigquery=fake.runner)
    assert got == code and summary["error"]["kind"] == kind and summary["status"] == "error"
    assert list(summary) == SUMMARY_KEYS
    assert fake.jobs == [] and not (tmp_path / "r.json").exists()
    assert f"q: {kind} error (exit {code}): " in err.decode("ascii")


@pytest.mark.parametrize("answer, code", [
    (_error(400, "bytesBilledLimitExceeded", "Query exceeded limit for bytes billed: 1000000000."), 5),
    (_error(400, "resourcesExceeded", "Resources exceeded during query execution"), 2),
    (_error(500, "internalError", "An internal error occurred"), 4),
    (ConnectionResetError("connection reset"), 4),
])
def test_each_job_failure_has_its_exit_code(tmp_path, home, answer, code):
    fake = FakeBigQuery(query=answer)
    got, summary, _ = _run("run", "-", *BQ, "-o", str(tmp_path / "r.json"), stdin=b"SELECT 1", bigquery=fake.runner)
    assert got == code and len(fake.jobs) == 1


def test_no_credentials_is_an_auth_error_and_nothing_is_sent(tmp_path, home):
    calls = []

    def runner(project, **options):
        def token():
            raise query_runner.AuthError("no BigQuery credentials: set BQ_ACCESS_TOKEN or run gcloud auth application-default login")

        return BigQueryRunner(project, post=lambda *a: calls.append(a), get=lambda *a: calls.append(a), token=token, **options)

    code, summary, _ = _run("run", "-", *BQ, stdin=b"SELECT 1", bigquery=runner)
    assert code == 3 and summary["error"]["kind"] == "auth" and calls == []


def test_a_job_that_does_not_finish_in_time_is_a_network_error_naming_the_job(tmp_path, home):
    running = (200, {"jobReference": {"jobId": "job_slow", "location": "EU"}, "jobComplete": False})
    fake = FakeBigQuery(query=running, pages=[running] * 5)
    code, summary, _ = _run("run", "-", *BQ, "--timeout", "150", "-o", str(tmp_path / "r.json"), stdin=b"SELECT 1", bigquery=fake.runner)
    assert code == 4 and "job_slow" in summary["error"]["message"] and "150 seconds" in summary["error"]["message"]


@pytest.mark.parametrize("argv, stdin, needle", [
    (("run",), b"", "FILE|-"),
    (("run", "missing.sql", *BQ), b"", "no SQL file"),
    (("run", "SELECT 1 FROM t", *BQ), b"", "not in an argument"),
    (("run", "-", *BQ), b"   \n", "empty"),
    (("run", "-", *BQ), b"\xff\xfeS\x00\xff", "UTF"),
    (("run", "-", "--project", "p", "--max-bytes", "5MB"), b"SELECT 1", "--max-bytes"),
    (("run", "-", "--max-rows", "0", *BQ), b"SELECT 1", "--max-rows"),
    (("run", "-", "--bogus"), b"SELECT 1", "unrecognized"),
    (("run", "-", "--timeout", "0", *BQ), b"SELECT 1", "--timeout"),
    (("run", "-", "--max-bytes", "1GB", "--location", "EU"), b"SELECT 1", "no billing project"),
])
def test_usage_errors_have_their_own_code(tmp_path, home, monkeypatch, argv, stdin, needle):
    monkeypatch.chdir(tmp_path)
    fake = FakeBigQuery()
    code, summary, _ = _run(*argv, stdin=stdin, bigquery=fake.runner)
    assert code == query_runner.EXIT_USAGE and summary["error"]["kind"] == "usage"
    assert needle in summary["error"]["message"] and fake.calls == []


def test_help_lists_every_exit_code(capsys):
    assert run(["run", "--help"]) == 0
    text = " ".join(capsys.readouterr().out.split())
    for code, meaning in query_runner.EXIT_CODES.items():
        assert f"{code} {' '.join(meaning.split())}" in text


# ------------------------------------------------------------------- the SQL


def test_byte_order_marks_and_line_ends_are_removed():
    sql = "SELECT 1,\r\n  2\r\n"
    assert query_runner.decode_sql(codecs.BOM_UTF8 + sql.encode("utf-8")) == "SELECT 1,\n  2"
    assert query_runner.decode_sql(codecs.BOM_UTF16_LE + sql.encode("utf-16-le")) == "SELECT 1,\n  2"  # PowerShell 5.1 Out-File
    assert query_runner.decode_sql(codecs.BOM_UTF16_BE + sql.encode("utf-16-be")) == "SELECT 1,\n  2"


def test_a_bom_sql_file_reaches_bigquery_without_it(tmp_path, home):
    sql = tmp_path / "q.sql"
    sql.write_bytes(codecs.BOM_UTF8 + b"SELECT 1 AS x\r\n")
    fake = FakeBigQuery()
    code, _, _ = _run("run", str(sql), *BQ, "--dry-run", bigquery=fake.runner)
    assert code == 0 and fake.calls[0][2]["configuration"]["query"]["query"] == "SELECT 1 AS x"


@pytest.mark.parametrize("sql", [
    "DELETE FROM sales.orders WHERE TRUE",
    "INSERT INTO sales.orders (id) VALUES (1)",
    "CREATE TABLE sales.copy AS SELECT * FROM sales.orders",
    "MERGE sales.orders t USING sales.new s ON t.id = s.id WHEN MATCHED THEN DELETE",
    "SELECT 1; SELECT 2",
    "DECLARE x INT64 DEFAULT 1; SELECT x",
    "DROP TABLE sales.orders",
    "EXPORT DATA OPTIONS (uri = 'gs://bucket/*.csv', format = 'CSV') AS SELECT 1 AS x",
])
def test_anything_but_one_select_is_refused_before_any_request(tmp_path, home, sql):
    fake = FakeBigQuery()
    code, summary, _ = _run("run", "-", *BQ, stdin=sql.encode(), bigquery=fake.runner)
    assert code == query_runner.EXIT_SQL and summary["error"]["kind"] == "sql"
    assert fake.calls == []


def test_a_statement_bigquery_does_not_call_select_is_refused_after_the_dry_run(tmp_path, home):
    fake = FakeBigQuery(dry=_dry(statement="SCRIPT"))
    code, summary, _ = _run("run", "-", *BQ, stdin=b"SELECT 1", bigquery=fake.runner)
    assert code == 2 and "SCRIPT" in summary["error"]["message"] and fake.jobs == []


def test_sql_sqlglot_cannot_parse_is_left_to_the_dry_run_and_listed_as_not_checked(tmp_path, home, monkeypatch):
    def broken(sql, **kw):
        raise ValueError("Invalid expression / Unexpected token. Line 1, Col: 9.")

    monkeypatch.setattr("kumo_sql_prover.ast_utils.parse_statements", broken)
    fake = FakeBigQuery()
    code, summary, _ = _run("run", "-", *BQ, "--dry-run", stdin=b"SELECT 1", bigquery=fake.runner)
    assert code == 0 and summary["checked"] == ["dry_run"]
    assert "sqlglot could not parse" in summary["not_checked"][0]
    fake = FakeBigQuery(dry=_dry(statement="INSERT"))
    code, summary, _ = _run("run", "-", *BQ, stdin=b"SELECT 1", bigquery=fake.runner)
    assert code == 2 and fake.jobs == []


# ------------------------------------------------------------------ pre-flight


def test_a_reserved_word_alias_is_caught_before_any_request(tmp_path, home):
    fake = FakeBigQuery()
    code, summary, _ = _run("run", "-", *BQ, stdin=b"SELECT id AS new\nFROM sales.orders", bigquery=fake.runner)
    assert code == 2 and "NEW is a reserved" in summary["error"]["message"] and "backticks" in summary["error"]["message"]
    assert summary["checked"] == ["read_only"] and fake.calls == []  # no dry run, no job, no credentials asked for
    code, _, _ = _run("run", "-", *BQ, "--no-preflight", "--dry-run", stdin=b"SELECT id AS new FROM sales.orders", bigquery=fake.runner)
    assert code == 0  # the escape hatch hands the question to BigQuery's dry run


def test_a_warning_from_the_static_checks_does_not_stop_the_run(tmp_path, home):
    fake = FakeBigQuery()
    code, summary, _ = _run("run", "-", *BQ, "--dry-run", stdin=b"SELECT id FROM sales.orders WHERE id NOT IN (1, NULL)",
                            bigquery=fake.runner)
    assert code == 0 and summary["warnings"] and summary["warnings"][0].startswith("not_in_null")


def _snapshot(tmp_path: Path) -> Path:
    path = tmp_path / "columns.csv"
    path.write_text("table_catalog,table_schema,table_name,column_name,data_type\n"
                    "p,sales,orders,id,INT64\np,sales,orders,amount,NUMERIC\n", encoding="utf-8")
    return path


def test_an_unknown_column_is_caught_from_a_schema_snapshot_before_any_request(tmp_path, home):
    fake = FakeBigQuery()
    snapshot = _snapshot(tmp_path)
    code, summary, _ = _run("run", "-", *BQ, "--schema-snapshot", str(snapshot), stdin=b"SELECT id, amout FROM sales.orders",
                            bigquery=fake.runner)
    assert code == 2 and "amout" in summary["error"]["message"] and fake.calls == []
    code, summary, _ = _run("run", "-", *BQ, "--schema-snapshot", str(snapshot), "--dry-run",
                            stdin=b"SELECT id, _PARTITIONTIME AS t FROM sales.orders", bigquery=fake.runner)
    assert code == 0 and "schema_snapshot" in summary["checked"]
    assert any("_PARTITIONTIME" in note for note in summary["not_checked"])  # a pseudo-column is never in a snapshot
    code, summary, _ = _run("run", "-", *BQ, "--schema-snapshot", str(snapshot), "--dry-run",
                            stdin=b"SELECT o.id, i.sku FROM sales.orders AS o JOIN sales.items AS i USING (id)", bigquery=fake.runner)
    assert code == 0 and any("not in the schema snapshot: sales.items" in note for note in summary["not_checked"])


def test_unknown_columns_on_duckdb_are_caught_when_it_binds_before_running(tmp_path, database):
    code, summary, _ = _run("run", "-", "--duckdb", str(database), "-o", str(tmp_path / "r.json"), stdin=b"SELECT nope FROM orders")
    assert code == 2 and "nope" in summary["error"]["message"] and not (tmp_path / "r.json").exists()


# ------------------------------------------------------------------- summarize


def test_summarize_gives_column_stats_and_the_first_rows(tmp_path, database):
    code, summary, _ = _run("run", "-", "--duckdb", str(database), "-o", str(tmp_path / "r.json"), "--summarize", "--head", "2",
                            stdin=b"SELECT id, name, amount, day FROM orders ORDER BY id")
    assert code == 0 and summary["summary_truncated"] is False
    stats = {entry["name"]: entry for entry in summary["column_stats"]}
    assert stats["id"] == {"name": "id", "type": "INTEGER", "nulls": 0, "distinct": 4, "min": 1, "max": 4, "mean": 2.5}
    assert stats["name"]["top"][0] == {"value": "ann", "count": 2} and stats["name"]["nulls"] == 1
    assert stats["day"]["min"] == "2024-01-01" and stats["day"]["max"] == "2024-03-01"
    assert summary["head"] == {"columns": ["id", "name", "amount", "day"], "rows": [[1, "ann", "2.50", "2024-01-01"], [2, "bob", "4.00", "2024-02-01"]]}


def test_summarize_never_exceeds_its_size_cap(tmp_path):
    columns = ", ".join(f"repeat('x', 500) || CAST(i AS VARCHAR) AS column_with_a_long_name_{n}" for n in range(30))
    sql = f"SELECT {columns} FROM range(200) t(i)".encode()
    out = io.BytesIO()
    code = run(["run", "-", "--duckdb", ":memory:", "-o", str(tmp_path / "r.json"), "--summarize", "--head", "50",
                "--summary-bytes", "1500"], stdin=io.BytesIO(sql), stdout=out, stderr=io.BytesIO())
    line = out.getvalue()
    assert code == 0 and len(line) <= 1500 + 1
    summary = json.loads(line)
    assert summary["summary_truncated"] is True and summary["summary_omitted"]["head_rows"] > 0
    assert summary["rows"] == 200 and summary["output"]  # the facts an agent needs survive the cut
    assert len(json.loads((tmp_path / "r.json").read_text(encoding="utf-8"))["rows"]) == 200  # the file keeps everything


def test_summary_cap_is_bounded(tmp_path, home):
    code, summary, _ = _run("run", "-", "--duckdb", ":memory:", "--summary-bytes", "999999", stdin=b"SELECT 1")
    assert code == query_runner.EXIT_USAGE


def test_default_output_goes_to_a_new_file_in_the_temp_folder(tmp_path, monkeypatch):
    monkeypatch.setattr(query_runner.tempfile, "gettempdir", lambda: str(tmp_path))
    code, summary, _ = _run("run", "-", "--duckdb", ":memory:", stdin=b"SELECT 1 AS x")
    path = Path(summary["output"])
    assert code == 0 and path.parent == tmp_path / "kumosql-results" and path.suffix == ".json"
    assert json.loads(path.read_text(encoding="utf-8"))["rows"] == [[1]]
