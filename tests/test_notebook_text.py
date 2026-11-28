"""A Jupyter notebook as a compact timeline: cells, outputs and errors, per-cell cuts that say what was cut, a hard cap.

The fixture (tests/fixtures/bi_workbooks/region_analysis.ipynb) is synthetic and written by hand: a markdown cell, a
progress bar on stderr, a DataFrame result with an HTML form, a plot, a KeyError with an ANSI-colored traceback, cells
run out of order and a cell that never ran. Larger notebooks are built in the test that needs them.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kumosql import notebook_text as N

FIXTURE = Path(__file__).parent / "fixtures" / "bi_workbooks" / "region_analysis.ipynb"


def _timeline(**options) -> dict:
    return N.timeline(N.read_notebook(FIXTURE), **options)


def test_header_lists_counts_run_order_and_every_error():
    data = _timeline()
    assert data["cells"] == 7 and data["cell_types"] == {"code": 6, "markdown": 1}
    assert data["code_cells_run"] == 5 and data["code_cells_not_run"] == 1
    assert data["run_order"] == [2, 3, 4, 6, 5] and not data["run_in_order"] and data["ran_after_a_later_cell"] == [5]
    assert data["errors"] == [{"cell": 6, "ename": "KeyError", "evalue": "'target'"}]
    text = N.render_timeline(data)
    lines = text.splitlines()
    assert lines[0] == ("notebook region_analysis.ipynb (nbformat 4.5, kernel python3, language python): "
                        "7 cells (6 code, 1 markdown)")
    assert lines[1] == ("code cells: 5 run, 1 not run; NOT run top to bottom: 1 cell ran after a cell below it (5); "
                        "run order: 2, 3, 4, 6, 5")
    assert lines[2:4] == ["errors (1):", "  cell 6: KeyError: 'target'"]


def test_cells_show_source_outputs_and_errors_compactly():
    text = N.render_timeline(_timeline(output_lines=4))
    assert "[6] code, run 4" in text and "[7] code, not run" in text
    assert "  ERROR KeyError: 'target'" in text
    assert "\x1b[" not in text  # ANSI colors stripped from the traceback
    assert "    | ... 12 more traceback lines cut from the start (619 characters)" in text
    assert text.splitlines()[text.splitlines().index("  ERROR KeyError: 'target'") + 5] == "    | KeyError: 'target'"
    # a DataFrame keeps its text form; its HTML and a plot's PNG are named with their size, never inlined
    assert "  result (also text/html 0.3 KB, not shown):" in text and "    = 1     East    0.24" in text
    assert "  display (also image/png 0.1 KB, not shown):" in text and "iVBOR" not in text and "<table" not in text
    # a progress bar that redraws its line shows what was shown last
    assert "    > Downloading: 100%|##########| 10/10" in text and "Downloading: 10%" not in text


def test_per_cell_cuts_say_what_was_cut():
    long_source = "".join(f"x{i} = {i}\n" for i in range(100))
    stream = "".join(f"step {i}\n" for i in range(60))
    notebook = {
        "nbformat": 4, "nbformat_minor": 5, "metadata": {},
        "cells": [
            {"cell_type": "code", "execution_count": 1, "metadata": {}, "source": long_source,
             "outputs": [{"output_type": "stream", "name": "stdout", "text": stream}]},
            {"cell_type": "code", "execution_count": 2, "metadata": {}, "source": "print('" + "a" * 2000 + "')",
             "outputs": [{"output_type": "execute_result", "execution_count": 2, "metadata": {},
                          "data": {"text/plain": "\n".join(f"row {i}" for i in range(50))}}]},
        ],
    }
    data = N.timeline(N.parse_notebook(json.dumps(notebook)), source_lines=10, output_lines=6)
    first, second = data["entries"]
    assert len(first["source"]) == 10
    assert first["source_cut"] == {"lines": 90, "characters": sum(len(f"x{i} = {i}") + 1 for i in range(10, 100))}
    stdout = first["outputs"][0]["text"]
    assert stdout[:2] == ["step 0", "step 1"] and stdout[-1] == "step 59"
    assert stdout[2] == "... 54 more output lines cut from the middle (" + f"{sum(len(f'step {i}') + 1 for i in range(2, 56)):,} characters)"
    assert second["source"][0].endswith(" ... (+1,709 characters)") and len(second["source"][0]) < 400
    text = N.render_timeline(data)
    assert "    ... 90 more source lines cut" in text
    assert "    = ... 44 more output lines cut" in text


def test_the_whole_text_has_a_hard_cap_and_names_the_cells_left_out():
    cells = []
    for i in range(300):
        outputs = [{"output_type": "stream", "name": "stdout", "text": "".join(f"line {j}\n" for j in range(40))}]
        if i == 250:
            outputs = [{"output_type": "error", "ename": "ValueError", "evalue": "bad region", "traceback": ["ValueError: bad region"]}]
        cells.append({"cell_type": "code", "execution_count": i + 1, "metadata": {}, "source": f"step({i})", "outputs": outputs})
    notebook = N.parse_notebook(json.dumps({"nbformat": 4, "nbformat_minor": 4, "metadata": {}, "cells": cells}), "big.ipynb")
    for cap in (5_000, 12_000, 800):
        text = N.render_timeline(N.timeline(notebook), max_chars=cap)
        assert len(text) <= cap
        last = text.splitlines()[-1]
        assert last.startswith("... ") and "cells not shown (" in last and "-300)" in last and f"{cap:,}-character cap" in last
        assert "cell 251: ValueError: bad region" in text  # an error past the cap is still in the header
    # a cell is never shown in part: every shown cell has its whole block
    text = N.render_timeline(N.timeline(notebook), max_chars=5_000)
    shown = [line for line in text.splitlines() if line.startswith("[")]
    assert text.count("    > line 39") == len(shown)
    # the cap holds even when the header alone is too long
    assert len(N.render_timeline(N.timeline(notebook), max_chars=60)) <= 60


def test_cells_and_errors_only_select_what_is_shown(capsys):
    data = _timeline(cells="2,5-6")
    assert [e["cell"] for e in data["entries"]] == [2, 5, 6] and data["selected"] == [2, 5, 6]
    assert [e["cell"] for e in _timeline(errors_only=True)["entries"]] == [6]
    assert [e["cell"] for e in _timeline(cells="6-")["entries"]] == [6, 7]
    assert N.main(["timeline", str(FIXTURE), "--errors-only"]) == 0
    out = capsys.readouterr().out
    assert "showing 1 of 7 cells" in out and "[6] code, run 4" in out and "[2] code" not in out
    assert N.main(["timeline", str(FIXTURE), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["errors"][0]["ename"] == "KeyError"
    with pytest.raises(N.NotebookError, match="--cells"):
        _timeline(cells="two")


def test_files_that_are_not_notebooks_are_refused(tmp_path, capsys):
    (tmp_path / "bad.ipynb").write_text("{not json", encoding="utf-8")
    assert N.main(["timeline", str(tmp_path / "bad.ipynb")]) == 2
    assert "not JSON" in capsys.readouterr().err
    (tmp_path / "old.ipynb").write_text(json.dumps({"nbformat": 3, "worksheets": []}), encoding="utf-8")
    with pytest.raises(N.NotebookError, match="nbformat 3"):
        N.read_notebook(tmp_path / "old.ipynb")
    (tmp_path / "list.ipynb").write_text("[]", encoding="utf-8")
    with pytest.raises(N.NotebookError, match="not a Jupyter notebook"):
        N.read_notebook(tmp_path / "list.ipynb")
