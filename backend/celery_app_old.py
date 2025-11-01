# backend/celery_app.py
import logging
from backend.celery_app_old import Celery

logger = logging.getLogger(__name__)

celery = Celery(
    "keyword_expansion",
    broker="redis://localhost:6379/0",
    backend="redis://localhost:6379/1",
)

# Default queue
celery.conf.task_default_queue = "celery"

# JSON serialization is safest (avoids pickle issues in production)
celery.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    result_expires=3600,   # results auto-expire in 1h
    worker_max_tasks_per_child=100,  # restart worker processes periodically (avoids leaks)
)

logger.info("✅ Celery app initialized (broker=%s, backend=%s)",
            celery.conf.broker_url, celery.conf.result_backend)

# --- IMPORTANT: import tasks so Celery registers them ---
import backend.tasks
