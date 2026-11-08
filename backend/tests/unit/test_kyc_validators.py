"""VA-01…VA-09 — deterministic KYC validators (D2, D3, D7, D8, D12)."""

from __future__ import annotations

from datetime import date

import pytest
from hypothesis import given
from hypothesis import strategies as st

from app.domain.identifiers import gstin_check_char, is_valid_gstin
from app.domain.registries import load_gstin_registry, load_udyam_registry
from app.kyc.validators import (
    IdStatus,
    address_proof_cutoff,
    address_similarity,
    addresses_match,
    business_names_match,
    clean_identifier,
    is_address_proof_fresh,
    is_id_valid,
    join_wrapped,
    names_match,
    parse_date,
    resolve_gstin,
    resolve_udyam,
    split_proprietor,
)
from tests.conftest import BACKEND_DIR, FIXTURE_DATA_DIR, REAL_DATA_DIR, requires_real_dataset

TODAY = date(2026, 7, 15)
KYC = BACKEND_DIR / "tests" / "fixtures" / "kyc"


def never(_value: str) -> bool:
    return False


def registry_of(*values: str):  # type: ignore[no-untyped-def]
    known = set(values)
    return lambda v: v in known


# ------------------------------------------------------------------ VA-01 / VA-06


@pytest.mark.parametrize("data_dir", [FIXTURE_DATA_DIR, KYC])
def test_va_01_registry_gstins_pass_format_and_checksum(data_dir) -> None:  # type: ignore[no-untyped-def]
    name = "mock_gstin_registry.csv" if data_dir == FIXTURE_DATA_DIR else "gstin_registry.csv"
    registry = load_gstin_registry(data_dir / name)
    for record in registry:
        assert resolve_gstin(record.gstin, never).status is IdStatus.VALID


@requires_real_dataset
def test_va_01_all_3534_registry_gstins() -> None:
    registry = load_gstin_registry(REAL_DATA_DIR / "mock_gstin_registry.csv")
    assert len(registry) == 3534
    assert all(resolve_gstin(r.gstin, never).status is IdStatus.VALID for r in registry)


@requires_real_dataset
def test_va_06_all_2723_registry_udyam_numbers() -> None:
    registry = load_udyam_registry(REAL_DATA_DIR / "mock_udyam_registry.csv")
    assert len(registry) == 2723
    assert all(resolve_udyam(r.udyam_number, never).status is IdStatus.VALID for r in registry)


def test_va_06_fixture_udyam_numbers() -> None:
    for record in load_udyam_registry(KYC / "udyam_registry.csv"):
        assert resolve_udyam(record.udyam_number, never).status is IdStatus.VALID


# ------------------------------------------------------------------ VA-02…VA-05


def test_va_02_k06_checksum_fails_and_is_not_corrected() -> None:
    known = registry_of("08YKZXS5189L1ZO")  # the registry neighbour, one digit away
    res = resolve_gstin("08YKZXS8189L1Z0", known)
    assert res.status is IdStatus.INVALID_CHECKSUM
    assert res.value == "08YKZXS8189L1Z0"
    assert not res.accepted


def test_va_02_ocr_variant_of_k06_still_fails() -> None:
    """OCR read 'O8…' (letter O): structure is repaired, the altered digit is not."""
    res = resolve_gstin("O8YKZXS8189L1Z0", registry_of("08YKZXS5189L1ZO"))
    assert res.status is IdStatus.INVALID_CHECKSUM
    assert res.value == "08YKZXS8189L1Z0"


def test_va_03_k02_zero_for_o_is_repaired_with_registry_hit() -> None:
    res = resolve_gstin("19E0GXH4115Y1ZL", registry_of("19EOGXH4115Y1ZL"))
    assert res.status is IdStatus.CORRECTED
    assert res.value == "19EOGXH4115Y1ZL"
    assert [(c.position, c.read, c.corrected) for c in res.corrections] == [(3, "0", "O")]


def test_va_04_k08_zero_for_o_is_repaired() -> None:
    res = resolve_gstin("24X0RXG9365M1Z3", registry_of("24XORXG9365M1Z3"))
    assert res.status is IdStatus.CORRECTED
    assert res.value == "24XORXG9365M1Z3"


def test_va_03_repair_needs_the_registry_hit() -> None:
    res = resolve_gstin("19E0GXH4115Y1ZL", never)
    assert not res.accepted


def test_va_05_repair_with_valid_checksum_but_no_registry_hit_is_rejected() -> None:
    # Build a GSTIN not in the registry whose OCR read has 'S' for '5' in a digit slot.
    first14 = "27ABCDE1534F1Z"
    target = first14 + gstin_check_char(first14)
    read = target[:8] + "S" + target[9:]  # position 8 (a "5") must be a digit
    assert is_valid_gstin(target)
    res = resolve_gstin(read, never)
    assert not res.accepted
    assert res.status is IdStatus.INVALID_FORMAT


