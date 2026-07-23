import logging
import os
from pathlib import Path

import jinja2
import yaml
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()

_allowed_id_raw = os.getenv("ALLOWED_USER_ID", "0").strip()
ALLOWED_USER_ID = int(_allowed_id_raw) if _allowed_id_raw.isdigit() else 0

# __Secure-1PSID: session token toàn quyền của tài khoản Google. KHÔNG log ra console/file.
GEMINI_SECURE_1PSID = os.getenv("GEMINI_SECURE_1PSID", "").strip()
GEMINI_SECURE_1PSIDTS = os.getenv("GEMINI_SECURE_1PSIDTS", "").strip() or None

GEMINI_PROXY = os.getenv("GEMINI_PROXY", "").strip() or None

# 2 API key cho provider-chain (cookie -> api1 -> api2). GOOGLE_AI_STUDIO_API_KEY
# (tên biến cũ) vẫn được đọc để tương thích ngược, coi như alias của _1 nếu
# GOOGLE_AI_STUDIO_API_KEY_1 chưa được set riêng.
GOOGLE_AI_STUDIO_API_KEY_1 = (
    os.getenv("GOOGLE_AI_STUDIO_API_KEY_1", "").strip()
    or os.getenv("GOOGLE_AI_STUDIO_API_KEY", "").strip()
    or None
)
GOOGLE_AI_STUDIO_API_KEY_2 = os.getenv("GOOGLE_AI_STUDIO_API_KEY_2", "").strip() or None
# Alias giữ tương thích ngược cho code/tài liệu cũ còn tham chiếu tên này.
GOOGLE_AI_STUDIO_API_KEY = GOOGLE_AI_STUDIO_API_KEY_1
GOOGLE_AI_STUDIO_MODEL = os.getenv("GOOGLE_AI_STUDIO_MODEL", "gemini-2.5-flash").strip()

HAS_ANY_AI_STUDIO_KEY = bool(GOOGLE_AI_STUDIO_API_KEY_1 or GOOGLE_AI_STUDIO_API_KEY_2)


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name, str(default)).strip()
    try:
        return float(raw)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name, str(default)).strip()
    try:
        return int(raw)
    except ValueError:
        return default


# Mặc định gemini-webapi refresh __Secure-1PSIDTS mỗi 600s. Giảm nhẹ xuống 540s
# để có biên an toàn trên host ngủ/restart/chậm mạng.
GEMINI_COOKIE_REFRESH_INTERVAL = _env_float("GEMINI_COOKIE_REFRESH_INTERVAL", 540)
GEMINI_COOKIE_CALL_TIMEOUT_SEC = _env_int("GEMINI_COOKIE_CALL_TIMEOUT_SEC", 18)
GEMINI_WEBAPI_DEBUG = _env_bool("GEMINI_WEBAPI_DEBUG", False)

# ─── Provider-chain (cookie -> api1 -> api2) + trí nhớ hội thoại ────────────
# Cookie chết -> chuyển hẳn sang API (không thử lại cookie mỗi tin, chỉ có
# background probe + /usecookie + đổi env mới kích hoạt thử lại cookie).
# API hết quota (429) -> cooldown cố định rồi tự thử lại.
CHAT_HISTORY_TURNS = _env_int("CHAT_HISTORY_TURNS", 8)
CHAT_SESSION_TIMEOUT_SEC = _env_int("CHAT_SESSION_TIMEOUT_SEC", 21600)  # 6 giờ
COOKIE_PROBE_INTERVAL_SEC = _env_int("COOKIE_PROBE_INTERVAL_SEC", 900)  # 15 phút
API_QUOTA_COOLDOWN_SEC = _env_int("API_QUOTA_COOLDOWN_SEC", 3600)  # 60 phút

# Thứ tự ưu tiên thử provider, đọc từ env PROVIDER_ORDER (vd "api1,api2,cookie"
# để dùng API chính thức làm xương sống, cookie chỉ là bonus - xem README
# mục Provider-chain để cân nhắc trước khi đổi: cookie miễn phí/không quota
# nhưng dễ vỡ + rủi ro TOS; API ổn định hơn nhưng có quota giới hạn theo
# free tier của Google AI Studio). Mặc định giữ hành vi cũ: cookie -> api1 -> api2.
_PROVIDER_ORDER_RAW = os.getenv("PROVIDER_ORDER", "cookie,api1,api2").strip()


