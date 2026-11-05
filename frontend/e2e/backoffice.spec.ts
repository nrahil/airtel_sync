import { type Page, expect, test } from "@playwright/test";

/** Back office against a mocked API: sign-in, role-based tabs, logged reveal, review decisions. */

const PERMS = {
  sales: ["handoff:read", "handoff:resolve", "leads:assign", "leads:read", "model:read", "reveal:contact", "reveal:lead", "sla:read"],
  partner: ["leads:read", "reveal:contact", "reveal:lead"],
  kyc_reviewer: ["handoff:read", "handoff:resolve", "review:decide", "review:read", "reveal:kyc"],
};

function me(role: keyof typeof PERMS) {
  return {
    staff_id: "s_test000000000000000000",
    username: role,
    role,
    partner_id: role === "partner" ? "PARTNER-A" : null,
    permissions: PERMS[role],
  };
}

const LEAD = {
  lead_id: "L00042",
  created_date: "2026-06-03",
  business_name: "S. D. C.",
  contact_name: "R. K.",
  contact_role: "Owner",
  business_type: "Dental clinic",
  segment: "prof",
  city: "Pune",
  city_tier: 1,
  lead_source: "Referral",
  preferred_language: "Marathi",
  employees: 8,
  sites: 1,
  gstin: "27ABCDX******1Z5",
  gstin_status: "ACTIVE",
  has_udyam: true,
  products_of_interest: ["P01"],
  stage_reached: "Needs assessed",
  drop_reason: "Price too high",
  assigned_partner: null,
  score: 0.31,
  factors: [
    { feature: "lead_source", value: "Referral", effect: 0.42 },
    { feature: "gstin_status", value: "ACTIVE", effect: 0.3 },
  ],
};

const ITEM = {
  item_id: 7,
  kind: "REVIEW",
  reasons: ["SIGNATORY_NOT_AUTHORISED"],
  created_at: "2026-07-15T10:00:00+05:30",
  application_id: "app-1",
  application_status: "REVIEW",
  failed_attempts: 0,
  handed_off: false,
  checks: [{ check_id: "signatory", status: "REVIEW", reason: "SIGNATORY_NOT_AUTHORISED", slot: "signatory_id", confidence: 0.9, evidence: { name: "N. A." } }],
  fixes: [],
  order_id: null,
  order_state: null,
};

async function mock(page: Page, role: keyof typeof PERMS) {
  const seen: { login: unknown[]; reveal: unknown[]; decide: unknown[] } = { login: [], reveal: [], decide: [] };
  await page.route("**/api/v1/backoffice/**", async (route) => {
    const req = route.request();
    const path = new URL(req.url()).pathname.replace("/api/v1/backoffice", "");
    if (path === "/auth/login") {
      const body = req.postDataJSON() as { password: string };
      seen.login.push(body);
      if (body.password !== "right password") {
        return route.fulfill({ status: 401, json: { detail: { code: "INVALID_CREDENTIALS" } } });
      }
      return route.fulfill({ json: { token: "s1.s_test.1.sig", expires_in_seconds: 28800, ...me(role) } });
    }
    if (path === "/me") return route.fulfill({ json: me(role) });
    if (path === "/leads") return route.fulfill({ json: { total: 1, offset: 0, limit: 25, model_available: true, leads: [LEAD] } });
    if (path === "/sessions") return route.fulfill({ json: { idle_minutes: 30, sessions: [] } });
    if (path === "/review-queue") return route.fulfill({ json: { items: seen.decide.length ? [] : [ITEM] } });
    if (path === "/reveal") {
      seen.reveal.push(req.postDataJSON());
      return route.fulfill({ json: { kind: "LEAD", ref: "L00042", fields: { contact_name: "Rohan Kulkarni", business_name: "Smile Dental Care" } } });
    }
    if (path === "/review/7") {
      seen.decide.push(req.postDataJSON());
      return route.fulfill({ json: { item_id: 7, status: "APPROVED", application_status: "PASS", orders_updated: 1 } });
    }
    return route.fulfill({ status: 404, json: { detail: { code: "NOT_FOUND" } } });
  });
  return seen;
}

async function signIn(page: Page, password = "right password") {
  await page.goto("/en/backoffice");
  await page.getByLabel("Username").fill("asha");
  await page.getByLabel("Password").fill(password);
  await page.getByRole("button", { name: "Sign in" }).click();
}

test("wrong password shows a generic message", async ({ page }) => {
  await mock(page, "sales");
  await signIn(page, "nope");
  await expect(page.getByTestId("staff-login").getByRole("alert")).toContainText("Wrong username or password");
  await expect(page.getByTestId("staff-who")).toHaveCount(0);
});

test("sales: ranked call list, masked until a reasoned, logged reveal", async ({ page }) => {
  const seen = await mock(page, "sales");
  await signIn(page);
  await expect(page.getByTestId("staff-who")).toContainText("sales · Sales");
  await expect(page.getByRole("tab")).toHaveText(["Call next", "Unclosed sessions", "KYC queue", "SLA breaches", "Model"]);

  const lead = page.getByTestId("lead");
  await expect(lead).toContainText("31%");
  await expect(lead).toContainText("S. D. C.");
  await expect(lead).toContainText("▲ lead source = Referral");
  await expect(page.getByText("Rohan Kulkarni")).toHaveCount(0);

  await lead.getByRole("button", { name: "Show details" }).click();
  await lead.getByLabel("Reason").selectOption("CALL_BACK");
  await lead.getByRole("button", { name: "Reveal (logged)" }).click();
  await expect(page.getByTestId("revealed")).toContainText("Rohan Kulkarni");
  await expect(page.getByTestId("revealed")).toContainText("This view was logged.");
  expect(seen.reveal).toEqual([{ kind: "LEAD", ref: "L00042", reason: "CALL_BACK" }]);
  // The staff token never reuses the customer's slot.
  expect(await page.evaluate(() => sessionStorage.getItem("udyam.token"))).toBeNull();
});

test("partner sees only the lead tabs and cannot assign", async ({ page }) => {
  await mock(page, "partner");
  await signIn(page);
  await expect(page.getByRole("tab")).toHaveText(["Call next", "Unclosed sessions"]);
  await expect(page.getByTestId("lead")).toBeVisible();
  await expect(page.getByRole("button", { name: "Assign" })).toHaveCount(0);
});

test("reviewer approves with a required note", async ({ page }) => {
  const seen = await mock(page, "kyc_reviewer");
  await signIn(page);
  await expect(page.getByRole("tab")).toHaveText(["KYC queue"]);
  const item = page.getByTestId("review-item");
  await expect(item).toContainText("SIGNATORY_NOT_AUTHORISED");
  const approve = item.getByRole("button", { name: "Approve" });
  await expect(approve).toBeDisabled();
  await item.getByLabel("Note (required)").fill("Board resolution names the signatory");
  await approve.click();
  await expect(item.getByRole("status")).toHaveText("Saved: PASS");
  expect(seen.decide).toEqual([{ approve: true, note: "Board resolution names the signatory" }]);
  await expect(page.getByText("Nothing here right now.")).toBeVisible();
});

test("Hindi back office", async ({ page }) => {
  await mock(page, "sales");
  await page.goto("/hi/backoffice");
  await expect(page.getByRole("heading", { name: "बैक ऑफ़िस" })).toBeVisible();
  await expect(page.getByTestId("staff-login")).toContainText("स्टाफ़ साइन-इन");
});
