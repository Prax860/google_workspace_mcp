import requests
from jose import jwt
from jose.exceptions import JWTError, ExpiredSignatureError

from core.config import (
    AUTHENTIK_JWKS_URL,
    AUTHENTIK_ISSUER,
    AUTHENTIK_AUDIENCE,
)

# Cache JWKS (do NOT fetch every request)
_JWKS = None


def _get_jwks():
    global _JWKS
    if _JWKS is None:
        resp = requests.get(AUTHENTIK_JWKS_URL, timeout=5)
        resp.raise_for_status()
        _JWKS = resp.json()
    return _JWKS


async def authentik_middleware(context, call_next):
    """
    Authentik JWT authentication middleware.
    - Verifies JWT
    - Attaches user info to context.user
    """

    auth_header = context.headers.get("authorization")

    if not auth_header or not auth_header.lower().startswith("bearer "):
        raise Exception("Missing or invalid Authorization header")

    token = auth_header.split(" ", 1)[1]

    try:
        payload = jwt.decode(
            token,
            _get_jwks(),
            algorithms=["RS256"],
            issuer=AUTHENTIK_ISSUER,
            audience=AUTHENTIK_AUDIENCE,
        )

    except ExpiredSignatureError:
        raise Exception("JWT expired")

    except JWTError as e:
        raise Exception(f"Invalid JWT: {str(e)}")

    # 🔥 Attach authenticated user to request context
    context.user = {
        "sub": payload.get("sub"),
        "email": payload.get("email"),
        "username": payload.get("preferred_username"),
        "name": payload.get("name"),
        "groups": payload.get("groups", []),
        "raw": payload,
    }

    return await call_next(context)
