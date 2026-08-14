"""Reading and writing the internal transaction ledger.

The ledger is the merchant's own record of what happened. In production this
would be a database view; here it is a CSV, which keeps the whole system
runnable with no infrastructure.
"""

from __future__ import annotations

import csv
from datetime import date, datetime
from pathlib import Path
from typing import Iterable, Sequence

from .models import Transaction, TransactionStatus
from .money import Money

FIELDS = (
    "transaction_id",
    "processor",
    "country",
    "payment_method",
    "amount",
    "currency",
    "status",
    "created_at",
    "captured_at",
)


def load_transactions(path: Path) -> list[Transaction]:
    rows = csv.DictReader(path.read_text(encoding="utf-8-sig").splitlines())
    return [_row_to_transaction(row) for row in rows]


def save_transactions(transactions: Sequence[Transaction], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        for txn in transactions:
            writer.writerow(
                {
                    "transaction_id": txn.transaction_id,
                    "processor": txn.processor,
                    "country": txn.country,
                    "payment_method": txn.payment_method,
                    "amount": txn.amount.amount,
                    "currency": txn.amount.currency,
                    "status": txn.status.value,
                    "created_at": txn.created_at.isoformat(),
                    "captured_at": txn.captured_at.isoformat() if txn.captured_at else "",
                }
            )


def _row_to_transaction(row: dict[str, str]) -> Transaction:
    return Transaction(
        transaction_id=row["transaction_id"].strip(),
        processor=row["processor"].strip(),
        country=row["country"].strip(),
        payment_method=row["payment_method"].strip(),
        amount=Money.parse(row["amount"], row["currency"]),
        status=TransactionStatus(row["status"].strip()),
        created_at=_date(row["created_at"]),
        captured_at=_date(row["captured_at"]),
    )


def _date(value: str) -> date | None:
    value = (value or "").strip()
    return datetime.strptime(value, "%Y-%m-%d").date() if value else None


def summarise(transactions: Iterable[Transaction]) -> dict[str, int]:
    """Status counts, used by the CLI to describe a freshly generated ledger."""
    counts: dict[str, int] = {}
    for txn in transactions:
        counts[txn.status.value] = counts.get(txn.status.value, 0) + 1
    return dict(sorted(counts.items()))
