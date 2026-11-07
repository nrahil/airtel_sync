import { type Page, expect, test } from "@playwright/test";

/** Quote screen against a mocked API: toggles, cascades and one-tap fixes (E2E-03/04). */

type Line = { product_id: string; product_name: string; qty: number; unit_paise: number };

const CATALOGUE: Record<string, { name: string; price: number; category: string }> = {
  P01: { name: "Biz Fibre 100", price: 119900, category: "Connectivity" },
  P02: { name: "Biz Fibre 300", price: 179900, category: "Connectivity" },
  P03: { name: "Biz Fibre 1G", price: 449900, category: "Connectivity" },
  P07: { name: "Business Postpaid 299", price: 39900, category: "Connectivity" },
  P22: { name: "Secure Wi-Fi with Guest Network", price: 59900, category: "Security" },
  P23: { name: "Cloud CCTV", price: 29900, category: "IoT & Physical" },
};

function quote(id: string, lines: Line[], extra: Record<string, unknown> = {}) {
  const monthly = lines.reduce((s, l) => s + l.unit_paise * l.qty, 0);
  const gst = Math.round(monthly * 0.18);
  const present = new Set(lines.map((l) => l.product_id));
  return {
    quote_id: id,
    session_id: "11111111-2222-3333-4444-555555555555",
    version: 1,
    status: "ISSUED",
    locale: "en",
    created_at: "2026-07-15T10:00:00+05:30",
    valid_until: "2099-01-01T10:00:00+05:30",
    contract_months: 12,
    required_contract_months: 12,
    contract_options: [12, 24],
    lines: lines.map((l) => ({
      ...l,
      category: CATALOGUE[l.product_id].category,
      unit: "per site",
      list_unit_paise: l.unit_paise,
      discounts: [],
      line_total_paise: l.unit_paise * l.qty,
      setup_total_paise: 0,
      min_contract_months: 12,
    })),
    totals: {
      list_monthly_paise: monthly,
      savings_paise: 0,
      monthly_subtotal_paise: monthly,
      monthly_gst_paise: gst,
      monthly_total_paise: monthly + gst,
      setup_subtotal_paise: 100000,
      setup_gst_paise: 18000,
      setup_total_paise: 118000,
    },
    gst_rate_bps: 1800,
    options: Object.entries(CATALOGUE).map(([pid, p]) => ({
      product_id: pid,
      product_name: p.name,
      description: `${p.name} description`,
      category: p.category,
      unit: "per site",
      list_unit_paise: p.price,
      setup_unit_paise: 0,
      in_bundle: present.has(pid),
      qty: 1,
      eligible: pid !== "P03",
      reasons: pid === "P03" ? ["TIER_NOT_SERVED", "GSTIN_REQUIRED"] : [],
      reason_text: pid === "P03" ? "not available in your city yet" : null,
      alternative: pid === "P03" ? "P02" : null,
      alternative_name: pid === "P03" ? "Biz Fibre 300" : null,
      requires_one_of: pid === "P22" || pid === "P23" ? ["P01", "P02", "P03"] : [],
      requires_met: true,
      recommended: pid === "P23",
    })),
    ...extra,
  };
}

const line = (pid: string, qty = 1): Line => ({
  product_id: pid,
  product_name: CATALOGUE[pid].name,
  qty,
  unit_paise: CATALOGUE[pid].price,
});

const BASE = [line("P01"), line("P07"), line("P22")];

async function mockQuotes(page: Page, onPatch: (items: { product_id: string; qty: number }[]) => { status: number; json: unknown }) {
  const patches: unknown[] = [];
  await page.route("**/api/v1/quotes/**", async (route) => {
    const req = route.request();
    if (req.method() === "GET") return route.fulfill({ json: quote("q-1", BASE) });
    const body = req.postDataJSON() as { items: { product_id: string; qty: number }[] };
    patches.push(body);
    const reply = onPatch(body.items);
    return route.fulfill(reply);
  });
  return patches;
}

