# backend/api_identity.py
import logging

logger = logging.getLogger(__name__)

class ApiIdentityResolver:
    def __init__(self, default_to_msal: bool = True):
        self.default_to_msal = default_to_msal

    def resolve(self, msal_username: str, requested_override: str | None = None, allow_override: bool = False) -> str:
        # No JSON mapping → always return MSAL username
        if allow_override and requested_override:
            logger.info("Admin override: %s -> %s", msal_username, requested_override)
            return requested_override

        if self.default_to_msal:
            logger.debug("Returning MSAL username as API identity: %s", msal_username)
            return msal_username

        logger.warning("API identity mapping disabled and no fallback for %s", msal_username)
        raise ValueError(f"No API identity mapping found for user: {msal_username}")
