# backend/openai_quota.py
import json
import os
import tempfile
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path
from threading import Lock
from contextlib import contextmanager

logger = logging.getLogger(__name__)

# -------- cross-platform file lock --------
try:
    import fcntl  # Unix
    _HAVE_FCNTL = True
except Exception:
    _HAVE_FCNTL = False
    import msvcrt  # Windows


@contextmanager
def file_lock(lock_path: Path):
    """Cross-process lock using a sidecar .lock file."""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    f = open(lock_path, "a+")
    try:
        if _HAVE_FCNTL:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        else:
            msvcrt.locking(f.fileno(), msvcrt.LK_LOCK, 1)
        yield
    finally:
        try:
            if _HAVE_FCNTL:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)
            else:
                msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
        finally:
            f.close()


# -------- helpers --------
def _today_utc() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def _next_reset_iso_utc() -> str:
    now = datetime.now(timezone.utc)
    tomorrow = (now + timedelta(days=1)).date()
    reset = datetime(tomorrow.year, tomorrow.month, tomorrow.day, tzinfo=timezone.utc)
    return reset.isoformat()


# -------- exceptions --------
class QuotaError(Exception):
    """Base class for quota errors."""


class QuotaExceeded(QuotaError):
    """Raised when a quota limit is exceeded."""


# -------- Global quota (no reset) --------
class OpenAIGlobalQuotaManager:
    """Tracks a single global OpenAI spend limit in USD. Does NOT reset daily."""

    def __init__(self, file_path: str, global_limit_usd: float):
        if global_limit_usd < 0:
            raise ValueError("global_limit_usd must be >= 0")
        self.file_path = Path(file_path)
        self.lock_path = self.file_path.with_suffix(self.file_path.suffix + ".lock")
        self.global_limit_usd = float(global_limit_usd)
        self._thread_lock = Lock()

    def _load(self) -> dict:
        try:
            with open(self.file_path, "r", encoding="utf-8") as f:
                obj = json.load(f)
                return obj if isinstance(obj, dict) else {}
        except (FileNotFoundError, json.JSONDecodeError):
            return {}


    def _save_atomic(self, data: dict) -> None:
        self.file_path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile("w", delete=False, dir=str(self.file_path.parent), encoding="utf-8") as tmp:
            json.dump(data, tmp, indent=2)
            tmp.flush()
            os.fsync(tmp.fileno())
            tmp_name = tmp.name
        os.replace(tmp_name, self.file_path)

    def try_consume(self, amount_usd: float) -> bool:
        if amount_usd < 0:
            raise ValueError("amount_usd must be >= 0")
        with self._thread_lock, file_lock(self.lock_path):
            data = self._load()
            usage = float(data.get("usage", 0.0))
            if usage + amount_usd > self.global_limit_usd:
                logger.warning("Global OpenAI quota exceeded (used=%.2f, requested=%.2f, limit=%.2f)",
                               usage, amount_usd, self.global_limit_usd)
                return False
            data["usage"] = round(usage + amount_usd, 6)
            self._save_atomic(data)
            logger.info("Consumed $%.6f OpenAI global quota (total=%.6f)", amount_usd, data["usage"])
            return True

    def consume_or_raise(self, amount_usd: float) -> None:
        if not self.try_consume(amount_usd):
            raise QuotaExceeded("Global OpenAI quota exhausted")

    def remaining_usd(self) -> float:
        with self._thread_lock, file_lock(self.lock_path):
            data = self._load()
            return max(self.global_limit_usd - float(data.get("usage", 0.0)), 0.0)

    def info(self) -> dict:
        with self._thread_lock, file_lock(self.lock_path):
            data = self._load()
            used = float(data.get("usage", 0.0))
            return {
                "limit_usd": self.global_limit_usd,
                "used_usd": used,
                "remaining_usd": max(self.global_limit_usd - used, 0.0),
            }


# -------- Daily quota (resets at UTC midnight) --------
class OpenAIDailyQuotaManager:
    """Tracks a global daily OpenAI spend limit in USD. Resets daily."""

    def __init__(self, file_path: str, daily_limit_usd: float):
        if daily_limit_usd < 0:
            raise ValueError("daily_limit_usd must be >= 0")
        self.file_path = Path(file_path)
        self.lock_path = self.file_path.with_suffix(self.file_path.suffix + ".lock")
        self.daily_limit_usd = float(daily_limit_usd)
        self._thread_lock = Lock()

    def _load(self) -> dict:
        try:
            with open(self.file_path, "r", encoding="utf-8") as f:
                obj = json.load(f)
                return obj if isinstance(obj, dict) else {}
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def _save_atomic(self, data: dict) -> None:
        self.file_path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile("w", delete=False, dir=str(self.file_path.parent), encoding="utf-8") as tmp:
            json.dump(data, tmp, indent=2)
            tmp.flush()
            os.fsync(tmp.fileno())
            tmp_name = tmp.name
        os.replace(tmp_name, self.file_path)

    def _ensure_today(self, data: dict) -> dict:
        today = _today_utc()
        if data.get("date") != today:
            logger.info("Resetting OpenAI daily quota")
            return {"date": today, "usage": 0.0}
        return {"date": data.get("date", today), "usage": float(data.get("usage", 0.0))}

    def try_consume(self, amount_usd: float) -> bool:
        """Attempt to consume amount_usd. Returns True on success, False if exceeded."""
        if amount_usd < 0:
            raise ValueError("amount_usd must be >= 0")
        with self._thread_lock, file_lock(self.lock_path):
            data = self._ensure_today(self._load())
            usage = float(data["usage"])
            if usage + amount_usd > self.daily_limit_usd:
                logger.warning("Daily OpenAI quota exceeded (used=%.2f, requested=%.2f, limit=%.2f)",
                               usage, amount_usd, self.daily_limit_usd)
                return False
            data["usage"] = round(usage + amount_usd, 6)
            self._save_atomic(data)
            logger.info("Consumed $%.6f OpenAI daily quota (total=%.6f)", amount_usd, data["usage"])
            return True

    def consume_or_raise(self, amount_usd: float) -> None:
        if not self.try_consume(amount_usd):
            raise QuotaExceeded("Daily OpenAI quota exhausted")

    def remaining_usd(self) -> float:
        with self._thread_lock, file_lock(self.lock_path):
            data = self._ensure_today(self._load())
            return max(self.daily_limit_usd - float(data["usage"]), 0.0)

    def info(self) -> dict:
        with self._thread_lock, file_lock(self.lock_path):
            data = self._ensure_today(self._load())
            used = float(data["usage"])
            return {
                "date": data["date"],
                "limit_usd": self.daily_limit_usd,
                "used_usd": used,
                "remaining_usd": max(self.daily_limit_usd - used, 0.0),
                "resetAt": _next_reset_iso_utc(),
            }
