# backend/daily_quota.py
import json
import os
import tempfile
import logging
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, Tuple
from contextlib import contextmanager
from zoneinfo import ZoneInfo

# -------- logging --------
logger = logging.getLogger(__name__)

# -------- cross-platform file lock (no extra deps) --------
try:
    import fcntl  # Unix
    _HAVE_FCNTL = True
except ImportError:
    _HAVE_FCNTL = False
    import msvcrt  # Windows


@contextmanager
def file_lock(lock_path: str):
    """Lock a sidecar .lock file. Blocks until acquired. Releases on exit."""
    os.makedirs(os.path.dirname(lock_path) or ".", exist_ok=True)
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


def _today_uk_str() -> str:
    uk_tz = ZoneInfo("Europe/London")
    return datetime.now(uk_tz).strftime("%Y-%m-%d")

def _next_reset_iso_uk() -> str:
    uk_tz = ZoneInfo("Europe/London")
    now_uk = datetime.now(uk_tz)
    tomorrow = (now_uk + timedelta(days=1)).date()
    reset_uk = datetime(tomorrow.year, tomorrow.month, tomorrow.day, tzinfo=uk_tz)
    return reset_uk.isoformat()



# -------- exceptions --------
class QuotaError(Exception):
    """Base class for quota errors."""


class QuotaExceeded(QuotaError):
    """Raised when a quota limit is exceeded."""


# -------- manager --------
class DailyQuotaManager:
    """
    File format (JSON):
    {
      "DEVELOPER_TOKEN": {"date": "YYYY-MM-DD", "used": 1234}
    }
    """

    def __init__(self, file_path: str = "quota_daily.json", limit_per_day: int = 5_000) -> None:
        if limit_per_day < 0:
            raise ValueError("limit_per_day must be >= 0")
        self.file_path = file_path
        self.lock_path = f"{file_path}.lock"
        self.limit = int(limit_per_day)

    # --- internal IO ---
    def _load(self) -> Dict[str, Dict[str, Any]]:
        try:
            with open(self.file_path, "r", encoding="utf-8") as f:
                obj = json.load(f)
                return obj if isinstance(obj, dict) else {}
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def _save_atomic(self, data: Dict[str, Dict[str, Any]]) -> None:
        directory = os.path.dirname(self.file_path) or "."
        os.makedirs(directory, exist_ok=True)
        with tempfile.NamedTemporaryFile("w", delete=False, dir=directory, encoding="utf-8") as tmp:
            json.dump(data, tmp, indent=2)
            tmp.flush()
            os.fsync(tmp.fileno())
            tmp_name = tmp.name
        os.replace(tmp_name, self.file_path)

    def _ensure_today(self, data: Dict[str, Dict[str, Any]], username: str) -> None:
        today = _today_uk_str()
        rec = data.get(username)
        if not rec or rec.get("date") != today:
            logger.info("Resetting quota for %s", username)
            data[username] = {"date": today, "used": 0}

    # --- public API ---
    def get_remaining(self, username: str) -> int:
        if not username or not isinstance(username, str):
            raise ValueError("username must be a non-empty string")
        with file_lock(self.lock_path):
            data = self._load()
            self._ensure_today(data, username)
            used = int(data[username].get("used", 0))
            remaining = max(self.limit - used, 0)
            logger.debug("Quota check for %s: used=%s, remaining=%s", username, used, remaining)
            return remaining

    def try_consume(self, username: str, amount: int = 1) -> Tuple[bool, int]:
        """Attempt to consume amount units. Returns (ok, remaining)."""
        if not username or not isinstance(username, str):
            raise ValueError("username must be a non-empty string")
        if not isinstance(amount, int) or amount < 0:
            raise ValueError("amount must be a non-negative integer")

        with file_lock(self.lock_path):
            data = self._load()
            self._ensure_today(data, username)
            used = int(data[username].get("used", 0))

            if used + amount > self.limit:
                remaining = max(self.limit - used, 0)
                logger.warning("Quota exceeded for %s: requested=%s, used=%s, limit=%s",
                               username, amount, used, self.limit)
                return False, remaining

            data[username]["used"] = used + amount
            self._save_atomic(data)
            remaining = self.limit - data[username]["used"]
            logger.info("Quota consumed for %s: amount=%s, new_used=%s, remaining=%s",
                        username, amount, used + amount, remaining)
            return True, remaining

    def consume_or_raise(self, username: str, amount: int = 1) -> int:
        """
        Attempt to consume quota or raise QuotaExceeded.
        Returns remaining after consumption.
        """
        ok, remaining = self.try_consume(username, amount)
        if not ok:
            raise QuotaExceeded(f"Quota exceeded for {username}, remaining={remaining}")
        return remaining

    def info(self, username: str) -> Dict[str, Any]:
        if not username or not isinstance(username, str):
            raise ValueError("username must be a non-empty string")
        with file_lock(self.lock_path):
            data = self._load()
            self._ensure_today(data, username)
            used = int(data[username].get("used", 0))
            info = {
                "date": data[username]["date"],
                "used": used,
                "limit": self.limit,
                "remaining": max(self.limit - used, 0),
                "resetAt": _next_reset_iso_uk(),
            }
            logger.debug("Quota info for %s: %s", username, info)
            return info
