"""KYC accuracy on documents the pipeline has never seen.

Renders fresh applicants from the registries in the specimen layout, plants one
known problem per applicant (or none), optionally damages the images like a phone
photo, then runs the real pipeline (Tesseract → cross-checks → verdict) and compares
the decision with the expected one.

    python -m scripts.kyc_stress --per-case 3 --fonts dejavu arial --photo
    python -m scripts.kyc_stress --cases clean bill_unit_changed --per-case 5 --keep out/

The headline number is **false passes**: a planted problem that came out as PASS.
It must be 0. "Clean pass rate" is how often a document with nothing wrong passes.
"""

from __future__ import annotations

import argparse
import io
import os
import random
import string
import sys
from collections import Counter
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from datetime import date, timedelta
from pathlib import Path
from typing import Final

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from app.domain.identifiers import is_valid_gstin
from app.domain.registries import (
    Constitution,
    GstinRecord,
    GstinRegistry,
    GstinStatus,
    MajorActivity,
    UdyamRecord,
    UdyamRegistry,
    load_gstin_registry,
    load_udyam_registry,
)
from app.kyc.crosscheck import evaluate
from app.kyc.extract import extract_document
from app.kyc.ocr import TesseractEngine
from app.kyc.types import ApplicationContext, Decision, DocSlot, Extraction, KycReason
from app.kyc.validators import split_proprietor
from app.kyc.verdict import decide
from app.lookups.registry import Registries

BACKEND: Final = Path(__file__).resolve().parents[1]
DATA: Final = BACKEND.parent / "mock_data"
TODAY: Final = date(2026, 7, 15)  # the dataset's "today" (TODAY_OVERRIDE)
WINDOWS_FONTS: Final = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
WATERMARK: Final = "SYNTHETIC SPECIMEN - NOT A VALID DOCUMENT"
FOOTER: Final = (
    "Accel YTL Hackathon - Track A test data. Fictional business, person and identifiers."
)
WRAP: Final = 46  # the specimens hard-wrap long values at this many characters
PEOPLE: Final = (
    "Arjun Mehta", "Kavya Nair", "Rohit Verma", "Sneha Iyer", "Imran Qureshi",
    "Divya Reddy", "Manoj Patil", "Farah Khan", "Suresh Gowda", "Priya Bhatt",
)  # fmt: skip


# ------------------------------------------------------------------ fonts


@dataclass(frozen=True)
class Fonts:
    name: str
    regular: Path
    bold: Path
    mono: Path

    def get(self, kind: str, size: int) -> ImageFont.FreeTypeFont:
        path = {"regular": self.regular, "bold": self.bold, "mono": self.mono}[kind]
        return ImageFont.truetype(str(path), size)


def _dejavu_dir() -> Path | None:
    env = os.environ.get("KYC_DEJAVU_DIR")
    candidates = [Path(env)] if env else []
    for base in (
        Path(sys.prefix),
        Path(sys.base_prefix),
        *Path.home().glob("AppData/Local/Programs/Python/Python3*"),
    ):
        candidates.append(
            base / "Lib" / "site-packages" / "matplotlib" / "mpl-data" / "fonts" / "ttf"
        )
    candidates.append(Path("/usr/share/fonts/truetype/dejavu"))
    return next((c for c in candidates if (c / "DejaVuSans.ttf").is_file()), None)


def font_sets() -> dict[str, Fonts]:
    sets: dict[str, Fonts] = {}
    dv = _dejavu_dir()
    if dv is not None:  # the specimens' own typeface
        sets["dejavu"] = Fonts(
            "dejavu", dv / "DejaVuSans.ttf", dv / "DejaVuSans-Bold.ttf", dv / "DejaVuSansMono.ttf"
        )
    w = WINDOWS_FONTS
    for name, reg, bold, mono in (
        ("arial", "arial.ttf", "arialbd.ttf", "consola.ttf"),
        ("verdana", "verdana.ttf", "verdanab.ttf", "cour.ttf"),
        ("times", "times.ttf", "timesbd.ttf", "lucon.ttf"),
    ):
        if all((w / f).is_file() for f in (reg, bold, mono)):
            sets[name] = Fonts(name, w / reg, w / bold, w / mono)
    return sets


# ------------------------------------------------------------------ applicant


