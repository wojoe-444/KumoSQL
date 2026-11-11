"""Local browser UI for the registered SQL rewrite rules."""

from __future__ import annotations

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
import json
import re
import secrets
import sys
import threading
import webbrowser
from urllib.parse import parse_qs, urlsplit

from . import console, live_graph, redact
from . import live_insights
from . import bigquery_catalog, catalogs, data_sources, scope_queries
from . import scopes as scope_store
from . import state, tags, version
from .formatting import FormatSqlRule, complexity, load_preferences, parse_preferences, save_preferences
from .rewrite import apply_rules, available_rules


MAX_REQUEST_BYTES = 5 * 1024 * 1024
MAX_GITHUB_REQUEST_BYTES = 8 * 1024 * 1024
MAX_UI_STATE_BYTES = 64 * 1024
MAX_JOBS_REQUEST_BYTES = 64 * 1024 * 1024
SESSION_HEADER = "X-KumoSQL-Session"
# Insight views. Each returns a JSON payload built from what the server has
# loaded (project, job history, change comparison); with nothing loaded the
# payload is an empty state that says what to load (see docs/ui-roadmap.md).
def _cost(scope: str | None = None, query: dict | None = None) -> dict:
    rate = (query or {}).get("rate", [""])[0]
    try:
        value = float(rate) if rate else None
    except ValueError:
        raise ValueError("price per TiB must be a number") from None
    return live_insights.cost_payload(scope, value)


INSIGHTS = {
    "/api/graph": lambda scope=None, query=None: live_graph.graph_or_empty(scope),
    "/api/cost": _cost,
    "/api/changes": lambda scope=None, query=None: live_insights.changes_payload(scope),
}
ASSETS = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/browse": ("browse.html", "text/html; charset=utf-8"),
    "/browse/": ("browse.html", "text/html; charset=utf-8"),
    "/refactor": ("refactor.html", "text/html; charset=utf-8"),
    "/shared-models": ("shared-models.html", "text/html; charset=utf-8"),
    "/assets/shared-models.js": ("shared-models.js", "text/javascript; charset=utf-8"),
    "/assets/shared-models.css": ("shared-models.css", "text/css; charset=utf-8"),
    "/reduce": ("reduce.html", "text/html; charset=utf-8"),
    "/assets/reduce.js": ("reduce.js", "text/javascript; charset=utf-8"),
    "/assets/reduce.css": ("reduce.css", "text/css; charset=utf-8"),
    "/assets/patch-view.js": ("patch-view.js", "text/javascript; charset=utf-8"),
    "/assets/refactor.js": ("refactor.js", "text/javascript; charset=utf-8"),
    "/assets/refactor.css": ("refactor.css", "text/css; charset=utf-8"),
    "/assets/settings.js": ("settings.js", "text/javascript; charset=utf-8"),
    "/assets/scopes.js": ("scopes.js", "text/javascript; charset=utf-8"),
    "/assets/tags.js": ("tags.js", "text/javascript; charset=utf-8"),
    "/assets/catalogs.js": ("catalogs.js", "text/javascript; charset=utf-8"),
    "/assets/datasources.js": ("datasources.js", "text/javascript; charset=utf-8"),
    "/assets/style.css": ("style.css", "text/css; charset=utf-8"),
    "/assets/shell.css": ("shell.css", "text/css; charset=utf-8"),
    "/assets/shell.js": ("shell.js", "text/javascript; charset=utf-8"),
    "/assets/session.js": ("session.js", "text/javascript; charset=utf-8"),
    "/assets/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/favicon.svg": ("favicon.svg", "image/svg+xml"),
    "/assets/evidence.js": ("evidence.js", "text/javascript; charset=utf-8"),
    "/graph": ("insights.html", "text/html; charset=utf-8"),
    "/cost": ("insights.html", "text/html; charset=utf-8"),
    "/changes": ("insights.html", "text/html; charset=utf-8"),
    "/assets/insights.css": ("insights.css", "text/css; charset=utf-8"),
    "/assets/vendor/cytoscape.min.js": ("vendor/cytoscape.min.js", "text/javascript; charset=utf-8"),
    "/assets/lineage-view.js": ("lineage-view.js", "text/javascript; charset=utf-8"),
    "/assets/insights.js": ("insights.js", "text/javascript; charset=utf-8"),
    "/assets/browse.css": ("browse.css", "text/css; charset=utf-8"),
    "/assets/bqprojects.js": ("bqprojects.js", "text/javascript; charset=utf-8"),
    "/assets/browse.js": ("browse.js", "text/javascript; charset=utf-8"),
    "/assets/background.jpg": ("background.jpg", "image/jpeg"),
}


