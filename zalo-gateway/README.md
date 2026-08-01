# Zalo gateway — phase 1

Headless `zca-js` adapter for account B. It accepts only private text messages
from account A, forwards them to the authenticated Python bridge, then sends the
returned Gemini/stock response back to A.

## Required environment variables

```env
ZALO_COOKIE_JSON=[]
ZALO_IMEI=
ZALO_USER_AGENT=
ZALO_CONTROLLER_ID=
ZALO_BOT_ACCOUNT_ID=
ZALO_BRIDGE_SECRET=
# Defaults to http://127.0.0.1:$PORT/internal/zalo/message
ZALO_BRIDGE_URL=
```

Never commit cookie, IMEI or bridge secrets. Only one listener may run for the
account. Opening Zalo Web with account B can disconnect this listener.

## Current scope

- Private text A → B → shared Gemini/stock/memory pipeline.
- Sender allowlist and bridge authentication.
- Sequential processing, message dedupe and automatic supervisor restart.

Next phases add command parity (`/gia`, `/prompt`, `/memory`, `/forget`), image
streaming, allowlisted group ingestion, 09:00 summaries, encrypted session
storage and the single-container Render runtime.
