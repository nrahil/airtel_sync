import fs from "node:fs";
import path from "node:path";

import { defineConfig, devices } from "@playwright/test";

/**
 * Live end-to-end suite (E2E-01…05): the real backend (FastAPI, local Tesseract, the
 * provided dataset in `mock_data`, Groq unreachable) and a production build of the PWA.
 * No mocks. Needs the dataset and Tesseract 5, so it runs locally, not in CI:
 *
 *   npm run test:e2e:live
 *
 * `scripts.e2e_server` wipes its own state on every start (fresh DB, keys and blobs).
 */
const API_PORT = Number(process.env.E2E_API_PORT ?? 8100);
const WEB_PORT = Number(process.env.E2E_LIVE_PORT ?? 3101);
const apiBase = `http://127.0.0.1:${API_PORT}`;
const baseURL = `http://localhost:${WEB_PORT}`;
const backend = path.resolve(__dirname, "../backend");

function python(): string {
  if (process.env.E2E_PYTHON) return process.env.E2E_PYTHON;
  for (const candidate of [".venv/Scripts/python.exe", ".venv/bin/python"]) {
    const full = path.join(backend, candidate);
    if (fs.existsSync(full)) return `"${full}"`;
  }
  return "python";
}

export default defineConfig({
  testDir: "./e2e-live",
  // One shared backend; journeys stay independent through distinct phone numbers.
  fullyParallel: false,
  workers: 1,
  retries: 0,
  timeout: 180_000,
  expect: { timeout: 30_000 }, // OCR of four specimens takes several seconds
  reporter: "list",
  use: { baseURL, trace: "retain-on-failure", video: process.env.E2E_VIDEO ? "on" : "off" },
  projects: [{ name: "live-mobile-chrome", use: { ...devices["Pixel 7"] } }],
  webServer: [
    {
      command: `${python()} -m scripts.e2e_server --port ${API_PORT} --origin ${baseURL}`,
      cwd: backend,
      url: `${apiBase}/healthz`,
      reuseExistingServer: false,
      timeout: 120_000,
      stdout: "ignore",
    },
    {
      command: `npx next build && npx next start -p ${WEB_PORT}`,
      env: { NEXT_DIST_DIR: ".next-live", NEXT_PUBLIC_API_BASE: apiBase },
      url: baseURL,
      reuseExistingServer: false,
      timeout: 400_000,
    },
  ],
});
