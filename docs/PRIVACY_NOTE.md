# Privacy note — Udyam Digital

*How we handle a small-business owner's data, under India's Digital Personal Data Protection Act 2023. One page; the controls behind each line are mapped to code and tests in [`SECURITY.md`](../SECURITY.md).*

**Data fiduciary:** Accel Business. **Grievance officer:** privacy@accel.example. **Notice version:** `2026-11-04.1`, shown in all 10 app languages.

## 1. Consent — asked separately, before anything is read
- The advisor is **anonymous**: no name, phone or ID is asked, and any typed by mistake is redacted before it is stored or sent anywhere.
- At checkout the owner confirms a mobile number (OTP) and sees one consent screen with **three separate choices**, none pre-ticked: *plan recommendation* and *business verification (KYC)* are needed to buy; *offers and updates* is optional and off.
- Every grant and withdrawal is written to an append-only, hash-chained **consent ledger** with the notice version. If the notice changes, the owner must accept it again.
- Withdrawing KYC consent stops KYC, lookups, orders and payments immediately.

## 2. Purpose limitation
| Data | Used only for |
|---|---|
| Business type, city, team size, sites, needs | Recommending and pricing a plan |
| GST / Udyam certificate, signatory ID, address proof | Verifying the business, as telecom KYC requires |
| Mobile number | Sign-in and order updates (WhatsApp, then SMS) |
| Lead history (staff only) | Ranking whom to call; built on pre-contact facts only |

KYC routes refuse requests without KYC consent (403). Marketing uses nothing unless that box was ticked.

## 3. Minimisation
- We ask for **four documents**, nothing else. We keep only the fields we verify, plus the verdict.
- The phone number is stored only **sealed** (encrypted); look-ups use a keyed hash.
- Uploads are re-encoded server-side: metadata and EXIF are dropped, and file names are never read.

## 4. Masking and security
- Identifiers are masked everywhere outside the owner's own export, e.g. GSTIN `27XUIX******1ZP`, Udyam `UDYAM-MH-00-***6696`, names as initials. A static audit test fails the build if any log call could print personal data.
- Staff see masked data by default. Revealing a lead needs a reason code, is rate-limited, and is audited; partners see only leads assigned to them.
- **Encryption:** each person has their own AES-256-GCM data key, wrapped by a master key. KYC images, extractions and the phone are sealed under it. All traffic is HTTPS, with strict security headers and CORS.

## 5. Retention
| Data | Kept until |
|---|---|
| Raw document images | **7 days after the journey ends**: activation, cancellation, "not feasible", withdrawal of consent, or 30 idle days without an order. Kept while a paid order is being installed. Deleted by a daily job. |
| Verified fields and verdict | As long as telecom KYC rules require for a live connection |
| Unused OTP challenges | 1 day |
| Consent ledger and audit trail | Kept as proof; they contain **no personal data** |

## 6. Processors and transfers
| Processor | Receives | Location |
|---|---|---|
| **Groq** (AI chat helper) | Only the owner's chat text, with phone numbers, email addresses, GSTIN, Udyam and PAN numbers removed first. **Never documents.** | Outside India (disclosed on the consent screen) |
| Tesseract OCR | Document images | **Our own server**: images never leave it |
| WhatsApp / SMS gateway (mocked in the prototype) | Phone number and order-status text | Contracted Indian gateway |

If Groq is unavailable or switched off, the advisor runs entirely on our own templates and guided form.

## 7. The owner's rights
Signed in with their number, the owner can:
- **see and export** everything we hold (decrypted, for them only);
- **correct** their name, GSTIN or Udyam number (re-verification follows);
- **withdraw** any consent;
- **erase** their data. Erasure cancels open orders with a refund, deletes images, sealed data and the phone mapping, and destroys their personal key ("crypto-shredding"), so any copy left in a backup becomes unreadable. Erasure is refused only while an installation is booked or the line is live, where telecom KYC retention applies.

**Breaches** follow the runbook in `SECURITY.md`: contain, assess, notify the Data Protection Board and the people affected.
