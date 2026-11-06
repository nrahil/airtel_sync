/** Typed client for the backend API (`/api/v1`). Money is always integer paise. */

export const API_BASE = process.env.NEXT_PUBLIC_API_BASE ?? "http://localhost:8000";

export type InputKind = "choice" | "multi" | "number" | "text" | "yes_no";

export interface Question {
  slot: string;
  kind: InputKind;
  options: string[];
  min: number | null;
  max: number | null;
  suggestions?: string[];
  /** For `needs`: what businesses of this kind usually add (shown first, pre-ticked). */
  suggested?: string[];
}

/** Fine-tune questions: the owner may answer "Not sure — pick for me". */
export const REFINE_SLOTS = ["num_sites", "has_gstin", "needs"] as const;

export interface AssistantMessage {
  role: "assistant";
  kind: "notice" | "question" | "explanation";
  text: string;
}

export interface PricedLine {
  product_id: string;
  product_name: string;
  qty: number;
  unit_paise: number;
  line_total_paise: number;
  setup_total_paise: number;
}

export interface Recommendation {
  pricing: {
    contract_months: number;
    lines: PricedLine[];
    totals: {
      monthly_subtotal_paise: number;
      monthly_gst_paise: number;
      monthly_total_paise: number;
      setup_total_paise: number;
    };
  };
  blocked: {
    product_id: string;
    product_name: string;
    reasons: string[];
    reason_text: string;
    alternative: string | null;
  }[];
  explanation: { text: string; source: "llm" | "template" };
  /** Optional extras (incl. what the owner asked for): priced once switched on in the quote. */
  addons?: { product_id: string; product_name?: string; qty: number; source: string }[];
}

export interface TurnResponse {
  session_id: string;
  status: string;
  locale: string;
  mode: "chat" | "form";
  messages: AssistantMessage[];
  question: Question | null;
  recommendation: Recommendation | null;
  /** "Question 2 of 3"; null once the plan is shown. */
  progress: { step: number; total: number } | null;
  /** What the plan assumes until fine-tuned: `num_sites`, `has_gstin`, `needs`. */
  assumed: string[];
  can_refine: boolean;
}

// ------------------------------------------------------------------ quotes

export interface ItemRef {
  product_id: string;
  qty: number;
}

export interface QuoteLine extends PricedLine {
  category: string;
  unit: string;
  list_unit_paise: number;
  discounts: string[];
  min_contract_months: number;
}

export interface ProductOption {
  product_id: string;
  product_name: string;
  description: string;
  category: string;
  unit: string;
  list_unit_paise: number;
  setup_unit_paise: number;
  in_bundle: boolean;
  qty: number;
  eligible: boolean;
  reasons: string[];
  reason_text: string | null;
  alternative: string | null;
  alternative_name: string | null;
  requires_one_of: string[];
  requires_met: boolean;
  recommended: boolean;
}

export interface QuoteView {
  quote_id: string;
  session_id: string;
  version: number;
  status: "ISSUED" | "ACCEPTED" | "VOID";
  locale: string;
  created_at: string;
  valid_until: string;
  /** Judged by the server clock (which honours TODAY_OVERRIDE), not the browser's. */
  expired?: boolean;
  contract_months: number;
  required_contract_months: number;
  contract_options: number[];
  lines: QuoteLine[];
  totals: {
    list_monthly_paise: number;
    savings_paise: number;
    monthly_subtotal_paise: number;
    monthly_gst_paise: number;
    monthly_total_paise: number;
    setup_subtotal_paise: number;
    setup_gst_paise: number;
    setup_total_paise: number;
  };
  gst_rate_bps: number;
  options: ProductOption[];
}

export type FixAction = "ADD" | "REPLACE" | "REMOVE" | "SET_QTY" | "REMOVE_BLOCKED";

export interface Fix {
  action: FixAction;
  product_id: string | null;
  product_name: string | null;
  products?: string[];
  qty: number | null;
  replaces: string | null;
  replaces_name: string | null;
  items: ItemRef[];
}

export interface Ineligible {
  code: "BUNDLE_INELIGIBLE";
  blocked: {
    product_id: string;
    product_name: string;
    reasons: string[];
    reason_text: string;
    requires_one_of: string[];
  }[];
  fixes: Fix[];
  quote?: QuoteView;
}

// ------------------------------------------------------------------ checkout

export type Purpose = "NEEDS_ASSESSMENT" | "KYC_VERIFICATION" | "MARKETING";
export const PURPOSES: Purpose[] = ["NEEDS_ASSESSMENT", "KYC_VERIFICATION", "MARKETING"];

export interface ConsentState {
  notice_version: string;
  purposes: Record<Purpose, { granted: boolean; required: boolean }>;
  ready_for_checkout: boolean;
}