test("toggling an add-on re-prices from the server's quote", async ({ page }) => {
  const patches = await mockQuotes(page, (items) => ({
    status: 200,
    json: quote("q-2", items.map((i) => line(i.product_id, i.qty))),
  }));
  await page.goto("/en/quote?q=q-1");
  await expect(page.getByRole("heading", { level: 1 })).toHaveText("Your plan");
  await expect(page.getByTestId("quote-lines")).not.toContainText("Cloud CCTV");
  // GST is its own line; the total is exactly what the server sent.
  await expect(page.getByTestId("gst")).toBeVisible();
  const before = await page.getByTestId("monthly-total").textContent();

  await page.getByRole("button", { name: "Add Cloud CCTV" }).click();
  await expect(page.getByTestId("quote-lines")).toContainText("Cloud CCTV");
  await expect(page.getByTestId("monthly-total")).not.toHaveText(before ?? "");
  expect(patches.at(-1)).toEqual({
    items: [
      { product_id: "P01", qty: 1 },
      { product_id: "P07", qty: 1 },
      { product_id: "P22", qty: 1 },
      { product_id: "P23", qty: 1 },
    ],
  });
  await expect(page).toHaveURL(/q=q-2/);
});

test("E2E-04: removing Biz Fibre while CCTV is selected shows the cascade and a one-tap fix", async ({ page }) => {
  const withCctv = [...BASE, line("P23", 2)];
  const patches: { product_id: string; qty: number }[][] = [];
  await page.route("**/api/v1/quotes/**", async (route) => {
    const req = route.request();
    if (req.method() === "GET") return route.fulfill({ json: quote("q-1", withCctv) });
    const { items } = req.postDataJSON() as { items: { product_id: string; qty: number }[] };
    patches.push(items);
    if (!items.some((i) => i.product_id === "P01")) {
      return route.fulfill({
        status: 422,
        json: {
          detail: {
            code: "BUNDLE_INELIGIBLE",
            blocked: [
              { product_id: "P22", product_name: "Secure Wi-Fi with Guest Network", reasons: ["DEPENDENCY_MISSING"], reason_text: "needs a compatible internet connection", requires_one_of: ["Biz Fibre 100"] },
              { product_id: "P23", product_name: "Cloud CCTV", reasons: ["DEPENDENCY_MISSING"], reason_text: "needs a compatible internet connection", requires_one_of: ["Biz Fibre 100"] },
            ],
            fixes: [
              { action: "ADD", product_id: "P01", product_name: "Biz Fibre 100", qty: 1, replaces: null, replaces_name: null, items: [...items, { product_id: "P01", qty: 1 }] },
              { action: "REMOVE_BLOCKED", product_id: null, product_name: null, products: ["Secure Wi-Fi with Guest Network", "Cloud CCTV"], qty: null, replaces: null, replaces_name: null, items: [{ product_id: "P07", qty: 1 }] },
            ],
          },
        },
      });
    }
    return route.fulfill({ json: quote("q-3", items.map((i) => line(i.product_id, i.qty))) });
  });

  await page.goto("/en/quote?q=q-1");
  await page.getByRole("button", { name: "Remove Biz Fibre 100" }).click();

  const cascade = page.getByTestId("cascade");
  await expect(cascade).toBeVisible();
  await expect(cascade).toContainText("This change needs one more step");
  await expect(cascade).toContainText("Cloud CCTV — needs a compatible internet connection");
  // The plan itself did not change.
  await expect(page.getByTestId("quote-lines")).toContainText("Biz Fibre 100");
  await expect(page.getByRole("button", { name: /Continue to checkout/ })).toBeDisabled();
  await expect(cascade.getByRole("button", { name: "Also remove Secure Wi-Fi with Guest Network, Cloud CCTV" })).toBeVisible();

  await cascade.getByRole("button", { name: "Add Biz Fibre 100" }).click();
  await expect(cascade).toBeHidden();
  expect(patches.at(-1)?.map((i) => i.product_id).sort()).toEqual(["P01", "P07", "P22", "P23"]);
  await expect(page.getByRole("button", { name: /Continue to checkout/ })).toBeEnabled();
});

test("E2E-03: tier-2 owner sees Biz Fibre 1G blocked with the reason and Biz Fibre 300 suggested", async ({ page }) => {
  const patches = await mockQuotes(page, (items) => ({
    status: 200,
    json: quote("q-2", items.map((i) => line(i.product_id, i.qty))),
  }));
  await page.goto("/en/quote?q=q-1");
  const option = page.getByTestId("option-P03");
  await expect(option).toContainText("Not available — not available in your city yet");
  await expect(option.getByRole("button", { name: /^Add/ })).toHaveCount(0);
  await option.getByRole("button", { name: "Use Biz Fibre 300 instead" }).click();
  await expect(page.getByTestId("quote-lines")).toContainText("Biz Fibre 300");
  const sent = patches.at(-1) as { items: { product_id: string }[] };
  expect(sent.items.map((i) => i.product_id)).toContain("P02");
  expect(sent.items.map((i) => i.product_id)).not.toContain("P03");
});