@dataclass
class Applicant:
    """Everything printed on the four documents, plus what is on record."""

    gstin: str
    legal_name: str
    trade_name: str
    constitution: str
    gst_address: str
    gst_date: str
    udyam_number: str | None
    enterprise_name: str
    major_activity: str
    city: str
    state: str
    udyam_date: str
    signatory: str
    designation: str
    for_business: str
    valid_till: str
    billed_to: str
    supply_address: str
    bill_date: str
    contact_name: str
    gstin_on_record: str
    udyam_on_record: str | None
    slots: dict[DocSlot, DocSlot] = field(default_factory=lambda: {s: s for s in DocSlot})
    skip: frozenset[DocSlot] = frozenset()
    damage: dict[DocSlot, str] = field(default_factory=dict)


def _pairs(gst: GstinRegistry, udyam: UdyamRegistry) -> list[tuple[GstinRecord, UdyamRecord]]:
    by_name: dict[str, UdyamRecord] = {}
    for u in udyam:
        by_name.setdefault(u.enterprise_name.lower(), u)
    return [(g, by_name[g.trade_name.lower()]) for g in gst if g.trade_name.lower() in by_name]


def base_applicant(g: GstinRecord, u: UdyamRecord, rng: random.Random) -> Applicant:
    prop = split_proprietor(g.legal_name)
    person = (
        prop[0] if prop and g.constitution is Constitution.PROPRIETORSHIP else rng.choice(PEOPLE)
    )
    return Applicant(
        gstin=g.gstin,
        legal_name=g.legal_name,
        trade_name=g.trade_name,
        constitution=g.constitution.value,
        gst_address=g.principal_place_of_business,
        gst_date=g.registration_date.isoformat(),
        udyam_number=u.udyam_number,
        enterprise_name=u.enterprise_name,
        major_activity=u.major_activity.value,
        city=u.city,
        state=u.state,
        udyam_date=u.date_of_registration.isoformat(),
        signatory=person,
        designation="Owner" if prop else rng.choice(("Partner", "Director", "Manager")),
        for_business=g.trade_name,
        valid_till=(TODAY + timedelta(days=rng.randrange(200, 1500))).isoformat(),
        billed_to=g.trade_name,
        supply_address=g.principal_place_of_business,
        bill_date=(TODAY - timedelta(days=rng.randrange(5, 80))).isoformat(),
        contact_name=person,
        gstin_on_record=g.gstin,
        udyam_on_record=u.udyam_number,
    )


# ------------------------------------------------------------------ planted problems


@dataclass(frozen=True)
class Case:
    name: str
    expected: frozenset[Decision]
    reason: str | None  # a reason code that must appear (None: any)
    plant: Callable[[Applicant, random.Random, Registries], Applicant | None]
    status: GstinStatus = GstinStatus.ACTIVE


def _alter_digit(value: str, rng: random.Random, positions: range) -> str:
    chars = list(value)
    digits = [i for i in positions if chars[i].isdigit()]
    i = rng.choice(digits)
    chars[i] = rng.choice([d for d in string.digits if d != chars[i]])
    return "".join(chars)


def _bad_gstin(a: Applicant, rng: random.Random, _: Registries) -> Applicant:
    while True:
        bad = _alter_digit(a.gstin, rng, range(2, 12))
        if not is_valid_gstin(bad):
            return replace(a, gstin=bad)


def _confusable_gstin(a: Applicant, rng: random.Random, _: Registries) -> Applicant | None:
    # Print a letter where a digit belongs (0→O etc.): must be repaired and disclosed.
    to_letter = {"0": "O", "1": "I", "5": "S", "8": "B"}
    spots = [i for i in (0, 1, 7, 8, 9, 10) if a.gstin[i] in to_letter]
    if not spots:
        return None
    i = rng.choice(spots)
    return replace(a, gstin=a.gstin[:i] + to_letter[a.gstin[i]] + a.gstin[i + 1 :])


def _other_gstin(a: Applicant, rng: random.Random, reg: Registries) -> Applicant:
    other = rng.choice(
        [g for g in reg.gstin if g.gstin != a.gstin and g.status is GstinStatus.ACTIVE]
    )
    return replace(a, gstin=other.gstin)


