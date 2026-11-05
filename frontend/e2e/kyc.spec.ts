import path from "node:path";

import { type Page, expect, test } from "@playwright/test";

/** Smart KYC screen against a mocked API: on-record details, uploads, verdicts, fixes (E2E-01/02 UI). */

const ORDER_ID = "abcdef12-0000-4000-8000-000000000000";
const APP_ID = "app-1";
const SPECIMENS = path.resolve(__dirname, "../../backend/tests/fixtures/kyc/specimens");
const FILES = {
  GST_CERTIFICATE: "gst_certificate",
  UDYAM_CERTIFICATE: "udyam_certificate",
  SIGNATORY_ID: "signatory_id",
  ADDRESS_PROOF: "address_proof",
} as const;
type Slot = keyof typeof FILES;

const specimen = (id: string, slot: Slot) => path.join(SPECIMENS, `${id}_${FILES[slot]}.png`);

function application(status: string, docs: Slot[], verdict: Record<string, unknown> | null = null) {
  return {
    application_id: APP_ID,
    status,
    failed_attempts: status === "FAIL" ? 1 : 0,
    handed_off: false,
    documents: docs.map((slot) => ({ slot, source: "OCR", uploaded_at: "2026-07-15T10:00:00+05:30" })),
    verdict,
  };
}

const order = (kyc: string | null, next: string) => ({
  order_id: ORDER_ID,
  state: next === "PAY" ? "KYC_VERIFIED" : "QUOTE_ACCEPTED",
  quote_id: "q-1",
  contract_months: 12,
  monthly_total_paise: 141482,
  setup_total_paise: 0,
  amount_due_now_paise: 141482,
  lines: [{ product_id: "P01", product_name: "Biz Fibre 100", qty: 1 }],
  kyc_status: kyc,
  next_action: next,
  payments: [],
  tracker: { kyc: { status: kyc, application_id: null, handed_off: false, fixes: [] } },
});

interface Options {
  verdict: { status: string; verdict: Record<string, unknown> };
  uploadReply?: (slot: Slot) => { status: number; json: unknown } | null;
  consent?: boolean;
}

async function mockKyc(page: Page, opts: Options) {
  const seen = { created: [] as unknown[], uploads: [] as { slot: string; type: string; bytes: number; locale: string | null }[], submits: [] as (string | null)[] };
  const docs = new Set<Slot>();
  let submitted = false;
  await page.addInitScript(() => sessionStorage.setItem("udyam.token", "v1.p_test.123.sig"));
  await page.route("**/api/v1/**", async (route) => {
    const req = route.request();
    const url = new URL(req.url());
    const p = url.pathname;
    if (p.endsWith(`/orders/${ORDER_ID}`)) {
      return route.fulfill({ json: submitted && opts.verdict.status === "PASS" ? order("PASS", "PAY") : order(null, "KYC") });
    }
    if (opts.consent === false) return route.fulfill({ status: 403, json: { detail: { code: "CONSENT_REQUIRED" } } });
    if (p.endsWith("/kyc/applications") && req.method() === "POST") {
      seen.created.push(req.postDataJSON());
      return route.fulfill({ status: 201, json: application("DRAFT", []) });
    }
    const upload = p.match(/\/documents\/([A-Z_]+)$/);
    if (upload) {
      const slot = upload[1] as Slot;
      seen.uploads.push({ slot, type: req.headers()["content-type"], bytes: req.postDataBuffer()?.length ?? 0, locale: url.searchParams.get("locale") });
      const custom = opts.uploadReply?.(slot);
      if (custom) return route.fulfill(custom);
      docs.add(slot);
      return route.fulfill({ status: 201, json: { document_id: `d-${slot}`, slot, accepted: true, reason: null, fix: null } });
    }
    if (p.endsWith("/submit")) {
      submitted = true;
      seen.submits.push(url.searchParams.get("locale"));
      return route.fulfill({ json: application(opts.verdict.status, [...docs], opts.verdict.verdict) });
    }
    if (p.endsWith(`/kyc/applications/${APP_ID}`)) {
      return route.fulfill({ json: submitted ? application(opts.verdict.status, [...docs], opts.verdict.verdict) : application("DRAFT", [...docs]) });
    }
    return route.fulfill({ status: 404, json: { detail: { code: "NOT_FOUND" } } });
  });
  return seen;
}