def _complexity(sql: str) -> dict | None:
    try:
        return complexity(sql).to_json()
    except Exception:
        return None


def transform(sql: str, names: list[str], format_preferences: object = None) -> dict:
    """Apply selected rules and return a JSON-ready report."""

    if not isinstance(sql, str):
        raise ValueError("sql must be a string")
    if not isinstance(names, list) or not names or any(not isinstance(name, str) for name in names):
        raise ValueError("select at least one transformation")
    known = available_rules()
    if len(names) > len(known) or len(set(names)) != len(names) or any(name not in known for name in names):
        raise ValueError("unknown or duplicate transformation")
    if not sql.strip():
        raise ValueError("paste SQL or SQLX to transform")

    overrides = None
    if format_preferences is not None:
        overrides = {"format_sql": FormatSqlRule(parse_preferences(format_preferences))}
    result = apply_rules(names, sql, overrides=overrides)
    rule_success = all(step.rule_success for step in result.steps)
    candidate_sql = result.sql if rule_success else ""
    return {
        "complexity": {
            "before": _complexity(sql),
            "after": _complexity(candidate_sql) if candidate_sql else None,
        },
        "sql": candidate_sql,
        "success": result.success,
        "rule_success": rule_success,
        "verification": result.verification.to_json(),
        "steps": [
            {
                "rule": step.rule,
                "changes": step.changes,
                "success": step.success,
                "rule_success": step.rule_success,
                "verification": step.verification.to_json(),
                "diagnostics": [
                    {"code": item.code, "message": item.message, "statement_index": item.statement_index}
                    for item in step.diagnostics
                ],
            }
            for step in result.steps
        ],
    }


class UIServer(ThreadingHTTPServer):
    """Threaded, with its errors logged to a file: the console may be frozen by a click."""

    daemon_threads = True

    def __init__(self, *args, **kwargs) -> None:
        # Kept only in memory, and replaced whenever the UI server starts.
        self.session_token = secrets.token_urlsafe(32)
        # Analysis is CPU-bound Python. With the default 5 ms switch interval it can hold the
        # interpreter long enough that a trivial request (Settings, the BigQuery tab) waits
        # seconds; a shorter interval keeps them under ~50 ms at a cost of about 5% build time.
        sys.setswitchinterval(min(sys.getswitchinterval(), 0.0005))
        super().__init__(*args, **kwargs)

    def handle_error(self, request, client_address) -> None:
        exc = sys.exc_info()[1]
        if isinstance(exc, (BrokenPipeError, ConnectionResetError, ConnectionAbortedError)):
            return  # the browser closed the page or cancelled a request mid-answer; not a server fault
        console.error("request handling failed (the browser got no answer)", exc, code="KS-REQUEST")