export interface Payment {
  payment_id: string;
  attempt: number;
  status: "SUCCEEDED" | "FAILED";
  method: string;
  amount_paise: number;
  failure_code: string | null;
}

export type OrderState =
  | "QUOTE_ACCEPTED"
  | "KYC_SUBMITTED"
  | "KYC_ACTION_NEEDED"
  | "KYC_VERIFIED"
  | "PAYMENT_CONFIRMED"
  | "FEASIBILITY_CHECK"
  | "INSTALL_SCHEDULED"
  | "INSTALLED"
  | "ACTIVATED"
  | "CANCELLED"
  | "PAYMENT_FAILED"
  | "NOT_FEASIBLE";

export type StepStatus = "done" | "current" | "attention" | "todo" | "skipped";

export interface Tracker {
  steps: { state: OrderState; status: StepStatus; at: string | null }[];
  history: { state: OrderState; at: string }[];
  lines: { product_id: string; product_name: string; qty: number; install_days: number; eta: string | null }[];
  eta: string | null;
  eta_estimated: boolean;
  sla: { due: string; breached: boolean };
  kyc: {
    status: string | null;
    application_id: string | null;
    handed_off: boolean;
    fixes: { reason: string; slot: string | null; message: string }[];
  };
  alternative: {
    product_id: string;
    product_name: string;
    replaces: string[];
    drops: string[];
    install_days: number;
    monthly_from_paise: number;
    monthly_to_paise: number;
  } | null;
  refund_due_paise: number;
  can_cancel: boolean;
  can_simulate: boolean;
  notifications: { channel: "WHATSAPP" | "SMS"; state: OrderState; status: "SENT" | "FAILED" | "NO_CONTACT"; at: string }[];
}

export interface OrderSummary {
  order_id: string;
  state: OrderState;
  created_at: string;
  eta: string | null;
  products: string[];
}

export interface Order {
  order_id: string;
  state: OrderState;
  quote_id: string;
  contract_months: number;
  monthly_total_paise: number;
  setup_total_paise: number;
  amount_due_now_paise: number;
  lines: { product_id: string; product_name: string; qty: number }[];
  kyc_status: string | null;
  next_action: "KYC" | "FIX_KYC" | "PAY" | "RETRY_PAYMENT" | "CHOOSE_ALTERNATIVE" | "NONE";
  payments: Payment[];
  created_at?: string;
  tracker?: Tracker;
}

export type PaymentMethod = "upi" | "card" | "netbanking";

// ------------------------------------------------------------------ transport

/** `detail` is the backend's error body (`{code, ...}`) when it sent one. */
export class ApiError extends Error {
  constructor(
    public status: number,
    public detail: Record<string, unknown> | null = null,
  ) {
    super(`API error ${status}`);
  }

  get code(): string | null {
    const code = this.detail?.code;
    return typeof code === "string" ? code : null;
  }
}

const TOKEN_KEY = "udyam.token";

export function getToken(): string | null {
  return typeof sessionStorage === "undefined" ? null : sessionStorage.getItem(TOKEN_KEY);
}

export function setToken(token: string | null): void {
  if (token) sessionStorage.setItem(TOKEN_KEY, token);
  else sessionStorage.removeItem(TOKEN_KEY);
}

