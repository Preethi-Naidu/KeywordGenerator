# backend/main.py

from fastapi import FastAPI, HTTPException, Depends, status, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, FileResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi.openapi.utils import get_openapi
from fastapi.staticfiles import StaticFiles
import os

from backend.enrich import enrich_keywords, GoogleEnrichError
from backend.generate import generate_keywords, OpenAIServiceError
from backend.schemas import GenerateKeywordsRequest, GenerateKeywordsResponse, KeywordRow
from backend.auth import verify_token
from backend.daily_quota import DailyQuotaManager
from backend.api_identity import ApiIdentityResolver
from backend.config import logger, settings, parse_brands, parse_models
from backend.pricing import router as pricing_router
from backend.estimate import router as estimate_router
from backend.openai_quota import OpenAIGlobalQuotaManager
import json

# --- FastAPI app setup ---
app = FastAPI(title="Keyword Expansion API")

# Path to your frontend folder
frontend_path = os.path.join(os.path.dirname(__file__), "../frontend")

# Serve all static assets (CSS, JS, images)
app.mount("/frontend", StaticFiles(directory=frontend_path), name="frontend")

# Include routers
app.include_router(pricing_router, prefix="/api", tags=["pricing"])
app.include_router(estimate_router, prefix="/api", tags=["estimate"])
persistent_path_openai = "/home/site/data/openai_quota.json"
persistent_path_google = "/home/site/data/quota_daily.json"

# Quotas
daily_quota = DailyQuotaManager(file_path=persistent_path_google, limit_per_day=5_000)
#openai_quota = OpenAIGlobalQuotaManager("openai_quota.json", global_limit_usd=100.0)
openai_quota = OpenAIGlobalQuotaManager(file_path=persistent_path_openai, global_limit_usd=100.0)
api_identity = ApiIdentityResolver(default_to_msal=True)

security = HTTPBearer()

# --- CORS ---
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

# --- Frontend config endpoint ---
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
        logger.warning("Invalid token attempted")
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")

    msal_username = claims.get("preferred_username")
    if not msal_username:
        logger.error("Username not found in token claims")
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Username not found in token")

    roles = set(claims.get("roles", [])) | set(claims.get("groups", []))
    allow_override = "api.admin" in roles or "Admin" in roles

    try:
        api_user = api_identity.resolve(
            msal_username=msal_username,
            requested_override=x_api_user,
            allow_override=allow_override,
        )
        logger.info("Authenticated user=%s (resolved api_user=%s)", msal_username, api_user)
    except ValueError as e:
        logger.warning("Access denied for user=%s (%s)", msal_username, str(e))
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(e))

    return api_user


# --- SYNC ENDPOINT ---
@app.post(
    "/api/generate_keywords",
    response_model=GenerateKeywordsResponse,
    summary="Generate keyword ideas with OpenAI",
)
def api_generate_keywords(
    payload: GenerateKeywordsRequest
    #api_user: str = Depends(get_api_identity),
):
    """Generates keywords and enriches them with Google Ads data (synchronous)."""
    REQUEST_COST_UNITS = 1  # Google quota unit

    logger.info("[SYNC] Generate keywords requested by user=%s model=%s count=%s",
                "anonymous", 
               payload.model, payload.num_keywords)

    # --- 1) Quota precheck ---
    oa_info = openai_quota.info()
    if oa_info["remaining_usd"] <= 0:
        return JSONResponse(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            content={"detail": "OpenAI daily spending quota exhausted", "quota": oa_info},
        )

    g_info = daily_quota.info("DEVELOPER_TOKEN")
    if g_info["remaining"] < REQUEST_COST_UNITS:
        return JSONResponse(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            content={"detail": "Daily Google Ads quota exhausted", "quota": g_info},
        )

    # --- 2) Generate ---
    try:
        result = generate_keywords(
            topic=payload.topic.strip(),
            brand=(payload.brand or "").strip(),
            modifiers=(payload.modifiers or "").strip(),
            num_keywords=payload.num_keywords,
            model=payload.model.strip(),
        )

        keywords = result.keywords
        usage = result.usage
        model_used = result.model
        usd_spent = result.usd_spent

        logger.info("OpenAI returned %s keywords (model=%s, usd_spent=%.4f)",
                    len(keywords), model_used, usd_spent)
    except OpenAIServiceError as e:
        logger.error("OpenAI error: %s", e)
        raise HTTPException(status_code=502, detail=str(e))
    except Exception:
        logger.exception("Unexpected error generating keywords")
        raise

    # --- 3) Enrich ---
    try:
        enriched_data = enrich_keywords(keywords)
        logger.info("Google Ads enrichment succeeded for %s keywords", len(keywords))
    except GoogleEnrichError as e:
        logger.error("Google Ads enrichment failed: %s", e)

        
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
        logger.exception("Unexpected error during Google Ads enrichment")
        raise

    # --- 4) Build rows ---
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

    # --- 5) Commit usage ---
    if not openai_quota.try_consume(usd_spent):
        return JSONResponse(
            status_code=429,
            content={"detail": "OpenAI daily spending quota exhausted", "quota": openai_quota.info()},
        )


    updated_oa_info = openai_quota.info()  # use cached

    ok, remaining_google = daily_quota.try_consume("DEVELOPER_TOKEN", amount=REQUEST_COST_UNITS)
    if not ok:
        return JSONResponse(
            status_code=429,
            content={"detail": "Daily Google Ads quota exhausted", "quota": daily_quota.info("DEVELOPER_TOKEN")},
        )

    logger.info("[SYNC] Success user=%s keywords=%s remaining_google=%s remaining_openai_usd=%.4f",
            "anonymous", len(rows), remaining_google, updated_oa_info["remaining_usd"])

    response_payload = {
        "source": "api",
        "model": model_used,
        "usage": usage,
        "usdSpent": usd_spent,
        "quotaLeft": remaining_google,
        "quota": daily_quota.info("DEVELOPER_TOKEN"),
        "openaiQuota": updated_oa_info,  # use cached
        "rows": [r.model_dump() for r in rows],
    }
    logger.info("Final response payload (to frontend): usdSpent=%s (type=%s)\n%s",
    usd_spent,
    type(usd_spent).__name__,
    json.dumps(response_payload, indent=2))

    return response_payload
