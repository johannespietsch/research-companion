"""Mint a short-lived GitHub App installation token for filing issues.

Auth chain: sign a JWT (RS256) with the App's private key → exchange it for an
installation access token (valid ~1h) → use that token as a Bearer credential
against the REST API. Issues filed with it appear as `<app-name>[bot]`.

Required env:
  GH_APP_ID                — the App's numeric App ID (or its client ID)
  GH_APP_PRIVATE_KEY       — the PEM private key contents (literal newlines or \\n)
                             OR GH_APP_PRIVATE_KEY_PATH pointing at a .pem file
Optional env:
  GH_APP_INSTALLATION_ID   — skip auto-discovery if set

`installation_token()` returns a FRESH token each call (no cross-call cache) so
the long-lived bot process doesn't reuse an expired token on the next day's run.
"""
from __future__ import annotations

import logging
import os
import time

import httpx

log = logging.getLogger("scan_errors.github_app")

_GH_API = "https://api.github.com"


def _load_private_key() -> str | None:
    key = os.getenv("GH_APP_PRIVATE_KEY")
    if key:
        # Fly secrets / .env often store the PEM with escaped newlines.
        return key.replace("\\n", "\n")
    path = os.getenv("GH_APP_PRIVATE_KEY_PATH")
    if path and os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return f.read()
    return None


def _app_jwt(app_id: str, private_key: str) -> str:
    import jwt  # PyJWT

    now = int(time.time())
    # iat backdated 60s for clock drift; exp max is 10 minutes per GitHub.
    payload = {"iat": now - 60, "exp": now + 540, "iss": str(app_id)}
    return jwt.encode(payload, private_key, algorithm="RS256")


def _discover_installation_id(app_jwt: str, repo: str) -> int:
    resp = httpx.get(
        f"{_GH_API}/repos/{repo}/installation",
        headers={"Authorization": f"Bearer {app_jwt}", "Accept": "application/vnd.github+json"},
        timeout=20,
    )
    resp.raise_for_status()
    return resp.json()["id"]


def configured() -> bool:
    """True if the App credentials are present in the environment."""
    return bool(os.getenv("GH_APP_ID") and _load_private_key())


def installation_token(repo: str) -> str | None:
    """Return a fresh installation access token, or None if the App isn't configured."""
    app_id = os.getenv("GH_APP_ID")
    private_key = _load_private_key()
    if not app_id or not private_key:
        return None

    app_jwt = _app_jwt(app_id, private_key)
    installation_id = os.getenv("GH_APP_INSTALLATION_ID") or _discover_installation_id(app_jwt, repo)

    resp = httpx.post(
        f"{_GH_API}/app/installations/{installation_id}/access_tokens",
        headers={"Authorization": f"Bearer {app_jwt}", "Accept": "application/vnd.github+json"},
        timeout=20,
    )
    resp.raise_for_status()
    return resp.json()["token"]
