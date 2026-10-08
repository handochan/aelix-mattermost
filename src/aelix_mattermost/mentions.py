"""Mention neutralization: a port of what the Mattermost server reads as text when it looks
for mentions (server/public/shared/markdown and app/mention_parser_standard.go)."""

from __future__ import annotations

import re
import string
import unicodedata

WORD_JOINER = "\u2060"
_GO_SPACE = ("\t\n\v\f\r \x85\xa0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007"
             "\u2008\u2009\u200a\u2028\u2029\u202f\u205f\u3000")
_ASCII_SPACE = " \t\n\v\f\r"
_FIELDS = re.compile("[" + re.escape(_GO_SPACE) + "]+")  # strings.Fields separators
_ALNUM = string.ascii_letters + string.digits
_AT = re.compile(r"@|&(?:#0{0,6}64|#[xX]0{0,6}40|commat);")
_WWW = re.compile(r"www[0-9]{0,3}\.")
_EMOJI = re.compile(r":[a-z0-9_\-+]+:\B", re.ASCII)  # RE2's \B is ASCII-only
_SCHEMES = {"http", "https", "ftp", "mailto", "tel"}


def neutralize_mentions(text: str) -> str:
    """Insert U+2060 after every "@" Mattermost could read as a mention, outside code.

    @channel, @all, @here and @user stop notifying anyone, including the forms the server
    still splits out of words (".@x", "a-@x", "_@x", and in link text also "w@x" and ":_@x")
    and "&#64;" entities. Vertical tabs and form feeds become spaces first, in code too: where
    one starts a paragraph's text, the server's trimLeftSpace cuts the line short at its
    end. Code blocks never change otherwise; code spans, link destinations and other emails
    stay unchanged, except in a paragraph that text_ranges reads whole as text, where every
    "@" before a word gets a joiner. A joiner that unlinks a reference link re-parses its
    paragraph, which can also leave a joiner from an earlier round in a code span or link
    destination."""
    text = text.replace("\v", " ").replace("\f", " ")
    # A joiner in the visible label of a reference link ("[@x]" with a "[@x]: url"
    # definition) unlinks it, which can change how the rest of the paragraph parses: repeat
    # until nothing changes. Each round only adds joiners.
    for _ in range(8):
        result = _neutralize(text)
        if result == text:
            return text
        text = result
    return _neutralize(text, everything=True)


def _neutralize(text: str, everything: bool = False) -> str:
    if "@" not in text and "&" not in text:
        return text
    marks: dict[int, None] = {}  # insertion points, in order
    for ranges, plain, splits in _paragraphs(text, everything):
        for start, stop in ranges:
            for match in _AT.finditer(text, start, stop):
                if _opens_mention(text, start, stop, match.start(), match.end(), marks, plain, splits):
                    marks[match.end()] = None
    if not marks:
        return text
    cuts = list(marks)
    pieces = [text[a:b] for a, b in zip([0, *cuts], [*cuts, len(text)])]
    return WORD_JOINER.join(pieces)


def _letter_or_digit(char: str) -> bool:
    return unicodedata.category(char)[0] in "LN"


def _word_char(char: str) -> bool:
    return char in ":.-_@" or _letter_or_digit(char)


def _opens_mention(text: str, start: int, stop: int, at: int, end: int, marks: dict[int, None],
                   plain: bool, splits: set[int]) -> bool:
    """Whether the "@" in text[at:end] can start a word the server checks for mentions.

    text[start:stop] is one run of inline text; ``marks`` are the joiners already
    inserted and ``splits`` the characters that are inline nodes of their own, each of
    which also ends the word before it. In a ``plain`` paragraph the server's inline nodes,
    which end words too (an emoji, say), are unknown: any "@" can start one."""
    following = text[end:end + 1] if end < stop else ""
    if not following or not (following in ".-_:&\\" or _letter_or_digit(following)):
        return False
    if plain or at == start or at in marks or at - 1 in splits:
        return True
    before = text[at - 1]
    if before in ".-:" or not _word_char(before):  # the server re-splits words on ".-:"
        return True
    if before != "_":
        return False
    first = at - 1  # TrimLeft(":.-_") exposes "@" after a word-initial run such as "_"
    while first > start and first not in marks and first - 1 not in splits and text[first - 1] in ":.-_":
        first -= 1
    return first == start or first in marks or first - 1 in splits or not _word_char(text[first - 1])


