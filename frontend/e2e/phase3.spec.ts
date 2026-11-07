import { type Page, expect, test } from "@playwright/test";

/**
 * Round 2 Phase 3 against a mocked API: the fastest KYC path (online check → DigiLocker →
 * photo), e-signing the order form before payment, the "My data" privacy centre, and the
 * consent notice saying how long each thing is kept.
 */

const TOKEN = "v1.p_test.123.sig";
const ORDER_ID = "abcdef12-0000-4000-8000-000000000000";
const APP_ID = "app-1";

const signedIn = (page: Page) => page.addInitScript((t) => sessionStorage.setItem("udyam.token", t), TOKEN);

type Doc = { slot: string; source: string; uploaded_at: string };

function application(docs: Doc[]) {
  return {
    application_id: APP_ID,
    status: "DRAFT",
    failed_attempts: 0,
    handed_off: false,
    documents: docs,
    verdict: null,
    on_record: { gstin: true, udyam: true },
  };
}

const doc = (slot: string, source: string): Doc => ({ slot, source, uploaded_at: "2026-07-15T10:00:00+05:30" });

function order(next: string, extra: Record<string, unknown> = {}) {
  return {
    order_id: ORDER_ID,
    state: "KYC_VERIFIED",
    quote_id: "q-1",
    contract_months: 12,
    monthly_total_paise: 391790,
    setup_total_paise: 413000,
    amount_due_now_paise: 804790,
    lines: [
      { product_id: "P01", product_name: "Biz Fibre 100", qty: 1 },
      { product_id: "P07", product_name: "Business Postpaid 299", qty: 2 },
    ],
    kyc_status: "PASS",
    next_action: next,
    payments: [],
    esign: null,
    tracker: { kyc: { status: "PASS", application_id: APP_ID, handed_off: false, fixes: [] } },
    ...extra,
  };
}

// ------------------------------------------------------------------ KYC: fastest path first

async function mockKycDocs(page: Page, locker: Record<string, { status: number; json: unknown }>) {
  const docs: Doc[] = [];
  const asked: { slot: string; allow: boolean }[] = [];
  await signedIn(page);
  await page.route("**/api/v1/**", async (route) => {
    const req = route.request();
    const p = new URL(req.url()).pathname;
    if (p.endsWith(`/orders/${ORDER_ID}`)) return route.fulfill({ json: order("KYC", { kyc_status: null }) });
    const fetch = p.match(/\/digilocker\/([A-Z_]+)$/);
    if (fetch) {
      const slot = fetch[1];
      const { allow } = req.postDataJSON() as { allow: boolean };
      asked.push({ slot, allow });
      const custom = locker[slot];
      if (custom && allow) return route.fulfill(custom);
      if (!allow) return route.fulfill({ status: 409, json: { detail: { code: "DIGILOCKER_DECLINED" } } });
      docs.push(doc(slot, "DIGILOCKER"));
      return route.fulfill({ json: application(docs) });
    }
    if (p.endsWith("/gst/lookup")) {
      docs.push(doc("GST_CERTIFICATE", "LOOKUP"));
      return route.fulfill({ json: application(docs) });
    }
    if (p.endsWith(`/kyc/applications/${APP_ID}`)) return route.fulfill({ json: application(docs) });
    return route.fulfill({ status: 404, json: {} });
  });
  return asked;
}

test("KYC lists the fastest option first, with how long each takes", async ({ page }) => {
  await mockKycDocs(page, {});
  await page.addInitScript((id) => sessionStorage.setItem("udyam.kyc.application", id), APP_ID);
  await page.goto("/en/kyc");
  await expect(page.getByTestId("kyc-fastest")).toContainText("Fastest way: about 1 minute");
  const gst = page.getByTestId("kyc-slot-GST_CERTIFICATE");
  await expect(gst.getByTestId("kyc-online-GST_CERTIFICATE")).toContainText("· instant");
  await expect(gst.locator("label")).toContainText("· about 1 minute");
  // Online check comes before the photo in the card.
  const order = await gst.evaluate((el) =>
    [...el.querySelectorAll("button, label")].map((n) => n.getAttribute("data-testid") ?? n.tagName),
  );
  expect(order.indexOf("kyc-online-GST_CERTIFICATE")).toBeLessThan(order.indexOf("LABEL"));
  const id = page.getByTestId("kyc-slot-SIGNATORY_ID");
  await expect(id.getByTestId("kyc-digilocker-SIGNATORY_ID")).toContainText("Fetch from DigiLocker · about 30 seconds");
  // The bill has no faster option: photo only.
  const bill = page.getByTestId("kyc-slot-ADDRESS_PROOF");
  await expect(bill.locator('[data-testid^="kyc-online-"], [data-testid^="kyc-digilocker-"]')).toHaveCount(0);
});

