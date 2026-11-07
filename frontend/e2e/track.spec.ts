import { type Page, expect, test } from "@playwright/test";

/** Order tracker against a mocked API: sign-in gate, timeline, ETAs, fixes, NOT_FEASIBLE, demo steps. */

const ID = "abcdef12-0000-4000-8000-000000000000";

type Step = { state: string; status: string; at: string | null };
const PATH = ["QUOTE_ACCEPTED", "KYC_SUBMITTED", "KYC_VERIFIED", "PAYMENT_CONFIRMED", "FEASIBILITY_CHECK", "INSTALL_SCHEDULED", "INSTALLED", "ACTIVATED"];

function steps(current: string, status = "current"): Step[] {
  const i = PATH.indexOf(current);
  return PATH.map((s, j) => ({
    state: s,
    status: j < i ? "done" : j === i ? status : "todo",
    at: j <= i ? "2026-07-15T10:00:00+05:30" : null,
  }));
}

function order(state: string, next: string, tracker: Record<string, unknown> = {}) {
  return {
    order_id: ID,
    state,
    quote_id: "q-1",
    contract_months: 12,
    monthly_total_paise: 141482,
    setup_total_paise: 0,
    amount_due_now_paise: 141482,
    lines: [
      { product_id: "P01", product_name: "Biz Fibre 100", qty: 1 },
      { product_id: "P23", product_name: "Cloud CCTV", qty: 2 },
    ],
    kyc_status: "PASS",
    next_action: next,
    payments: [],
    tracker: {
      steps: steps(state),
      history: [],
      lines: [
        { product_id: "P01", product_name: "Biz Fibre 100", qty: 1, install_days: 5, eta: "2026-07-20" },
        { product_id: "P23", product_name: "Cloud CCTV", qty: 2, install_days: 4, eta: "2026-07-19" },
      ],
      eta: "2026-07-20",
      eta_estimated: false,
      sla: { due: "2026-08-05", breached: false },
      kyc: { status: "PASS", application_id: null, handed_off: false, fixes: [] },
      alternative: null,
      refund_due_paise: 0,
      can_cancel: true,
      can_simulate: true,
      notifications: [
        { channel: "WHATSAPP", template: "STATUS_UPDATE", state: "QUOTE_ACCEPTED", status: "SENT", at: "2026-07-15T10:42:00+05:30" },
      ],
      slot: null,
      calendar: [],
      late: null,
      callback_requested: false,
      ...tracker,
    },
  };
}

async function signedIn(page: Page) {
  await page.addInitScript(() => sessionStorage.setItem("udyam.token", "v1.p_test.123.sig"));
}

async function mock(page: Page, responses: Record<string, unknown>) {
  const seen: { fulfil: unknown[]; cancel: number } = { fulfil: [], cancel: 0 };
  let current = responses.get;
  await page.route("**/api/v1/**", async (route) => {
    const req = route.request();
    const path = new URL(req.url()).pathname;
    if (path.endsWith(`/orders/${ID}/fulfilment`)) {
      seen.fulfil.push(req.postDataJSON());
      current = responses.fulfil;
      return route.fulfill({ json: current });
    }
    if (path.endsWith(`/orders/${ID}/cancel`)) {
      seen.cancel += 1;
      current = responses.cancel;
      return route.fulfill({ json: current });
    }
    if (path.endsWith(`/orders/${ID}`)) {
      if (current === "401") return route.fulfill({ status: 401, json: { detail: { code: "UNAUTHENTICATED" } } });
      if (current === "404") return route.fulfill({ status: 404, json: { detail: { code: "NOT_FOUND" } } });
      return route.fulfill({ json: current });
    }
    if (path.endsWith("/orders")) return route.fulfill({ json: responses.list });
    return route.fulfill({ status: 404, json: {} });
  });
  return seen;
}

test("tracker asks for OTP sign-in when there is no token", async ({ page }) => {
  await mock(page, { get: order("QUOTE_ACCEPTED", "KYC") });
  await page.goto(`/en/track/${ID}`);
  await expect(page.getByText("Confirm your number to see your orders")).toBeVisible();
});

test("timeline, per-line ETAs, updates and the next step", async ({ page }) => {
  await signedIn(page);
  await mock(page, { get: order("PAYMENT_CONFIRMED", "NONE") });
  await page.goto(`/en/track/${ID}`);
  await expect(page.getByTestId("order-ref")).toHaveText("Order ABCDEF12");
  await expect(page.getByTestId("order-state")).toHaveText("Payment received");
  await expect(page.getByTestId("order-eta")).toHaveText("Expected by 20 Jul");
  await expect(page.getByTestId("lines")).toContainText("Cloud CCTV × 2");
  await expect(page.getByTestId("lines")).toContainText("4 days · by 19 Jul");
  await expect(page.getByTestId("timeline").locator('[data-status="current"]')).toHaveText(/Payment received/);
  // Updates say when they were sent, to the minute (India time).
  await expect(page.getByTestId("notifications")).toHaveText("WhatsApp · 15 Jul, 10:42 — Order received");
  // What happens next, and by when.
  await expect(page.getByTestId("next-action")).toContainText("What happens next");
  await expect(page.getByTestId("next-sentence")).toHaveText(
    "We're checking that your site can get the service. Then you pick a time for the installation visit. Ready by 20 Jul.",
  );
});

