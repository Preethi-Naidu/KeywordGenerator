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


# Daily, per-user quota
daily_quota = DailyQuotaManager(file_path="quota_daily.json", limit_per_day=1_000_000)
#openai_quota = OpenAIDailyQuotaManager("openai_quota.json", daily_limit_usd=100.0)
openai_quota = OpenAIGlobalQuotaManager("openai_quota.json", global_limit_usd=100.0)

api_identity = ApiIdentityResolver(default_to_msal=True)

logger = logging.getLogger(__name__)

@celery.task(name="generate_keywords_task", bind=True)
def generate_keywords_task(self, topic, brand, modifiers, num_keywords, model, api_user=None):
    print(">>> Task received in Celery <<<")
    logger.info(">>> Task received in Celery <<<")

    REQUEST_COST_UNITS = 1  # Google quota unit

    # --- 0) Resolve api_user (for logging, not quota enforcement) ---
    try:
        if not api_user:
            api_user = "anonymous"
        else:
            api_user = api_identity.resolve(api_user)
    except Exception as e:
        result = {"status": "FAILURE", "detail": f"User identity resolution failed: {str(e)}"}
        self.update_state(state=states.SUCCESS, meta=result)
        raise Ignore()

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
