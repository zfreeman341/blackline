"""HTTP surface. Handlers translate between the API shapes and the
repository/services: no storage details, no business rules beyond wiring."""

import uuid

from fastapi import APIRouter, Depends, Query, Request

from app.changes import ChangeError, apply_change_set, validate_change_set
from app.errors import APIError
from app.llm import LLMClient, LLMError, parse_proposal
from app.models import (
    ChangeRequest,
    CreateDocumentRequest,
    Document,
    DocumentResponse,
    DocumentSummary,
    PatchResponse,
    ProposalResponse,
    ProposeRequest,
    RevisionSummary,
    SearchResponse,
    SearchResult,
)
from app.repository import DocumentRepository
from app.search import DEFAULT_CONTEXT_CHARS, SearchMatch, find_matches

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


# NOTE: declared before /documents/{document_id}, because FastAPI matches routes in
# declaration order, and "search" must not be captured as a document id.
@router.get("/documents/search", response_model=SearchResponse)
def search_all_documents(
    q: str = Query(min_length=1),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    context: int = Query(default=DEFAULT_CONTEXT_CHARS, ge=0, le=500),
    repo: DocumentRepository = Depends(get_repository),
) -> SearchResponse:
    matches: list[SearchMatch] = []
    for document in repo.list():
        text = repo.get_text(document.id)
        assert text is not None
        matches.extend(find_matches(document.id, text, q, context))
    return _paginated(q, matches, limit, offset)


@router.get("/documents/{document_id}/search", response_model=SearchResponse)
def search_one_document(
    document_id: str,
    q: str = Query(min_length=1),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    context: int = Query(default=DEFAULT_CONTEXT_CHARS, ge=0, le=500),
    repo: DocumentRepository = Depends(get_repository),
) -> SearchResponse:
    document = require_document(repo, document_id)
    text = repo.get_text(document.id)
    assert text is not None
    return _paginated(q, find_matches(document.id, text, q, context), limit, offset)


def _paginated(
    query: str, matches: list[SearchMatch], limit: int, offset: int
) -> SearchResponse:
    return SearchResponse(
        query=query,
        total=len(matches),
        limit=limit,
        offset=offset,
        results=[
            SearchResult(document_id=m.document_id, offset=m.offset, snippet=m.snippet)
            for m in matches[offset : offset + limit]
        ],
    )


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

    # Version gate: evaluated first; a stale expectation means nothing else
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

    # Provenance is derived, never client-asserted: passing a proposal_id is
    # the caller saying "this is the reviewed proposal I'm applying".
    revision = repo.append_revision(
        document_id=document.id,
        text=new_text,
        base_version=base_version,
        change_summary=[
            change.model_dump(mode="json", exclude_none=True)
            for change in payload.changes
        ],
        source="llm_proposal_applied" if payload.proposal_id else "api",
        proposal_id=payload.proposal_id,
    )
    return PatchResponse(
        id=document.id,
        title=document.title,
        version=revision.version,
        base_version=base_version,
        text=new_text,
    )


@router.post("/documents/{document_id}/changes/propose", response_model=ProposalResponse)
def propose_changes(
    document_id: str,
    payload: ProposeRequest,
    request: Request,
    repo: DocumentRepository = Depends(get_repository),
) -> ProposalResponse:
    """Natural-language instruction -> validated structured proposal.

    This endpoint NEVER writes. The model suggests; the caller reviews and
    submits the proposal to PATCH themselves. A proposal is returned only
    if it would survive the same resolve/validate pipeline a direct PATCH
    runs; hallucinated targets die here, as 422s, with the same candidate
    machinery a human gets."""
    document = require_document(repo, document_id)
    text = repo.get_text(document.id)
    assert text is not None

    llm: LLMClient = request.app.state.llm
    try:
        raw_output = llm.propose(text, payload.instruction)
        proposal = parse_proposal(raw_output)
    except LLMError as error:
        raise APIError(502, str(error))

    # An empty proposal is the model's well-formed way of saying "this
    # instruction doesn't map onto this document" (e.g. it names a clause
    # that doesn't exist). That's a caller problem, not a provider failure.
    if not proposal.changes:
        raise APIError(
            422,
            "no changes proposed: the instruction could not be mapped onto "
            "this document as it stands",
        )

    try:
        validate_change_set(text, proposal.changes)
    except ChangeError as error:
        raise APIError(422, f"proposal failed validation: {error.message}", error.extra)

    return ProposalResponse(
        proposal_id=uuid.uuid4().hex,
        document_id=document.id,
        base_version=document.current_version,
        instruction=payload.instruction,
        changes=proposal.changes,
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
