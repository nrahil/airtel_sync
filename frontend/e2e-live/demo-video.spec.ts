import fs from "node:fs";
import path from "node:path";

import { type APIRequestContext, type Page, devices, expect, test } from "@playwright/test";

import hi from "../messages/hi.json";

/**
 * The 3-minute demo video, recorded on the real stack (docs/DEMO_SCRIPT.md):
 * a Hindi-speaking kirana owner goes landing → advisor → quote (Starter vs Recommended)
 * → consent → fastest KYC (online GSTIN + DigiLocker + one photo) → e-sign → pay →
 * tracker (book the install visit) → back office (one call list, call outcome).
 * English captions are drawn on the page so the video reads without narration.
 *
 *   DEMO_VIDEO=1 npx playwright test -c playwright.live.config.ts demo-video
 *
 * Skipped otherwise. The .webm lands in DEMO_VIDEO_DIR (default: test-results/demo-video).
 */
test.skip(!process.env.DEMO_VIDEO, "set DEMO_VIDEO=1 to record the demo video");
test.setTimeout(420_000);

const t = hi;
const API = `http://127.0.0.1:${process.env.E2E_API_PORT ?? 8100}/api/v1`;
const OUT = process.env.DEMO_VIDEO_DIR ?? path.resolve(__dirname, "../test-results/demo-video");
const SPECIMENS = path.resolve(__dirname, "../../backend/tests/fixtures/kyc/specimens");
const C01 = { name: "Joseph Singh", gstin: "27XUIXG0767D1ZP", udyam: "UDYAM-MH-00-9516696" };
const PASSWORD = process.env.BACKOFFICE_DEMO_PASSWORD ?? "demo-password-2026"; // gitleaks:allow (demo users)
const before = (template: string) => template.split("{")[0].trim();

/** Caption bar at the bottom of the screen, then hold so it can be read. */
async function say(page: Page, text: string, holdMs: number) {
  await page.evaluate((caption) => {
    let el = document.getElementById("__demo_caption");
    if (!el) {
      el = document.createElement("div");
      el.id = "__demo_caption";
      Object.assign(el.style, {
        position: "fixed",
        left: "8px",
        right: "8px",
        bottom: "8px",
        zIndex: "2147483647",
        background: "rgba(17,17,17,0.9)",
        color: "#fff",
        font: "600 15px/1.35 system-ui, -apple-system, Segoe UI, sans-serif",
        padding: "10px 12px",
        borderRadius: "12px",
        pointerEvents: "none",
      });
      document.body.appendChild(el);
    }
    el.textContent = caption;
  }, text);
  await page.waitForTimeout(holdMs);
}

async function show(page: Page, testId: string) {
  await page.getByTestId(testId).first().scrollIntoViewIfNeeded();
}

/** Owners who stopped part-way, so the back office has app journeys to show. */
async function stalledOwner(request: APIRequestContext, phone: string, callback: boolean) {
  let body = await (await request.post(`${API}/advisor/sessions`, { data: { locale: "hi", mode: "form" } })).json();
  const sid = body.session_id as string;
  const answers: Record<string, unknown> = { segment: "prof", city: "Pune", employee_count: 8, num_sites: 1, has_gstin: true };
  for (let i = 0; i < 6 && body.status !== "QUOTED"; i++) {
    body = await (
      await request.post(`${API}/advisor/sessions/${sid}/turns`, { data: { slot: body.question.slot, value: answers[body.question.slot] } })
    ).json();
  }
  const quote = await (await request.post(`${API}/quotes`, { data: { session_id: sid } })).json();
  const challenge = await (await request.post(`${API}/auth/otp/request`, { data: { phone } })).json();
  const { token } = await (
    await request.post(`${API}/auth/otp/verify`, { data: { challenge_id: challenge.challenge_id, code: challenge.dev_code } })
  ).json();
  const auth = { Authorization: `Bearer ${token}` };
  await request.post(`${API}/consents`, {
    headers: auth,
    data: { notice_version: t.consent.noticeVersion, locale: "hi", choices: { NEEDS_ASSESSMENT: true, KYC_VERIFICATION: true, MARKETING: false } },
  });
  if (callback) {
    await request.post(`${API}/advisor/sessions/${sid}/help`, { headers: auth, data: { kind: "callback", agree: true } });
  } else {
    await request.post(`${API}/orders`, { headers: { ...auth, "Idempotency-Key": crypto.randomUUID() }, data: { quote_id: quote.quote_id } });
  }
}

