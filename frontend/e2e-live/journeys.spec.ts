import path from "node:path";

import { type Page, expect, test } from "@playwright/test";

import en from "../messages/en.json";
import hi from "../messages/hi.json";

/**
 * E2E-01…05 against the real stack (see playwright.live.config.ts): real advisor engine,
 * real quotes and eligibility, real OTP / consent / orders / mock payments, and real
 * Tesseract OCR over the provided KYC specimens. Groq is unreachable on purpose.
 */

type T = typeof en;
const SPECIMENS = path.resolve(__dirname, "../../backend/tests/fixtures/kyc/specimens");
const SLOTS = {
  GST_CERTIFICATE: "gst_certificate",
  UDYAM_CERTIFICATE: "udyam_certificate",
  SIGNATORY_ID: "signatory_id",
  ADDRESS_PROOF: "address_proof",
} as const;
type Slot = keyof typeof SLOTS;

/** What each applicant declares "on record" (tests/fixtures/kyc/specimens.json). */
const ON_RECORD = {
  K03: { name: "Joseph Singh", gstin: "27XUIXG0767D1ZP", udyam: "UDYAM-MH-00-9516696" },
  K06: { name: "Pooja Shah", gstin: "08YKZXS5189L1ZO", udyam: "UDYAM-RJ-00-6577157" },
  K11: { name: "Vikram Pillai", gstin: "03KGZXT1320R1ZZ", udyam: "UDYAM-PB-00-9719478" },
} as const;

const before = (template: string) => template.split("{")[0].trim();
let phoneSeq = 0;
const freshPhone = () => `9${String(Date.now() % 1e8).padStart(8, "0")}${phoneSeq++ % 10}`;

async function signIn(page: Page, t: T) {
  await page.getByLabel(t.otp.phone).fill(freshPhone());
  await page.getByRole("button", { name: t.otp.send }).click();
  const code = (await page.getByTestId("dev-code").textContent())?.match(/\d{6}/)?.[0];
  expect(code, "dev OTP code is echoed outside production").toBeTruthy();
  await page.getByLabel(t.otp.code).fill(code!);
  await page.getByRole("button", { name: t.otp.verify }).click();
}

async function consentToRequired(page: Page, t: T) {
  const form = page.getByTestId("consent");
  await form.getByRole("checkbox", { name: new RegExp(t.consent.NEEDS_ASSESSMENT.title) }).check();
  await form.getByRole("checkbox", { name: new RegExp(t.consent.KYC_VERIFICATION.title.replace(/[()]/g, ".")) }).check();
  await expect(form.getByRole("checkbox", { name: new RegExp(t.consent.MARKETING.title) })).not.toBeChecked();
  await form.getByRole("button", { name: t.consent.save }).click();
}

async function answerAdvisor(page: Page, t: T, chat: string) {
  await page.getByLabel(t.advisor.placeholder).fill(chat);
  await page.getByRole("button", { name: t.advisor.send }).click();
  // Groq is unreachable: the advisor switches to the guided form and keeps going.
  await page.getByTestId("form-segment").getByRole("button", { name: t.segment.micro }).click();
  const city = page.getByTestId("form-city");
  await city.getByLabel(t.advisor.cityPlaceholder).fill("Surat"); // tier-2 city
  await city.getByRole("button", { name: t.advisor.next }).click();
  for (const [slot, value] of [["employee_count", "4"], ["num_sites", "1"]] as const) {
    const form = page.getByTestId(`form-${slot}`);
    await form.getByLabel(t.advisor.numberPlaceholder).fill(value);
    await form.getByRole("button", { name: t.advisor.next }).click();
  }
  await page.getByTestId("form-has_gstin").getByRole("button", { name: t.advisor.yes }).click();
  await page.getByTestId("form-current_internet").getByRole("button", { name: t.internet.mobile_hotspot }).click();
  const needs = page.getByTestId("form-needs");
  for (const need of ["internet", "pos", "wifi"] as const) await needs.getByRole("button", { name: t.needs[need], exact: true }).click();
  await needs.getByRole("button", { name: t.advisor.next }).click();
  await expect(page.getByTestId("recommendation")).toBeVisible();
}

