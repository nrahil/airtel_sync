"""Registry access with the failure modes of a real remote service.

* :class:`RegistryProvider` loads the registries lazily and caches them. Missing or
  malformed data is an *outage* (``RegistryUnavailableError`` → ``KYC_PENDING``),
  not a crash, and the next call retries.
* :class:`FaultInjector` simulates latency, hard outages and flaky failures for
  resilience testing; every call is bounded by a timeout.
"""

from __future__ import annotations

import asyncio
import logging
import random
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, TypeVar

from app.domain.csv_schema import DataValidationError
from app.domain.dataset import DATA_FILES
from app.domain.registries import (
    GstinRegistry,
    UdyamRegistry,
    load_gstin_registry,
    load_udyam_registry,
)

log = logging.getLogger("udyam.lookups")

T = TypeVar("T")


class RegistryUnavailableError(Exception):
    """The registry can't answer right now. Callers must never treat this as a pass."""


@dataclass(frozen=True, slots=True)
class Registries:
    gstin: GstinRegistry
    udyam: UdyamRegistry


class RegistryProvider:
    def __init__(self, data_dir: Path) -> None:
        self._data_dir = data_dir
        self._cached: Registries | None = None
        self._lock = threading.Lock()

    def get(self) -> Registries:
        if self._cached is not None:
            return self._cached
        with self._lock:
            if self._cached is None:
                try:
                    self._cached = Registries(
                        gstin=load_gstin_registry(self._data_dir / DATA_FILES["gstin_registry"]),
                        udyam=load_udyam_registry(self._data_dir / DATA_FILES["udyam_registry"]),
                    )
                except DataValidationError as exc:
                    log.error("registry data could not be loaded: %s", exc.errors[0])
                    raise RegistryUnavailableError("registry data unavailable") from exc
            return self._cached


FaultMode = Literal["off", "down", "flaky"]


class FaultInjector:
    def __init__(
        self,
        *,
        mode: FaultMode = "off",
        latency_ms: int = 0,
        failure_rate: float = 0.0,
        timeout_ms: int = 2000,
        rng: random.Random | None = None,
    ) -> None:
        self.mode = mode
        self.latency_ms = latency_ms
        self.failure_rate = failure_rate
        self.timeout_ms = timeout_ms
        self._rng = rng or random.Random()  # noqa: S311 - simulation, not security

    async def _call(self, fn: Callable[[], T]) -> T:
        if self.latency_ms:
            await asyncio.sleep(self.latency_ms / 1000)
        if self.mode == "down":
            raise RegistryUnavailableError("simulated outage")
        if self.mode == "flaky" and self._rng.random() < self.failure_rate:
            raise RegistryUnavailableError("simulated intermittent failure")
        return await asyncio.to_thread(fn)  # file I/O off the event loop

    async def run(self, fn: Callable[[], T]) -> T:
        try:
            return await asyncio.wait_for(self._call(fn), timeout=self.timeout_ms / 1000)
        except TimeoutError as exc:
            raise RegistryUnavailableError("registry timed out") from exc
