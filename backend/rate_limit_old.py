import os
import time
import uuid

# Optional Redis import
try:
    import redis
except ImportError:
    redis = None

# Use Redis only if explicitly enabled
USE_REDIS = os.getenv("USE_REDIS", "false").lower() == "true"
r = None

if USE_REDIS and redis:
    try:
        r = redis.Redis(
            host=os.getenv("REDIS_HOST", "localhost"),
            port=int(os.getenv("REDIS_PORT", "6379")),
            password=os.getenv("REDIS_PASSWORD", None),
            decode_responses=True,
            ssl=os.getenv("REDIS_SSL", "false").lower() == "true"
        )
        r.ping()
        print("Connected to Redis!")
    except Exception as e:
        print(f"Redis disabled (connection failed): {e}")
        r = None
else:
    print("Redis disabled by configuration (USE_REDIS=false).")

RATE_LIMIT_SECONDS = 1  # 1 QPS per customer_id


def enqueue_and_wait(customer_id: str):
    """
    Rate-limit logic for Google Ads API calls.
    If Redis is disabled, returns immediately (no throttling).
    """
    if not r:
        # Skip Redis-based rate limiting
        print(f"Redis not enabled — skipping rate limiting for {customer_id}")
        return

    queue_key = f"rate_limit_queue:{customer_id}"
    last_ts_key = f"rate_limit_last_ts:{customer_id}"
    token = str(uuid.uuid4())

    try:
        r.rpush(queue_key, token)

        while True:
            # Wait until we're at the front of the queue
            first = r.lindex(queue_key, 0)
            if first != token:
                time.sleep(0.1)
                continue

            # Check last timestamp
            last_ts = r.get(last_ts_key)
            now = time.time()

            if not last_ts or (now - float(last_ts)) >= RATE_LIMIT_SECONDS:
                r.set(last_ts_key, str(now))
                r.lpop(queue_key)
                return
            else:
                time.sleep(0.1)
    except Exception as e:
        print(f"Rate limiter failed for {customer_id}: {e}")
        return
