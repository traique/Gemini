# Gemini Personal Assistant — Telegram + Zalo

Trợ lý cá nhân chạy trên **Telegram** và **Zalo**, dùng chung Gemini, trí nhớ, công cụ, phân tích cổ phiếu Việt Nam và Supabase.

Một deployment phục vụ một chủ sở hữu:

- Telegram được khóa bằng `ALLOWED_USER_ID`.
- Zalo dùng tài khoản **B** làm bot; tài khoản **A** được ghép đôi làm controller.
- Python/Uvicorn và Node/zca-js chạy trong **một Docker Web Service** trên Render.

## Tính năng

### Dùng chung cho Telegram và Zalo

- Chat Gemini đa lĩnh vực với persona trong `chat_skill.yaml`.
- Provider-chain: cookie Gemini → AI Studio key 1 → key 2.
- Tra giá và phân tích cổ phiếu Việt Nam.
- Trí nhớ theo phiên và trí nhớ dài hạn trong Supabase.
- Ghi chú, nhắc việc và danh mục qua ngôn ngữ tự nhiên.
- `/gia`, `/prompt`, `/reset`, `/history`, `/memory`, `/forget`, `/notes`, `/model`, `/status`, `/usecookie`.
- Ảnh → prompt bằng Gemini Vision.

### Riêng cho Zalo

- Đăng nhập tài khoản B bằng QR gửi trong Telegram qua `/zalo`.
- Ghép đôi tài khoản A bằng mã dùng một lần, không cần tìm Zalo UID thủ công.
- Theo dõi động các nhóm mà B đang tham gia.
- Chỉ lưu text từ nhóm được bật; B không tự phản hồi trong nhóm.
- Tổng kết theo yêu cầu hoặc tự động lúc 09:00 `Asia/Ho_Chi_Minh`.
- Session B, controller A, tin nhắn nhóm, summary và outbox được lưu trong Supabase.
- Ảnh riêng A → B được tải từ CDN Zalo bằng session của B, giới hạn 8 MB và xóa file tạm sau xử lý.

## Kiến trúc

```text
Telegram ── webhook ──┐
                      ├─ FastAPI / shared services ── Gemini provider-chain
Zalo A ── chat ── B ──┘             │
          zca-js                     ├─ stock pipeline
                                     ├─ memory/tools
10 nhóm Zalo ── B listener ──────────└─ Supabase

Render Docker service
├── Uvicorn: Telegram, bridge, scheduler, Gemini
└── Node.js: zca-js listener và local control server
```

Control server Zalo chỉ bind tại `127.0.0.1:9901`; Render không công khai port này.

## Cảnh báo

- `gemini-webapi` và `zca-js` đều là thư viện không chính thức. Tài khoản Google/Zalo có thể bị giới hạn hoặc khóa.
- `GEMINI_SECURE_1PSID` là session token nhạy cảm. Không commit, không log, không gửi cho người khác.
- Chỉ chạy **một listener** cho tài khoản B. Mở Zalo Web bằng B có thể làm listener trên Render bị ngắt.
- Luôn cấu hình `SETTINGS_ENC_KEY` hợp lệ để mã hóa session trước khi lưu Supabase.
- Dự án thiết kế cho một người dùng, không phải hệ thống multi-tenant.

## Yêu cầu

- Python 3.12.
- Node.js 18 trở lên.
- Telegram Bot Token.
- Supabase Postgres, dùng connection string **Session pooler**.
- Cookie Gemini hoặc ít nhất một Google AI Studio API key.
- Một tài khoản Zalo B riêng nếu bật kênh Zalo.

## Cấu hình bắt buộc

| Biến | Mô tả |
|---|---|
| `TELEGRAM_TOKEN` | Token từ BotFather |
| `ALLOWED_USER_ID` | Telegram user ID duy nhất được phép dùng bot |
| `DATABASE_URL` | Supabase Session pooler URL |
| `WEBHOOK_SECRET` | Secret bảo vệ Telegram webhook trên Render |
| `GEMINI_SECURE_1PSID` | Cookie Gemini; code hiện tại vẫn yêu cầu biến này |
| `GEMINI_SECURE_1PSIDTS` | Cookie phụ của Gemini |
| `SETTINGS_ENC_KEY` | Fernet key mã hóa cookie và session Zalo |

Tạo secret:

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

Tạo Fernet key:

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Fernet key thường dài 44 ký tự và có thể kết thúc bằng `=`. Dán nguyên chuỗi, không thêm dấu nháy hoặc tiền tố tên biến.

## Biến Gemini và Telegram

| Biến | Mặc định | Mô tả |
|---|---:|---|
| `GOOGLE_AI_STUDIO_API_KEY_1` | trống | API fallback thứ nhất |
| `GOOGLE_AI_STUDIO_API_KEY_2` | trống | API fallback thứ hai |
| `GOOGLE_AI_STUDIO_MODEL` | `gemini-2.5-flash` | Model nhánh API |
| `PROVIDER_ORDER` | `cookie,api1,api2` | Thứ tự provider |
| `CHAT_HISTORY_TURNS` | `8` | Lượt chat gần nhất nạp lại |
| `CHAT_SESSION_TIMEOUT_SEC` | `21600` | Timeout phiên chat |
| `COOKIE_PROBE_INTERVAL_SEC` | `900` | Chu kỳ thử lại cookie |
| `API_QUOTA_COOLDOWN_SEC` | `3600` | Cooldown API hết quota |
| `MEDIA_DIR` | `media` | Thư mục file tạm |

