"""
Cấu hình tập trung, đọc từ file .env (hoặc biến môi trường trên Render).
Không commit file .env thực tế lên GitHub - chỉ commit .env.example.
"""
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()

_allowed_id_raw = os.getenv("ALLOWED_USER_ID", "0").strip()
ALLOWED_USER_ID = int(_allowed_id_raw) if _allowed_id_raw.isdigit() else 0

GEMINI_SECURE_1PSID = os.getenv("GEMINI_SECURE_1PSID", "").strip()
GEMINI_SECURE_1PSIDTS = os.getenv("GEMINI_SECURE_1PSIDTS", "").strip() or None

GALLERY_CHANNEL_ID = os.getenv("GALLERY_CHANNEL_ID", "").strip() or None

# Optional: proxy cho mọi request tới gemini.google.com (gemini-webapi nhận
# trực tiếp qua curl_cffi, hỗ trợ http://, https://, socks5://...).
# Lý do cần: account dùng Cookie lấy từ Việt Nam, nhưng server chạy ở vùng
# khác (vd Render region) -> Google geo-fence tính năng tạo ảnh theo IP của
# REQUEST chứ không theo nơi cookie được tạo, dù region host gần VN (như
# Singapore) cũng có thể không được Google tính là "VN". Khi gặp lỗi kiểu
# Gemini tự trả lời "có thể tính năng chưa khả dụng ở vị trí của bạn" dù
# cookie còn mới và tài khoản test trên web (từ mạng VN) vẫn tạo ảnh được
# bình thường -> set biến này tới 1 proxy có IP exit tại Việt Nam để thử.
GEMINI_PROXY = os.getenv("GEMINI_PROXY", "").strip() or None

# File chứa "skill" (system prompt) định hướng chat tự nhiên - xem
# chat_skill.txt. Có thể override path bằng biến môi trường nếu muốn.
CHAT_SKILL_PATH = Path(os.getenv("CHAT_SKILL_PATH", "chat_skill.txt").strip())


def load_chat_skill() -> str:
    """Đọc nội dung skill hiện tại từ đĩa. Trả về chuỗi rỗng nếu file không
    tồn tại (khi đó gemini_client sẽ bỏ qua việc gắn Gem, chat chạy bình
    thường không giới hạn phạm vi)."""
    try:
        return CHAT_SKILL_PATH.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return ""

# Supabase Postgres - bền hơn SQLite, không bị mất khi Render free tier
# ngủ/restart (ổ đĩa local trên Render free là ephemeral).
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()

MEDIA_DIR = Path(os.getenv("MEDIA_DIR", "media").strip())


def ensure_media_dir() -> None:
    """Tạo thư mục media nếu chưa có. Gọi rõ ràng lúc khởi động (main.py/web.py
    lifespan) - KHÔNG để side-effect filesystem xảy ra ngay khi import module,
    vì điều đó gây khó test/khó đoán hành vi khi import config ở nơi khác."""
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)


# ---- Webhook: dùng khi deploy lên Render (web.py) ----
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "").strip()
# CHÚ Ý: KHÔNG nhúng secret vào URL path. Telegram đã hỗ trợ secret_token
# riêng qua header "X-Telegram-Bot-Api-Secret-Token" (web.py kiểm tra header
# này) - đó là cơ chế được thiết kế để tránh phải đặt secret trong URL, vì
# URL path thường bị log lại bởi access log của hosting/proxy/CDN. Path cố
# định, không phụ thuộc giá trị secret.
WEBHOOK_PATH = "/webhook"

# Render tự set RENDER_EXTERNAL_URL cho web service (dạng
# https://your-app.onrender.com). Có thể override bằng WEBHOOK_BASE_URL
# nếu dùng custom domain hoặc chạy ở nơi khác.
WEBHOOK_BASE_URL = (
    os.getenv("WEBHOOK_BASE_URL", "").strip()
    or os.getenv("RENDER_EXTERNAL_URL", "").strip()
)

# Render set PORT tự động cho web service (mặc định 10000)
PORT = int(os.getenv("PORT", "10000"))


def validate(require_webhook: bool = False) -> None:
    """Kiểm tra biến môi trường bắt buộc, raise lỗi rõ ràng nếu thiếu."""
    missing = []
    if not TELEGRAM_TOKEN:
        missing.append("TELEGRAM_TOKEN")
    if not ALLOWED_USER_ID:
        missing.append("ALLOWED_USER_ID")
    if not GEMINI_SECURE_1PSID:
        missing.append("GEMINI_SECURE_1PSID")
    if not DATABASE_URL:
        missing.append("DATABASE_URL")

    if require_webhook:
        if not WEBHOOK_SECRET:
            missing.append("WEBHOOK_SECRET")
        if not WEBHOOK_BASE_URL:
            missing.append(
                "WEBHOOK_BASE_URL (hoặc deploy trên Render để tự có RENDER_EXTERNAL_URL)"
            )

    if missing:
        raise RuntimeError(
            "Thiếu biến môi trường bắt buộc: "
            + ", ".join(missing)
            + "\nXem hướng dẫn trong README.md"
        )
