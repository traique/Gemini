# Gemini Telegram Bot (bản cá nhân, full free)

Bot Telegram tạo ảnh / video / content bằng tài khoản **Gemini Pro cá nhân**,
dùng `gemini-webapi` (thư viện reverse-engineered, không phải API chính thức
của Google). Thiết kế cho **1 người dùng**. Có 2 cách chạy:

| Cách chạy | File | Khi nào dùng |
|---|---|---|
| Long polling | `main.py` | Chạy trên máy cá nhân/VPS, test nhanh, không cần public URL |
| Webhook | `web.py` | **Deploy lên Render** (hoặc bất kỳ host yêu cầu Web Service) |

## ⚠️ Đọc trước khi dùng

- `gemini-webapi` giả lập phiên đăng nhập web bằng cookie, **không phải** API
  chính thức của Google → có thể vi phạm Điều khoản dịch vụ của Google. Dùng
  cho mục đích cá nhân, tự chịu rủi ro tài khoản Google có thể bị giới hạn/khoá
  nếu Google phát hiện truy cập bất thường.
- `__Secure-1PSID` là session token **toàn quyền tài khoản Google** của bạn.
  Tuyệt đối không chia sẻ, không commit lên GitHub, không log ra console.
- Bot chỉ cho phép đúng 1 Telegram user ID (`ALLOWED_USER_ID`) sử dụng.

## Vì sao cần Supabase, không dùng SQLite nữa?

Render free tier **không có persistent disk** — ổ đĩa local bị xoá mỗi khi
service ngủ (sau 15 phút không có traffic) hoặc redeploy. Nếu dùng SQLite,
`/history` sẽ mất dữ liệu liên tục. Vì bạn chọn phương án bền hơn, bot giờ
lưu lịch sử vào **Supabase Postgres free** (không tự hết hạn theo thời gian,
khác với Postgres free của Render chỉ tồn tại ~30 ngày).

Ảnh/video bản thân đã an toàn ngay khi gửi vào Telegram (chat + gallery
channel nếu có) — Supabase chỉ lưu phần *lịch sử/metadata* (`/history`).

## Cấu trúc project

```
gemini-telegram-bot/
├── main.py            # Entrypoint LOCAL (long polling)
├── web.py              # Entrypoint RENDER (webhook, FastAPI)
├── config.py           # Đọc biến môi trường
├── db.py               # Supabase Postgres (asyncpg)
├── gemini_client.py    # Wrapper gemini-webapi
├── handlers.py         # Xử lý /anh /history + chat tự nhiên + tra giá/phân tích cổ phiếu
├── render.yaml          # Blueprint để Render tự đọc cấu hình
├── requirements.txt
├── .env.example
└── README.md
```

## Bước 1 — Tạo Supabase project (free)

