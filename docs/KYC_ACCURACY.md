# KYC accuracy on documents it has never seen

## Decision rules

| Result | API value | When |
|---|---|---|
| Verified | `PASS` | Every check matches the registries and the documents agree |
| Manual review | `REVIEW` | Anything unclear or correctable: a differing field, an unreadable or stale document, a missing certificate. A person decides; the owner still sees what to fix |
| Rejected | `FAIL` | The GST registry marks the registration cancelled or suspended |

A value OCR could not read is reported as unreadable, never as a mismatch.

The 12 provided applicants (47 images) all give the expected decision in the team's table,
and C01 (K03 with a Udyam certificate matching the registry) is verified; that is the golden suite (`backend/tests/kyc_golden`). To check the pipeline is not just fitted to
those images, `backend/scripts/kyc_stress.py` makes **new** applicants and runs them
through the real pipeline, exactly as an upload would.

```bash
cd backend
python -m scripts.kyc_stress --per-case 3 --photo --seed 2026   # ~25 min; exit 1 on any false pass
```

## How the test documents are made

- **Applicants** are drawn from the provided GSTIN and Udyam registries (businesses with
  both), with the proprietor or a contact as signatory and a recent bill.
- **Layout** copies the specimens (labels, value column, 46-character address wrap,
  diagonal red watermark). A clean applicant's documents match the registries in full.
- **Typefaces:** DejaVu (the specimens' own), Arial + Consolas, Times + Lucida Console,
  Verdana + Courier New. Only DejaVu and Arial were used while fixing; Times and Verdana
  are held out.
- **Phone-photo damage** (`--photo`): tilt up to 3.5°, rescale 0.85–1.25×, soft focus,
  exposure shift, sensor noise, JPEG quality 55–85.
- **31 cases**: a clean applicant, a GSTIN printed with look-alike glyphs, and 29 planted
  problems — altered GSTIN digit, another business's GSTIN, cancelled / suspended GSTIN,
  changed legal / trade name, constitution, GST address, GST date, blank / unknown /
  other business's Udyam number, Udyam name, city/state, date, activity, missing Udyam, signatory
  not on record / expired / other business, stale / future / other-address /
  other-unit / other-pincode / other-business bill, wrong document, blurred, tiny.

## Results (seed 2026, 744 applicants = 31 cases x 3 x 4 typefaces x render/photo)

**False passes: 0 of 744.** No planted problem was ever verified. The decision itself
was right for 730 of 744 (98.1%).

The table is stricter: the right decision **and** the specific planted reason found.

| Typeface | Clean render | Phone photo |
|---|---|---|
| DejaVu (specimen typeface) | 93/93 (100%) | 91/93 (97.8%) |
| Arial / Consolas | 92/93 (98.9%) | 88/93 (94.6%) |
| Times / Lucida Console (held out) | 93/93 (100%) | 87/93 (93.5%) |
| Verdana / Courier New (held out) | 89/93 (95.7%) | 65/93 (69.9%) |
| **All** | | **698/744 (93.8%)** |

The 14 wrong decisions all ended in manual review: 7 of 48 clean or repairable applicants
on photos or Arial (held back, not wrongly verified) and 7 of 48 cancelled or suspended
applicants on photos, whose GSTIN could not be read so the registry status was not reached.
The weak spot is a thin typewriter face (Courier New) on a blurred photo.

## What the test found, and what changed

| Found on unseen documents | Change |
|---|---|
| GST address and date, Udyam city and state were never compared, so a changed one passed | Compared with the registry; a difference goes to REVIEW |
| A bill for "Shop/Unit 81" passed for a business at "Shop/Unit 80" (word overlap 78%) | Addresses must also share every number (unit, pincode) |
| An unreadable Udyam enterprise name was silently skipped | Now asks for a clearer copy |
| DejaVu Mono `J` read as `I` (and `3`), `L` as `U`, `0` as `G`/`Q`, a stray extra glyph → genuine GSTINs failed the checksum | Look-alike repair, at most two glyphs or one stray glyph, accepted only if the checksum passes, the registry has it and it is the only such value; never one digit for another (K06 still fails) |
| Udyam prefix read `UDYAK-`/`UDYAH-`, district `00` read `0`/`80`/`908` | Repaired only to the single registered number with the same state and 7-digit serial |
| "Iyer" read "lyer" → clean proprietor rejected | Names compare look-alike glyphs as equal |
| Watermark painted white left scars on grey photo paper; page unreadable | Watermark painted in the paper's colour; unrecognised page retried with lighting stretched |
| Watermark fragments glued onto values ("Mumbai Ane X…", "IA Ishita …") | Only addresses wrap onto extra lines; values cut at wide gaps, noise trimmed from the ends |
| A label crossed by the watermark lost its field | Missing fields re-read from a sharpened black/white copy of the page |
| Identifier misreads on photos | Extra sharpened black/white reads; thickened strokes as a last resort for thin type |
| A blank Udyam number on a photo read as garbage ("number doesn't look right") | Blank judged on the whole value band; an unfound label is "unreadable", not "blank" |

## Notes on the provided specimens

- Udyam registration date and major activity differ from the registry on the specimens; each
  difference goes to manual review with its own reason.
- K02's certificate prints the registry GSTIN exactly. K06's prints `S8189` where the
  registry has `S5189`, reported as a failed check digit; it still goes to manual review.
- **No pincode-to-state check**: the dataset's pincodes are synthetic.

## Limits

The parser reads this certificate format by its labels. A real government layout
(e.g. Form GST REG-06, with different labels) would not be read field by field; it fails
safe — "we couldn't read part of your certificate" or review — never a wrong pass.
DigiLocker and the GSTIN/Udyam lookups are the intended path for such documents.
