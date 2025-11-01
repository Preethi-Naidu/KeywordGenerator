import logging
from google.ads.googleads.client import GoogleAdsClient

logger = logging.getLogger(__name__)

def get_google_ads_client():
    try:
        # Loads credentials from google-ads.yaml
        client = GoogleAdsClient.load_from_storage("google-ads.yaml")
        return client
    except Exception as e:
        logger.error("Unexpected error initializing Google Ads client", exc_info=True)
        raise
