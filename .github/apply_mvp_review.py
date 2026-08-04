from __future__ import annotations

import io
import re
import tokenize
from pathlib import Path

ROOT = Path('.')

PYPROJECT = '''[tool.ruff]
target-version = "py312"
line-length = 100
extend-exclude = ["zalo-gateway/dist"]

[tool.ruff.lint]
select = ["E4", "E7", "E9", "F", "I", "UP"]
ignore = ["E501"]

[tool.ruff.lint.per-file-ignores]
"**/__init__.py" = ["F401"]

[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["test"]
'''
Path('pyproject.toml').write_text(PYPROJECT, encoding='utf-8')
Path('pytest.ini').unlink(missing_ok=True)
Path('requirements-dev.txt').write_text('-r requirements.txt\npytest>=8.0.0\npytest-asyncio>=0.24.0\nruff>=0.9.0\n', encoding='utf-8')

CI = '''name: CI
on:
  push:
    branches: [main]
  pull_request:
permissions:
  contents: read
jobs:
  python:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
          cache: pip
      - run: pip install -r requirements-dev.txt
      - run: ruff check .
      - run: ruff format --check .
      - run: python -m compileall -q .
      - run: pytest -q
  typescript:
    runs-on: ubuntu-latest
    defaults:
      run:
        working-directory: zalo-gateway
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-node@v4
        with:
          node-version: "20"
          cache: npm
          cache-dependency-path: zalo-gateway/package-lock.json
      - run: npm install
      - run: npm run check
'''
Path('.github/workflows/ci.yml').write_text(CI, encoding='utf-8')

KEEP = (
    'noqa', 'type:', 'pragma', 'nosec', 'fmt:', 'security', 'bảo mật', 'secret',
    'token', 'fail', 'fallback', 'race', 'lock', 'idempot', 'atomic', 'transaction',
    'retry', 'timeout', 't+', 'out-of-sample', 'risk', 'provider', 'cache', 'lifecycle',
    'event loop', 'timezone', 'stale', 'corporate', 'contract', 'invariant', 'compat',
    'tương thích', 'api', 'schema', 'sla', 'quota', 'rate limit', 'cảnh báo',
    'lưu ý', 'quan trọng', 'không được', 'để tránh', 'vì ', 'because', 'must ',
)
DROP = (
    'trước đây', 'bản cũ', 'port từ', 'ca gây lỗi', 'regression cho bug',
    'bước 1', 'bước 2', 'bước 3', 'bước 4', 'bước 5', 'bước 6',
    'fix a', 'fix b', 'fix c', 'fix d', 'fix e', 'fix f', 'fix g',
)

def keep_comment(raw: str) -> bool:
    text = raw.lstrip('#').strip().lower()
    if not text:
        return False
    if set(text) <= {'─', '━', '=', '-', ' '}:
        return False
    if any(marker in text for marker in DROP):
        return False
    return any(marker in text for marker in KEEP)


def clean_python_comments(path: Path) -> None:
    source = path.read_text(encoding='utf-8')
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(source).readline))
    except (tokenize.TokenError, IndentationError):
        return
    cleaned = []
    for token in tokens:
        if token.type == tokenize.COMMENT and not keep_comment(token.string):
            token = tokenize.TokenInfo(token.type, '', token.start, token.end, token.line)
        cleaned.append(token)
    path.write_text(tokenize.untokenize(cleaned), encoding='utf-8')

for path in ROOT.rglob('*.py'):
    if any(part in {'.git', '.venv', 'node_modules'} for part in path.parts):
        continue
    if path.name == 'apply_mvp_review.py':
        continue
    clean_python_comments(path)

# Replace historical implementation notes with concise module contracts.
p = Path('stock/fundamentals.py')
text = p.read_text(encoding='utf-8')
if text.startswith('"""'):
    end = text.find('"""', 3)
    text = '''"""Sector-aware fundamentals from vnstock/VCI.

The provider is unofficial and fail-soft: unavailable or ambiguous fields are
returned as ``None`` instead of being guessed. Blocking calls run in worker
threads with timeouts so they cannot block the bot event loop.
"""''' + text[end + 3:]
p.write_text(text, encoding='utf-8')

