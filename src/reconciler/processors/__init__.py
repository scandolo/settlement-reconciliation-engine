"""Processor registry.

Supporting a new acquirer is: write one adapter module, add it to `ADAPTERS`.
Nothing in the engine, the reports or the CLI needs to change.
"""

from __future__ import annotations

from pathlib import Path

from .adyen import AdyenAdapter
from .base import FeeRate, ProcessorProfile, SettlementAdapter
from .dlocal import DLocalAdapter
from .payu import PayUAdapter
from .stripe import StripeAdapter

ADAPTERS: tuple[SettlementAdapter, ...] = (
    AdyenAdapter(),
    StripeAdapter(),
    DLocalAdapter(),
    PayUAdapter(),
)

PROFILES: dict[str, ProcessorProfile] = {a.profile.name: a.profile for a in ADAPTERS}


class UnknownSettlementFormatError(ValueError):
    """No registered adapter recognised the file."""


def adapter_for(path: Path) -> SettlementAdapter:
    """Pick the adapter that recognises `path`, by sniffing its contents.

    Content sniffing rather than filename convention, because processors change
    their export filenames far more often than their schemas.
    """
    for adapter in ADAPTERS:
        if adapter.sniff(path):
            return adapter
    raise UnknownSettlementFormatError(
        f"no adapter recognised {path.name!r}; registered processors: "
        f"{', '.join(sorted(PROFILES))}"
    )


def load_batch(path: Path):
    """Parse one settlement file into a canonical `SettlementBatch`."""
    return adapter_for(path).parse(path)


def load_batches(directory: Path) -> list:
    """Parse every settlement file in a directory, oldest settlement first."""
    files = sorted(
        path for path in directory.iterdir()
        if path.is_file() and not path.name.startswith(".")
    )
    batches = [load_batch(path) for path in files]
    return sorted(batches, key=lambda b: (b.settlement_date, b.batch_id))


__all__ = [
    "ADAPTERS",
    "PROFILES",
    "FeeRate",
    "ProcessorProfile",
    "SettlementAdapter",
    "UnknownSettlementFormatError",
    "adapter_for",
    "load_batch",
    "load_batches",
]
