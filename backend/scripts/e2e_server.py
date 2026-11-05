"""Throw-away backend for the live end-to-end suite and the recorded demo. Never production.

    python -m scripts.e2e_server                     # port 8100, PWA origin http://localhost:3101
    python -m scripts.e2e_server --port 8000 --origin http://localhost:3000 --groq

Every start wipes ``--state-dir`` (default ``data/e2e-live``) and runs with:

* fresh random ``MASTER_KEY`` / ``QUOTE_HMAC_KEY`` (nothing survives a restart);
* ``TODAY_OVERRIDE=2026-07-15`` — the dataset's "today", so K11's bill is stale;
* the provided dataset in ``mock_data`` and local Tesseract;
* Groq **unreachable** unless ``--groq`` is given (base URL on a closed local port), so the
  advisor runs its guided-form fallback deterministically (E2E-05);
* raised rate limits (one browser drives many journeys) and the demo staff users
  ``sales`` / ``partner`` / ``reviewer`` / ``admin`` with ``BACKOFFICE_DEMO_PASSWORD``
  (default ``demo-password-2026``).
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import shutil
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Final

DEFAULT_PASSWORD: Final = "demo-password-2026"  # noqa: S105  gitleaks:allow - throw-away demo users
_WINDOWS_TESSERACT: Final = Path(r"C:\Program Files\Tesseract-OCR\tesseract.exe")

_RAISED_LIMITS: Final = {
    "API_IP_RATE_PER_MINUTE": "5000",
    "ADVISOR_RATE_PER_MINUTE": "500",
    "QUOTE_RATE_PER_MINUTE": "500",
    "OTP_IP_RATE_PER_MINUTE": "200",
    "OTP_REQUESTS_PER_WINDOW": "50",
    "CHECKOUT_IP_RATE_PER_MINUTE": "500",
    "CHECKOUT_RATE_PER_MINUTE": "500",
    "KYC_IP_RATE_PER_MINUTE": "500",
    "KYC_UPLOAD_RATE_PER_MINUTE": "500",
    "LOOKUP_IP_RATE_PER_MINUTE": "500",
    "LOOKUP_RATE_PER_MINUTE": "500",
    "STAFF_LOGIN_IP_RATE_PER_MINUTE": "200",
    "BACKOFFICE_RATE_PER_MINUTE": "1000",
    "PRIVACY_RATE_PER_MINUTE": "200",
}


def _tesseract() -> str:
    found = shutil.which("tesseract")
    if found:
        return found
    return str(_WINDOWS_TESSERACT) if _WINDOWS_TESSERACT.is_file() else "tesseract"


def configure(args: argparse.Namespace) -> Path:
    """Set the environment *before* any ``app`` module reads settings."""
    state = Path(args.state_dir).resolve()
    if state.exists():
        shutil.rmtree(state)
    state.mkdir(parents=True)
    env = {
        "ENVIRONMENT": "development",
        "DATABASE_URL": f"sqlite:///{(state / 'udyam.db').as_posix()}",
        "KYC_STORAGE_DIR": str(state / "kyc_blobs"),
        "MASTER_KEY": secrets.token_urlsafe(48),
        "QUOTE_HMAC_KEY": secrets.token_urlsafe(48),
        "TODAY_OVERRIDE": "2026-07-15",
        "TESSERACT_CMD": os.environ.get("TESSERACT_CMD") or _tesseract(),
        "CORS_ORIGINS": json.dumps(args.origin),
        "PUBLIC_APP_URL": args.origin[0],
        "PAYMENT_FAILURE_RATE": "0",
        "LOOKUP_FAULT_MODE": "off",
        "BACKOFFICE_DEMO_PASSWORD": os.environ.get("BACKOFFICE_DEMO_PASSWORD") or DEFAULT_PASSWORD,
        # Every advisor session counts as "unclosed" after a minute (back-office demo).
        "BACKOFFICE_IDLE_MINUTES": "1",
        **_RAISED_LIMITS,
    }
    if not args.groq:
        env |= {
            "GROQ_API_KEY": "disabled",  # any non-empty value; the base URL is closed
            "GROQ_BASE_URL": "http://127.0.0.1:9/openai/v1",
            "GROQ_TIMEOUT_SECONDS": "1",
        }
    os.environ.update(env)
    return state


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--port", type=int, default=8100)
    parser.add_argument("--origin", action="append", help="PWA origin (repeatable)")
    parser.add_argument("--state-dir", default="data/e2e-live")
    parser.add_argument("--groq", action="store_true", help="use GROQ_API_KEY from the env")
    args = parser.parse_args(argv)
    args.origin = args.origin or ["http://localhost:3101"]
    if os.environ.get("ENVIRONMENT") == "production":
        print("e2e_server is never run in production", file=sys.stderr)
        return 2

    state = configure(args)

    # Imported only now: settings are read (and cached) on first use.
    import uvicorn

    from app.main import create_app
    from scripts import create_staff

    if create_staff.main(["--demo"]) != 0:
        return 2
    print(f"state: {state} - PWA origin(s): {', '.join(args.origin)}", flush=True)
    uvicorn.run(create_app(), host="127.0.0.1", port=args.port, server_header=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
