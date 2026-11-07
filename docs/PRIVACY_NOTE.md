# Privacy and security note — Udyam Digital

*How we handle a small-business owner's data under India's Digital Personal Data Protection Act 2023. One page; each control is mapped to code and tests in [`SECURITY.md`](../SECURITY.md).*

**Data fiduciary:** Accel Business · **Grievance officer:** privacy@accel.example · **Notice version:** `2026-11-07.1`, in all 10 app languages.

## 1. Consent: separate, before anything is read
- The advisor is **anonymous**: no name, phone or ID is asked; any typed by mistake is redacted before it is stored or sent.
- At checkout the owner confirms a mobile number (OTP) and sees **three separate choices, none pre-ticked**, each saying what, why and **how long**: plan recommendation and business verification are needed to buy; offers is optional and off. Saying no to verification still leaves a way forward: "have someone call me".
- Every grant and withdrawal goes to an append-only, hash-chained **consent ledger** with the notice version; a new notice must be accepted again. Withdrawing KYC consent stops KYC, lookups, orders and payment at once.

## 2. Purpose limitation and minimisation
| Data | Used only for |
|---|---|
| Business type, city, team size, sites, needs | Recommending and pricing a plan |
| GST / Udyam numbers, signatory ID, address proof | Verifying the business, as telecom KYC requires |
| Mobile number | Sign-in and order updates (WhatsApp, then SMS) |
| Lead history, call outcomes (staff only) | Deciding whom to call; outcomes are stored as codes, never notes |

- **Fastest path collects least:** an online GSTIN check replaces the GST certificate; **DigiLocker** returns an issuer-signed ID and Udyam certificate after DigiLocker's own Allow screen. From the Aadhaar we keep **the name only, never the Aadhaar number**. Typically one photo (the electricity bill) is needed.
- Uploads are re-encoded on the server (metadata and EXIF dropped). The phone number is stored sealed; look-ups use a keyed hash.
- **e-sign** stores the SHA-256 of the exact order form and the time; the one-time code is kept only as a keyed hash, expires in 10 minutes and locks after 5 wrong tries.
- **Share links** ("send this price to my accountant") carry a random token stored only as a SHA-256 hash, reveal no quote or session ID, are not indexed by search engines, and stop working when the price expires, changes or is ordered.

## 3. Masking, access and encryption
- Identifiers are masked everywhere except the owner's own export (GSTIN `27XUIX******1ZP`, names as initials). A static test fails the build if a log call could print personal data.
- Staff see masked data. Opening it needs a reason code, is rate-limited and audited; partners see and act only on leads assigned to their firm.
- Each person has their own AES-256-GCM key, wrapped by a master key; KYC images, extractions and the phone are sealed under it. HTTPS only, strict CSP and exact-origin CORS.

## 4. Retention
| Data | Kept until |
|---|---|
| Document images | 7 days after the journey ends (activation, cancellation, not feasible, consent withdrawn, or 30 idle days); deleted by a daily job |
| Verified fields and verdict | As long as telecom KYC rules require for a live connection |
| Consent ledger, audit trail, e-sign hash | Kept as proof; no personal data in them |

## 5. Processors and transfers
| Processor | Receives | Where |
|---|---|---|
| Groq (AI chat helper) | Chat text with phone, email, GSTIN, Udyam and PAN removed; **never documents** | Outside India, disclosed on the consent screen |
| Tesseract OCR | Document images | **Our own server**; images never leave it |
| Browser speech | Speaking into the mic: Chrome sends the audio to Google's speech service; the app records nothing. Replies read aloud use the phone's own voice | Owner's browser |
| DigiLocker, e-sign, WhatsApp/SMS | Mocked in the prototype; production uses MeitY DigiLocker, a licensed e-sign ASP and an Indian SMS gateway | India |

If Groq is off, the advisor runs entirely on our templates and guided form.

## 6. The owner's rights: the "My data" page
Signed in with their number, the owner sees what we hold (masked), where each document came from and when its photo is deleted, and can **download** everything, **correct** their details (checks made for the old value are dropped and redone), **turn off** any consent, and **erase** everything. Erasure cancels open orders with a refund, deletes images and sealed data and destroys their key ("crypto-shredding", so backups become unreadable); it is refused only while an install is booked or the line is live. **Breaches** follow the runbook in `SECURITY.md`: contain, assess, notify the Data Protection Board and the people affected.
