import { type Page, expect, test } from "@playwright/test";

/** Checkout against a mocked API: OTP → unbundled consent → idempotent order → payment retry. */

const QUOTE = {
  quote_id: "q-1",
  session_id: "s-1",
  version: 1,
  status: "ISSUED",
  locale: "en",
  created_at: "2026-07-15T10:00:00+05:30",
  valid_until: "2099-01-01T10:00:00+05:30",
  contract_months: 12,
  required_contract_months: 12,
  contract_options: [12, 24],
  lines: [
    { product_id: "P01", product_name: "Biz Fibre 100", qty: 1, unit_paise: 119900, line_total_paise: 119900, setup_total_paise: 0, category: "Connectivity", unit: "per site", list_unit_paise: 119900, discounts: [], min_contract_months: 12 },
  ],
  totals: {
    list_monthly_paise: 119900,
    savings_paise: 0,
    monthly_subtotal_paise: 119900,
    monthly_gst_paise: 21582,
    monthly_total_paise: 141482,
    setup_subtotal_paise: 0,
    setup_gst_paise: 0,
    setup_total_paise: 0,
  },
  gst_rate_bps: 1800,
  options: [],
};

function order(overrides: Record<string, unknown> = {}) {
  return {
    order_id: "abcdef12-0000-4000-8000-000000000000",
    state: "QUOTE_ACCEPTED",
    quote_id: "q-1",
    contract_months: 12,
    monthly_total_paise: 141482,
    setup_total_paise: 0,
    amount_due_now_paise: 141482,
    lines: [{ product_id: "P01", product_name: "Biz Fibre 100", qty: 1 }],
    kyc_status: "PASS",
    next_action: "PAY",
    payments: [],
    ...overrides,
  };
}

const notGranted = {
  notice_version: "2026-11-07.1",
  purposes: {
    NEEDS_ASSESSMENT: { granted: false, required: true },
    KYC_VERIFICATION: { granted: false, required: true },
    MARKETING: { granted: false, required: false },
  },
  ready_for_checkout: false,
};

interface Seen {
  otp: unknown[];
  consents: unknown[];
  orderKeys: string[];
  payKeys: string[];
  auth: string[];
}

async function mockCheckout(page: Page, opts: { orderFailsOnce?: boolean; payments?: ("FAILED" | "SUCCEEDED")[] } = {}) {
  const seen: Seen = { otp: [], consents: [], orderKeys: [], payKeys: [], auth: [] };
  const payments = [...(opts.payments ?? ["SUCCEEDED"])];
  let orderFails = opts.orderFailsOnce ?? false;
  await page.route("**/api/v1/**", async (route) => {
    const req = route.request();
    const url = new URL(req.url());
    const path = url.pathname;
    const auth = req.headers()["authorization"];
    if (auth) seen.auth.push(auth);
    if (path.endsWith("/quotes/q-1")) return route.fulfill({ json: QUOTE });
    if (path.endsWith("/auth/otp/request")) {
      seen.otp.push(req.postDataJSON());
      return route.fulfill({ status: 202, json: { challenge_id: "c".repeat(36), expires_in_seconds: 300, code_length: 6, dev_code: "482913" } });
    }
    if (path.endsWith("/auth/otp/verify")) {
      const { code } = req.postDataJSON() as { code: string };
      if (code !== "482913") return route.fulfill({ status: 401, json: { detail: { code: "OTP_INVALID", attempts_left: 4 } } });
      return route.fulfill({ json: { token: "v1.p_test.123.sig", token_type: "Bearer" } });
    }
    if (path.endsWith("/consents") && req.method() === "GET") return route.fulfill({ json: notGranted });
    if (path.endsWith("/consents") && req.method() === "POST") {
      const body = req.postDataJSON() as { choices: Record<string, boolean> };
      seen.consents.push(body);
      const ready = body.choices.NEEDS_ASSESSMENT && body.choices.KYC_VERIFICATION;
      return route.fulfill({ json: { ...notGranted, ready_for_checkout: ready } });
    }
    if (path.endsWith("/orders") && req.method() === "POST") {
      seen.orderKeys.push(req.headers()["idempotency-key"]);
      if (orderFails) {
        orderFails = false;
        return route.abort("connectionreset");
      }
      return route.fulfill({ status: 201, json: order() });
    }
    if (path.endsWith("/payments")) {
      seen.payKeys.push(req.headers()["idempotency-key"]);
      const status = payments.shift() ?? "SUCCEEDED";
      const ok = status === "SUCCEEDED";
      return route.fulfill({
        status: 201,
        json: {
          payment: { payment_id: `pay-${seen.payKeys.length}`, attempt: seen.payKeys.length, status, method: "upi", amount_paise: 141482, failure_code: ok ? null : "DECLINED_BY_BANK" },
          order: order({ state: ok ? "PAYMENT_CONFIRMED" : "PAYMENT_FAILED", next_action: ok ? "NONE" : "RETRY_PAYMENT" }),
        },
      });
    }
    return route.fulfill({ status: 404, json: { detail: { code: "NOT_FOUND" } } });
  });
  return seen;
}