1. Tạo project mới tại [supabase.com](https://supabase.com) (free plan).
2. Vào **Project Settings → Database → Connect** (hoặc nút **Connect** ở
   trang chủ project).
3. ⚠️ **Quan trọng**: chọn tab **"Session pooler"** (KHÔNG chọn "Direct
   connection"). Từ giữa 2024, Supabase đổi "Direct connection"
   (`db.<ref>.supabase.co:5432`) thành **chỉ phân giải IPv6**, mà hầu hết
   nền tảng deploy (Render, Railway, Vercel...) **không hỗ trợ outbound
   IPv6** → lỗi `Network is unreachable` khi connect. "Session pooler" có
   cả IPv4 nên luôn kết nối được.
4. Copy chuỗi dạng:
   ```
   postgresql://postgres.[project-ref]:[YOUR-PASSWORD]@aws-0-[region].pooler.supabase.com:5432/postgres
   ```
   Thay `[YOUR-PASSWORD]` bằng password bạn đặt lúc tạo project. Chú ý
   username là `postgres.[project-ref]` (có thêm phần project-ref), khác
   với "Direct connection" chỉ là `postgres`.

Bot tự tạo bảng (`prompts`, `results`) khi khởi động lần đầu, không cần
chạy migration tay.

## Bước 2 — Tạo Telegram Bot

1. Chat với [@BotFather](https://t.me/BotFather) → `/newbot` → lấy **Bot Token**.
2. Chat với [@userinfobot](https://t.me/userinfobot) để lấy **Telegram User ID**
   của chính bạn.

## Bước 3 — Lấy cookie Gemini Pro

1. Đăng nhập [gemini.google.com](https://gemini.google.com) bằng tài khoản
   Pro/Advanced (khuyến nghị dùng tab ẩn danh, lấy cookie xong thì đóng tab).
2. `F12` → tab **Network** → reload trang → click 1 request → tìm trong
   **Cookies**: `__Secure-1PSID` và `__Secure-1PSIDTS`.

`gemini-webapi` tự refresh cookie trong nền khi process đang chạy. Nếu bot
báo lỗi đăng nhập, lấy cookie mới theo bước này.

## Bước 4 — (Tùy chọn) Channel gallery riêng

1. Tạo 1 Telegram Channel **private**, add bot vào làm **admin**.
2. Lấy `chat_id` (dạng `-100xxxxxxxxxx`) bằng cách forward 1 tin nhắn từ
   channel đến [@JsonDumpBot](https://t.me/JsonDumpBot).
3. Điền vào `GALLERY_CHANNEL_ID`. Bỏ trống nếu không cần.

## Bước 5A — Deploy lên Render (khuyến nghị cho bạn)

1. Push project này lên 1 repo GitHub (private repo cũng được).
2. Vào [Render Dashboard](https://dashboard.render.com) → **New +** →
   **Blueprint** → chọn repo này. Render sẽ tự đọc `render.yaml`.
3. Điền các biến môi trường được yêu cầu (Render sẽ hỏi vì `render.yaml`
   khai `sync: false` cho từng key):
   - `TELEGRAM_TOKEN`, `ALLOWED_USER_ID`
   - `GEMINI_SECURE_1PSID`, `GEMINI_SECURE_1PSIDTS`
   - `DATABASE_URL` (chuỗi Supabase ở Bước 1)
   - `WEBHOOK_SECRET` — tự tạo bằng:
     ```bash
     python -c "import secrets; print(secrets.token_urlsafe(32))"
     ```
   - `GALLERY_CHANNEL_ID` (tùy chọn)
4. Bấm **Apply** — Render build & deploy. Lúc service khởi động, code tự
   gọi Telegram API để set webhook trỏ về `https://<tên-service>.onrender.com`
   (Render tự cấp biến `RENDER_EXTERNAL_URL`, không cần bạn điền tay).
5. Vào Telegram, gõ `/start` cho bot — nếu service đang "ngủ", lần đầu có
   thể mất 30-60 giây để Render khởi động lại (cold start), từ lần sau
   trong lúc còn "thức" sẽ phản hồi ngay.

**Về việc service bị "ngủ":** đây là giới hạn cố định của Render free tier,
không có cách nào loại bỏ hoàn toàn nếu không trả phí. Muốn giảm tần suất
ngủ, có thể dùng [cron-job.org](https://cron-job.org) (free) để ping
`https://<tên-service>.onrender.com/` mỗi 10 phút — không đảm bảo 100%
nhưng giảm đáng kể số lần phải cold start.

## Bước 5B — Hoặc chạy trên VPS / máy local (long polling)

VPS **không cần webhook/HTTPS/domain** — dùng `main.py` (long polling), đơn
giản hơn nhiều so với deploy Render. Disk trên VPS không bị xoá như Render
free tier, nhưng vẫn nên dùng `DATABASE_URL` Supabase (Bước 1) để không phụ
thuộc việc VPS có Postgres cài sẵn hay không.

### 1. Cài Python & lấy code

```bash
sudo apt update && sudo apt install -y python3 python3-venv git
git clone <URL-repo-cua-ban> gemini-bot
cd gemini-bot
```

### 2. Tạo virtualenv & cài thư viện

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 3. Tạo file `.env`

```bash
cp .env.example .env
nano .env   # điền TELEGRAM_TOKEN, ALLOWED_USER_ID, GEMINI_SECURE_1PSID,
            # GEMINI_SECURE_1PSIDTS, DATABASE_URL. Bỏ trống WEBHOOK_*/PORT.
```

### 4. Test chạy tay trước

```bash
python main.py
```

Thấy log `Bot đang khởi động (long polling, local)...` và `/start` trên
Telegram trả lời được là đã chạy đúng. `Ctrl+C` để dừng rồi chuyển sang chạy
nền ở bước 5.

### 5. Chạy nền bền vững bằng `systemd` (khuyến nghị)

Long polling cần chạy **liên tục 24/7**; chạy tay bằng `python main.py` sẽ
chết khi bạn đóng SSH. Tạo service để tự khởi động lại khi crash và tự chạy
lại khi VPS reboot:

```bash
sudo nano /etc/systemd/system/gemini-bot.service
```

Nội dung (sửa `User` và đường dẫn `WorkingDirectory`/`ExecStart` cho khớp
máy bạn — `whoami` để biết user hiện tại, `pwd` trong thư mục repo để biết
đường dẫn):

```ini
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

Kích hoạt:

```bash
sudo systemctl daemon-reload
sudo systemctl enable gemini-bot
sudo systemctl start gemini-bot
```

### 6. Kiểm tra & xem log

```bash
sudo systemctl status gemini-bot     # xem đang chạy hay lỗi
sudo journalctl -u gemini-bot -f     # xem log realtime (Ctrl+C để thoát)
```

### 7. Cập nhật code sau này

```bash
cd gemini-bot
git pull
source venv/bin/activate
pip install -r requirements.txt      # nếu requirements.txt có thay đổi
sudo systemctl restart gemini-bot
```

**Lưu ý:** không cần mở port/firewall gì cả — long polling là bot **chủ
động** gọi ra Telegram để lấy update, không nhận request từ bên ngoài vào,
nên VPS không cần IP public/domain/HTTPS như khi chạy `web.py` (webhook,
dành cho Render).

## Các lệnh

| Lệnh | Mô tả | Ví dụ |
|---|---|---|
| `/anh <mô tả>` | Tạo ảnh | `/anh Một chú mèo anime uống trà sữa` |
| `/history` | Xem 10 lượt gần nhất | `/history` |
| `/help` | Hiển thị hướng dẫn | `/help` |

Ngoài các lệnh trên, mọi tin nhắn thường (không bắt đầu bằng `/`) đều được
xử lý như chat tự nhiên với Gemini. Nếu tin nhắn nhắc tới 1 mã cổ phiếu Việt
Nam, bot tự trả giá khớp lệnh realtime + % thay đổi lấy từ DNSE; chỉ khi có
từ ngữ yêu cầu phân tích rõ ràng (vd "phân tích giúp anh mã FPT") mới chạy
pipeline phân tích kỹ thuật/cơ bản đầy đủ.

## Xử lý lỗi thường gặp

- **`OSError: [Errno 101] Network is unreachable` lúc startup (db.init_db)**
  → bạn đang dùng "Direct connection" của Supabase (IPv6-only), Render
  không hỗ trợ outbound IPv6. Sửa: lấy lại `DATABASE_URL` từ Supabase,
  chọn tab **"Session pooler"** (xem Bước 1), cập nhật biến môi trường
  trên Render, service sẽ tự redeploy.
- **Lỗi đăng nhập/cookie** → lấy cookie mới theo Bước 3, cập nhật biến môi
  trường trên Render (hoặc `.env` local), service tự redeploy.
- **Video > 50MB không gửi được** → giới hạn cứng của Telegram Bot API,
  không phải lỗi bot. Thử mô tả ngắn hơn / chất lượng thấp hơn.
- **Tin nhắn đầu tiên sau 1 lúc không dùng bị chậm/không phản hồi** → service
  đang cold start trên Render, đợi ~30-60s rồi thử lại; Telegram cũng tự
  retry gửi update trong 1 khoảng thời gian.
- **`/history` báo lỗi kết nối DB** → kiểm tra lại `DATABASE_URL`, đảm bảo
  Supabase project chưa bị pause (project free của Supabase tự pause nếu
  không có hoạt động ~1 tuần — vào dashboard Supabase bấm "Restore" nếu thấy
  project đang paused).

## Mở rộng sau này (không bắt buộc)

- `/model <tên>` — chọn model cụ thể qua `client.list_models()`.
- Cron-job.org ping `/` định kỳ để giảm cold start.
- Chuyển từ cookie-hack sang Gemini API chính thức nếu cần ổn định hơn,
  hoặc nếu sau này muốn chia sẻ bot cho người khác dùng.
