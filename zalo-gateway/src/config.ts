export type GatewayConfig = {
  cookie: unknown; imei: string; userAgent: string; controllerId: string; accountId: string;
  bridgeBaseUrl: string; bridgeSecret: string; groupRefreshMs: number; outboxPollMs: number;
};

function required(name: string): string {
  const value = process.env[name]?.trim();
  if (!value) throw new Error(`Missing required environment variable: ${name}`);
  return value;
}

function interval(name: string, fallback: number): number {
  const parsed = Number(process.env[name] || fallback);
  return Number.isFinite(parsed) ? Math.max(10_000, parsed) : fallback;
}

export function loadConfig(): GatewayConfig {
  const rawCookie = required("ZALO_COOKIE_JSON");
  let cookie: unknown;
  try { cookie = JSON.parse(rawCookie); } catch { throw new Error("ZALO_COOKIE_JSON must be valid JSON"); }
  const base = process.env.ZALO_BRIDGE_BASE_URL?.trim() || `http://127.0.0.1:${process.env.PORT || "8000"}/internal/zalo`;
  return {
    cookie, imei: required("ZALO_IMEI"), userAgent: required("ZALO_USER_AGENT"),
    controllerId: required("ZALO_CONTROLLER_ID"), accountId: process.env.ZALO_BOT_ACCOUNT_ID?.trim() || "zalo-bot",
    bridgeBaseUrl: base.replace(/\/$/, ""), bridgeSecret: required("ZALO_BRIDGE_SECRET"),
    groupRefreshMs: interval("ZALO_GROUP_REFRESH_MS", 60_000), outboxPollMs: interval("ZALO_OUTBOX_POLL_MS", 15_000),
  };
}