def _swap_word(name: str, rng: random.Random) -> str:
    words = name.split()
    fillers = ("Shree", "Royal", "Prime", "Galaxy", "Lotus", "Metro", "Sunrise")
    i = rng.randrange(len(words))
    words[i] = rng.choice([f for f in fillers if f.lower() != words[i].lower()])
    return " ".join(words)


def _legal_name(a: Applicant, rng: random.Random, _: Registries) -> Applicant:
    prop = split_proprietor(a.legal_name)
    if prop:
        return replace(a, legal_name=f"{prop[0]} (Prop. {_swap_word(prop[1], rng)})")
    return replace(a, legal_name=_swap_word(a.legal_name, rng))


def _constitution(a: Applicant, rng: random.Random, _: Registries) -> Applicant:
    other = [c.value for c in Constitution if c.value != a.constitution]
    return replace(a, constitution=rng.choice(other))


def _new_unit(address: str, rng: random.Random) -> str:
    head, _, rest = address.partition(",")
    unit = head.rsplit(" ", 1)
    return f"{unit[0]} {int(unit[1]) + rng.randrange(1, 40)},{rest}"


def _new_pincode(address: str, rng: random.Random) -> str:
    head, _, pin = address.rpartition(" ")
    return f"{head} {(int(pin) + rng.randrange(1, 9000)) % 900000 + 100000}"


def _other_address(a: Applicant, rng: random.Random, reg: Registries) -> str:
    return rng.choice([g.principal_place_of_business for g in reg.gstin if g.state != a.state])


def _other_udyam(a: Applicant, rng: random.Random, reg: Registries) -> Applicant:
    other = rng.choice([u for u in reg.udyam if u.udyam_number != a.udyam_number])
    return replace(a, udyam_number=other.udyam_number)


def _unknown_udyam(a: Applicant, rng: random.Random, reg: Registries) -> Applicant:
    if a.udyam_number is None:
        raise ValueError("applicant has no Udyam number")
    while True:
        cand = a.udyam_number[:-7] + "".join(rng.choices(string.digits, k=7))
        if cand not in reg.udyam:
            return replace(a, udyam_number=cand)


def _other_city(a: Applicant, rng: random.Random, reg: Registries) -> Applicant:
    other = rng.choice([u for u in reg.udyam if u.city != a.city and u.state != a.state])
    return replace(a, city=other.city, state=other.state)


def _other_person(a: Applicant, rng: random.Random, _: Registries) -> Applicant:
    return replace(a, signatory=rng.choice([p for p in PEOPLE if p != a.contact_name]))


def _wrong_type(a: Applicant, _: random.Random, __: Registries) -> Applicant:
    slots = dict(a.slots)
    slots[DocSlot.GST_CERTIFICATE] = DocSlot.ADDRESS_PROOF  # a bill uploaded as the GST certificate
    return replace(a, slots=slots)


def _same(a: Applicant, _: random.Random, __: Registries) -> Applicant:
    return a


def _later_gst_date(a: Applicant, rng: random.Random, _: Registries) -> Applicant:
    later = date.fromisoformat(a.gst_date) + timedelta(days=rng.randrange(30, 700))
    return replace(a, gst_date=later.isoformat())


def _d(days: int) -> str:
    return (TODAY + timedelta(days=days)).isoformat()


def _other_udyam_date(a: Applicant, rng: random.Random, _: Registries) -> Applicant:
    moved = date.fromisoformat(a.udyam_date) + timedelta(days=rng.randrange(30, 700))
    return replace(a, udyam_date=moved.isoformat())


def _other_activity(a: Applicant, rng: random.Random, _: Registries) -> Applicant:
    return replace(
        a,
        major_activity=rng.choice([m.value for m in MajorActivity if m.value != a.major_activity]),
    )


