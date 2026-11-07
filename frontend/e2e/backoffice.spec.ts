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
    if (path === "/sla-breaches") {
      return route.fulfill({
        json: {
          orders: [
            {
              order_id: "e1149764-0000-4000-8000-000000000000",
              state: "INSTALL_SCHEDULED",
              created_at: "2026-06-23T10:00:00+05:30",
              age_days: 22,
              sla_due: "2026-07-14",
              eta: "2026-07-20",
              alerted_at: "2026-07-15T10:00:00+05:30",
              callback_requested_at: "2026-07-15T10:05:00+05:30",
              products: ["Biz Fibre 100"],
            },
          ],
        },
      });
    }
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
  await expect(lead).toContainText("Came via Referral"); // plain words, not codes (Phase 6)
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

test("SLA breaches flag a late order whose owner asked for a call", async ({ page }) => {
  await mock(page, "sales");
  await signIn(page);
  await page.getByRole("tab", { name: "SLA breaches" }).click();
  const row = page.getByTestId("sla-order");
  await expect(row).toContainText("Day 22");
  await expect(row.getByTestId("sla-call-me")).toHaveText("Owner asked us to call — call first");
});


// ------------------------------------------------------------------ Round 2 Phase 6: a back office that says why

const APP_SESSION = {
  session_id: "5e5510n0-0000-4000-8000-000000000001",
  status: "CHECKOUT",
  stall_reason: "KYC_ACTION_NEEDED",
  at_question: null,
  idle_minutes: 95,
  updated_at: "2026-07-15T09:00:00+05:30",
  locale: "hi",
  turns: 4,
  profile: { business_type: "Kirana", segment: "micro", city: "Surat", employee_count: 4 },
  monthly_total_paise: 347100,
  order_id: "o-1",
  order_state: "KYC_ACTION_NEEDED",
  contactable: true,
  callback_requested_at: null,
  stuck_at: "KYC_ACTION_NEEDED",
  detail: {
    kyc_fixes: ["Your electricity bill is dated 31 Oct 2025, more than 90 days ago. Please upload a bill dated on or after 16 Apr 2026."],
    kyc_attempts: 2,
  },
  last_call: null,
  assigned_partner: null,
  score: 0.44,
  factors: [{ feature: "journey_stage", value: "Quote accepted", effect: 2.23 }],
  basis: { stage: "Quote accepted", segment: "micro", rate: 0.44, n: 325 },
};

const CALLBACK_SESSION = {
  ...APP_SESSION,
  session_id: "5e5510n0-0000-4000-8000-000000000002",
  stall_reason: "CALLBACK_REQUESTED",
  stuck_at: "QUOTE_NOT_ACCEPTED",
  callback_requested_at: "2026-07-15T10:30:00+05:30",
  order_id: null,
  order_state: null,
  detail: {},
  score: 0.05,
  factors: [],
};

async function mockPhase6(page: Page) {
  const calls: unknown[] = [];
  let leads = [LEAD];
  await page.route("**/api/v1/backoffice/**", async (route) => {
    const req = route.request();
    const url = new URL(req.url());
    const path = url.pathname.replace("/api/v1/backoffice", "");
    if (path === "/me") return route.fulfill({ json: me("sales") });
    if (path === "/leads") {
      return route.fulfill({
        json: { total: leads.length, offset: 0, limit: 25, model_available: true, leads, sessions: [APP_SESSION, CALLBACK_SESSION], lang: url.searchParams.get("lang") },
      });
    }
    if (path === "/calls") {
      const body = req.postDataJSON() as { outcome: string };
      calls.push(body);
      const closed = body.outcome === "NOT_INTERESTED";
      if (closed) leads = [];
      else leads = [{ ...LEAD, last_call: { outcome: body.outcome, at: "2026-07-15T11:00:00+05:30", by: "sales" } } as typeof LEAD];
      return route.fulfill({ json: { kind: "LEAD", ref: LEAD.lead_id, last_call: null, closed, callback_cleared: false } });
    }
    return route.fulfill({ status: 404, json: { detail: { code: "NOT_FOUND" } } });
  });
  return calls;
}

