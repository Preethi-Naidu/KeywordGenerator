import logging
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field, ValidationError
from typing import List, Dict, Union
from pathlib import Path

# Configure logger
logger = logging.getLogger("app.config")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)

class Settings(BaseSettings):
    # Microsoft Entra ID
    CLIENT_ID: str = Field(..., env="CLIENT_ID")
    TENANT_ID: str = Field(..., env="TENANT_ID")
    API_SCOPE: Union[str, List[str]] = Field(..., env="API_SCOPE")
    API_BASE: str = Field(..., env="API_BASE")
    SPA_CLIENT_ID: str = Field(..., env="SPA_CLIENT_ID")  # Frontend SPA

    # OpenAI
    OPENAI_API_KEY: str = Field(..., env="OPENAI_API_KEY")
    OPENAI_USD_LIMIT: int = Field(..., env="OPENAI_USD_LIMIT")

    # Google Ads (optional)
    GOOGLE_DAILY_QUOTA: int = Field(..., env="GOOGLE_DAILY_QUOTA")

    # Dropdowns
    BRANDS: str = Field(..., env="BRANDS")
    MODELS: str = Field(..., env="MODELS")

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

try:
    settings = Settings()
    logger.info("Configuration loaded successfully")
except ValidationError as e:
    logger.error("Failed to load configuration: %s", e.errors())
    raise

# --- Ensure API_SCOPE is always a list and includes OpenID defaults ---
if isinstance(settings.API_SCOPE, str):
    settings.API_SCOPE = [
        "openid",
        "profile",
        "email",
        settings.API_SCOPE
    ]

# --- Helpers ---
def parse_brands() -> List[str]:
    try:
        return [b.strip() for b in settings.BRANDS.split(",") if b.strip()]
    except Exception:
        logger.exception("Failed to parse BRANDS from config")
        return []

def parse_models() -> List[Dict]:
    result = []
    try:
        for m in settings.MODELS.split(","):
            parts = m.split(":")
            if len(parts) >= 2:
                result.append({
                    "id": parts[0],
                    "label": parts[1],
                    "default": (len(parts) == 3 and parts[2].lower() == "default")
                })
    except Exception:
        logger.exception("Failed to parse MODELS from config")
    return result

# --- Prompt Template Loader ---
def load_prompt_template() -> str:
    file_path = Path(__file__).parent / "prompt_template.txt"
    try:
        text = file_path.read_text(encoding="utf-8")
        logger.info("Prompt template loaded successfully from %s", file_path)
        return text
    except FileNotFoundError:
        logger.error("Prompt template file not found: %s", file_path)
        return ""
    except Exception as e:
        logger.exception("Unexpected error loading prompt template")
        return ""

# Load once at startup
PROMPT_TEMPLATE = load_prompt_template()
