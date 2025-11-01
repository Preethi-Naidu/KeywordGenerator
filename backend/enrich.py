# backend/enrich.py

from typing import Dict, List
from google.ads.googleads.errors import GoogleAdsException
from backend.rate_limit import enqueue_and_wait
from backend.config import logger
from backend.google_ads_client import get_google_ads_client

client = get_google_ads_client()

class GoogleEnrichError(Exception):
    """Raised when Google Ads enrichment fails."""


def enrich_keywords(keywords: List[str]) -> Dict[str, Dict[str, str]]:
    """
    Enriches keywords with Google Ads historical metrics (volume, competition, CPC).

    Args:
        keywords: List of keyword strings.

    Returns:
        dict mapping lowercase keyword -> {"searchVolume": str, "competition": str, "cpc": str}

    Raises:
        GoogleEnrichError: If the Google Ads API call fails.
    """
    enriched: Dict[str, Dict[str, str]] = {}

    try:
        logger.info("Starting enrichment for %s keywords", len(keywords))

        client = get_google_ads_client()
        service = client.get_service("KeywordPlanIdeaService")

        # Wait for our turn based on shared developer token / login_customer_id
        enqueue_and_wait(client.login_customer_id)

        request = client.get_type("GenerateKeywordHistoricalMetricsRequest")
        request.customer_id = client.login_customer_id
        request.keywords.extend(keywords)
        request.keyword_plan_network = client.enums.KeywordPlanNetworkEnum.GOOGLE_SEARCH

        response = service.generate_keyword_historical_metrics(request=request)

        for idea in response.results:
            kw = idea.text.lower()
            metrics = idea.keyword_metrics

            search_volume = (
                str(metrics.avg_monthly_searches)
                if metrics.avg_monthly_searches
                else "NA"
            )
            competition = (
                metrics.competition.name
                if metrics.competition.name != "UNSPECIFIED"
                else "NA"
            )
            cpc_micros = metrics.high_top_of_page_bid_micros
            cpc = f"£{cpc_micros / 1_000_000:.2f}" if cpc_micros else "NA"

            enriched[kw] = {
                "searchVolume": search_volume,
                "competition": competition,
                "cpc": cpc,
            }

        logger.info("Google Ads enrichment completed successfully for %s keywords", len(enriched))
        return enriched

    except GoogleAdsException as ex:
        logger.error("Google Ads API error: %s", ex)
        raise GoogleEnrichError(f"Google Ads API error: {ex}") from ex
    except Exception as ex:
        logger.exception("Unexpected error during Google Ads enrichment")
        raise GoogleEnrichError(f"Unexpected error during enrichment: {ex}") from ex
