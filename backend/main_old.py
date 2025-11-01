# backend/main.py

from fastapi import FastAPI, HTTPException, Depends, status, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi.openapi.utils import get_openapi
from celery.result import AsyncResult

from backend.enrich import enrich_keywords, GoogleEnrichError
from backend.generate import generate_keywords, OpenAIServiceError
from backend.schemas import GenerateKeywordsRequest, GenerateKeywordsResponse, KeywordRow
from backend.auth import verify_token
from backend.daily_quota import DailyQuotaManager
from backend.openai_quota import OpenAIDailyQuotaManager
from backend.api_identity import ApiIdentityResolver
from backend.tasks import generate_keywords_task
from backend.celery_app import celery
from backend.pricing import router as pricing_router
from backend.config import logger
from backend.config import settings, parse_brands, parse_models
from backend.estimate import router as estimate_router
import os
from backend.openai_quota import OpenAIGlobalQuotaManager
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

# --- FastAPI app setup ---
app = FastAPI(title="Keyword Expansion API")
# Path to your frontend folder
frontend_path = os.path.join(os.path.dirname(__file__), "../frontend")

# Serve all static assets (CSS, JS, images)
app.mount("/frontend", StaticFiles(directory=frontend_path), name="frontend")

app.include_router(pricing_router, prefix="/api", tags=["pricing"])
app.include_router(estimate_router, prefix="/api", tags=["estimate"])
inspect = celery.control.inspect()

# Quotas (developer-token level, not per user)
daily_quota = DailyQuotaManager(file_path="quota_daily.json", limit_per_day=1_000)
#openai_quota = OpenAIDailyQuotaManager("openai_quota.json", daily_limit_usd=100.0)
openai_quota = OpenAIGlobalQuotaManager("openai_quota.json", global_limit_usd=100.0)
api_identity = ApiIdentityResolver(default_to_msal=True)

security = HTTPBearer()


# --- CORS ---
# Load allowed origins from env (comma-separated)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://127.0.0.1:5500",
        "http://localhost:5500",
        "https://keywordgenpoc-anafeagpe4abh3hj.uksouth-01.azurewebsites.net",
    ],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/api/frontend_config")
def get_frontend_config():
    return {
        "API_BASE": settings.API_BASE,
        "TENANT_ID": settings.TENANT_ID,
        "CLIENT_ID": settings.SPA_CLIENT_ID,
        "SCOPES": settings.API_SCOPE,
        "BRANDS": parse_brands(),
        "MODELS": parse_models(),
    }



# --- Custom OpenAPI with BearerAuth ---
def custom_openapi():
    if app.openapi_schema:
        return app.openapi_schema
    openapi_schema = get_openapi(
        title="Keyword Expansion API",
        version="1.0.0",
        description="Generates keyword ideas using OpenAI and enriches them using Google Ads. Requires Azure AD login.",
        routes=app.routes,
    )
    openapi_schema["components"]["securitySchemes"] = {
        "BearerAuth": {"type": "http", "scheme": "bearer", "bearerFormat": "JWT"}
    }
    for path in openapi_schema["paths"].values():
        for method in path.values():
            method.setdefault("security", [{"BearerAuth": []}])
    app.openapi_schema = openapi_schema
    return app.openapi_schema


app.openapi = custom_openapi


# --- Dependencies ---
def get_token(credentials: HTTPAuthorizationCredentials = Depends(security)) -> str:
    return credentials.credentials


def get_api_identity(
    credentials: HTTPAuthorizationCredentials = Depends(HTTPBearer()),
    x_api_user: str | None = Header(default=None),
) -> str:
    try:
        claims = verify_token(credentials.credentials)
    except Exception:
        logger.warning(" Invalid token attempted")
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")

    msal_username = claims.get("preferred_username")
    if not msal_username:
        logger.error(" Username not found in token claims")
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Username not found in token")

    roles = set(claims.get("roles", [])) | set(claims.get("groups", []))
    allow_override = "api.admin" in roles or "Admin" in roles

    try:
        api_user = api_identity.resolve(
            msal_username=msal_username,
            requested_override=x_api_user,
            allow_override=allow_override,
        )
        logger.info(" Authenticated user=%s (resolved api_user=%s)", msal_username, api_user)
    except ValueError as e:
        logger.warning(" Access denied for user=%s (%s)", msal_username, str(e))
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(e))

    return api_user


