"""Edit an XML document in place: change a few attribute values or texts and leave every other byte alone.

A BI workbook can be 17,000 lines of XML. Parsing it with ``xml.etree`` and writing it back reorders nothing that matters
to a parser, but it rewrites quoting, entities, empty tags and whitespace across the whole file, so a review diff of a
one-word change becomes thousands of lines. This module instead scans the text once and records where every start tag,
attribute value and text sits. A patch is then a short list of :class:`Edit` spans on the original text.

The scanner is deliberately small: elements, attributes, text, comments, CDATA, processing instructions and a DOCTYPE
without an internal subset. It decodes values the way an XML parser does (the five predefined entities, character
references, line-end and attribute whitespace normalization), and :func:`scan` checks the result against
``xml.etree.ElementTree`` element by element, so a document this scanner misreads is refused instead of being edited.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

__all__ = ["Attr", "Edit", "Node", "Document", "XmlEditError", "scan", "apply_edits", "escape_attr", "escape_text"]


class XmlEditError(ValueError):
    """The document cannot be edited in place safely; the message says why."""


@dataclass(frozen=True)
class Edit:
    """Replace ``text[start:end]`` with ``new`` (``start == end`` inserts)."""

    start: int
    end: int
    new: str


@dataclass
class Attr:
    name: str
    value: str  # decoded, as an XML parser reports it
    start: int  # first character of the raw value, inside the quotes
    end: int  # the closing quote
    quote: str
    name_start: int = 0  # first character of the attribute's name


@dataclass
class Node:
    tag: str
    index: int  # position in document order, the same as in ``ElementTree.iter()``
    start: int  # the "<" of the start tag
    tag_end: int  # just after the ">" of the start tag
    attrs: dict[str, Attr]
    attrs_end: int  # just after the last attribute, where a new attribute is inserted
    self_closing: bool
    parent: int | None
    children: list[int] = field(default_factory=list)
    content_start: int = 0  # equal to tag_end
    content_end: int = 0  # the "<" of the end tag (equal to tag_end when self-closing)
    end: int = 0  # just after the whole element
    plain_text: bool = True  # the content is text only: no child element, comment, CDATA or instruction
    expanded: str = ""  # the tag with its namespace, as ElementTree names it
    expanded_attrs: dict[str, str] = field(default_factory=dict)


_NAME = r"[^\s/>=<\"']+"
_ATTR = re.compile(r"(\s+)(" + _NAME + r")(\s*=\s*)(?:'([^'<]*)'|\"([^\"<]*)\")", re.S)
_TAG_OPEN = re.compile(r"<(" + _NAME + ")")
_TAG_CLOSE = re.compile(r"\s*(/?)>")
_END_TAG = re.compile(r"</(" + _NAME + r")\s*>")
_ENTITY = re.compile(r"&(#x[0-9A-Fa-f]+|#[0-9]+|amp|lt|gt|quot|apos);")
_NAMED = {"amp": "&", "lt": "<", "gt": ">", "quot": '"', "apos": "'"}


def _units(raw: str, offset: int, attribute: bool) -> tuple[str, list[tuple[int, int]]]:
    """Decode ``raw`` and give, for every decoded character, the span of raw text it came from (absolute offsets)."""

    out: list[str] = []
    spans: list[tuple[int, int]] = []
    i = 0
    while i < len(raw):
        ch = raw[i]
        if ch == "&":
            match = _ENTITY.match(raw, i)
            if match is None:
                raise XmlEditError(f"an entity at offset {offset + i} is not one of XML's predefined ones")
            name = match.group(1)
            if name.startswith("#x"):
                value = chr(int(name[2:], 16))
            elif name.startswith("#"):
                value = chr(int(name[1:]))
            else:
                value = _NAMED[name]
            out.append(value)
            spans.append((offset + i, offset + match.end()))
            i = match.end()
            continue
        if ch == "\r":  # a line end is one "\n" to a parser, whether written \r\n or \r
            width = 2 if raw.startswith("\r\n", i) else 1
            out.append(" " if attribute else "\n")
            spans.append((offset + i, offset + i + width))
            i += width
            continue
        if attribute and ch in "\n\t":
            ch = " "
        out.append(ch)
        spans.append((offset + i, offset + i + 1))
        i += 1
    return "".join(out), spans


_SPECIAL_ATTR = re.compile(r"[&\r\n\t]")
_SPECIAL_TEXT = re.compile(r"[&\r]")


def _decode(raw: str, attribute: bool) -> str:
    """``_units(...)[0]`` without the spans; most values hold no entity or line end, and come back as they are."""

    if not (_SPECIAL_ATTR if attribute else _SPECIAL_TEXT).search(raw):
        return raw
    return _units(raw, 0, attribute)[0]


@dataclass
class Document:
    """The text of an XML document with the location of every element, plus the standard parser's tree of the same text."""

    text: str
    nodes: list[Node]
    root: ET.Element
    elements: list[ET.Element]
    _spans: dict[tuple[int, str], list[tuple[int, int]]] = field(default_factory=dict, repr=False)

    def node(self, element: ET.Element) -> Node:
        return self.nodes[self._index[id(element)]]

    def __post_init__(self) -> None:
        self._index = {id(element): number for number, element in enumerate(self.elements)}
        self._parents = {id(child): parent for parent in self.elements for child in parent}

    def parent(self, element: ET.Element) -> ET.Element | None:
        return self._parents.get(id(element))

    def index(self, element: ET.Element) -> int:
        return self._index[id(element)]

    # ------------------------------------------------------------------ edits

    def _value_spans(self, node: Node, key: str) -> list[tuple[int, int]]:
        cached = self._spans.get((node.index, key))
        if cached is None:
            if key == "#text":
                _, cached = _units(self.text[node.content_start:node.content_end], node.content_start, attribute=False)
            else:
                attr = node.attrs[key]
                _, cached = _units(self.text[attr.start:attr.end], attr.start, attribute=True)
            self._spans[(node.index, key)] = cached
        return cached

    def replace_in_value(self, element: ET.Element, key: str, replacements: list[tuple[int, int, str]]) -> list[Edit]:
        """Edits that replace decoded ``value[start:end]`` with ``new`` for each replacement, keeping all other raw bytes.

        ``key`` is an attribute name as written in the document (``caption``, ``user:ui-domain``) or ``#text`` for the
        text of an element whose content is text only."""

        node = self.node(element)
        if key == "#text":
            if not node.plain_text:
                raise XmlEditError(f"<{node.tag}> holds more than text; its text cannot be edited in place")
            if node.self_closing:
                raise XmlEditError(f"<{node.tag}/> is empty; adding text to it is not supported")
            quote = None
            limit_end = node.content_end
        else:
            if key not in node.attrs:
                raise XmlEditError(f"<{node.tag}> has no attribute {key!r}")
            quote = node.attrs[key].quote
            limit_end = node.attrs[key].end
        spans = self._value_spans(node, key)
        edits = []
        for start, end, new in sorted(replacements):
            if not 0 <= start <= end <= len(spans):
                raise XmlEditError(f"replacement {start}:{end} is outside the value of {key!r}")
            raw_start = spans[start][0] if start < len(spans) else limit_end
            raw_end = spans[end - 1][1] if end > start else raw_start
            edits.append(Edit(raw_start, raw_end, escape_text(new) if quote is None else escape_attr(new, quote)))
        return edits

    def set_value(self, element: ET.Element, key: str, value: str) -> list[Edit]:
        """Edits that make the decoded value ``value``, touching only the characters that differ (common ends kept)."""

        old = (element.text or "") if key == "#text" else self.decoded_attr(element, key)
        prefix = 0
        while prefix < min(len(old), len(value)) and old[prefix] == value[prefix]:
            prefix += 1
        suffix = 0
        while suffix < min(len(old), len(value)) - prefix and old[len(old) - 1 - suffix] == value[len(value) - 1 - suffix]:
            suffix += 1
        if old == value:
            return []
        return self.replace_in_value(element, key, [(prefix, len(old) - suffix, value[prefix:len(value) - suffix])])

    def decoded_attr(self, element: ET.Element, key: str) -> str:
        return self.node(element).attrs[key].value

    def insert_attr(self, element: ET.Element, key: str, value: str, before: str | None = None) -> list[Edit]:
        """An edit that adds ``key='value'`` to the start tag: before attribute ``before`` when given, else after the last one.

        Tableau writes attributes in alphabetical order; keeping that order keeps the file as Tableau would save it."""

        node = self.node(element)
        if key in node.attrs:
            raise XmlEditError(f"<{node.tag}> already has attribute {key!r}")
        quote = next(iter(node.attrs.values())).quote if node.attrs else "'"
        text = f"{key}={quote}{escape_attr(value, quote)}{quote}"
        if before is not None and before in node.attrs:
            head = node.attrs[before].name_start
            return [Edit(head, head, text + " ")]
        return [Edit(node.attrs_end, node.attrs_end, " " + text)]