async function signIn(page: Page) {
  await page.getByLabel("Mobile number").fill("98765 43210");
  await page.getByRole("button", { name: "Send code" }).click();
  await expect(page.getByTestId("dev-code")).toHaveText("Demo code: 482913");
  await page.getByLabel("6-digit code").fill("111111");
  await page.getByRole("button", { name: "Verify" }).click();
  await expect(page.getByRole("alert").filter({ hasText: "wrong" })).toHaveText("That code is wrong. 4 tries left.");
  await page.getByLabel("6-digit code").fill("482913");
  await page.getByRole("button", { name: "Verify" }).click();
}

test("OTP → consent (nothing pre-ticked) → order → failed payment → retry succeeds", async ({ page }) => {
  const seen = await mockCheckout(page, { payments: ["FAILED", "SUCCEEDED"] });
  await page.goto("/en/checkout?q=q-1");
  await expect(page.getByTestId("checkout-summary")).toContainText("Biz Fibre 100");
  await signIn(page);

  const form = page.getByTestId("consent");
  await expect(form).toBeVisible();
  await expect(form).toContainText("Groq");
  await expect(form).toContainText("7 days");
  const boxes = form.getByRole("checkbox");
  await expect(boxes).toHaveCount(3);
  for (const box of await boxes.all()) await expect(box).not.toBeChecked();

  // Saving without the required purposes records the choice but does not proceed.
  await form.getByRole("button", { name: "Save my choices" }).click();
  await expect(form.getByRole("status")).toHaveText("Your choices are saved.");
  await expect(page.getByRole("button", { name: "Confirm order" })).toHaveCount(0);

  await form.getByRole("checkbox", { name: /Plan recommendation/ }).check();
  await form.getByRole("checkbox", { name: /Business verification/ }).check();
  await form.getByRole("button", { name: "Save my choices" }).click();
  expect(seen.consents.at(-1)).toEqual({
    notice_version: "2026-11-07.1",
    locale: "en",
    choices: { NEEDS_ASSESSMENT: true, KYC_VERIFICATION: true, MARKETING: false },
  });

  await page.getByRole("button", { name: "Confirm order" }).click();
  const placed = page.getByTestId("order");
  await expect(placed).toContainText("Order placed");
  await expect(page.getByTestId("order-id")).toHaveText("ABCDEF12");
  await expect(page.getByTestId("due-now")).toHaveText("₹1,415");

  await page.getByRole("radio", { name: "Card" }).check();
  await page.getByRole("button", { name: "Pay ₹1,415" }).click();
  await expect(page.getByRole("alert").filter({ hasText: "didn't go through" })).toBeVisible();
  await page.getByRole("button", { name: "Try again" }).click();
  await expect(page.getByTestId("paid")).toContainText("Payment received");

  expect(seen.payKeys).toHaveLength(2);
  expect(new Set(seen.payKeys).size).toBe(2); // a retry is a new attempt
  expect(seen.auth.every((a) => a === "Bearer v1.p_test.123.sig")).toBe(true);
  expect(seen.otp).toEqual([{ phone: "98765 43210" }]);
  const stored = await page.evaluate(() => JSON.stringify(sessionStorage));
  expect(stored).not.toContain("9876543210");
  expect(stored).not.toContain("98765 43210");
});

test("a dropped connection while confirming reuses the same Idempotency-Key", async ({ page }) => {
  const seen = await mockCheckout(page, { orderFailsOnce: true });
  await page.goto("/en/checkout?q=q-1");
  await signIn(page);
  const form = page.getByTestId("consent");
  await form.getByRole("checkbox", { name: /Plan recommendation/ }).check();
  await form.getByRole("checkbox", { name: /Business verification/ }).check();
  await form.getByRole("button", { name: "Save my choices" }).click();

  await page.getByRole("button", { name: "Confirm order" }).click();
  await expect(page.getByRole("alert").filter({ hasText: "Something went wrong" })).toBeVisible();
  await page.getByRole("button", { name: "Confirm order" }).click();
  await expect(page.getByTestId("order")).toBeVisible();
  expect(seen.orderKeys).toHaveLength(2);
  expect(seen.orderKeys[0]).toBe(seen.orderKeys[1]);
  expect(seen.orderKeys[0]).toMatch(/^[0-9a-f]{32}$/);
});

test("consent notice renders in Tamil", async ({ page }) => {
  await mockCheckout(page);
  await page.goto("/ta/consent");
  await expect(page.getByRole("heading", { level: 1 })).toHaveText("உங்கள் தரவு, உங்கள் விருப்பம்");
  await expect(page.getByLabel("மொபைல் எண்")).toBeVisible();
});
