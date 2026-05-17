import secrets

from fastapi import Header, HTTPException, status

from bot.db import get_profile_by_token


def generate_token() -> str:
    return secrets.token_urlsafe(32)


async def require_token(authorization: str = Header(...)) -> str:
    """FastAPI dependency — validates Bearer token, returns user_id."""
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or malformed Authorization header",
        )
    row = get_profile_by_token(token)
    if not row:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token",
        )
    return row["user_id"]