p = Path('stock/backtest.py')
text = p.read_text(encoding='utf-8')
if text.startswith('"""'):
    end = text.find('"""', 3)
    text = '''"""Cost-aware, walk-forward backtesting for the deterministic stock policy.

The engine evaluates BUY signals with next-session entry, T+ settlement,
fees, sell tax, slippage and a strict out-of-sample holdout. Heavy runs are
disabled on Render by default and belong in an offline or scheduled job.
"""''' + text[end + 3:]
p.write_text(text, encoding='utf-8')

# Import smoke test must import every stock module intentionally.
Path('test/test_stock_import_smoke.py').write_text('''import importlib
from pathlib import Path


def test_stock_package_imports_and_template():
    modules = [
        "analysis", "backtest", "features", "fundamental_profiles", "fundamentals",
        "policy", "providers", "sector", "validation",
    ]
    loaded = {name: importlib.import_module(f"stock.{name}") for name in modules}
    analysis = loaded["analysis"]
    backtest = loaded["backtest"]
    assert analysis._STOCK_PROMPT_TEMPLATE is not None
    assert (Path(analysis.__file__).parent / "templates" / "stock_analysis_prompt.j2").is_file()
    assert backtest.DEFAULT_BACKTEST_DAYS >= 3 * backtest.TRADING_DAYS_PER_YEAR
''', encoding='utf-8')

Path('.github/workflows/keep-alive.yml').write_text('''name: Keep Render awake
on:
  schedule:
    - cron: "7,17,27,37,47,57 * * * *"
  workflow_dispatch:
jobs:
  ping:
    runs-on: ubuntu-latest
    steps:
      - name: Ping health endpoint
        run: curl --fail --silent --show-error --max-time 30 "${{ vars.RENDER_APP_URL }}/"
''', encoding='utf-8')

ENV_EXAMPLE = '''# Required
TELEGRAM_TOKEN=
ALLOWED_USER_ID=
GEMINI_SECURE_1PSID=
GEMINI_SECURE_1PSIDTS=
DATABASE_URL=
SETTINGS_ENC_KEY=

# Required for webhook deployment
WEBHOOK_SECRET=
WEBHOOK_BASE_URL=

# Recommended provider fallback
GOOGLE_AI_STUDIO_API_KEY_1=
GOOGLE_AI_STUDIO_API_KEY_2=
GOOGLE_AI_STUDIO_MODEL=gemini-2.5-flash
PROVIDER_ORDER=cookie,api1,api2

# Diagnostics and networking
DIAGNOSE_SECRET=
GEMINI_PROXY=
GEMINI_WEBAPI_DEBUG=false

# Provider lifecycle
COOKIE_PROBE_INTERVAL_SEC=900
API_QUOTA_COOLDOWN_SEC=3600
GEMINI_COOKIE_REFRESH_INTERVAL=540
GEMINI_COOKIE_CALL_TIMEOUT_SEC=18

# Conversation and scheduler
CHAT_SKILL_PATH=chat_skill.yaml
CHAT_HISTORY_TURNS=8
CHAT_SESSION_TIMEOUT_SEC=21600
REMINDER_CHECK_INTERVAL_SEC=30
ENABLE_DAILY_DIGEST=true
DAILY_DIGEST_HOUR_VN=8

# Telegram media
MEDIA_DIR=media
TELEGRAM_CONNECT_TIMEOUT=30
TELEGRAM_READ_TIMEOUT=90
TELEGRAM_WRITE_TIMEOUT=90
TELEGRAM_POOL_TIMEOUT=30
TELEGRAM_MEDIA_RETRIES=3

# Zalo gateway
ZALO_ENABLED=false
ZALO_BRIDGE_SECRET=
ZALO_CONTROL_PORT=9901
ZALO_BOT_ACCOUNT_ID=zalo-bot
ZALO_CONTROLLER_ID=
ZALO_COOKIE_JSON=
ZALO_IMEI=
ZALO_USER_AGENT=
ZALO_GROUP_REFRESH_MS=60000
ZALO_OUTBOX_POLL_MS=15000
ZALO_GROUP_RETENTION_DAYS=30
ZALO_DAILY_SUMMARY_HOUR=9
ZALO_IMAGE_MAX_BYTES=8388608

# Heavy backtests are forbidden on the Render web process.
STOCK_BACKTEST_ALLOW_ON_RENDER=false
STOCK_BACKTEST_CONCURRENCY=4
'''
Path('.env.example').write_text(ENV_EXAMPLE, encoding='utf-8')