class UIHandler(BaseHTTPRequestHandler):
    """Serve bundled assets and a small same-origin JSON API."""

    def parse_request(self) -> bool:
        if not super().parse_request():
            return False
        # Check before dispatch, including assets and unsupported HTTP methods.
        port = self.server.server_port
        authorities = {f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}"}
        hosts = self.headers.get_all("Host", [])
        if len(hosts) != 1 or hosts[0].lower() not in authorities:
            self._json(403, {"error": "use the local UI server address"})
            return False
        origins = self.headers.get_all("Origin", [])
        if self.command in {"PUT", "POST", "DELETE"} and origins:
            if len(origins) != 1 or origins[0].lower() != f"http://{hosts[0].lower()}":
                self._json(403, {"error": "request origin must match the local UI server"})
                return False
        route = self.path.split("?", 1)[0]
        if route == "/api" or route.startswith("/api/"):
            tokens = self.headers.get_all(SESSION_HEADER, [])
            expected = getattr(self.server, "session_token", None)  # a plain server has none: fail closed
            if expected is None or len(tokens) != 1 or not secrets.compare_digest(
                tokens[0].encode("utf-8"), expected.encode("ascii")
            ):
                self._json(403, {"error": "reload the UI page for the current session"})
                return False
        return True

    def log_message(self, format: str, *args) -> None:  # noqa: A002 - signature set by the base class
        # Query values can hold names (a project, a table); only their keys are logged.
        text = re.sub(r"\?\S*", lambda m: "?" + ",".join(part.split("=")[0] for part in m.group(0)[1:].split("&")), format % args)
        console.say(f"{self.address_string()} {text}", console=console.VERBOSE)

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
            "img-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
        )
        self.send_header("X-Frame-Options", "DENY")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, payload: dict | list) -> None:
        self._send(status, json.dumps(payload).encode("utf-8"), "application/json; charset=utf-8")

    def _read_json(self, limit: int = MAX_REQUEST_BYTES):
        """Read a JSON request body; sends the error response and returns None on failure."""

        if self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() != "application/json":
            self._json(415, {"error": "send application/json"})
            return None
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            self._json(411, {"error": "Content-Length is required"})
            return None
        if length < 0 or length > limit:
            self._json(413, {"error": "request is too large"})
            return None
        try:
            return json.loads(self.rfile.read(length))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            self._json(400, {"error": str(exc)})
            return None

    def do_GET(self) -> None:
        if self.path == "/api/version":
            self._json(200, version.info())
            return
        if self.path == "/api/diagnostics":
            self._json(200, {"text": console.diagnostics(), "redacted": True})
            return
        if self.path == "/api/status":
            self._json(200, live_graph.server_status())
            return
        if self.path == "/api/refactor/status":
            from . import refactor

            self._json(200, refactor.job_status())
            return
        if self.path == "/api/refactor":
            from . import refactor

            self._json(200, refactor.classes_view())
            return
        if self.path == "/api/shared-models":
            from . import shared_models

            self._json(200, shared_models.repeated_payload())
            return
        if self.path == "/api/reduce":
            from . import reduction_app

            self._json(200, reduction_app.reduction_payload())
            return
        if self.path == "/api/reduce/status":
            from . import reduction_app

            self._json(200, reduction_app.job_status())
            return
        if self.path == "/api/equivalences":
            from . import equivalences

            self._json(200, {"equivalences": [item.to_json() for item in equivalences.load()]})
            return
        if self.path == "/api/prover":
            from . import prover_context

            self._json(200, prover_context.status())
            return
        if self.path == "/api/lineage-limits":
            from . import lineage_limits

            self._json(200, lineage_limits.status())
            return
        if self.path == "/api/schema-fetch":
            from . import schema_fetch

            self._json(200, schema_fetch.settings())
            return
        if self.path == "/api/settings":
            self._json(200, {
                "ui": state.get_section("ui", {}),
                "format": load_preferences().to_json(),
                "scopes": [scope.to_json() for scope in scope_store.list_scopes()],
                "tag_rules": tags.list_rules(),
                "catalogs": catalogs.list_catalogs(builtin=False),
                "data_sources": [source.to_json() for source in data_sources.list_sources()],
            })
            return
        if self.path == "/api/data-sources":
            self._json(200, {
                "types": [{"key": key, "label": label} for key, label in data_sources.TYPES.items()],
                "sources": data_sources.describe(),
                "default_cache_hours": bigquery_catalog.query_cache_hours(),
                "billing_project": scope_queries.billing_project(),
                "tag_fields": data_sources.tag_fields(),
            })
            return
        if self.path == "/api/catalogs":
            try:
                self._json(200, catalogs.snapshot())
            except Exception as exc:  # noqa: BLE001 - the page shows this instead of silently having no catalogs
                self._json(500, {"error": f"could not work out catalogs: {type(exc).__name__}: {exc}"})
            return
        if self.path == "/api/tags":
            try:
                self._json(200, tags.snapshot())
            except Exception as exc:  # noqa: BLE001 - the page shows this instead of silently having no tags
                self._json(500, {"error": f"could not work out tags: {type(exc).__name__}: {exc}"})
            return
        if self.path == "/api/scope-queries":
            self._json(200, {
                "settings": scope_queries.get_settings().to_json(),
                "billing_project": scope_queries.billing_project(),
                "cache_hours": scope_queries.cache_seconds() / 3600,
                "cached": scope_queries.cached_queries(),
            })
            return
        if self.path == "/api/storage":
            from . import storage

            self._json(200, storage.describe())
            return
        if self.path == "/api/repositories":
            from . import repositories

            self._json(200, repositories.listing())
            return
        if self.path == "/api/workflow-configs":
            from . import repositories, workflow_configs

            self._json(200, {
                "default_location": workflow_configs.default_location(),
                "repositories": {item["id"]: workflow_configs.summary(item["url"])
                                 for item in repositories.listing()["repositories"]},
            })
            return
        if self.path == "/api/scope-fields":
            self._json(200, self._scope_fields())
            return
        if self.path == "/api/sqlfluff/rules":
            from .formatting import sqlfluff_rules

            self._json(200, sqlfluff_rules())
            return
        if self.path == "/api/rules":
            self._json(200, [
                {"name": name, "summary": rule.summary}
                for name, rule in available_rules().items()
            ])
            return
        route = self.path.split("?", 1)[0]
        if route == "/api/impact":
            self._impact()
            return
        if route == "/api/overlaps":
            self._overlaps()
            return
        if route in INSIGHTS:
            query = parse_qs(urlsplit(self.path).query)
            scope = query.get("scope", [""])[0] or None
            try:
                self._json(200, INSIGHTS[route](scope, query))
            except ValueError as exc:
                self._json(400, {"error": str(exc)})
            return
        if self.path.startswith("/api/catalog/"):
            self._catalog()
            return
        asset = ASSETS.get(route)
        if asset is None:
            self._json(404, {"error": "not found"})
            return
        filename, content_type = asset
        body = files("kumosql").joinpath("static", filename).read_bytes()
        if content_type.startswith("text/html"):
            body = body.replace(b"__KUMOSQL_SESSION_TOKEN__", self.server.session_token.encode("ascii"))
        self._send(200, body, content_type)

    @staticmethod
    def _scope_fields() -> dict:
        """Fields a scope rule can use, discovered from the loaded project and its job history."""

        loaded = live_graph.loaded()
        fields = scope_store.discover_fields(
            loaded["pipeline"] if loaded else None,
            loaded["observed_reads"] if loaded else (),
            _profiles(loaded["pipeline"]) if loaded else None,
        )
        from . import data_sources

        for source in data_sources.list_sources():
            rows = data_sources.records(source)
            for column in data_sources.columns_of(source):
                examples = tuple(dict.fromkeys(str(r[column]) for r in rows[:200] if r.get(column)))[:5]
                fields.append(scope_store.FieldInfo(column, source.name, "text", examples))
        known = {info.name.casefold() for info in fields}
        # Fields of saved scopes stay editable even when their data is not loaded.
        for scope in scope_store.list_scopes():
            for name in scope.fields_used():
                if name.casefold() not in known:
                    known.add(name.casefold())
                    fields.append(scope_store.FieldInfo(name, "saved"))
        return {
            "fields": [info.to_json() for info in fields],
            "operators": [{"op": op, "label": label} for op, label in scope_store.OPERATORS.items()],
            "domains": [{"key": key, "label": label} for key, label in scope_store.all_domains().items()],
            "loaded": loaded["label"] if loaded else None,
        }

    def _impact(self) -> None:
        query = parse_qs(urlsplit(self.path).query)
        try:
            payload = live_graph.impact_payload(
                _required(query, "node"), _required(query, "column"),
                query.get("change", ["drop"])[0], query.get("scope", [""])[0] or None,
            )
        except ValueError as exc:
            self._json(400, {"error": str(exc)})
            return
        self._json(200, payload)

    def _overlaps(self) -> None:
        query = parse_qs(urlsplit(self.path).query)
        try:
            payload = live_graph.overlaps_payload(
                _required(query, "node"), query.get("scope", [""])[0] or None
            )
        except ValueError as exc:
            self._json(400, {"error": str(exc)})
            return
        self._json(200, payload)

    def _put_selection(self) -> None:
        from . import bigquery_catalog

        payload = self._read_json(MAX_UI_STATE_BYTES)
        if payload is None:
            return
        try:
            projects = bigquery_catalog.select_projects(
                payload.get("projects") if isinstance(payload, dict) else None)
        except ValueError as exc:
            self._json(400, {"error": str(exc)})
            return
        except (OSError, RuntimeError) as exc:
            self._json(502, {"error": str(exc)})
            return
        self._json(200, {"projects": projects})

    def _put_bigquery_settings(self) -> None:
        from . import bigquery_catalog

        payload = self._read_json(MAX_UI_STATE_BYTES)
        if payload is None:
            return
        try:
            if not isinstance(payload, dict):
                raise ValueError("settings must be an object")
            saved = bigquery_catalog.save_settings(
                payload.get("billingProject"), payload.get("queryCacheHours"))
        except ValueError as exc:
            self._json(400, {"error": str(exc)})
            return
        except (OSError, RuntimeError) as exc:
            self._json(502, {"error": str(exc)})
            return
        self._json(200, saved)

    def _put_prover(self) -> None:
        from . import prover_context

        payload = self._read_json(MAX_UI_STATE_BYTES)
        if payload is None:
            return
        try:
            if not isinstance(payload, dict):
                raise ValueError("settings must be an object")
            prover_context.save_settings(payload.get("enabled"), payload.get("timeout_ms"), payload.get("bounded_rows"))
        except ValueError as exc:
            self._json(400, {"error": str(exc)})
            return
        except OSError as exc:
            self._json(500, {"error": str(exc)})
            return
        self._json(200, prover_context.status())

    def _put_lineage_limits(self) -> None:
        from . import lineage_limits

        payload = self._read_json(MAX_UI_STATE_BYTES)
        if payload is None:
            return
        try:
            if not isinstance(payload, dict):
                raise ValueError("settings must be an object")
            saved = lineage_limits.save_settings(
                model_seconds=payload.get("model_seconds"), total_seconds=payload.get("total_seconds")
            )
        except ValueError as exc:
            self._json(400, {"error": str(exc)})
            return
        except OSError as exc:
            self._json(500, {"error": str(exc)})
            return
        self._json(200, saved)

    def _put_schema_fetch(self) -> None:
        from . import schema_fetch

        payload = self._read_json(MAX_UI_STATE_BYTES)
        if payload is None:
            return
        try:
            if not isinstance(payload, dict):
                raise ValueError("settings must be an object")
            saved = schema_fetch.save_settings(payload.get("enabled"))
        except ValueError as exc:
            self._json(400, {"error": str(exc)})
            return
        except OSError as exc:
            self._json(500, {"error": str(exc)})
            return
        self._json(200, lineage_limits.status())

    def do_PUT(self) -> None:
        if self.path == "/api/schema-fetch":
            self._put_schema_fetch()
            return
        if self.path == "/api/lineage-limits":
            self._put_lineage_limits()
            return
        if self.path == "/api/prover":
            self._put_prover()
            return
        if self.path == "/api/catalog/settings":
            self._put_bigquery_settings()
            return
        if self.path == "/api/catalog/selection":
            self._put_selection()
            return
        if self.path == "/api/tags":
            payload = self._read_json(MAX_UI_STATE_BYTES)
            if payload is None:
                return
            try:
                if not isinstance(payload, dict) or not isinstance(payload.get("keys"), list):
                    raise ValueError("send the objects to change as keys")
                self._json(200, tags.change_manual(
                    payload["keys"], payload.get("add") or [], payload.get("remove") or []))
            except (ValueError, TypeError) as exc:
                self._json(400, {"error": str(exc)})
            except OSError as exc:
                self._json(500, {"error": str(exc)})
            return
        section = self.path.removeprefix("/api/settings/")
        if section not in ("ui", "format", "scopes", "scope_queries", "tag_rules", "catalogs", "data_sources", "refactor") or section == self.path:
            self._json(404, {"error": "not found"})
            return
        payload = self._read_json(MAX_UI_STATE_BYTES if section == "ui" else MAX_REQUEST_BYTES)
        if payload is None:
            return
        try:
            if section == "ui":
                if not isinstance(payload, dict):
                    raise ValueError("ui settings must be an object")
                state.set_section("ui", payload)
                saved = payload
            elif section == "format":
                prefs = parse_preferences(payload)
                save_preferences(prefs)
                saved = prefs.to_json()
            elif section == "scope_queries":
                saved = scope_queries.save_settings(payload).to_json()
            elif section == "tag_rules":
                saved = tags.save_rules(payload)
            elif section == "catalogs":
                saved = catalogs.save_catalogs(payload)
            elif section == "data_sources":
                saved = [source.to_json() for source in data_sources.save_sources(payload)]
            elif section == "refactor":
                from . import refactor

                saved = refactor.save_classes(payload).to_json()
            else:
                if not isinstance(payload, list):
                    raise ValueError("scopes must be a list")
                parsed = [scope_store.parse_scope(item) for item in payload]
                parsed = scope_store.save_scopes(parsed)
                saved = [scope.to_json() for scope in parsed]
        except (ValueError, OSError) as exc:
            self._json(400 if isinstance(exc, ValueError) else 500, {"error": str(exc)})
            return
        self._json(200, saved)

    def _catalog(self) -> None:
        from urllib.parse import parse_qs, urlsplit

        from . import bigquery_catalog

        route = urlsplit(self.path).path
        query = parse_qs(urlsplit(self.path).query)
        refresh = query.get("refresh", [""])[0] == "1"
        try:
            if route == "/api/catalog/selection":
                self._json(200, {"projects": bigquery_catalog.selected_projects()})
                return
            if route == "/api/catalog/settings":
                self._json(200, bigquery_catalog.bigquery_settings())
                return
            if route == "/api/catalog/projects":
                result = bigquery_catalog.cached(
                    "projects\x1fall", bigquery_catalog.list_projects, refresh)
            elif route == "/api/catalog/datasets":
                project = _required(query, "project")
                result = bigquery_catalog.cached(
                    bigquery_catalog.datasets_key(project),
                    lambda: bigquery_catalog.list_datasets(project), refresh)
            elif route == "/api/catalog/tables":
                project, dataset = _required(query, "project"), _required(query, "dataset")
                result = bigquery_catalog.cached(
                    bigquery_catalog.tables_key(project, dataset),
                    lambda: bigquery_catalog.list_tables(project, dataset), refresh)
            elif route == "/api/catalog/table":
                project, dataset = _required(query, "project"), _required(query, "dataset")
                table = _required(query, "table")
                try:
                    result = bigquery_catalog.cached(
                        f"table\x1f{project}\x1f{dataset}\x1f{table}",
                        lambda: bigquery_catalog.get_table(project, dataset, table), refresh)
                except bigquery_catalog.CatalogError as exc:
                    if exc.status not in (403, 404):
                        raise
                    # Inaccessible tables are removed from the cached list, and the page hides them.
                    bigquery_catalog.forget_table(project, dataset, table)
                    self._json(502, {"error": str(exc), "removed": True})
                    return
            else:
                self._json(404, {"error": "not found"})
                return
        except (ValueError, bigquery_catalog.CatalogError, RuntimeError) as exc:
            self._json(502, {"error": str(exc)})
            return
        self._json(200, result)

    def do_POST(self) -> None:
        if self.path not in (
            "/api/transform", "/api/github/connect", "/api/github/file", "/api/github/load",
            "/api/project/git", "/api/project", "/api/project/clear",
            "/api/jobs", "/api/jobs/clear", "/api/changes/compare", "/api/scope-queries",
            "/api/repositories", "/api/repositories/refresh", "/api/repositories/activate", "/api/repositories/clear",
            "/api/storage", "/api/workflow-configs/refresh", "/api/workflow-configs/settings",
            "/api/tag-rules/preview", "/api/catalogs/preview", "/api/catalogs/active", "/api/data-sources/populate", "/api/equivalences", "/api/equivalences/remove", "/api/prove-tables", "/api/prove-queries", "/api/consolidate-tables",
            "/api/refactor/run", "/api/refactor/cancel", "/api/shared-models/patch", "/api/reduce/run",
        ):
            self._json(404, {"error": "not found"})
            return
        payload = self._read_json(
            MAX_JOBS_REQUEST_BYTES if self.path == "/api/jobs"
            else MAX_GITHUB_REQUEST_BYTES if self.path.startswith("/api/github/") else MAX_REQUEST_BYTES
        )
        if payload is None:
            return
        try:
            if not isinstance(payload, dict):
                raise ValueError("request must be a JSON object")
            if self.path == "/api/github/connect":
                from .github_repo import connect

                result = connect(payload.get("url"))
            elif self.path in ("/api/github/load", "/api/project/git"):
                from .git_repo import load_into_graph

                result = load_into_graph(
                    payload.get("url"), payload.get("branch"), payload.get("refresh") is True)
            elif self.path == "/api/project":
                label = payload.get("label")
                live_graph.load_files(payload.get("files"), label if isinstance(label, str) else "")
                result = {"loaded": True, "label": live_graph.loaded()["label"], "files": len(payload["files"])}
            elif self.path == "/api/scope-queries":
                result = _scope_query(payload)
            elif self.path == "/api/data-sources/populate":
                source = data_sources.get_source(payload.get("id") if isinstance(payload.get("id"), str) else "")
                if source is None:
                    raise ValueError("no saved data source with that id")
                result = {**data_sources.populate(source, refresh=payload.get("refresh") is True).status(), "id": source.id}
            elif self.path == "/api/tag-rules/preview":
                result = tags.preview_rule(payload)
            elif self.path == "/api/catalogs/preview":
                result = catalogs.preview(payload)
            elif self.path == "/api/catalogs/active":
                result = {"active": catalogs.set_active(payload.get("active"))}
            elif self.path == "/api/refactor/run":
                from . import refactor

                result = refactor.run_loaded(payload)
            elif self.path == "/api/refactor/cancel":
                from . import refactor

                result = refactor.cancel_job()
            elif self.path == "/api/shared-models/patch":
                from . import shared_models

                result = shared_models.patch_payload(payload)
            elif self.path == "/api/reduce/run":
                from . import reduction_app

                result = reduction_app.run_payload(payload)
            elif self.path == "/api/consolidate-tables":
                from . import consolidate

                result = consolidate.consolidate_loaded(payload.get("tables"), payload.get("target"), payload.get("stable_selection", False))
            elif self.path in ("/api/equivalences", "/api/equivalences/remove", "/api/prove-tables", "/api/prove-queries"):
                from . import equivalences, pipeline_equivalence

                if self.path == "/api/equivalences":
                    result = {"added": equivalences.add(payload).to_json()}
                elif self.path == "/api/equivalences/remove":
                    result = {"removed": equivalences.remove(payload.get("right"))}
                elif self.path == "/api/prove-queries":
                    result = pipeline_equivalence.prove_queries(payload.get("left"), payload.get("right"), payload.get("explain") is True)
                else:
                    result = pipeline_equivalence.prove_loaded(payload.get("left"), payload.get("right"))
            elif self.path == "/api/storage":
                from . import storage

                result = storage.save(payload.get("folder"))
            elif self.path.startswith("/api/workflow-configs/"):
                from . import repositories, workflow_configs

                item = next((i for i in repositories.listing()["repositories"] if i["id"] == payload.get("id")), None)
                if item is None:
                    raise ValueError("unknown repository")
                if self.path.endswith("/refresh"):
                    result = workflow_configs.summary(item["url"], refresh=True)
                else:
                    workflow_configs.save_settings(
                        item["url"], payload.get("projects"), payload.get("location"), payload.get("defaultLocation"))
                    result = workflow_configs.summary(item["url"])
            elif self.path.startswith("/api/repositories"):
                from . import repositories

                if self.path == "/api/repositories":
                    result = repositories.replace(payload.get("repositories"), payload.get("active"))
                elif self.path == "/api/repositories/clear":
                    result = repositories.clear_all()
                elif self.path == "/api/repositories/activate":
                    result = repositories.activate(payload.get("id"))
                else:
                    result = repositories.load(payload.get("id"), refresh=True)
            elif self.path == "/api/project/clear":
                live_graph.clear_project()
                result = {"loaded": False}
            elif self.path == "/api/jobs":
                result = {"jobs": live_graph.load_job_history(payload.get("text"), payload.get("filename"))}
            elif self.path == "/api/jobs/clear":
                live_graph.clear_job_history()
                result = {"jobs": 0}
            elif self.path == "/api/changes/compare":
                scope = payload.get("scope")
                result = live_insights.compare_branch(
                    payload.get("base"), payload.get("refresh") is True, scope if isinstance(scope, str) and scope else None)
            elif self.path == "/api/github/file":
                from .github_repo import read_file

                result = read_file(payload.get("url"), payload.get("branch"), payload.get("path"))
            else:
                result = transform(payload.get("sql"), payload.get("rules"), payload.get("format"))
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            self._json(400, {"error": str(exc)})
            return
        self._json(200, result)