def escape_attr(value: str, quote: str) -> str:
    out = value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
    if quote == "'":
        out = out.replace("'", "&apos;")
    return out.replace("\r", "&#13;").replace("\n", "&#10;").replace("\t", "&#9;")


def escape_text(value: str) -> str:
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("\r", "&#13;")


def apply_edits(text: str, edits: list[Edit]) -> str:
    """Apply non-overlapping edits; two edits at the same place or overlapping spans are a programming error and raise."""

    ordered = sorted(edits, key=lambda e: (e.start, e.end))
    for before, after in zip(ordered, ordered[1:]):
        if after.start < before.end or (after.start == before.start and before.start == before.end == after.end):
            raise XmlEditError(f"edits overlap at offset {after.start}")
    out = []
    cursor = 0
    for edit in ordered:
        out.append(text[cursor:edit.start])
        out.append(edit.new)
        cursor = edit.end
    out.append(text[cursor:])
    return "".join(out)


def _expand(name: str, namespaces: dict[str, str], attribute: bool) -> str:
    if ":" not in name and (attribute or "" not in namespaces):
        return name
    prefix, colon, local = name.partition(":")
    if colon:
        if prefix == "xml":
            return "{http://www.w3.org/XML/1998/namespace}" + local
        if prefix in namespaces:
            return "{" + namespaces[prefix] + "}" + local
        raise XmlEditError(f"prefix {prefix!r} is not declared")
    if not attribute and namespaces.get(""):
        return "{" + namespaces[""] + "}" + name
    return name


