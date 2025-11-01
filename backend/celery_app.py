# backend/celery_app.py

import logging
# from celery import Celery

logger = logging.getLogger(__name__)

# Celery temporarily disabled for local testing / Redis-free mode
logger.warning("Celery disabled — running in synchronous mode (no Redis connection).")

# celery = Celery(
#     "keyword_expansion",
#     broker="redis://localhost:6379/0",
#     backend="redis://localhost:6379/1",
# )

# # Default queue
# celery.conf.task_default_queue = "celery"

# celery.conf.update(
#     task_serializer="json",
#     result_serializer="json",
#     accept_content=["json"],
#     result_expires=3600,
#     worker_max_tasks_per_child=100,
# )

# logger.info("Celery app initialized (broker=%s, backend=%s)",
#             celery.conf.broker_url, celery.conf.result_backend)

# # --- Normally imports tasks so Celery registers them ---
# import backend.tasks