def text_ranges(text: str, everything: bool = False) -> list[tuple[int, int]]:
    """Ranges the server parses as inline text: paragraphs minus reference definitions, code
    spans, link destinations, reference labels, autolinks and emoji. Every paragraph
    character counts as text when ``everything`` is set or the text holds unported markdown
    (\\v or \\f); a reference label whose match depends on Unicode case folding does that for
    its own paragraph, after its leading reference definitions. These fallbacks are
    approximate: they do not split words where the server's inline nodes do, so they can
    miss a mention the server reads (neutralize_mentions opens every "@" there instead)."""
    return [run for ranges, _, _ in _paragraphs(text, everything) for run in ranges]


def _paragraphs(text: str, everything: bool) -> list[tuple[list[tuple[int, int]], bool, set[int]]]:
    """The text_ranges of each paragraph, whether it fell back to plain text, and where its
    link text has a "w", "W" or ":": InspectInline visits each Text node of a link or image
    on its own (MergeInlineText joins top-level ones only), and inlineParser.parseText
    makes each of these characters a node of its own (autolinks are off in link text)."""
    fallback = everything or "\v" in text or "\f" in text
    labels: list[str] = []  # of every reference definition: they apply to the whole post
    paragraphs = []
    for block in _Blocks(text).parse():
        ranges = block.lines if fallback else _strip_definitions(text, block.lines, labels)
        ranges = _trim_paragraph(text, ranges)
        paragraphs.append((ranges, "".join(text[a:b] for a, b in ranges)))
    result: list[tuple[list[tuple[int, int]], bool, set[int]]] = []
    for ranges, raw in paragraphs:
        links: list[tuple[int, int]] = []
        skips = None if fallback else _inline_skips(raw, labels, links)
        splits: set[int] = set()
        if skips is not None and links:
            where = [index for a, b in ranges for index in range(a, b)]  # raw offset -> text offset
            splits = {where[index] for a, b in links for index in range(a, b) if raw[index] in "wW:"}
        result.append((_subtract(ranges, skips or []), skips is None, splits))
    return result


def _strip_definitions(text: str, lines: list[tuple[int, int]], labels: list[str]) -> list[tuple[int, int]]:
    """Paragraph.Close: leading reference definitions are not text; collect their labels."""
    ranges = list(lines)
    while ranges:
        for index, (start, end) in enumerate(ranges):  # trimLeftSpace up to the first text
            while start < end and text[start] in _ASCII_SPACE:
                start += 1
            ranges[index] = (start, end)
            if start < end:
                break
        if ranges[0][0] < ranges[0][1] and text[ranges[0][0]] != "[":
            break
        found = _reference_definition("".join(text[a:b] for a, b in ranges))
        if found is None:
            break
        labels.append(_label(found[0]))
        ranges = _drop_prefix(ranges, found[1])
    return ranges


def _label(raw: str) -> str:
    """A reference label as inlineParser.referenceDefinition compares it (strings.Fields)."""
    return " ".join(x for x in _FIELDS.split(raw) if x)


def _drop_prefix(ranges: list[tuple[int, int]], count: int) -> list[tuple[int, int]]:
    """trimBytesFromRanges: the ranges without their first `count` characters."""
    result = []
    for start, end in ranges:
        if count >= end - start:
            count -= end - start
        else:
            result.append((start + count, end))
            count = 0
    return result


