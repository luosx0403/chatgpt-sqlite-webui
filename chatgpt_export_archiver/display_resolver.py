from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Iterable

from .identifiers import unicode_scalar_text


PLACEHOLDER_PREFIXES = ("[non-text content:", "[non-text part:")
_SURROGATE_RE = re.compile("[\ud800-\udfff]")


def normalize_visible_text(value: str | None) -> str:
    """Return the one visible representation used by display and search."""

    text = value or ""
    # Valid SQLite TEXT is overwhelmingly already scalar and NUL-free.  Let
    # the C string/regex searches prove that common case without rebuilding
    # every multi-megabyte body one Python character at a time.
    if "\x00" not in text and _SURROGATE_RE.search(text) is None:
        return text
    scalar, _changed = unicode_scalar_text(text, replace_invalid=True)
    return (scalar or "").replace("\x00", "\ufffd")


@dataclass
class PlaceholderStreamClassifier:
    """Classify the complete placeholder grammar without retaining its body."""

    phase: str = "leading"
    prefix: str = ""
    payload_nonspace: bool = False

    def feed(self, value: str) -> None:
        text = normalize_visible_text(value)
        position = 0
        length = len(text)
        while position < length:
            if self.phase == "invalid":
                return
            if self.phase == "leading":
                remaining = text[position:].lstrip()
                if not remaining:
                    return
                position = length - len(remaining)
            if self.phase == "trailing":
                if not text[position:].isspace():
                    self.phase = "invalid"
                return
            if self.phase == "payload":
                close = text.find("]", position)
                payload = text[position:] if close < 0 else text[position:close]
                if "\n" in payload or "\r" in payload:
                    self.phase = "invalid"
                    return
                if not self.payload_nonspace and payload and not payload.isspace():
                    self.payload_nonspace = True
                if close < 0:
                    return
                self.phase = "trailing"
                position = close + 1
                continue
            character = text[position]
            if self.phase == "leading":
                if character.isspace():
                    position += 1
                    continue
                self.phase = "prefix"
            if self.phase == "prefix":
                self.prefix += character
                candidates = [
                    prefix
                    for prefix in PLACEHOLDER_PREFIXES
                    if prefix.startswith(self.prefix)
                ]
                if not candidates:
                    self.phase = "invalid"
                elif self.prefix in PLACEHOLDER_PREFIXES:
                    self.phase = "payload"
                position += 1
                continue

    @property
    def exact_placeholder(self) -> bool:
        return self.phase == "trailing" and self.payload_nonspace


def is_generated_placeholder(value: str | None) -> bool:
    classifier = PlaceholderStreamClassifier()
    classifier.feed(value or "")
    return classifier.exact_placeholder


def classify_placeholder_chunks(chunks: Iterable[str]) -> bool:
    classifier = PlaceholderStreamClassifier()
    for chunk in chunks:
        classifier.feed(chunk)
        if classifier.phase == "invalid":
            return False
    return classifier.exact_placeholder


def placeholder_prefix_may_match(value: str | None, *, truncated: bool) -> bool:
    """Return whether a bounded prefix still can be the placeholder grammar.

    This is deliberately state based rather than a fixed-prefix string test:
    an all-whitespace prefix, a marker split across chunks, and a payload whose
    closing bracket is not loaded yet must all continue through the bounded
    streaming classifier.
    """

    classifier = PlaceholderStreamClassifier()
    classifier.feed(value or "")
    if classifier.phase == "invalid":
        return False
    return classifier.exact_placeholder or (
        truncated and classifier.phase in {"leading", "prefix", "payload", "trailing"}
    )
