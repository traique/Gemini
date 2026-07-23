# Gemini Telegram Bot

Bot Telegram trợ lý cá nhân đa lĩnh vực kiêm phân tích cổ phiếu Việt Nam,
chạy trên nền tảng Gemini với **provider-chain 3 lớp** (cookie Gemini Pro cá
nhân → Google AI Studio API key 1 → key 2), **trí nhớ hội thoại** theo phiên
và **trí nhớ dài hạn** sống qua nhiều phiên (user facts, tóm tắt, tuỳ chọn
semantic recall bằng pgvector).

Thiết kế cho **1 người dùng/1 deployment** (`ALLOWED_USER_ID`), không phải
kiến trúc multi-tenant.

## Mục lục

- [Cảnh báo trước khi dùng](#cảnh-báo-trước-khi-dùng)
- [Tính năng](#tính-năng)
- [Kiến trúc](#kiến-trúc)
- [Cài đặt nhanh](#cài-đặt-nhanh)
- [Cấu hình](#cấu-hình)
- [Deploy](#deploy)
- [Các lệnh](#các-lệnh)
- [Xử lý lỗi thường gặp](#xử-lý-lỗi-thường-gặp)
- [Kiểm thử](#kiểm-thử)

## Cảnh báo trước khi dùng

- `gemini-webapi` (nhánh cookie) giả lập phiên đăng nhập web bằng cookie
  của tài khoản Gemini Pro/Advanced cá nhân — **không phải API chính thức**
  của Google, và có thể vi phạm Điều khoản dịch vụ của Google. Dùng cho mục
  đích cá nhân/nội bộ, tự chịu rủi ro tài khoản có thể bị giới hạn hoặc khoá
  nếu Google phát hiện truy cập bất thường.
- `GEMINI_SECURE_1PSID` là session token **toàn quyền tài khoản Google** của
  bạn. Không chia sẻ, không commit lên Git, không log ra console. Bật
  `SETTINGS_ENC_KEY` để mã hoá trước khi lưu vào DB (xem [Cấu hình](#cấu-hình)).
- Bot chỉ phục vụ đúng 1 Telegram user ID. Muốn cho nhiều người dùng cần
  kiến trúc multi-tenant riêng (auth, cách ly dữ liệu, billing) — không nằm
  trong phạm vi bản này.
- Muốn một trợ lý ổn định lâu dài thay vì tối đa hoá miễn phí, cấu hình
  `PROVIDER_ORDER=api1,api2,cookie` để API chính thức làm xương sống, cookie
  chỉ là lớp bonus (xem [Provider-chain](#provider-chain-cookie--api1--api2)).

## Tính năng

- **Chat tự nhiên đa lĩnh vực** với persona có thể tuỳ biến qua
  `chat_skill.yaml` (đời sống, công việc, kiến thức chung...).
- **Phân tích cổ phiếu Việt Nam**: tra giá/khối lượng/vốn hoá realtime, phân
  tích kỹ thuật (MA/RSI/MACD/ADX/hỗ trợ-kháng cự), phân tích cơ bản (P/E,
  P/B, khối ngoại...), ngữ cảnh ngành, tin tức — qua pipeline
  `stock_analysis.py` (fetch → validate → feature → policy → prompt) độc
  lập với Gemini cho phần số liệu, chỉ dùng Gemini để diễn giải.
- **Provider-chain tự phục hồi**: cookie lỗi tự chuyển API chính thức, quay
  lại cookie khi cookie sống lại; API hết quota tự chuyển sang key dự
  phòng.
- **Trí nhớ 2 lớp**: cửa sổ trượt theo phiên (nhánh API) + trí nhớ dài hạn
  (user facts, tóm tắt hội thoại, tuỳ chọn semantic recall qua pgvector).
- **Function calling**: ghi chú, đặt nhắc việc, tra danh mục đầu tư qua ngôn
  ngữ tự nhiên, không cần lệnh riêng.
- **Scheduler nền**: gửi reminder đúng giờ, digest danh mục đầu tư hàng
  ngày.

## Kiến trúc

```
gemini-telegram-bot/
├── main.py                # Entrypoint local (long polling)
├── web.py                 # Entrypoint webhook (FastAPI, dùng khi deploy Render)
├── bot_app.py              # Factory đăng ký handler dùng chung cho main.py + web.py
├── core/                    # Nền tảng dùng chung, không phụ thuộc business logic khác
│   ├── config.py               # Đọc biến môi trường + nạp chat_skill.yaml
│   ├── database.py              # Supabase Postgres (asyncpg)
│   └── crypto.py                 # Mã hoá đối xứng (Fernet) cho dữ liệu nhạy cảm lưu DB
├── ai/                       # Provider-chain cookie -> api1 -> api2 + trí nhớ phiên
│   ├── provider_state.py        # State machine thuần (cookie chết/sống, api cooldown)
│   ├── cookie_client.py          # Nhánh cookie (gemini-webapi: session/token/ChatSession)
│   ├── official_client.py        # Nhánh api1/api2 (Google AI Studio SDK chính thức)
│   └── orchestrator.py            # Facade công khai: ask()/chat()/analyze_image()/...
├── services/                 # Business logic phụ thuộc ai/ + core/
│   ├── memory_service.py        # Trí nhớ dài hạn: user_facts + rolling summary + semantic recall
│   ├── tools.py                   # Function calling: ghi chú / nhắc việc / tra danh mục
│   └── telemetry.py               # Ghi prompt/kết quả (bảng prompts/results) dùng chung
├── handlers/                 # Handler Telegram
│   ├── common.py                 # Helper dùng chung: auth, tải file, reply dài
│   ├── commands.py                # Lệnh đơn giản (/start, /help, /status, /model...)
│   ├── chat_router.py             # Điều hướng tin nhắn thường: stock hay chat tự nhiên
│   ├── stock_handler.py           # Nhánh hỏi giá/phân tích cổ phiếu của chat
│   └── media_handler.py           # Xử lý ảnh gửi tới bot
├── logging_setup.py        # Cấu hình logging, filter che secret
├── scheduler.py              # Background task: reminder + daily digest
├── messages.py                # Chuỗi thông báo UI/cảnh báo dùng chung
├── stock_analysis.py          # Orchestrator: fetch -> validate -> feature -> policy -> prompt
├── stock_features.py          # Feature layer thuần toán (RSI/MACD/ADX/ATR/Donchian/Bollinger)
├── stock_indicators.py        # Chỉ báo kỹ thuật bổ sung
├── stock_validation.py        # Đánh giá chất lượng dữ liệu OHLCV
├── stock_policy.py            # Policy layer: gate quyết định action (bao gồm NO_TRADE)
├── stock_backtest.py          # Backtest walk-forward tối thiểu cho tín hiệu BUY
├── stock_sector.py            # Ngữ cảnh ngành (rotation dòng tiền)
├── stock_fundamentals.py      # Định giá + khối ngoại (vnstock)
├── stock_providers.py         # Nguồn dữ liệu giá/tin tức (DNSE...)
├── tg_format.py                # Convert markdown-lite -> HTML cho Telegram
├── diagnose_gemini.py          # Script/endpoint debug
├── templates/                  # Prompt templates (Jinja2), tách khỏi business logic
│   ├── stock_analysis_prompt.j2
│   └── chat_skill_prompt.j2
├── chat_skill.yaml              # Persona/rules/tone cho chat tự nhiên (có cấu trúc)
├── tests/                       # Unit test (pytest, chạy bằng mock)
├── .github/workflows/keep-alive.yml  # Cron ping giữ Render không ngủ
├── render.yaml                  # Blueprint Render
├── requirements.txt
├── requirements-dev.txt
└── .env.example
```

### Provider-chain: cookie → api1 → api2

Chat tự nhiên, phân tích cổ phiếu và ảnh→prompt đều đi qua provider-chain có
trạng thái, mặc định ưu tiên **cookie** (Gemini Pro/Advanced cá nhân, mạnh
hơn free tier AI Studio nhưng dễ vỡ hơn):

- **Cookie lỗi** → bot chuyển hẳn sang `GOOGLE_AI_STUDIO_API_KEY_1` (api1)
  cho mọi request tiếp theo, không thử lại cookie ở mỗi tin nhắn. Cookie chỉ
  được thử lại qua: probe nền tự động mỗi `COOKIE_PROBE_INTERVAL_SEC` giây,
  phát hiện biến môi trường cookie mới lúc khởi động, hoặc lệnh `/usecookie`.
- **api1 hết quota (429)** → cooldown `API_QUOTA_COOLDOWN_SEC` giây, chuyển
  sang `GOOGLE_AI_STUDIO_API_KEY_2` (api2) nếu có; hết cooldown tự dùng lại
  api1.
- Đổi thứ tự ưu tiên bằng `PROVIDER_ORDER` (mặc định `cookie,api1,api2`).

Biết 1 câu trả lời đến từ đâu: gõ `/status`, hoặc xem tin nhắn có dòng ghi
chú `⚙️ API` ở cuối (nghĩa là vừa dùng api1/api2, không có dòng này là từ
cookie), hoặc xem log dòng `Provider-chain: chuyển active_provider -> ...`.

### Trí nhớ hội thoại theo phiên

Nhánh cookie dùng `ChatSession` (gemini-webapi), Google tự giữ lịch sử phía
họ. Nhánh api1/api2 (mỗi lượt gọi độc lập) nạp lại `CHAT_HISTORY_TURNS` lượt
gần nhất (mặc định 8) từ bảng `chat_messages`, kèm `chat_skill.yaml` làm
`system_instruction`. Mọi lượt chat (bất kể provider) đều ghi vào bảng này
nên chuyển provider giữa chừng vẫn liền mạch. Nghỉ quá `CHAT_SESSION_TIMEOUT_SEC`
giây (mặc định 6 giờ) thì phiên coi như kết thúc, không nạp lại lịch sử cũ.
`/reset` xoá cả hai.

### Trí nhớ dài hạn (`services/memory_service.py`)

Sống qua mọi phiên, gồm `user_facts` (sự thật bền về người dùng, trích bằng
Gemini sau mỗi lượt chat) và rolling summary (tóm tắt hợp nhất dần, tránh
phình token theo thời gian). Cả hai được chèn vào đầu prompt ở cả 2 nhánh
provider, trước khối grounding (dữ liệu tức thời như giá cổ phiếu). Chạy
ngầm, không chặn phản hồi chính; tự tắt êm nếu chưa cấu hình API key chính
thức. Xem bằng `/memory`, xoá bằng `/forget`.

Tuỳ chọn thêm lớp **semantic recall bằng pgvector**: mỗi lượt chat được lưu
kèm embedding (`text-embedding-004`, luôn qua API chính thức) để tìm lại
đoạn hội thoại cũ gần nghĩa nhất. Tự tắt êm nếu Postgres không bật được
extension `vector` — `user_facts`/rolling summary không phụ thuộc tính năng
này.

### Function calling (`services/tools.py`)

Ghi chú, nhắc việc, và tra danh mục đầu tư được nhận diện qua ngôn ngữ tự
nhiên (Gemini tự quyết định có cần gọi tool không, qua 1 lượt gọi JSON
riêng), không cần lệnh riêng. Tra giá/phân tích cổ phiếu vẫn dùng
keyword-matching xác định (`stock_analysis.wants_full_analysis`/
`wants_price_quote`) để lấy giá realtime từ DNSE mà không phụ thuộc 1 lượt
gọi LLM.

## Cài đặt nhanh

### 1. Supabase (database)

1. Tạo project free tại [supabase.com](https://supabase.com).
2. **Project Settings → Database → Connect** → chọn tab **Session pooler**
   (không chọn "Direct connection" — chỉ hỗ trợ IPv6, hầu hết nền tảng
   deploy không hỗ trợ outbound IPv6, sẽ lỗi `Network is unreachable`).
3. Copy connection string, điền vào `DATABASE_URL`.

Bot tự tạo toàn bộ bảng cần thiết khi khởi động lần đầu, không cần chạy
migration tay.

### 2. Telegram Bot

1. Chat [@BotFather](https://t.me/BotFather) → `/newbot` → lấy **Bot Token**.
2. Chat [@userinfobot](https://t.me/userinfobot) → lấy **Telegram User ID**.

### 3. Cookie Gemini

1. Đăng nhập [gemini.google.com](https://gemini.google.com) bằng tài khoản
   Pro/Advanced (khuyến nghị tab ẩn danh, đóng tab sau khi lấy cookie).
2. `F12` → tab **Network** → reload → mở 1 request → tab **Cookies** → lấy
   `__Secure-1PSID` và `__Secure-1PSIDTS`.

### 4. (Khuyến nghị) Google AI Studio — lớp dự phòng khi cookie lỗi

1. Tạo API key tại [aistudio.google.com/apikey](https://aistudio.google.com/apikey)
   (free tier, không cần trùng tài khoản Gemini Pro ở bước 3) → điền
   `GOOGLE_AI_STUDIO_API_KEY_1`.
2. (Tuỳ chọn) Lặp lại với tài khoản Google khác để có `_2`, nhân đôi quota
   free/ngày.
3. Bỏ trống cả hai nếu không muốn dùng provider-chain — bot chỉ dùng cookie.

### 5. (Khuyến nghị) Mã hoá dữ liệu nhạy cảm lưu DB

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Điền vào `SETTINGS_ENC_KEY`. Bỏ trống thì bot vẫn chạy, chỉ là lưu dạng
plaintext.

## Cấu hình

Copy `.env.example` thành `.env` rồi điền giá trị thật — **không commit
`.env`**.

| Biến | Bắt buộc | Mặc định | Mô tả |
|---|---|---|---|
| `TELEGRAM_TOKEN` | ✅ | — | Token từ BotFather |
| `ALLOWED_USER_ID` | ✅ | — | Telegram user ID duy nhất được phép dùng bot |
| `GEMINI_SECURE_1PSID` | ✅ | — | Cookie đăng nhập Gemini |
| `GEMINI_SECURE_1PSIDTS` | ✅ | — | Cookie đăng nhập Gemini |
| `DATABASE_URL` | ✅ | — | Connection string Supabase (Session pooler) |
| `GEMINI_PROXY` | | — | Proxy HTTP(S) cho nhánh cookie, nếu cần |
| `CHAT_SKILL_PATH` | | `chat_skill.yaml` | Đường dẫn file persona (`.yaml`/`.yml` có cấu trúc, hoặc `.txt` thô để tương thích ngược) |
| `SETTINGS_ENC_KEY` | | — | Khoá Fernet mã hoá dữ liệu nhạy cảm lưu DB |
| `DIAGNOSE_SECRET` | | — | Header bắt buộc để gọi `/diagnose`; bỏ trống = endpoint luôn trả 403 |
| `GOOGLE_AI_STUDIO_API_KEY_1` / `_2` | | — | API key Google AI Studio cho provider-chain |
| `GOOGLE_AI_STUDIO_MODEL` | | `gemini-2.5-flash` | Model dùng cho nhánh API |
| `PROVIDER_ORDER` | | `cookie,api1,api2` | Thứ tự ưu tiên provider |
| `COOKIE_PROBE_INTERVAL_SEC` | | `900` | Chu kỳ tự thử lại cookie khi đang ở nhánh API |
| `API_QUOTA_COOLDOWN_SEC` | | `3600` | Thời gian cooldown khi 1 API key hết quota |
| `CHAT_HISTORY_TURNS` | | `8` | Số lượt chat gần nhất nạp lại cho nhánh API |
| `CHAT_SESSION_TIMEOUT_SEC` | | `21600` | Nghỉ quá thời gian này thì coi phiên đã kết thúc |
| `REMINDER_CHECK_INTERVAL_SEC` | | `30` | Chu kỳ quét reminder đến hạn |
| `ENABLE_DAILY_DIGEST` | | `true` | Bật/tắt digest danh mục hàng ngày |
| `DAILY_DIGEST_HOUR_VN` | | `8` | Giờ gửi digest (giờ VN) |
| `WEBHOOK_SECRET` / `WEBHOOK_BASE_URL` | Chỉ khi chạy `web.py` | — | Cấu hình webhook (Render tự cấp `RENDER_EXTERNAL_URL`/`$PORT`) |
| `MEDIA_DIR` | | `media` | Thư mục lưu file tạm |
| `TELEGRAM_*_TIMEOUT`, `TELEGRAM_MEDIA_RETRIES` | | xem `.env.example` | Timeout/retry gọi Telegram Bot API |

Xem đầy đủ (bao gồm các biến nâng cao ít dùng) trong `.env.example`.

## Deploy

### Render (khuyến nghị)

1. Push project lên GitHub (repo private cũng được).
2. [Render Dashboard](https://dashboard.render.com) → **New +** →
   **Blueprint** → chọn repo, Render tự đọc `render.yaml`.
3. Điền các biến môi trường được yêu cầu (xem bảng [Cấu hình](#cấu-hình));
   `WEBHOOK_SECRET` tự tạo bằng `python -c "import secrets; print(secrets.token_urlsafe(32))"`.
4. **Apply** — service khởi động và tự set webhook Telegram trỏ về
   `https://<tên-service>.onrender.com`.

Nếu deploy trước khi có biến mới, Render không tự thêm — vào **Environment
→ Edit → + Add Environment Variable** rồi **Save Changes**.

**Giữ service không ngủ (free tier)**: dùng UptimeRobot ping `/` (hỗ trợ cả
GET/HEAD) mỗi 5 phút làm lớp chính, và workflow có sẵn
`.github/workflows/keep-alive.yml` (đặt biến repo `RENDER_APP_URL`) làm lớp
backup. Không đặt cron vào `/diagnose` — endpoint này gọi Gemini thật mỗi
lần, chỉ dùng để debug thủ công.

### VPS / máy local (long polling)

Không cần webhook/HTTPS/domain:

```bash
git clone <repo-url> gemini-bot && cd gemini-bot
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env && nano .env   # điền các biến bắt buộc, bỏ trống WEBHOOK_*
python main.py
```

Chạy nền bền vững bằng `systemd`:

```ini
# /etc/systemd/system/gemini-bot.service
[Unit]
Description=Gemini Telegram Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/home/ubuntu/gemini-bot
ExecStart=/home/ubuntu/gemini-bot/venv/bin/python main.py
Restart=always
RestartSec=5
EnvironmentFile=/home/ubuntu/gemini-bot/.env

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload && sudo systemctl enable --now gemini-bot
sudo journalctl -u gemini-bot -f   # log realtime
```

### Cookie Gemini sống qua các lần restart

`gemini-webapi` tự rotate `__Secure-1PSIDTS` mỗi ~10 phút khi đang chạy,
nhưng giá trị mới chỉ nằm trong RAM. Bot tự lưu cookie đã rotate vào bảng
`settings` (mã hoá nếu có `SETTINGS_ENC_KEY`) mỗi 90 giây và ưu tiên đọc từ
đó khi khởi động — miễn cookie ban đầu còn hợp lệ và bạn không đăng nhập lại
`gemini.google.com` bằng tài khoản đó trên trình duyệt sau đó (hành động
này khiến Google rotate cookie phía server, vô hiệu hoá cookie bot đang
giữ).

## Các lệnh

| Lệnh | Mô tả |
|---|---|
| `/reset` | Xoá ngữ cảnh chat (cả cookie lẫn trí nhớ API), bắt đầu hội thoại mới |
| `/history` | Xem 10 lượt gần nhất (trí nhớ theo phiên) |
| `/memory` | Xem trí nhớ dài hạn (sự thật + tóm tắt) |
| `/forget` | Xoá trí nhớ dài hạn (không ảnh hưởng phiên hiện tại) |
| `/notes` | Xem ghi chú đã lưu qua function calling |
| `/model` | Xem/đổi model dùng cho chat (`/model pro`, `/model auto`) |
| `/status` | Xem provider đang dùng, cooldown quota, cấu hình trí nhớ |
| `/usecookie` | Ép thử lại cookie ngay sau khi dán cookie mới |
| `/help` | Hiển thị hướng dẫn |

Mọi tin nhắn không bắt đầu bằng `/` được xử lý như chat tự nhiên. Nhắc tới
1 mã cổ phiếu Việt Nam sẽ tự trả giá realtime (không qua Gemini); có từ ngữ
yêu cầu phân tích rõ ràng (vd "phân tích giúp anh mã FPT") mới chạy pipeline
phân tích đầy đủ. Ghi chú/nhắc việc cũng được nhận diện tự động, không cần
lệnh riêng.

## Xử lý lỗi thường gặp

| Triệu chứng | Nguyên nhân / cách xử lý |
|---|---|
| `OSError: [Errno 101] Network is unreachable` lúc khởi động | Đang dùng "Direct connection" của Supabase (IPv6-only). Đổi sang connection string tab **Session pooler**. |
| Lỗi đăng nhập/cookie | Lấy cookie mới (xem [bước 3](#3-cookie-gemini)), cập nhật biến môi trường, gõ `/usecookie` để không phải đợi redeploy. |
| Bot báo lỗi chat nhưng `/diagnose` báo `init()` OK | `__Secure-1PSIDTS` đã cũ do đăng nhập lại `gemini.google.com` trên trình duyệt sau khi copy cookie. Lấy cookie mới, dán ngay, không thao tác gì thêm trên tab đăng nhập đó. |
| Không rõ câu trả lời từ cookie hay API | Gõ `/status`, hoặc xem [Provider-chain](#provider-chain-cookie--api1--api2). |
| Đã điền API key nhưng provider-chain không chuyển khi cookie lỗi | Xem log dòng `Cookie Gemini lỗi hoặc treo quá ...s`; kiểm tra key/quota/`GOOGLE_AI_STUDIO_MODEL`, hoặc gõ `/status`. |
| UptimeRobot báo "Down \| 405" | Kiểm tra URL monitor đúng domain — route `/` đã hỗ trợ cả GET/HEAD. |
| Tin nhắn đầu sau 1 lúc không dùng bị chậm | Service đang cold start trên Render, đợi ~30-60s. |
| `/history` báo lỗi kết nối DB | Kiểm tra `DATABASE_URL`, đảm bảo Supabase project chưa bị pause (tự pause sau ~1 tuần không hoạt động ở gói free). |

## Kiểm thử

```bash
pip install -r requirements-dev.txt
pytest tests/ -v
```

Toàn bộ test chạy bằng mock, không cần Postgres/API key thật — bao gồm
provider-chain (chuyển đổi cookie/api1/api2, cooldown quota), trí nhớ dài
hạn, router function calling, các lớp phân tích cổ phiếu (validation,
policy, backtest, indicators), và handler Telegram.
