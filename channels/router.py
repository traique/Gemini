"""Authenticated HTTP bridge used by the local zca-js process.

This endpoint is intentionally channel-specific at the edge, while all Gemini,
stock, memory and tool logic stays in the Python service layer.
"""
import hmac
import os

from fastapi import APIRouter, Header, HTTPException

from channels.contracts import ZaloMessageRequest, ZaloMessageResponse
from core import config
from services.channel_chat_service import handle_channel_text, split_for_zalo

router = APIRouter(prefix="/internal/zalo", tags=["zalo-internal"])


def _bridge_secret() -> str:
    return os.getenv("ZALO_BRIDGE_SECRET", "").strip()


def _controller_id() -> str:
    return os.getenv("ZALO_CONTROLLER_ID", "").strip()


def _shared_user_id() -> int:
    raw = os.getenv("ZALO_SHARED_USER_ID", "").strip()
    if not raw:
        return config.ALLOWED_USER_ID
    try:
        return int(raw)
    except ValueError as exc:
        raise HTTPException(status_code=503, detail="Invalid ZALO_SHARED_USER_ID") from exc


def _authorize(secret: str | None, sender_id: str) -> None:
    expected = _bridge_secret()
    controller = _controller_id()
    if not expected or not controller:
        raise HTTPException(status_code=503, detail="Zalo bridge is not configured")
    if not secret or not hmac.compare_digest(secret, expected):
        raise HTTPException(status_code=403, detail="Forbidden")
    if not hmac.compare_digest(sender_id, controller):
        raise HTTPException(status_code=403, detail="Sender is not allowed")


@router.post("/message", response_model=ZaloMessageResponse)
async def receive_zalo_message(
    payload: ZaloMessageRequest,
    x_zalo_bridge_secret: str | None = Header(default=None),
) -> ZaloMessageResponse:
    _authorize(x_zalo_bridge_secret, payload.sender_id)
    result = await handle_channel_text(
        user_id=_shared_user_id(),
        text=payload.text.strip(),
    )
    chunks: list[str] = []
    for message in result.messages:
        chunks.extend(split_for_zalo(message))
    return ZaloMessageResponse(messages=chunks, provider=result.provider)