async function call<T>(
  method: string,
  path: string,
  body?: unknown,
  opts: { auth?: boolean; idempotencyKey?: string } = {},
): Promise<T> {
  const headers: Record<string, string> = {};
  if (body !== undefined) headers["Content-Type"] = "application/json";
  const token = opts.auth ? getToken() : null;
  if (token) headers.Authorization = `Bearer ${token}`;
  if (opts.idempotencyKey) headers["Idempotency-Key"] = opts.idempotencyKey;
  const res = await fetch(`${API_BASE}${path}`, {
    method,
    headers,
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!res.ok) {
    let detail: Record<string, unknown> | null = null;
    try {
      const json = (await res.json()) as { detail?: unknown };
      if (json && typeof json.detail === "object" && !Array.isArray(json.detail)) {
        detail = json.detail as Record<string, unknown>;
      }
    } catch {
      /* not JSON */
    }
    throw new ApiError(res.status, detail);
  }
  return (await res.json()) as T;
}

const enc = encodeURIComponent;

export function startSession(locale: string, mode: "chat" | "form"): Promise<TurnResponse> {
  return call("POST", "/api/v1/advisor/sessions", { locale, mode });
}

export function sendText(sessionId: string, text: string): Promise<TurnResponse> {
  return call("POST", `/api/v1/advisor/sessions/${enc(sessionId)}/turns`, { text });
}

export function sendAnswer(sessionId: string, slot: string, value: unknown): Promise<TurnResponse> {
  return call("POST", `/api/v1/advisor/sessions/${enc(sessionId)}/turns`, { slot, value });
}

/** "Not sure — pick for me" for a fine-tune question. */
export function sendUnsure(sessionId: string, slot: string): Promise<TurnResponse> {
  return call("POST", `/api/v1/advisor/sessions/${enc(sessionId)}/turns`, { slot, unsure: true });
}

/** Ask the questions skipped so far, to fine-tune the plan shown. */
export function refinePlan(sessionId: string): Promise<TurnResponse> {
  return call("POST", `/api/v1/advisor/sessions/${enc(sessionId)}/turns`, { action: "refine" });
}

/** Pick up a saved session (resume link). */
export function resumeSession(sessionId: string): Promise<TurnResponse> {
  return call("GET", `/api/v1/advisor/sessions/${enc(sessionId)}`);
}

/** Needs a signed-in (OTP-confirmed) owner: a call back, or a link texted to carry on later. */
export function askForHelp(
  sessionId: string,
  kind: "callback" | "resume_link",
): Promise<{ kind: string; status: "REQUESTED" | "SENT" | "FAILED" }> {
  return call(
    "POST",
    `/api/v1/advisor/sessions/${enc(sessionId)}/help`,
    { kind, agree: true },
    { auth: true },
  );
}

export function createQuote(
  sessionId: string,
  items?: ItemRef[],
  contractMonths?: number | null,
): Promise<QuoteView> {
  return call("POST", "/api/v1/quotes", {
    session_id: sessionId,
    ...(items ? { items } : {}),
    ...(contractMonths ? { contract_months: contractMonths } : {}),
  });
}

export function getQuote(quoteId: string): Promise<QuoteView> {
  return call("GET", `/api/v1/quotes/${enc(quoteId)}`, undefined, { auth: true });
}

/** Resolves with the new quote, or rejects with ApiError(422, Ineligible) for a cascade. */
export function changeItems(
  quoteId: string,
  items: ItemRef[],
  contractMonths: number | null,
): Promise<QuoteView> {
  return call("PATCH", `/api/v1/quotes/${enc(quoteId)}/items`, {
    items,
    ...(contractMonths ? { contract_months: contractMonths } : {}),
  });
}

export function requestOtp(
  phone: string,
): Promise<{ challenge_id: string; expires_in_seconds: number; code_length: number; dev_code?: string }> {
  return call("POST", "/api/v1/auth/otp/request", { phone });
}

export function verifyOtp(challengeId: string, code: string): Promise<{ token: string }> {
  return call("POST", "/api/v1/auth/otp/verify", { challenge_id: challengeId, code });
}

export function getConsents(): Promise<ConsentState> {
  return call("GET", "/api/v1/consents", undefined, { auth: true });
}

export function submitConsents(
  noticeVersion: string,
  locale: string,
  choices: Record<Purpose, boolean>,
): Promise<ConsentState> {
  return call(
    "POST",
    "/api/v1/consents",
    { notice_version: noticeVersion, locale, choices },
    { auth: true },
  );
}

export function placeOrder(quoteId: string, idempotencyKey: string): Promise<Order> {
  return call("POST", "/api/v1/orders", { quote_id: quoteId }, { auth: true, idempotencyKey });
}

export function getOrder(orderId: string): Promise<Order> {
  return call("GET", `/api/v1/orders/${enc(orderId)}`, undefined, { auth: true });
}

export function listOrders(): Promise<{ orders: OrderSummary[] }> {
  return call("GET", "/api/v1/orders", undefined, { auth: true });
}

export function cancelOrder(orderId: string): Promise<Order> {
  return call("POST", `/api/v1/orders/${enc(orderId)}/cancel`, undefined, { auth: true });
}

/** Demo only: stands in for operations moving the install forward. */
export function simulateFulfilment(orderId: string, state: OrderState): Promise<Order> {
  return call("POST", `/api/v1/orders/${enc(orderId)}/fulfilment`, { state }, { auth: true });
}

export function payOrder(
  orderId: string,
  method: PaymentMethod,
  idempotencyKey: string,
): Promise<{ payment: Payment; order: Order }> {
  return call(
    "POST",
    `/api/v1/orders/${enc(orderId)}/payments`,
    { method },
    { auth: true, idempotencyKey },
  );
}

/** A fresh idempotency key (32 hex chars; works outside secure contexts too). */
export function newKey(): string {
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  return Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
}

// ------------------------------------------------------------------ KYC

export type DocSlot = "GST_CERTIFICATE" | "UDYAM_CERTIFICATE" | "SIGNATORY_ID" | "ADDRESS_PROOF";
export const DOC_SLOTS: DocSlot[] = ["GST_CERTIFICATE", "UDYAM_CERTIFICATE", "SIGNATORY_ID", "ADDRESS_PROOF"];

export type KycStatus = "DRAFT" | "PASS" | "REVIEW" | "PENDING" | "ACTION_NEEDED" | "FAIL";

export interface KycFix {
  reason: string;
  slot: DocSlot | null;
  message: string;
}

export interface KycApplication {
  application_id: string;
  status: KycStatus;
  failed_attempts: number;
  handed_off: boolean;
  documents: { slot: DocSlot; source: "OCR" | "MANUAL" | "LOOKUP"; uploaded_at: string }[];
  verdict: {
    decision: Exclude<KycStatus, "DRAFT">;
    review_required: boolean;
    lookup_offered: boolean;
    handed_off: boolean;
    checks: { check_id: string; status: string; reason: string | null; slot: DocSlot | null }[];
    fixes: KycFix[];
  } | null;
  /** Which numbers the owner gave (never the values): offers "verify online". */
  on_record?: { gstin: boolean; udyam: boolean };
}

export interface UploadResult {
  document_id: string;
  slot: DocSlot;
  accepted: boolean;
  reason: string | null;
  fix: string | null;
}

/** What the applicant declares "on record"; every field is optional and sealed at rest. */
export function createKycApplication(onRecord: {
  contact_name?: string;
  gstin?: string;
  udyam_number?: string;
  /** The order this KYC is for; it is then never reused for another order. */
  order_id?: string;
}): Promise<KycApplication> {
  return call("POST", "/api/v1/kyc/applications", onRecord, { auth: true });
}

export function getKycApplication(applicationId: string): Promise<KycApplication> {
  return call("GET", `/api/v1/kyc/applications/${enc(applicationId)}`, undefined, { auth: true });
}

/**
 * Uploads the raw file as the request body: the server reads the type from the bytes
 * and never sees the file name (UP-06). Rejected files (type, size) throw ApiError.
 */
export async function uploadKycDocument(
  applicationId: string,
  slot: DocSlot,
  file: Blob,
  locale: string,
): Promise<UploadResult> {
  const token = getToken();
  const res = await fetch(
    `${API_BASE}/api/v1/kyc/applications/${enc(applicationId)}/documents/${slot}?locale=${enc(locale)}`,
    {
      method: "POST",
      headers: {
        "Content-Type": "application/octet-stream",
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
      },
      body: file,
    },
  );
  const json = (await res.json().catch(() => null)) as Record<string, unknown> | null;
  if (!res.ok) {
    const detail = json && typeof json.detail === "object" ? (json.detail as Record<string, unknown>) : json;
    throw new ApiError(res.status, detail);
  }
  return json as unknown as UploadResult;
}

/** Verify the GSTIN given on record in the GST registry instead of uploading a certificate. */
export function lookupGstinOnRecord(applicationId: string): Promise<KycApplication> {
  return call("POST", `/api/v1/kyc/applications/${enc(applicationId)}/gst/lookup`, undefined, {
    auth: true,
  });
}

/** Ask for someone to visit and collect the documents instead of uploading. */
export function requestKycVisit(applicationId: string): Promise<KycApplication> {
  return call("POST", `/api/v1/kyc/applications/${enc(applicationId)}/visit`, undefined, {
    auth: true,
  });
}

export interface GstinPreview {
  outcome: "FOUND";
  gstin: string;
  status: string;
  /** Masked by the server (anti-enumeration), e.g. "B***** D**** C***". */
  legal_name: string;
  trade_name: string;
  state: string;
}

/** Masked registry preview of a GSTIN ("is this your business?"). Throws on 404 / 422. */
export function previewGstin(gstin: string): Promise<GstinPreview> {
  return call("GET", `/api/v1/lookup/gstin/${enc(gstin)}`, undefined, { auth: true });
}

export function lookupUdyamOnRecord(applicationId: string): Promise<KycApplication> {
  return call("POST", `/api/v1/kyc/applications/${enc(applicationId)}/udyam/lookup`, undefined, {
    auth: true,
  });
}

export function submitKyc(applicationId: string, locale: string): Promise<KycApplication> {
  return call(
    "POST",
    `/api/v1/kyc/applications/${enc(applicationId)}/submit?locale=${enc(locale)}`,
    undefined,
    { auth: true },
  );
}

/** N2 fallback: type the GSTIN / Udyam number; a person always reviews it. */
export function manualEntry(applicationId: string, slot: DocSlot, value: string): Promise<KycApplication> {
  return call(
    "POST",
    `/api/v1/kyc/applications/${enc(applicationId)}/documents/${slot}/manual`,
    { value },
    { auth: true },
  );
}
