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

// ------------------------------------------------------------------ simpler journey

test("plan first: progress, then the plan with its assumptions and an optional fine-tune", async ({ page }) => {
  const sent: unknown[] = [];
  await mockApi(page, (body) => {
    sent.push(body);
    if (body && "locale" in body) {
      return turn({ locale: "en", mode: "form", progress: { step: 1, total: 3 } });
    }
    if (body && body.action === "refine") {
      return turn({
        locale: "en",
        mode: "form",
        progress: { step: 1, total: 3 },
        question: { slot: "num_sites", kind: "number", options: [], min: 1, max: 50 },
      });
    }
    return turn({
      locale: "en",
      status: "QUOTED",
      mode: "form",
      question: null,
      recommendation,
      progress: null,
      assumed: ["num_sites", "has_gstin", "needs"],
      can_refine: true,
    });
  });
  await page.goto("/en/advisor");
  await expect(page.getByTestId("journey-steps")).toHaveAttribute("data-step", "1");
  await expect(page.getByTestId("progress")).toHaveText("Question 1 of 3 · about 1 minute left");
  await page.getByRole("button", { name: "Professional office" }).click();

  const assumed = page.getByTestId("assumed");
  await expect(assumed).toContainText("one location");
  await expect(assumed).toContainText("GST registration not confirmed yet");
  await page.getByTestId("refine").click();
  expect(sent.at(-1)).toEqual({ action: "refine" });
  await expect(page.getByLabel("Enter a number")).toBeVisible();

  // Fine-tune questions can be skipped: "Not sure — pick for me".
  await page.getByTestId("not-sure").click();
  expect(sent.at(-1)).toEqual({ slot: "num_sites", unsure: true });
});

test("needs: suggestions for this kind of business come first, pre-ticked; the rest behind 'see all'", async ({ page }) => {
  const sent: unknown[] = [];
  await mockApi(page, (body) => {
    sent.push(body);
    if (body && "locale" in body) {
      return turn({
        locale: "en",
        mode: "form",
        question: {
          slot: "needs",
          kind: "multi",
          options: ["internet", "wifi", "website", "failover", "static_ip"],
          min: null,
          max: null,
          suggested: ["wifi", "website"],
        },
      });
    }
    return turn({ locale: "en", mode: "form", status: "QUOTED", question: null, recommendation });
  });
  await page.goto("/en/advisor");
  const suggested = page.getByTestId("suggested-needs");
  await expect(suggested.getByRole("button", { pressed: true })).toHaveCount(2);
  await expect(suggested).toContainText("Wi-Fi for staff and customers");
  await expect(page.getByRole("button", { name: /Fixed internet address/ })).toHaveCount(0);
  await page.getByRole("button", { name: "See all options" }).click();
  await expect(page.getByRole("button", { name: /Fixed internet address/ })).toBeVisible();
  await suggested.getByRole("button", { name: /A simple website/ }).click(); // untick one
  await page.getByTestId("form-needs").getByRole("button", { name: "Next", exact: true }).click();
  expect(sent.at(-1)).toEqual({ slot: "needs", value: ["wifi"] });
});

test("resume link (?s=) picks the session back up", async ({ page }) => {
  let resumed = false;
  await page.route("**/api/v1/advisor/sessions/abc-123", async (route) => {
    resumed = route.request().method() === "GET";
    await route.fulfill({
      json: turn({
        session_id: "abc-123",
        locale: "en",
        mode: "form",
        messages: [{ role: "assistant", kind: "notice", text: "Welcome back! Let's carry on where you left off." }],
        question: { slot: "city", kind: "text", options: [], min: null, max: null },
        progress: { step: 2, total: 3 },
      }),
    });
  });
  await page.goto("/en/advisor?s=abc-123");
  await expect(page.getByTestId("transcript")).toContainText("Welcome back!");
  await expect(page.getByTestId("progress")).toHaveText("Question 2 of 3 · about 1 minute left");
  expect(resumed).toBe(true);
  expect(new URL(page.url()).search).toBe("");
});

