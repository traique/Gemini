# Zalo gateway

Headless `zca-js` adapter for account B. It runs beside the Python API in the
same Render container, so the project consumes one Render Free instance.

## 1. Create the account-B session locally

```bash
cd zalo-gateway
npm install
npm run login:qr
```

Open `zalo-qr.png`, scan it with account B and approve the login. The command
writes `zalo-session.json` with mode `0600` and removes the QR image after
success. Never commit or send that session file.

Map the generated fields to Render:

```text
accountId  -> ZALO_BOT_ACCOUNT_ID
cookie     -> ZALO_COOKIE_JSON (JSON array, one line)
imei       -> ZALO_IMEI
userAgent  -> ZALO_USER_AGENT
```

## 2. Configure Render

Set these environment variables before enabling the process:

```env
ZALO_ENABLED=false
ZALO_COOKIE_JSON=[]
ZALO_IMEI=
ZALO_USER_AGENT=
ZALO_CONTROLLER_ID=
ZALO_BOT_ACCOUNT_ID=zalo-bot
ZALO_BRIDGE_SECRET=
```

Generate the bridge secret with:

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

After all values are saved, set `ZALO_ENABLED=true` and redeploy. The gateway
connects to `http://127.0.0.1:$PORT/internal/zalo` by default.

## Commands from account A

```text
/nhomzalo
/nhom
/themnhom <group_id> <alias>
/xoanhom <group_id hoặc alias>
/tongket <group> [24h|7d|homnay|homqua]
```

Daily summaries run after 09:00 Asia/Ho_Chi_Minh and are delivered from B to A
through the persistent outbox.

## Operational constraints

- Use one dedicated account B and one listener only.
- Do not open Zalo Web with B while the gateway is active; it can disconnect the listener.
- Keep `ZALO_ENABLED=false` until all credentials exist, otherwise supervisor
  will restart a failing process repeatedly.
- Render filesystem is ephemeral; session values live in environment variables,
  group messages and summaries live in Supabase.
- `zca-js` is unofficial and can cause account restrictions. Use at your own risk.
