import pytest
from cryptography.fernet import Fernet

from app.crypto import decrypt, encrypt


@pytest.fixture(autouse=True)
def _set_key(monkeypatch):
    monkeypatch.setenv("ENCRYPTION_KEY", Fernet.generate_key().decode())
    from app.config import get_settings
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_encrypt_decrypt_roundtrip():
    token = encrypt("my-refresh-token")
    assert isinstance(token, bytes)
    assert token != b"my-refresh-token"
    assert decrypt(token) == "my-refresh-token"


def test_decrypt_rejects_tampered_token():
    token = encrypt("secret")
    # Flip a byte in the middle of the token. (Appending bytes after the
    # trailing base64 "==" padding is silently ignored by the decoder, so it
    # would not be a real tamper.)
    mid = len(token) // 2
    tampered = token[:mid] + bytes([token[mid] ^ 0x01]) + token[mid + 1 :]
    with pytest.raises(Exception):
        decrypt(tampered)