def test_repair_only_touches_class_violations() -> None:
    """'0' in a digit slot stays a digit; only listed confusable pairs are used."""
    known = registry_of("27XUIXG0767D1ZP")
    assert resolve_gstin("27XUIXG0767D1ZP", known).status is IdStatus.VALID
    res = resolve_gstin("27XUIXGO767D1ZP", known)  # O in a digit slot
    assert res.status is IdStatus.CORRECTED
    assert res.value == "27XUIXG0767D1ZP"
    # 'Y' looks like no digit: no repair.
    assert not resolve_gstin("27XUIXGY767D1ZP", known).accepted


@given(st.sampled_from(["O", "I", "S", "B", "Z"]), st.integers(min_value=0, max_value=1))
def test_property_digit_slot_confusables_round_trip(letter: str, pos: int) -> None:
    digit = {"O": "0", "I": "1", "S": "5", "B": "8", "Z": "2"}[letter]
    first14 = digit + "7ABCDE1234F1Z" if pos == 0 else "2" + digit + "ABCDE1234F1Z"
    target = first14 + gstin_check_char(first14)
    if not is_valid_gstin(target):
        return
    read = target[:pos] + letter + target[pos + 1 :]
    res = resolve_gstin(read, registry_of(target))
    assert res.status is IdStatus.CORRECTED
    assert res.value == target


def test_gstin_missing_and_noise() -> None:
    assert resolve_gstin("   ", never).status is IdStatus.MISSING
    assert resolve_gstin("27 xuix g0767 d1zp", never).status is IdStatus.VALID
    assert clean_identifier(" udyam- pb-00-9719478 ") == "UDYAM-PB-00-9719478"


def test_udyam_repair_and_rejection() -> None:
    known = registry_of("UDYAM-MH-00-9516696")
    assert resolve_udyam("UDYAM-MH-OO-9516696", known).status is IdStatus.CORRECTED
    assert not resolve_udyam("UDYAM-MH-OO-9516696", never).accepted
    assert resolve_udyam("UDYAM-MH-00-951669", known).status is IdStatus.INVALID_FORMAT
    assert resolve_udyam("", known).status is IdStatus.MISSING


# ------------------------------------------------------------------ VA-07…VA-09


def test_va_07_bill_exactly_90_days_old_passes() -> None:
    assert address_proof_cutoff(TODAY) == date(2026, 4, 16)
    assert is_address_proof_fresh(date(2026, 4, 16), TODAY)


def test_va_08_bill_91_days_old_fails() -> None:
    assert not is_address_proof_fresh(date(2026, 4, 15), TODAY)
    assert not is_address_proof_fresh(date(2025, 10, 31), TODAY)  # K11


def test_va_09_signatory_id_validity() -> None:
    assert not is_id_valid(date(2026, 7, 14), TODAY)
    assert is_id_valid(date(2026, 7, 15), TODAY)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("2026-05-30", date(2026, 5, 30)),
        ("2O26-O5-3O", date(2026, 5, 30)),
        ("2022.04-17", date(2022, 4, 17)),
        ("2026-02-30", None),
        ("no date", None),
    ],
)
def test_parse_date(text: str, expected: date | None) -> None:
    assert parse_date(text) == expected


# ------------------------------------------------------------------ names, addresses, wrapping


def test_names() -> None:
    assert names_match(
        "Joseph Shah (Prop. Harmony Denta} Care)", "Joseph Shah (Prop. Harmony Dental Care)"
    )
    assert not names_match(
        "Neha Banerjee (Prop. Shri Physio Clinic)", "Neha Banerjee (Prop. Neelgiri Physio Clinic)"
    )
    assert not names_match("Nisha Arora", "Fatima Menon")
    assert business_names_match("Kaveri Stays LLP", "Kaveri Stays")
    assert split_proprietor("Joseph Singh (Prop. Ganesh Woodcraft)") == (
        "Joseph Singh",
        "Ganesh Woodcraft",
    )
    assert split_proprietor("Kaveri Stays LLP") is None


@pytest.mark.parametrize(
    ("previous", "continuation", "joined"),
    [
        (
            "Shop/Unit 80, Civil Lines, Nagpur, Maharashtr",
            "a - 411231",
            "Shop/Unit 80, Civil Lines, Nagpur, Maharashtra - 411231",
        ),
        (
            "Shop/Unit 1, Tech Park Road, Visakhapatnam, A",
            "ndhra Pradesh - 118383",
            "Shop/Unit 1, Tech Park Road, Visakhapatnam, Andhra Pradesh - 118383",
        ),
        (
            "Shop/Unit 38, Civil Lines, Kochi, Kerala - 75",
            "2083",
            "Shop/Unit 38, Civil Lines, Kochi, Kerala - 752083",
        ),
        (
            "Shop/Unit 52, Transport Nagar, Delhi, Delhi -",
            "334596",
            "Shop/Unit 52, Transport Nagar, Delhi, Delhi - 334596",
        ),
        (
            "Shop/Unit 71, Market Yard, Jaipur, Rajasthan",
            "- 194057",
            "Shop/Unit 71, Market Yard, Jaipur, Rajasthan - 194057",
        ),
        ("", "first", "first"),
    ],
)
def test_kg_14_join_wrapped(previous: str, continuation: str, joined: str) -> None:
    assert join_wrapped(previous, continuation) == joined


