import { type Page, type Route, expect, test } from "@playwright/test";

/** Advisor UI against a mocked API: rendering rules, not backend logic (that is pytest's job). */

const API = "**/api/v1/advisor/sessions**";

const segmentQuestion = {
  slot: "segment",
  kind: "choice",
  options: ["micro", "prof", "multi", "mfg"],
  min: null,
  max: null,
};

function turn(overrides: Record<string, unknown>) {
  return {
    session_id: "11111111-2222-3333-4444-555555555555",
    status: "ACTIVE",
    locale: "hi",
    mode: "chat",
    messages: [],
    question: segmentQuestion,
    recommendation: null,
    ...overrides,
  };
}

const recommendation = {
  pricing: {
    contract_months: 12,
    lines: [
      { product_id: "P01", product_name: "Biz Fibre 100", qty: 1, unit_paise: 99900, line_total_paise: 99900, setup_total_paise: 0 },
      { product_id: "P07", product_name: "Business Mobile Basic", qty: 2, unit_paise: 29900, line_total_paise: 59800, setup_total_paise: 0 },
    ],
    totals: {
      monthly_subtotal_paise: 10462250,
      monthly_gst_paise: 1883205,
      monthly_total_paise: 12345600,
      setup_total_paise: 150000,
    },
  },
  blocked: [
    {
      product_id: "P15",
      product_name: "WhatsApp Business API",
      reasons: ["GSTIN_REQUIRED"],
      reason_text: "इसके लिए GST रजिस्ट्रेशन चाहिए",
      alternative: null,
    },
  ],
  explanation: { text: "<b>Biz Fibre 100</b> आपकी दुकान को ऑनलाइन रखता है।", source: "template" },
};

async function mockApi(page: Page, handler: (body: Record<string, unknown> | null, route: Route) => unknown) {
  await page.route(API, async (route) => {
    const body = route.request().postDataJSON() as Record<string, unknown> | null;
    const reply = handler(body, route);
    if (reply === undefined) return;
    await route.fulfill({ json: reply, status: route.request().url().endsWith("/sessions") ? 201 : 200 });
  });
}

test("Hindi advisor: quick options lead to a recommendation priced from data", async ({ page }) => {
  const sent: unknown[] = [];
  await mockApi(page, (body) => {
    sent.push(body);
    if (body && "locale" in body) {
      return turn({ messages: [{ role: "assistant", kind: "question", text: "आपका व्यवसाय किस तरह का है?" }] });
    }
    return turn({
      status: "QUOTED",
      question: null,
      messages: [{ role: "assistant", kind: "notice", text: "यह प्लान मैं सुझाता हूँ।" }],
      recommendation,
    });
  });

  await page.goto("/hi/advisor");
  await expect(page.getByRole("heading", { name: "अपने व्यवसाय का प्लान चुनें" })).toBeVisible();
  await expect(page.locator("main")).toHaveAttribute("lang", "hi-IN");
  await expect(page.getByTestId("transcript")).toContainText("आपका व्यवसाय किस तरह का है?");

  await page.getByRole("button", { name: "आसान विकल्पों से जवाब दें" }).click();
  await page.getByRole("button", { name: "दुकान या स्थानीय सेवा" }).click();

  const card = page.getByTestId("recommendation");
  await expect(card).toBeVisible();
  // Indian grouping, from integer paise: 12345600 paise → ₹1,23,456.
  await expect(page.getByTestId("monthly-total")).toContainText("₹1,23,456");
  await expect(page.getByTestId("setup-total")).toHaveText("₹1,500");
  await expect(card).toContainText("Business Mobile Basic");
  await expect(page.getByTestId("blocked")).toContainText("इसके लिए GST रजिस्ट्रेशन चाहिए");
  // Explanation is plain text: markup is shown literally, never rendered.
  await expect(page.getByTestId("explanation")).toContainText("<b>Biz Fibre 100</b>");
  await expect(page.getByTestId("explanation").locator("b")).toHaveCount(0);

  expect(sent).toEqual([
    { locale: "hi", mode: "chat" },
    { slot: "segment", value: "micro" },
  ]);
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth);
  expect(overflow).toBe(false);
});

test("free text is sent; the owner's words are not kept in sessionStorage", async ({ page }) => {
  await mockApi(page, (body) => {
    if (body && "locale" in body) return turn({ locale: "en" });
    return turn({
      locale: "en",
      messages: [{ role: "assistant", kind: "question", text: "Which city is your business in?" }],
      question: { slot: "city", kind: "text", options: [], min: null, max: null, suggestions: ["Surat"] },
    });
  });
  await page.goto("/en/advisor");
  await page.getByLabel("Tell me about your business…").fill("Kirana shop, call me 9876543210");
  await page.getByRole("button", { name: "Send" }).click();
  await expect(page.getByTestId("transcript")).toContainText("Which city is your business in?");
  await expect(page.getByTestId("transcript")).toContainText("Kirana shop");
  const stored = await page.evaluate(() => sessionStorage.getItem("udyam.advisor.en") ?? "");
  expect(stored).toContain("Which city is your business in?");
  expect(stored).not.toContain("9876543210");
  expect(stored).not.toContain("Kirana");
});

test("form mode (Groq down): number input validates before sending", async ({ page }) => {
  const sent: unknown[] = [];
  await mockApi(page, (body) => {
    sent.push(body);
    if (body && "locale" in body) {
      return turn({ locale: "en", mode: "form", question: { slot: "employee_count", kind: "number", options: [], min: 1, max: 500 } });
    }
    return turn({ locale: "en", mode: "form", question: { slot: "has_gstin", kind: "yes_no", options: [], min: null, max: null } });
  });
  await page.goto("/en/advisor");
  const input = page.getByLabel("Enter a number");
  const next = page.getByRole("button", { name: "Next" });
  await input.fill("0");
  await expect(next).toBeDisabled();
  await input.fill("501");
  await expect(next).toBeDisabled();
  await input.fill("8");
  await next.click();
  await expect(page.getByRole("button", { name: "Yes" })).toBeVisible();
  expect(sent.at(-1)).toEqual({ slot: "employee_count", value: 8 });
});

test("an API failure shows a retryable error, not a blank page", async ({ page }) => {
  await page.route(API, (route) => route.fulfill({ status: 503, json: { code: "DOWN" } }));
  await page.goto("/ta/advisor");
  // (Next.js also renders an empty route-announcer alert, hence the text filter.)
  await expect(page.getByRole("alert").filter({ hasText: "ஏதோ" })).toHaveText(
    "ஏதோ தவறு நடந்தது. மீண்டும் முயற்சிக்கவும்.",
  );
  await expect(page.getByRole("button", { name: "மீண்டும் தொடங்கு" })).toBeVisible();
});

test("all 10 languages render and unknown locales 404", async ({ page, request }) => {
  await page.route(API, (route) => route.fulfill({ status: 201, json: turn({}) }));
  for (const [code, title] of [
    ["mr", "तुमच्या व्यवसायासाठी प्लॅन शोधा"],
    ["bn", "আপনার ব্যবসার জন্য প্ল্যান খুঁজুন"],
    ["ml", "നിങ്ങളുടെ ബിസിനസിനുള്ള പ്ലാൻ കണ്ടെത്തൂ"],
  ]) {
    await page.goto(`/${code}/advisor`);
    await expect(page.getByRole("heading", { level: 1 })).toHaveText(title);
  }
  const nav = page.getByRole("navigation", { name: "Language" });
  await expect(nav.getByRole("link")).toHaveCount(10);
  expect((await request.get("/fr/advisor")).status()).toBe(404);
});