test("KYC fixes are listed when a document needs work", async ({ page }) => {
  await signedIn(page);
  const fixes = [{ reason: "K06_NAME_MISMATCH", slot: "PAN", message: "Upload the PAN of the business owner." }];
  await mock(page, {
    get: order("KYC_ACTION_NEEDED", "FIX_KYC", {
      steps: steps("KYC_SUBMITTED", "attention"),
      kyc: { status: "ACTION_NEEDED", application_id: "a-1", handed_off: false, fixes },
      eta_estimated: true,
    }),
  });
  await page.goto(`/en/track/${ID}`);
  await expect(page.getByTestId("kyc-fixes")).toHaveText("Upload the PAN of the business owner.");
  await expect(page.getByTestId("timeline").locator('[data-status="attention"]')).toHaveText(/Document fix needed/);
  await expect(page.getByTestId("order-eta")).toContainText("the clock starts once your documents are verified");
});

test("a late order says sorry, gives a new date and offers a call", async ({ page }) => {
  await signedIn(page);
  const late = { sla: { due: "2026-08-05", breached: true }, late: { new_date: "2026-08-09" } };
  let called = 0;
  await mock(page, { get: order("FEASIBILITY_CHECK", "NONE", late) });
  await page.route(`**/api/v1/orders/${ID}/callback`, (route) => {
    called += 1;
    return route.fulfill({ json: order("FEASIBILITY_CHECK", "NONE", { ...late, callback_requested: true }) });
  });
  await page.goto(`/en/track/${ID}`);
  const banner = page.getByTestId("sla");
  await expect(banner).toContainText("Sorry, your order is taking longer than we promised.");
  await expect(banner.getByTestId("late-date")).toHaveText("New date: 9 Aug.");
  await banner.getByRole("button", { name: "Call me" }).click();
  await expect(banner.getByTestId("late-called")).toContainText("Someone from our team will call you soon");
  expect(called).toBe(1);
});

test("NOT_FEASIBLE offers the wireless alternative and the refund (TR-06)", async ({ page }) => {
  await signedIn(page);
  await mock(page, {
    get: order("NOT_FEASIBLE", "CHOOSE_ALTERNATIVE", {
      steps: steps("FEASIBILITY_CHECK", "done").map((s) => (s.status === "todo" ? { ...s, status: "skipped" } : s)),
      eta: null,
      can_cancel: false,
      refund_due_paise: 141482,
      alternative: {
        product_id: "P04",
        product_name: "Biz 5G AirFiber 100",
        replaces: ["P01"],
        drops: [],
        install_days: 2,
        monthly_from_paise: 89900,
        monthly_to_paise: 109900,
      },
    }),
  });
  await page.goto(`/en/track/${ID}`);
  await expect(page.getByTestId("order-state")).toHaveText("Fibre not available");
  await expect(page.getByTestId("alternative")).toContainText("Biz 5G AirFiber 100 works at your site: ₹899–₹1,099 a month");
  await expect(page.getByTestId("alternative")).toContainText("It replaces: Biz Fibre 100.");
  await expect(page.getByRole("link", { name: "Get a new plan with Biz 5G AirFiber 100" })).toHaveAttribute("href", "/en/advisor");
  await expect(page.getByTestId("refund")).toContainText("Refund of ₹1,415 started");
  await expect(page.getByRole("button", { name: "Cancel order" })).toHaveCount(0);
  await expect(page.getByTestId("demo")).toHaveCount(0);
});

test("demo step and cancellation call the API", async ({ page }) => {
  await signedIn(page);
  const seen = await mock(page, {
    get: order("PAYMENT_CONFIRMED", "NONE"),
    fulfil: order("FEASIBILITY_CHECK", "NONE"),
    cancel: order("CANCELLED", "NONE", { can_cancel: false, can_simulate: true, refund_due_paise: 141482 }),
  });
  await page.goto(`/en/track/${ID}`);
  await page.getByTestId("demo").getByRole("button", { name: "→ Site check" }).click();
  await expect(page.getByTestId("order-state")).toHaveText("Site check");
  expect(seen.fulfil).toEqual([{ state: "FEASIBILITY_CHECK" }]);

  await page.getByRole("button", { name: "Cancel order" }).click();
  await page.getByRole("button", { name: "Yes, cancel" }).click();
  await expect(page.getByTestId("order-state")).toHaveText("Cancelled");
  expect(seen.cancel).toBe(1);
});

