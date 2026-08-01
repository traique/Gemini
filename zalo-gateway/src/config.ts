export type GatewayConfig = { cookie?: unknown; imei?: string; userAgent: string; controllerId: string; accountId: string; bridgeBaseUrl: string; bridgeSecret: string; groupRefreshMs: number; outboxPollMs: number; controlPort: number };
function required(name: string) { const value=process.env[name]?.trim(); if(!value) throw new Error(`Missing required environment variable: ${name}`); return value; }
function interval(name:string,fallback:number){const parsed=Number(process.env[name]||fallback);return Number.isFinite(parsed)?Math.max(1000,parsed):fallback;}
export function loadConfig(): GatewayConfig {
 let cookie: unknown; const raw=process.env.ZALO_COOKIE_JSON?.trim(); if(raw) try{cookie=JSON.parse(raw)}catch{throw new Error("ZALO_COOKIE_JSON must be valid JSON")}
 const base=process.env.ZALO_BRIDGE_BASE_URL?.trim()||`http://127.0.0.1:${process.env.PORT||"8000"}/internal/zalo`;
 return {cookie,imei:process.env.ZALO_IMEI?.trim(),userAgent:process.env.ZALO_USER_AGENT?.trim()||"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/145.0.0.0 Safari/537.36",controllerId:required("ZALO_CONTROLLER_ID"),accountId:process.env.ZALO_BOT_ACCOUNT_ID?.trim()||"zalo-bot",bridgeBaseUrl:base.replace(/\/$/,""),bridgeSecret:required("ZALO_BRIDGE_SECRET"),groupRefreshMs:interval("ZALO_GROUP_REFRESH_MS",60000),outboxPollMs:interval("ZALO_OUTBOX_POLL_MS",15000),controlPort:interval("ZALO_CONTROL_PORT",9901)};
}
