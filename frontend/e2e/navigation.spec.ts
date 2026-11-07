import { type Page, expect, test } from "@playwright/test";

/**
 * Going back at every step: fix the details given at the start of KYC (a wrong GSTIN)
 * without starting over, step-bar links to earlier steps, "Change my answers" from the
 * plan, and a way out of every first screen.
 */

const ORDER_ID = "abcdef12-0000-4000-8000-000000000000";
const APP_ID = "app-1";
const SESSION = "11111111-2222-3333-4444-555555555555";

const signedIn = (page: Page) =>
  page.addInitScript(() => sessionStorage.setItem("udyam.token", "v1.p_test.123.sig"));

function application(extra: Record<string, unknown> = {}) {
  return {
    application_id: APP_ID,
    status: "DRAFT",
    failed_attempts: 0,
    handed_off: false,
    documents: [
      { slot: "GST_CERTIFICATE", source: "LOOKUP", uploaded_at: "2026-07-15T10:00:00+05:30" },
      { slot: "ADDRESS_PROOF", source: "OCR", uploaded_at: "2026-07-15T10:00:00+05:30" },
    ],
    verdict: null,
    on_record: { gstin: true, udyam: true },
    on_record_masked: { contact_name: "J. S.", gstin: "27XUIX******1ZP", udyam_number: "UDYAM-MH-00-***6696" },
    ...extra,
  };
}

const order = {
  order_id: ORDER_ID,
  state: "QUOTE_ACCEPTED",
  quote_id: "q-1",
  contract_months: 12,
  monthly_total_paise: 141482,
  setup_total_paise: 0,
  amount_due_now_paise: 141482,
  lines: [{ product_id: "P01", product_name: "Biz Fibre 100", qty: 1 }],
  kyc_status: null,
  next_action: "KYC",
  payments: [],
  tracker: { kyc: { status: "DRAFT", application_id: APP_ID, handed_off: false, fixes: [] } },
};

test("a wrong GSTIN can be fixed after uploading, without starting over", async ({ page }) => {
  await signedIn(page);
  const edits: unknown[] = [];
  await page.route("**/api/v1/**", async (route) => {
    const req = route.request();
    const p = new URL(req.url()).pathname;
    if (p.endsWith(`/orders/${ORDER_ID}`)) return route.fulfill({ json: order });
    if (p.endsWith("/on-record")) {
      edits.push(req.postDataJSON());
      return route.fulfill({
        json: {
          ...application({
            documents: [{ slot: "ADDRESS_PROOF", source: "OCR", uploaded_at: "2026-07-15T10:00:00+05:30" }],
            on_record: { gstin: true, udyam: false },
            on_record_masked: { contact_name: "J. S.", gstin: "27AAPF******1ZV", udyam_number: null },
          }),
          changed: ["gstin_on_record", "udyam_on_record"],
          documents_dropped: ["GST_CERTIFICATE"],
        },
      });
    }
    if (p.includes("/lookup/gstin/")) {
      return route.fulfill({ json: { gstin: "27AAPFU0939F1ZV", status: "ACTIVE", legal_name: "R*** K***", trade_name: "S*** D***", state: "Maharashtra" } });
    }
    if (p.endsWith(`/kyc/applications/${APP_ID}`)) return route.fulfill({ json: application() });
    return route.fulfill({ status: 404, json: {} });
  });
  await page.goto(`/en/kyc?o=${ORDER_ID}`);
  const details = page.getByTestId("kyc-details");
  await expect(details).toContainText("27XUIX******1ZP");
  await expect(details).not.toContainText("if you have one");
  await details.getByTestId("kyc-details-edit").click();
  const form = details.getByTestId("kyc-details-form");
  await expect(form.getByLabel("GSTIN")).toHaveAttribute("placeholder", "Now: 27XUIX******1ZP");
  await form.getByLabel("GSTIN").fill("27aapfu0939f1zv");
  // "I don't have one" for the Udyam number.
  await form.getByRole("checkbox").nth(1).check();
  await form.getByRole("button", { name: "Save details" }).click();
  expect(edits).toEqual([{ gstin: "27aapfu0939f1zv", udyam_number: null }]);
  await expect(details.getByTestId("kyc-details-notice")).toHaveText(
    "Your details are updated. Please check these again: GST certificate.",
  );
  await expect(details).toContainText("27AAPF******1ZV");
  await expect(details.getByTestId("gst-preview")).toContainText("S*** D***, Maharashtra");
  // The GST card is open again (online check offered), the bill photo is kept.
  await expect(page.getByTestId("kyc-online-GST_CERTIFICATE")).toBeVisible();
  await expect(page.getByTestId("kyc-slot-ADDRESS_PROOF").getByTestId("kyc-slot-done")).toBeVisible();
});