test("someone else's order is not found", async ({ page }) => {
  await signedIn(page);
  await mock(page, { get: "404" });
  await page.goto(`/en/track/${ID}`);
  await expect(page.locator("main").getByRole("alert")).toHaveText("We couldn't find this order on your number.");
});

test("order list links to each tracker, in Hindi", async ({ page }) => {
  await signedIn(page);
  await mock(page, {
    list: { orders: [{ order_id: ID, state: "INSTALL_SCHEDULED", created_at: "2026-07-15T10:00:00+05:30", eta: "2026-07-20", products: ["Biz Fibre 100"] }] },
  });
  await page.goto("/hi/track");
  await expect(page.getByTestId("orders")).toContainText("इंस्टॉलेशन बुक");
  await expect(page.getByTestId("orders").getByRole("link")).toHaveAttribute("href", `/hi/track/${ID}`);
});

// ------------------------------------------------------------------ installation visit (Phase 4)

const CALENDAR = [
  { date: "2026-07-16", free: ["MORNING", "AFTERNOON", "EVENING"] },
  { date: "2026-07-17", free: ["AFTERNOON"] },
  { date: "2026-07-18", free: ["MORNING", "EVENING"] },
];

test("pick a time for the installation visit, then move it", async ({ page }) => {
  await signedIn(page);
  const booked: { date: string; window: string }[] = [];
  await mock(page, { get: order("INSTALL_SCHEDULED", "NONE", { calendar: CALENDAR }) });
  await page.route(`**/api/v1/orders/${ID}/slot`, (route) => {
    const body = route.request().postDataJSON() as { date: string; window: string };
    booked.push(body);
    return route.fulfill({
      json: order("INSTALL_SCHEDULED", "NONE", {
        calendar: CALENDAR,
        slot: { ...body, changes_left: 3 - booked.length + 1, can_change: true, why_not: null },
        notifications: [
          { channel: "WHATSAPP", template: "INSTALL_SLOT", state: "INSTALL_SCHEDULED", status: "SENT", at: "2026-07-15T11:05:00+05:30" },
        ],
      }),
    });
  });
  await page.goto(`/en/track/${ID}`);
  await expect(page.getByTestId("next-sentence")).toHaveText(
    "Your site can get the service. Pick a time for the installation visit.",
  );
  const picker = page.getByTestId("visit-picker");
  await expect(picker).toContainText("You can change it until the day before.");
  await picker.getByTestId("visit-day-2026-07-17").click();
  // Full windows can't be picked.
  await expect(picker.getByTestId("visit-window-MORNING")).toBeDisabled();
  await picker.getByTestId("visit-window-AFTERNOON").click();
  await picker.getByRole("button", { name: "Book Fri, 17 Jul, 2–5 pm" }).click();
  expect(booked.at(-1)).toEqual({ date: "2026-07-17", window: "AFTERNOON" });
  await expect(page.getByTestId("next-sentence")).toHaveText(
    "Please keep someone at the shop on Fri, 17 Jul, 2–5 pm. The engineer calls before coming.",
  );
  await expect(page.getByTestId("notifications")).toHaveText("WhatsApp · 15 Jul, 11:05 — Installation visit booked");

  // Move it.
  await page.getByTestId("visit-change").click();
  await page.getByTestId("visit-day-2026-07-18").click();
  await page.getByTestId("visit-window-EVENING").click();
  await page.getByTestId("visit-book").click();
  expect(booked.at(-1)).toEqual({ date: "2026-07-18", window: "EVENING" });
  await expect(page.getByTestId("next-sentence")).toContainText("Sat, 18 Jul, 5–7 pm");
});

test("a visit that can no longer move says what to do instead", async ({ page }) => {
  await signedIn(page);
  await mock(page, {
    get: order("INSTALL_SCHEDULED", "NONE", {
      slot: { date: "2026-07-15", window: "MORNING", changes_left: 3, can_change: false, why_not: "TOO_LATE_TO_CHANGE" },
    }),
  });
  await page.goto(`/en/track/${ID}`);
  await expect(page.getByTestId("visit-locked")).toHaveText(
    "The visit is today, so it can't be moved here. Ask us to call you if you need to.",
  );
  await expect(page.getByTestId("visit-change")).toHaveCount(0);
});

test("the visit picker in Hindi fits a phone", async ({ page }) => {
  await signedIn(page);
  await mock(page, { get: order("INSTALL_SCHEDULED", "NONE", { calendar: CALENDAR }) });
  await page.goto(`/hi/track/${ID}`);
  await expect(page.getByTestId("next-action")).toContainText("आगे क्या होगा");
  await page.getByTestId("visit-day-2026-07-16").click();
  await expect(page.getByTestId("visit-window-MORNING")).toHaveText("सुबह 10–दोपहर 1");
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth);
  expect(overflow).toBe(false);
});