async function startKyc(page: Page, t: T, who: keyof typeof ON_RECORD) {
  const form = page.getByTestId("kyc-on-record");
  await form.getByLabel(t.kyc.contactName).fill(ON_RECORD[who].name);
  await form.getByLabel(t.kyc.gstin).fill(ON_RECORD[who].gstin);
  await form.getByLabel(t.kyc.udyam).fill(ON_RECORD[who].udyam);
  await form.getByRole("button", { name: t.kyc.start }).click();
  for (const slot of Object.keys(SLOTS) as Slot[]) {
    await page.getByTestId(`kyc-file-${slot}`).setInputFiles(path.join(SPECIMENS, `${who}_${SLOTS[slot]}.png`));
    await expect(page.getByTestId(`kyc-slot-${slot}`).getByTestId("kyc-slot-done")).toHaveText(t.kyc.uploaded);
  }
  await page.getByTestId("kyc-submit").click();
}

/** Sign in on the consent page, accept the required purposes, then open the KYC screen. */
async function kycOnly(page: Page, t: T, locale: string) {
  await page.goto(`/${locale}/consent`);
  await signIn(page, t);
  await consentToRequired(page, t);
  await expect(page.getByTestId("consent").getByRole("status")).toHaveText(t.consent.saved);
  await page.goto(`/${locale}/kyc`);
}

test("E2E-01/03/04: Hindi advisor → plan → P03 blocked → dependency cascade → consent → K03 PASS → pay → track → live", async ({ page }) => {
  const t = hi;
  await page.goto("/hi/advisor");
  await answerAdvisor(page, t, "मेरी सूरत में किराने की दुकान है");
  const card = page.getByTestId("recommendation");
  await expect(card).toContainText("Biz Fibre 100");
  await expect(page.getByTestId("explanation")).not.toBeEmpty();
  await expect(card).toContainText(t.quote.templateNote); // explained from facts, no LLM

  await page.getByRole("button", { name: t.quote.customise }).click();
  await expect(page).toHaveURL(/\/hi\/quote\?q=/);
  await expect(page.getByTestId("quote-lines")).toContainText("Biz Fibre 100");

  // E2E-03: tier-2 city → Biz Fibre 1G is blocked with a reason and an alternative.
  const p03 = page.getByTestId("option-P03");
  await expect(p03.getByTestId("unavailable-reason")).toContainText(t.quote.unavailable);
  await expect(p03.getByRole("button", { name: /Biz Fibre 300/ })).toBeVisible();

  // Toggle an add-on: the price comes back from the server.
  const total = page.getByTestId("monthly-total");
  const firstTotal = await total.textContent();
  await page.getByRole("button", { name: `${t.quote.add} Cloud CCTV` }).click();
  await expect(page.getByTestId("quote-lines")).toContainText("Cloud CCTV");
  await expect(total).not.toHaveText(firstTotal ?? "");

  // E2E-04: removing the fibre while CCTV depends on it → cascade + one-tap fix.
  await page.getByRole("button", { name: `${t.quote.remove} Biz Fibre 100` }).click();
  const cascade = page.getByTestId("cascade");
  await expect(cascade).toContainText(t.quote.cascadeTitle);
  await expect(cascade).toContainText("Cloud CCTV");
  await expect(page.getByTestId("quote-lines")).toContainText("Biz Fibre 100"); // nothing changed yet
  await cascade.getByRole("button").first().click();
  await expect(cascade).toBeHidden();
  await expect(page.getByTestId("quote-lines")).toContainText("Cloud CCTV");

  // Checkout: OTP → unbundled consent (nothing pre-ticked) → order.
  await page.getByRole("button", { name: new RegExp(t.quote.continue) }).click();
  await expect(page).toHaveURL(/\/hi\/checkout\?q=/);
  await signIn(page, t);
  for (const box of await page.getByTestId("consent").getByRole("checkbox").all()) await expect(box).not.toBeChecked();
  await consentToRequired(page, t);
  await page.getByRole("button", { name: t.checkout.placeOrder }).click();
  await expect(page.getByTestId("order")).toContainText(t.checkout.orderPlaced);
  await expect(page.getByTestId("order")).toContainText(t.checkout.kycNext);

  // Smart KYC with the real OCR pipeline: K03 is the clean applicant.
  await page.getByTestId("kyc-link").click();
  await expect(page).toHaveURL(/\/hi\/kyc\?o=/);
  await startKyc(page, t, "K03");
  const verdict = page.getByTestId("kyc-verdict");
  await expect(verdict).toHaveAttribute("data-decision", "PASS", { timeout: 90_000 });
  await expect(verdict).toContainText(t.kyc.status.PASS);
  // Identifiers are never echoed back in the clear.
  await expect(page.locator("main")).not.toContainText(ON_RECORD.K03.gstin);

  // Pay (setup + first month), then track.
  await page.getByTestId("kyc-to-payment").click();
  await page.getByRole("button", { name: new RegExp(`^${before(t.checkout.pay)}`) }).click();
  await expect(page.getByTestId("paid")).toContainText(t.checkout.paid);
  await page.getByTestId("track-link").click();
  await expect(page.getByTestId("order-state")).toHaveText(t.track.states.PAYMENT_CONFIRMED);
  await expect(page.getByTestId("order-eta")).toBeVisible();
  await expect(page.getByTestId("notifications")).toBeVisible();

  // Operations (demo control) move the install forward to activation.
  for (const state of ["FEASIBILITY_CHECK", "INSTALL_SCHEDULED", "INSTALLED", "ACTIVATED"] as const) {
    await page.getByTestId("demo").getByRole("button", { name: `→ ${t.track.states[state]}` }).click();
    await expect(page.getByTestId("order-state")).toHaveText(t.track.states[state]);
  }
  await expect(page.getByTestId("next-action")).toContainText(t.track.next.NONE);
});

