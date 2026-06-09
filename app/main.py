from fastapi import FastAPI

from app.api.routes import auth, health


def create_app() -> FastAPI:
    application = FastAPI(title="Google Meet Notes")
    application.include_router(health.router)
    application.include_router(auth.router)
    return application


app = create_app()
