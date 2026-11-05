"""Lead propensity score: who to call next (ARCHITECTURE §10.2, PS-01/PS-02).

An L2-regularised logistic regression on **pre-contact features only** — what we
know the moment a lead arrives. Everything recorded later in the funnel (stage,
outcome, drop reason, stage dates, follow-up calls, acquisition cost, what was bought,
churn, NPS) is leakage and is excluded by construction: features are read through
:class:`LeadFeatures`, which has no field for any of them.

Fitted with Newton-IRLS in numpy — deterministic, a few milliseconds on 5,000 rows,
and no extra dependency. The model is saved as plain JSON (never pickle): the
vocabulary, standardisation, coefficients and holdout metrics.

Explanations: a lead's logit is ``intercept + Σ coef_j · z_j`` with ``z`` standardised
against the training mean, so ``coef_j · z_j`` is that feature's push away from an
average lead. One-hot levels are summed back to their feature for display.
"""

from __future__ import annotations

import json
import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import date
from enum import StrEnum
from pathlib import Path
from typing import Any, Final

import numpy as np
import numpy.typing as npt

from app.domain.catalogue import Segment
from app.domain.leads import (
    CurrentInternet,
    CurrentProvider,
    Language,
    Lead,
    LeadSegment,
    LeadSource,
    Outcome,
    TurnoverBand,
)
from app.domain.registries import GstinRegistry

MODEL_VERSION: Final = "propensity-v1"

FloatArray = npt.NDArray[np.float64]


class GstinState(StrEnum):
    """GSTIN status as a lookup would report it before anyone calls the lead."""

    ACTIVE = "ACTIVE"
    CANCELLED = "CANCELLED"
    SUSPENDED = "SUSPENDED"
    NOT_FOUND = "NOT_FOUND"
    NONE = "NONE"  # no GSTIN given


# Lead columns that are only known after contact (or are outcomes). Never features.
LEAKAGE_COLUMNS: Final = frozenset(
    {
        "stage_reached",
        "outcome",
        "drop_reason",
        "contacted_date",
        "needs_assessed_date",
        "quote_accepted_date",
        "kyc_complete_date",
        "activated_date",
        "days_lead_to_activation",
        "customer_follow_up_calls",
        "acquisition_cost_inr",
        "bundle_products",
        "monthly_bundle_value_inr",
        "one_time_setup_inr",
        "contract_months",
        "churned_within_6_months",
        "nps_score",
    }
)
# Identifiers, personal data and free text: not modelled (no signal we want to learn,
# and keeping names/numbers out of the model is data minimisation).
NOT_MODELLED_COLUMNS: Final = frozenset(
    {
        "lead_id",
        "created_date",  # used only for the time-based split
        "business_name",
        "business_type",
        "city",
        "state",
        "udyam_number",
        "contact_name",
        "contact_role",
        "monthly_telecom_spend_inr",
    }
)
# Lead columns each feature is derived from (PS-01 checks these against the leakage set).
FEATURE_SOURCES: Final[Mapping[str, tuple[str, ...]]] = {
    "segment": ("segment",),
    "city_tier": ("city_tier",),
    "lead_source": ("lead_source",),
    "gstin_status": ("has_gstin", "gstin"),  # the number only to look up its status
    "has_udyam": ("has_udyam",),
    "current_internet": ("current_internet",),
    "current_provider": ("current_provider",),
    "turnover_band": ("annual_turnover_band",),
    "employees": ("employee_count",),
    "sites": ("num_sites",),
    "products_of_interest": ("products_of_interest",),
    "preferred_language": ("preferred_language",),
}

CATEGORICAL: Final[Mapping[str, tuple[str, ...]]] = {
    "segment": tuple(s.code.value for s in LeadSegment),
    "city_tier": ("1", "2"),
    "lead_source": tuple(s.value for s in LeadSource),
    "gstin_status": tuple(s.value for s in GstinState),
    "current_internet": tuple(s.value for s in CurrentInternet),
    "current_provider": tuple(s.value for s in CurrentProvider),
    "turnover_band": tuple(s.value for s in TurnoverBand),
    "preferred_language": tuple(s.value for s in Language),
}
NUMERIC: Final = ("has_udyam", "employees", "sites", "products_of_interest")
FEATURES: Final = (*CATEGORICAL, *NUMERIC)
if set(FEATURES) != set(FEATURE_SOURCES):  # keep the two lists in step
    raise RuntimeError("propensity feature list out of sync")