P, R, F = Decision.PASS, Decision.REVIEW, Decision.FAIL
CASES: Final[tuple[Case, ...]] = (
    Case("clean", frozenset({P}), None, lambda a, r, g: a),
    Case("gstin_confusable", frozenset({P}), None, _confusable_gstin),
    Case("gstin_checksum", frozenset({R}), "GSTIN_CHECKSUM_INVALID", _bad_gstin),
    Case("gstin_other_business", frozenset({R}), "LEGAL_NAME_MISMATCH", _other_gstin),
    Case("gstin_cancelled", frozenset({F}), "GSTIN_CANCELLED", _same, GstinStatus.CANCELLED),
    Case("gstin_suspended", frozenset({F}), "GSTIN_SUSPENDED", _same, GstinStatus.SUSPENDED),
    Case("legal_name_changed", frozenset({R}), "LEGAL_NAME_MISMATCH", _legal_name),
    Case("trade_name_changed", frozenset({R}), "TRADE_NAME_MISMATCH",
         lambda a, r, g: replace(a, trade_name=_swap_word(a.trade_name, r))),
    Case("constitution_changed", frozenset({R}), "CONSTITUTION_MISMATCH", _constitution),
    Case("gst_address_changed", frozenset({R}), "GST_ADDRESS_MISMATCH",
         lambda a, r, g: replace(a, gst_address=_new_unit(a.gst_address, r))),
    Case("gst_date_changed", frozenset({R}), "GST_DATE_MISMATCH",
         _later_gst_date),
    Case("udyam_blank", frozenset({R}), "UDYAM_NUMBER_MISSING",
         lambda a, r, g: replace(a, udyam_number=None)),
    Case("udyam_unknown", frozenset({R}), "UDYAM_NOT_FOUND", _unknown_udyam),
    Case("udyam_other_business", frozenset({R}), "UDYAM_NOT_ON_RECORD", _other_udyam),
    Case("udyam_name_changed", frozenset({R}), "UDYAM_NAME_MISMATCH",
         lambda a, r, g: replace(a, enterprise_name=_swap_word(a.enterprise_name, r))),
    Case("udyam_location_changed", frozenset({R}), "UDYAM_LOCATION_MISMATCH", _other_city),
    Case("udyam_date_changed", frozenset({R}), "UDYAM_DATE_MISMATCH", _other_udyam_date),
    Case("udyam_activity_changed", frozenset({R}), "UDYAM_ACTIVITY_MISMATCH", _other_activity),
    Case("udyam_missing", frozenset({R}), "DOCUMENT_MISSING:UDYAM",
         lambda a, r, g: replace(a, skip=frozenset({DocSlot.UDYAM_CERTIFICATE}))),
    Case("signatory_other_person", frozenset({R}), "SIGNATORY_NOT_AUTHORISED", _other_person),
    Case("signatory_expired", frozenset({R}), "SIGNATORY_ID_EXPIRED",
         lambda a, r, g: replace(a, valid_till=_d(-r.randrange(1, 400)))),
    Case("signatory_other_business", frozenset({R}), "BUSINESS_NAME_INCONSISTENT",
         lambda a, r, g: replace(a, for_business=_swap_word(a.for_business, r))),
    Case("bill_stale", frozenset({R}), "ADDRESS_PROOF_STALE",
         lambda a, r, g: replace(a, bill_date=_d(-r.randrange(91, 600)))),
    Case("bill_future", frozenset({R}), "ADDRESS_PROOF_FUTURE_DATED",
         lambda a, r, g: replace(a, bill_date=_d(r.randrange(2, 60)))),
    Case("bill_unit_changed", frozenset({R}), "ADDRESS_MISMATCH",
         lambda a, r, g: replace(a, supply_address=_new_unit(a.supply_address, r))),
    Case("bill_pincode_changed", frozenset({R}), "ADDRESS_MISMATCH",
         lambda a, r, g: replace(a, supply_address=_new_pincode(a.supply_address, r))),
    Case("bill_other_address", frozenset({R}), "ADDRESS_MISMATCH",
         lambda a, r, g: replace(a, supply_address=_other_address(a, r, g))),
    Case("bill_other_business", frozenset({R}), "BUSINESS_NAME_INCONSISTENT",
         lambda a, r, g: replace(a, billed_to=_swap_word(a.billed_to, r))),
    Case("wrong_document", frozenset({R}), "WRONG_DOCUMENT_TYPE", _wrong_type),
    Case("gst_blurred", frozenset({R}), "IMAGE_TOO_BLURRY",
         lambda a, r, g: replace(a, damage={DocSlot.GST_CERTIFICATE: "blur"})),
    Case("gst_tiny", frozenset({R}), "IMAGE_RESOLUTION_LOW",
         lambda a, r, g: replace(a, damage={DocSlot.GST_CERTIFICATE: "tiny"})),
)  # fmt: skip


# ------------------------------------------------------------------ rendering


SUBTITLE: Final = "Format for hackathon OCR testing only"
BILL_SUBTITLE: Final = "Fictional utility - address proof for OCR testing"