async function declare(page: Page, name: string, gstin: string, udyam: string) {
  const form = page.getByTestId("kyc-on-record");
  await form.getByLabel(/Name of the person signing/).fill(name);
  await form.getByLabel(/GSTIN/).fill(gstin);
  await form.getByLabel(/Udyam number/).fill(udyam);
  await form.getByRole("button", { name: "Continue to documents" }).click();
}

async function uploadAll(page: Page, id: string) {
  for (const slot of Object.keys(FILES) as Slot[]) {
    await page.getByTestId(`kyc-file-${slot}`).setInputFiles(specimen(id, slot));
    await expect(page.getByTestId(`kyc-slot-${slot}`).getByTestId("kyc-slot-done")).toHaveText("Uploaded");
  }
}

test("E2E-01 (UI): K03 documents upload, pass, and lead to payment", async ({ page }) => {
  const seen = await mockKyc(page, {
    verdict: {
      status: "PASS",
      verdict: { decision: "PASS", review_required: false, lookup_offered: false, handed_off: false, checks: [], fixes: [] },
    },
  });
  await page.goto(`/en/kyc?o=${ORDER_ID}`);
  await expect(page.getByRole("heading", { level: 1 })).toHaveText("Verify your business");
  await expect(page.getByTestId("kyc-on-record")).toContainText("never by the AI helper");
  await declare(page, "Joseph Singh", "27xuixg0767d1zp", "UDYAM-MH-00-9516696");
  await expect(page.getByTestId("kyc-submit")).toBeDisabled();
  await uploadAll(page, "K03");
  await page.getByTestId("kyc-submit").click();

  const verdict = page.getByTestId("kyc-verdict");
  await expect(verdict).toHaveAttribute("data-decision", "PASS");
  await expect(verdict).toContainText("All documents verified");
  await expect(page.getByTestId("kyc-to-payment")).toHaveAttribute("href", "/en/checkout?q=q-1");

  expect(seen.created).toEqual([{ contact_name: "Joseph Singh", gstin: "27xuixg0767d1zp", udyam_number: "UDYAM-MH-00-9516696" }]);
  expect(seen.uploads.map((u) => u.slot)).toEqual(Object.keys(FILES));
  // Raw bytes, no multipart, no file name (UP-06).
  expect(seen.uploads.every((u) => u.type === "application/octet-stream" && u.bytes > 10_000 && u.locale === "en")).toBe(true);
  expect(seen.submits).toEqual(["en"]);
  const stored = await page.evaluate(() => JSON.stringify(sessionStorage));
  expect(stored).not.toContain("Joseph");
  expect(stored).not.toContain("27XUIXG0767D1ZP");
});

test("E2E-02 (UI): K11 stale bill shows the exact fix on the address-proof slot", async ({ page }) => {
  const message =
    "Your electricity bill is dated 31 Oct 2025, more than 90 days ago. Please upload a bill dated on or after 16 Apr 2026.";
  await mockKyc(page, {
    verdict: {
      status: "FAIL",
      verdict: {
        decision: "FAIL",
        review_required: false,
        lookup_offered: false,
        handed_off: false,
        checks: [{ check_id: "address.date", status: "FAIL", reason: "ADDRESS_PROOF_STALE", slot: "ADDRESS_PROOF" }],
        fixes: [{ reason: "ADDRESS_PROOF_STALE", slot: "ADDRESS_PROOF", message }],
      },
    },
  });
  await page.goto(`/en/kyc?o=${ORDER_ID}`);
  await declare(page, "Vikram Pillai", "03KGZXT1320R1ZZ", "UDYAM-PB-00-9719478");
  await uploadAll(page, "K11");
  await page.getByTestId("kyc-submit").click();
  await expect(page.getByTestId("kyc-verdict")).toHaveAttribute("data-decision", "FAIL");
  const slot = page.getByTestId("kyc-slot-ADDRESS_PROOF");
  await expect(slot.getByTestId("kyc-fixes")).toHaveText(message);
  await expect(page.getByTestId("kyc-slot-GST_CERTIFICATE").getByTestId("kyc-fixes")).toHaveCount(0);
  await expect(page.getByTestId("kyc-to-payment")).toHaveCount(0);
  // Re-uploading the bill clears its old fix until the next check.
  await page.getByTestId("kyc-file-ADDRESS_PROOF").setInputFiles(specimen("K03", "ADDRESS_PROOF"));
  await expect(slot.getByTestId("kyc-fixes")).toHaveCount(0);
});