test("an expired quote must be re-priced before checkout", async ({ page }) => {
  await page.route("**/api/v1/quotes/**", (route) =>
    route.fulfill({ json: quote("q-1", BASE, { valid_until: "2020-01-01T00:00:00+05:30" }) }),
  );
  let created: unknown = null;
  await page.route("**/api/v1/quotes", (route) => {
    created = route.request().postDataJSON();
    return route.fulfill({ status: 201, json: quote("q-9", BASE) });
  });
  await page.goto("/en/quote?q=q-1");
  await expect(page.getByRole("alert").filter({ hasText: "expired" })).toBeVisible();
  await expect(page.getByRole("button", { name: /Continue to checkout/ })).toBeDisabled();
  await page.getByRole("button", { name: "Get today's price" }).click();
  await expect(page).toHaveURL(/q=q-9/);
  expect(created).toMatchObject({ session_id: "11111111-2222-3333-4444-555555555555" });
});

test("quote screen renders in Hindi", async ({ page }) => {
  await mockQuotes(page, () => ({ status: 500, json: {} }));
  await page.goto("/hi/quote?q=q-1");
  await expect(page.getByRole("heading", { level: 1 })).toHaveText("आपका प्लान");
  await expect(page.locator("main")).toHaveAttribute("lang", "hi-IN");
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth);
  expect(overflow).toBe(false);
});

// ------------------------------------------------------------------ Round 2 Phase 2

const STARTER_ITEMS = [
  { product_id: "P04", qty: 1 },
  { product_id: "P07", qty: 1 },
  { product_id: "P19", qty: 1 },
];

/** The quote as the server now sends it: plans, deltas, contract prices, pay today. */
function phase2Quote(id: string, extra: Record<string, unknown> = {}) {
  const base = quote(id, BASE);
  const deltas: Record<string, { monthly_total_paise: number; setup_total_paise: number } | null> = {
    P01: null, // removing it would break Wi-Fi: the cascade, not a price
    P07: { monthly_total_paise: -47082, setup_total_paise: 0 },
    P22: { monthly_total_paise: -50062, setup_total_paise: -118000 },
    P23: { monthly_total_paise: 35282, setup_total_paise: 212400 },
  };
  return {
    ...base,
    options: base.options.map((o) => ({ ...o, delta: deltas[o.product_id] ?? null })),
    contract_prices: [
      { months: 12, monthly_total_paise: base.totals.monthly_total_paise },
      { months: 24, monthly_total_paise: base.totals.monthly_total_paise - 20621 },
    ],
    due_today_paise: base.totals.monthly_total_paise + base.totals.setup_total_paise,
    plans: [
      {
        key: "starter",
        items: STARTER_ITEMS,
        products: [
          { product_name: "Biz 5G AirFiber 100", qty: 1 },
          { product_name: "Business Postpaid 299", qty: 1 },
          { product_name: "Cloud POS & Billing", qty: 1 },
        ],
        monthly_total_paise: 282846,
        setup_total_paise: 295000,
        current: false,
        changes: [
          { kind: "swapped", from: "Biz Fibre 100", to: "Biz 5G AirFiber 100" },
          { kind: "removed", product: "Secure Wi-Fi with Guest Network" },
          { kind: "fewer", product: "Business Postpaid 299", from: 2, to: 1 },
        ],
      },
      {
        key: "recommended",
        items: BASE.map((l) => ({ product_id: l.product_id, qty: l.qty })),
        products: BASE.map((l) => ({ product_name: l.product_name, qty: l.qty })),
        monthly_total_paise: base.totals.monthly_total_paise,
        setup_total_paise: base.totals.setup_total_paise,
        current: true,
        changes: [],
      },
    ],
    ...extra,
  };
}

type Sent = { items: { product_id: string; qty: number }[]; contract_months?: number };

async function mockPhase2(page: Page) {
  const sent: Sent[] = [];
  await page.route("**/api/v1/quotes/**", async (route) => {
    const req = route.request();
    if (req.method() === "GET") return route.fulfill({ json: phase2Quote("q-1") });
    sent.push(req.postDataJSON() as Sent);
    return route.fulfill({ json: phase2Quote("q-2") });
  });
  return sent;
}