# --- SYNC ENDPOINT ---
@app.post(
    "/api/generate_keywords",
    response_model=GenerateKeywordsResponse,
    summary="Generate keyword ideas with OpenAI",
)
def api_generate_keywords(
    payload: GenerateKeywordsRequest,
    api_user: str = Depends(get_api_identity),
):
    REQUEST_COST_UNITS = 1  # Google quota unit

    logger.info(" [SYNC] Generate keywords requested by user=%s model=%s count=%s",
                api_user, payload.model, payload.num_keywords)

    # --- 1) PRECHECK ---
    oa_info = openai_quota.info()
    if oa_info["remaining_usd"] <= 0:
        logger.warning(" OpenAI quota exhausted (remaining=%s)", oa_info["remaining_usd"])
        return JSONResponse(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            content={"detail": "OpenAI daily spending quota exhausted", "quota": oa_info},
        )

    g_info = daily_quota.info("DEVELOPER_TOKEN")
    if g_info["remaining"] < REQUEST_COST_UNITS:
        logger.warning(" Google Ads quota exhausted for developer token")
        return JSONResponse(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            content={"detail": "Daily Google Ads quota exhausted", "quota": g_info},
        )

    # --- 2) DO THE WORK ---
    try:
        keywords, usage, model_used, usd_spent = generate_keywords(
            topic=payload.topic.strip(),
            brand=(payload.brand or "").strip(),
            modifiers=(payload.modifiers or "").strip(),
            num_keywords=payload.num_keywords,
            model=payload.model.strip(),
        )
        logger.info(" OpenAI returned %s keywords (model=%s, usd_spent=%.4f)",
                    len(keywords), model_used, usd_spent)
    except OpenAIServiceError as e:
        logger.error(" OpenAI error: %s", e)
        raise HTTPException(status_code=502, detail=str(e))
    except Exception:
        logger.exception(" Unexpected error generating keywords")
        raise

    try:
        enriched_data = enrich_keywords(keywords)
        logger.info(" Google Ads enrichment succeeded for %s keywords", len(keywords))
    except GoogleEnrichError as e:
        logger.error(" Google Ads enrichment failed: %s", e)
        return JSONResponse(
            status_code=502,
            content={
                "detail": str(e),
                "openaiQuota": openai_quota.info(),
                "quota": daily_quota.info("DEVELOPER_TOKEN"),
                "model": model_used,
            },
        )
    except Exception:
        logger.exception(" Unexpected error during Google Ads enrichment")
        raise

    rows: list[KeywordRow] = []
    for kw in keywords:
        enriched = enriched_data.get(kw.lower(), {}) or {}
        rows.append(
            KeywordRow(
                keyword=kw,
                competition=enriched.get("competition", "NA") or "NA",
                searchVolume=enriched.get("searchVolume", "NA"),
                cpc=enriched.get("cpc", "NA"),
            )
        )

    # --- 3) COMMIT ---
    if not openai_quota.try_consume(usd_spent):
        logger.warning(" OpenAI quota exhausted while committing usage")
        return JSONResponse(
            status_code=429,
            content={"detail": "OpenAI daily spending quota exhausted", "quota": openai_quota.info()},
        )

    ok, remaining_google = daily_quota.try_consume("DEVELOPER_TOKEN", amount=REQUEST_COST_UNITS)
    if not ok:
        logger.warning(" Google Ads quota exhausted at commit stage")
        return JSONResponse(
            status_code=429,
            content={"detail": "Daily Google Ads quota exhausted", "quota": daily_quota.info("DEVELOPER_TOKEN")},
        )

    logger.info(" [SYNC] Success user=%s keywords=%s remaining_google=%s remaining_openai_usd=%.4f",
                api_user, len(rows), remaining_google, openai_quota.info()["remaining_usd"])

    return {
        "source": "api",
        "model": model_used,
        "usage": usage,
        "usdSpent": usd_spent,   # <--- added
        "quotaLeft": remaining_google,
        "quota": daily_quota.info("DEVELOPER_TOKEN"),
        "openaiQuota": openai_quota.info(),
        "rows": [r.model_dump() for r in rows],
    }


