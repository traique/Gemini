# Gemini Personal Assistant — MVP một người dùng

Trợ lý AI cá nhân chạy trên Telegram và tùy chọn Zalo, dùng chung Gemini, trí nhớ dài hạn, công cụ, nhắc việc và phân tích cổ phiếu Việt Nam.

## Trạng thái MVP

Repository được thiết kế cho **một chủ sở hữu**:

- Telegram chỉ chấp nhận `ALLOWED_USER_ID`.
- Zalo ghép đôi một tài khoản controller với tài khoản bot riêng.
- Telegram và Zalo dùng chung memory, tools và stock pipeline.
- Cookie và session nhạy cảm được mã hóa trước khi lưu database.
- Webhook và background tasks được drain khi shutdown.
- Backtest nặng bị chặn trên Render Web Service.
- Python và TypeScript có lint, format, test và CI.

Đây không phải hệ thống multi-tenant, nền tảng tư vấn tài chính được cấp phép hoặc broker đặt lệnh.

## Tính năng

### Trợ lý chung

- Provider chain: Gemini cookie → AI Studio key 1 → key 2.
- Tự cooldown API hết quota và probe lại cookie.
- Lịch sử theo phiên và trí nhớ dài hạn trên Supabase Postgres.
- Ghi chú, reminder và facts danh mục qua ngôn ngữ tự nhiên.
- Tìm giá sản phẩm bằng grounded search chính thức.
- Phân tích ảnh và tạo prompt.

### Telegram

- Long polling khi chạy local; webhook khi deploy Render.
- Khóa truy cập theo một Telegram user ID.
- Xử lý text dài, ảnh và image document.
- Lệnh quản lý memory, provider, model và Zalo.

### Zalo

- Gateway `zca-js` tùy chọn chạy cạnh Python service.
- Đăng nhập tài khoản bot bằng QR từ Telegram.
- Ghép đôi controller bằng mã dùng một lần.
- Chat riêng controller → bot và gửi ảnh.
- Thu thập text từ nhóm allowlist, tạo summary và gửi qua durable outbox.
- Tổng kết hằng ngày theo `Asia/Ho_Chi_Minh`.

### Phân tích cổ phiếu Việt Nam

`stock/` tách rõ data, validation, features, policy và presentation:

```text
DNSE ──┐
       ├─ OHLCV contract ─ features ─ deterministic policy ─ report
VCI ───┘                         │
                         VNINDEX / ngành / cơ bản / tin tức
```

Năng lực hiện tại:

- DNSE OHLCV với failover tự động sang `vnstock`/VCI.
- Strict contract cho độ dài mảng, số hữu hạn, quan hệ OHLC và ngày giao dịch.
- RSI, MACD, MA/EMA, Bollinger, ADX, ATR, Donchian, thanh khoản, distribution days và key levels.
- Gate theo market regime, data quality, setup và risk/reward.
- `BUY`, `HOLD`, `WATCH`, `SELL`, `NO_TRADE` do code quyết định; LLM chỉ diễn giải.
- Vùng mua, stop, target, R:R, position sizing và kịch bản bull/base/bear.
- Fundamental theo ngành: ưu tiên P/B cho ngân hàng, chứng khoán, bảo hiểm và bất động sản; P/E ở nhóm phù hợp.
- Walk-forward backtest có phí, thuế bán, slippage, T+, và 30% out-of-sample.

Bot không kết nối tài khoản chứng khoán và không đặt lệnh.

## Kiến trúc

```text
Telegram webhook ─┐
                  ├─ FastAPI / shared services ─ Gemini provider chain
Zalo gateway ─────┘              │
                                 ├─ memory và tools
                                 ├─ stock research
                                 └─ Supabase Postgres

Render Docker service
├── Uvicorn: webhook, bridge, scheduler và assistant
└── Node.js: Zalo listener và loopback control server
```

Zalo control server chỉ bind `127.0.0.1:9901`.

## Yêu cầu

- Python 3.12
- Node.js 18+
- Telegram bot token
- Supabase Postgres Session Pooler URL
- Gemini cookie session
- Fernet encryption key
- Tùy chọn: Google AI Studio keys và tài khoản Zalo bot riêng

## Cấu hình bắt buộc

| Biến | Mục đích |
|---|---|
| `TELEGRAM_TOKEN` | Token từ BotFather |
| `ALLOWED_USER_ID` | Telegram user duy nhất được phép dùng bot |
| `GEMINI_SECURE_1PSID` | Credential của Gemini cookie session |
| `DATABASE_URL` | Supabase Session Pooler URL |
| `SETTINGS_ENC_KEY` | Fernet key mã hóa settings nhạy cảm |
| `WEBHOOK_SECRET` | Secret cho Telegram webhook trên Render |
| `WEBHOOK_BASE_URL` | Public base URL; Render có thể cung cấp `RENDER_EXTERNAL_URL` |

Tạo secret:

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

