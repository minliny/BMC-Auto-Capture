"""
Resource monitor — polls CPU and memory via psutil.
Runs in a background thread, exposes latest sample.
"""

from __future__ import annotations

import logging
import threading
import time

logger = logging.getLogger("bmc_auto_capture.monitor")


class ResourceMonitor:
    def __init__(self, interval: float = 5.0):
        self._interval = interval
        self._lock = threading.Lock()
        self._cpu: float = 0.0
        self._mem: float = 0.0
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    @property
    def latest(self) -> tuple[float, float]:
        with self._lock:
            return self._cpu, self._mem

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._poll_loop, daemon=True, name="resource-monitor")
        self._thread.start()
        logger.info("资源监控已启动 (interval=%.1fs)", self._interval)

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5.0)
        logger.info("资源监控已停止")

    def _poll_loop(self):
        import psutil

        while not self._stop.is_set():
            try:
                cpu = psutil.cpu_percent(interval=min(1.0, max(0.0, self._interval)))
                mem = psutil.virtual_memory().percent
                with self._lock:
                    self._cpu = cpu
                    self._mem = mem
                logger.debug("资源采样:  CPU %.1f%%, MEM %.1f%%", cpu, mem)
            except Exception as e:
                logger.warning("资源采样失败: %s", e)

            self._stop.wait(self._interval)
