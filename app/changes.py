"""The change engine: resolve -> validate -> apply, atomically.

All changes in a request are interpreted against a single base text, never
sequentially against each other's output. Sequential semantics were
considered and rejected: order-dependent (the same set means different
things shuffled), quadratic on large documents (each step rebuilds the
text), and impossible for a caller to reason about (change 3's offsets
depend on what changes 1–2 did). Against-base semantics make a change set
a pure function of (base version, changes), which is also what makes the
revision log's `base_version` meaningful.

This module is deliberately HTTP-free: it raises typed ChangeError
subclasses carrying a message and a structured payload; the route layer
translates them into the 422 envelope. Version conflicts (409) are not
this module's concern; they are decided before resolution begins.
"""

from dataclasses import dataclass
from typing import Any, Callable

from app.models import Change, CharRange, Operation, TargetCandidate

# Ambiguity candidates include this much context either side of the match.
CANDIDATE_CONTEXT_CHARS = 40
# A common word in a large contract can occur thousands of times; the 422
# stays useful (and bounded) by reporting the first N with the total count.
MAX_CANDIDATES = 20


class ChangeError(Exception):
    """A change set that cannot be applied as submitted. Always the whole
    set: partial application is impossible by construction, because errors
    are raised before any text is built."""

    def __init__(self, message: str, extra: dict[str, Any] | None = None):
        self.message = message
        self.extra = extra or {}
        super().__init__(message)


@dataclass(frozen=True)
class ResolvedEdit:
    """A change reduced to its primitive: replace [start, end) with new_text.
    Every operation (current and future) resolves to this shape, so the
    validate and apply passes never care which operation produced an edit."""

    start: int
    end: int
    new_text: str
    change_index: int


def apply_change_set(base_text: str, changes: list[Change]) -> str:
    """Full pipeline. Raises ChangeError (nothing applied) or returns the
    new text. One request -> one atomic transformation."""
    return _apply_edits(base_text, validate_change_set(base_text, changes))


def validate_change_set(base_text: str, changes: list[Change]) -> list[ResolvedEdit]:
    """Pipeline steps 2-3 (resolve + overlap check) without applying.
    This is what the proposal path runs: an LLM proposal must survive
    exactly the validation a direct PATCH would, but must never write."""
    return _validate_no_overlap(resolve_change_set(base_text, changes))


def resolve_change_set(base_text: str, changes: list[Change]) -> list[ResolvedEdit]:
    """Convert every change to a concrete edit against the base text.
    Also used alone by the proposal path, which validates without applying."""
    return [
        _RESOLVERS[change.operation](base_text, change, index)
        for index, change in enumerate(changes)
    ]


def _resolve_replace(base_text: str, change: Change, index: int) -> ResolvedEdit:
    assert change.replacement is not None  # guaranteed by schema validation
    if change.range is not None:
        rng = change.range
        if rng.end > len(base_text):
            raise ChangeError(
                f"change {index}: range [{rng.start}, {rng.end}) is out of bounds "
                f"for document of length {len(base_text)}",
                {"change_index": index},
            )
        return ResolvedEdit(rng.start, rng.end, change.replacement, index)

    assert change.target is not None
    target = change.target
    occurrences = _find_occurrences(base_text, target.text)

    if not occurrences:
        raise ChangeError(
            f"change {index}: target text {target.text!r} not found",
            {"change_index": index},
        )
    if target.occurrence is not None:
        if target.occurrence > len(occurrences):
            raise ChangeError(
                f"change {index}: occurrence {target.occurrence} of {target.text!r} "
                f"requested but only {len(occurrences)} occurrence(s) exist",
                {"change_index": index, "occurrences_found": len(occurrences)},
            )
        start = occurrences[target.occurrence - 1]
        return ResolvedEdit(start, start + len(target.text), change.replacement, index)
    if len(occurrences) == 1:
        start = occurrences[0]
        return ResolvedEdit(start, start + len(target.text), change.replacement, index)

    # Multiple matches, no occurrence: refusing to guess about someone's
    # contract is the product behavior here. The candidate list is exactly
    # what the caller needs to disambiguate.
    raise ChangeError(
        f"change {index}: target text {target.text!r} is ambiguous: "
        f"{len(occurrences)} occurrences found; specify 'occurrence' or use a range",
        {
            "change_index": index,
            "occurrences_found": len(occurrences),
            "candidates": [
                _candidate(base_text, occurrence_number, start, len(target.text))
                for occurrence_number, start in enumerate(
                    occurrences[:MAX_CANDIDATES], start=1
                )
            ],
        },
    )


# Operation dispatch: adding `insert`/`delete` means one enum member plus one
# resolver here; both reduce to a ResolvedEdit (insert: empty span at the
# anchor; delete: empty new_text).
_RESOLVERS: dict[Operation, Callable[[str, Change, int], ResolvedEdit]] = {
    Operation.REPLACE: _resolve_replace,
}


def _find_occurrences(haystack: str, needle: str) -> list[int]:
    """Non-overlapping matches, left to right: the natural way a person
    counts occurrences ('the 2nd "Company"')."""
    found: list[int] = []
    position = haystack.find(needle)
    while position != -1:
        found.append(position)
        position = haystack.find(needle, position + len(needle))
    return found


def _candidate(
    base_text: str, occurrence_number: int, start: int, length: int
) -> dict[str, Any]:
    end = start + length
    return TargetCandidate(
        occurrence=occurrence_number,
        range=CharRange(start=start, end=end),
        context=base_text[max(0, start - CANDIDATE_CONTEXT_CHARS) : end + CANDIDATE_CONTEXT_CHARS],
    ).model_dump()


def _validate_no_overlap(edits: list[ResolvedEdit]) -> list[ResolvedEdit]:
    """Two resolved ranges overlapping means the request contradicts itself;
    applying either order would silently pick a winner. Also rejected: two
    insertions at the identical position (their relative order would be a
    guess). Touching boundaries are fine: [3,5) then [5,8) compose
    unambiguously, and an insertion at a range's start applies before it."""
    ordered = sorted(edits, key=lambda e: (e.start, e.end))
    for previous, current in zip(ordered, ordered[1:]):
        identical_empty = (
            previous.start == current.start == previous.end == current.end
        )
        if current.start < previous.end or identical_empty:
            first, second = sorted((previous.change_index, current.change_index))
            raise ChangeError(
                f"changes {first} and {second} overlap: "
                f"[{previous.start}, {previous.end}) and [{current.start}, {current.end}) "
                "target intersecting text",
                {"conflicting_changes": [first, second]},
            )
    return ordered


def _apply_edits(base_text: str, ordered_edits: list[ResolvedEdit]) -> str:
    """Single pass over the base text: near-linear in document size, which
    is what keeps 10MB documents cheap regardless of how many edits ride
    in one request."""
    parts: list[str] = []
    cursor = 0
    for edit in ordered_edits:
        parts.append(base_text[cursor : edit.start])
        parts.append(edit.new_text)
        cursor = edit.end
    parts.append(base_text[cursor:])
    return "".join(parts)