README = r'''# Gemini Personal Assistant — Single-user MVP

A personal AI assistant for one owner, delivered through Telegram and optionally Zalo. It combines Gemini chat, durable memory, reminders, notes, product search, image-to-prompt workflows, and a deterministic Vietnam stock-analysis pipeline.

## MVP status

The repository is designed and hardened for a **single-user deployment**:

- Telegram access is restricted by `ALLOWED_USER_ID`.
- Telegram and Zalo share the same services, memory and stock pipeline.
- Sensitive settings are encrypted before database storage.
- Webhook work and background tasks are drained during shutdown.
- Heavy stock backtests are blocked on the Render web process.
- Python lint, formatting, tests and TypeScript checks run in CI.

This is not a multi-tenant SaaS, licensed financial-advice platform or brokerage execution system.

## Features

### Assistant

- Gemini provider chain: cookie session, AI Studio key 1, then key 2.
- Configurable provider order, quota cooldown and cookie recovery probe.
- Session history plus long-term memory in Supabase Postgres.
- Natural-language notes, reminders and portfolio facts.
- Product-price search using official grounded search only.
- Image analysis and prompt generation.

### Telegram

- Long polling for local development and webhook mode for Render.
- Owner-only access.
- Rich-text chunking and image/document handling.
- Operational commands for memory, provider status, model selection and Zalo login.

### Zalo

- Optional `zca-js` gateway running beside the Python service.
- QR login for a dedicated bot account.
- One-time pairing of the owner/controller account.
- Private owner-to-bot chat and image forwarding.
- Allowlisted group ingestion, summaries and a durable outbox.
- Daily group summaries in `Asia/Ho_Chi_Minh`.

### Vietnam stock research

The `stock/` package separates data, validation, features, policy and presentation:

```text
DNSE ──┐
       ├─ OHLCV contract ─ features ─ deterministic policy ─ report
VCI ───┘                         │
                         VNINDEX / sector / fundamentals / news
```

Capabilities include:

- DNSE OHLCV with automatic `vnstock`/VCI failover.
- Strict validation for array lengths, finite values, OHLC relationships and dates.
- RSI, MACD, moving averages, Bollinger Bands, ADX, ATR, Donchian channels, liquidity, distribution days and key levels.
- Market-regime, data-quality, setup and risk/reward gates.
- `BUY`, `HOLD`, `WATCH`, `SELL` and `NO_TRADE` decisions made by code, not by the language model.
- Entry zone, stop, targets, R:R, position sizing and bull/base/bear scenarios.
- Sector-aware fundamentals: P/B-oriented profiles for banks, securities, insurance and real estate; P/E-oriented profiles where appropriate.
- Cost-aware walk-forward backtesting with fees, tax, slippage, T+ settlement and a 30% out-of-sample holdout.

The bot does not connect to a brokerage account and cannot place orders.

## Architecture

```text
Telegram webhook ─┐
                  ├─ FastAPI / shared services ─ Gemini provider chain
Zalo gateway ─────┘              │
                                 ├─ memory and tools
                                 ├─ stock research
                                 └─ Supabase Postgres

Render Docker service
├── Uvicorn: webhook, bridge, scheduler and assistant
└── Node.js: optional Zalo listener and loopback control server
```

The Zalo control server binds to `127.0.0.1:9901`; it is not exposed publicly.

## Requirements

- Python 3.12
- Node.js 18 or newer
- Telegram bot token
- Supabase Postgres Session Pooler URL
- Gemini cookie session
- Fernet encryption key
- Optional Google AI Studio keys
- Optional dedicated Zalo account

## Required environment variables

| Variable | Purpose |
|---|---|
| `TELEGRAM_TOKEN` | Token from BotFather |
| `ALLOWED_USER_ID` | The only Telegram user allowed to use the bot |
| `GEMINI_SECURE_1PSID` | Gemini cookie-session credential |
| `DATABASE_URL` | Supabase Session Pooler connection string |
| `SETTINGS_ENC_KEY` | Fernet key for sensitive database settings |
| `WEBHOOK_SECRET` | Telegram webhook secret on Render |
| `WEBHOOK_BASE_URL` | Public base URL; Render can supply `RENDER_EXTERNAL_URL` |

Generate secrets:

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

`SETTINGS_ENC_KEY` is mandatory and fail-closed. Losing or changing it makes previously encrypted sessions unreadable.

See `.env.example` and `render.yaml` for all optional settings.

## Local development

```bash
git clone https://github.com/traique/Gemini.git
cd Gemini
python -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env
python main.py
```

Run the Docker deployment locally:

```bash
docker build -t gemini-assistant .
docker run --env-file .env -p 10000:10000 gemini-assistant
```

## Render deployment

1. Create a Supabase project and copy the **Session Pooler** connection string.
2. Create a Telegram bot and obtain the owner Telegram ID.
3. Create a Render Blueprint from this repository.
4. Enter the required secrets from `render.yaml`.
5. Deploy with `ZALO_ENABLED=false` first.
6. Verify `/` returns `200` and Telegram responds.
7. Enable Zalo only after the Telegram/webhook path is healthy.

The web service explicitly sets:

```env
STOCK_BACKTEST_ALLOW_ON_RENDER=false
```

A backtest call on Render fails before fetching data or consuming significant CPU. Generate historical statistics in a separate CI/offline job and deploy the resulting `stock/data/backtest_stats.json`.

## Zalo setup

Set `ZALO_ENABLED=true` and configure `ZALO_BRIDGE_SECRET`, then redeploy. In Telegram:

```text
/zalo
```

Scan the QR with the dedicated Zalo bot account and confirm the login on the phone. Request `/zalo` again to obtain a one-time pairing code, then send from the owner Zalo account:

```text
/pair 123456
```

Use `/zalologout` in Telegram to remove the saved session and controller.

Group management from the paired Zalo owner:

```text
/nhomzalo
/themnhom <group_id> <alias>
/nhom
/xoanhom <group_id-or-alias>
/tongket <alias|all> <24h|7d|homnay|homqua>
```

Only new text messages from allowlisted groups are stored. Group media is not persisted and the bot does not reply inside groups.

## Commands

| Command | Purpose |
|---|---|
| `/help` | Show help |
| `/zalo` | Login or inspect Zalo state |
| `/zalologout` | Remove the Zalo session |
| `/prompt` | Generate an image prompt |
| `/gia` | Search product prices |
| `/reset` | Reset conversation context |
| `/history` | Show recent history |
| `/memory` | Show long-term memory |
| `/forget` | Remove long-term memory |
| `/notes` | Show notes |
| `/model` | View or change the cookie model |
| `/status` | Show provider-chain status |
| `/usecookie` | Retry the cookie provider |

## Quality checks

```bash
pip install -r requirements-dev.txt
ruff check .
ruff format --check .
python -m compileall -q .
pytest -q

cd zalo-gateway
npm install
npm run check
```

CI runs the same Python and TypeScript checks on pushes and pull requests.

## Project layout

```text
ai/                 Gemini clients, routing and provider state
channels/           Shared channel contracts and Zalo persistence
core/               Configuration, encryption and database access
handlers/           Telegram handlers
services/           Shared chat, command, memory and telemetry services
stock/              Market data, validation, research policy and backtest
zalo-gateway/        Node.js Zalo listener
web.py               FastAPI webhook entrypoint
main.py              Local long-polling entrypoint
bot_app.py           Telegram application factory and lifecycle
```

## Operational security

- Never commit `.env`, Gemini cookies, Zalo sessions, QR images or media files.
- Never log complete credentials or signed Zalo CDN URLs.
- Keep the Zalo control port on loopback only.
- Use different values for webhook, diagnostics and Zalo bridge secrets.
- Keep `SETTINGS_ENC_KEY` stable and backed up securely.
- Use one Zalo listener for the bot account; opening another web session may disconnect it.
- Rotate sessions immediately if a credential may have leaked.

## Known limitations

- `gemini-webapi`, `zca-js`, DNSE endpoints and `vnstock` are unofficial or undocumented dependencies and can change without notice.
- The stock module is a research assistant, not a broker or licensed adviser.
- Backtest results depend on provider coverage and do not guarantee future performance.
- The project is intentionally single-user and does not implement tenant isolation, billing, roles or horizontal scaling.
- Render free instances can sleep; the optional keep-alive workflow depends on the repository variable `RENDER_APP_URL`.

## Additional documentation

- `docs/zalo-render.md`
- `zalo-gateway/README.md`
- `.env.example`
- `render.yaml`

## Responsibility

For personal/internal use. The operator is responsible for the terms of service of Google, Telegram, Zalo and market-data providers, and for all investment decisions.
'''
Path('README.md').write_text(README, encoding='utf-8')
