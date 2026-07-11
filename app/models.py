"""Domain and API models.

The domain centers on two objects: a Document (identity + version counter)
and its append-only Revisions. A document's current text is always the text
of its latest revision; creation itself is just revision 1. The revision
log is the audit-trail primitive: nothing ever mutates in place.
"""

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal

from pydantic import AliasChoices, BaseModel, Field, model_validator

RevisionSource = Literal["api", "llm_proposal_applied"]


class Document(BaseModel):
    id: str
    title: str
    current_version: int = 1


class Revision(BaseModel):
    document_id: str
    version: int
    # Version the change set was resolved against. None only for revision 1,
    # which has no predecessor.
    base_version: int | None = None
    text: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    # The applied change set, as submitted by the caller. None for revision 1.
    change_summary: list[dict[str, Any]] | None = None
    source: RevisionSource = "api"
    proposal_id: str | None = None


# --- Change schema ------------------------------------------------------------
# Shared verbatim between the PATCH path and the LLM proposal path: one source
# of truth for what a valid change is, so a model-generated proposal is
# validated by exactly the rules a human submission is.


class Operation(str, Enum):
    """String enum rather than a bare literal: `insert` and `delete` are
    deliberate future additions: a new member here plus a resolver in
    app/changes.py is the whole change."""

    REPLACE = "replace"


class TextTarget(BaseModel):
    text: str = Field(min_length=1)
    occurrence: int | None = Field(default=None, ge=1)  # 1-indexed


class CharRange(BaseModel):
    """Half-open [start, end) character offsets. start == end is a valid
    empty span (replacing it is an insertion at that position)."""

    start: int = Field(ge=0)
    end: int = Field(ge=0)

    @model_validator(mode="after")
    def start_not_after_end(self) -> "CharRange":
        if self.start > self.end:
            raise ValueError(f"range start ({self.start}) must be <= end ({self.end})")
        return self


class Change(BaseModel):
    operation: Operation
    # Exactly one of `target` (find text) or `range` (explicit offsets).
    target: TextTarget | None = None
    range: CharRange | None = None
    # The client's sketch spells the new content `replacement` on text-target
    # changes and `text` on range changes; we accept both spellings for either
    # targeting style, canonicalized to `replacement`.
    replacement: str | None = Field(
        default=None, validation_alias=AliasChoices("replacement", "text")
    )

    @model_validator(mode="before")
    @classmethod
    def reject_conflicting_replacement_keys(cls, data: Any) -> Any:
        # Both spellings present with different values: refusing to guess
        # which one the caller meant beats silently preferring one.
        if (
            isinstance(data, dict)
            and "replacement" in data
            and "text" in data
            and data["replacement"] != data["text"]
        ):
            raise ValueError(
                "'replacement' and 'text' are aliases and were given conflicting values"
            )
        return data

    @model_validator(mode="after")
    def validate_shape(self) -> "Change":
        if (self.target is None) == (self.range is None):
            raise ValueError("exactly one of 'target' or 'range' must be provided")
        if self.operation is Operation.REPLACE and self.replacement is None:
            raise ValueError("replace requires 'replacement' (alias: 'text')")
        return self


class ChangeRequest(BaseModel):
    # Optional opt-in optimistic concurrency: when present and stale -> 409.
    # Absent, changes resolve against the current version at request time;
    # the revision records the resolved base_version either way.
    expected_version: int | None = None
    # Provenance: when the caller is applying a reviewed LLM proposal, they
    # pass its id back and the revision's source is derived from that. There
    # is deliberately no client-supplied `source` field; callers labelling
    # their own revisions would make the audit trail's meaning negotiable.
    proposal_id: str | None = None
    changes: list[Change] = Field(min_length=1)


class ProposedChanges(BaseModel):
    """The shape LLM output must parse into. `changes` reuses the same
    Change schema PATCH accepts: one source of truth, so a proposal is
    valid iff the identical JSON would be accepted as a direct edit.

    An empty list is schema-valid here (unlike ChangeRequest): the model is
    instructed to return {"changes": []} when an instruction can't be
    fulfilled against the document, and the route turns that into a 422.
    The caller's instruction is the problem there, not the provider."""

    changes: list[Change]


class ProposeRequest(BaseModel):
    instruction: str = Field(min_length=1)


class ProposalResponse(BaseModel):
    proposal_id: str
    document_id: str
    # The version this proposal was validated against. Apply it with
    # expected_version=base_version to guarantee it still means what it
    # meant when proposed.
    base_version: int
    instruction: str
    changes: list[Change]


# --- API request/response models ---------------------------------------------


class CreateDocumentRequest(BaseModel):
    title: str = Field(min_length=1)
    text: str


class DocumentResponse(BaseModel):
    id: str
    title: str
    version: int
    text: str


class DocumentSummary(BaseModel):
    """List-view shape: deliberately omits text so listing stays cheap even
    when individual documents are tens of megabytes."""

    id: str
    title: str
    version: int


class SearchResult(BaseModel):
    document_id: str
    offset: int  # character offset of the match in the document text
    snippet: str


class SearchResponse(BaseModel):
    query: str
    total: int  # total matches before pagination
    limit: int
    offset: int  # pagination offset (in matches, not characters)
    results: list[SearchResult]


class TargetCandidate(BaseModel):
    """One possible resolution of an ambiguous text target. Returned (as a
    list) with the 422 so the caller has exactly what they need to
    disambiguate: pick an occurrence or use the range directly."""

    occurrence: int
    range: CharRange
    context: str


class PatchResponse(DocumentResponse):
    base_version: int


class RevisionSummary(BaseModel):
    """Revision-history entry. Omits the revision text (documents can be
    10MB+); fetching a specific revision's text is a listed next step."""

    version: int
    base_version: int | None
    created_at: datetime
    change_summary: list[dict[str, Any]] | None
    source: RevisionSource
    proposal_id: str | None