def scan(text: str) -> Document:
    """Locate every element of ``text`` and check the reading against ``xml.etree.ElementTree``.

    Raises :class:`XmlEditError` when the text is not XML this module can edit in place, or when its reading of any
    element, attribute or text differs from the standard parser's."""

    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise XmlEditError(f"not well-formed XML: {exc}") from exc
    nodes: list[Node] = []
    stack: list[int] = []
    scopes: list[dict[str, str]] = [{}]
    i = 0
    n = len(text)
    while i < n:
        lt = text.find("<", i)
        if lt < 0:
            break
        if text.startswith("<!--", lt):
            close = text.find("-->", lt + 4)
            if close < 0:
                raise XmlEditError("a comment never closes")
            if stack:
                nodes[stack[-1]].plain_text = False
            i = close + 3
            continue
        if text.startswith("<![CDATA[", lt):
            close = text.find("]]>", lt)
            if close < 0:
                raise XmlEditError("a CDATA section never closes")
            if stack:
                nodes[stack[-1]].plain_text = False
            i = close + 3
            continue
        if text.startswith("<?", lt):
            close = text.find("?>", lt)
            if close < 0:
                raise XmlEditError("a processing instruction never closes")
            if stack:
                nodes[stack[-1]].plain_text = False
            i = close + 2
            continue
        if text.startswith("<!", lt):
            close = text.find(">", lt)
            if close < 0 or "[" in text[lt:close]:
                raise XmlEditError("a DOCTYPE with an internal subset is not supported")
            i = close + 1
            continue
        if text.startswith("</", lt):
            match = _END_TAG.match(text, lt)
            if match is None or not stack or nodes[stack[-1]].tag != match.group(1):
                raise XmlEditError(f"unexpected end tag at offset {lt}")
            node = nodes[stack.pop()]
            scopes.pop()
            node.content_end = lt
            node.end = match.end()
            i = match.end()
            continue
        match = _TAG_OPEN.match(text, lt)
        if match is None:
            raise XmlEditError(f"cannot read the tag at offset {lt}")
        tag = match.group(1)
        pos = match.end()
        attrs: dict[str, Attr] = {}
        while True:
            attr = _ATTR.match(text, pos)
            if attr is None:
                break
            raw = attr.group(4) if attr.group(4) is not None else attr.group(5)
            quote = "'" if attr.group(4) is not None else '"'
            value_start = attr.start(4) if attr.group(4) is not None else attr.start(5)
            value = _decode(raw, attribute=True)
            if attr.group(2) in attrs:
                raise XmlEditError(f"attribute {attr.group(2)!r} repeats at offset {lt}")
            attrs[attr.group(2)] = Attr(attr.group(2), value, value_start, value_start + len(raw), quote, attr.start(2))
            pos = attr.end()
        close = _TAG_CLOSE.match(text, pos)
        if close is None:
            raise XmlEditError(f"cannot read the start tag at offset {lt}")
        namespaces = dict(scopes[-1])
        for name, attr in attrs.items():
            if name == "xmlns":
                namespaces[""] = attr.value
            elif name.startswith("xmlns:"):
                namespaces[name[6:]] = attr.value
        parent = stack[-1] if stack else None
        node = Node(tag=tag, index=len(nodes), start=lt, tag_end=close.end(), attrs=attrs, attrs_end=pos,
                    self_closing=bool(close.group(1)), parent=parent)
        node.content_start = node.tag_end
        node.expanded = _expand(tag, namespaces, attribute=False)
        node.expanded_attrs = {
            _expand(name, namespaces, attribute=True): attr.value
            for name, attr in attrs.items() if name != "xmlns" and not name.startswith("xmlns:")
        }
        if parent is not None:
            nodes[parent].children.append(node.index)
            nodes[parent].plain_text = False
        nodes.append(node)
        if node.self_closing:
            node.content_end = node.end = node.tag_end
        else:
            stack.append(node.index)
            scopes.append(namespaces)
        i = close.end()
    if stack:
        raise XmlEditError("an element never closes")
    elements = list(root.iter())
    if len(elements) != len(nodes):
        raise XmlEditError(f"found {len(nodes)} elements where the XML parser found {len(elements)}")
    for node, element in zip(nodes, elements):
        if node.expanded != element.tag or node.expanded_attrs != dict(element.attrib):
            raise XmlEditError(f"element {node.index} <{node.tag}> reads differently than the XML parser reads it")
        if node.plain_text and not node.self_closing:
            decoded = _decode(text[node.content_start:node.content_end], attribute=False)
            if decoded != (element.text or ""):
                raise XmlEditError(f"the text of element {node.index} <{node.tag}> reads differently than the XML parser reads it")
    return Document(text=text, nodes=nodes, root=root, elements=elements)
