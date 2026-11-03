"""Historic SMB leads (Apr-Jun 2026): read-only, keyed by ``lead_id``.

The loader enforces *structural* consistency (types, enums, a stage date exists
iff the stage was reached, activation-only fields exist iff activated). Semantic
anomalies — dates after "today", rule-violating bundles, cancelled GSTINs that
were activated — are legitimate facts about the past and are *reported* by the
audit, not rejected (edge cases D15, D16).
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from datetime import date
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Annotated, Any, Final, NamedTuple, Self

from pydantic import BeforeValidator, Field, NonNegativeInt, PositiveInt, model_validator

from app.domain.catalogue import CITY_TIERS, Segment
from app.domain.csv_schema import (
    CsvRow,
    DataValidationError,
    IsoDate,
    OptionalIsoDate,
    OptionalYesNo,
    RowError,
    YesNo,
    load_table,
    split_semicolon,
)
from app.domain.identifiers import is_gstin_format, is_udyam_format
from app.domain.india import STATES_AND_UTS

_PRODUCT_ID_LEN: Final = 3


class LeadSource(StrEnum):
    FIELD_SALES = "Field sales"
    CHANNEL_PARTNER = "Channel partner"
    WEBSITE_FORM = "Website form"
    INBOUND_CALL = "Inbound call"
    REFERRAL = "Referral"
    WHATSAPP = "WhatsApp campaign"


class LeadSegment(StrEnum):
    MICRO = "Micro retail & services"
    PROF = "Professional offices"
    MULTI = "Small multi-site businesses"
    MFG = "Small manufacturers"

    @property
    def code(self) -> Segment:
        return Segment[self.name]


class Language(StrEnum):
    HINDI = "Hindi"
    ENGLISH = "English"
    MARATHI = "Marathi"
    KANNADA = "Kannada"
    GUJARATI = "Gujarati"
    TELUGU = "Telugu"
    TAMIL = "Tamil"
    BENGALI = "Bengali"
    PUNJABI = "Punjabi"
    MALAYALAM = "Malayalam"


class TurnoverBand(StrEnum):
    BELOW_40L = "< 40 lakh"
    L40_TO_1_5CR = "40 lakh - 1.5 cr"
    CR1_5_TO_5 = "1.5 - 5 cr"
    CR5_TO_25 = "5 - 25 cr"
    CR25_TO_100 = "25 - 100 cr"


class CurrentInternet(StrEnum):
    BUSINESS_BROADBAND = "Business broadband"
    HOME_BROADBAND = "Home broadband"
    MOBILE_HOTSPOT = "Mobile hotspot"
    LEASED_LINE = "Leased line"
    MIXED = "Mixed per site"
    NONE = "None"


class CurrentProvider(StrEnum):
    ACCEL = "Accel"
    ACCEL_MOBILE = "Accel (mobile only)"
    COMPETITOR_A = "Competitor A"
    COMPETITOR_B = "Competitor B"
    LOCAL_ISP = "Local ISP"
    NONE = "None"


class ContactRole(StrEnum):
    OWNER = "Owner"
    PARTNER = "Partner"
    MANAGER = "Manager"
    OFFICE_ADMIN = "Office admin"


class Stage(StrEnum):
    """Funnel stages, in order."""

    LEAD = "Lead"
    CONTACTED = "Contacted"
    NEEDS_ASSESSED = "Needs assessed"
    QUOTE_ACCEPTED = "Quote accepted"
    KYC_COMPLETE = "KYC complete"
    ACTIVATED = "Activated"

    @property
    def rank(self) -> int:
        return STAGE_ORDER.index(self)


STAGE_ORDER: Final = tuple(Stage)

# Date column recorded when each stage (after LEAD, which uses created_date) is reached.
STAGE_DATE_COLUMN: Final = MappingProxyType(
    {
        Stage.CONTACTED: "contacted_date",
        Stage.NEEDS_ASSESSED: "needs_assessed_date",
        Stage.QUOTE_ACCEPTED: "quote_accepted_date",
        Stage.KYC_COMPLETE: "kyc_complete_date",
        Stage.ACTIVATED: "activated_date",
    }
)


class Outcome(StrEnum):
    ACTIVATED = "Activated"
    DROPPED = "Dropped"


class DropReason(StrEnum):
    # dropped as Lead
    UNREACHABLE = "Unreachable after 3 attempts"
    NOT_INTERESTED = "Not interested on first call"
    WRONG_NUMBER = "Wrong or invalid number"
    # dropped after contact
    NO_TIME_FOR_VISIT = "Owner had no time for a visit"
    LOCKED_IN = "Locked in with current provider"
    VISIT_NOT_SCHEDULED = "Visit not scheduled by sales"
    ONLY_MOBILE = "Only wanted a mobile plan"
    # dropped after needs assessment
    PRICE_TOO_HIGH = "Price too high"
    COMPETITOR_CHEAPER = "Competitor offered cheaper plan"
    QUOTE_TOO_COMPLEX = "Quote too complex to understand"
    QUOTE_TOO_SLOW = "Quote took too long to arrive"
    DECIDED_TO_WAIT = "Decided to wait"
    # dropped after quote accepted
    MISSING_DOCUMENT = "Missing GST or Udyam document"
    PAPERWORK_TOO_LONG = "Found paperwork too long"
    SIGNATORY_UNAVAILABLE = "Authorised signatory not available"
    DOCUMENT_MISMATCH = "Document details did not match"
    # dropped after KYC
    INSTALL_DELAYED = "Installation delayed beyond 21 days"
    CANCELLED_BEFORE_INSTALL = "Customer cancelled before install"
    SITE_NOT_FEASIBLE = "Site not feasible for fibre"
    FIRST_PAYMENT_FAILED = "First payment failed"


class BundleLine(NamedTuple):
    product_id: str
    qty: int


def _parse_bundle(value: Any) -> Any:
    """``P01x1;P23x2`` → ``(BundleLine('P01', 1), BundleLine('P23', 2))``."""
    if value is None:
        return ()
    if not isinstance(value, str):
        return value
    lines: list[BundleLine] = []
    for item in split_semicolon(value):
        pid, sep, qty = item.partition("x")
        if (
            not sep
            or len(pid) != _PRODUCT_ID_LEN
            or not pid.startswith("P")
            or not pid[1:].isdigit()
            or not qty.isdigit()
        ):
            raise ValueError(f"bad bundle item {item!r} (expected e.g. P01x2)")
        if int(qty) < 1:
            raise ValueError(f"bundle item {item!r} must have quantity >= 1")
        if any(line.product_id == pid for line in lines):
            raise ValueError(f"duplicate product {pid} in bundle")
        lines.append(BundleLine(pid, int(qty)))
    return tuple(lines)


def _parse_product_ids(value: Any) -> Any:
    ids = split_semicolon(value)
    if isinstance(ids, tuple):
        for pid in ids:
            if len(pid) != _PRODUCT_ID_LEN or not pid.startswith("P") or not pid[1:].isdigit():
                raise ValueError(f"bad product id {pid!r}")
    return ids


def _city_tier(value: Any) -> Any:
    if isinstance(value, str) and value.isdigit() and int(value) in CITY_TIERS:
        return int(value)
    raise ValueError(f"invalid city tier {value!r} (allowed: 1, 2)")


def _state(value: Any) -> Any:
    if value not in STATES_AND_UTS:
        raise ValueError(f"unknown Indian state/UT {value!r}")
    return value


NonEmpty = Annotated[str, Field(min_length=1)]


class Lead(CsvRow):
    lead_id: Annotated[str, Field(pattern=r"^L[0-9]{5}$")]
    created_date: IsoDate
    lead_source: LeadSource
    business_name: NonEmpty
    business_type: NonEmpty
    segment: LeadSegment
    city: NonEmpty
    city_tier: Annotated[int, BeforeValidator(_city_tier)]
    state: Annotated[str, BeforeValidator(_state)]
    preferred_language: Language
    employee_count: PositiveInt
    num_sites: PositiveInt
    annual_turnover_band: TurnoverBand
    has_gstin: YesNo
    gstin: str | None
    has_udyam: YesNo
    udyam_number: str | None
    current_internet: CurrentInternet
    current_provider: CurrentProvider
    monthly_telecom_spend_inr: NonNegativeInt
    products_of_interest: Annotated[tuple[str, ...], BeforeValidator(_parse_product_ids)]
    contact_name: Annotated[NonEmpty, Field(repr=False)]  # personal data
    contact_role: ContactRole
    stage_reached: Stage
    outcome: Outcome
    drop_reason: DropReason | None
    contacted_date: OptionalIsoDate
    needs_assessed_date: OptionalIsoDate
    quote_accepted_date: OptionalIsoDate
    kyc_complete_date: OptionalIsoDate
    activated_date: OptionalIsoDate
    days_lead_to_activation: NonNegativeInt | None
    customer_follow_up_calls: NonNegativeInt
    bundle_products: Annotated[tuple[BundleLine, ...], BeforeValidator(_parse_bundle)]
    monthly_bundle_value_inr: PositiveInt | None
    one_time_setup_inr: NonNegativeInt | None
    contract_months: PositiveInt | None
    churned_within_6_months: OptionalYesNo
    nps_score: Annotated[int, Field(ge=0, le=10)] | None
    acquisition_cost_inr: NonNegativeInt

    # ---------------------------------------------------------------- consistency

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        problems: list[str] = []
        problems += self._identifier_problems()
        problems += self._stage_problems()
        problems += self._activation_problems()
        if problems:
            raise ValueError("; ".join(problems))
        return self

    def _identifier_problems(self) -> list[str]:
        out: list[str] = []
        if self.has_gstin and not self.gstin:
            out.append("has_gstin=Y requires gstin")
        if not self.has_gstin and self.gstin:
            out.append("has_gstin=N but gstin is set")
        if self.gstin and not is_gstin_format(self.gstin):
            out.append("gstin is not a well-formed GSTIN")
        if self.has_udyam and not self.udyam_number:
            out.append("has_udyam=Y requires udyam_number")
        if not self.has_udyam and self.udyam_number:
            out.append("has_udyam=N but udyam_number is set")
        if self.udyam_number and not is_udyam_format(self.udyam_number):
            out.append("udyam_number is not a well-formed Udyam number")
        return out

    def _stage_problems(self) -> list[str]:
        out: list[str] = []
        reached = self.stage_reached.rank
        for stage, column in STAGE_DATE_COLUMN.items():
            present = getattr(self, column) is not None
            if stage.rank <= reached and not present:
                out.append(f"{column} is required when stage_reached is {self.stage_reached}")
            elif stage.rank > reached and present:
                out.append(f"{column} must be empty when stage_reached is {self.stage_reached}")
        activated = self.stage_reached is Stage.ACTIVATED
        if activated != (self.outcome is Outcome.ACTIVATED):
            out.append(f"outcome {self.outcome} contradicts stage_reached {self.stage_reached}")
        if self.outcome is Outcome.DROPPED and self.drop_reason is None:
            out.append("drop_reason is required when outcome is Dropped")
        if self.outcome is Outcome.ACTIVATED and self.drop_reason is not None:
            out.append("drop_reason must be empty when outcome is Activated")
        return out

    def _activation_problems(self) -> list[str]:
        fields = {
            "days_lead_to_activation": self.days_lead_to_activation,
            "bundle_products": self.bundle_products or None,
            "monthly_bundle_value_inr": self.monthly_bundle_value_inr,
            "one_time_setup_inr": self.one_time_setup_inr,
            "contract_months": self.contract_months,
            "churned_within_6_months": self.churned_within_6_months,
            "nps_score": self.nps_score,
        }
        activated = self.outcome is Outcome.ACTIVATED
        out: list[str] = []
        for name, value in fields.items():
            if activated and value is None:
                out.append(f"{name} is required for an activated lead")
            elif not activated and value is not None:
                out.append(f"{name} must be empty unless activated")
        return out

    # ---------------------------------------------------------------- helpers

    def stage_date(self, stage: Stage) -> date | None:
        if stage is Stage.LEAD:
            return self.created_date
        value: date | None = getattr(self, STAGE_DATE_COLUMN[stage])
        return value


class Leads:
    """Read-only, ordered collection keyed by ``lead_id``. No name index, by design (D14)."""

    __slots__ = ("_by_id", "content_hash")

    def __init__(self, by_id: Mapping[str, Lead], content_hash: str) -> None:
        self._by_id: Mapping[str, Lead] = MappingProxyType(dict(by_id))
        self.content_hash = content_hash

    def get(self, lead_id: str) -> Lead | None:
        return self._by_id.get(lead_id)

    def __getitem__(self, lead_id: str) -> Lead:
        return self._by_id[lead_id]

    def __contains__(self, lead_id: object) -> bool:
        return lead_id in self._by_id

    def __iter__(self) -> Iterator[Lead]:
        return iter(self._by_id.values())

    def __len__(self) -> int:
        return len(self._by_id)


def load_leads(path: Path) -> Leads:
    table = load_table(path, Lead)
    by_id: dict[str, Lead] = {}
    first_line: dict[str, int] = {}
    errors: list[RowError] = []
    for lead, line in zip(table.rows, table.lines, strict=True):
        if lead.lead_id in by_id:
            errors.append(
                RowError(
                    line,
                    "lead_id",
                    f"duplicate lead_id {lead.lead_id} (first on line {first_line[lead.lead_id]})",
                )
            )
            continue
        by_id[lead.lead_id] = lead
        first_line[lead.lead_id] = line
    if errors:
        raise DataValidationError(table.file, errors)
    return Leads(by_id, table.sha256)
