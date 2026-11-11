"""The Compare tables and Compare queries panels in Settings show a conditional proof's assumptions next to its conditions.

Runs the shipped ``settings.js`` under node against a small fake DOM and a fake backend; skips without node.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

SETTINGS = Path(__file__).resolve().parent.parent / "src" / "kumosql" / "static" / "settings.js"

HARNESS = r"""
const fs = require("fs");
const vm = require("vm");

class Node {
  constructor(tag) { this.tag = tag; this.children = []; this.attrs = {}; this.listeners = {}; this._text = ""; this.className = ""; this.value = ""; this.checked = false; this.dataset = {}; }
  append(...kids) { for (const kid of kids) this.children.push(typeof kid === "string" ? Object.assign(new Node("#text"), { _text: kid }) : kid); }
  replaceChildren(...kids) { this.children = []; this.append(...kids); }
  setAttribute(key, value) { this.attrs[key] = value; }
  addEventListener(name, fn) { (this.listeners[name] ||= []).push(fn); }
  set textContent(value) { this._text = String(value); this.children = []; }
  get textContent() { return this._text + this.children.map((kid) => kid.textContent).join(" "); }
  all() { return [this, ...this.children.flatMap((kid) => kid.all())]; }
}

const ASSUMPTION = "users.name is never NULL (assumed by the proof)";
const replies = {
  "GET /api/prover": { enabled: true, timeout_ms: 1000, bounded_rows: 0, available: true, tables: 0, constrained: 0 },
  "GET /api/equivalences": { equivalences: [] },
  "POST /api/prove-tables": { status: "conditional", method: "layers", reason: "", lemmas: [], assumptions: [ASSUMPTION], conditions: [{ text: "users.id is NOT NULL", check_sql: "SELECT 1" }] },
  "POST /api/prove-queries": { status: "proven_conditionally", reason: "", assumptions: [ASSUMPTION], conditions: [{ text: "users.id is NOT NULL", check_sql: "SELECT 1" }] },
};
const document = {
  createElement: (tag) => new Node(tag), createElementNS: (_, tag) => new Node(tag),
  addEventListener() {}, getElementById: () => null, documentElement: new Node("html"), body: new Node("body"),
};
const sandbox = {
  document, console, location: {}, localStorage: { getItem: () => null, setItem() {} }, setTimeout, clearTimeout,
  fetch: async (url, options = {}) => {
    const reply = replies[`${options.method || "GET"} ${url}`];
    return { ok: Boolean(reply), json: async () => reply || { error: "no reply" } };
  },
};
sandbox.window = sandbox;
let source = fs.readFileSync(process.argv[2], "utf8");
source = source.replace(/\}\)\(\);\s*$/, "window.__renderSolver = renderSolver;\n})();");
vm.runInNewContext(source, sandbox);

(async () => {
  const body = new Node("div");
  await sandbox.__renderSolver(body);
  const out = {};
  for (const [name, button, detail] of [["tables", "Compare tables", 0], ["queries", "Prove equivalent", 1]]) {
    const click = body.all().find((node) => node.tag === "button" && node.textContent.trim() === button);
    await Promise.all(click.listeners.click.map((fn) => fn()));
    const holders = body.all().filter((node) => node.className === "sp-query-detail");
    out[name] = holders[detail].all().filter((node) => node.tag === "summary").map((node) => node.textContent.trim());
    out[name + "_pre"] = holders[detail].all().filter((node) => node.tag === "pre").length;
    out[name + "_items"] = holders[detail].all().filter((node) => node.tag === "li" && node.textContent.includes(ASSUMPTION)).length > 0;
  }
  console.log(JSON.stringify(out));
})();
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_conditional_compare_shows_assumptions_in_both_panels(tmp_path):
    harness = tmp_path / "harness.js"
    harness.write_text(HARNESS)
    run = subprocess.run(["node", str(harness), str(SETTINGS)], capture_output=True, text=True, timeout=60)
    assert run.returncode == 0, run.stderr[-800:]
    shown = json.loads(run.stdout.strip().splitlines()[-1])
    for panel in ("tables", "queries"):
        assert any(line.startswith("Conditions (1)") for line in shown[panel]), shown
        assert any(line.startswith("Assumptions (1)") for line in shown[panel]), (panel, shown)
        assert shown[panel + "_items"], (panel, shown)


def run_harness(tmp_path, check_sql):
    harness = tmp_path / "harness.js"
    harness.write_text(HARNESS.replace('check_sql: "SELECT 1"', f"check_sql: {json.dumps(check_sql)}"))
    run = subprocess.run(["node", str(harness), str(SETTINGS)], capture_output=True, text=True, timeout=60)
    assert run.returncode == 0, run.stderr[-800:]
    return json.loads(run.stdout.strip().splitlines()[-1])


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_condition_with_a_query_offers_it_and_one_without_shows_only_its_text(tmp_path):
    """A same-statement-time condition (``NOW()`` read twice) has no table and an empty check query: no empty box."""

    with_query = run_harness(tmp_path, "SELECT 1")
    without = run_harness(tmp_path, "")
    for panel in ("tables", "queries"):
        assert with_query[panel + "_pre"] == 1, (panel, with_query)
        assert without[panel + "_pre"] == 0, (panel, without)
        assert any(line.startswith("Conditions (1)") for line in without[panel]), without