@dataclass(frozen=True, slots=True)
class LeadFeatures:
    """Everything the model may see. ``None`` = unknown (e.g. an advisor session)."""

    segment: str | None = None
    city_tier: str | None = None
    lead_source: str | None = None
    gstin_status: str | None = None
    has_udyam: bool | None = None
    current_internet: str | None = None
    current_provider: str | None = None
    turnover_band: str | None = None
    employees: int | None = None
    sites: int | None = None
    products_of_interest: int | None = None
    preferred_language: str | None = None


def gstin_state(lead: Lead, registry: GstinRegistry | None) -> GstinState:
    if not lead.gstin:
        return GstinState.NONE
    record = registry.get(lead.gstin) if registry is not None else None
    if record is None:
        return GstinState.NOT_FOUND
    return GstinState[record.status.name]


def lead_features(lead: Lead, registry: GstinRegistry | None) -> LeadFeatures:
    return LeadFeatures(
        segment=lead.segment.code.value,
        city_tier=str(lead.city_tier),
        lead_source=lead.lead_source.value,
        gstin_status=gstin_state(lead, registry).value,
        has_udyam=lead.has_udyam,
        current_internet=lead.current_internet.value,
        current_provider=lead.current_provider.value,
        turnover_band=lead.annual_turnover_band.value,
        employees=lead.employee_count,
        sites=lead.num_sites,
        products_of_interest=len(lead.products_of_interest),
        preferred_language=lead.preferred_language.value,
    )


def _numeric(name: str, value: bool | int) -> float:
    if name == "has_udyam":
        return 1.0 if value else 0.0
    if name in ("employees", "sites"):
        return math.log1p(float(value))
    return float(value)


def column_names() -> list[str]:
    cols = [f"{name}={level}" for name, levels in CATEGORICAL.items() for level in levels]
    return [*cols, *NUMERIC]


def _owner(column: str) -> str:
    return column.split("=", 1)[0]


def raw_vector(f: LeadFeatures) -> tuple[FloatArray, npt.NDArray[np.bool_]]:
    """One-hot + numeric values, and a mask of columns whose feature is unknown."""
    values: list[float] = []
    unknown: list[bool] = []
    for name, levels in CATEGORICAL.items():
        v = getattr(f, name)
        if v is not None and v not in levels:
            raise ValueError(f"unknown {name} level {v!r}")
        values.extend(1.0 if v == level else 0.0 for level in levels)
        unknown.extend([v is None] * len(levels))
    for name in NUMERIC:
        v = getattr(f, name)
        values.append(0.0 if v is None else _numeric(name, v))
        unknown.append(v is None)
    return np.asarray(values, dtype=np.float64), np.asarray(unknown, dtype=np.bool_)


# ------------------------------------------------------------------ metrics


def auc(scores: Sequence[float] | FloatArray, labels: Sequence[int] | FloatArray) -> float:
    """ROC AUC via the rank-sum (Mann-Whitney) formula; ties get average ranks."""
    s = np.asarray(scores, dtype=np.float64)
    y = np.asarray(labels, dtype=np.float64)
    pos = int(y.sum())
    neg = len(y) - pos
    if pos == 0 or neg == 0:
        raise ValueError("AUC needs both classes")
    order = np.argsort(s, kind="mergesort")
    ranks = np.empty(len(s), dtype=np.float64)
    sorted_s = s[order]
    i = 0
    while i < len(s):
        j = i
        while j + 1 < len(s) and sorted_s[j + 1] == sorted_s[i]:
            j += 1
        ranks[order[i : j + 1]] = (i + j) / 2.0 + 1.0
        i = j + 1
    return float((ranks[y == 1].sum() - pos * (pos + 1) / 2.0) / (pos * neg))


def lift_at(scores: FloatArray, labels: FloatArray, fraction: float = 0.1) -> float:
    """Conversion rate in the top ``fraction`` by score ÷ the overall rate."""
    n = max(1, math.ceil(len(scores) * fraction))
    top = np.argsort(-scores, kind="mergesort")[:n]
    overall = float(labels.mean())
    return float(labels[top].mean() / overall) if overall else 0.0


# ------------------------------------------------------------------ model


def _sigmoid(z: FloatArray) -> FloatArray:
    out: FloatArray = 1.0 / (1.0 + np.exp(-np.clip(z, -35.0, 35.0)))
    return out


