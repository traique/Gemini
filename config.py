import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()

_allowed_id_raw = os.getenv("ALLOWED_USER_ID", "0").strip()
ALLOWED_USER_ID = int(_allowed_id_raw) if _allowed_id_raw.isdigit() else 0

# __Secure-1PSID: session token toàn quyền của tài khoản Google. KHÔNG log ra console/file.
GEMINI_SECURE_1PSID = os.getenv("GEMINI_SECURE_1PSID", "").strip()
GEMINI_SECURE_1PSIDTS = os.getenv("GEMINI_SECURE_1PSIDTS", "").strip() or None

GALLERY_CHANNEL_ID = os.getenv("GALLERY_CHANNEL_ID", "").strip() or None
GEMINI_PROXY = os.getenv("GEMINI_PROXY", "").strip() or None

CHAT_SKILL_PATH = Path(os.getenv("CHAT_SKILL_PATH", "chat_skill.txt").strip())


def load_chat_skill() -> str:
    try:
        return CHAT_SKILL_PATH.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return ""


DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
MEDIA_DIR = Path(os.getenv("MEDIA_DIR", "media").strip())


def ensure_media_dir() -> None:
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)


WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "").strip()
WEBHOOK_PATH = "/webhook"
DIAGNOSE_PATH = "/diagnose"

WEBHOOK_BASE_URL = (
    os.getenv("WEBHOOK_BASE_URL", "").strip()
    or os.getenv("RENDER_EXTERNAL_URL", "").strip()
)

PORT = int(os.getenv("PORT", "10000"))


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
