import { expect, test } from "@playwright/test";

test("app shell renders on a phone viewport", async ({ page }) => {
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "Udyam Digital" })).toBeVisible();
  await expect(page.getByTestId("start")).toBeVisible();
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

test("AC-09: pages carry the security headers and run cleanly under the CSP", async ({ page }) => {
  const violations: string[] = [];
  page.on("console", (msg) => {
    if (/Content Security Policy|Refused to/i.test(msg.text())) violations.push(msg.text());
  });
  for (const path of ["/", "/en/advisor", "/hi/backoffice"]) {
    const res = await page.goto(path);
    const headers = res!.headers();
    const csp = headers["content-security-policy"];
    expect(csp).toContain("default-src 'self'");
    expect(csp).toContain("frame-ancestors 'none'");
    expect(csp).toContain("object-src 'none'");
    expect(csp).not.toContain("unsafe-eval");
    expect(headers["strict-transport-security"]).toContain("max-age=");
    expect(headers["x-content-type-options"]).toBe("nosniff");
    expect(headers["x-frame-options"]).toBe("DENY");
    expect(headers["referrer-policy"]).toBe("strict-origin-when-cross-origin");
    expect(headers["x-powered-by"]).toBeUndefined();
    await expect(page.locator("h1").first()).toBeVisible();
  }
  expect(violations).toEqual([]);
});
