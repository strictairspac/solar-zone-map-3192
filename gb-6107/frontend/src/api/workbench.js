import { request } from "./http.js";

export const api = {
  config: () => request("/workbench"),
  login: data => request("/auth/login", {method: "POST", data}),
  cases: () => request("/cases"),
  createCase: data => request("/cases", {method: "POST", data}),
  runs: () => request("/runs"),
  run: (id, signal) => request(`/runs/${id}`, {signal}),
  enqueue: (id, data, idempotencyKey) => request(`/cases/${id}/runs`, {
    method: "POST",
    data,
    headers: idempotencyKey ? {"Idempotency-Key": idempotencyKey} : undefined,
  }),
  review: id => request(`/runs/${id}/review`),
  submitReview: (id, data) => request(`/runs/${id}/review`, {method: "POST", data}),
  retryRun: id => request(`/runs/${id}/retry`, {method: "POST"}),
  advanceRun: id => request(`/runs/${id}/advance`, {method: "POST"}),
  controlSources: (id, data) => request(`/runs/${id}/source-control`, {method: "POST", data}),
  receipt: id => request(`/runs/${id}/receipt`),
  receipts: () => request("/receipts"),
  export: id => request(`/runs/${id}/export`, {raw: true}),
  audit: () => request("/audit", {signal: AbortSignal.timeout(5000)}),
};
