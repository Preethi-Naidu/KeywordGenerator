import time
import redis
import uuid

# Connect to Redis
r = redis.Redis(host="localhost", port=6379, decode_responses=True)

# Test connection
try:
    r.ping()
    print("Connected to Redis!")
except redis.ConnectionError:
    print("Redis connection failed.")

RATE_LIMIT_SECONDS = 1  # 1 QPS per customer_id

def enqueue_and_wait(customer_id: str):
    queue_key = f"rate_limit_queue:{customer_id}"
    last_ts_key = f"rate_limit_last_ts:{customer_id}"
    token = str(uuid.uuid4())

    # Add user to FIFO queue
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
            # Allowed to go; update last timestamp
            r.set(last_ts_key, str(now))
            r.lpop(queue_key)  # Remove self from queue
            return
        else:
            # Too soon; wait a bit
            time.sleep(0.1)