test("help: 'call me' confirms the number first, then asks for the call", async ({ page }) => {
  const help: unknown[] = [];
  await mockApi(page, () => turn({ locale: "en", mode: "form" }));
  await page.route("**/api/v1/auth/otp/request", (route) =>
    route.fulfill({ status: 202, json: { challenge_id: "c1", expires_in: 300, dev_code: "123456" } }),
  );
  await page.route("**/api/v1/auth/otp/verify", (route) =>
    route.fulfill({ json: { token: "v1.p_test.123.sig", expires_in: 3600 } }),
  );
  await page.route("**/api/v1/advisor/sessions/*/help", async (route) => {
    help.push({ body: route.request().postDataJSON(), auth: route.request().headers()["authorization"] });
    await route.fulfill({ json: { kind: "callback", status: "REQUESTED" } });
  });
  await page.goto("/en/advisor");
  const panel = page.getByTestId("help-panel");
  await panel.getByRole("button", { name: "Have someone call me" }).click();
  await expect(panel).toContainText("used only for this");
  await panel.getByLabel("Mobile number").fill("9876543210");
  await panel.getByRole("button", { name: "Send code" }).click();
  await panel.getByLabel("6-digit code").fill("123456");
  await panel.getByRole("button", { name: "Verify" }).click();
  await expect(page.getByTestId("help-done")).toContainText("will call you soon");
  expect(help).toEqual([{ body: { kind: "callback", agree: true }, auth: "Bearer v1.p_test.123.sig" }]);
});

test("the plan card lists the suggested extras by name", async ({ page }) => {
  await mockApi(page, () =>
    turn({
      locale: "en",
      status: "QUOTED",
      mode: "form",
      question: null,
      recommendation: {
        ...recommendation,
        addons: [{ product_id: "P22", product_name: "Secure Wi-Fi with Guest Network", qty: 1, source: "NEED" }],
      },
      assumed: [],
      can_refine: false,
    }),
  );
  await page.goto("/en/advisor");
  await expect(page.getByTestId("suggested-extras")).toContainText("Secure Wi-Fi with Guest Network");
  await expect(page.getByTestId("refine")).toHaveCount(0);
});

// ------------------------------------------------------------------ round 2, phase 1

const why = {
  P01: "Your main internet connection — one for each location (you have 1).",
  P07: "You said 3 staff → 1 SIMs, one for every 3 people.",
};

test("every product on the plan says why it is there", async ({ page }) => {
  await mockApi(page, () =>
    turn({ locale: "en", status: "QUOTED", question: null, recommendation: { ...recommendation, why }, assumed: [] }),
  );
  await page.goto("/en/advisor");
  for (const line of recommendation.pricing.lines) {
    await expect(page.getByTestId(`why-${line.product_id}`)).toHaveText(why[line.product_id as keyof typeof why]);
  }
});

test("progress on the last question says it is the last one", async ({ page }) => {
  await mockApi(page, () =>
    turn({
      locale: "en",
      mode: "form",
      progress: { step: 3, total: 3 },
      question: { slot: "employee_count", kind: "number", options: [], min: 1, max: 500 },
    }),
  );
  await page.goto("/en/advisor");
  await expect(page.getByTestId("progress")).toHaveText("Question 3 of 3 · last question");
});

const sixStaff = {
  ...recommendation,
  why,
  pricing: {
    ...recommendation.pricing,
    lines: [
      recommendation.pricing.lines[0],
      { ...recommendation.pricing.lines[1], qty: 4, line_total_paise: 119600 },
    ],
    totals: { ...recommendation.pricing.totals, monthly_total_paise: 12415800 },
  },
};

test("edit after the plan (typed): the plan updates and the change is highlighted", async ({ page }) => {
  const sent: unknown[] = [];
  await mockApi(page, (body) => {
    sent.push(body);
    if (body && "locale" in body) {
      return turn({ locale: "en", status: "QUOTED", question: null, recommendation: { ...recommendation, why } });
    }
    return turn({
      locale: "en",
      status: "QUOTED",
      question: null,
      messages: [{ role: "assistant", kind: "notice", text: "I've updated your plan — the changes are highlighted below." }],
      recommendation: sixStaff,
    });
  });
  await page.goto("/en/advisor");
  await expect(page.getByTestId("recommendation")).toBeVisible();
  await expect(page.getByTestId("plan-changes")).toHaveCount(0);

  await page.getByPlaceholder("Type a change…").fill("actually 6 staff");
  await page.getByRole("button", { name: "Send" }).click();
  expect(sent.at(-1)).toEqual({ text: "actually 6 staff" });

  const changes = page.getByTestId("plan-changes");
  await expect(changes).toContainText("Business Mobile Basic: 2 → 4");
  await expect(changes).toContainText("Monthly total: ₹1,23,456 → ₹1,24,158");
  await expect(page.getByTestId("line-P07")).toHaveAttribute("data-changed", "true");
  await expect(page.getByTestId("line-P01")).not.toHaveAttribute("data-changed", "true");
});

