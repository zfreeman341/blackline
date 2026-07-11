"""Domain and API models.

The domain centers on two objects: a Document (identity + version counter)
and its append-only Revisions. A document's current text is always the text
of its latest revision — creation itself is just revision 1. The revision
log is the audit-trail primitive: nothing ever mutates in place.
"""

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field

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