test("DigiLocker: its own consent screen, then the ID arrives without a photo", async ({ page }) => {
  const asked = await mockKycDocs(page, {});
  await page.addInitScript((id) => sessionStorage.setItem("udyam.kyc.application", id), APP_ID);
  await page.goto("/en/kyc");
  await page.getByTestId("kyc-online-GST_CERTIFICATE").click();
  await page.getByTestId("kyc-digilocker-SIGNATORY_ID").click();
  const dialog = page.getByTestId("digilocker-consent");
  await expect(dialog).toContainText("DigiLocker (demo)");
  await expect(dialog).toContainText("fetch your Aadhaar (your name only) from your DigiLocker");
  await dialog.getByRole("button", { name: "Allow", exact: true }).click();
  await expect(page.getByTestId("kyc-slot-SIGNATORY_ID").getByTestId("kyc-slot-done")).toHaveText("From DigiLocker");
  await expect(page.getByTestId("kyc-slot-GST_CERTIFICATE").getByTestId("kyc-slot-done")).toHaveText("Verified from registry");
  expect(asked).toEqual([{ slot: "SIGNATORY_ID", allow: true }]);

  // Saying no fetches nothing (and is still sent, so it is audited).
  await page.getByTestId("kyc-digilocker-UDYAM_CERTIFICATE").click();
  await page.getByTestId("digilocker-consent").getByRole("button", { name: "Don't allow" }).click();
  await expect(page.getByTestId("kyc-slot-UDYAM_CERTIFICATE").getByTestId("kyc-upload-fix")).toHaveText(
    "Nothing was fetched. You can take a photo instead.",
  );
  expect(asked.at(-1)).toEqual({ slot: "UDYAM_CERTIFICATE", allow: false });
});

test("DigiLocker without the document says to take a photo", async ({ page }) => {
  await mockKycDocs(page, {
    SIGNATORY_ID: { status: 422, json: { detail: { code: "DIGILOCKER_NOT_LINKED" } } },
  });
  await page.addInitScript((id) => sessionStorage.setItem("udyam.kyc.application", id), APP_ID);
  await page.goto("/hi/kyc");
  await page.getByTestId("kyc-digilocker-SIGNATORY_ID").click();
  await page.getByTestId("digilocker-consent").getByRole("button", { name: "अनुमति दें", exact: true }).click();
  await expect(page.getByTestId("kyc-slot-SIGNATORY_ID").getByTestId("kyc-upload-fix")).toHaveText(
    "यह आपके DigiLocker में नहीं मिला। कृपया इसकी फ़ोटो लें।",
  );
});

// ------------------------------------------------------------------ e-sign before payment