`SETTINGS_ENC_KEY` là bắt buộc và fail closed. Không đổi hoặc làm mất key sau khi đã lưu ciphertext.

Xem `.env.example` và `render.yaml` để biết toàn bộ biến tùy chọn.

## Chạy local

```bash
git clone https://github.com/traique/Gemini.git
cd Gemini
python -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env
python main.py
```

Docker:

```bash
docker build -t gemini-assistant .
docker run --env-file .env -p 10000:10000 gemini-assistant
```

## Deploy Render

1. Tạo Supabase và lấy **Session Pooler** connection string.
2. Tạo Telegram bot và lấy Telegram user ID của chủ sở hữu.
3. Tạo Render Blueprint từ repository.
4. Điền secret trong `render.yaml`.
5. Deploy lần đầu với `ZALO_ENABLED=false`.
6. Kiểm tra `/` trả `200` và Telegram hoạt động.
7. Chỉ bật Zalo sau khi webhook ổn định.

Render đặt cứng:

```env
STOCK_BACKTEST_ALLOW_ON_RENDER=false
```

Nếu code vô tình gọi backtest trên Render, tác vụ dừng trước khi tải dữ liệu hoặc dùng CPU đáng kể. Hãy tạo `stock/data/backtest_stats.json` bằng job CI/offline riêng rồi deploy file kết quả.

## Thiết lập Zalo

Đặt `ZALO_ENABLED=true`, cấu hình `ZALO_BRIDGE_SECRET` và redeploy. Trong Telegram gửi:

```text
/zalo
```

Quét QR bằng tài khoản Zalo bot và xác nhận trên điện thoại. Gửi `/zalo` lần nữa để nhận mã pairing, sau đó từ tài khoản controller nhắn bot:

```text
/pair 123456
```

Dùng `/zalologout` trên Telegram để xóa session và controller đã lưu.

Lệnh quản lý nhóm từ controller:

```text
/nhomzalo
/themnhom <group_id> <alias>
/nhom
/xoanhom <group_id-or-alias>
/tongket <alias|all> <24h|7d|homnay|homqua>
```

Gateway chỉ lưu text mới từ nhóm allowlist, không backfill, không lưu media nhóm và không trả lời trong nhóm.

## Lệnh chính

| Lệnh | Chức năng |
|---|---|
| `/help` | Hướng dẫn |
| `/zalo` | Đăng nhập hoặc xem trạng thái Zalo |
| `/zalologout` | Xóa Zalo session |
| `/prompt` | Tạo prompt hình ảnh |
| `/gia` | Tìm giá sản phẩm |
| `/reset` | Reset conversation context |
| `/history` | Xem lịch sử gần nhất |
| `/memory` | Xem trí nhớ dài hạn |
| `/forget` | Xóa trí nhớ dài hạn |
| `/notes` | Xem ghi chú |
| `/model` | Xem hoặc đổi cookie model |
| `/status` | Xem provider chain |
| `/usecookie` | Thử lại cookie provider |

## Kiểm tra chất lượng

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

CI chạy các kiểm tra Python và TypeScript trên push và pull request.

## Cấu trúc repository

```text
ai/                 Gemini clients và provider routing
channels/           Channel contracts và Zalo persistence
core/               Config, encryption và database
handlers/           Telegram handlers
services/           Chat, commands, memory và telemetry dùng chung
stock/              Market data, validation, policy và backtest
zalo-gateway/        Node.js Zalo listener
web.py               FastAPI webhook entrypoint
main.py              Local long-polling entrypoint
bot_app.py           Telegram app factory và lifecycle
```

## Bảo mật vận hành

- Không commit `.env`, Gemini cookie, Zalo session, QR hoặc media.
- Không log credential hoặc signed Zalo CDN URL đầy đủ.
- Không public port `9901`.
- Dùng secret riêng cho webhook, diagnostics và Zalo bridge.
- Giữ `SETTINGS_ENC_KEY` ổn định và backup an toàn.
- Chỉ chạy một Zalo listener cho tài khoản bot.
- Rotate session ngay nếu nghi ngờ bị lộ.

## Giới hạn đã biết

- `gemini-webapi`, `zca-js`, DNSE và `vnstock` là dependency không chính thức hoặc không có SLA.
- Stock module là research assistant, không phải broker hoặc tư vấn viên được cấp phép.
- Backtest phụ thuộc độ phủ provider và không bảo đảm hiệu suất tương lai.
- Dự án cố ý chỉ hỗ trợ một người dùng; không có tenant isolation, billing, roles hoặc horizontal scaling.
- Render Free có thể sleep; keep-alive workflow cần repository variable `RENDER_APP_URL`.

## Tài liệu bổ sung

- `docs/zalo-render.md`
- `zalo-gateway/README.md`
- `.env.example`
- `render.yaml`

## Trách nhiệm

Dùng cho mục đích cá nhân/nội bộ. Người vận hành chịu trách nhiệm về điều khoản của Google, Telegram, Zalo, nguồn dữ liệu thị trường và mọi quyết định đầu tư.