def fit_logistic(
    x: FloatArray, y: FloatArray, *, l2: float = 1.0, max_iter: int = 50, tol: float = 1e-8
) -> tuple[float, FloatArray]:
    """Newton-IRLS for L2-penalised logistic regression (intercept unpenalised)."""
    n, d = x.shape
    design = np.hstack([np.ones((n, 1)), x])
    w = np.zeros(d + 1)
    penalty = np.full(d + 1, l2)
    penalty[0] = 0.0
    for _ in range(max_iter):
        p = _sigmoid(design @ w)
        grad = design.T @ (p - y) + penalty * w
        weighted = design * (p * (1 - p))[:, None]
        hess = weighted.T @ design + np.diag(penalty) + 1e-9 * np.eye(d + 1)
        step = np.linalg.solve(hess, grad)
        w -= step
        if float(np.max(np.abs(step))) < tol:
            break
    return float(w[0]), w[1:]


@dataclass(frozen=True, slots=True)
class Factor:
    feature: str
    value: str
    effect: float  # logit contribution vs an average lead (+ raises the score)


@dataclass(frozen=True, slots=True)
class Score:
    probability: float
    factors: tuple[Factor, ...]


@dataclass(frozen=True)
class Metrics:
    train_rows: int
    test_rows: int
    train_rate: float
    test_rate: float
    auc: float
    baseline_random_auc: float
    baseline_gstin_auc: float
    lift_top_decile: float


@dataclass(frozen=True)
class PropensityModel:
    columns: tuple[str, ...]
    mean: tuple[float, ...]
    scale: tuple[float, ...]
    intercept: float
    coefficients: tuple[float, ...]
    metrics: Metrics | None = None
    data_hash: str | None = None
    split: dict[str, str] = field(default_factory=dict)
    version: str = MODEL_VERSION

    def __post_init__(self) -> None:
        n = len(self.columns)
        if list(self.columns) != column_names():
            raise ValueError("model columns do not match this code's feature set")
        if not (len(self.mean) == len(self.scale) == len(self.coefficients) == n):
            raise ValueError("model arrays have inconsistent lengths")

    # ---------------------------------------------------------------- scoring

    def _z(self, f: LeadFeatures) -> FloatArray:
        raw, unknown = raw_vector(f)
        z: FloatArray = (raw - np.asarray(self.mean)) / np.asarray(self.scale)
        z[unknown] = 0.0  # unknown ⇒ treat as an average lead for that feature
        return z

    def score(self, f: LeadFeatures, *, top: int = 3) -> Score:
        contributions = np.asarray(self.coefficients) * self._z(f)
        logit = self.intercept + float(contributions.sum())
        per_feature: dict[str, float] = {}
        for column, c in zip(self.columns, contributions, strict=True):
            per_feature[_owner(column)] = per_feature.get(_owner(column), 0.0) + float(c)
        ranked = sorted(
            (
                Factor(name, _display(name, getattr(f, name)), round(effect, 4))
                for name, effect in per_feature.items()
                if getattr(f, name) is not None and abs(effect) >= 1e-6
            ),
            key=lambda fa: (-abs(fa.effect), fa.feature),
        )
        return Score(float(_sigmoid(np.asarray([logit]))[0]), tuple(ranked[:top]))

    def coefficient_table(self) -> list[dict[str, Any]]:
        """Per one-hot column, in original units' direction, largest first."""
        rows = [
            {"column": c, "coefficient": round(w, 4)}
            for c, w in zip(self.columns, self.coefficients, strict=True)
        ]
        return sorted(rows, key=lambda r: (-abs(r["coefficient"]), r["column"]))

    # ---------------------------------------------------------------- persistence

    def to_json(self) -> dict[str, Any]:
        out = asdict(self)
        out["coefficient_table"] = self.coefficient_table()
        return out

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> PropensityModel:
        if data.get("version") != MODEL_VERSION:
            raise ValueError("unsupported model version")
        metrics = data.get("metrics")
        return cls(
            columns=tuple(data["columns"]),
            mean=tuple(float(v) for v in data["mean"]),
            scale=tuple(float(v) for v in data["scale"]),
            intercept=float(data["intercept"]),
            coefficients=tuple(float(v) for v in data["coefficients"]),
            metrics=Metrics(**metrics) if metrics else None,
            data_hash=data.get("data_hash"),
            split=dict(data.get("split") or {}),
        )

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_json(), indent=2, sort_keys=True), encoding="utf-8")

    @classmethod
    def load(cls, path: Path) -> PropensityModel:
        return cls.from_json(json.loads(path.read_text(encoding="utf-8")))


def _display(name: str, value: Any) -> str:
    if isinstance(value, bool):
        return "Y" if value else "N"
    return str(value)


# ------------------------------------------------------------------ training


@dataclass(frozen=True, slots=True)
class Example:
    features: LeadFeatures
    converted: bool
    created: date