test("edit after the plan (tapped, works with the guided form): change the staff count", async ({ page }) => {
  const sent: unknown[] = [];
  await mockApi(page, (body) => {
    sent.push(body);
    if (body && "locale" in body) {
      return turn({ locale: "en", mode: "form", status: "QUOTED", question: null, recommendation });
    }
    return turn({ locale: "en", mode: "form", status: "QUOTED", question: null, recommendation: sixStaff });
  });
  await page.goto("/en/advisor");
  // Guided form (Groq off): no free-text box, but every answer can still be changed.
  await expect(page.getByPlaceholder("Type a change…")).toHaveCount(0);
  await page.getByTestId("edit-employee_count").click();
  await page.getByLabel("Enter a number").fill("6");
  await page.getByRole("button", { name: "Next" }).click();
  expect(sent.at(-1)).toEqual({ slot: "employee_count", value: 6 });
  await expect(page.getByTestId("plan-changes")).toContainText("Business Mobile Basic: 2 → 4");
  await expect(page.getByTestId("edit-employee_count")).toBeVisible(); // editor closed again
});

test("starts one session only (the greeting is never shown twice)", async ({ page }) => {
  let starts = 0;
  await mockApi(page, (body) => {
    if (body && "locale" in body) {
      starts += 1;
      return turn({
        locale: "en",
        messages: [{ role: "assistant", kind: "notice", text: "Hello! A few quick questions." }],
      });
    }
    return turn({ locale: "en" });
  });
  await page.goto("/en/advisor");
  await expect(page.getByTestId("transcript").locator("li")).toHaveCount(1);
  await page.waitForTimeout(500);
  await expect(page.getByTestId("transcript").locator("li")).toHaveCount(1);
  expect(starts).toBe(1);
});

/** A stand-in for the browser's speech recogniser: says `heard`, or fails with `error`. */
async function fakeSpeech(page: Page, outcome: { heard?: string; error?: string }) {
  await page.addInitScript((o) => {
    class FakeRecognition {
      lang = "";
      interimResults = false;
      maxAlternatives = 1;
      onresult: ((e: unknown) => void) | null = null;
      onend: (() => void) | null = null;
      onerror: ((e: { error: string }) => void) | null = null;
      start() {
        (window as unknown as { __speechLang: string }).__speechLang = this.lang;
        setTimeout(() => {
          if (o.error) this.onerror?.({ error: o.error });
          else this.onresult?.({ results: [[{ transcript: o.heard }]] });
          this.onend?.();
        }, 50);
      }
      stop() {
        this.onend?.();
      }
    }
    // Newer Chrome has the unprefixed name too; the app prefers it.
    const w = window as unknown as { SpeechRecognition: unknown; webkitSpeechRecognition: unknown };
    w.SpeechRecognition = FakeRecognition;
    w.webkitSpeechRecognition = FakeRecognition;
  }, outcome);
}

test("Speak: what the owner says lands in the box, in their language", async ({ page }) => {
  await fakeSpeech(page, { heard: "मेरी किराने की दुकान है" });
  await mockApi(page, () => turn({}));
  await page.goto("/hi/advisor");
  await page.getByRole("button", { name: "बोलें" }).click();
  await expect(page.locator("#advisor-input")).toHaveValue("मेरी किराने की दुकान है");
  expect(await page.evaluate(() => (window as unknown as { __speechLang: string }).__speechLang)).toBe("hi-IN");
  await expect(page.getByTestId("voice-error")).toHaveCount(0);
});

test("Speak: a blocked microphone says how to fix it instead of failing silently", async ({ page }) => {
  await fakeSpeech(page, { error: "not-allowed" });
  await mockApi(page, () => turn({ locale: "en" }));
  await page.goto("/en/advisor");
  await page.getByRole("button", { name: "Speak" }).click();
  await expect(page.getByTestId("voice-error")).toContainText("The microphone is blocked");
  await expect(page.getByRole("button", { name: "Speak" })).toBeVisible(); // back to idle, can retry
});

test("the microphone is allowed for this site (Permissions-Policy)", async ({ request }) => {
  const res = await request.get("/en/advisor");
  expect(res.headers()["permissions-policy"]).toContain("microphone=(self)");
});


