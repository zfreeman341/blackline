"""Storage seam.

Route handlers and services depend only on DocumentRepository; the in-memory
implementation is one binding of that interface (production maps it to
Postgres; see INFRA.md). Nothing above this layer may know how documents
are stored.

Revisions are append-only. There is no update-in-place anywhere in this
interface by design: the only write primitives are "create a document
(which is revision 1)" and "append a revision".
"""

from __future__ import annotations

import threading
import uuid
from typing import Any, Protocol

from app.models import Document, Revision, RevisionSource


class DocumentRepository(Protocol):
    def create(self, title: str, text: str) -> Document: ...

    def get(self, document_id: str) -> Document | None: ...

    def list(self) -> list[Document]: ...

    def get_text(self, document_id: str) -> str | None: ...

    def get_revisions(self, document_id: str) -> list[Revision]: ...

    def append_revision(
        self,
        document_id: str,
        text: str,
        base_version: int,
        change_summary: list[dict[str, Any]],
        source: RevisionSource = "api",
        proposal_id: str | None = None,
    ) -> Revision: ...


class InMemoryDocumentRepository:
    def __init__(self) -> None:
        self._documents: dict[str, Document] = {}
        self._revisions: dict[str, list[Revision]] = {}
        # Single-process store; one lock keeps version bumps + revision
        # appends atomic across threaded request handlers.
        self._lock = threading.Lock()

    def create(self, title: str, text: str) -> Document:
        with self._lock:
            document = Document(id=uuid.uuid4().hex, title=title, current_version=1)
            self._documents[document.id] = document
            self._revisions[document.id] = [
                Revision(document_id=document.id, version=1, text=text)
            ]
            return document

    def get(self, document_id: str) -> Document | None:
        return self._documents.get(document_id)

    def list(self) -> list[Document]:
        return list(self._documents.values())

    def get_text(self, document_id: str) -> str | None:
        revisions = self._revisions.get(document_id)
        return revisions[-1].text if revisions else None

    def get_revisions(self, document_id: str) -> list[Revision]:
        return list(self._revisions.get(document_id, []))

    def append_revision(
        self,
        document_id: str,
        text: str,
        base_version: int,
        change_summary: list[dict[str, Any]],
        source: RevisionSource = "api",
        proposal_id: str | None = None,
    ) -> Revision:
        with self._lock:
            document = self._documents[document_id]
            revision = Revision(
                document_id=document_id,
                version=document.current_version + 1,
                base_version=base_version,
                text=text,
                change_summary=change_summary,
                source=source,
                proposal_id=proposal_id,
            )
            self._revisions[document_id].append(revision)
            self._documents[document_id] = document.model_copy(
                update={"current_version": revision.version}
            )
            return revision
