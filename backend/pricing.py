# backend/pricing.py
from fastapi import APIRouter, HTTPException
from pathlib import Path
import json
import logging

logger = logging.getLogger(__name__)
router = APIRouter()

def load_pricing() -> dict:
    """
    Load pricing JSON into a dict.
    """
    file_path = Path(__file__).parent / "openai_pricing.json"
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        logger.info("Pricing file loaded successfully: %s", file_path)
        return data
    except FileNotFoundError:
        logger.error("Pricing file not found: %s", file_path)
        raise HTTPException(status_code=500, detail="Pricing file not found")
    except json.JSONDecodeError as e:
        logger.error("Failed to parse pricing.json: %s", e)
        raise HTTPException(status_code=500, detail="Pricing file invalid JSON")
    except Exception as e:
        logger.exception("Unexpected error reading pricing.json")
        raise HTTPException(status_code=500, detail=f"Unexpected error: {e}")


@router.get("/pricing", summary="Get OpenAI model pricing")
async def get_pricing():
    return load_pricing()
