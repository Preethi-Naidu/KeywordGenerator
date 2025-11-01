from backend.celery_app import celery
from backend.generate import generate_keywords, OpenAIServiceError, KeywordGenerationResult
from backend.enrich import enrich_keywords, GoogleEnrichError
from backend.schemas import KeywordRow
import logging
from backend.daily_quota import DailyQuotaManager
from backend.openai_quota import OpenAIDailyQuotaManager
from backend.api_identity import ApiIdentityResolver
from backend.celery_app_old import states
from celery.exceptions import Ignore
from backend.openai_quota import OpenAIGlobalQuotaManager


# Use persistent paths (survive redeploys + shared with main.py)
persistent_path_google = "/home/site/data/quota_daily.json"
persistent_path_openai = "/home/site/data/openai_quota.json"

daily_quota = DailyQuotaManager(file_path=persistent_path_google, limit_per_day=1_000_000)
openai_quota = OpenAIGlobalQuotaManager(file_path=persistent_path_openai, global_limit_usd=100.0)


api_identity = ApiIdentityResolver(default_to_msal=True)

logger = logging.getLogger(__name__)

def generate_keywords_task(topic, brand, modifiers, num_keywords, model, api_user=None):
    """
    Sync fallback for local testing — runs keyword generation inline (no Celery/Redis).
    """
    logger.warning("⚠️ Celery disabled — running keyword generation synchronously")

    REQUEST_COST_UNITS = 1  # Google quota unit
    if not api_user:
        api_user = "anonymous"


    # --- 1) GENERATE ---
    try:
        result_obj: KeywordGenerationResult = generate_keywords(
            topic=topic,
            brand=brand,
            modifiers=modifiers,
            num_keywords=num_keywords,
            model=model,
        )
    except OpenAIServiceError as e:
        result = {"status": "FAILURE", "detail": str(e)}
        self.update_state(state=states.SUCCESS, meta=result)
        raise Ignore()

    # --- 2) Commit OpenAI quota ---
    if not openai_quota.try_consume(result_obj.usd_spent):
        result = {
            "status": "FAILURE",
            "detail": "OpenAI daily spending quota exhausted",
            "openaiQuota": openai_quota.info(),
        }
        self.update_state(state=states.SUCCESS, meta=result)
        raise Ignore()
    logger.info(f"OpenAI quota consumed: ${result_obj.usd_spent:.6f}, remaining: {openai_quota.remaining_usd()}")

    # --- 3) Commit Google quota (global, developer-token level) ---
    ok, remaining_google = daily_quota.try_consume("DEVELOPER_TOKEN", amount=REQUEST_COST_UNITS)
    if not ok:
        result = {
            "status": "FAILURE",
            "detail": "Daily quota exhausted",
            "quota": daily_quota.info("DEVELOPER_TOKEN"),
        }
        self.update_state(state=states.SUCCESS, meta=result)
        raise Ignore()
    logger.info(f"Google quota consumed for DEVELOPER_TOKEN: {REQUEST_COST_UNITS}, remaining: {remaining_google}")

    # --- 4) ENRICH ---
    try:
        enriched = enrich_keywords(result_obj.keywords)
    except GoogleEnrichError as e:
        result = {
            "status": "FAILURE",
            "detail": str(e),
            "openaiQuota": openai_quota.info(),
            "quota": daily_quota.info("DEVELOPER_TOKEN"),
            "model": result_obj.model,
        }
        self.update_state(state=states.SUCCESS, meta=result)
        raise Ignore()

    # --- 5) SUCCESS ---
    rows = []
    for kw in result_obj.keywords:
        e = enriched.get(kw.lower(), {}) or {}
        rows.append(
            KeywordRow(
                keyword=kw,
                competition=e.get("competition", "NA") or "NA",
                searchVolume=e.get("searchVolume", "NA"),
                cpc=e.get("cpc", "NA"),
            ).model_dump()
        )

    result = {
        "status": "SUCCESS",
        "rows": rows,
        "usage": result_obj.usage,
        "usdSpent": result_obj.usd_spent,
        "model": result_obj.model,
        "quotaLeft": remaining_google,
        "quota": daily_quota.info("DEVELOPER_TOKEN"),
        "openaiQuota": openai_quota.info(),
    }
    return result