Xem toàn bộ cấu hình trong `.env.example` và `render.yaml`.

## Biến Zalo

| Biến | Mặc định | Mô tả |
|---|---:|---|
| `ZALO_ENABLED` | `false` | Bật Node gateway |
| `ZALO_BRIDGE_SECRET` | — | Secret giữa Node và Python |
| `ZALO_CONTROL_PORT` | `9901` | Port loopback, không public |
| `ZALO_BOT_ACCOUNT_ID` | `zalo-bot` | ID logic trước khi B đăng nhập |
| `ZALO_CONTROLLER_ID` | trống | Có thể để trống; `/pair` tự lưu UID A |
| `ZALO_GROUP_REFRESH_MS` | `60000` | Chu kỳ tải allowlist nhóm |
| `ZALO_OUTBOX_POLL_MS` | `15000` | Chu kỳ gửi outbox B → A |
| `ZALO_GROUP_RETENTION_DAYS` | `30` | Số ngày giữ tin nhắn nhóm |
| `ZALO_DAILY_SUMMARY_HOUR` | `9` | Giờ tổng kết tại Việt Nam |
| `ZALO_IMAGE_MAX_BYTES` | `8388608` | Giới hạn ảnh A → B |

`ZALO_COOKIE_JSON`, `ZALO_IMEI` và `ZALO_USER_AGENT` chỉ là fallback. Nếu dùng QR Telegram, có thể để trống.

## Deploy lên Render

### 1. Tạo Supabase

1. Tạo project tại Supabase.
2. Vào **Project Settings → Database → Connect**.
3. Chọn **Session pooler**, không dùng Direct connection IPv6-only.
4. Điền URL vào `DATABASE_URL`.

Ứng dụng tự tạo schema khi khởi động, gồm memory, settings, nhóm Zalo, tin nhắn nhóm, summaries và outbox.

### 2. Tạo Telegram bot

1. Tạo bot với `@BotFather` và lấy `TELEGRAM_TOKEN`.
2. Lấy Telegram ID bằng `@userinfobot` và điền `ALLOWED_USER_ID`.

### 3. Deploy Blueprint

1. Render Dashboard → **New → Blueprint**.
2. Chọn repository; Render đọc `render.yaml` và build `Dockerfile`.
3. Điền các secret bắt buộc.
4. Để `ZALO_ENABLED=false` ở lần deploy đầu.
5. Xác nhận `/` trả `200 OK` và Telegram hoạt động.

### 4. Bật và đăng nhập Zalo B

Trong Render Environment:

```env
ZALO_ENABLED=true
ZALO_BRIDGE_SECRET=<secret-ngẫu-nhiên>
ZALO_CONTROLLER_ID=
```

Save và redeploy. Trong Telegram riêng với bot:

```text
/zalo
```

1. Bot gửi QR.
2. Dùng B quét QR và **bấm xác nhận đăng nhập trên điện thoại**.
3. Gửi lại `/zalo`; bot trả mã ghép đôi 6 chữ số.
4. Từ A nhắn riêng B:

```text
/pair 123456
```

Mã hết hạn sau 5 phút và chỉ dùng một lần. UID của A được mã hóa rồi lưu Supabase.

Đăng xuất và xóa session/controller:

```text
/zalologout
```

## Sử dụng Telegram

| Lệnh | Chức năng |
|---|---|
| `/zalo` | Đăng nhập hoặc xem trạng thái B; tạo mã ghép đôi A |
| `/zalologout` | Đăng xuất B và xóa liên kết A |
| `/prompt <mô tả>` | Viết prompt tạo ảnh |
| `/gia <sản phẩm>` | Tìm và so sánh giá |
| `/reset` | Xóa ngữ cảnh phiên |
| `/history` | Xem lịch sử gần nhất |
| `/memory` | Xem trí nhớ dài hạn |
| `/forget` | Xóa trí nhớ dài hạn |
| `/notes` | Xem ghi chú |
| `/model [tên|auto]` | Xem/đổi model cookie |
| `/status` | Kiểm tra provider-chain |
| `/usecookie` | Thử lại cookie ngay |

Gửi ảnh trực tiếp cho Telegram bot để tạo prompt từ ảnh.

## Sử dụng Zalo A → B

Sau khi ghép đôi, A có thể chat tự nhiên, hỏi cổ phiếu và dùng:

```text
/help
/prompt <mô tả>
/gia <sản phẩm>
/reset
/history
/memory
/forget
/notes
/model <tên|auto>
/status
/usecookie
```

### Ảnh → prompt