def _scope_query(payload: dict) -> dict:
    """Check, look up or run one scope query. ``mode`` is ``status`` (the saved result, if any),
    ``check`` (a free dry run), ``run`` (use the cache while fresh) or ``refresh`` (run now)."""

    sql, column = payload.get("query"), payload.get("column") or None
    if not isinstance(sql, str) or not sql.strip():
        raise ValueError("write the SQL query first")
    if column is not None and not isinstance(column, str):
        raise ValueError("column must be text")
    mode = payload.get("mode", "status")
    if mode == "status":
        saved = scope_queries.peek(sql, column)
        return {"result": saved.to_json() if saved else None}
    if mode == "check":
        return {"plan": scope_queries.dry_run(sql, column)}
    if mode in ("run", "refresh"):
        return {"result": scope_queries.result_for(sql, column, refresh=mode == "refresh").to_json()}
    raise ValueError("mode must be status, check, run or refresh")


def _profiles(pipeline) -> dict | None:
    from .table_profile import profile_pipeline

    try:
        return profile_pipeline(pipeline)
    except Exception:  # noqa: BLE001 - profile fields are optional suggestions
        return None


def _required(query: dict[str, list[str]], name: str) -> str:
    value = query.get(name, [""])[0]
    if not value or len(value) > 1024:
        raise ValueError(f"{name} is required")
    return value


