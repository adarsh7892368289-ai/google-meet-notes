from fastapi import FastAPI

from app.api.routes import health


def create_app() -> FastAPI:
    application = FastAPI(title="Google Meet Notes")
    application.include_router(health.router)
    return application


app = create_app()
