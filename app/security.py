from datetime import datetime, timedelta, timezone

import bcrypt
import jwt

from app.config import get_settings

_ALGORITHM = "HS256"


def hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode(), bcrypt.gensalt()).decode()


def verify_password(plain: str, hashed: str) -> bool:
    return bcrypt.checkpw(plain.encode(), hashed.encode())


def create_access_token(subject: str) -> str:
    settings = get_settings()
    expire = datetime.now(timezone.utc) + timedelta(minutes=settings.jwt_expire_minutes)
    payload = {"sub": subject, "exp": expire}
    return jwt.encode(payload, settings.jwt_secret, algorithm=_ALGORITHM)


def decode_access_token(token: str) -> dict:
    settings = get_settings()
    return jwt.decode(token, settings.jwt_secret, algorithms=[_ALGORITHM])