def _parse_provider_order(raw: str) -> list[str]:
    valid = {"cookie", "api1", "api2"}
    order = [p.strip().lower() for p in raw.split(",") if p.strip()]
    order = [p for p in order if p in valid]
    seen = set()
    deduped = []
    for p in order:
        if p not in seen:
            seen.add(p)
            deduped.append(p)
    # Đảm bảo đủ cả 3 provider (thêm provider bị thiếu vào cuối theo thứ tự
    # mặc định), để không vô tình loại hẳn 1 provider chỉ vì gõ thiếu trong env.
    for p in ("cookie", "api1", "api2"):
        if p not in deduped:
            deduped.append(p)
    return deduped


PROVIDER_ORDER = _parse_provider_order(_PROVIDER_ORDER_RAW)

# ─── Scheduler: reminder + daily digest danh mục (Bước 6) ──────────────────
REMINDER_CHECK_INTERVAL_SEC = _env_int("REMINDER_CHECK_INTERVAL_SEC", 30)
ENABLE_DAILY_DIGEST = _env_bool("ENABLE_DAILY_DIGEST", True)
DAILY_DIGEST_HOUR_VN = _env_int("DAILY_DIGEST_HOUR_VN", 8)

CHAT_SKILL_PATH = Path(os.getenv("CHAT_SKILL_PATH", "chat_skill.yaml").strip())
_CHAT_SKILL_TEMPLATE_PATH = Path(__file__).resolve().parent / "templates" / "chat_skill_prompt.j2"


def load_chat_skill() -> str:
    """Nạp persona/rules cho chat tự nhiên. Định dạng mặc định là YAML có
    cấu trúc (chat_skill.yaml), render qua templates/chat_skill_prompt.j2
    thành system_instruction gửi cho Gemini. Nếu CHAT_SKILL_PATH trỏ tới
    file .txt (cấu hình cũ), đọc thẳng làm văn bản để tương thích ngược."""
    try:
        raw = CHAT_SKILL_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""

    if CHAT_SKILL_PATH.suffix.lower() not in (".yaml", ".yml"):
        return raw.strip()

    try:
        data = yaml.safe_load(raw)
        template = jinja2.Environment(
            loader=jinja2.FileSystemLoader(_CHAT_SKILL_TEMPLATE_PATH.parent),
            trim_blocks=True,
            lstrip_blocks=True,
        ).get_template(_CHAT_SKILL_TEMPLATE_PATH.name)
        return template.render(
            p=data["persona"],
            tv=data["tone_of_voice"],
            rules=data["rules"],
            cm=data["content_modes"],
        ).strip()
    except Exception:
        logger.exception("Lỗi parse/render chat_skill.yaml, dùng nội dung thô làm dự phòng.")
        return raw.strip()


DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
MEDIA_DIR = Path(os.getenv("MEDIA_DIR", "media").strip())

# Timeout cho các request tới Telegram Bot API, đặc biệt là tải ảnh/file.
# Render đôi khi đọc stream từ Telegram chậm hơn timeout mặc định của
# python-telegram-bot, dẫn tới telegram.error.TimedOut ở download_to_drive().
TELEGRAM_CONNECT_TIMEOUT = float(os.getenv("TELEGRAM_CONNECT_TIMEOUT", "30"))
TELEGRAM_READ_TIMEOUT = float(os.getenv("TELEGRAM_READ_TIMEOUT", "90"))
TELEGRAM_WRITE_TIMEOUT = float(os.getenv("TELEGRAM_WRITE_TIMEOUT", "90"))
TELEGRAM_POOL_TIMEOUT = float(os.getenv("TELEGRAM_POOL_TIMEOUT", "30"))
TELEGRAM_MEDIA_RETRIES = int(os.getenv("TELEGRAM_MEDIA_RETRIES", "3"))


def ensure_media_dir() -> None:
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)


WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "").strip()
WEBHOOK_PATH = "/webhook"
DIAGNOSE_PATH = "/diagnose"

WEBHOOK_BASE_URL = (
    os.getenv("WEBHOOK_BASE_URL", "").strip()
    or os.getenv("RENDER_EXTERNAL_URL", "").strip()
)

# Khoá mã hoá đối xứng (Fernet) dùng để mã hoá các giá trị nhạy cảm (vd
# __Secure-1PSIDTS đã rotate) trước khi lưu vào bảng settings trong DB. Tạo
# bằng: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
# Nếu để trống, bot vẫn chạy được nhưng giá trị sẽ lưu dạng plaintext (kém an toàn hơn).
SETTINGS_ENC_KEY = os.getenv("SETTINGS_ENC_KEY", "").strip() or None

# Secret riêng cho endpoint /diagnose (KHÔNG dùng chung với WEBHOOK_SECRET), truyền
# qua header X-Diagnose-Token thay vì query string để tránh lộ qua access log.
DIAGNOSE_SECRET = os.getenv("DIAGNOSE_SECRET", "").strip() or None


def validate(require_webhook: bool = False) -> None:
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