def _stable(a: Applicant, low: int, high: int) -> int:
    """A number that is the same for an applicant on every run (consumer no., ID no.)."""
    return random.Random(a.gstin).randrange(low, high)  # noqa: S311 - not a secret


def _wrap(value: str) -> list[str]:
    return [value[i : i + WRAP] for i in range(0, len(value), WRAP)] or [""]


def _watermarks(
    img: Image.Image, fonts: Fonts, size: int, spots: list[tuple[int, int]]
) -> Image.Image:
    font = fonts.get("bold", size)
    box = ImageDraw.Draw(img).textbbox((0, 0), WATERMARK, font=font)
    tile = Image.new("RGBA", (int(box[2]) + 10, int(box[3]) + 10), (0, 0, 0, 0))
    ImageDraw.Draw(tile).text((5, 0), WATERMARK, font=font, fill=(214, 120, 120, 120))
    tile = tile.rotate(22, expand=True, resample=Image.Resampling.BICUBIC)
    base = img.convert("RGBA")
    for x, y in spots:
        base.paste(tile, (x, y), tile)
    return base.convert("RGB")


def _page(title: str, subtitle: str, rows: list[tuple[str, str, str]], fonts: Fonts) -> Image.Image:
    img = Image.new("RGB", (1240, 1754), (253, 253, 249))
    d = ImageDraw.Draw(img)
    d.rectangle((40, 40, 1200, 1714), outline=(60, 60, 60), width=3)
    d.rectangle((40, 40, 1200, 190), fill=(232, 235, 244))
    d.text((80, 72), title, font=fonts.get("bold", 38), fill=(27, 33, 64))
    d.text((80, 132), subtitle, font=fonts.get("regular", 20), fill=(70, 70, 70))
    y = 252
    for label, value, kind in rows:
        size = 22 if kind == "wrap" else 24
        parts = _wrap(value) if kind == "wrap" else [value]
        labels = label.split("|")
        for i, part in enumerate(parts):
            if i < len(labels) and labels[i]:
                d.text(
                    (90 + (18 if i else 0), y),
                    labels[i],
                    font=fonts.get("bold", 22),
                    fill=(40, 40, 40),
                )
            d.text(
                (520, y + 2),
                part,
                font=fonts.get("mono" if kind == "mono" else "regular", size),
                fill=(15, 15, 15),
            )
            y += 70
    d.text((80, 1660), FOOTER, font=fonts.get("regular", 18), fill=(140, 30, 30))
    return _watermarks(img, fonts, 44, [(-60, 50), (40, 430), (220, 820), (400, 1250)])


def render(slot: DocSlot, a: Applicant, fonts: Fonts) -> Image.Image:
    if slot is DocSlot.GST_CERTIFICATE:
        return _page("Specimen: GST Registration Certificate", SUBTITLE, [
            ("GSTIN", a.gstin, "mono"),
            ("Legal name", a.legal_name, ""),
            ("Trade name", a.trade_name, ""),
            ("Constitution", a.constitution, ""),
            ("Address of principal|place of business", a.gst_address, "wrap"),
            ("Date of liability", a.gst_date, ""),
            ("Type of registration", "Regular", ""),
        ], fonts)  # fmt: skip
    if slot is DocSlot.UDYAM_CERTIFICATE:
        return _page("Specimen: Udyam Registration Certificate", SUBTITLE, [
            ("Udyam reg. number", a.udyam_number or "", "mono"),
            ("Enterprise name", a.enterprise_name, ""),
            ("Major activity", a.major_activity, ""),
            ("City", a.city, ""),
            ("State", a.state, ""),
            ("Date of registration", a.udyam_date, ""),
        ], fonts)  # fmt: skip
    if slot is DocSlot.ADDRESS_PROOF:
        return _page("Specimen: Electricity Bill", BILL_SUBTITLE, [
            ("Consumer no.", str(_stable(a, 10**9, 10**10)), ""),
            ("Billed to", a.billed_to, ""),
            ("Supply address", a.supply_address, "wrap"),
            ("Bill date", a.bill_date, ""),
            ("Units consumed", "812", ""),
            ("Amount due (INR)", "14,210", ""),
        ], fonts)  # fmt: skip
    img = Image.new("RGB", (1013, 638), (243, 246, 251))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((10, 10, 1002, 627), radius=28, outline=(38, 52, 92), width=4)
    d.text(
        (40, 35), "Specimen: Authorised Signatory ID", font=fonts.get("bold", 34), fill=(24, 42, 92)
    )
    d.rectangle((40, 120, 300, 440), fill=(210, 214, 222))
    d.ellipse((110, 160, 230, 280), fill=(168, 174, 186))
    d.rectangle((90, 300, 250, 440), fill=(168, 174, 186))
    y = 130
    rows = (
        ("Name", a.signatory),
        ("Designation", a.designation),
        ("For business", a.for_business),
        ("ID no.", f"SYN-ID-{_stable(a, 100000, 999999)}"),
        ("Valid till", a.valid_till),
    )
    for label, value in rows:
        d.text((340, y), label, font=fonts.get("regular", 20), fill=(90, 90, 90))
        d.text((340, y + 24), value, font=fonts.get("bold", 26), fill=(10, 10, 10))
        y += 70
    d.text((40, 522), WATERMARK, font=fonts.get("bold", 32), fill=(220, 120, 120))
    return img


