"""ARCHITECTURE.md and the code that realises it, checked against each other.

The rules are ARCHITECTURE.md's own (Conventions): items `AD-nnn` are defined by
an anchor, every arrow in a diagram is labelled with one, items link the plans'
decisions `Dnn` to their anchors, and code cites items in `Implements:` comments. An undefined,
duplicated or unlabelled identifier fails; an item nothing cites yet is only
reported, as a warning, so the design may run ahead of the code.
"""

from __future__ import annotations

import re
import warnings
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ARCHITECTURE = ROOT / "ARCHITECTURE.md"

DEFINITION = re.compile(r'<a id="ad-(\d{3})"></a>\*\*AD-(\d{3})\*\*')
ANCHOR = re.compile(r'<a id="ad-(\d{3})"></a>')
REFERENCE = re.compile(r"\bAD-(\d{3})\b")
DECISION = re.compile(r"\bD(\d{1,3})\b")
DECISION_LINK = re.compile(r"\[D(\d{1,3})\]\((planning/[^)#]+\.md)#d(\d{1,3})\)")
CODE_OR_LINK = re.compile(r"`[^`]*`|\[[^\]]*\]\([^)]*\)")
DECISION_ANCHOR = re.compile(r'<a id="d(\d{1,3})"></a>\*\*D\d')
ARROW = re.compile(r"-->|-\.->|==>|---")
IMPLEMENTS = re.compile(r"Implements:\s*(.*)")
ITEMS_LINE = re.compile(r"\*\*Items\*\* \| \[AD-(\d{3})\].* to \[AD-(\d{3})\].*\*\*AD-(\d{3})\*\*")

# Where code, scripts and configuration may cite items.
SOURCES = [
    *ROOT.glob("ibkr_to_xero/**/*.py"),
    *ROOT.glob("tests/**/*.py"),
    *(p for p in ROOT.glob("packaging/**/*") if p.is_file() and "__pycache__" not in p.parts),
    *ROOT.glob(".github/workflows/*.yml"),
    ROOT / "pyproject.toml",
]


def _text() -> str:
    return ARCHITECTURE.read_text(encoding="utf-8")


def _defined() -> list[str]:
    return [f"AD-{a}" for a, _ in DEFINITION.findall(_text())]


def _citations() -> dict[str, list[str]]:
    """Item -> the files citing it in an Implements: comment."""
    cited: dict[str, list[str]] = {}
    for path in SOURCES:
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue  # binaries (icons) cite nothing
        for match in IMPLEMENTS.finditer(text):
            for number in REFERENCE.findall(match.group(1)):
                cited.setdefault(f"AD-{number}", []).append(str(path.relative_to(ROOT)))
    return cited


def test_every_anchor_is_one_well_formed_definition():
    text = _text()
    anchors = ANCHOR.findall(text)
    definitions = DEFINITION.findall(text)
    assert len(anchors) == len(definitions), "an anchor without its **AD-nnn** label"
    mismatched = [f"ad-{a}/AD-{b}" for a, b in definitions if a != b]
    assert not mismatched, f"anchor and label disagree: {mismatched}"
    defined = _defined()
    duplicates = sorted({item for item in defined if defined.count(item) > 1})
    assert not duplicates, f"defined more than once: {duplicates}"


def test_the_items_line_names_the_first_the_last_and_the_next():
    match = ITEMS_LINE.search(_text())
    assert match, "the header's Items line is missing"
    first, last, following = (int(n) for n in match.groups())
    numbers = sorted(int(item[3:]) for item in _defined())
    assert (first, last, following) == (numbers[0], numbers[-1], numbers[-1] + 1)


def test_every_reference_in_the_document_is_defined():
    defined = set(_defined())
    body = ITEMS_LINE.sub("", _text())  # the Items line names the next, undefined, item
    undefined = sorted({f"AD-{n}" for n in REFERENCE.findall(body)} - defined)
    assert not undefined, f"referred to but never defined: {undefined}"


def test_every_arrow_in_a_diagram_is_a_defined_item():
    defined = set(_defined())
    problems = []
    in_diagram = False
    for number, line in enumerate(_text().splitlines(), start=1):
        if line.strip() == "```mermaid":
            in_diagram = True
        elif line.strip() == "```":
            in_diagram = False
        elif in_diagram and ARROW.search(line):
            items = [f"AD-{n}" for n in REFERENCE.findall(line)]
            if len(items) != 1:
                problems.append(f"line {number}: an arrow needs exactly one item: {line.strip()}")
            elif items[0] not in defined:
                problems.append(f"line {number}: {items[0]} is not defined")
    assert not problems, "\n".join(problems)


def test_every_decision_cited_is_a_link_to_its_anchor_in_a_plan():
    text = _text()
    problems = []
    for label, plan, target in DECISION_LINK.findall(text):
        path = ROOT / plan
        if label != target:
            problems.append(f"[D{label}] links to #d{target}")
        elif not path.is_file():
            problems.append(f"[D{label}] links to a missing plan {plan}")
        elif f'<a id="d{target}"></a>**D{target} —' not in path.read_text(encoding="utf-8"):
            problems.append(f"[D{label}]: {plan} has no anchor d{target}")
    # A decision named outside a link, or outside inline code (`D16` in the
    # conventions is an example), is a reference without its hyperlink.
    bare = DECISION.findall(CODE_OR_LINK.sub("", text))
    problems += [f"D{n} is not a link to its decision" for n in sorted(set(bare), key=int)]
    assert not problems, "\n".join(problems)


def test_every_decision_number_is_defined_once_across_the_plans():
    seen: dict[str, list[str]] = {}
    for path in sorted((ROOT / "planning").glob("*.md")):
        for number in DECISION_ANCHOR.findall(path.read_text(encoding="utf-8")):
            seen.setdefault(number, []).append(path.name)
    twice = {f"D{n}": files for n, files in seen.items() if len(files) > 1}
    assert not twice, f"decision numbers defined more than once: {twice}"


def test_code_cites_only_defined_items_and_uncited_items_are_reported():
    defined = _defined()
    cited = _citations()
    undefined = sorted(set(cited) - set(defined))
    assert not undefined, "cited but not in ARCHITECTURE.md: " + ", ".join(
        f"{item} ({', '.join(sorted(set(cited[item])))})" for item in undefined
    )
    uncovered = [item for item in defined if item not in cited]
    if uncovered:
        warnings.warn(
            f"{len(uncovered)} architecture item(s) not yet realised in code: "
            + ", ".join(uncovered),
            stacklevel=1,
        )