def stop_when_stdin_closes(server: ThreadingHTTPServer, stream=None) -> threading.Thread | None:
    """Shut ``server`` down once standard input reaches end of file; returns the watcher thread.

    A program that starts the UI keeps the pipe open for as long as it lives, so end of file means that
    program is gone, however it ended. Anything read is discarded. Without a readable stdin (``pythonw``)
    nothing can be watched, and the server keeps running.
    """

    stream = stream if stream is not None else sys.stdin
    reader = getattr(stream, "buffer", stream)
    if reader is None:
        return None

    def watch() -> None:
        try:
            while reader.read(65536):
                pass
        except (OSError, ValueError):
            pass  # a closed or broken pipe is also the end of the program that held it
        server.shutdown()  # called from this thread, not the one running serve_forever

    thread = threading.Thread(target=watch, name="stdin-watch", daemon=True)
    thread.start()
    return thread


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m kumosql.ui", description="Open the local KumoSQL browser UI")
    parser.add_argument("--version", action="version", version=f"kumosql {version.describe()}")
    parser.add_argument("--port", type=int, default=8765, help="Local port (default: 8765; 0 picks a free one, see --print-port)")
    parser.add_argument("--print-port", action="store_true", help="Print `PORT <number>` as the first line of standard output once the server is bound, for a program that starts the UI and must learn the port (use with --port 0)")
    parser.add_argument("--exit-on-stdin-close", action="store_true", help="Stop when standard input reaches end of file, so the UI ends with the program that started it (the starter keeps the pipe open)")
    parser.add_argument("--project", metavar="DIR", help="Load a Dataform or SQL folder into the query graph page")
    parser.add_argument("--git", metavar="URL", help="Load a Dataform repository through the local git CLI (private repositories work with your own credentials)")
    parser.add_argument("--branch", help="With --git: branch to load (default: the remote's default branch)")
    parser.add_argument("--refresh", action="store_true", help="With --git: fetch the latest commit instead of reusing the cached clone")
    parser.add_argument("--jobs", metavar="FILE", help="Load a BigQuery job-history export (JSON, JSON lines or CSV) for the cost page and observed edges; needs --project or --git")
    parser.add_argument("--diagnose-repo", metavar="URL", help="Load a repository once and print a report of every git call (for bug reports); does not start the server")
    parser.add_argument("--no-redact", action="store_true", help="Log real repository, project, table, model and file names instead of placeholders (local debugging only; never paste such a log)")
    parser.add_argument("--lookup", metavar="PLACEHOLDER", help="Print the real name behind a placeholder from the log, for example repo#1 or model#417 (read from the private map on this computer); does not start the server")
    parser.add_argument("--verbose", action="store_true", help="Show every request in the console (they are always written to ui.log)")
    parser.add_argument("--no-browser", action="store_true", help="Print the URL without opening a browser")
    args = parser.parse_args(argv)
    if not 0 <= args.port <= 65535:
        parser.error("--port must be between 0 and 65535 (0 picks a free port)")
    console.set_redaction(not args.no_redact and redact.enabled_by_default())
    if args.lookup:
        found = redact.lookup(args.lookup)
        for session, token, value in found:
            print(f"{token}  =  {value}   (session {session})")
        if not found:
            print(f"No saved name for {args.lookup}. Placeholders are only saved while redaction is on, for the last {redact.KEEP_SESSIONS} sessions.")
        return 0 if found else 1
    if args.diagnose_repo:
        from .git_repo import diagnose

        print(console.scrub(diagnose(args.diagnose_repo, args.branch)))  # names are replaced so the report can be pasted
        return 0

    if args.git and args.project:
        parser.error("use either --project or --git, not both")
    if (args.branch or args.refresh) and not args.git:
        parser.error("--branch and --refresh need --git")
    if args.git:
        from .git_repo import GitRepoError, load_into_graph

        try:
            result = load_into_graph(args.git, args.branch, args.refresh)
            console.say(f"Loaded repository ({result['files']} files)")
        except GitRepoError as exc:
            parser.error(console.scrub(str(exc)))
    if args.project:
        from .pipeline import load_sqlx_project

        try:
            from .shared_models import read_project_files

            pipeline = load_sqlx_project(args.project)
            pipeline.source_files = read_project_files(args.project)
            live_graph.set_project(pipeline, args.project)
        except Exception as exc:
            parser.error(f"could not load project: {exc}")
    defer_autoload = False
    if not (args.project or args.git):
        from . import repositories

        # Connected repositories reload in the background; --jobs needs the project now.
        if args.jobs:
            repositories.autoload(background=False)
        else:
            defer_autoload = True
    if args.jobs:
        if not live_graph.loaded():
            parser.error("--jobs needs --project or --git")
        try:
            with open(args.jobs, encoding="utf-8") as handle:
                console.say(f"Loaded {live_graph.load_job_history(handle.read(), args.jobs)} jobs")
        except (OSError, ValueError) as exc:
            parser.error(f"could not load --jobs: {exc}")
    try:
        server = UIServer(("127.0.0.1", args.port), UIHandler)
    except OSError as exc:
        parser.error(f"could not start local server: {exc}")
    port = server.server_address[1]  # the real port: --port 0 lets the system choose
    if args.print_port:
        print(f"PORT {port}", flush=True)
    url = f"http://127.0.0.1:{port}/"
    console.set_verbose(args.verbose)
    console.disable_quick_edit()
    console.announce(url)
    if defer_autoload:
        repositories.autoload(background=True)  # after the banner, so its lines come after it
    if not args.no_browser:
        threading.Timer(0.3, lambda: webbrowser.open(url)).start()
    if args.exit_on_stdin_close:
        stop_when_stdin_closes(server)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
