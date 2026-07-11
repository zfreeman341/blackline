from fastapi import FastAPI

from app.errors import register_error_handlers
from app.repository import DocumentRepository, InMemoryDocumentRepository
from app.routes import router


def create_app(repository: DocumentRepository | None = None) -> FastAPI:
    app = FastAPI(title="Blackline", description="Document redlining & search service")
    app.state.repository = repository if repository is not None else InMemoryDocumentRepository()
    register_error_handlers(app)
    app.include_router(router)
    return app


app = create_app()
