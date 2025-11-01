from fastapi import HTTPException
from jose import jwt, JWTError
import requests
import logging
from backend.config import settings

logger = logging.getLogger(__name__)

TENANT_ID = settings.TENANT_ID
CLIENT_ID = settings.CLIENT_ID
JWKS_CACHE = None  # global cache

def _fetch_jwks() -> dict:
    """Fetch JWKS from Microsoft discovery endpoint."""
    discovery_url = f"https://login.microsoftonline.com/{TENANT_ID}/v2.0/.well-known/openid-configuration"
    try:
        discovery_response = requests.get(discovery_url, timeout=5)
        discovery_response.raise_for_status()
        jwks_uri = discovery_response.json()["jwks_uri"]

        jwks_response = requests.get(jwks_uri, timeout=5)
        jwks_response.raise_for_status()
        jwks = jwks_response.json()

        logger.info("JWKS refreshed from %s", jwks_uri)
        return jwks
    except Exception as e:
        logger.error("Failed to fetch JWKS: %s", e)
        raise HTTPException(status_code=503, detail=f"Failed to fetch JWKS: {str(e)}")

def get_jwks(force_refresh: bool = False) -> dict:
    global JWKS_CACHE
    if JWKS_CACHE is None or force_refresh:
        JWKS_CACHE = _fetch_jwks()
    return JWKS_CACHE

def verify_token(token: str) -> dict:
    try:
        # Extract header for kid
        unverified_header = jwt.get_unverified_header(token)
        kid = unverified_header.get("kid")
        if not kid:
            raise ValueError("Token header missing 'kid'")

        # Try with cached JWKS
        jwks = get_jwks().get("keys", [])
        key = next((k for k in jwks if k["kid"] == kid), None)

        # If not found, force-refresh JWKS and try again
        if key is None:
            logger.warning("JWKS cache miss for kid=%s → refreshing", kid)
            jwks = get_jwks(force_refresh=True).get("keys", [])
            key = next((k for k in jwks if k["kid"] == kid), None)

        if key is None:
            raise ValueError(f"Signing key {kid} not found in JWKS")

        # Build public key dict
        public_key = {
            "kty": key["kty"],
            "kid": key["kid"],
            "use": key["use"],
            "n": key["n"],
            "e": key["e"],
        }

        # Decode and validate
        decoded = jwt.decode(
            token,
            key=public_key,
            algorithms=["RS256"],
            audience=CLIENT_ID,
            issuer=f"https://login.microsoftonline.com/{TENANT_ID}/v2.0"
        )

        #  Debug: log token audience and scopes
        aud = decoded.get("aud")
        scopes = decoded.get("scp", decoded.get("roles", []))
        logger.info(f"Token verified | aud={aud} | scopes={scopes}")
        print(f"Token verified | aud={aud} | scopes={scopes}")

        logger.debug("Token verified for %s", decoded.get("preferred_username"))
        logger.info(f"Full decoded token claims: {decoded}")
        print(f"Full decoded token claims: {decoded}")

        return decoded

    except JWTError as e:
        logger.warning("Invalid token: %s", e)
        raise HTTPException(status_code=401, detail=f"Invalid token: {str(e)}")
    except Exception as e:
        logger.error("Token verification failed: %s", e)
        raise HTTPException(status_code=401, detail=f"Token verification failed: {str(e)}")
