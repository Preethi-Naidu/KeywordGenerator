# backend/estimate.py
from fastapi import APIRouter
import tiktoken
import logging
from backend.pricing import load_pricing

router = APIRouter()
logger = logging.getLogger("app.estimate")

MODEL_PRICING = load_pricing()  # single source of truth


def count_tokens(text: str, model: str) -> int:
    try:
        encoding = tiktoken.encoding_for_model(model)
    except KeyError:
        encoding = tiktoken.get_encoding("cl100k_base")
    return len(encoding.encode(text))


@router.post("/estimate_cost")
@router.post("/estimate_cost")
def estimate_cost(payload: dict):
    topic = payload.get("topic", "")
    brand = payload.get("brand", "")
    modifiers = payload.get("modifiers", "")
    num_keywords = int(payload.get("num_keywords", 10))
    model = payload.get("model", "gpt-5")

    parts = [topic, brand, modifiers]
    prompt_text = " ".join([p for p in parts if p])

    model_key = model.lower().replace("openai/", "").strip()

    pricing = MODEL_PRICING.get(model_key, MODEL_PRICING.get("gpt-4o", {}))
    if not pricing:
        logger.warning(f"[Estimate] Model '{model_key}' not found — using gpt-4o fallback.")
    else:
        logger.info(f"[Estimate] Using pricing for model '{model_key}'")

    base_system_tokens = 1000
    avg_tokens_per_keyword = 100
    prompt_tokens = count_tokens(prompt_text, model) + base_system_tokens
    completion_tokens = num_keywords * avg_tokens_per_keyword

    cost_prompt = (prompt_tokens / 1000) * pricing.get("prompt", 0)
    cost_completion = (completion_tokens / 1000) * pricing.get("completion", 0)
    total_cost = cost_prompt + cost_completion

    return {
        "model": model_key,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "estimated_cost": round(total_cost, 4),
        "currency": "USD"
    }
