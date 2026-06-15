from __future__ import annotations

import gc
import os
import platform
import subprocess
import time
from pathlib import Path
from typing import Callable

from reqahe.infra.io import append_jsonl


class CloseWaitCleaner:
    """Periodically release Python-side HTTP resources during long rollouts."""

    def __init__(
        self,
        enabled: bool = True,
        interval_tasks: int = 1,
        interval_seconds: float = 180.0,
        log_path: str | Path | None = None,
    ):
        self.enabled = enabled
        self.interval_tasks = max(1, int(interval_tasks))
        self.interval_seconds = max(1.0, float(interval_seconds))
        self.log_path = Path(log_path) if log_path else None
        self._last_cleanup_at = 0.0
        self._tasks_since_cleanup = 0

    def maybe_cleanup(
        self,
        label: str,
        close_resources: list[Callable[[], None]] | None = None,
        force: bool = False,
    ) -> None:
        if not self.enabled:
            return
        self._tasks_since_cleanup += 1
        now = time.monotonic()
        elapsed = now - self._last_cleanup_at if self._last_cleanup_at else self.interval_seconds
        if not force and self._tasks_since_cleanup < self.interval_tasks and elapsed < self.interval_seconds:
            return
        self.cleanup(label, close_resources=close_resources)

    def cleanup(self, label: str, close_resources: list[Callable[[], None]] | None = None) -> None:
        if not self.enabled:
            return
        before = count_current_process_close_wait()
        for close_resource in close_resources or []:
            try:
                close_resource()
            except Exception:
                pass
        collected = gc.collect()
        after = count_current_process_close_wait()
        event = {
            "label": label,
            "pid": os.getpid(),
            "close_wait_before": before,
            "close_wait_after": after,
            "gc_collected": collected,
        }
        print(
            f"[close-wait] cleanup label={label} pid={event['pid']} "
            f"before={before if before is not None else 'unknown'} "
            f"after={after if after is not None else 'unknown'} gc_collected={collected}",
            flush=True,
        )
        if self.log_path:
            append_jsonl(self.log_path, event)
        self._last_cleanup_at = time.monotonic()
        self._tasks_since_cleanup = 0


def count_current_process_close_wait() -> int | None:
    if platform.system().lower() != "windows":
        return None
    try:
        result = subprocess.run(
            ["netstat", "-ano"],
            check=False,
            capture_output=True,
            text=True,
            timeout=20,
        )
    except Exception:
        return None
    if result.returncode != 0:
        return None
    current_pid = str(os.getpid())
    count = 0
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[-2].upper() == "CLOSE_WAIT" and parts[-1] == current_pid:
            count += 1
    return count
