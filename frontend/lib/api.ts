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
}

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
}

export interface TurnResponse {
  session_id: string;
  status: string;
  locale: string;
  mode: "chat" | "form";
  messages: AssistantMessage[];
  question: Question | null;
  recommendation: Recommendation | null;
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

export interface Order {
  order_id: string;
  state: string;
  quote_id: string;
  contract_months: number;
  monthly_total_paise: number;
  setup_total_paise: number;
  amount_due_now_paise: number;
  lines: { product_id: string; product_name: string; qty: number }[];
  kyc_status: string | null;
  next_action: "KYC" | "PAY" | "RETRY_PAYMENT" | "NONE";
  payments: Payment[];
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
