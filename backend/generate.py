# backend/generate.py

import time
import json
from pathlib import Path
from typing import List, Dict, Optional
from openai import OpenAI, APIError, RateLimitError, APITimeoutError
from pydantic import BaseModel
from backend.config import Settings, logger, PROMPT_TEMPLATE  


settings = Settings()  # loads from .env


class OpenAIServiceError(Exception):
    """Raised when the OpenAI call fails in a way we want to surface to the client."""
    pass


# ---- Pydantic result model ----
class KeywordGenerationResult(BaseModel):
    keywords: List[str]
    usage: Dict[str, Optional[int]]
    model: str
    usd_spent: float


# ---- Load pricing.json dynamically ----
def _load_pricing(path: str = "backend/openai_pricing.json") -> dict:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"Pricing file not found: {path}")
    with p.open("r", encoding="utf-8") as f:
        return json.load(f)


_PRICING = _load_pricing()


def _client() -> OpenAI:
    if not settings.OPENAI_API_KEY:
        raise OpenAIServiceError("OPENAI_API_KEY is not set")
    return OpenAI(api_key=settings.OPENAI_API_KEY)



def generate_keywords(
    topic: str,
    brand: str = "",
    modifiers: str = "",
    num_keywords: int = 10,
    model: str = "gpt-5",
) -> KeywordGenerationResult:
    """
    Calls OpenAI and returns a KeywordGenerationResult.
    Raises OpenAIServiceError on upstream issues.
    """
    client = _client()
    prompt = PROMPT_TEMPLATE.format(
    n=num_keywords,
    topic=topic,
    brand=(brand or "None"),
    mod=(modifiers or "None"),
)

    backoffs = [0.75, 1.5, 3.0]
    last_err: Exception | None = None

    logger.info("Calling OpenAI model=%s topic=%s brand=%s num_keywords=%s",
                model, topic, brand, num_keywords)

    for delay in [0.0, *backoffs]:
        if delay:
            logger.warning("Retrying OpenAI call after %.2fs due to error: %s", delay, last_err)
            time.sleep(delay)
        try:
            resp = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": "You produce clean keyword lists."},
                    {"role": "user", "content": prompt},
                ],
                temperature=1,
            )
            text = (resp.choices[0].message.content or "").strip()
            lines = [ln.strip("-• ").strip() for ln in text.splitlines() if ln.strip()]

            # de-dup and cap to requested count
            seen, keywords = set(), []
            for k in lines:
                k_norm = " ".join(k.split()).lower()
                if k_norm and k_norm not in seen:
                    seen.add(k_norm)
                    keywords.append(" ".join(k.split())[:80])
                if len(keywords) >= num_keywords:
                    break

            usage = {
                "prompt_tokens": getattr(resp.usage, "prompt_tokens", None),
                "completion_tokens": getattr(resp.usage, "completion_tokens", None),
                "total_tokens": getattr(resp.usage, "total_tokens", None),
            }

            # ---- Calculate USD spent from pricing.json ----
            price = _PRICING.get(model)
            if not price:
                logger.warning("Pricing not found for model=%s, defaulting to zero", model)
                price = {"prompt": 0, "completion": 0}

            usd_spent = 0.0
            if usage["prompt_tokens"]:
                usd_spent += (usage["prompt_tokens"] / 1000) * price["prompt"]
            if usage["completion_tokens"]:
                usd_spent += (usage["completion_tokens"] / 1000) * price["completion"]

            result = KeywordGenerationResult(
                keywords=keywords,
                usage=usage,
                model=model,
                usd_spent=round(usd_spent, 6),
            )

            logger.info("OpenAI returned %s keywords model=%s usd_spent=%.4f",
                        len(keywords), model, result.usd_spent)
            return result

        except (RateLimitError, APITimeoutError) as e:
            last_err = e
            continue
        except APIError as e:
            logger.error("OpenAI API error: %s", e)
            raise OpenAIServiceError(f"OpenAI API error: {e}") from e
        except Exception as e:
            logger.exception("Unexpected OpenAI call failure")
            raise OpenAIServiceError(f"OpenAI call failed: {e}") from e

    logger.error("OpenAI temporarily unavailable after retries: %s", last_err)
    raise OpenAIServiceError(f"OpenAI temporarily unavailable: {last_err}")