// ------------------------------------------------------------------ Round 2 Phase 6: spoken replies

/** A stand-in for the browser's text-to-speech: records what would be said, and in which voice. */
async function fakeTts(page: Page, voices: { lang: string; name: string }[]) {
  await page.addInitScript((list) => {
    const spoken: { text: string; lang: string }[] = [];
    (window as unknown as { __spoken: typeof spoken }).__spoken = spoken;
    class Utterance {
      text: string;
      lang = "";
      rate = 1;
      voice: { lang: string } | null = null;
      constructor(text: string) {
        this.text = text;
      }
    }
    const synth = {
      getVoices: () => list,
      speak: (u: Utterance) => spoken.push({ text: u.text, lang: u.voice?.lang ?? u.lang }),
      cancel: () => {
        (window as unknown as { __cancelled: number }).__cancelled =
          ((window as unknown as { __cancelled?: number }).__cancelled ?? 0) + 1;
      },
      addEventListener: () => {},
      removeEventListener: () => {},
    };
    Object.defineProperty(window, "speechSynthesis", { value: synth, configurable: true });
    (window as unknown as { SpeechSynthesisUtterance: unknown }).SpeechSynthesisUtterance = Utterance;
  }, voices);
}

const spoken = (page: Page) =>
  page.evaluate(() => (window as unknown as { __spoken: { text: string; lang: string }[] }).__spoken);

test("Speaker: replies are read aloud in Hindi once turned on, and it is remembered", async ({ page }) => {
  await fakeTts(page, [
    { lang: "en-US", name: "English" },
    { lang: "hi-IN", name: "Hindi" },
  ]);
  await mockApi(page, (body) => {
    if (body?.text) {
      return turn({ messages: [{ role: "assistant", kind: "question", text: "आपकी दुकान किस शहर में है?" }] });
    }
    return turn({ messages: [{ role: "assistant", kind: "question", text: "आपका व्यवसाय किस तरह का है?" }] });
  });
  await page.goto("/hi/advisor");
  await expect(page.getByTestId("transcript")).toContainText("आपका व्यवसाय किस तरह का है?");
  expect(await spoken(page)).toEqual([]); // off until the owner asks

  const toggle = page.getByTestId("speaker");
  await expect(toggle).toHaveText(/जवाब बोलकर सुनाएँ/);
  await toggle.click();
  await expect(toggle).toHaveAttribute("aria-pressed", "true");
  await expect(toggle).toHaveText(/बोलना बंद करें/);
  // Turning it on reads the question on screen, in a Hindi voice.
  expect(await spoken(page)).toEqual([{ text: "आपका व्यवसाय किस तरह का है?", lang: "hi-IN" }]);

  await page.locator("#advisor-input").fill("किराना");
  await page.getByRole("button", { name: "भेजें", exact: true }).click();
  await expect(page.getByTestId("transcript")).toContainText("आपकी दुकान किस शहर में है?");
  expect((await spoken(page)).at(-1)).toEqual({ text: "आपकी दुकान किस शहर में है?", lang: "hi-IN" });
  await expect(page.getByTestId("speaker-no-voice")).toHaveCount(0);

  await page.reload();
  await expect(page.getByTestId("speaker")).toHaveAttribute("aria-pressed", "true");
});

test("Speaker: with no voice for the language it says so instead of reading gibberish", async ({ page }) => {
  await fakeTts(page, [{ lang: "en-US", name: "English" }]);
  await mockApi(page, () => turn({ messages: [{ role: "assistant", kind: "question", text: "आपका व्यवसाय किस तरह का है?" }] }));
  await page.goto("/hi/advisor");
  await page.getByTestId("speaker").click();
  await expect(page.getByTestId("speaker-no-voice")).toContainText("इस फ़ोन में हिंदी आवाज़ नहीं है");
  expect(await spoken(page)).toEqual([]);
});

test("Speaker: hidden where the browser can't speak", async ({ page }) => {
  await page.addInitScript(() => {
    Object.defineProperty(window, "speechSynthesis", { value: undefined, configurable: true });
  });
  await mockApi(page, () => turn({}));
  await page.goto("/en/advisor");
  await expect(page.getByRole("button", { name: "Send" })).toBeVisible();
  await expect(page.getByTestId("speaker")).toHaveCount(0);
});