test("editing can be cancelled and leaves everything as it was", async ({ page }) => {
  await signedIn(page);
  let patched = 0;
  await page.route("**/api/v1/**", async (route) => {
    const p = new URL(route.request().url()).pathname;
    if (p.endsWith("/on-record")) patched += 1;
    if (p.endsWith(`/orders/${ORDER_ID}`)) return route.fulfill({ json: order });
    if (p.endsWith(`/kyc/applications/${APP_ID}`)) return route.fulfill({ json: application() });
    return route.fulfill({ status: 404, json: {} });
  });
  await page.goto(`/en/kyc?o=${ORDER_ID}`);
  await page.getByTestId("kyc-details-edit").click();
  await page.getByTestId("kyc-details-form").getByLabel("GSTIN").fill("WRONG");
  await page.getByRole("button", { name: "Cancel" }).click();
  await expect(page.getByTestId("kyc-details-form")).toHaveCount(0);
  await expect(page.getByTestId("kyc-details")).toContainText("27XUIX******1ZP");
  expect(patched).toBe(0);
});

test("the first KYC form has a way back, and the step bar links to checkout", async ({ page }) => {
  await signedIn(page);
  await page.route("**/api/v1/**", async (route) => {
    const p = new URL(route.request().url()).pathname;
    if (p.endsWith(`/orders/${ORDER_ID}`)) {
      return route.fulfill({ json: { ...order, tracker: { kyc: { status: null, application_id: null, handed_off: false, fixes: [] } } } });
    }
    return route.fulfill({ status: 404, json: {} });
  });
  await page.goto(`/en/kyc?o=${ORDER_ID}`);
  await expect(page.getByTestId("kyc-on-record")).toBeVisible();
  await expect(page.getByTestId("kyc-back")).toHaveAttribute("href", `/en/track/${ORDER_ID}`);
  await expect(page.getByTestId("journey-back-2")).toHaveAttribute("href", "/en/checkout?q=q-1");
  // Steps not done yet are never links.
  await expect(page.getByTestId("journey-back-4")).toHaveCount(0);
});

test("from the plan: change my answers; from checkout: back to the plan", async ({ page }) => {
  await page.route("**/api/v1/quotes/**", (route) =>
    route.fulfill({
      json: {
        quote_id: "q-1",
        session_id: SESSION,
        version: 1,
        status: "ISSUED",
        locale: "en",
        created_at: "2026-07-15T10:00:00+05:30",
        valid_until: "2099-01-01T10:00:00+05:30",
        contract_months: 12,
        required_contract_months: 12,
        contract_options: [12],
        lines: [],
        totals: {
          list_monthly_paise: 0,
          savings_paise: 0,
          monthly_subtotal_paise: 0,
          monthly_gst_paise: 0,
          monthly_total_paise: 0,
          setup_subtotal_paise: 0,
          setup_gst_paise: 0,
          setup_total_paise: 0,
        },
        gst_rate_bps: 1800,
        options: [],
      },
    }),
  );
  await page.goto("/en/quote?q=q-1");
  await expect(page.getByTestId("change-answers")).toHaveAttribute("href", `/en/advisor?s=${SESSION}`);
  await page.goto("/en/checkout?q=q-1");
  await expect(page.getByTestId("journey-back-1")).toHaveAttribute("href", "/en/quote?q=q-1");
});

test("order list and My data have a way home", async ({ page }) => {
  for (const path of ["/hi/track", "/hi/privacy"]) {
    await page.goto(path);
    await expect(page.getByTestId("home")).toHaveAttribute("href", "/hi");
  }
});