test("one call list: callbacks first, then leads and app journeys by likelihood, in plain words", async ({ page }) => {
  await page.addInitScript(() => sessionStorage.setItem("udyam.staff", "s1.s_test.1.sig"));
  await mockPhase6(page);
  await page.goto("/en/backoffice");
  const list = page.getByTestId("leads");
  await expect(list).toContainText("1 leads + 2 from the app, best first");

  // Order: the "call me" owner (5%) first, then by score: app journey 44%, lead 31%.
  const cards = list.locator('[data-testid="lead"], [data-testid="session"]');
  await expect(cards).toHaveCount(3);
  await expect(cards.nth(0)).toHaveAttribute("data-callback", "true");
  await expect(cards.nth(0)).toContainText("Asked us to call them — call first");
  await expect(cards.nth(0)).toContainText("Before that: Opened the quote, didn't accept");
  await expect(cards.nth(1)).toContainText("44%");
  await expect(cards.nth(1)).toContainText("From the app");
  await expect(cards.nth(2)).toContainText("31%");

  // What exactly went wrong, not a code.
  const app = cards.nth(1);
  await expect(app.getByTestId("stall")).toHaveText("KYC needs a fix");
  await expect(app.getByTestId("kyc-detail")).toContainText("Your electricity bill is dated 31 Oct 2025");
  await expect(app.getByTestId("kyc-detail")).toContainText("2 failed tries so far");
  await expect(app.getByTestId("saw-price")).toHaveText("Last price they saw: ₹3,471/month");
  await expect(app.getByTestId("factors")).toContainText("Placed an order");
  await expect(app.getByTestId("basis")).toHaveText("Of 325 businesses like this that got this far, 44% went live");

  // Score reasons in words a caller would say.
  const lead = cards.nth(2);
  await expect(lead.getByTestId("factors")).toContainText("Came via Referral");
  await expect(lead.getByTestId("factors")).toContainText("GSTIN active");
  await expect(lead.getByTestId("factors")).not.toContainText("lead_source");
  await expect(lead.getByTestId("factors")).not.toContainText("=");
});

test("logging a call: shown on the row, and 'not interested' takes it off the list", async ({ page }) => {
  await page.addInitScript(() => sessionStorage.setItem("udyam.staff", "s1.s_test.1.sig"));
  const calls = await mockPhase6(page);
  await page.goto("/en/backoffice");
  const lead = page.getByTestId("lead");
  const log = lead.getByTestId("call-log");
  await log.getByLabel("How did the call go?").selectOption("NO_ANSWER");
  await log.getByRole("button", { name: "Log call" }).click();
  expect(calls.at(-1)).toEqual({ kind: "LEAD", ref: "L00042", outcome: "NO_ANSWER" });
  await expect(lead.getByTestId("last-call")).toContainText("Last call: No answer");
  await expect(lead.getByTestId("last-call")).toContainText("sales");

  await lead.getByTestId("call-log").getByLabel("How did the call go?").selectOption("NOT_INTERESTED");
  await lead.getByTestId("call-log").getByRole("button", { name: "Log call" }).click();
  await expect(page.getByTestId("lead")).toHaveCount(0);
});

test("Hindi back office asks for Hindi KYC fixes and says the factors in Hindi", async ({ page }) => {
  await page.addInitScript(() => sessionStorage.setItem("udyam.staff", "s1.s_test.1.sig"));
  await mockPhase6(page);
  const asked = page.waitForRequest((r) => r.url().includes("/backoffice/leads"));
  await page.goto("/hi/backoffice");
  expect(new URL((await asked).url()).searchParams.get("lang")).toBe("hi");
  await expect(page.getByTestId("lead").getByTestId("factors")).toContainText("GSTIN चालू");
  await expect(page.getByTestId("session").first()).toContainText("कॉल करने को कहा — पहले कॉल करें");
});
