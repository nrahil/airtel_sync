# Demo script — Udyam Digital (3 minutes)

One owner, one phone, discovery to a live service: Hindi advisor → plan → tier-2 block → dependency toggle → consent → K03 PASS → K11 / K06 fixes → tracker → back-office call list.
Everything shown is the real stack: deterministic engines, real Tesseract OCR on the provided specimens, and Groq unreachable on purpose.

## Before recording (5 minutes, not on camera)

```bash
# Terminal 1 — throw-away backend: fresh DB and keys, today = 2026-07-15, Groq unreachable,
# demo staff users (password demo-password-2026)
cd backend && .venv/Scripts/python -m scripts.e2e_server --port 8000 --origin http://localhost:3000
# Terminal 2 — production build of the PWA (API at http://localhost:8000)
cd frontend && npm run build && npm start
```

- Chrome DevTools → device toolbar → **Pixel 7**, zoom 100 %.
- **Tab A** (main journey): `http://localhost:3000/hi/advisor`.
- **Tab B** (prepared fails): open `http://localhost:3000/en/consent`, sign in with `98765 00011`, tick both required consents, save. Open `/en/kyc`, enter K11's details (Vikram Pillai · `03KGZXT1320R1ZZ` · `UDYAM-PB-00-9719478`) and upload the four `K11_*.png` files. **Don't press "Check my documents" yet.**
- **Tab C**: do the same in Hindi at `/hi/consent` and `/hi/kyc` for K06 (Pooja Shah · `08YKZXS5189L1ZO` · `UDYAM-RJ-00-6577157`).
- **Tab D**: `http://localhost:3000/en/backoffice`, signed out.
- The specimen images are in `backend/tests/fixtures/kyc/specimens/` (`K03_*.png`, `K06_*.png`, `K11_*.png`).
- Optional hands-free recording of the same journeys: `cd frontend && E2E_VIDEO=1 npm run test:e2e:live`. The videos land in `frontend/test-results/`.

## Script

| Time | Screen | Do | Say |
|---|---|---|---|
| 0:00 | Tab A, Hindi advisor | Type **"मेरी सूरत में किराने की दुकान है"** and send. | "A kirana owner in Surat, on a phone, in Hindi. No login, no forms to start. The chat is anonymous, and ID or phone numbers are stripped before any AI sees them." |
| 0:12 | Guided options | The AI helper is down, so the app switches to quick options: **दुकान या स्थानीय सेवा**, Surat, 4, 1, **हाँ**, **मोबाइल हॉटस्पॉट**, then needs **इंटरनेट, कार्ड / UPI भुगतान (POS), वाई-फ़ाई** → **आगे**. | "Even with the LLM offline, the journey continues on deterministic templates. AI phrases the conversation; it never decides products or prices." |
| 0:35 | Recommendation | Point at the bundle, the monthly total and the blocked WhatsApp API line. Tap **बदलें और आगे बढ़ें**. | "Biz Fibre, two postpaid lines, POS and Wi-Fi: one monthly price incl. GST. WhatsApp API waits for a verified GSTIN, and it says so." |
| 0:45 | Quote | Scroll to **Biz Fibre 1G**: *उपलब्ध नहीं — अभी आपके शहर में उपलब्ध नहीं* with **इसके बजाय Biz Fibre 300 लें**. | "Surat is tier 2, so 1G isn't sold there. Instead of a dead end, the owner gets the reason and an alternative." |
| 0:55 | Quote | **जोड़ें Cloud CCTV** → the price updates. Then **हटाएँ Biz Fibre 100**. | "Toggle CCTV and the server re-prices. Now remove the fibre…" |
| 1:05 | Cascade card | Read it, tap the first fix (**Biz Fibre 100 जोड़ें**). | "…and CCTV and Wi-Fi would lose their connection. Nothing breaks silently: one tap restores a valid plan. This is the dependency rule from the catalogue." |
| 1:15 | Checkout | **चेकआउट पर जाएँ** → enter `98765 00001` → copy the demo code → verify. | "The number is used only to sign in and send updates. It's stored encrypted, never in the browser." |
| 1:25 | Consent | Show that **nothing is pre-ticked**. Tick the two required purposes, leave marketing off, save, then **ऑर्डर पक्का करें**. | "DPDP: three separate purposes in plain Hindi, the processors (Groq gets chat text only, never documents), 7-day image retention, and the owner's rights." |
| 1:40 | KYC | **दस्तावेज़ अपलोड करें** → enter K03's details → upload the four `K03_*.png` → **मेरे दस्तावेज़ जाँचें**. | "Four photos. OCR runs on our own server, then GSTIN and Udyam checksums, registry lookups and cross-document checks." |
| 1:55 | PASS | Green **सभी दस्तावेज़ सत्यापित** → **भुगतान पर आगे बढ़ें** → pay by UPI. | "Verified in seconds, not days. Pay setup plus the first month." |
| 2:05 | Tab B | Press **Check my documents** (K11). | "Another applicant. Their electricity bill is from 31 Oct 2025, stale against the 90-day rule, and the app names the exact date that would pass: on or after 16 Apr 2026." |
| 2:15 | Tab C | Press **मेरे दस्तावेज़ जाँचें** (K06). | "Here one GSTIN digit was altered. The checksum fails, and we refuse to auto-correct it to a similar registry number: a fix message, not a guess." |
| 2:25 | Tab A tracker | **इस ऑर्डर को ट्रैक करें**. Show the timeline, ETA per line and WhatsApp updates. Tap the dashed demo button through **साइट की जाँच → इंस्टॉलेशन बुक → इंस्टॉल हो गया → सेवाएँ चालू**. | "The tracker replaces the three status-chase calls: ETA per product from install days, a 21-day SLA alarm, and a WhatsApp message at every step." |
| 2:42 | Tab D back office | Sign in as `sales` / `demo-password-2026`. Show **Call next**, then **Unclosed sessions**. | "For sales: leads the advisor didn't close, ranked by a propensity model on pre-contact data (AUC 0.72, 2.4× lift in the top decile), with masked PII and a logged reveal." |
| 2:55 | — | — | "Discover, buy, verify, track: on a phone, in your language." |

## If something goes wrong
- **"इस कीमत की अवधि खत्म हो गई है"** (price expired): the backend clock was not started with `TODAY_OVERRIDE`. Use `scripts.e2e_server`, which sets it.
- **The KYC check takes more than 10 s:** the first OCR call warms Tesseract. Run one upload before recording.
- **An OTP is refused:** each phone allows a few codes per 10 minutes, so use a new number.
