# Zalo gateway

Headless `zca-js` adapter for account B, running beside Python in the same Render container.

## Telegram QR login and pairing

Configure `ZALO_ENABLED=true`, `ZALO_BRIDGE_SECRET` and `SETTINGS_ENC_KEY`, then redeploy. `ZALO_CONTROLLER_ID` may now be left empty.

1. In the private Telegram chat, send `/zalo`.
2. Scan the QR with account B and approve it.
3. Send `/zalo` again. Telegram returns a six-digit pairing code.
4. From account A, message B exactly: `/pair 123456` using the current code.
5. B confirms the pairing and stores A's UID encrypted in Supabase.

The code expires after five minutes, is single-use and is only displayed to the allowed Telegram user. After pairing, A can use chat, stock, prompt, group and summary commands normally.

Use `/zalologout` in Telegram to close B's listener and delete both the saved Zalo session and controller pairing.

## Security and operations

- The control server binds only to `127.0.0.1:9901`.
- Session and controller records use `SETTINGS_ENC_KEY` before storage.
- Never expose the control port or share the QR/pairing code.
- Only one B listener and one active pairing code are allowed.
- Do not open Zalo Web with B while the gateway is active.
- `zca-js` is unofficial and may cause account restrictions.
