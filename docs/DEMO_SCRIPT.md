# Demo script — Udyam Digital (under 3 minutes)

One kirana owner in Surat, one phone, in Hindi: landing → advisor → Starter vs Recommended → consent → fastest KYC (online GSTIN + DigiLocker + one photo) → e-sign → pay → tracker with a booked install visit → back office.
Everything shown is the real stack: deterministic catalogue engines, Groq for the conversation, real Tesseract OCR on the provided specimens. WhatsApp/SMS, DigiLocker, e-sign and payment are **mocks** and are said to be.

## The recorded video

The video is produced by a script, so it can be regenerated after any change (about 2 min 15 s, English captions burnt in, the owner's screens in Hindi):

```bash
cd frontend
DEMO_VIDEO=1 npx playwright test -c playwright.live.config.ts demo-video
# → test-results/demo-video/udyam-digital-demo.webm
```

The live config starts its own throw-away backend (fresh DB, today = 2026-07-15) and a production build. To include Groq, add `--groq` to the `scripts.e2e_server` command in a copy of the config (needs `GROQ_API_KEY` in `backend/.env`); without it the advisor switches to the guided form and the video still completes. To narrate, play the video and read the right-hand column.

## Doing it live

```bash
# Terminal 1 — throw-away backend with Groq; demo staff users (password demo-password-2026)
cd backend && .venv/Scripts/python -m scripts.e2e_server --port 8000 --origin http://localhost:3000 --groq
# Terminal 2 — production build of the PWA
cd frontend && npm run build && npm start
```

Chrome DevTools → device toolbar → **Pixel 7**. Specimens are in `backend/tests/fixtures/kyc/specimens/`. C01 (clean applicant) = Joseph Singh · `27XUIXG0767D1ZP` · `UDYAM-MH-00-9516696`. Run one KYC check before recording to warm OCR.

| Time | Screen | Do | Say |
|---|---|---|---|
| 0:00 | Landing `/hi` | Show the one-line promise and language row; tap **शुरू करें**. | "A kirana owner in Surat on a phone. It opens in Hindi; ten languages. No login, no salesperson." |
| 0:08 | Advisor | Type **"मेरी सूरत में किराने की दुकान है, 4 लोग काम करते हैं"** and send. Point at **बोलें** and **🔈 जवाब बोलकर सुनाएँ**. | "They can type or speak, and have replies read aloud. One message is enough." |
| 0:18 | Plan | Scroll the plan: four products, one monthly total incl. GST, the WhatsApp line "needs a GST registration — tell me if you have one", and **"why"** under each product. | "One clear monthly price, and a plain reason for every line. Prices and eligibility come from the catalogue rules, never from the AI." |
| 0:32 | Quote | **बदलें और आगे बढ़ें**. Show **Starter ₹2,828 / Recommended ₹3,918**, the change shown on each toggle, and the 12/24-month buttons. | "Micro shops are price-sensitive, so a Starter plan sits next to the recommended one, with what it gives up. Each add-on shows its exact rupee change before you tap. A read-only link can go to the accountant." |
| 0:55 | Checkout | Enter a number, use the demo code. Show the consent screen. Tick the two required choices, save, **ऑर्डर पक्का करें**. | "DPDP: three separate choices, none pre-ticked, each saying what, why and how long. A 'My data' page lets them see, download or delete it all." |
| 1:05 | KYC | Enter C01's details (the registry match appears as you type). Then **इसकी जगह मेरा GSTIN ऑनलाइन जाँचें** on GST, **DigiLocker से लें** → **अनुमति दें** on ID and Udyam, upload `C01_address_proof.png`, **मेरे दस्तावेज़ जाँचें**. | "Fastest way first: online check, then DigiLocker, then a photo. From the Aadhaar we keep the name only. One photo, OCR on our own server. Verified in seconds." |
| 1:30 | e-sign + pay | Send the code, enter it, **हस्ताक्षर करें**, then pay. | "e-sign replaces the paper form. Setup plus first month; a failed payment can be retried without a double charge." |
| 1:42 | Tracker | Show "what happens next" and the timed WhatsApp updates. In a second tab, sign in to `/en/backoffice` as `ops` and post site check, then installation booked; the tracker updates by itself. Pick a day and window, **…बुक करें**. | "No status-chase calls: the tracker says what happens next with a date, and the owner books the install visit themselves." |
| 1:58 | Back office `/en/backoffice` | Sign in as `sales`. Show the "call me" owner on top, an app journey ("Of 325 businesses like this that got this far, 34% went live"), factors in plain words; log a call. | "For sales and partners: one call list, people who asked first, each card saying what went wrong and how likely they are to buy. Details stay masked until a logged reveal." |
| 2:12 | — | — | "Discover, buy, verify and track: on a phone, in the owner's language." |

Optional extras if there is time: a K11 stale bill ("upload a bill dated on or after 16 Apr 2026") or K06 altered GSTIN digit, both in the KYC screen.

## If something goes wrong
- **Groq rate-limited or off:** the advisor switches to quick buttons (business type, city, staff) and reaches the same plan.
- **"इस कीमत की अवधि खत्म हो गई है"** (price expired): the backend clock is not on `TODAY_OVERRIDE`; use `scripts.e2e_server`, which sets it.
- **KYC check is slow the first time:** OCR warms up on the first image; run one check before recording.
- **An OTP is refused:** each phone gets a few codes per 10 minutes; use a new number.
- **Back office list has no app journeys:** they appear after a minute of inactivity (the demo server's idle window).
