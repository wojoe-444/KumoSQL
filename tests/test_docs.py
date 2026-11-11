"""Docs stay navigable: relative links point at files and headings that exist, and the user-facing entry pages have a plain-language companion.

A page missing from an index is not a failure: ``tools/docs_index.py --write`` adds it to a generated block (see below)."""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"
SIMPLE_DOCS = ROOT / "docs_simple"


def _slug(heading: str) -> str:
    return re.sub(r"[^\w\- ]", "", heading.lstrip("#").strip().lower()).replace(" ", "-")


def _lines_outside_code(path: Path):
    fence = False
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if line.startswith("```"):
            fence = not fence
        elif not fence:
            yield number, line


def _anchors(path: Path) -> set[str]:
    return {_slug(line) for _, line in _lines_outside_code(path) if line.startswith("#")}


def _load_index_tool():
    import importlib.util

    spec = importlib.util.spec_from_file_location("docs_index_tool", ROOT / "tools" / "docs_index.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("folder", [DOCS, SIMPLE_DOCS], ids=["full", "simple"])
def test_the_index_links_the_evals_folder_and_its_generated_block_has_no_dead_entries(folder):
    index = (folder / "README.md").read_text(encoding="utf-8")
    assert "](evals/README.md)" in index, f"{folder.name}/README.md must link the evals folder"
    for index_file in (folder / "README.md", folder / "evals" / "README.md"):
        text = index_file.read_text(encoding="utf-8")
        block = text.split("<!-- generated-pages:start", 1)[1].split("<!-- generated-pages:end -->")[0] if "<!-- generated-pages:start" in text else ""
        dead = [t for t in re.findall(r"\]\(([^)#\s]+\.md)\)", block) if not (index_file.parent / t).is_file()]
        assert not dead, f"{index_file.relative_to(ROOT)} generated block lists missing pages: {dead}; run python tools/docs_index.py --write"


def test_the_index_tool_adds_a_new_page_and_changes_nothing_else(tmp_path, monkeypatch):
    tool = _load_index_tool()
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "README.md").write_text("# Docs\n\n| [Listed](listed.md) | by hand |\n", encoding="utf-8")
    (docs / "listed.md").write_text("# Listed\n\nA page already in the index.\n", encoding="utf-8")
    (docs / "new-feature.md").write_text("# New feature\n\nWhat it does. More text that is not in the summary.\n", encoding="utf-8")
    monkeypatch.setattr(tool, "ROOT", tmp_path)
    monkeypatch.setattr(tool, "FOLDERS", [docs])
    assert tool.main(["--check"]) == 1
    assert tool.main(["--write"]) == 0
    text = (docs / "README.md").read_text(encoding="utf-8")
    assert text.startswith("# Docs\n\n| [Listed](listed.md) | by hand |\n")
    assert "- [New feature](new-feature.md): What it does." in text and text.count("(listed.md)") == 1
    assert tool.main(["--check"]) == 0
    # a person moves the line into the hand-written part: the generated block disappears
    (docs / "README.md").write_text(text.split("\n## More pages")[0] + "\n| [New feature](new-feature.md) | moved |\n", encoding="utf-8")
    assert tool.main(["--check"]) == 0
    assert "generated" not in (docs / "README.md").read_text(encoding="utf-8")


# The pages an analyst reads first keep a hand-written plain-language companion (the owner decided on 2026-11-11 that other
# pages need none; their older companions stay as snapshots and are not required to follow every change).
SIMPLE_REQUIRED = {
    "README.md", "getting-started.md", "mcp-server.md", "ui.md", "data-profiling.md", "pipeline-analysis.md",
    "cost-and-change-reports.md", "dataform-repositories.md", "field-trial.md", "scoreboard.md", "evals/README.md",
}


def test_every_entry_page_has_a_simple_companion():
    expected = {p.relative_to(DOCS) for p in DOCS.rglob("*.md")}
    actual = {p.relative_to(SIMPLE_DOCS) for p in SIMPLE_DOCS.rglob("*.md") if "benchmarks" not in p.relative_to(SIMPLE_DOCS).parts}
    missing = sorted(name for name in SIMPLE_REQUIRED if Path(name) in expected and Path(name) not in actual)
    orphaned = sorted(str(p) for p in actual - expected)
    assert not missing, f"add simple companions: {', '.join(missing)}"
    assert not orphaned, f"simple pages without full references: {', '.join(orphaned)}"


def test_simple_companions_link_to_the_full_reference():
    for full in [*sorted(DOCS.rglob("*.md")), ROOT / "benchmarks" / "README.md"]:
        relative = full.relative_to(DOCS) if full.is_relative_to(DOCS) else full.relative_to(ROOT)
        simple = SIMPLE_DOCS / relative
        if not simple.exists():
            continue  # only the entry pages are required to have one (SIMPLE_REQUIRED)
        targets = [
            target.partition("#")[0]
            for _, line in _lines_outside_code(simple)
            for target in re.findall(r"\]\(([^)\s]+)\)", line)
            if not re.match(r"^[a-z]+:", target)
        ]
        assert any((simple.parent / target).resolve() == full for target in targets), f"{simple.relative_to(ROOT)} must link {full.relative_to(ROOT)}"


def test_relative_links_resolve():
    broken = []
    for page in [ROOT / "README.md", ROOT / "benchmarks" / "README.md", *sorted(DOCS.rglob("*.md")), *sorted(SIMPLE_DOCS.rglob("*.md"))]:
        for number, line in _lines_outside_code(page):
            for target in re.findall(r"\]\(([^)\s]+)\)", line):
                if re.match(r"^[a-z]+:", target):
                    continue
                file, _, anchor = target.partition("#")
                resolved = (page.parent / file).resolve() if file else page
                if not resolved.exists():
                    broken.append(f"{page.relative_to(ROOT)}:{number}: {target} (no such file)")
                elif anchor and resolved.suffix == ".md" and anchor not in _anchors(resolved):
                    broken.append(f"{page.relative_to(ROOT)}:{number}: {target} (no such heading)")
    assert not broken, "\n".join(broken)
