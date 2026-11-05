"""CLI entry points: scripts/load_data.py and scripts/audit_data.py."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import DatasetFileRecord
from app.db.session import make_engine
from scripts import audit_data, load_data
from tests.conftest import FIXTURE_DATA_DIR, replace_in_file


def test_load_data_validates_and_records_hashes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    url = f"sqlite:///{tmp_path / 'app.db'}"
    args = ["--data-dir", str(FIXTURE_DATA_DIR), "--database-url", url]

    assert load_data.main(args) == 0
    assert load_data.main(args) == 0  # re-running is idempotent

    out = capsys.readouterr().out
    assert "smb_leads.csv" in out
    assert "OK" in out
    engine = make_engine(url)
    with Session(engine) as db:
        rows = db.scalars(select(DatasetFileRecord)).all()
    engine.dispose()
    assert sorted(r.name for r in rows) == [
        "mock_gstin_registry.csv",
        "mock_udyam_registry.csv",
        "product_catalogue.csv",
        "smb_leads.csv",
    ]


def test_load_data_check_only_writes_nothing(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    rc = load_data.main(
        [
            "--data-dir",
            str(FIXTURE_DATA_DIR),
            "--database-url",
            f"sqlite:///{db_path}",
            "--check-only",
        ]
    )
    assert rc == 0
    assert not db_path.exists()


def test_load_data_fails_loudly_on_malformed_input(
    data_dir: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    replace_in_file(data_dir / "product_catalogue.csv", ",1;2,1,1,N,,5,", ",1;3,1,1,N,,5,")
    rc = load_data.main(["--data-dir", str(data_dir), "--check-only"])

    assert rc == 1
    err = capsys.readouterr().err
    assert "product_catalogue.csv" in err
    assert "line 2" in err
    assert "invalid city tier" in err


def test_audit_cli_markdown_and_json(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    common = ["--data-dir", str(FIXTURE_DATA_DIR), "--today", "2026-07-15"]
    assert audit_data.main(common) == 0
    assert "# Data audit (today = 2026-07-15)" in capsys.readouterr().out

    out_file = tmp_path / "audit.json"
    assert audit_data.main([*common, "--json", "--output", str(out_file)]) == 0
    payload = json.loads(out_file.read_text(encoding="utf-8"))
    assert payload["anomalies"]["future_activated"] == ["L00004"]


def test_audit_cli_defaults_to_injected_clock(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("TODAY_OVERRIDE", "2026-07-31")
    assert audit_data.main(["--data-dir", str(FIXTURE_DATA_DIR)]) == 0
    assert "today = 2026-07-31" in capsys.readouterr().out


def test_audit_cli_reports_malformed_data(
    data_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (data_dir / "smb_leads.csv").unlink()
    assert audit_data.main(["--data-dir", str(data_dir)]) == 1
    assert "not found" in capsys.readouterr().err


def test_sla_scan_runs_on_an_empty_database(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from app.config import get_settings
    from scripts import sla_scan

    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'scan.db'}")
    get_settings.cache_clear()
    try:
        assert sla_scan.main([]) == 0
    finally:
        get_settings.cache_clear()
    assert "0 order(s) newly breached" in capsys.readouterr().out
