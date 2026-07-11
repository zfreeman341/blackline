"""Search: case-insensitive substring scan with contextual snippets.

Deliberate asymmetry with the change engine's exact matching: searching is
a question ("where might this be?"), so it should be forgiving; editing is
an act on someone's contract, so it must be literal. The README covers this.

Implementation is a linear scan per document. re.finditer with an escaped
pattern gives non-overlapping matches with offsets into the *original* text
(avoiding the Unicode traps of scanning a lowercased copy, where case
mapping can change string length and skew offsets). The inverted index is
outlined in the README, not built.
"""

import re
from dataclasses import dataclass

DEFAULT_CONTEXT_CHARS = 60


@dataclass(frozen=True)
class SearchMatch:
    document_id: str
    offset: int
    snippet: str


def find_matches(
    document_id: str, text: str, query: str, context_chars: int
) -> list[SearchMatch]:
    pattern = re.compile(re.escape(query), re.IGNORECASE)
    return [
        SearchMatch(
            document_id=document_id,
            offset=match.start(),
            snippet=text[max(0, match.start() - context_chars) : match.end() + context_chars],
        )
        for match in pattern.finditer(text)
    ]