def _reference_definition(raw: str) -> tuple[str, int] | None:
    """markdown.parseReferenceDefinition: (raw label, characters through its last line)."""
    label = _link_label(raw, 0)
    if label is None or label[2] >= len(raw) or raw[label[2]] != ":":
        return None
    position = _skip_space(raw, label[2] + 1)
    if position >= len(raw):
        return None
    position = _destination_end(raw, position)
    name = raw[label[0]:label[1]]
    if position < len(raw) and raw[position] in _ASCII_SPACE:
        opener = _skip_space(raw, position)
        title = _title_end(raw, opener) if opener < len(raw) and raw[opener] in "\"'(" else None
        if title is None:
            line, skipped = _next_line(raw, position)
            return None if skipped else (name, line)
        line, skipped = _next_line(raw, title)
        if not skipped:
            return name, line
    line, skipped = _next_line(raw, position)
    return None if skipped else (name, line)


def _link_label(raw: str, index: int) -> tuple[int, int, int] | None:
    """markdown.parseLinkLabel: (start, end, next) of "[label]" at index."""
    if index >= len(raw) or raw[index] != "[":
        return None
    position = index + 1
    while position < len(raw):
        char = raw[position]
        if char == "\\":
            position += 2 if position + 1 < len(raw) and raw[position + 1] in string.punctuation else 1
        elif char == "[":
            return None
        elif char == "]":
            return None if position - index >= 1000 else (index + 1, position, position + 1)
        else:
            position += 1
    return None


def _next_line(raw: str, index: int) -> tuple[int, bool]:
    """markdown.nextLine: where the next line starts, and whether non-space came first."""
    skipped = False
    for position in range(index, len(raw)):
        char = raw[position]
        if char == "\r":
            return position + (2 if raw[position + 1:position + 2] == "\n" else 1), skipped
        if char == "\n":
            return position + 1, skipped
        skipped = skipped or char not in _ASCII_SPACE
    return len(raw), skipped


def _defined(name: str, labels: list[str]) -> bool | None:
    """inlineParser.referenceDefinition: whether a definition label (normalized by _label)
    matches `name` under strings.EqualFold; None when Unicode case folding decides."""
    wanted, result = _label(name), False
    if wanted in labels:
        return True
    for other in labels:
        if len(wanted) != len(other):
            continue
        same = [_fold_equal(a, b) for a, b in zip(wanted, other)]
        if all(same):
            return True
        if False not in same:
            result = None
    return result


def _fold_equal(a: str, b: str) -> bool | None:
    """Whether unicode.SimpleFold puts two characters in one case orbit; None where that
    needs the Unicode tables (two different cased or unassigned non-ASCII characters)."""
    if a == b or (a.isascii() and b.isascii()):
        return a.lower() == b.lower()
    if a.isascii() or b.isascii():  # only K (Kelvin) and long s fold to ASCII letters
        letter, other = (a, b) if a.isascii() else (b, a)
        return (letter.lower(), other) in (("k", "\u212a"), ("s", "\u017f"))
    cased = [c.lower() != c.upper() or unicodedata.category(c) == "Cn" for c in (a, b)]
    return None if all(cased) else False


def _trim_paragraph(text: str, lines: list[tuple[int, int]]) -> list[tuple[int, int]]:
    ranges = list(lines)
    while ranges:
        start, end = ranges[-1]
        end = start + len(text[start:end].rstrip(_ASCII_SPACE))
        if end > start:
            ranges[-1] = (start, end)
            break
        ranges.pop()
    return ranges