def test_address_similarity() -> None:
    a = "Shop/Unit 80, Civil Lines, Nagpur, Maharashtra - 411231"
    assert address_similarity(a, a) == 1.0
    assert address_similarity(a, a.replace("411231", "110001")) >= 0.6  # pincode ignored-ish
    assert address_similarity(a, "Shop/Unit 21, Sector 14, Mumbai, Maharashtra - 570043") < 0.6
    assert address_similarity("", a) == 0.0


# ------------------------------------------------------------------ unseen-document misreads
# Each read below came from scripts/kyc_stress.py: fresh certificates, real Tesseract.


def test_lookalike_letter_misread_is_repaired_only_with_checksum_and_registry() -> None:
    known = registry_of("08CJCXH5108B1ZC")
    res = resolve_gstin("08CICXH5108B1ZC", known)  # DejaVu Mono 'J' read as 'I'
    assert res.status is IdStatus.CORRECTED
    assert res.value == "08CJCXH5108B1ZC"
    assert [(c.position, c.read, c.corrected) for c in res.corrections] == [(3, "I", "J")]
    assert not resolve_gstin("08CICXH5108B1ZC", never).accepted


@pytest.mark.parametrize(
    ("read", "truth"),
    [
        ("27Q3LXS916601Z5", "27QJLXS9166O1Z5"),  # J→3 and O→0 together
        ("29TWXXAG702Q1Z2", "29TWXXA0702Q1Z2"),  # 0→G in a digit slot
        ("Q8CJCXH5108B1ZC", "08CJCXH5108B1ZC"),  # 0→Q
        ("27XRVXSG6133J1ZX", "27XRVXS6133J1ZX"),  # one stray glyph added
    ],
)
def test_photo_misreads_resolve_to_the_registered_gstin(read: str, truth: str) -> None:
    res = resolve_gstin(read, registry_of(truth))
    assert res.status is IdStatus.CORRECTED
    assert res.value == truth
    assert res.corrections


def test_digit_read_as_another_digit_is_never_repaired() -> None:
    """'0' read as '8' is indistinguishable from an altered digit (K06): report it."""
    res = resolve_gstin("88CJCXH5108B1ZC", registry_of("08CJCXH5108B1ZC"))
    assert res.status is IdStatus.INVALID_CHECKSUM


def test_two_registered_repairs_are_refused() -> None:
    """'H' could be 'N' or 'M': both numbers registered, so neither is guessed."""
    read = "UDYAM-TH-00-8789510"
    assert resolve_udyam(read, registry_of("UDYAM-TN-00-8789510")).value == "UDYAM-TN-00-8789510"
    res = resolve_udyam(read, registry_of("UDYAM-TN-00-8789510", "UDYAM-TM-00-8789510"))
    assert res.value == read
    assert res.status is not IdStatus.CORRECTED


@pytest.mark.parametrize(
    ("read", "truth"),
    [
        ("UDYAK-TW-00-8789510", "UDYAM-TN-00-8789510"),
        ("UDYAH-GJ-00-2265011", "UDYAM-GJ-00-2265011"),
        ("UDYAM-MH-908-2271795", "UDYAM-MH-00-2271795"),
        ("UDYAM-DL-0-0649289", "UDYAM-DL-00-0649289"),
        ("UDYAM-AP-80-9732272", "UDYAM-AP-00-9732272"),
    ],
)
def test_udyam_photo_misreads_resolve_to_the_registered_number(read: str, truth: str) -> None:
    res = resolve_udyam(read, registry_of(truth))
    assert res.status is IdStatus.CORRECTED
    assert res.value == truth


def test_udyam_serial_digit_is_never_swapped() -> None:
    res = resolve_udyam("UDYAM-MH-00-2271796", registry_of("UDYAM-MH-00-2271795"))
    assert res.status is IdStatus.VALID  # well-formed, but not that enterprise
    assert res.value == "UDYAM-MH-00-2271796"


def test_names_match_through_lookalike_glyphs() -> None:
    assert names_match("Lakshmi lyer", "Lakshmi Iyer")  # capital I read as small l
    assert names_match("Rarn Kumar", "Ram Kumar")
    assert not names_match("Lakshmi Iyer", "Lakshmi Iyengar")


def test_addresses_must_share_every_number() -> None:
    registered = "Shop/Unit 80, Civil Lines, Nagpur, Maharashtra - 411231"
    assert addresses_match(
        "Shop/Unit 80, Civil Lines, Nagpur, Maharashtra- 411231", registered, 0.6
    )[0]
    assert not addresses_match(
        "Shop/Unit 81, Civil Lines, Nagpur, Maharashtra - 411231", registered, 0.6
    )[0]
    assert not addresses_match(
        "Shop/Unit 80, Civil Lines, Nagpur, Maharashtra - 411239", registered, 0.6
    )[0]
