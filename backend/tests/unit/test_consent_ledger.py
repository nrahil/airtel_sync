"""Consent ledger: unbundled purposes, latest action wins, hash-chained, append-only."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from app.consent.ledger import GENESIS_HASH, ConsentLedger
from app.db.models import AppendOnlyError, ConsentAction, ConsentEvent, ConsentPurpose
from app.db.session import init_db, make_engine

T0 = datetime(2026, 7, 15, 9, 0, tzinfo=UTC)
KYC = ConsentPurpose.KYC_VERIFICATION


class StepClock:
    def __init__(self) -> None:
        self.t = T0

    def now(self) -> datetime:
        self.t += timedelta(seconds=1)
        return self.t

    def today(self) -> date:
        return self.t.date()


@pytest.fixture
def engine() -> Iterator[Engine]:
    eng = make_engine("sqlite://")
    init_db(eng)
    yield eng
    eng.dispose()


@pytest.fixture
def ledger(engine: Engine) -> Iterator[ConsentLedger]:
    with Session(engine) as db:
        yield ConsentLedger(db, StepClock())


def test_no_consent_by_default(ledger: ConsentLedger) -> None:
    assert not ledger.is_granted("p_1", KYC)


def test_grant_then_withdraw(ledger: ConsentLedger) -> None:
    ledger.grant("p_1", KYC, notice_version="v1", locale="hi")
    assert ledger.is_granted("p_1", KYC)
    ledger.withdraw("p_1", KYC, notice_version="v1", locale="hi")
    assert not ledger.is_granted("p_1", KYC)
    ledger.grant("p_1", KYC, notice_version="v2", locale="hi")
    assert ledger.is_granted("p_1", KYC)
    assert [e.action for e in ledger.history("p_1")] == [
        ConsentAction.GRANT,
        ConsentAction.WITHDRAW,
        ConsentAction.GRANT,
    ]


def test_purposes_are_unbundled(ledger: ConsentLedger) -> None:
    ledger.grant("p_1", ConsentPurpose.NEEDS_ASSESSMENT, notice_version="v1", locale="en")
    assert not ledger.is_granted("p_1", KYC)
    assert not ledger.is_granted("p_1", ConsentPurpose.MARKETING)


def test_principals_are_isolated(ledger: ConsentLedger) -> None:
    ledger.grant("p_1", KYC, notice_version="v1", locale="en")
    assert not ledger.is_granted("p_2", KYC)


def test_chain_links_and_verifies(ledger: ConsentLedger) -> None:
    a = ledger.grant("p_1", KYC, notice_version="v1", locale="en")
    b = ledger.grant("p_2", KYC, notice_version="v1", locale="en")
    assert a.prev_hash == GENESIS_HASH
    assert b.prev_hash == a.hash
    assert ledger.verify_chain()


def test_chain_detects_a_forged_row(ledger: ConsentLedger, engine: Engine) -> None:
    ledger.grant("p_1", KYC, notice_version="v1", locale="en")
    with Session(engine) as other:
        other.add(
            ConsentEvent(
                principal_token="p_evil",
                purpose=KYC,
                action=ConsentAction.GRANT,
                notice_version="v1",
                locale="en",
                at=T0,
                prev_hash=GENESIS_HASH,  # does not link to the real tail
                hash="e" * 64,
            )
        )
        other.commit()
    assert not ledger.verify_chain()


def test_ledger_rows_cannot_be_rewritten(ledger: ConsentLedger) -> None:
    event = ledger.grant("p_1", KYC, notice_version="v1", locale="en")
    event.action = ConsentAction.WITHDRAW
    with pytest.raises(AppendOnlyError):
        ledger.session.flush()
