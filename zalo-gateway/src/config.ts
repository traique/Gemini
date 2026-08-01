export type GatewayConfig = {
  cookie: unknown;
  imei: string;
  userAgent: string;
  controllerId: string;
  accountId: string;
  bridgeUrl: string;
  bridgeSecret: string;
};

function required(name: string): string {
  const value = process.env[name]?.trim();
  if (!value) throw new Error(`Missing required environment variable: ${name}`);
  return value;
}

export function loadConfig(): GatewayConfig {
  const rawCookie = required("ZALO_COOKIE_JSON");
  let cookie: unknown;
  try {
    cookie = JSON.parse(rawCookie);
  } catch {
    throw new Error("ZALO_COOKIE_JSON must be valid JSON");
  }

  return {
    cookie,
    imei: required("ZALO_IMEI"),
    userAgent: required("ZALO_USER_AGENT"),
    controllerId: required("ZALO_CONTROLLER_ID"),
    accountId: process.env.ZALO_BOT_ACCOUNT_ID?.trim() || "zalo-bot",
    bridgeUrl:
      process.env.ZALO_BRIDGE_URL?.trim() ||
      `http://127.0.0.1:${process.env.PORT || "8000"}/internal/zalo/message`,
    bridgeSecret: required("ZALO_BRIDGE_SECRET"),
  };
}
