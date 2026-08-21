import time


class RateLimiter:
    """Token-bucket-ish limiter shared by every upload/download call site in
    graph_client.py: after each chunk (or, for a single-shot small upload,
    the whole payload at once), sleeps just long enough that the average
    rate since construction doesn't exceed limit_bytes_per_sec. A limit of
    None/0 means unlimited - the common, default case - so it must add zero
    overhead then (no clock calls at all, not even a no-op sleep(0))."""

    def __init__(self, limit_bytes_per_sec: float | None):
        self.limit = limit_bytes_per_sec if limit_bytes_per_sec and limit_bytes_per_sec > 0 else None
        self._start = time.monotonic()
        self._sent = 0

    def throttle(self, n: int) -> None:
        if self.limit is None:
            return
        self._sent += n
        elapsed = time.monotonic() - self._start
        expected = self._sent / self.limit
        if expected > elapsed:
            time.sleep(expected - elapsed)
