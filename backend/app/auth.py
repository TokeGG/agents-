import os

from fastapi import Header, HTTPException, Query

BEARER_PREFIX = "Bearer "


def _expected_token() -> str | None:
    return os.environ.get("AUTH_TOKEN")


def require_auth(authorization: str | None = Header(default=None)) -> None:
    """HTTP endpoint dependency: requires 'Authorization: Bearer <AUTH_TOKEN>'.

    This is the stub bearer-token check the agent-core plan called for and
    never wired in — every endpoint using it is unreachable without the
    configured token. Full passkey/2FA login is deploy-time config, still
    out of scope here."""
    expected = _expected_token()
    if expected is None:
        # No token configured: fail closed rather than silently open, so a
        # missing .env can't accidentally leave the API unauthenticated.
        raise HTTPException(status_code=401, detail="AUTH_TOKEN is not configured on the server")
    if authorization != f"{BEARER_PREFIX}{expected}":
        raise HTTPException(status_code=401, detail="missing or invalid bearer token")


def require_auth_ws(token: str | None = Query(default=None)) -> None:
    """WebSocket endpoint dependency: requires '?token=<AUTH_TOKEN>'.

    Browsers cannot set custom headers on a WebSocket handshake, so the
    token travels as a query parameter instead of an Authorization header."""
    expected = _expected_token()
    if expected is None or token != expected:
        raise HTTPException(status_code=401, detail="missing or invalid token")