test("demo video", async ({ browser, request }) => {
  await stalledOwner(request, "98222 00001", false); // ordered, never uploaded documents
  await stalledOwner(request, "98222 00002", true); // asked us to call
  fs.mkdirSync(OUT, { recursive: true });
  // Video is recorded in CSS pixels, so a phone viewport gives a tiny video. Use a 2×
  // viewport and zoom the page 2×: the same 412-px phone layout, recorded sharp.
  const size = { width: 824, height: 1830 };
  const context = await browser.newContext({
    userAgent: devices["Pixel 7"].userAgent,
    viewport: size,
    hasTouch: true,
    recordVideo: { dir: OUT, size },
  });
  await context.addInitScript(() => {
    document.addEventListener("DOMContentLoaded", () => {
      document.documentElement.style.zoom = "2";
    });
  });
  const page = await context.newPage();

  // 0:00 Landing
  await page.goto("/hi");
  await say(page, "Udyam Digital: a kirana owner in Surat opens it on a phone. It opens in Hindi; 10 languages.", 6000);
  await page.getByTestId("start").click();

  // 0:08 Advisor
  await expect(page.getByLabel(t.advisor.placeholder)).toBeVisible();
  await say(page, "No login, no salesperson. Type or tap Speak; replies can be read aloud.", 4500);
  await page.getByLabel(t.advisor.placeholder).pressSequentially("मेरी सूरत में किराने की दुकान है, 4 लोग काम करते हैं", { delay: 25 });
  await page.getByRole("button", { name: t.advisor.send, exact: true }).click();
  const plan = page.getByTestId("recommendation");
  const form = page.getByTestId("form-segment");
  await expect(plan.or(form)).toBeVisible({ timeout: 30_000 });
  if (await form.isVisible()) {
    // AI helper unavailable: the guided form takes over (the same plan, no LLM needed).
    await say(page, "AI helper offline? The app switches to quick buttons and carries on.", 3500);
    await form.getByRole("button", { name: t.segment.micro }).click();
    const city = page.getByTestId("form-city");
    await city.getByLabel(t.advisor.cityPlaceholder).fill("सूरत");
    await city.getByRole("button", { name: t.advisor.next }).click();
    const staff = page.getByTestId("form-employee_count");
    await staff.getByLabel(t.advisor.numberPlaceholder).fill("4");
    await staff.getByRole("button", { name: t.advisor.next }).click();
  }
  await expect(plan).toBeVisible({ timeout: 30_000 });
  await show(page, "recommendation");
  await say(page, "One message → a plan with one monthly price incl. GST, and a plain 'why' under every product.", 7000);
  await show(page, "why-P01");
  await say(page, "Prices and eligibility come from the catalogue rules, never from the AI.", 4500);

  // 0:35 Quote
  await page.getByRole("button", { name: t.quote.customise }).click();
  await expect(page.getByTestId("plans")).toBeVisible({ timeout: 20_000 });
  await show(page, "plans");
  await say(page, "Price-sensitive shop: a cheaper Starter plan is offered next to Recommended, with what it gives up.", 7000);
  await show(page, "delta");
  await say(page, "Every add-on shows the exact monthly change before you tap it, including any lost bundle discount.", 6500);
  await show(page, "contract");
  await say(page, "12 or 24 months, each with its monthly price. A read-only link can be sent to the owner's accountant.", 5500);

  // 0:58 Checkout: OTP + consent
  await page.getByRole("button", { name: new RegExp(t.quote.continue) }).click();
  await page.getByLabel(t.otp.phone).fill("98222 00099");
  await page.getByRole("button", { name: t.otp.send }).click();
  const code = (await page.getByTestId("dev-code").textContent())?.match(/\d{6}/)?.[0];
  await page.getByLabel(t.otp.code).fill(code ?? "");
  await page.getByRole("button", { name: t.otp.verify }).click();
  const consent = page.getByTestId("consent");
  await expect(consent).toBeVisible();
  await say(page, "DPDP consent: three separate choices, none pre-ticked; each says what, why and how long.", 6000);
  await consent.getByRole("checkbox", { name: new RegExp(t.consent.NEEDS_ASSESSMENT.title) }).check();
  await consent.getByRole("checkbox", { name: new RegExp(t.consent.KYC_VERIFICATION.title.replace(/[()]/g, ".")) }).check();
  await consent.getByRole("button", { name: t.consent.save }).click();
  await page.getByRole("button", { name: t.checkout.placeOrder }).click();
  await expect(page.getByTestId("kyc-link")).toBeVisible();
  await page.getByTestId("kyc-link").click();

  // 1:15 Smart KYC, fastest path
  const record = page.getByTestId("kyc-on-record");
  await record.getByLabel(t.kyc.contactName).fill(C01.name);
  await record.getByLabel(t.kyc.gstin).fill(C01.gstin);
  await record.getByLabel(t.kyc.udyam).fill(C01.udyam);
  await say(page, "KYC: the GSTIN is matched with the registry as it is typed ('Is this your business?').", 4500);
  await record.getByRole("button", { name: t.kyc.start }).click();
  await say(page, "Fastest way first: online check (instant) → DigiLocker (~30 s) → photo (~1 min).", 5500);
  await page.getByTestId("kyc-online-GST_CERTIFICATE").click();
  await expect(page.getByTestId("kyc-slot-GST_CERTIFICATE").getByTestId("kyc-slot-done")).toBeVisible({ timeout: 20_000 });
  for (const slot of ["SIGNATORY_ID", "UDYAM_CERTIFICATE"] as const) {
    await page.getByTestId(`kyc-digilocker-${slot}`).click();
    const dialog = page.getByTestId("digilocker-consent");
    await expect(dialog).toBeVisible();
    if (slot === "SIGNATORY_ID") await say(page, "DigiLocker asks first; we keep the name only, never the Aadhaar number.", 5000);
    await dialog.getByRole("button", { name: t.kyc.digilockerAllow, exact: true }).click();
    await expect(page.getByTestId(`kyc-slot-${slot}`).getByTestId("kyc-slot-done")).toBeVisible({ timeout: 20_000 });
  }
  await page.getByTestId("kyc-file-ADDRESS_PROOF").setInputFiles(path.join(SPECIMENS, "C01_address_proof.png"));
  await expect(page.getByTestId("kyc-slot-ADDRESS_PROOF").getByTestId("kyc-slot-done")).toBeVisible({ timeout: 30_000 });
  await say(page, "One photo: the electricity bill. OCR runs on our own server; images never leave it.", 4500);
  await page.getByTestId("kyc-submit").click();
  const verdict = page.getByTestId("kyc-verdict");
  await expect(verdict).toHaveAttribute("data-decision", "PASS", { timeout: 90_000 });
  await show(page, "kyc-verdict");
  await say(page, "Verified in seconds, not days. A wrong or stale document gets the exact fix instead.", 5000);

  // 1:55 e-sign + pay
  await page.getByTestId("kyc-to-payment").click();
  const esign = page.getByTestId("esign");
  await expect(esign).toBeVisible();
  await say(page, "e-sign replaces the paper form: the owner sees exactly what they agree to.", 4500);
  await esign.getByRole("button", { name: t.checkout.signSend }).click();
  const signCode = (await esign.getByTestId("esign-dev-code").textContent())?.match(/\d{6}/)?.[0];
  await esign.getByLabel(t.otp.code).fill(signCode ?? "");
  await esign.getByRole("button", { name: t.checkout.signConfirm, exact: true }).click();
  await expect(page.getByTestId("esigned")).toBeVisible();
  await page.getByRole("button", { name: new RegExp(`^${before(t.checkout.pay)}`) }).click();
  await expect(page.getByTestId("paid")).toBeVisible();
  await say(page, "Setup + first month paid (mock gateway). A failed payment can be retried; nothing is charged twice.", 4000);

  // 2:12 Tracker
  await page.getByTestId("track-link").click();
  await expect(page.getByTestId("next-sentence")).toBeVisible();
  await say(page, "The tracker says what happens next, with a date, so nobody has to call to ask.", 5000);
  const staffLogin = await page.request.post(`${API}/backoffice/auth/login`, { data: { username: "ops", password: PASSWORD } });
  const staffToken = ((await staffLogin.json()) as { token: string }).token;
  const orderId = page.url().split("/").pop();
  for (const state of ["FEASIBILITY_CHECK", "INSTALL_SCHEDULED"] as const) {
    await say(page, `Staff post "${t.track.states[state]}" from the back office. The owner's tracker updates by itself.`, 1500);
    await page.request.post(`${API}/backoffice/orders/${orderId}/fulfilment`, {
      headers: { Authorization: `Bearer ${staffToken}` },
      data: { state },
    });
    await expect(page.getByTestId("order-state")).toHaveText(t.track.states[state], { timeout: 15_000 });
  }
  const picker = page.getByTestId("visit-picker");
  await expect(picker).toBeVisible();
  await picker.scrollIntoViewIfNeeded();
  await picker.locator('[data-testid^="visit-day-"]').nth(1).click();
  await picker.locator('[data-testid^="visit-window-"]:enabled').first().click();
  await say(page, "The owner books the installation visit and can move it until the day before.", 4000);
  await page.getByTestId("visit-book").click();
  await expect(page.getByTestId("visit-change")).toBeVisible();
  await show(page, "notifications");
  await say(page, "Every step also goes out on WhatsApp, then SMS (mocked here).", 4000);

  // 2:30 Back office
  await page.goto("/en/backoffice");
  await page.getByLabel("Username").fill("sales");
  await page.getByLabel("Password").fill(PASSWORD);
  await page.getByRole("button", { name: "Sign in" }).click();
  const list = page.getByTestId("leads");
  await expect(list.getByTestId("lead").first()).toBeVisible({ timeout: 60_000 });
  await say(page, "Sales and partners: one call list. Owners who asked for a call come first.", 5000);
  const app = list.locator('[data-testid="session"]', { has: page.getByTestId("basis") }).first();
  if (await app.count()) {
    await app.scrollIntoViewIfNeeded();
    await say(page, "Each card says why: what went wrong, and how likely they are to buy, from the 5,000 past leads.", 5500);
  }
  const lead = list.getByTestId("lead").first();
  await lead.scrollIntoViewIfNeeded();
  await lead.getByTestId("call-log").getByRole("combobox").selectOption("REACHED_INTERESTED");
  await lead.getByTestId("call-log").getByRole("button").click();
  await say(page, "Contact details stay masked until a logged reveal; call outcomes are recorded. Partners see only their leads.", 5500);
  await say(page, "Discover, buy, verify and track: on a phone, in the owner's language.", 4000);

  const video = page.video();
  await context.close();
  if (video) {
    const target = path.join(OUT, "udyam-digital-demo.webm");
    await video.saveAs(target);
    console.log(`[demo] video: ${target}`);
  }
});