test("e-sign: the order form, a code, then payment opens", async ({ page }) => {
  await signedIn(page);
  await page.addInitScript((id) => sessionStorage.setItem("udyam.order.q-1", id), ORDER_ID);
  let signed = false;
  let sentCode: string | null = null;
  await page.route("**/api/v1/**", async (route) => {
    const req = route.request();
    const p = new URL(req.url()).pathname;
    if (p.endsWith("/quotes/q-1")) return route.fulfill({ status: 404, json: {} });
    if (p.endsWith(`/orders/${ORDER_ID}`)) return route.fulfill({ json: order(signed ? "PAY" : "SIGN") });
    if (p.endsWith(`/orders/${ORDER_ID}/esign`)) {
      return route.fulfill({
        status: 201,
        json: {
          request_id: "es-1",
          status: "PENDING",
          document_sha256: "a".repeat(64),
          form: { order_id: ORDER_ID, contract_months: 12, monthly_total_paise: 391790, setup_total_paise: 413000, lines: [] },
          expires_at: "2099-01-01T00:00:00+05:30",
          dev_code: "482913",
        },
      });
    }
    if (p.endsWith(`/orders/${ORDER_ID}/esign/es-1`)) {
      sentCode = (req.postDataJSON() as { code: string }).code;
      if (sentCode !== "482913") {
        return route.fulfill({ status: 400, json: { detail: { code: "ESIGN_CODE_WRONG", attempts_left: 4 } } });
      }
      signed = true;
      return route.fulfill({
        json: order("PAY", { esign: { signed_at: "2026-07-15T11:30:00+05:30", document_sha256: "a".repeat(64) } }),
      });
    }
    return route.fulfill({ status: 404, json: {} });
  });
  await page.goto("/en/checkout?q=q-1");
  const esign = page.getByTestId("esign");
  await expect(page.getByText("Your documents are verified. Next: sign your order form.")).toBeVisible();
  await expect(esign).toContainText("This replaces a paper signature.");
  await expect(esign.getByTestId("esign-form")).toContainText("Business Postpaid 299 × 2");
  await expect(esign.getByTestId("esign-form")).toContainText("₹3,918/month · One-time setup ₹4,130 · 12 months");
  await expect(page.getByRole("button", { name: /^Pay/ })).toHaveCount(0); // not before signing

  await esign.getByRole("button", { name: "Send code to my Aadhaar-linked mobile" }).click();
  await expect(esign.getByTestId("esign-dev-code")).toHaveText("Demo code: 482913");
  await esign.getByLabel("6-digit code").fill("111111");
  await esign.getByRole("button", { name: "Sign", exact: true }).click();
  await expect(esign.getByRole("alert")).toHaveText("That code is wrong. 4 tries left.");
  await esign.getByLabel("6-digit code").fill("482913");
  await esign.getByRole("button", { name: "Sign", exact: true }).click();
  await expect(page.getByTestId("esigned")).toContainText("Signed on");
  await expect(page.getByRole("button", { name: /Pay ₹8,048/ })).toBeVisible();
  expect(sentCode).toBe("482913");
});

// ------------------------------------------------------------------ My data

const SUMMARY = {
  phone: "********10",
  consents: {
    NEEDS_ASSESSMENT: { granted: true, at: "2026-07-15T10:00:00+05:30" },
    KYC_VERIFICATION: { granted: true, at: "2026-07-15T10:00:00+05:30" },
    MARKETING: { granted: true, at: "2026-07-15T10:00:00+05:30" },
  },
  advisor_sessions: 1,
  orders: [{ order_id: ORDER_ID, state: "KYC_VERIFIED", created_at: "2026-07-15T10:00:00+05:30", products: ["Biz Fibre 100"] }],
  kyc_applications: [
    {
      application_id: APP_ID,
      status: "PASS",
      readable: true,
      on_record: { contact_name: "J. S.", gstin: "27XUIX******1ZP", udyam_number: null },
      documents: [
        { slot: "ADDRESS_PROOF", source: "OCR", uploaded_at: "2026-07-15T10:00:00+05:30", image_retained: true, image_delete_after: null },
        { slot: "SIGNATORY_ID", source: "DIGILOCKER", uploaded_at: "2026-07-15T10:00:00+05:30", image_retained: false, image_delete_after: null },
      ],
    },
  ],
};

