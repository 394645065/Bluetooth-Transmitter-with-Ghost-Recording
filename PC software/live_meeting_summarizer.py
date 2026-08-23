import copy
import queue
import threading
import time
from typing import Any

from dashscope_llm import DashScopeMeetingSummaryClient


class LiveMeetingSummarizer:
    def __init__(
        self,
        event_queue: "queue.Queue",
        summary_interval_sec: float = 30.0,
        llm_client: DashScopeMeetingSummaryClient | None = None,
    ) -> None:
        self._event_queue = event_queue
        self._summary_interval_sec = max(1.0, float(summary_interval_sec))
        self._llm_client = llm_client or DashScopeMeetingSummaryClient()
        self._lock = threading.Lock()
        self._pending_segments: list[dict[str, Any]] = []
        self._state = self._default_state()
        self._last_summary_ts = time.monotonic()
        self._worker: threading.Thread | None = None
        self._shutdown = False

    def add_segment(self, segment: dict[str, Any]) -> None:
        if self._shutdown:
            return
        with self._lock:
            self._pending_segments.append(copy.deepcopy(segment))

    def maybe_trigger(self, force: bool = False) -> bool:
        if self._shutdown:
            return False
        with self._lock:
            if self._worker and self._worker.is_alive():
                return False
            if not self._pending_segments:
                return False
            now = time.monotonic()
            if not force and (now - self._last_summary_ts) < self._summary_interval_sec:
                return False
            segments = self._pending_segments
            self._pending_segments = []
            previous_state = copy.deepcopy(self._state)
            self._last_summary_ts = now

        self._worker = threading.Thread(
            target=self._run_summary_job,
            args=(previous_state, segments),
            daemon=True,
        )
        self._worker.start()
        return True

    def reset(self) -> None:
        with self._lock:
            self._pending_segments = []
            self._state = self._default_state()
            self._last_summary_ts = time.monotonic()

    def get_state(self) -> dict[str, Any]:
        with self._lock:
            return copy.deepcopy(self._state)

    def flush(self, timeout_sec: float = 20.0) -> bool:
        deadline = time.monotonic() + max(1.0, timeout_sec)
        while time.monotonic() < deadline:
            self.maybe_trigger(force=True)
            worker = self._worker
            if worker and worker.is_alive():
                worker.join(timeout=0.3)
                continue
            with self._lock:
                if not self._pending_segments:
                    return True
            time.sleep(0.05)
        return False

    def shutdown(self, flush: bool = True, timeout_sec: float = 20.0) -> bool:
        ok = True
        if flush:
            ok = self.flush(timeout_sec=timeout_sec)
        self._shutdown = True
        worker = self._worker
        if worker and worker.is_alive():
            worker.join(timeout=1.0)
        return ok

    def _run_summary_job(
        self,
        previous_state: dict[str, Any],
        segments: list[dict[str, Any]],
    ) -> None:
        try:
            new_state = self._llm_client.summarize_incremental(previous_state, segments)
            with self._lock:
                self._state = copy.deepcopy(new_state)
            self._event_queue.put(
                {
                    "type": "summary_updated",
                    "state": new_state,
                    "timestamp": time.strftime("%H:%M:%S"),
                    "segments": len(segments),
                }
            )
        except Exception as exc:  # noqa: BLE001
            with self._lock:
                self._pending_segments = segments + self._pending_segments
            self._event_queue.put(
                {
                    "type": "summary_error",
                    "error": str(exc),
                    "timestamp": time.strftime("%H:%M:%S"),
                }
            )

    @staticmethod
    def _default_state() -> dict[str, Any]:
        return {
            "running_summary": "",
            "bullets": [],
            "decisions": [],
            "action_items": [],
            "open_questions": [],
            "meta": {"window_start": "", "window_end": "", "language": "zh-en"},
        }