test("Starter vs Recommended: both prices, what the starter gives up, one tap to switch", async ({ page }) => {
  const sent = await mockPhase2(page);
  await page.goto("/en/quote?q=q-1");
  const plans = page.getByTestId("plans");
  await expect(plans).toContainText("Want a lower monthly bill?");
  await expect(page.getByTestId("plan-starter-price")).toHaveText("₹2,828/month");
  await expect(page.getByTestId("plan-recommended")).toContainText("Your plan now");
  const changes = page.getByTestId("plan-changes");
  await expect(changes).toContainText("Biz 5G AirFiber 100 instead of Biz Fibre 100");
  await expect(changes).toContainText("No Secure Wi-Fi with Guest Network");
  await expect(changes).toContainText("Business Postpaid 299: 1 instead of 2");
  await expect(page.locator("main")).not.toContainText(/\bP\d{2}\b/); // no product codes

  await plans.getByRole("button", { name: "Switch to Starter" }).click();
  await expect.poll(() => sent.at(-1)?.items).toEqual(STARTER_ITEMS);
});

test("each toggle shows the rupee change before you tap", async ({ page }) => {
  await mockPhase2(page);
  await page.goto("/en/quote?q=q-1");
  await expect(page.getByTestId("option-P22").getByTestId("delta")).toHaveText("Remove: −₹500/month · −₹1,180 one-time");
  await expect(page.getByTestId("option-P23").getByTestId("delta")).toHaveText("Add: +₹353/month · +₹2,124 one-time");
  await expect(page.getByRole("button", { name: "Add Cloud CCTV (+₹353/month · +₹2,124 one-time)" })).toBeVisible();
  // A removal the engine would refuse shows no price (tapping it opens the cascade).
  await expect(page.getByTestId("option-P01").getByTestId("delta")).toHaveCount(0);
});

test("what you pay today vs from next month, with GST in plain words", async ({ page }) => {
  await mockPhase2(page);
  await page.goto("/en/quote?q=q-1");
  const box = page.getByTestId("pay-box");
  // Monthly ₹2,592 + setup ₹1,180 = ₹3,772 today.
  await expect(box.getByTestId("pay-today")).toHaveText("₹3,772");
  await expect(box).toContainText("setup ₹1,180 + first month ₹2,592");
  await expect(box.getByTestId("pay-monthly")).toHaveText("₹2,592");
  await expect(box).toContainText("each month, 12-month contract");
  await expect(box.getByTestId("gst-plain")).toHaveText(
    "All prices include GST, an 18% government tax (₹395 of your monthly bill).",
  );
});

test("24 months says the saving in rupees and re-prices on tap", async ({ page }) => {
  const sent = await mockPhase2(page);
  await page.goto("/en/quote?q=q-1");
  const contract = page.getByTestId("contract");
  await expect(contract.getByRole("button", { name: /12 months/ })).toHaveAttribute("aria-pressed", "true");
  await expect(page.getByTestId("contract-saves-24")).toHaveText("save ₹206 a month");
  await contract.getByRole("button", { name: /24 months/ }).click();
  await expect.poll(() => sent.at(-1)?.contract_months).toBe(24);
});

const TOKEN = "tok_abcdefghijklmnopqrstuvwxyz012345";

test("share: a WhatsApp link to a read-only page", async ({ page }) => {
  await mockPhase2(page);
  let shared = 0;
  await page.route("**/api/v1/quotes/q-1/share", (route) => {
    shared += 1;
    return route.fulfill({ status: 201, json: { token: TOKEN, valid_until: "2099-01-01T10:00:00+05:30" } });
  });
  await page.goto("/en/quote?q=q-1");
  await page.getByRole("button", { name: "Send this price to my partner or accountant" }).click();
  await expect(page.getByTestId("share")).toContainText("They can see the price but can't change your plan.");
  const url = await page.getByTestId("share-url").inputValue();
  expect(url).toMatch(new RegExp(`/en/quote/shared\\?t=${TOKEN}$`));
  const href = (await page.getByTestId("share-whatsapp").getAttribute("href")) ?? "";
  expect(href.startsWith("https://wa.me/?text=")).toBe(true);
  expect(decodeURIComponent(href.split("text=")[1])).toBe(
    `Price for our business plan: ₹2,592/month. Details (view only): ${url}`,
  );
  expect(shared).toBe(1);
});

const SHARED = {
  valid_until: "2099-01-01T10:00:00+05:30",
  contract_months: 12,
  lines: [
    { product_name: "Biz Fibre 100", qty: 1, line_total_paise: 113905 },
    { product_name: "Business Postpaid 299", qty: 2, line_total_paise: 75810 },
  ],
  totals: {
    monthly_subtotal_paise: 189715,
    monthly_gst_paise: 34149,
    monthly_total_paise: 223864,
    setup_total_paise: 0,
    savings_paise: 0,
  },
  due_today_paise: 223864,
  gst_rate_bps: 1800,
};