# --- ASYNC (SYNC FALLBACK) ---
@app.post("/api/generate_keywords_async")
def generate_keywords_async_fallback(
    payload: GenerateKeywordsRequest
    #api_user: str = Depends(get_api_identity),
):
    """Temporary fallback: run keyword generation synchronously (no Redis/Celery)."""
    REQUEST_COST_UNITS = 1
    REQUEST_COST_USD_EST = 0.01

    logger.warning("Celery disabled — running keyword generation synchronously")

    # --- Precheck quotas ---
    oa_info = openai_quota.info()
    if oa_info["remaining_usd"] < REQUEST_COST_USD_EST:
        raise HTTPException(status_code=429, detail="OpenAI daily spending quota exhausted")

    g_info = daily_quota.info("DEVELOPER_TOKEN")
    if g_info["remaining"] < REQUEST_COST_UNITS:
        raise HTTPException(status_code=429, detail="Daily Google Ads quota exhausted")

    try:
        # Step 1: Generate
        result = generate_keywords(
            topic=payload.topic.strip(),
            brand=(payload.brand or "").strip(),
            modifiers=(payload.modifiers or "").strip(),
            num_keywords=payload.num_keywords,
            model=payload.model.strip(),
        )

        keywords = result.keywords
        usage = result.usage
        model_used = result.model
        usd_spent = result.usd_spent

        # Step 2: Enrich
        enriched_data = enrich_keywords(keywords)

        # Step 3: Prepare rows
        rows = []
        for kw in keywords:
            enriched = enriched_data.get(kw.lower(), {}) or {}
            rows.append({
                "keyword": kw,
                "competition": enriched.get("competition", "NA") or "NA",
                "searchVolume": enriched.get("searchVolume", "NA"),
                "cpc": enriched.get("cpc", "NA"),
            })

        # Step 4: Commit usage
        openai_quota.try_consume(usd_spent)
        daily_quota.try_consume("DEVELOPER_TOKEN", amount=REQUEST_COST_UNITS)

        return {
            "rows": rows,
            "model": model_used,
            "usage": usage,
            "usdSpent": usd_spent,
            "quotaLeft": daily_quota.info("DEVELOPER_TOKEN")["remaining"],
            "openaiQuota": openai_quota.info(),
        }

    except Exception as e:
        logger.exception("Error generating keywords (sync fallback)")
        raise HTTPException(status_code=500, detail=str(e))


# --- Quota endpoints ---
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



# --- Serve frontend pages ---
@app.get("/login")
async def serve_login():
    return FileResponse(os.path.join(frontend_path, "login.html"))


@app.get("/frontend/dashboard")
async def serve_dashboard():
    return FileResponse(os.path.join(frontend_path, "index.html"))


@app.get("/")
async def serve_root():
    return FileResponse(os.path.join(frontend_path, "login.html"))


# --- Whoami endpoint ---
@app.get("/api/whoami")
def whoami(token: str = Depends(get_token)):
    claims = verify_token(token)
    return {
        "preferred_username": claims.get("preferred_username"),
        "tid": claims.get("tid"),
        "aud": claims.get("aud"),
        "roles": claims.get("roles"),
        "groups": claims.get("groups"),
    }
