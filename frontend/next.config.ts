import path from "node:path";
import type { NextConfig } from "next";

const isDev = process.env.NODE_ENV !== "production";
const apiOrigin = new URL(process.env.NEXT_PUBLIC_API_BASE ?? "http://localhost:8000").origin;

/**
 * Content-Security-Policy (ARCHITECTURE §12.1 A05, AC-09).
 *
 * The pages are pre-rendered static HTML, so per-request nonces are not available and
 * Next.js's inline bootstrap scripts need `'unsafe-inline'` in `script-src`. Everything
 * else is locked down: scripts only from this origin, no `eval` (except the dev
 * server's hot reload), network calls only to this origin and the API, no plugins,
 * no framing, no foreign form targets. React escapes text, and nothing renders
 * HTML from the API or the LLM (LLM02).
 */
const csp = [
  "default-src 'self'",
  `script-src 'self' 'unsafe-inline'${isDev ? " 'unsafe-eval'" : ""}`,
  "style-src 'self' 'unsafe-inline'",
  "img-src 'self' data: blob:",
  "font-src 'self'",
  `connect-src 'self' ${apiOrigin}${isDev ? " ws:" : ""}`,
  "worker-src 'self'",
  "manifest-src 'self'",
  "object-src 'none'",
  "base-uri 'self'",
  "form-action 'self'",
  "frame-ancestors 'none'",
].join("; ");

const securityHeaders = [
  { key: "Content-Security-Policy", value: csp },
  { key: "Strict-Transport-Security", value: "max-age=63072000; includeSubDomains" },
  { key: "X-Content-Type-Options", value: "nosniff" },
  { key: "X-Frame-Options", value: "DENY" },
  { key: "Referrer-Policy", value: "strict-origin-when-cross-origin" },
  { key: "Cross-Origin-Opener-Policy", value: "same-origin" },
  // Camera: the KYC upload (file input). Microphone: the advisor's Speak button
  // (browser speech-to-text; we never record or upload audio). Nothing else.
  { key: "Permissions-Policy", value: "camera=(self), microphone=(self), geolocation=(), payment=(), usb=()" },
];

const nextConfig: NextConfig = {
  // Standalone server only for the Docker image; `next start` is used locally / in CI.
  output: process.env.NEXT_OUTPUT === "standalone" ? "standalone" : undefined,
  // The live end-to-end build (its own API base) must not overwrite the normal one.
  distDir: process.env.NEXT_DIST_DIR || ".next",
  outputFileTracingRoot: path.join(__dirname),
  poweredByHeader: false,
  reactStrictMode: true,
  async headers() {
    return [
      { source: "/:path*", headers: securityHeaders },
      {
        // Service worker must never be served stale.
        source: "/sw.js",
        headers: [
          { key: "Cache-Control", value: "no-cache, no-store, must-revalidate" },
          { key: "Content-Type", value: "application/javascript; charset=utf-8" },
        ],
      },
    ];
  },
};

export default nextConfig;