async function mockPrivacy(page: Page, opts: { blocked?: boolean } = {}) {
  const calls: string[] = [];
  let marketing = true;
  await signedIn(page);
  await page.route("**/api/v1/privacy/**", async (route) => {
    const req = route.request();
    const p = new URL(req.url()).pathname;
    calls.push(`${req.method()} ${p.split("/privacy/")[1]}`);
    if (p.endsWith("/summary")) {
      return route.fulfill({
        json: { ...SUMMARY, consents: { ...SUMMARY.consents, MARKETING: { granted: marketing, at: null } } },
      });
    }
    if (p.endsWith("/withdrawal")) {
      marketing = false;
      return route.fulfill({ json: { purpose: "MARKETING", changed: true, granted: false, deletion_scheduled_for: null } });
    }
    if (p.endsWith("/export")) return route.fulfill({ json: { principal: "p_test", orders: [] } });
    if (p.endsWith("/erasure")) {
      if (opts.blocked) return route.fulfill({ status: 409, json: { detail: { code: "ERASURE_BLOCKED", orders: [] } } });
      return route.fulfill({ json: { erased: true, orders_cancelled: [ORDER_ID] } });
    }
    return route.fulfill({ status: 404, json: {} });
  });
  return calls;
}

test("My data shows what we hold, masked, and lets the owner turn off a choice", async ({ page }) => {
  await mockPrivacy(page);
  await page.goto("/en/privacy");
  await expect(page.getByRole("heading", { level: 1 })).toHaveText("My data");
  await expect(page.getByTestId("privacy-phone")).toHaveText("********10");
  const kyc = page.getByTestId("privacy-kyc");
  await expect(kyc).toContainText("27XUIX******1ZP");
  await expect(kyc).not.toContainText("if you have one"); // form hints don't belong on a data page
  await expect(kyc).toContainText("Photo kept while your order is in progress");
  await expect(kyc).toContainText("From DigiLocker");
  await expect(page.getByTestId("privacy-orders")).toContainText("Biz Fibre 100");
  // Required choices warn that turning them off stops the order.
  await expect(page.getByTestId("consent-KYC_VERIFICATION")).toContainText("Turning this off stops your order");
  await expect(page.getByTestId("consent-MARKETING")).not.toContainText("Turning this off stops your order");
  await page.getByTestId("consent-MARKETING").getByRole("button", { name: "Turn off" }).click();
  await expect(page.getByTestId("consent-MARKETING")).toContainText("Off");
  await expect(page.getByTestId("consent-MARKETING").getByRole("button")).toHaveCount(0);
  await expect(page.getByText("Grievance officer: privacy@accel.example")).toBeVisible();
});

test("My data: download, then delete everything with an explanation", async ({ page }) => {
  const calls = await mockPrivacy(page);
  await page.goto("/en/privacy");
  const download = page.waitForEvent("download");
  await page.getByTestId("privacy-download").click();
  expect((await download).suggestedFilename()).toBe("my-data.json");

  const del = page.getByTestId("privacy-delete");
  await expect(del.getByRole("button", { name: "Delete everything" })).toBeDisabled();
  await del.getByLabel("I understand this can't be undone").check();
  await del.getByRole("button", { name: "Delete everything" }).click();
  await expect(page.getByTestId("erased")).toContainText("earlier orders can't be shown");
  expect(calls).toContain("POST erasure");
  expect(await page.evaluate(() => sessionStorage.getItem("udyam.token"))).toBeNull();
});

test("My data: deletion refused while the service is live says why", async ({ page }) => {
  await mockPrivacy(page, { blocked: true });
  await page.goto("/hi/privacy");
  await page.getByTestId("privacy-delete").getByRole("checkbox").check();
  await page.getByTestId("privacy-delete").getByRole("button").click();
  await expect(page.getByTestId("privacy-problem")).toContainText("क़ानून ये रिकॉर्ड रखने को कहता है");
});

test("My data asks to sign in first", async ({ page }) => {
  await page.goto("/en/privacy");
  await expect(page.getByText("Confirm your mobile number to see your data.")).toBeVisible();
  await expect(page.getByLabel("Mobile number")).toBeVisible();
});

