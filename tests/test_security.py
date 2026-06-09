import jwt
import pytest

from app.security import (
    create_access_token,
    decode_access_token,
    hash_password,
    verify_password,
)


def test_password_hash_roundtrip():
    hashed = hash_password("s3cret")
    assert hashed != "s3cret"
    assert verify_password("s3cret", hashed) is True
    assert verify_password("wrong", hashed) is False


def test_jwt_roundtrip():
    token = create_access_token(subject="user-123")
    payload = decode_access_token(token)
    assert payload["sub"] == "user-123"


def test_jwt_rejects_tampered_token():
    token = create_access_token(subject="user-123")
    with pytest.raises(jwt.PyJWTError):
        decode_access_token(token + "tampered")


def test_password_longer_than_72_bytes_roundtrip():
    long_password = "a" * 100
    hashed = hash_password(long_password)
    assert verify_password(long_password, hashed) is True
    assert verify_password("b" * 100, hashed) is False