def _subtract(ranges: list[tuple[int, int]], skips: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Map raw paragraph offsets back to text, dropping the skipped parts; an empty skip
    only splits the text (the server flushes its word buffer there)."""
    result: list[tuple[int, int]] = []
    offset = 0
    for start, end in ranges:
        cursor = start
        for skip_start, skip_end in skips:
            a, b = start + skip_start - offset, start + skip_end - offset
            if b < start or a > end:
                continue
            a, b = max(a, start), min(b, end)
            if a > cursor:
                result.append((cursor, a))
            cursor = max(cursor, b)
        if cursor < end:
            result.append((cursor, end))
        offset += end - start
    return result


def _blank(text: str) -> bool:
    return not text.strip(_GO_SPACE)


def _lines(text: str) -> list[tuple[int, int]]:
    ranges, start = [], 0
    for match in re.finditer(r"\r\n?|\n", text):
        ranges.append((start, match.end()))
        start = match.end()
    if start < len(text):
        ranges.append((start, len(text)))
    return ranges


class _Block:
    __slots__ = ("kind", "indent", "marker", "closed", "children", "lines")

    def __init__(self, kind: str, indent: int = 0, marker: str = "") -> None:
        self.kind, self.indent, self.marker = kind, indent, marker
        self.closed = self.children = False
        self.lines: list[tuple[int, int]] = []


class _Blocks:
    """Port of markdown.ParseBlocks; collects paragraphs, the only blocks holding text."""

    def __init__(self, text: str) -> None:
        self.text = text
        self.paragraphs: list[_Block] = []

    def parse(self) -> list[_Block]:
        blocks = [_Block("doc")]
        for start, end in _lines(self.text):
            indent, start = self.indentation(start, end)
            last = 0
            for index, block in enumerate(blocks):
                found = self.continuation(block, indent, start, end)
                if found is None:
                    break
                extra, start = self.indentation(found[1], end)
                indent, last = found[0] + extra, index
            if blocks[last].kind not in ("fence", "code"):
                new = self.start(indent, start, end, blocks[:last + 1], blocks[last + 1:], last + 1)
                if new and self.add(blocks, last, new):
                    continue
            blank = _blank(self.text[start:end])
            if blocks[-1].kind == "para" and not blank:
                blocks[-1].lines.append((start, end))
                continue
            del blocks[last + 1:]
            if not self.add_line(blocks[last], indent, start, end) and not blank:
                self.add(blocks, last, [self.paragraph(start, end)])
        return self.paragraphs

    def indentation(self, start: int, end: int) -> tuple[int, int]:
        columns = 0
        while start < end and self.text[start] in " \t":
            columns += 4 if self.text[start] == "\t" else 1
            start += 1
        return columns, start

    def continuation(self, block: _Block, indent: int, start: int, end: int) -> tuple[int, int] | None:
        line = self.text[start:end]
        if block.kind == "doc":
            return indent, start
        if block.kind == "quote":
            if indent > 3 or not line.startswith(">"):
                return None
            columns, start = self.indentation(start + 1, end)
            return max(columns - 1, 0), start
        if block.kind == "list":
            return (0 if _blank(line) else indent), start
        if block.kind == "item":
            if _blank(line):
                return (0, start) if block.children else None
            return (indent - block.indent, start) if indent >= block.indent else None
        if block.kind == "para":
            return None if _blank(line) else (indent, start)
        if block.kind == "fence":
            return None if block.closed else (indent, start)
        if indent >= 4:  # indented code
            return indent - 4, start
        return (0, start) if _blank(line) else None

    @staticmethod
    def accept(container: _Block, new: list[_Block]) -> list[_Block] | None:
        if container.kind in ("doc", "quote", "item"):
            container.children = True
            return new
        if container.kind == "list" and new[0].kind == "item":
            return new
        if container.kind == "list" and new[0].kind == "list" and new[0].marker == container.marker:
            return new[1:]
        return None

    def add(self, blocks: list[_Block], last: int, new: list[_Block]) -> bool:
        for index in range(last, -1, -1):
            accepted = self.accept(blocks[index], new)
            if accepted is not None:
                del blocks[index + 1:]
                blocks.extend(accepted)
                return True
        return False

    def add_line(self, block: _Block, indent: int, start: int, end: int) -> bool:
        if block.kind != "fence":
            return block.kind == "code"
        line = self.text[start:end]
        if indent <= 3 and line.startswith(block.marker):
            if all(char == line[0] for char in line[len(block.marker):].strip(_GO_SPACE)):
                block.closed = True
        return True

    def paragraph(self, start: int, end: int) -> _Block:
        block = _Block("para")
        block.lines.append((start, end))
        self.paragraphs.append(block)
        return block

    def start(self, indent: int, start: int, end: int, matched: list[_Block],
              unmatched: list[_Block], depth: int) -> list[_Block] | None:
        if start >= end:
            return None
        return (self.quote_start(indent, start, end, depth)
                or self.list_start(indent, start, end, matched, unmatched, depth)
                or self.code_start(indent, start, end, matched, unmatched)
                or self.fence_start(start, end))

    def start_or_paragraph(self, indent: int, start: int, end: int, depth: int) -> list[_Block] | None:
        found = self.start(indent, start, end, [], [], depth)
        if found or _blank(self.text[start:end]):
            return found
        return [self.paragraph(start, end)]

    def quote_start(self, indent: int, start: int, end: int, depth: int) -> list[_Block] | None:
        if indent > 3 or depth >= 32 or self.text[start] != ">":
            return None
        start += 2 if start + 1 < end and self.text[start + 1] == " " else 1
        columns, start = self.indentation(start, end)
        return [_Block("quote"), *(self.start_or_paragraph(columns, start, end, depth + 1) or ())]

    def list_start(self, indent: int, start: int, end: int, matched: list[_Block],
                   unmatched: list[_Block], depth: int) -> list[_Block] | None:
        after_list = bool(matched) and matched[-1].kind == "list"
        if (not after_list and indent > 3) or depth >= 32:
            return None
        digits = 0
        while start + digits < end and self.text[start + digits] in string.digits:
            digits += 1
        if digits:
            rest = start + digits + 1
            if digits > 9 or rest > end or self.text[rest - 1] not in ".)":
                return None
            marker, number = "o" + self.text[rest - 1], int(self.text[start:rest - 1])
        elif self.text[start] in "-+*":
            marker, number, rest = "b" + self.text[start], 0, start + 1
        else:
            return None
        blank = _blank(self.text[rest:end])
        if matched and not unmatched and matched[-1].kind == "para":
            if blank or (marker[0] == "o" and number != 1):
                return None
        columns, content = self.indentation(rest, end)
        if not blank and columns < 1:
            return None
        used = 1 if blank or columns >= 5 else columns
        item = _Block("item", indent + rest - start + used)
        children = self.start_or_paragraph(columns - used, content, end, depth + 1)
        item.children = bool(children)
        return [_Block("list", marker=marker), item, *(children or ())]

    def code_start(self, indent: int, start: int, end: int, matched: list[_Block],
                   unmatched: list[_Block]) -> list[_Block] | None:
        last = unmatched[-1] if unmatched else (matched[-1] if matched else None)
        if (last is not None and last.kind == "para") or indent < 4 or _blank(self.text[start:end]):
            return None
        return [_Block("code")]

    def fence_start(self, start: int, end: int) -> list[_Block] | None:
        line = self.text[start:end]
        if not line.startswith(("```", "~~~")):
            return None
        size = len(line) - len(line.lstrip(line[0]))
        if "`" in line[size:]:
            return None
        return [_Block("fence", marker=line[:size])]


def _inline_skips(raw: str, labels: list[str], links: list[tuple[int, int]]) -> list[tuple[int, int]] | None:
    """Code spans, link destinations, reference labels, autolinks and emoji of a paragraph:
    the parts that are not merged text nodes (markdown.inlineParser.Parse). None when a
    reference label's match is uncertain. The text of every link and image is added to
    ``links``. markdown.InspectInline never visits an Autolink's text, so a URL never
    mentions anyone and is left intact."""
    skips: list[tuple[int, int]] = []
    openers: list[list] = []  # [image, inactive, end of "[" or "!["] for each opener
    index, size = 0, len(raw)
    while index < size:
        char = raw[index]
        if char == "\\":
            index += 2 if index + 1 < size and raw[index + 1] in string.punctuation else 1
        elif char == "`":
            run = index
            while run < size and raw[run] == "`":
                run += 1
            end = _code_span_end(raw, run, run - index)
            if end is not None:
                skips.append((index, end))
            index = end if end is not None else run
        elif char == "[" or (char == "!" and raw[index + 1:index + 2] == "["):
            index += 2 if char == "!" else 1
            openers.append([char == "!", False, index])
        elif char == "]" and openers:
            image, inactive, start = openers.pop()
            end = None if inactive else _inline_link_end(raw, index + 1, image)
            if end is None and not inactive:
                matched, after = _reference_end(raw, start, index, labels)
                if matched is None:
                    return None
                end = after if matched else None
            if end is None:
                index += 1
                continue
            skips.append((index + 1, end))
            links.append((start, index))
            if not image:
                for opener in openers:
                    opener[1] = opener[1] or not opener[0]
            index = end
        elif char in ":wW":
            # Autolinks are off inside an open "[" or "!["; a ":" that starts no URL may
            # still start an emoji such as ":https:", which then hides the URL's colon.
            link = None
            if all(opener[1] for opener in openers):
                link = _url_autolink(raw, index) if char == ":" else _www_autolink(raw, index)
            if link is not None:
                skips.append(link)
                index = link[1]
                continue
            end = _emoji_end(raw, index) if char == ":" else None
            if end is not None:
                skips.append((index, end))
            index = end if end is not None else index + 1
        else:
            index += 1
    return skips


def _reference_end(raw: str, start: int, close: int, labels: list[str]) -> tuple[bool | None, int]:
    """The reference branch of lookForLinkOrImage for the "]" at `close`: whether a definition
    matches (None: uncertain) and where the link ends ("[label]" or "[]" is not text)."""
    label = _link_label(raw, close + 1)
    if label is not None and label[1] > label[0]:
        name, end = raw[label[0]:label[1]], label[2]
    else:
        name, end = raw[start:close], (label[2] if label is not None else close + 1)
    return (_defined(name, labels) if name else False), end


def _code_span_end(raw: str, search: int, size: int) -> int | None:
    opening = "`" * size
    while search < len(raw):
        found = raw.find(opening, search)
        if found < 0:
            return None
        search = found + size
        if search < len(raw) and raw[search] == "`":
            while search < len(raw) and raw[search] == "`":
                search += 1
            continue
        return search
    return None


def _skip_space(raw: str, index: int) -> int:
    while index < len(raw) and raw[index] in _ASCII_SPACE:
        index += 1
    return index


def _inline_link_end(raw: str, index: int, image: bool) -> int | None:
    """End of "(destination title)" after "]" (peekAtInlineLinkDestinationAndTitle)."""
    if index >= len(raw) or raw[index] != "(":
        return None
    start = _skip_space(raw, index + 1)
    if start >= len(raw):
        return None
    if raw[start] == ")":
        return start + 1
    index = _destination_end(raw, start)
    if image and index < len(raw) and raw[index] in _ASCII_SPACE:
        start = _skip_space(raw, index)
        if start >= len(raw):
            return None
        if raw[start] == "=":
            found = _dimensions_end(raw, start)
            if found is None:
                return None
            index = found
    if index < len(raw) and raw[index] in _ASCII_SPACE:
        start = _skip_space(raw, index)
        if start >= len(raw):
            return None
        if raw[start] == ")":
            return start + 1
        if raw[start] in "\"'(":
            found = _title_end(raw, start)
            if found is None:
                return None
            index = found
    index = _skip_space(raw, index)
    return index + 1 if index < len(raw) and raw[index] == ")" else None


def _destination_end(raw: str, index: int) -> int:
    if raw[index] == "<":
        escaped = False
        for position in range(index + 1, len(raw)):
            char = raw[position]
            if escaped:
                escaped = False
                if char in string.punctuation:
                    continue
            if char == "\\":
                escaped = True
            elif char == "<" or char in _ASCII_SPACE:
                break
            elif char == ">":
                return position + 1
    depth, escaped = 0, False
    for position in range(index, len(raw)):
        char = raw[position]
        if escaped:
            escaped = False
            if char in string.punctuation:
                continue
        if char == "\\":
            escaped = True
        elif char == "(":
            depth += 1
        elif char == ")":
            if depth < 1:
                return position
            depth -= 1
        elif char in _ASCII_SPACE:
            return position
    return len(raw)


def _title_end(raw: str, index: int) -> int | None:
    closer = ")" if raw[index] == "(" else raw[index]
    index += 1
    while index < len(raw):
        if raw[index] == "\\":
            index += 2 if index + 1 < len(raw) and raw[index + 1] in string.punctuation else 1
        elif raw[index] == closer:
            return index + 1
        else:
            index += 1
    return None


def _dimensions_end(raw: str, index: int) -> int | None:
    """Mattermost's "=WIDTHxHEIGHT" image size extension (parseImageDimensions)."""
    last = len(raw) - 1
    index += 1
    if index > last:
        return None
    width = index
    while index < last and raw[index] in string.digits:
        index += 1
    has_width = index > width
    if raw[index] in _ASCII_SPACE or raw[index] == ")":
        return index
    if raw[index] not in "xX" or index == last:
        return None
    index += 1
    height = index
    while index < last and raw[index] in string.digits:
        index += 1
    if raw[index] not in _ASCII_SPACE and raw[index] != ")":
        return None
    return index if has_width or index > height else None


def _url_autolink(raw: str, colon: int) -> tuple[int, int] | None:
    if len(raw) - colon < 4 or raw[colon + 1:colon + 3] != "//":
        return None
    start = colon - 1
    while start > 0 and raw[start - 1] in _ALNUM:
        start -= 1
    if start < 0 or raw[start:colon].lower() not in _SCHEMES:
        return None
    if not _host_char(raw[colon + 3]) or not _domain(raw[colon + 3:], True):
        return None
    return start, _link_end(raw, colon)


def _www_autolink(raw: str, index: int) -> tuple[int, int] | None:
    # The server checks the previous byte only after byte offset 1.
    if (index > 1 or (index == 1 and not raw[0].isascii())) and raw[index - 1] not in _ASCII_SPACE + "*_~)<(>":
        return None
    if not _WWW.match(raw, index) or not _domain(raw[index:], False):
        return None
    return index, _link_end(raw, index)


def _emoji_end(raw: str, index: int) -> int | None:
    """End of a ":name:" emoji (markdown.parseEmoji); not right after an ASCII word byte."""
    if index > 1 and raw[index - 1] in _ALNUM + "_":
        return None
    match = _EMOJI.match(raw, index)
    return match.end() if match else None


def _link_end(raw: str, index: int) -> int:
    """Autolinks run to whitespace; "<" or ">" ends them early. The trailing punctuation
    the server trims is plain text either way."""
    while index < len(raw) and raw[index] not in _ASCII_SPACE and raw[index] not in "<>":
        index += 1
    return index


def _host_char(char: str) -> bool:
    return (bool(char) and char[0] != "\ufffd" and char[0] not in _GO_SPACE
            and not unicodedata.category(char[0]).startswith("P"))


def _domain(text: str, short: bool) -> bool:
    """Whether markdown.checkDomain accepts text; it scans UTF-8 bytes."""
    data, period, index = text.encode(), False, 1
    while index < len(data) - 1:
        byte = data[index]
        if byte == 0x5F:  # "_"
            return False
        if byte == 0x2E:  # "."
            period = True
        elif byte != 0x2D and not _host_byte(data, index):
            break
        index += 1
    return short or period


def _host_byte(data: bytes, index: int) -> bool:
    """isValidHostCharacter at a byte offset; a UTF-8 continuation byte is invalid."""
    lead = data[index]
    if 0x80 <= lead < 0xC0:
        return False
    size = 1 if lead < 0x80 else 2 if lead < 0xE0 else 3 if lead < 0xF0 else 4
    return _host_char(data[index:index + size].decode("utf-8", "replace"))
