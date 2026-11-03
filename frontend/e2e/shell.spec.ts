import { expect, test } from "@playwright/test";

test("app shell renders on a phone viewport", async ({ page }) => {
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "Udyam Digital" })).toBeVisible();
  await expect(page.getByTestId("shell-status")).toBeVisible();
  // Mobile-first: no horizontal overflow.
  const overflow = await page.evaluate(
    () => document.documentElement.scrollWidth > window.innerWidth,
  );
  expect(overflow).toBe(false);
});

test("PWA manifest is linked and installable-shaped", async ({ page, request }) => {
  await page.goto("/");
  const href = await page.locator('link[rel="manifest"]').getAttribute("href");
  expect(href).toBeTruthy();

  const res = await request.get(href!);
  expect(res.ok()).toBe(true);
  const manifest = await res.json();
  expect(manifest).toMatchObject({ name: "Udyam Digital", start_url: "/", display: "standalone" });
  expect(manifest.icons.length).toBeGreaterThan(0);
});

test("service worker is served uncached", async ({ request }) => {
  const res = await request.get("/sw.js");
  expect(res.ok()).toBe(true);
  expect(res.headers()["cache-control"]).toContain("no-store");
});