def photo(img: Image.Image, rng: random.Random) -> Image.Image:
    """Damage like a phone photo of a printout: tilt, rescale, soft focus, light, noise, JPEG."""
    bg = (int(rng.uniform(200, 245)),) * 3
    out = img.rotate(
        rng.uniform(-3.5, 3.5), expand=True, fillcolor=bg, resample=Image.Resampling.BICUBIC
    )
    scale = rng.uniform(0.85, 1.25)  # a phone camera out-resolves the specimen scans
    out = out.resize((int(out.width * scale), int(out.height * scale)), Image.Resampling.LANCZOS)
    out = out.filter(ImageFilter.GaussianBlur(rng.uniform(0.2, 0.9)))
    gain, bias = rng.uniform(0.85, 1.05), rng.uniform(-15, 10)
    out = out.point(lambda v: max(0, min(255, int(v * gain + bias))))
    noise = Image.effect_noise(out.size, rng.uniform(4, 12)).convert("RGB")
    out = Image.blend(out, noise, 0.06)
    buf = io.BytesIO()
    out.save(buf, format="JPEG", quality=rng.randrange(55, 85))
    buf.seek(0)
    return Image.open(buf).convert("RGB")


def damage(img: Image.Image, kind: str) -> Image.Image:
    if kind == "blur":
        return img.filter(ImageFilter.GaussianBlur(5))
    return img.resize((img.width // 4, img.height // 4), Image.Resampling.LANCZOS)  # tiny


# ------------------------------------------------------------------ run


@dataclass(frozen=True)
class Outcome:
    case: Case
    font: str
    photo: bool
    decision: Decision
    reasons: frozenset[str]
    unreadable: tuple[str, ...]
    label: str

    @property
    def ok(self) -> bool:
        return self.decision in self.case.expected and (
            self.case.reason is None or self.case.reason in self.reasons
        )

    @property
    def false_pass(self) -> bool:
        return self.decision is Decision.PASS and Decision.PASS not in self.case.expected


def run_one(
    case: Case, a: Applicant, fonts: Fonts, use_photo: bool, seed: int,
    engine: TesseractEngine, reg: Registries, keep: Path | None, label: str,
) -> Outcome:  # fmt: skip
    rng = random.Random(seed)  # noqa: S311 - reproducible test data, not secrets
    docs: dict[DocSlot, Extraction] = {}
    for slot in DocSlot:
        if slot in a.skip:
            continue
        img = render(a.slots[slot], a, fonts)
        if use_photo:
            img = photo(img, rng)
        if slot in a.damage:
            img = damage(img, a.damage[slot])
        if keep is not None:
            img.save(keep / f"{label}_{slot.short.lower()}.png")
        docs[slot] = extract_document(img, slot, engine)
    ctx = ApplicationContext(
        contact_name=a.contact_name,
        gstin_on_record=a.gstin_on_record,
        udyam_on_record=a.udyam_on_record,
    )
    verdict = decide(evaluate(docs, ctx, reg, TODAY))
    unreadable = tuple(
        f"{c.check_id}:{c.params.get('field', '')}"
        for c in verdict.checks
        if c.reason is KycReason.FIELD_UNREADABLE
    )
    return Outcome(
        case, fonts.name, use_photo, verdict.decision, verdict.reasons, unreadable, label
    )


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument(
        "--per-case", type=int, default=2, help="applicants per case, per font, per mode"
    )
    p.add_argument("--cases", nargs="*", help="only these cases (default: all)")
    p.add_argument("--fonts", nargs="*", help="font sets (default: all available)")
    p.add_argument("--photo", action="store_true", help="also run phone-photo damaged copies")
    p.add_argument("--seed", type=int, default=7)
    p.add_argument("--workers", type=int, default=max(2, (os.cpu_count() or 4) - 1))
    p.add_argument("--keep", type=Path, help="save the rendered images here")
    p.add_argument("--tesseract", default=os.environ.get("TESSERACT_CMD") or _tesseract())
    args = p.parse_args()

    gst, udyam = (
        load_gstin_registry(DATA / "mock_gstin_registry.csv"),
        load_udyam_registry(DATA / "mock_udyam_registry.csv"),
    )
    reg = Registries(gstin=gst, udyam=udyam)
    pairs = _pairs(gst, udyam)
    available = font_sets()
    fonts = [available[f] for f in (args.fonts or available)]
    cases = [c for c in CASES if not args.cases or c.name in args.cases]
    if args.keep:
        args.keep.mkdir(parents=True, exist_ok=True)
    engine = TesseractEngine(args.tesseract)
    rng = random.Random(args.seed)  # noqa: S311 - reproducible test data, not secrets

    jobs: list[tuple[Case, Applicant, Fonts, bool, int, str]] = []
    for case in cases:
        pool = [(g, u) for g, u in pairs if g.status is case.status]
        for f in fonts:
            for use_photo in (False, True) if args.photo else (False,):
                made = 0
                while made < args.per_case:
                    g, u = rng.choice(pool)
                    a = case.plant(base_applicant(g, u, rng), rng, reg)
                    if a is None:
                        continue
                    made += 1
                    label = f"{case.name}_{f.name}{'_photo' if use_photo else ''}_{made}"
                    jobs.append((case, a, f, use_photo, rng.randrange(10**9), label))

    print(
        f"{len(jobs)} applicants | fonts {[f.name for f in fonts]} | photo={args.photo}", flush=True
    )
    with ThreadPoolExecutor(args.workers) as ex:
        outcomes = list(
            ex.map(
                lambda j: run_one(j[0], j[1], j[2], j[3], j[4], engine, reg, args.keep, j[5]), jobs
            )
        )

    by_case: dict[str, list[Outcome]] = {}
    for o in outcomes:
        by_case.setdefault(o.case.name, []).append(o)
    print(f"\n{'case':28} {'ok':>7}  decisions")
    for name, items in by_case.items():
        ok = sum(o.ok for o in items)
        dist = Counter(o.decision.value for o in items)
        print(f"{name:28} {ok:3}/{len(items):<3}  {dict(dist)}")
    print(f"\n{'font':10} {'mode':8} {'expected outcome':>17}  {'false passes':>12}")
    for f in fonts:
        for use_photo in (False, True) if args.photo else (False,):
            group = [o for o in outcomes if o.font == f.name and o.photo is use_photo]
            ok = sum(o.ok for o in group)
            fp = sum(o.false_pass for o in group)
            mode = "photo" if use_photo else "render"
            share = 100 * ok / len(group)
            print(f"{f.name:10} {mode:8} {ok:8}/{len(group):<4} {share:5.1f}%  {fp:12}")
    bad = [o for o in outcomes if not o.ok]
    for o in bad:
        extra = f" unreadable={list(o.unreadable)}" if o.unreadable else ""
        print(f"  x {o.label}: {o.decision.value} {sorted(o.reasons)}{extra}")
    clean = [o for o in outcomes if Decision.PASS in o.case.expected]
    false_pass = [o for o in outcomes if o.false_pass]
    print(
        f"\nexpected outcome: {len(outcomes) - len(bad)}/{len(outcomes)}"
        f" | false passes: {len(false_pass)}"
        f" | clean/repairable passed: {sum(o.decision is Decision.PASS for o in clean)}"
        f"/{len(clean)}"
    )
    sys.exit(1 if false_pass else 0)


def _tesseract() -> str:
    from scripts.e2e_server import _tesseract as find

    return find()


if __name__ == "__main__":
    main()
