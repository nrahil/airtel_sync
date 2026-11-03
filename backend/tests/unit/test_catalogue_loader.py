"""DA-02 — catalogue loader: strict schema, fails loudly on malformed input."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from app.domain.catalogue import Catalogue, Category, Segment, load_catalogue
from app.domain.csv_schema import DataValidationError
from tests.conftest import FIXTURE_DATA_DIR, replace_in_file

CATALOGUE = "product_catalogue.csv"


def _load(data_dir: Path) -> Catalogue:
    return load_catalogue(data_dir / CATALOGUE)


def _errors(data_dir: Path) -> DataValidationError:
    with pytest.raises(DataValidationError) as exc:
        _load(data_dir)
    return exc.value


# ------------------------------------------------------------------ happy path


def test_loads_typed_products() -> None:
    cat = load_catalogue(FIXTURE_DATA_DIR / CATALOGUE)

    assert len(cat) == 4
    assert cat.ids == ("P01", "P03", "P11", "P23")
    p03 = cat["P03"]
    assert p03.category is Category.CONNECTIVITY
    assert p03.available_city_tiers == frozenset({1})
    assert p03.requires_gstin is True
    assert p03.min_employees == 5
    assert p03.target_segments == frozenset({Segment.PROF, Segment.MULTI, Segment.MFG})
    assert cat["P11"].requires_one_of == ("P01", "P03")
    assert cat["P01"].requires_one_of == ()
    assert cat["P23"].one_time_setup_inr == 1800


def test_content_hash_is_sha256_of_raw_bytes() -> None:
    raw = (FIXTURE_DATA_DIR / CATALOGUE).read_bytes()
    assert load_catalogue(FIXTURE_DATA_DIR / CATALOGUE).content_hash == (
        hashlib.sha256(raw).hexdigest()
    )


def test_content_hash_changes_when_file_changes(data_dir: Path) -> None:
    before = _load(data_dir).content_hash
    replace_in_file(data_dir / CATALOGUE, "Test Fibre 100", "Test Fibre 100 Plus")
    assert _load(data_dir).content_hash != before


def test_catalogue_is_read_only() -> None:
    cat = load_catalogue(FIXTURE_DATA_DIR / CATALOGUE)
    with pytest.raises(TypeError):
        cat.products["P99"] = cat["P01"]  # type: ignore[index]
    with pytest.raises(ValueError, match="frozen"):
        cat["P01"].price_max_inr_per_month = 1  # type: ignore[misc]


def test_unknown_product_is_a_key_error() -> None:
    cat = load_catalogue(FIXTURE_DATA_DIR / CATALOGUE)
    assert "P99" not in cat
    assert cat.get("P99") is None
    with pytest.raises(KeyError):
        cat["P99"]


# ------------------------------------------------------------------ DA-02: header


def test_rejects_missing_column(data_dir: Path) -> None:
    path = data_dir / CATALOGUE
    lines = path.read_text(encoding="utf-8").splitlines()
    header = lines[0].split(",")
    idx = header.index("min_sites")
    # Drop the column from the header only — the strict header check must fire first.
    lines[0] = ",".join(h for i, h in enumerate(header) if i != idx)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    err = _errors(data_dir)
    assert err.file == CATALOGUE
    assert any("missing column(s): min_sites" in e.message for e in err.errors)


def test_rejects_unexpected_column(data_dir: Path) -> None:
    replace_in_file(data_dir / CATALOGUE, "target_segments\n", "target_segments,discount\n")
    err = _errors(data_dir)
    assert any("unexpected column(s): discount" in e.message for e in err.errors)


def test_rejects_empty_file(data_dir: Path) -> None:
    (data_dir / CATALOGUE).write_text("", encoding="utf-8")
    err = _errors(data_dir)
    assert "empty" in err.errors[0].message


def test_rejects_header_only_file(data_dir: Path) -> None:
    path = data_dir / CATALOGUE
    path.write_text(path.read_text(encoding="utf-8").splitlines()[0] + "\n", encoding="utf-8")
    err = _errors(data_dir)
    assert "no data rows" in err.errors[0].message


# ------------------------------------------------------------------ DA-02: rows


@pytest.mark.parametrize(
    ("old", "new", "column", "fragment"),
    [
        pytest.param(
            ",1;2,1,1,N,,5,", ",1;3,1,1,N,,5,", "available_city_tiers", "tier", id="tier-3"
        ),
        pytest.param(",1;2,1,1,N,,5,", ",x,1,1,N,,5,", "available_city_tiers", "tier", id="tier-x"),
        pytest.param(",1;2,1,1,N,,5,", ",,1,1,N,,5,", "available_city_tiers", "", id="tier-empty"),
        pytest.param(",12,1,5,1,Y,", ",12,1,5,1,Maybe,", "requires_gstin", "Y or N", id="bad-yn"),
        pytest.param(",3999,4499,", ",4999,4499,", None, "price_min", id="min-gt-max"),
        pytest.param(",3999,4499,", ",-1,4499,", "price_min_inr_per_month", "", id="neg-price"),
        pytest.param(",3999,4499,", ",39.99,4499,", "price_min_inr_per_month", "", id="float"),
        pytest.param("micro;prof\n", "micro;startup\n", "target_segments", "startup", id="segment"),
        pytest.param(
            "Connectivity,Test Fibre 1G", "Telepathy,Test Fibre 1G", "category", "", id="cat"
        ),
        pytest.param("per IP,", "per galaxy,", "unit", "", id="unit"),
        pytest.param("N,P01|P03,1,", "N,P01|P99,1,", None, "P99", id="unknown-dep"),
        pytest.param("N,P01|P03,1,", "N,P11,1,", None, "itself", id="self-dep"),
        pytest.param("N,P01|P03,1,", "N,P01;P03,1,", "requires_one_of", "", id="wrong-separator"),
        pytest.param("\nP11,", "\nP1,", "product_id", "", id="bad-id"),
        pytest.param("\nP11,", "\nP01,", None, "duplicate product_id P01", id="duplicate-id"),
        pytest.param(
            ",12,1;2,1,1,N,P01|P03,1,",
            ",0,1;2,1,1,N,P01|P03,1,",
            "min_contract_months",
            "",
            id="contract0",
        ),
    ],
)
def test_rejects_malformed_row(
    data_dir: Path, old: str, new: str, column: str | None, fragment: str
) -> None:
    replace_in_file(data_dir / CATALOGUE, old, new)
    err = _errors(data_dir)

    assert err.errors, "must report at least one error"
    assert all(e.line >= 2 for e in err.errors), "row errors carry a 1-based file line"
    if column is not None:
        assert any(e.column == column for e in err.errors), err.errors
    assert any(fragment in e.message for e in err.errors), err.errors


def test_rejects_row_with_wrong_field_count(data_dir: Path) -> None:
    replace_in_file(data_dir / CATALOGUE, ",1,prof;mfg\n", ",1,prof;mfg,extra\n")
    err = _errors(data_dir)
    assert any("expected 16 fields, got 17" in e.message for e in err.errors)


def test_reports_every_bad_row_not_just_the_first(data_dir: Path) -> None:
    path = data_dir / CATALOGUE
    replace_in_file(path, ",1;2,1,1,N,,5,", ",9,1,1,N,,5,")  # P01, line 2
    replace_in_file(path, "per camera,199,299", "per camera,abc,299")  # P23, line 5
    err = _errors(data_dir)

    assert {e.line for e in err.errors} == {2, 5}
    assert "2 error(s)" in str(err)
    assert "line 2" in str(err)
    assert "line 5" in str(err)


def test_error_message_names_file_line_and_column(data_dir: Path) -> None:
    replace_in_file(data_dir / CATALOGUE, ",1;2,1,1,N,,5,", ",1;3,1,1,N,,5,")
    text = str(_errors(data_dir))
    assert "product_catalogue.csv" in text
    assert "line 2" in text
    assert "available_city_tiers" in text