Gửi trực tiếp ảnh JPEG, PNG hoặc WebP trong chat riêng A → B. Có thể thêm caption:

```text
mặt tôi
giữ mặt
cô gái 20
```

B tải ảnh bằng session Zalo, gửi binary qua bridge nội bộ, gọi Gemini Vision và xóa file tạm trong `finally`. Ảnh nhóm không được xử lý.

### Quản lý nhóm

Từ A nhắn B:

```text
/nhomzalo
```

Lấy `group_id`, sau đó thêm nhóm:

```text
/themnhom <group_id> <alias>
```

Ví dụ:

```text
/themnhom 1234567890123456789 chung-khoan
```

Các lệnh còn lại:

```text
/nhom
/xoanhom <group_id hoặc alias>
/tongket <alias> 24h
/tongket <alias> 7d
/tongket <alias> homnay
/tongket <alias> homqua
/tongket all 24h
```

Gateway chỉ thu thập **tin nhắn text mới sau khi nhóm được thêm**. Không backfill lịch sử, không lưu media nhóm và không phản hồi trong nhóm.

Xóa nhóm sẽ ngừng theo dõi và xóa dữ liệu liên quan qua cascade.

### Tổng kết 09:00

Scheduler chạy theo `Asia/Ho_Chi_Minh`, tổng kết từng nhóm trong cửa sổ:

```text
09:00 hôm trước → 09:00 hôm nay
```

Summary được đưa vào outbox; B gửi riêng cho A. Unique key theo nhóm/cửa sổ giúp hạn chế gửi trùng khi Render restart.

## Dữ liệu Supabase

Các bảng Zalo được tạo lazy-init:

```text
zalo_groups
zalo_group_messages
zalo_group_summaries
zalo_outbox
```

Session B và controller A được mã hóa rồi lưu trong bảng `settings`. Tin nhắn nhóm được dọn theo `ZALO_GROUP_RETENTION_DAYS`.

## Chạy local

Telegram long polling:

```bash
git clone https://github.com/traique/Gemini.git
cd Gemini
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python main.py
```

Docker giống Render:

```bash
docker build -t gemini-assistant .
docker run --env-file .env -p 10000:10000 gemini-assistant
```

## Kiểm thử

```bash
pip install -r requirements.txt -r requirements-dev.txt
pytest test/ -v

cd zalo-gateway
npm install
npm run check
```

Repository hiện chưa có CI bắt buộc; nên chạy cả Python test và TypeScript check trước khi merge.

## Xử lý lỗi thường gặp

| Triệu chứng | Cách xử lý |
|---|---|
| `Network is unreachable` với Supabase | Đổi từ Direct connection sang Session pooler |
| `SETTINGS_ENC_KEY ... Incorrect padding` | Tạo Fernet key mới, dán nguyên 44 ký tự và giữ dấu `=` cuối |
| `[zalo] gateway disabled` | Đặt `ZALO_ENABLED=true` rồi redeploy |
| `ECONNREFUSED 127.0.0.1:10000` lúc startup | Node sẽ retry trong lúc chờ Uvicorn; chỉ đáng lo nếu lặp liên tục |
| `/zalo` gửi lại QR sau khi quét | Bấm xác nhận đăng nhập trên điện thoại B; dùng bản mới nhất |
| B không phản hồi A | Kiểm tra `/zalo` đã báo kết nối và ghép đôi; không mở Zalo Web bằng B |
| `/nhomzalo` không có nhóm | B chưa tham gia nhóm hoặc listener mất kết nối |
| Tổng kết trống | Chỉ tin nhắn text phát sinh sau `/themnhom` mới được lưu |
| Ảnh Zalo không tải được | Kiểm tra hostname/HTTP/MIME trong lỗi đã được làm sạch; ảnh phải ≤8 MB và là JPEG/PNG/WebP |
| TypeScript lỗi export `zca-js` | Đảm bảo đang deploy commit mới có `zca-js-compat.d.ts` |
| Telegram webhook hoạt động nhưng Zalo không chạy | Kiểm tra log Supervisor và biến `ZALO_ENABLED` |

## Bảo mật vận hành

- Không commit `.env`, cookie, QR, `zalo-session.json` hoặc file media.
- Không log URL CDN Zalo đầy đủ vì URL có thể chứa token.
- Không public port `9901`.
- Dùng secret khác nhau cho webhook, diagnose và Zalo bridge.
- Không đổi/mất `SETTINGS_ENC_KEY` sau khi đã lưu ciphertext.
- Nếu nghi session lộ, dùng `/zalologout`, đổi secret và đăng nhập QR lại.

## Tài liệu thêm

- `docs/zalo-render.md` — rollout/rollback Zalo trên Render.
- `zalo-gateway/README.md` — chi tiết gateway Node.
- `.env.example` và `render.yaml` — danh sách biến môi trường.

## License và trách nhiệm

Dùng cho mục đích cá nhân/nội bộ. Người vận hành tự chịu trách nhiệm về điều khoản sử dụng của Google, Telegram, Zalo và các nguồn dữ liệu thị trường.
