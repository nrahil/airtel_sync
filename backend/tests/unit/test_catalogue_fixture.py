"""The committed catalogue fixture must stay byte-identical to the provided catalogue."""

from __future__ import annotations

from app.domain.catalogue import load_catalogue
from tests.conftest import CATALOGUE_FIXTURE, REAL_DATA_DIR, requires_real_dataset


@requires_real_dataset
def test_catalogue_fixture_matches_provided_dataset() -> None:
    assert CATALOGUE_FIXTURE.read_bytes() == (REAL_DATA_DIR / "product_catalogue.csv").read_bytes()


def test_catalogue_fixture_is_the_25_product_catalogue() -> None:
    cat = load_catalogue(CATALOGUE_FIXTURE)
    assert cat.ids == tuple(f"P{i:02d}" for i in range(1, 26))
