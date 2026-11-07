"""Success metrics for the business case (key question 5): today's baselines from
``smb_leads.csv``, the targets we set, and a scenario that shows where they come from.

Everything is computed from the provided leads, so the deck's numbers are reproducible:

    python -m scripts.baselines            # Markdown
    python -m scripts.baselines --json

The scenario is deliberately simple and stated in the output: at each funnel step,
the drops the product addresses directly (by named drop reason) are recovered at a
given share (default half); every other drop stays. Aggregates only, no personal data.
"""

from __future__ import annotations

import itertools
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass
from statistics import mean
from typing import Any, Final

from app.domain.leads import STAGE_ORDER, DropReason, Lead, Outcome, Stage

D = DropReason

# Drops the product answers directly, per step (the stage the lead reached, then stopped).
ADDRESSED: Final[Mapping[Stage, tuple[tuple[DropReason, str], ...]]] = {
    Stage.CONTACTED: (
        (D.NO_TIME_FOR_VISIT, "needs found on the phone by the advisor, no visit"),
        (D.VISIT_NOT_SCHEDULED, "no visit to schedule"),
    ),
    Stage.NEEDS_ASSESSED: (
        (D.PRICE_TOO_HIGH, "starter plan; price change shown before each toggle"),
        (D.QUOTE_TOO_COMPLEX, "one monthly price incl. GST, a plain 'why' on every line"),
        (D.QUOTE_TOO_SLOW, "quote in the same sitting"),
    ),
    Stage.QUOTE_ACCEPTED: (
        (D.MISSING_DOCUMENT, "online GSTIN / Udyam check, DigiLocker"),
        (D.PAPERWORK_TOO_LONG, "fastest path first: one photo"),
        (D.SIGNATORY_UNAVAILABLE, "e-sign on the phone"),
        (D.DOCUMENT_MISMATCH, "exact fix per document; details editable"),
    ),
    Stage.KYC_COMPLETE: (
        (D.INSTALL_DELAYED, "bookable install visit; day-21 alert and call back"),
        (D.FIRST_PAYMENT_FAILED, "failed payment retryable, nothing charged"),
    ),
}

RECOVERED_SHARE: Final = 0.5


@dataclass(frozen=True, slots=True)
class Metric:
    key: str
    name: str
    baseline: float
    target: float
    unit: str  # "%", "days", "INR", "calls"
    lower_is_better: bool
    why_target: str


@dataclass(frozen=True, slots=True)
class Step:
    from_stage: str
    to_stage: str
    reached: int
    passed: int
    rate: float
    addressed_drops: int
    scenario_rate: float


@dataclass(frozen=True, slots=True)
class Baselines:
    leads: int
    activated: int
    conversion: float
    needs_to_quote: float
    days_to_activate: float
    days_by_step: dict[str, float]
    status_chases: float
    cac_inr: float
    cac_by_source: dict[str, float]
    churn_6m: float
    median_bundle_inr: float
    steps: tuple[Step, ...]
    scenario_conversion: float
    recovered_share: float
    metrics: tuple[Metric, ...]

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


def _days(leads: list[Lead], start: str, end: str) -> float:
    gaps: list[int] = [(getattr(x, end) - getattr(x, start)).days for x in leads]
    return round(mean(gaps), 1)