test("E2E-02: K11 stale electricity bill → precise fix naming the cut-off date", async ({ page }) => {
  const t = en;
  await kycOnly(page, t, "en");
  await startKyc(page, t, "K11");
  const verdict = page.getByTestId("kyc-verdict");
  await expect(verdict).toHaveAttribute("data-decision", "FAIL", { timeout: 90_000 });
  const fix = page.getByTestId("kyc-slot-ADDRESS_PROOF").getByTestId("kyc-fixes");
  await expect(fix).toContainText("31 Oct 2025");
  await expect(fix).toContainText("Please upload a bill dated on or after 16 Apr 2026.");
  await expect(page.getByTestId("kyc-to-payment")).toHaveCount(0);
});

test("K06 (Hindi): altered GSTIN digit fails with a fix on the GST certificate", async ({ page }) => {
  const t = hi;
  await kycOnly(page, t, "hi");
  await startKyc(page, t, "K06");
  await expect(page.getByTestId("kyc-verdict")).toHaveAttribute("data-decision", "FAIL", { timeout: 90_000 });
  const gst = page.getByTestId("kyc-slot-GST_CERTIFICATE");
  await expect(gst.getByTestId("kyc-fixes")).not.toBeEmpty();
  await expect(page.locator("main")).not.toContainText(ON_RECORD.K06.gstin);
});

test("E2E-05: with Groq down the guided form completes the journey to a priced quote", async ({ page }) => {
  const t = en;
  await page.goto("/en/advisor");
  await answerAdvisor(page, t, "I run a kirana shop in Surat");
  await expect(page.getByTestId("recommendation")).toContainText(t.quote.templateNote);
  await expect(page.getByTestId("monthly-total")).toHaveText(/^₹[\d,]+/);
  await page.getByRole("button", { name: t.quote.customise }).click();
  await expect(page.getByTestId("quote-lines")).toContainText("Biz Fibre 100");
  await expect(page.getByTestId("gst")).toBeVisible();
});

test("back office: sales sees the ranked call list and the model's holdout AUC", async ({ page }) => {
  await page.goto("/en/backoffice");
  await page.getByLabel("Username").fill("sales");
  await page.getByLabel("Password").fill(process.env.BACKOFFICE_DEMO_PASSWORD ?? "demo-password-2026"); // gitleaks:allow (demo users)
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page.getByTestId("staff-who")).toContainText("sales");
  const leads = page.getByTestId("lead");
  await expect(leads.first()).toContainText("%", { timeout: 60_000 }); // model trains on first use
  expect(await leads.count()).toBeGreaterThan(5);
  await page.getByRole("tab", { name: "Model" }).click();
  await expect(page.getByTestId("model-auc")).toHaveText(/^0\.[6-9]\d\d$/);
});
