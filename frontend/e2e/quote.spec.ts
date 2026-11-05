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
