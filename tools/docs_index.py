"""Keep the docs indexes complete without hand-editing them: ``python tools/docs_index.py [--check | --write]``.

A page nobody listed yet is added to a generated block at the end of its folder's ``README.md`` (the title and the first
sentence of the page), and a results file no index names is listed in ``docs/evals/README.md``. Hand-written entries are
never touched or duplicated: the generated block only holds what the hand-written part does not link. A person who
wants a page in a better place moves its line above the block.

``tests/test_docs.py`` does not fail a pull request for a page missing from an index, so a feature page does not have to
edit a shared list. Run ``--write`` by hand (no CI does it) and commit the block when it changes.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
START = "<!-- generated-pages:start (python tools/docs_index.py --write) -->"
END = "<!-- generated-pages:end -->"
HEADING = "## More pages (generated)"
FOLDERS = [ROOT / "docs", ROOT / "docs" / "evals", ROOT / "docs_simple", ROOT / "docs_simple" / "evals"]


def _title_and_summary(page: Path) -> tuple[str, str]:
    title = page.stem.replace("-", " ").capitalize()
    summary = ""
    in_fence = False
    for line in page.read_text(encoding="utf-8").splitlines():
        if line.startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence or not line.strip():
            continue
        if line.startswith("# "):
            title = line[2:].strip()
        elif not summary and not line.startswith(("#", "|", ">", "-", "*", "[Plain", "<!--")) and not re.match(r"^\d+\.", line):
            summary = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", line.strip())
            summary = re.split(r"(?<=[.!?])\s", summary, maxsplit=1)[0]
            if len(summary) > 220:
                summary = summary[:217].rstrip() + "..."
    return title, summary


def _split(text: str) -> tuple[str, str]:
    """The hand-written part of an index, without the generated block."""

    if START in text:
        head = text.split(START)[0]
        return head.rstrip().removesuffix(HEADING).rstrip() + "\n", ""
    return text.rstrip() + "\n", ""


def render(folder: Path) -> str:
    """The new text of ``folder/README.md``."""

    index = folder / "README.md"
    hand, _ = _split(index.read_text(encoding="utf-8"))
    pages = [p for p in sorted(folder.glob("*.md")) if p.name != "README.md" and f"]({p.name})" not in hand]
    lines = []
    for page in pages:
        title, summary = _title_and_summary(page)
        lines.append(f"- [{title}]({page.name})" + (f": {summary}" if summary else ""))
    if folder.name == "evals" and folder.parent.name == "docs":
        for result in sorted((ROOT / "benchmarks" / "results").glob("*.json")):
            if f"`{result.stem}`" not in hand:
                lines.append(f"- Results file `{result.stem}`")
    if not lines:
        return hand
    return hand + f"\n{HEADING}\n\n{START}\n" + "\n".join(lines) + f"\n{END}\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--write", action="store_true", help="update the generated blocks")
    parser.add_argument("--check", action="store_true", help="exit 1 if a generated block is out of date (the default)")
    args = parser.parse_args(argv)
    stale = []
    for folder in FOLDERS:
        index = folder / "README.md"
        new = render(folder)
        if new != index.read_text(encoding="utf-8"):
            stale.append(index.relative_to(ROOT))
            if args.write:
                index.write_text(new, encoding="utf-8")
    for path in stale:
        print(("updated " if args.write else "out of date: ") + str(path))
    return 0 if args.write or not stale else 1


if __name__ == "__main__":
    sys.exit(main())