TRAIN_MONTHS: Final = (4, 5)  # April-May 2026
TEST_MONTHS: Final = (6,)  # June 2026


def examples_from(leads: Iterable[Lead], registry: GstinRegistry | None) -> list[Example]:
    return [
        Example(lead_features(lead, registry), lead.outcome is Outcome.ACTIVATED, lead.created_date)
        for lead in leads
    ]


def _matrix(examples: Sequence[Example]) -> tuple[FloatArray, FloatArray]:
    rows = [raw_vector(e.features)[0] for e in examples]
    x = np.vstack(rows) if rows else np.zeros((0, len(column_names())))
    y = np.asarray([1.0 if e.converted else 0.0 for e in examples])
    return x, y


def train(
    examples: Sequence[Example], *, l2: float = 1.0, data_hash: str | None = None
) -> PropensityModel:
    """Fit on Apr-May leads, evaluate on June (time-based split, no shuffling)."""
    train_set = [e for e in examples if e.created.month in TRAIN_MONTHS]
    test_set = [e for e in examples if e.created.month in TEST_MONTHS]
    if not train_set or not test_set:
        raise ValueError("need leads in both the April-May and June windows")
    x, y = _matrix(train_set)
    if y.min() == y.max():
        raise ValueError("training window has a single class")
    mean = x.mean(axis=0)
    scale = x.std(axis=0)
    scale[scale == 0] = 1.0  # constant column (e.g. a level absent from training)
    intercept, coef = fit_logistic((x - mean) / scale, y, l2=l2)
    model = PropensityModel(
        columns=tuple(column_names()),
        mean=tuple(float(v) for v in mean),
        scale=tuple(float(v) for v in scale),
        intercept=intercept,
        coefficients=tuple(float(v) for v in coef),
        data_hash=data_hash,
        split={"train": "2026-04..2026-05", "test": "2026-06"},
    )
    return PropensityModel(**{**_fields(model), "metrics": evaluate(model, train_set, test_set)})


def _fields(model: PropensityModel) -> dict[str, Any]:
    return {k: getattr(model, k) for k in PropensityModel.__dataclass_fields__}


def evaluate(
    model: PropensityModel, train_set: Sequence[Example], test_set: Sequence[Example]
) -> Metrics:
    _, y_test = _matrix(test_set)
    scores = np.asarray([model.score(e.features, top=0).probability for e in test_set])
    gstin_scores = np.asarray(
        [1.0 if e.features.gstin_status != GstinState.NONE.value else 0.0 for e in test_set]
    )
    return Metrics(
        train_rows=len(train_set),
        test_rows=len(test_set),
        train_rate=round(sum(e.converted for e in train_set) / len(train_set), 4),
        test_rate=round(float(y_test.mean()), 4),
        auc=round(auc(scores, y_test), 4),
        baseline_random_auc=0.5,
        baseline_gstin_auc=round(auc(gstin_scores, y_test), 4),
        lift_top_decile=round(lift_at(scores, y_test, 0.1), 3),
    )


# ------------------------------------------------------------------ advisor sessions


_SETUP_TO_INTERNET: Final = {
    "business_broadband": CurrentInternet.BUSINESS_BROADBAND.value,
    "home_broadband": CurrentInternet.HOME_BROADBAND.value,
    "mobile_hotspot": CurrentInternet.MOBILE_HOTSPOT.value,
    "leased_line": CurrentInternet.LEASED_LINE.value,
    "mixed": CurrentInternet.MIXED.value,
    "none": CurrentInternet.NONE.value,
}


def session_features(slots: Mapping[str, Any]) -> LeadFeatures:
    """What an anonymous advisor session tells us. Unasked slots stay unknown.

    A *claimed* GSTIN is unverified, so it maps to "has a GSTIN" only through the
    ACTIVE level when the owner said yes — the same signal a lead form gives.
    """
    segment = slots.get("segment")
    tier = slots.get("city_tier")
    has_gstin = slots.get("has_gstin")
    internet = slots.get("current_internet")
    return LeadFeatures(
        segment=segment if segment in {s.value for s in Segment} else None,
        city_tier=str(tier) if tier in (1, 2) else None,
        lead_source=LeadSource.WEBSITE_FORM.value,  # the PWA is our web channel
        gstin_status=(
            None
            if has_gstin is None
            else (GstinState.ACTIVE.value if has_gstin else GstinState.NONE.value)
        ),
        current_internet=_SETUP_TO_INTERNET.get(internet) if isinstance(internet, str) else None,
        employees=slots.get("employee_count"),
        sites=slots.get("num_sites"),
    )
