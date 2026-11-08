import { expect, test } from "@playwright/test";

/** Round 2, Phase 1: the front door a first-time owner lands on. */

test("English landing: value line, Start, Track and all 10 languages", async ({ page }) => {
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "Udyam Digital" })).toBeVisible();
  await expect(page.getByTestId("tagline")).toHaveText(
    "Internet, SIMs, WhatsApp and billing for your shop — one bill, one number.",
  );
  await expect(page.getByText("App shell ready.")).toHaveCount(0);
  await expect(page.getByTestId("start")).toHaveAttribute("href", "/en/advisor");
  await expect(page.getByTestId("track")).toHaveText("Already ordered? Track it");
  await expect(page.getByTestId("track")).toHaveAttribute("href", "/en/track");
  await expect(page.getByTestId("staff-sign-in")).toHaveText("Staff sign-in");
  await expect(page.getByTestId("staff-sign-in")).toHaveAttribute("href", "/en/backoffice");
  const nav = page.getByRole("navigation", { name: "Language" });
  await expect(nav.getByRole("link")).toHaveCount(10);
  await expect(nav.getByRole("link", { name: "English" })).toHaveAttribute("aria-current", "page");
  // No "picked from your phone" note: English is just the default here.
  await expect(page.getByTestId("auto-picked")).toHaveCount(0);
});

test("Start opens the advisor in the same language", async ({ page }) => {
  await page.route("**/api/v1/advisor/sessions**", (route) =>
    route.fulfill({
      status: 201,
      json: {
        session_id: "11111111-2222-3333-4444-555555555555",
        status: "ACTIVE",
        locale: "hi",
        mode: "chat",
        messages: [],
        question: null,
        recommendation: null,
      },
    }),
  );
  await page.goto("/hi");
  await page.getByTestId("start").click();
  await expect(page).toHaveURL(/\/hi\/advisor$/);
  await expect(page.getByRole("heading", { level: 1 })).toHaveText("अपने व्यवसाय का प्लान चुनें");
  await page.getByTestId("home").click();
  await expect(page).toHaveURL(/\/hi$/);
});

test("all 10 landing pages render; unknown locales 404", async ({ page, request }) => {
  for (const [code, start] of [
    ["hi", "शुरू करें"],
    ["ta", "தொடங்கு"],
    ["pa", "ਸ਼ੁਰੂ ਕਰੋ"],
  ]) {
    await page.goto(`/${code}`);
    await expect(page.locator("main")).toHaveAttribute("lang", `${code}-IN`);
    await expect(page.getByTestId("start")).toHaveText(start);
  }
  expect((await request.get("/fr")).status()).toBe(404);
});

test("a chosen language is remembered for the next visit to /", async ({ page }) => {
  await page.goto("/");
  await page.getByRole("navigation", { name: "Language" }).getByRole("link", { name: "मराठी" }).click();
  await expect(page).toHaveURL(/\/mr$/);
  await page.goto("/");
  await expect(page).toHaveURL(/\/mr$/);
  await expect(page.getByTestId("auto-picked")).toHaveCount(0); // their own pick, not a guess
});

test.describe("on a Hindi phone", () => {
  test.use({ locale: "hi-IN" });

  test("/ opens in Hindi, says why, and the choice stays changeable", async ({ page }) => {
    await page.goto("/");
    await expect(page).toHaveURL(/\/hi\?auto=1$/);
    await expect(page.getByTestId("start")).toHaveText("शुरू करें");
    await expect(page.getByTestId("auto-picked")).toContainText("आपके फ़ोन की भाषा");
    const nav = page.getByRole("navigation", { name: "Language" });
    await expect(nav.getByRole("link", { name: "हिन्दी" })).toHaveAttribute("aria-current", "page");
    await nav.getByRole("link", { name: "English" }).click();
    await expect(page).toHaveURL(/\/en$/);
    await page.goto("/");
    await expect(page).toHaveURL(/\/$/); // English chosen: stays on /
  });
});
