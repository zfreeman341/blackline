from fastapi import FastAPI

from app.errors import register_error_handlers
from app.llm import LLMClient, client_from_env
from app.repository import DocumentRepository, InMemoryDocumentRepository
from app.routes import router


def create_app(
    repository: DocumentRepository | None = None,
    llm_client: LLMClient | None = None,
) -> FastAPI:
    app = FastAPI(title="Blackline", description="Document redlining & search service")
    app.state.repository = repository if repository is not None else InMemoryDocumentRepository()
    # Default resolves from env: MOCK_LLM=true (the default) needs no keys
    # and no network, so tests and the demo run anywhere.
    app.state.llm = llm_client if llm_client is not None else client_from_env()
    register_error_handlers(app)
    app.include_router(router)
    return app


app = create_app()
