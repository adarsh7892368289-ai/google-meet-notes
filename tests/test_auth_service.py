import pytest

from app.services.auth_service import (
    EmailAlreadyExistsError,
    InvalidCredentialsError,
    authenticate_user,
    create_user,
)


async def test_create_user_persists_and_hashes(db_session):
    user = await create_user(
        db_session, email="a@acme.com", name="Alice", password="password123"
    )
    assert user.id is not None
    assert user.hashed_password != "password123"


async def test_create_user_rejects_duplicate_email(db_session):
    await create_user(db_session, email="dup@acme.com", name="A", password="password123")
    with pytest.raises(EmailAlreadyExistsError):
        await create_user(db_session, email="dup@acme.com", name="B", password="password123")


async def test_authenticate_user_success(db_session):
    await create_user(db_session, email="b@acme.com", name="Bob", password="password123")
    user = await authenticate_user(db_session, email="b@acme.com", password="password123")
    assert user.email == "b@acme.com"


async def test_authenticate_user_wrong_password(db_session):
    await create_user(db_session, email="c@acme.com", name="C", password="password123")
    with pytest.raises(InvalidCredentialsError):
        await authenticate_user(db_session, email="c@acme.com", password="nope")
