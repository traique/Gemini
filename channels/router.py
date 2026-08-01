"""Authenticated HTTP bridge used by the local zca-js process."""
import hmac
import os

from fastapi import APIRouter, Header, HTTPException, Response

from channels.contracts import (
    ZaloGroupConfig,
    ZaloGroupMessageRequest,
    ZaloMessageRequest,
    ZaloMessageResponse,
)
from channels.group_commands import maybe_handle_group_command
from channels import zalo_repository
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


def _authorize_gateway(secret: str | None) -> None:
    expected = _bridge_secret()
    if not expected:
        raise HTTPException(status_code=503, detail="Zalo bridge is not configured")
    if not secret or not hmac.compare_digest(secret, expected):
        raise HTTPException(status_code=403, detail="Forbidden")


def _authorize_controller(secret: str | None, sender_id: str) -> None:
    _authorize_gateway(secret)
    controller = _controller_id()
    if not controller:
        raise HTTPException(status_code=503, detail="Zalo controller is not configured")
    if not hmac.compare_digest(sender_id, controller):
        raise HTTPException(status_code=403, detail="Sender is not allowed")


@router.post("/message", response_model=ZaloMessageResponse)
async def receive_zalo_message(
    payload: ZaloMessageRequest,
    x_zalo_bridge_secret: str | None = Header(default=None),
) -> ZaloMessageResponse:
    _authorize_controller(x_zalo_bridge_secret, payload.sender_id)
    result = await maybe_handle_group_command(payload.account_id, payload.text)
    if result is None:
        result = await handle_channel_text(user_id=_shared_user_id(), text=payload.text.strip())
    chunks: list[str] = []
    for message in result.messages:
        chunks.extend(split_for_zalo(message))
    return ZaloMessageResponse(messages=chunks, provider=result.provider)


@router.get("/groups/{account_id}", response_model=list[ZaloGroupConfig])
async def get_allowed_groups(
    account_id: str,
    x_zalo_bridge_secret: str | None = Header(default=None),
) -> list[ZaloGroupConfig]:
    _authorize_gateway(x_zalo_bridge_secret)
    groups = await zalo_repository.list_groups(account_id)
    return [ZaloGroupConfig(group_id=group_id, alias=alias) for group_id, alias in groups]


@router.post("/group-message", status_code=204)
async def receive_group_message(
    payload: ZaloGroupMessageRequest,
    x_zalo_bridge_secret: str | None = Header(default=None),
) -> Response:
    _authorize_gateway(x_zalo_bridge_secret)
    await zalo_repository.save_group_message(
        account_id=payload.account_id,
        group_id=payload.group_id,
        message_id=payload.message_id,
        sender_id=payload.sender_id,
        sender_name=payload.sender_name,
        text=payload.text,
        sent_at_ms=payload.sent_at_ms,
    )
    return Response(status_code=204)
