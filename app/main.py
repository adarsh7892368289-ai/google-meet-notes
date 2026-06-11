from fastapi import FastAPI

from app.api.routes import auth, connections, health, meetings, notes, webhooks


def create_app() -> FastAPI:
    application = FastAPI(title="Google Meet Notes")
    application.include_router(health.router)
    application.include_router(auth.router)
    application.include_router(connections.router)
    application.include_router(meetings.router)
    application.include_router(notes.router)
    application.include_router(webhooks.router)
    return application


app = create_app()