test("the shared page is read-only: prices, no buttons that change anything", async ({ page }) => {
  await page.route("**/api/v1/quotes/shared/*", (route) => route.fulfill({ json: SHARED }));
  await page.goto(`/en/quote/shared?t=${TOKEN}`);
  await expect(page.getByRole("heading", { level: 1 })).toHaveText("Plan price (shared)");
  await expect(page.getByTestId("shared-monthly-total")).toHaveText("₹2,239/month");
  await expect(page.getByTestId("shared-quote")).toContainText("Business Postpaid 299 · Qty 2");
  await expect(page.getByText(/View only/)).toBeVisible();
  await expect(page.getByRole("button")).toHaveCount(0);
  await expect(page.getByRole("link", { name: "Find a plan for your own business" })).toHaveAttribute("href", "/en");
  expect(await page.locator('meta[name="robots"]').getAttribute("content")).toContain("noindex");
});

test("a shared link that has ended says why and what to do", async ({ page }) => {
  await page.route("**/api/v1/quotes/shared/*", (route) =>
    route.fulfill({ status: 410, json: { detail: { code: "SHARE_ENDED", reason: "CHANGED" } } }),
  );
  await page.goto(`/hi/quote/shared?t=${TOKEN}`);
  await expect(page.getByTestId("shared-problem")).toHaveText(
    "यह कीमत वाला लिंक अब बंद हो गया है। मालिक ने यह प्लान बदल दिया है या ऑर्डर कर दिया है। उनसे नया लिंक भेजने को कहें।",
  );
});

test("expired quote: 'Get today's price' says the price was refreshed and what it is", async ({ page }) => {
  await page.route("**/api/v1/quotes/**", (route) =>
    route.fulfill({ json: phase2Quote("q-1", { valid_until: "2020-01-01T00:00:00+05:30", expired: true }) }),
  );
  await page.route("**/api/v1/quotes", (route) => route.fulfill({ status: 201, json: phase2Quote("q-9") }));
  await page.goto("/en/quote?q=q-1");
  // While expired: no plan switch and no share (the price isn't held any more).
  await expect(page.getByTestId("plans")).toHaveCount(0);
  await expect(page.getByRole("button", { name: /Send this price/ })).toHaveCount(0);
  await page.getByRole("button", { name: "Get today's price" }).click();
  await expect(page.getByTestId("refreshed")).toHaveText("Prices refreshed. Today's price is the same: ₹2,592/month.");
  await expect(page.getByRole("button", { name: /Continue to checkout/ })).toBeEnabled();
});

test("Phase 2 quote screen in Hindi fits a phone", async ({ page }) => {
  await mockPhase2(page);
  await page.goto("/hi/quote?q=q-1");
  await expect(page.getByTestId("plans")).toContainText("महीने का बिल कम करना है?");
  await expect(page.getByTestId("pay-box")).toContainText("अगले महीने से");
  await expect(page.getByTestId("contract-saves-24")).toHaveText("हर महीने ₹206 की बचत");
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth);
  expect(overflow).toBe(false);
});

test("walkthrough fixes: discount vs 24-month saving are named apart; setup says one-time once", async ({ page }) => {
  await page.route("**/api/v1/quotes/**", (route) => {
    const q = phase2Quote("q-1");
    return route.fulfill({ json: { ...q, totals: { ...q.totals, savings_paise: 17475 } } });
  });
  await page.goto("/en/quote?q=q-1");
  await expect(page.getByTestId("bundle-discount")).toHaveText("Bundle discount: ₹175 off every month");
  await expect(page.getByTestId("contract-saves-24")).toHaveText("save ₹206 a month");
  await expect(page.getByTestId("quote")).toContainText("One-time setup (incl. GST)");
  await expect(page.getByTestId("quote")).not.toContainText("one-time, incl. GST");
});

test("walkthrough fix: the shared page says what 'Today' is made of", async ({ page }) => {
  await page.route("**/api/v1/quotes/shared/*", (route) => route.fulfill({ json: SHARED }));
  await page.goto(`/en/quote/shared?t=${TOKEN}`);
  await expect(page.getByTestId("shared-quote")).toContainText("setup ₹0 + first month ₹2,239");
});
