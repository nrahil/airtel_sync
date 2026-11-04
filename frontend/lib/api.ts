/** Typed client for the advisor API (`/api/v1/advisor`). */

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

export class ApiError extends Error {
  constructor(public status: number) {
    super(`API error ${status}`);
  }
}

async function post<T>(path: string, body: unknown): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) throw new ApiError(res.status);
  return (await res.json()) as T;
}

export function startSession(locale: string, mode: "chat" | "form"): Promise<TurnResponse> {
  return post("/api/v1/advisor/sessions", { locale, mode });
}

export function sendText(sessionId: string, text: string): Promise<TurnResponse> {
  return post(`/api/v1/advisor/sessions/${encodeURIComponent(sessionId)}/turns`, { text });
}

export function sendAnswer(sessionId: string, slot: string, value: unknown): Promise<TurnResponse> {
  return post(`/api/v1/advisor/sessions/${encodeURIComponent(sessionId)}/turns`, { slot, value });
}
