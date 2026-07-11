"""HTTP surface. Handlers translate between the API shapes and the
repository/services — no storage details, no business rules beyond wiring."""

from fastapi import APIRouter, Depends, Request

from app.errors import APIError
from app.models import (
    CreateDocumentRequest,
    Document,
    DocumentResponse,
    DocumentSummary,
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