# --- ASYNC ENDPOINT ---
@app.post("/api/generate_keywords_async")
def generate_keywords_async(
    payload: GenerateKeywordsRequest,
    api_user: str = Depends(get_api_identity),
):
    REQUEST_COST_UNITS = 1
    REQUEST_COST_USD_EST = 0.01

    logger.info(" [ASYNC] Enqueue keyword task user=%s model=%s count=%s",
                api_user, payload.model, payload.num_keywords)

    # Precheck OpenAI quota
    oa_info = openai_quota.info()
    if oa_info["remaining_usd"] < REQUEST_COST_USD_EST:
        logger.warning("OpenAI quota exhausted")
        raise HTTPException(status_code=429, detail="OpenAI daily spending quota exhausted")

    # Precheck Google quota
    g_info = daily_quota.info("DEVELOPER_TOKEN")
    if g_info["remaining"] < REQUEST_COST_UNITS:
        logger.warning("Google Ads quota exhausted for developer token")
        raise HTTPException(status_code=429, detail="Daily Google Ads quota exhausted")

    try:
        task = generate_keywords_task.delay(
            topic=payload.topic.strip(),
            brand=(payload.brand or "").strip(),
            modifiers=(payload.modifiers or "").strip(),
            num_keywords=payload.num_keywords,
            model=payload.model.strip(),
            api_user=api_user,
        )
        logger.info("Task enqueued task_id=%s user=%s", task.id, api_user)
    except Exception:
        logger.exception("Failed to enqueue Celery task")
        raise

    return {"task_id": task.id}


@app.get("/api/task_status/{task_id}")
def get_task_status(task_id: str):
    result = AsyncResult(task_id, app=celery)
    if result.failed():
        logger.error("Task failed task_id=%s error=%s", task_id, str(result.result))
        return {"task_id": task_id, "status": "FAILURE", "error": str(result.result)}

    logger.info("Task status task_id=%s status=%s", task_id, result.status)
    return {
        "task_id": task_id,
        "status": result.status,
        "result": result.result if result.successful() else None,
    }


@app.get("/api/quota")
def get_quota():
    info = daily_quota.info("DEVELOPER_TOKEN")
    logger.info("Google Ads quota checked: %s", info)
    return {
        "quotaLeft": info["remaining"],
        "limit": info["limit"],
        "date": info["date"],
        "resetAt": info["resetAt"],
    }


@app.get("/api/openai_quota")
async def get_openai_quota():
    info = openai_quota.info()
    logger.info("OpenAI quota checked: %s", info)
    return info


@app.get("/health")
async def health():
    insp = inspect()
    active = insp.active()
    logger.info("Health check requested celery_active=%s", bool(active))
    return {"ok": True, "celery_active": bool(active)}



# Serve login.html at /frontend
@app.get("/frontend")
async def serve_login():
    login_file = os.path.join(frontend_path, "login.html")
    return FileResponse(login_file)

# Serve index.html (main dashboard) at /frontend/dashboard
@app.get("/frontend/dashboard")
async def serve_dashboard():
    index_file = os.path.join(frontend_path, "index.html")
    return FileResponse(index_file)

# Optional: make the root (/) go directly to login page
@app.get("/")
async def serve_root():
    login_file = os.path.join(frontend_path, "login.html")
    return FileResponse(login_file)

@app.get("/api/whoami")
def whoami(token: str = Depends(get_token)):
    from backend.auth import verify_token
    claims = verify_token(token)
    return {
        "preferred_username": claims.get("preferred_username"),
        "tid": claims.get("tid"),
        "aud": claims.get("aud"),
        "roles": claims.get("roles"),
        "groups": claims.get("groups"),
    }