test("K06: a failed GSTIN check offers typing the number instead", async ({ page }) => {
  await mockKyc(page, {
    verdict: {
      status: "ACTION_NEEDED",
      verdict: {
        decision: "ACTION_NEEDED",
        review_required: false,
        lookup_offered: false,
        handed_off: false,
        checks: [],
        fixes: [{ reason: "GSTIN_CHECKSUM_INVALID", slot: "GST_CERTIFICATE", message: "The GSTIN on your certificate (08YKZX••••••••ZO) is not valid." }],
      },
    },
  });
  await page.goto(`/en/kyc?o=${ORDER_ID}`);
  await declare(page, "Pooja Shah", "08YKZXS5189L1ZO", "");
  await uploadAll(page, "K06");
  await page.getByTestId("kyc-submit").click();
  const gst = page.getByTestId("kyc-slot-GST_CERTIFICATE");
  await expect(gst.getByTestId("kyc-fixes")).toContainText("is not valid");
  await expect(gst.getByLabel("Or type the number")).toBeVisible();
  await expect(page.getByTestId("kyc-slot-SIGNATORY_ID").getByLabel("Or type the number")).toHaveCount(0);
});

test("upload problems are explained on the slot right away", async ({ page }) => {
  await mockKyc(page, {
    verdict: { status: "PASS", verdict: { decision: "PASS", review_required: false, lookup_offered: false, handed_off: false, checks: [], fixes: [] } },
    uploadReply: (slot) =>
      slot === "GST_CERTIFICATE"
        ? { status: 201, json: { document_id: "d", slot, accepted: false, reason: "WRONG_DOCUMENT_TYPE", fix: "This looks like your electricity bill, not your GST certificate. Please upload your GST certificate here." } }
        : slot === "SIGNATORY_ID"
          ? { status: 413, json: { detail: { code: "FILE_TOO_LARGE" } } }
          : null,
  });
  await page.goto(`/en/kyc?o=${ORDER_ID}`);
  await declare(page, "Joseph Singh", "", "");
  await page.getByTestId("kyc-file-GST_CERTIFICATE").setInputFiles(specimen("K03", "ADDRESS_PROOF"));
  await expect(page.getByTestId("kyc-slot-GST_CERTIFICATE").getByTestId("kyc-upload-fix")).toHaveText(
    "This looks like your electricity bill, not your GST certificate. Please upload your GST certificate here.",
  );
  await page.getByTestId("kyc-file-SIGNATORY_ID").setInputFiles(specimen("K03", "SIGNATORY_ID"));
  await expect(page.getByTestId("kyc-slot-SIGNATORY_ID").getByTestId("kyc-upload-fix")).toHaveText(
    "This file is over 5 MB. Please take a smaller photo.",
  );
});

test("without KYC consent the screen points to the consent page (Hindi)", async ({ page }) => {
  await mockKyc(page, {
    consent: false,
    verdict: { status: "PASS", verdict: { decision: "PASS", review_required: false, lookup_offered: false, handed_off: false, checks: [], fixes: [] } },
  });
  await page.goto(`/hi/kyc?o=${ORDER_ID}`);
  await expect(page.getByRole("heading", { level: 1 })).toHaveText("अपना व्यवसाय सत्यापित करें");
  await page.getByTestId("kyc-on-record").getByRole("button").click();
  const alert = page.getByRole("alert").filter({ hasText: "KYC" });
  await expect(alert).toContainText("कृपया पहले व्यवसाय सत्यापन (KYC) की अनुमति दें।");
  await expect(alert.getByRole("link")).toHaveAttribute("href", "/hi/consent");
});

test("checkout links an order waiting on KYC to the document screen", async ({ page }) => {
  await page.addInitScript((id) => {
    sessionStorage.setItem("udyam.token", "v1.p_test.123.sig");
    sessionStorage.setItem("udyam.order.q-1", id);
  }, ORDER_ID);
  await page.route("**/api/v1/**", (route) => {
    const p = new URL(route.request().url()).pathname;
    if (p.endsWith(`/orders/${ORDER_ID}`)) return route.fulfill({ json: order(null, "KYC") });
    return route.fulfill({ status: 404, json: { detail: { code: "NOT_FOUND" } } });
  });
  await page.goto("/en/checkout?q=q-1");
  await expect(page.getByTestId("kyc-link")).toHaveAttribute("href", `/en/kyc?o=${ORDER_ID}`);
});
