/** Back-office API client (`/api/v1/backoffice`). Staff token kept apart from the customer's. */

import { API_BASE, ApiError } from "@/lib/api";

export type Role = "sales" | "partner" | "kyc_reviewer" | "admin";

export interface StaffMe {
  staff_id: string;
  username: string;
  role: Role;
  partner_id: string | null;
  permissions: string[];
}

export interface Factor {
  feature: string;
  value: string;
  effect: number;
}

export interface LeadRow {
  lead_id: string;
  created_date: string;
  business_name: string;
  contact_name: string;
  contact_role: string;
  business_type: string;
  segment: string;
  city: string;
  city_tier: number;
  lead_source: string;
  preferred_language: string;
  employees: number;
  sites: number;
  gstin: string | null;
  gstin_status: string;
  has_udyam: boolean;
  products_of_interest: string[];
  stage_reached: string;
  drop_reason: string | null;
  assigned_partner: string | null;
  score: number | null;
  factors: Factor[];
}

export interface SessionRow {
  session_id: string;
  status: string;
  stall_reason: string;
  at_question: string | null;
  idle_minutes: number;
  updated_at: string;
  locale: string;
  turns: number;
  profile: Record<string, unknown>;
  monthly_total_paise: number | null;
  order_id: string | null;
  order_state: string | null;
  contactable: boolean;
  /** The owner asked to be called (shown first). */
  callback_requested_at?: string | null;
  assigned_partner: string | null;
  score: number | null;
  factors: Factor[];
}

export interface ReviewItem {
  item_id: number;
  kind: "REVIEW" | "HANDOFF";
  reasons: string[];
  created_at: string;
  application_id: string;
  application_status: string;
  failed_attempts: number;
  handed_off: boolean;
  checks: { check_id: string; status: string; reason: string | null; slot: string | null; confidence: number | null; evidence: Record<string, string> }[];
  fixes: { reason: string; slot: string | null; message: string }[];
  order_id: string | null;
  order_state: string | null;
}

export interface SlaRow {
  order_id: string;
  state: string;
  created_at: string;
  age_days: number;
  sla_due: string;
  eta: string | null;
  alerted_at: string | null;
  /** The owner tapped "Call me" on their late order. */
  callback_requested_at: string | null;
  products: string[];
}

export interface ModelView {
  available: boolean;
  version?: string;
  split?: { train: string; test: string };
  metrics?: {
    train_rows: number;
    test_rows: number;
    train_rate: number;
    test_rate: number;
    auc: number;
    baseline_random_auc: number;
    baseline_gstin_auc: number;
    lift_top_decile: number;
  } | null;
  top_coefficients?: { column: string; coefficient: number }[];
}

export type RevealKind = "LEAD" | "SESSION" | "ORDER" | "KYC";
export type RevealReason = "CALL_BACK" | "KYC_REVIEW" | "CUSTOMER_REQUEST" | "FULFILMENT";
export const REVEAL_REASONS: RevealReason[] = ["CALL_BACK", "KYC_REVIEW", "CUSTOMER_REQUEST", "FULFILMENT"];

const STAFF_KEY = "udyam.staff";

export function getStaffToken(): string | null {
  return typeof sessionStorage === "undefined" ? null : sessionStorage.getItem(STAFF_KEY);
}

export function setStaffToken(token: string | null): void {
  if (token) sessionStorage.setItem(STAFF_KEY, token);
  else sessionStorage.removeItem(STAFF_KEY);
}

async function call<T>(method: string, path: string, body?: unknown): Promise<T> {
  const headers: Record<string, string> = {};
  if (body !== undefined) headers["Content-Type"] = "application/json";
  const token = getStaffToken();
  if (token) headers.Authorization = `Bearer ${token}`;
  const res = await fetch(`${API_BASE}/api/v1/backoffice${path}`, {
    method,
    headers,
    body: body === undefined ? undefined : JSON.stringify(body),
    cache: "no-store",
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

const q = (params: Record<string, string | number | boolean | null | undefined>) => {
  const s = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) if (v !== null && v !== undefined && v !== "") s.set(k, String(v));
  const text = s.toString();
  return text ? `?${text}` : "";
};

export const bo = {
  login: (username: string, password: string) =>
    call<StaffMe & { token: string; expires_in_seconds: number }>("POST", "/auth/login", { username, password }),
  me: () => call<StaffMe>("GET", "/me"),
  model: () => call<ModelView>("GET", "/model"),
  leads: (p: { segment?: string; source?: string; unassigned?: boolean; offset?: number; limit?: number } = {}) =>
    call<{ total: number; offset: number; limit: number; model_available: boolean; leads: LeadRow[] }>(
      "GET",
      `/leads${q(p)}`,
    ),
  sessions: (idleMinutes?: number) =>
    call<{ idle_minutes: number; sessions: SessionRow[] }>("GET", `/sessions${q({ idle_minutes: idleMinutes })}`),
  assign: (kind: "LEAD" | "SESSION", ref: string, partnerId: string | null) =>
    call<{ kind: string; ref: string; partner_id: string | null }>("POST", "/assignments", {
      kind,
      ref,
      partner_id: partnerId,
    }),
  reviewQueue: () => call<{ items: ReviewItem[] }>("GET", "/review-queue"),
  decide: (itemId: number, approve: boolean, note: string) =>
    call<{ item_id: number; status: string; application_status: string; orders_updated: number }>(
      "POST",
      `/review/${itemId}`,
      { approve, note },
    ),
  sla: () => call<{ orders: SlaRow[] }>("GET", "/sla-breaches"),
  fulfil: (orderId: string, state: string) =>
    call<{ order_id: string; state: string }>("POST", `/orders/${encodeURIComponent(orderId)}/fulfilment`, { state }),
  reveal: (kind: RevealKind, ref: string, reason: RevealReason) =>
    call<{ kind: string; ref: string; fields: Record<string, string> }>("POST", "/reveal", { kind, ref, reason }),
};
