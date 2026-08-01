# Deploy Zalo account B on the existing Render service

This deployment intentionally uses one Docker web service. `supervisord` keeps
two processes alive:

1. `uvicorn web:api` — Telegram webhook, Gemini and Supabase.
2. `node zalo-gateway/dist/index.js` — account-B Zalo listener.

## Safe rollout

1. Merge/deploy with `ZALO_ENABLED=false`; verify `/` still returns `status: ok`.
2. Generate the account-B session locally using `npm run login:qr` inside
   `zalo-gateway/`.
3. Add the generated cookie JSON, IMEI, user agent, account ID, controller A ID
   and a random bridge secret to Render Environment.
4. Keep every session value secret and ensure `ZALO_COOKIE_JSON` remains valid
   single-line JSON.
5. Change `ZALO_ENABLED=true` and redeploy.
6. In logs, verify `[zalo] authenticated account=...` and `[zalo] listener started`.
7. From A, send `/nhomzalo`, add only approved groups, then test `/tongket`.

## Rollback

Set `ZALO_ENABLED=false` and redeploy. Telegram/Python remains active and the
Zalo process becomes an idle process, with no need to revert the Docker runtime.

## Resource notes

Both processes share the Free instance memory. Keep the gateway text-only,
process summaries sequentially and avoid Electron, Chromium, Deplao UI, media
caches or multiple Zalo accounts.
