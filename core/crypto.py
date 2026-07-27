"""Mã hoá đối xứng (Fernet) cho các giá trị nhạy cảm lưu trong bảng settings
(chủ yếu __Secure-1PSIDTS đã rotate, xem ai/cookie_client.py). Dùng
core.config.SETTINGS_ENC_KEY - để trống thì lưu plaintext (kém an toàn hơn
nhưng bot vẫn chạy được).

Prefix "enc:" PHẢI giữ nguyên: đây là scheme đang chạy thật trong production
(trước refactor nằm trong gemini_client._enc/_dec). Đổi prefix sẽ khiến các
giá trị đã mã hoá sẵn trong DB không giải mã được nữa sau khi deploy.
"""
import logging
from typing import Optional

from core import config

logger = logging.getLogger(__name__)

_PREFIX = "enc:"

_fernet = None
if config.SETTINGS_ENC_KEY:
    try:
        from cryptography.fernet import Fernet

        _fernet = Fernet(config.SETTINGS_ENC_KEY.encode())
    except Exception:
        logger.error(
            "SETTINGS_ENC_KEY không hợp lệ (phải là Fernet key hợp lệ) - "
            "sẽ lưu settings dạng plaintext, hãy sửa lại biến môi trường.",
            exc_info=True,
        )
        _fernet = None


def encrypt(value: str) -> str:
    if not _fernet:
        return value
    try:
        return _PREFIX + _fernet.encrypt(value.encode()).decode()
    except Exception:
        logger.warning("Mã hoá giá trị settings lỗi, lưu plaintext.", exc_info=True)
        return value


def decrypt(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    if not value.startswith(_PREFIX):
        # Giá trị cũ (từ trước khi bật SETTINGS_ENC_KEY) hoặc chưa cấu hình key.
        return value
    if not _fernet:
        logger.warning("Có giá trị đã mã hoá trong DB nhưng thiếu SETTINGS_ENC_KEY để giải mã.")
        return None
    try:
        return _fernet.decrypt(value[len(_PREFIX):].encode()).decode()
    except Exception:
        logger.warning("Giải mã giá trị settings lỗi.", exc_info=True)
        return None