def compute(leads_in: Iterable[Lead], *, recovered_share: float = RECOVERED_SHARE) -> Baselines:
    leads = list(leads_in)
    if not leads:
        raise ValueError("no leads")
    live = [x for x in leads if x.outcome is Outcome.ACTIVATED]
    if not live:
        raise ValueError("no activated leads")

    steps: list[Step] = []
    scenario = 1.0
    for here, nxt in itertools.pairwise(STAGE_ORDER):
        reached = [x for x in leads if x.stage_reached.rank >= here.rank]
        passed = sum(x.stage_reached.rank >= nxt.rank for x in reached)
        reasons = {r for r, _ in ADDRESSED.get(here, ())}
        addressed = sum(x.stage_reached is here and x.drop_reason in reasons for x in reached)
        rate = passed / len(reached)
        new_rate = (passed + recovered_share * addressed) / len(reached)
        scenario *= new_rate
        steps.append(
            Step(
                here.value,
                nxt.value,
                len(reached),
                passed,
                round(rate, 4),
                addressed,
                round(new_rate, 4),
            )
        )

    by_source: dict[str, list[Lead]] = {}
    for x in leads:
        by_source.setdefault(x.lead_source.value, []).append(x)
    cac_by_source = {
        source: float(round(sum(x.acquisition_cost_inr for x in group) / n))
        for source, group in by_source.items()
        if (n := sum(x.outcome is Outcome.ACTIVATED for x in group))
    }

    conversion = len(live) / len(leads)
    needs = next(s for s in steps if s.from_stage == Stage.NEEDS_ASSESSED.value)
    days = round(mean(x.days_lead_to_activation or 0 for x in live), 1)
    chases = round(mean(x.customer_follow_up_calls for x in live), 1)
    cac = round(sum(x.acquisition_cost_inr for x in leads) / len(live))
    churned = [x for x in live if x.churned_within_6_months is not None]
    churn = sum(bool(x.churned_within_6_months) for x in churned) / len(churned)
    bundles = sorted(x.monthly_bundle_value_inr or 0 for x in live)
    days_by_step = {
        "lead → contacted": _days(live, "created_date", "contacted_date"),
        "contacted → needs assessed": _days(live, "contacted_date", "needs_assessed_date"),
        "needs assessed → quote accepted": _days(
            live, "needs_assessed_date", "quote_accepted_date"
        ),
        "quote accepted → KYC complete": _days(live, "quote_accepted_date", "kyc_complete_date"),
        "KYC complete → activated": _days(live, "kyc_complete_date", "activated_date"),
    }
    install_days = days_by_step["KYC complete → activated"]
    digital_cac = min(cac_by_source.get(s, cac) for s in ("Website form", "Inbound call"))

    metrics = (
        Metric(
            "conversion",
            "Lead → activation",
            round(conversion, 4),
            0.15,
            "%",
            False,
            f"Recovering half of the drops the app addresses gives {scenario:.1%}; "
            "15% leaves room for error.",
        ),
        Metric(
            "needs_to_quote",
            "Needs assessed → quote accepted (weakest step)",
            needs.rate,
            0.60,
            "%",
            False,
            f"{needs.addressed_drops} of the drops here were price, complexity or "
            f"delay; recovering half gives {needs.scenario_rate:.0%}.",
        ),
        Metric(
            "days",
            "Days from lead to activation",
            days,
            7.0,
            "days",
            True,
            f"{days - install_days:.1f} of the {days} days are waits before install "
            f"that the app does in one sitting; install itself takes {install_days}.",
        ),
        Metric(
            "cac",
            "Acquisition cost per activated business",
            cac,
            7000.0,
            "INR",
            True,
            f"Digital channels already cost ₹{digital_cac:,.0f} per activation; "
            "field visits only where they add value.",
        ),
        Metric(
            "chases",
            "Status chases per customer",
            chases,
            0.5,
            "calls",
            True,
            "Tracker plus a WhatsApp/SMS update at every step; a call only when something is late.",
        ),
    )
    return Baselines(
        leads=len(leads),
        activated=len(live),
        conversion=round(conversion, 4),
        needs_to_quote=needs.rate,
        days_to_activate=days,
        days_by_step=days_by_step,
        status_chases=chases,
        cac_inr=cac,
        cac_by_source=cac_by_source,
        churn_6m=round(churn, 4),
        median_bundle_inr=float(bundles[len(bundles) // 2]),
        steps=tuple(steps),
        scenario_conversion=round(scenario, 4),
        recovered_share=recovered_share,
        metrics=metrics,
    )


def _fmt(value: float, unit: str) -> str:
    if unit == "%":
        return f"{value:.1%}"
    if unit == "INR":
        return f"₹{value:,.0f}"
    if unit == "calls":
        return f"{value:g}"
    return f"{value:g} {unit}"


def render_markdown(b: Baselines) -> str:
    out = [
        "# Success metrics: baselines and targets",
        "",
        f"From `smb_leads.csv`: {b.leads:,} leads, {b.activated} activated.",
        "",
        "| Metric | Today | Target | Why this target |",
        "|---|---|---|---|",
    ]
    out += [
        f"| {m.name} | {_fmt(m.baseline, m.unit)} | {'≤' if m.lower_is_better else '≥'} "
        f"{_fmt(m.target, m.unit)} | {m.why_target} |"
        for m in b.metrics
    ]
    out += [
        "",
        f"Guardrail: 6-month churn {b.churn_6m:.1%} today; must not rise "
        "(a Starter plan brings in more price-sensitive micro shops, whose churn is 22.7%). "
        f"Median bundle ₹{b.median_bundle_inr:,.0f}/month.",
        "",
        "## Where the days go (activated leads, average)",
        "",
        *[f"- {k}: {v} days" for k, v in b.days_by_step.items()],
        "",
        f"## Funnel scenario (recover {b.recovered_share:.0%} of addressed drops)",
        "",
        "| Step | Today | Addressed drops | Scenario |",
        "|---|---|---|---|",
        *[
            f"| {s.from_stage} → {s.to_stage} | {s.rate:.0%} | {s.addressed_drops} | "
            f"{s.scenario_rate:.0%} |"
            for s in b.steps
        ],
        "",
        f"Lead → activation: {b.conversion:.1%} today → {b.scenario_conversion:.1%} "
        "in the scenario.",
        "",
        "## Acquisition cost per activation, by source",
        "",
        *[f"- {k}: ₹{v:,.0f}" for k, v in sorted(b.cac_by_source.items(), key=lambda kv: kv[1])],
        "",
    ]
    return "\n".join(out)
