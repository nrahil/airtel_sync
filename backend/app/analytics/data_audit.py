"""Reproducible data & constraint audit (IMPLEMENTATION_PLAN Appendix A).

Pure function of (dataset, today). Output is aggregate-only: counts, rates and
pseudonymous lead IDs — never names, GSTINs, phone numbers or addresses — so the
report is safe to print in CI logs and paste into slides.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Sequence
from datetime import date
from itertools import pairwise
from statistics import fmean

from pydantic import BaseModel, ConfigDict

from app.domain.catalogue import Catalogue
from app.domain.dataset import Dataset
from app.domain.identifiers import (
    gstin_state_code,
    is_udyam_format,
    is_valid_gstin,
    udyam_state_code,
)
from app.domain.india import GST_STATE_CODES, UDYAM_STATE_CODES
from app.domain.leads import STAGE_ORDER, Lead, LeadSegment, LeadSource, Outcome
from app.domain.reasons import ReasonCode
from app.domain.registries import GstinStatus

# ------------------------------------------------------------------ report model


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True)


class FileSummary(_Model):
    name: str
    rows: int
    sha256: str


class StageCount(_Model):
    stage: str
    reached: int


class StepRate(_Model):
    from_stage: str
    to_stage: str
    rate: float


class Funnel(_Model):
    reached: list[StageCount]
    steps: list[StepRate]
    weakest: StepRate
    overall_rate: float
    drop_reasons: dict[str, dict[str, int]]  # stage where dropped -> reason -> count


class GroupRow(_Model):
    key: str
    leads: int
    activations: int
    activation_rate: float
    churn_rate: float | None  # among activated
    avg_nps: float | None
    avg_days: float | None
    avg_bundle_inr: float | None
    acquisition_cost_inr: int
    cac_per_activation_inr: float | None


class RuleViolation(_Model):
    lead_id: str
    product_id: str
    rule: ReasonCode


class Anomalies(_Model):
    lead_gstin_status: dict[str, int]
    activated_with_gstin_status: dict[str, int]
    historic_violations: list[RuleViolation]
    violations_by_rule: dict[str, dict[str, int]]
    violating_bundles: int
    catalogue_gap_p11: int
    future_activated: list[str]
    future_kyc_complete: list[str]
    duplicated_business_names: int
    udyam_without_gstin: int


class PricingCheck(_Model):
    activations: int
    inside_range: int
    position_min: float | None
    position_max: float | None
    position_mean: float | None
    position_mean_by_tier: dict[int, float]
    setup_exact: int


class CleanChecks(_Model):
    duplicate_lead_ids: int
    invalid_gstin_checksums: int
    malformed_udyam_numbers: int
    unordered_stage_dates: int
    gst_state_mismatches: int
    udyam_state_mismatches: int
    lead_gstins_not_in_registry: int
    lead_udyams_not_in_registry: int


class AuditReport(_Model):
    today: date
    files: list[FileSummary]
    gstin_registry_status: dict[str, int]
    udyam_registry_types: dict[str, int]
    funnel: Funnel
    segments: list[GroupRow]
    sources: list[GroupRow]
    predictors: dict[str, list[GroupRow]]
    anomalies: Anomalies
    pricing: PricingCheck
    clean: CleanChecks


# ------------------------------------------------------------------ audit


def run_audit(dataset: Dataset, *, today: date) -> AuditReport:
    leads = list(dataset.leads)
    return AuditReport(
        today=today,
        files=[FileSummary(name=f.name, rows=f.rows, sha256=f.sha256) for f in dataset.manifest()],
        gstin_registry_status={
            s.value: n for s, n in dataset.gstin_registry.status_counts().items()
        },
        udyam_registry_types={t.value: n for t, n in dataset.udyam_registry.type_counts().items()},
        funnel=_funnel(leads),
        segments=_grouped(leads, lambda lead: lead.segment.value, [s.value for s in LeadSegment]),
        sources=_grouped(leads, lambda lead: lead.lead_source.value, [s.value for s in LeadSource]),
        predictors=_predictors(leads),
        anomalies=_anomalies(dataset, leads, today),
        pricing=_pricing(dataset.catalogue, leads),
        clean=_clean_checks(dataset, leads),
    )


def _activated(leads: Iterable[Lead]) -> list[Lead]:
    return [lead for lead in leads if lead.outcome is Outcome.ACTIVATED]


def _rate(num: int, den: int) -> float | None:
    return num / den if den else None


def _mean(values: Sequence[float]) -> float | None:
    return fmean(values) if values else None


# ------------------------------------------------------------------ funnel


def _funnel(leads: list[Lead]) -> Funnel:
    reached = [
        StageCount(stage=s.value, reached=sum(1 for lead in leads if lead.stage_reached.rank >= i))
        for i, s in enumerate(STAGE_ORDER)
    ]
    steps = [
        StepRate(
            from_stage=a.stage, to_stage=b.stage, rate=b.reached / a.reached if a.reached else 0.0
        )
        for a, b in pairwise(reached)
    ]
    drops: dict[str, Counter[str]] = defaultdict(Counter)
    for lead in leads:
        if lead.drop_reason is not None:
            drops[lead.stage_reached.value][lead.drop_reason.value] += 1
    drop_reasons = {
        s.value: dict(sorted(drops[s.value].items(), key=lambda kv: (-kv[1], kv[0])))
        for s in STAGE_ORDER
        if drops.get(s.value)
    }
    return Funnel(
        reached=reached,
        steps=steps,
        weakest=min(steps, key=lambda s: s.rate),
        overall_rate=reached[-1].reached / reached[0].reached if reached[0].reached else 0.0,
        drop_reasons=drop_reasons,
    )


# ------------------------------------------------------------------ groups


def _group_row(key: str, group: list[Lead]) -> GroupRow:
    act = _activated(group)
    cost = sum(lead.acquisition_cost_inr for lead in group)
    return GroupRow(
        key=key,
        leads=len(group),
        activations=len(act),
        activation_rate=len(act) / len(group) if group else 0.0,
        churn_rate=_rate(sum(1 for lead in act if lead.churned_within_6_months), len(act)),
        avg_nps=_mean([lead.nps_score for lead in act if lead.nps_score is not None]),
        avg_days=_mean(
            [
                lead.days_lead_to_activation
                for lead in act
                if lead.days_lead_to_activation is not None
            ]
        ),
        avg_bundle_inr=_mean(
            [
                lead.monthly_bundle_value_inr
                for lead in act
                if lead.monthly_bundle_value_inr is not None
            ]
        ),
        acquisition_cost_inr=cost,
        cac_per_activation_inr=cost / len(act) if act else None,
    )


def _grouped(leads: list[Lead], key: Callable[[Lead], str], order: Sequence[str]) -> list[GroupRow]:
    groups: dict[str, list[Lead]] = defaultdict(list)
    for lead in leads:
        groups[key(lead)].append(lead)
    keys = [k for k in order if k in groups] + sorted(k for k in groups if k not in order)
    return [_group_row(k, groups[k]) for k in keys]


def _yn(flag: bool) -> str:
    return "Y" if flag else "N"


def _predictors(leads: list[Lead]) -> dict[str, list[GroupRow]]:
    dims: dict[str, Callable[[Lead], str]] = {
        "has_gstin": lambda lead: _yn(lead.has_gstin),
        "has_udyam": lambda lead: _yn(lead.has_udyam),
        "current_internet": lambda lead: lead.current_internet.value,
        "annual_turnover_band": lambda lead: lead.annual_turnover_band.value,
        "city_tier": lambda lead: str(lead.city_tier),
        "preferred_language": lambda lead: lead.preferred_language.value,
    }
    return {name: _grouped(leads, fn, ()) for name, fn in dims.items()}


# ------------------------------------------------------------------ anomalies


def historic_rule_violations(catalogue: Catalogue, leads: Iterable[Lead]) -> list[RuleViolation]:
    """Catalogue rules each activated bundle broke (D16). Historic sales are never precedent."""
    out: list[RuleViolation] = []
    for lead in _activated(leads):
        in_bundle = {line.product_id for line in lead.bundle_products}
        for line in lead.bundle_products:
            p = catalogue[line.product_id]
            checks = (
                (lead.city_tier not in p.available_city_tiers, ReasonCode.TIER_NOT_SERVED),
                (lead.employee_count < p.min_employees, ReasonCode.BELOW_MIN_EMPLOYEES),
                (lead.num_sites < p.min_sites, ReasonCode.BELOW_MIN_SITES),
                (p.requires_gstin and not lead.has_gstin, ReasonCode.GSTIN_REQUIRED),
                (
                    bool(p.requires_one_of) and not in_bundle.intersection(p.requires_one_of),
                    ReasonCode.DEPENDENCY_MISSING,
                ),
            )
            out.extend(
                RuleViolation(lead_id=lead.lead_id, product_id=p.product_id, rule=rule)
                for failed, rule in checks
                if failed
            )
    return out


def _anomalies(dataset: Dataset, leads: list[Lead], today: date) -> Anomalies:
    registry = dataset.gstin_registry
    bad = (GstinStatus.CANCELLED, GstinStatus.SUSPENDED)

    def status_of(lead: Lead) -> GstinStatus | None:
        rec = registry.get(lead.gstin) if lead.gstin else None
        return rec.status if rec else None

    activated = _activated(leads)
    violations = historic_rule_violations(dataset.catalogue, leads)
    by_rule: dict[str, Counter[str]] = defaultdict(Counter)
    for v in violations:
        by_rule[v.rule.value][v.product_id] += 1

    by_id = {lead.lead_id: lead for lead in leads}
    leased_line = {"P05", "P06"}
    gap_p11 = sum(
        1
        for v in violations
        if v.product_id == "P11"
        and v.rule is ReasonCode.DEPENDENCY_MISSING
        and leased_line & {line.product_id for line in by_id[v.lead_id].bundle_products}
    )

    name_counts = Counter(lead.business_name for lead in leads)
    return Anomalies(
        lead_gstin_status={s.value: sum(1 for x in leads if status_of(x) is s) for s in bad},
        activated_with_gstin_status={
            s.value: sum(1 for x in activated if status_of(x) is s) for s in bad
        },
        historic_violations=violations,
        violations_by_rule={
            rule.value: dict(sorted(by_rule[rule.value].items()))
            for rule in ReasonCode
            if rule.value in by_rule
        },
        violating_bundles=len({v.lead_id for v in violations}),
        catalogue_gap_p11=gap_p11,
        future_activated=[
            x.lead_id for x in leads if x.activated_date is not None and x.activated_date > today
        ],
        future_kyc_complete=[
            x.lead_id
            for x in leads
            if x.kyc_complete_date is not None and x.kyc_complete_date > today
        ],
        duplicated_business_names=sum(1 for n in name_counts.values() if n > 1),
        udyam_without_gstin=sum(1 for x in leads if x.has_udyam and not x.has_gstin),
    )


# ------------------------------------------------------------------ pricing


def _pricing(catalogue: Catalogue, leads: list[Lead]) -> PricingCheck:
    activated = _activated(leads)
    positions: list[float] = []
    by_tier: dict[int, list[float]] = defaultdict(list)
    inside = setup_exact = 0
    for lead in activated:
        lo = sum(
            catalogue[ln.product_id].price_min_inr_per_month * ln.qty for ln in lead.bundle_products
        )
        hi = sum(
            catalogue[ln.product_id].price_max_inr_per_month * ln.qty for ln in lead.bundle_products
        )
        setup = sum(
            catalogue[ln.product_id].one_time_setup_inr * ln.qty for ln in lead.bundle_products
        )
        value = lead.monthly_bundle_value_inr
        if value is not None and lo <= value <= hi:
            inside += 1
            pos = (value - lo) / (hi - lo) if hi > lo else 0.0
            positions.append(pos)
            by_tier[lead.city_tier].append(pos)
        if lead.one_time_setup_inr == setup:
            setup_exact += 1
    return PricingCheck(
        activations=len(activated),
        inside_range=inside,
        position_min=min(positions, default=None),
        position_max=max(positions, default=None),
        position_mean=_mean(positions),
        position_mean_by_tier={t: fmean(v) for t, v in sorted(by_tier.items())},
        setup_exact=setup_exact,
    )


# ------------------------------------------------------------------ clean checks


def _clean_checks(dataset: Dataset, leads: list[Lead]) -> CleanChecks:
    gstin_reg = dataset.gstin_registry
    udyam_reg = dataset.udyam_registry

    # (identifier, claimed state) pairs from both the registries and the leads.
    gstins = [(r.gstin, r.state) for r in gstin_reg] + [
        (x.gstin, x.state) for x in leads if x.gstin
    ]
    udyams = [(r.udyam_number, r.state) for r in udyam_reg] + [
        (x.udyam_number, x.state) for x in leads if x.udyam_number
    ]

    def unordered(lead: Lead) -> bool:
        dates = [d for s in STAGE_ORDER if (d := lead.stage_date(s)) is not None]
        return any(a > b for a, b in pairwise(dates))

    lead_ids = Counter(x.lead_id for x in leads)
    return CleanChecks(
        duplicate_lead_ids=sum(1 for n in lead_ids.values() if n > 1),
        invalid_gstin_checksums=len({g for g, _ in gstins if not is_valid_gstin(g)}),
        malformed_udyam_numbers=len({u for u, _ in udyams if not is_udyam_format(u)}),
        unordered_stage_dates=sum(1 for x in leads if unordered(x)),
        gst_state_mismatches=sum(
            1 for g, st in gstins if GST_STATE_CODES.get(gstin_state_code(g)) != st
        ),
        udyam_state_mismatches=sum(
            1 for u, st in udyams if UDYAM_STATE_CODES.get(udyam_state_code(u)) != st
        ),
        lead_gstins_not_in_registry=sum(1 for x in leads if x.gstin and x.gstin not in gstin_reg),
        lead_udyams_not_in_registry=sum(
            1 for x in leads if x.udyam_number and x.udyam_number not in udyam_reg
        ),
    )


# ------------------------------------------------------------------ rendering


def _pct(rate: float | None, digits: int = 1) -> str:
    return "-" if rate is None else f"{rate * 100:.{digits}f}%"


def _num(value: float | None, digits: int = 0) -> str:
    return "-" if value is None else f"{value:,.{digits}f}"


def render_markdown(r: AuditReport) -> str:
    """Human-readable report mirroring Appendix A. Aggregates only."""
    out: list[str] = [f"# Data audit (today = {r.today.isoformat()})", "", "## Files", ""]
    out += ["| File | Rows | SHA-256 |", "|---|---|---|"]
    out += [f"| `{f.name}` | {f.rows:,} | `{f.sha256[:16]}…` |" for f in r.files]
    out += [
        "",
        "GSTIN registry: " + " · ".join(f"{k} {v:,}" for k, v in r.gstin_registry_status.items()),
        "Udyam registry: " + " · ".join(f"{k} {v:,}" for k, v in r.udyam_registry_types.items()),
        "",
        "## Funnel",
        "",
        " → ".join(f"{s.reached:,}" for s in r.funnel.reached)
        + f" ({_pct(r.funnel.overall_rate)})",
        f"Weakest step: {r.funnel.weakest.from_stage} → {r.funnel.weakest.to_stage} "
        f"({_pct(r.funnel.weakest.rate, 0)})",
        "",
    ]
    for stage, reasons in r.funnel.drop_reasons.items():
        out.append(f"- Dropped at *{stage}*: " + ", ".join(f"{k} {v}" for k, v in reasons.items()))

    out += [
        "",
        "## Segments",
        "",
        "| Segment | Leads | Activation | Churn (6m) | Avg NPS | Avg days | Avg bundle ₹/mo |",
    ]
    out.append("|---|---|---|---|---|---|---|")
    out += [
        f"| {s.key} | {s.leads:,} | {_pct(s.activation_rate)} | {_pct(s.churn_rate)} | "
        f"{_num(s.avg_nps, 2)} | {_num(s.avg_days, 1)} | {_num(s.avg_bundle_inr)} |"
        for s in r.segments
    ]
    out += ["", "## Sources", "", "| Source | Leads | Activation | CAC / activation | Churn |"]
    out.append("|---|---|---|---|---|")
    out += [
        f"| {s.key} | {s.leads:,} | {_pct(s.activation_rate)} | "
        f"₹{_num(s.cac_per_activation_inr)} | {_pct(s.churn_rate)} |"
        for s in r.sources
    ]
    out += ["", "## Activation by predictor", ""]
    for dim, groups in r.predictors.items():
        out.append(
            f"- **{dim}**: " + ", ".join(f"{g.key} {_pct(g.activation_rate)}" for g in groups)
        )

    a = r.anomalies
    out += ["", "## Planted anomalies", "", "| Anomaly | Count |", "|---|---|"]
    out.append(
        "| Lead GSTIN Cancelled / Suspended in registry | "
        + " / ".join(str(v) for v in a.lead_gstin_status.values())
        + " |"
    )
    out.append(
        "| …of which activated anyway | "
        + " / ".join(str(v) for v in a.activated_with_gstin_status.values())
        + " |"
    )
    for rule, per_product in a.violations_by_rule.items():
        detail = ", ".join(f"{p} x{n}" for p, n in per_product.items())
        out.append(f"| Historic bundle rule `{rule}` ({detail}) | {sum(per_product.values())} |")
    out += [
        f"| Distinct activated bundles violating ≥1 rule | {a.violating_bundles} |",
        f"| `CATALOGUE_GAP_P11` (P11 bought with leased line only) | {a.catalogue_gap_p11} |",
        f"| Activated / KYC dates after {r.today.isoformat()} | "
        f"{len(a.future_activated)} / {len(a.future_kyc_complete)} |",
        f"| Duplicated business names | {a.duplicated_business_names:,} |",
        f"| Udyam Y but GSTIN N | {a.udyam_without_gstin:,} |",
    ]
    p = r.pricing
    out += [
        f"| Historic value inside [Σmin, Σmax] | {p.inside_range}/{p.activations}; position "
        f"{_pct(p.position_min, 0)}-{_pct(p.position_max, 0)}, mean {_pct(p.position_mean, 0)}; "
        + ", ".join(f"tier {t} {_pct(v, 0)}" for t, v in p.position_mean_by_tier.items())
        + " |",
        f"| Setup fee = Σ setup x qty | {p.setup_exact}/{p.activations} |",
        "",
        "## Clean checks (all should be 0)",
        "",
    ]
    out += [f"- {k}: {v}" for k, v in r.clean.model_dump().items()]
    return "\n".join(out) + "\n"
