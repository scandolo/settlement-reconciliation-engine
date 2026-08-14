"""Same-origin Vercel endpoint for browser-uploaded reconciliation files."""

from __future__ import annotations

import json
import sys
from datetime import date
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from reconciler import ledger, reporting  # noqa: E402
from reconciler.engine import ReconciliationEngine  # noqa: E402
from reconciler.processors import PROFILES, load_batch  # noqa: E402
from reconciler.rules import DEFAULT_RULES  # noqa: E402

MAX_BODY_BYTES = 4_000_000
MAX_SETTLEMENT_FILES = 50


class UploadError(ValueError):
    """The uploaded payload cannot be reconciled safely."""


def reconcile_payload(payload: dict[str, Any]) -> dict:
    """Validate uploaded text files, run the engine, and return its report model."""
    if not isinstance(payload, dict):
        raise UploadError("Request body must be a JSON object.")

    ledger_file = _file_entry(payload.get("ledger"), {".csv"}, "ledger")
    settlement_entries = payload.get("settlements")
    if not isinstance(settlement_entries, list) or not settlement_entries:
        raise UploadError("Add at least one settlement report.")
    if len(settlement_entries) > MAX_SETTLEMENT_FILES:
        raise UploadError(f"A run supports at most {MAX_SETTLEMENT_FILES} settlement files.")

    settlement_files = [
        _file_entry(entry, None, "settlement")
        for entry in settlement_entries
    ]
    names = [entry["name"] for entry in settlement_files]
    if len(names) != len(set(names)):
        raise UploadError("Settlement filenames must be unique within a run.")

    try:
        as_of = date.fromisoformat(str(payload.get("as_of")))
    except ValueError as error:
        raise UploadError("Reconciliation date must use YYYY-MM-DD.") from error

    with TemporaryDirectory(prefix="reconcile-upload-") as temporary:
        directory = Path(temporary)
        ledger_path = directory / ledger_file["name"]
        ledger_path.write_text(ledger_file["content"], encoding="utf-8")

        parsed_batches = []
        for entry in settlement_files:
            path = directory / entry["name"]
            path.write_text(entry["content"], encoding="utf-8")
            parsed_batches.append(load_batch(path))

        transactions = ledger.load_transactions(ledger_path)
        parsed_batches.sort(key=lambda batch: (batch.settlement_date, batch.batch_id))
        result = ReconciliationEngine(
            transactions, PROFILES, DEFAULT_RULES
        ).reconcile(parsed_batches, as_of=as_of)
        return reporting.to_dict(result)


def _file_entry(
    entry: Any, allowed_suffixes: set[str] | None, label: str
) -> dict[str, str]:
    if not isinstance(entry, dict):
        raise UploadError(f"The {label} file is missing.")
    name = entry.get("name")
    content = entry.get("content")
    if not isinstance(name, str) or not name.strip():
        raise UploadError(f"The {label} filename is missing.")
    if name != Path(name).name:
        raise UploadError(f"The {label} filename is invalid.")
    if allowed_suffixes is not None and Path(name).suffix.lower() not in allowed_suffixes:
        expected = ", ".join(sorted(allowed_suffixes))
        raise UploadError(f"{name}: unsupported format; expected {expected}.")
    if not isinstance(content, str) or not content.strip():
        raise UploadError(f"{name}: file is empty.")
    return {"name": name, "content": content}


class handler(BaseHTTPRequestHandler):
    """Vercel's Python runtime discovers this handler class."""

    def do_GET(self) -> None:
        self._send_json(200, {"status": "ok", "endpoint": "POST /api/reconcile"})

    def do_POST(self) -> None:
        try:
            content_length = int(self.headers.get("Content-Length", "0"))
            if content_length <= 0:
                raise UploadError("Request body is empty.")
            if content_length > MAX_BODY_BYTES:
                raise UploadError("Upload is too large; keep the request below 4 MB.")
            payload = json.loads(self.rfile.read(content_length))
            self._send_json(200, reconcile_payload(payload))
        except (UploadError, json.JSONDecodeError, UnicodeDecodeError, ValueError) as error:
            self._send_json(400, {"error": str(error)})
        except Exception:
            self._send_json(500, {"error": "The reconciliation could not be completed."})

    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