test("consent says how long each thing is kept, and links to My data; so does the landing page", async ({ page }) => {
  await signedIn(page);
  await page.route("**/api/v1/consents", (route) =>
    route.fulfill({
      json: {
        notice_version: "2026-11-07.1",
        purposes: {
          NEEDS_ASSESSMENT: { granted: false, required: true },
          KYC_VERIFICATION: { granted: false, required: true },
          MARKETING: { granted: false, required: false },
        },
        ready_for_checkout: false,
      },
    }),
  );
  await page.goto("/en/consent");
  await expect(page.getByTestId("keep-KYC_VERIFICATION")).toHaveText(
    "Kept: document photos for 7 days after your service starts or you cancel; checked details as long as the law requires.",
  );
  await expect(page.getByTestId("keep-MARKETING")).toHaveText("Kept: until you turn it off.");
  await expect(page.getByTestId("consent-my-data")).toHaveAttribute("href", "/en/privacy");
  await page.goto("/hi");
  await expect(page.getByTestId("my-data")).toHaveText("मेरा डेटा");
  await expect(page.getByTestId("my-data")).toHaveAttribute("href", "/hi/privacy");
});

// ------------------------------------------------------------------ consent step at checkout

async function mockConsentCheckout(page: Page, reply: "ok" | "outdated") {
  await signedIn(page);
  let granted = { NEEDS_ASSESSMENT: false, KYC_VERIFICATION: false, MARKETING: false };
  const state = () => ({
    notice_version: "2026-11-07.1",
    purposes: {
      NEEDS_ASSESSMENT: { granted: granted.NEEDS_ASSESSMENT, required: true },
      KYC_VERIFICATION: { granted: granted.KYC_VERIFICATION, required: true },
      MARKETING: { granted: granted.MARKETING, required: false },
    },
    ready_for_checkout: granted.NEEDS_ASSESSMENT && granted.KYC_VERIFICATION,
  });
  await page.route("**/api/v1/**", async (route) => {
    const req = route.request();
    const p = new URL(req.url()).pathname;
    if (p.endsWith("/quotes/q-1")) {
      return route.fulfill({
        json: {
          quote_id: "q-1",
          session_id: "11111111-2222-3333-4444-555555555555",
          lines: [{ product_id: "P01", product_name: "Biz Fibre 100", qty: 1 }],
          totals: { monthly_total_paise: 141482, setup_total_paise: 0 },
        },
      });
    }
    if (p.endsWith("/consents") && req.method() === "GET") return route.fulfill({ json: state() });
    if (p.endsWith("/consents")) {
      if (reply === "outdated") return route.fulfill({ status: 409, json: { detail: { code: "NOTICE_OUTDATED" } } });
      granted = (req.postDataJSON() as { choices: typeof granted }).choices;
      return route.fulfill({ json: state() });
    }
    return route.fulfill({ status: 404, json: {} });
  });
}

test("the 'rather not share documents' help appears only after saving without KYC", async ({ page }) => {
  await mockConsentCheckout(page, "ok");
  await page.goto("/en/checkout?q=q-1");
  const form = page.getByTestId("consent");
  await expect(form).toBeVisible();
  await expect(page.getByTestId("no-kyc-help")).toHaveCount(0); // not before any choice is saved
  await form.getByRole("checkbox", { name: /Plan recommendation/ }).check();
  await form.getByRole("button", { name: "Save my choices" }).click();
  await expect(page.getByTestId("no-kyc-help")).toContainText("Rather not share documents online?");
  // Changing their mind: with KYC allowed the help goes away and the order can be placed.
  await form.getByRole("checkbox", { name: /Business verification/ }).check();
  await form.getByRole("button", { name: "Save my choices" }).click();
  await expect(page.getByRole("button", { name: "Confirm order" })).toBeVisible();
  await expect(page.getByTestId("no-kyc-help")).toHaveCount(0);
});

test("an outdated consent notice offers to reload the page", async ({ page }) => {
  await mockConsentCheckout(page, "outdated");
  await page.goto("/en/checkout?q=q-1");
  const form = page.getByTestId("consent");
  await form.getByRole("checkbox", { name: /Plan recommendation/ }).check();
  await form.getByRole("button", { name: "Save my choices" }).click();
  await expect(form.getByRole("status")).toHaveText("This notice was updated. Please review it again.");
  await expect(page.getByTestId("consent-reload")).toHaveText("Reload the page");
  await expect(page.getByTestId("no-kyc-help")).toHaveCount(0);
});
