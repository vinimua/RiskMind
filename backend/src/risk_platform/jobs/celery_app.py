"""Celery process entry point; no production jobs registered yet."""

import os

from celery import Celery

broker_url = os.environ.get("RISK_REDIS_URL", "redis://localhost:6379/0")
celery_app = Celery("risk_platform", broker=broker_url, backend=broker_url)
