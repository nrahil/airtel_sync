"""Identifier primitives and the city directory (D13, D22)."""

from __future__ import annotations

import pytest

from app.domain import cities as cities_module
from app.domain.cities import CityDirectory, CityInfo
from app.domain.identifiers import (
    gstin_check_char,
    is_gstin_format,
    is_udyam_format,
    is_valid_gstin,
)
from app.domain.leads import load_leads
from tests.conftest import FIXTURE_DATA_DIR


@pytest.mark.parametrize(
    ("gstin", "valid"),
    [
        ("27ABCDE1234F1Z0", True),
        ("29PQRSX5678K1ZU", True),
        ("27ABCDE1234F1Z1", False),  # wrong check char
        ("08YKZXS8189L1Z0", False),  # K06 specimen as printed (digit altered)
        ("27abcde1234f1z0", False),  # lower case is not normalised here
        ("27ABCDE1234F0Z0", False),  # entity code 0 not allowed
        ("", False),
    ],
)
def test_gstin_validity(gstin: str, valid: bool) -> None:
    assert is_valid_gstin(gstin) is valid


def test_gstin_format_is_independent_of_checksum() -> None:
    assert is_gstin_format("27ABCDE1234F1Z1")
    assert not is_valid_gstin("27ABCDE1234F1Z1")


def test_check_char_rejects_bad_input() -> None:
    with pytest.raises(ValueError, match="14"):
        gstin_check_char("27abc")


@pytest.mark.parametrize(
    ("udyam", "ok"),
    [
        ("UDYAM-MH-00-1234567", True),
        ("UDYAM-MH-0-1234567", False),
        ("UDYAM-MH-00-123456", False),
        ("udyam-mh-00-1234567", False),
        ("", False),
    ],
)
def test_udyam_format(udyam: str, ok: bool) -> None:
    assert is_udyam_format(udyam) is ok


# ------------------------------------------------------------------ cities


@pytest.fixture
def directory() -> CityDirectory:
    return CityDirectory.from_leads(load_leads(FIXTURE_DATA_DIR / "smb_leads.csv"))


def test_city_directory_from_leads(directory: CityDirectory) -> None:
    assert directory.names == ("Delhi", "Mumbai", "Mysuru", "Surat")
    assert directory.lookup("Mumbai") == CityInfo("Mumbai", 1, "Maharashtra")
    assert directory.lookup("Mysuru") == CityInfo("Mysuru", 2, "Karnataka")


def test_city_lookup_tolerates_case_and_whitespace_only(directory: CityDirectory) -> None:
    assert directory.lookup("  mumbai ") == CityInfo("Mumbai", 1, "Maharashtra")
    assert directory.lookup("MYSURU") is not None


@pytest.mark.parametrize("name", ["Mysor", "Mumbay", "Pune", "", "   "])
def test_unknown_city_is_never_guessed(directory: CityDirectory, name: str) -> None:
    """D22: an unknown city returns None so the advisor asks; no fuzzy tier guess."""
    assert directory.lookup(name) is None


@pytest.mark.parametrize(
    ("name", "city"), [("Mysore", "Mysuru"), ("Bombay", "Mumbai"), ("मुंबई", "Mumbai")]
)
def test_listed_old_names_and_scripts_are_the_same_city(
    directory: CityDirectory, name: str, city: str
) -> None:
    """Round 2 Phase 5: an exact, listed other name of a served city is not a guess."""
    info = directory.lookup(name)
    assert info is not None
    assert info.name == city


def test_city_module_has_no_pincode_logic() -> None:
    """D13: dataset pincodes are synthetic; nothing may be derived from them."""
    source = cities_module.__doc__ or ""
    assert "pincode" in source.lower()  # the decision is documented ...
    names = [n.lower() for n in dir(cities_module)]
    assert not [n for n in names if any(w in n for w in ("pincode", "postal", "zip_"))]
