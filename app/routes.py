"""HTTP surface. Handlers translate between the API shapes and the
repository/services — no storage details, no business rules beyond wiring."""

from fastapi import APIRouter, Depends, Request

from app.changes import ChangeError, apply_change_set
from app.errors import APIError
from app.models import (
    ChangeRequest,
    CreateDocumentRequest,
    Document,
    DocumentResponse,
    DocumentSummary,
    PatchResponse,
    RevisionSummary,
)
from app.repository import DocumentRepository

router = APIRouter()


def get_repository(request: Request) -> DocumentRepository:
    return request.app.state.repository


def require_document(repo: DocumentRepository, document_id: str) -> Document:
    document = repo.get(document_id)
    if document is None:
        raise APIError(404, f"document '{document_id}' not found")
    return document


def document_response(repo: DocumentRepository, document: Document) -> DocumentResponse:
    text = repo.get_text(document.id)
    assert text is not None  # a document always has at least revision 1
    return DocumentResponse(
        id=document.id,
        title=document.title,
        version=document.current_version,
        text=text,
    )


@router.post("/documents", response_model=DocumentResponse, status_code=201)
def create_document(
    payload: CreateDocumentRequest,
    repo: DocumentRepository = Depends(get_repository),
) -> DocumentResponse:
    document = repo.create(title=payload.title, text=payload.text)
    return document_response(repo, document)


@router.get("/documents", response_model=list[DocumentSummary])
def list_documents(
    repo: DocumentRepository = Depends(get_repository),
) -> list[DocumentSummary]:
    return [
        DocumentSummary(id=d.id, title=d.title, version=d.current_version)
        for d in repo.list()
    ]


@router.get("/documents/{document_id}", response_model=DocumentResponse)
def get_document(
    document_id: str,
    repo: DocumentRepository = Depends(get_repository),
) -> DocumentResponse:
    document = require_document(repo, document_id)
    return document_response(repo, document)


@router.patch("/documents/{document_id}", response_model=PatchResponse)
def patch_document(
    document_id: str,
    payload: ChangeRequest,
    repo: DocumentRepository = Depends(get_repository),
) -> PatchResponse:
    document = require_document(repo, document_id)

    # Version gate: evaluated first — a stale expectation means nothing else
    # about the request is meaningful (its ranges refer to a different text).
    if (
        payload.expected_version is not None
        and payload.expected_version != document.current_version
    ):
        raise APIError(
            409,
            f"version conflict: expected version {payload.expected_version}, "
            f"current version is {document.current_version}",
            {"current_version": document.current_version},
        )

    # Single snapshot: every change in this request resolves against this
    # text, whether or not the caller pinned a version.
    base_version = document.current_version
    base_text = repo.get_text(document.id)
    assert base_text is not None

    try:
        new_text = apply_change_set(base_text, payload.changes)
    except ChangeError as error:
        raise APIError(422, error.message, error.extra)

    revision = repo.append_revision(
        document_id=document.id,
        text=new_text,
        base_version=base_version,
        change_summary=[
            change.model_dump(mode="json", exclude_none=True)
            for change in payload.changes
        ],
    )
    return PatchResponse(
        id=document.id,
        title=document.title,
        version=revision.version,
        base_version=base_version,
        text=new_text,
    )


@router.get("/documents/{document_id}/revisions", response_model=list[RevisionSummary])
def list_revisions(
    document_id: str,
    repo: DocumentRepository = Depends(get_repository),
) -> list[RevisionSummary]:
    require_document(repo, document_id)
    return [
        RevisionSummary(
            version=r.version,
            base_version=r.base_version,
            created_at=r.created_at,
            change_summary=r.change_summary,
            source=r.source,
            proposal_id=r.proposal_id,
        )
        for r in repo.get_revisions(document_id)
    ]
