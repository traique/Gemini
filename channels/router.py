"""Authenticated HTTP bridge used by the local zca-js process."""
import hmac, os
from fastapi import APIRouter, Header, HTTPException, Response
from pydantic import BaseModel
from channels.contracts import ZaloGroupConfig, ZaloGroupMessageRequest, ZaloMessageRequest, ZaloMessageResponse, ZaloOutboxItem
from channels.group_commands import maybe_handle_group_command
from channels import zalo_repository, zalo_session
from core import config
from services.channel_chat_service import handle_channel_text, split_for_zalo
router=APIRouter(prefix="/internal/zalo",tags=["zalo-internal"])
class SessionPayload(BaseModel): cookie:list|dict; imei:str; userAgent:str; accountId:str
class ControllerPayload(BaseModel): controllerId:str
def _secret():return os.getenv("ZALO_BRIDGE_SECRET","").strip()
def _shared_user_id():
 raw=os.getenv("ZALO_SHARED_USER_ID","").strip()
 try:return int(raw) if raw else config.ALLOWED_USER_ID
 except ValueError as exc:raise HTTPException(503,"Invalid ZALO_SHARED_USER_ID") from exc
def _auth(value):
 expected=_secret()
 if not expected:raise HTTPException(503,"Zalo bridge is not configured")
 if not value or not hmac.compare_digest(value,expected):raise HTTPException(403,"Forbidden")
def _controller_env():return os.getenv("ZALO_CONTROLLER_ID","").strip()
async def _controller():return _controller_env() or await zalo_session.load_controller()

@router.get("/session")
async def get_session(x_zalo_bridge_secret:str|None=Header(default=None)):_auth(x_zalo_bridge_secret);return await zalo_session.load_session() or {}
@router.put("/session",status_code=204)
async def put_session(payload:SessionPayload,x_zalo_bridge_secret:str|None=Header(default=None)):_auth(x_zalo_bridge_secret);await zalo_session.save_session(payload.model_dump());return Response(status_code=204)
@router.delete("/session",status_code=204)
async def delete_session(x_zalo_bridge_secret:str|None=Header(default=None)):_auth(x_zalo_bridge_secret);await zalo_session.clear_session();return Response(status_code=204)
@router.get("/controller")
async def get_controller(x_zalo_bridge_secret:str|None=Header(default=None)):_auth(x_zalo_bridge_secret);return {"controllerId":await _controller()}
@router.put("/controller",status_code=204)
async def put_controller(payload:ControllerPayload,x_zalo_bridge_secret:str|None=Header(default=None)):
 _auth(x_zalo_bridge_secret)
 if not payload.controllerId.strip():raise HTTPException(400,"Missing controllerId")
 await zalo_session.save_controller(payload.controllerId);return Response(status_code=204)
@router.delete("/controller",status_code=204)
async def delete_controller(x_zalo_bridge_secret:str|None=Header(default=None)):_auth(x_zalo_bridge_secret);await zalo_session.clear_controller();return Response(status_code=204)
@router.post("/message",response_model=ZaloMessageResponse)
async def receive(payload:ZaloMessageRequest,x_zalo_bridge_secret:str|None=Header(default=None)):
 _auth(x_zalo_bridge_secret);controller=await _controller()
 if not controller or not hmac.compare_digest(payload.sender_id,controller):raise HTTPException(403,"Sender is not allowed")
 result=await maybe_handle_group_command(payload.account_id,payload.text)
 if result is None:result=await handle_channel_text(_shared_user_id(),payload.text.strip())
 return ZaloMessageResponse(messages=[c for m in result.messages for c in split_for_zalo(m)],provider=result.provider)
@router.get("/groups/{account_id}",response_model=list[ZaloGroupConfig])
async def groups(account_id:str,x_zalo_bridge_secret:str|None=Header(default=None)):_auth(x_zalo_bridge_secret);return [ZaloGroupConfig(group_id=g,alias=a) for g,a in await zalo_repository.list_groups(account_id)]
@router.post("/group-message",status_code=204)
async def group_message(payload:ZaloGroupMessageRequest,x_zalo_bridge_secret:str|None=Header(default=None)):_auth(x_zalo_bridge_secret);await zalo_repository.save_group_message(account_id=payload.account_id,group_id=payload.group_id,message_id=payload.message_id,sender_id=payload.sender_id,sender_name=payload.sender_name,text=payload.text,sent_at_ms=payload.sent_at_ms);return Response(status_code=204)
@router.get("/outbox/{account_id}/{recipient_id}",response_model=list[ZaloOutboxItem])
async def outbox(account_id:str,recipient_id:str,x_zalo_bridge_secret:str|None=Header(default=None)):_auth(x_zalo_bridge_secret);return [ZaloOutboxItem(id=r["id"],content=r["content"]) for r in await zalo_repository.get_pending_outbox(account_id,recipient_id)]
@router.post("/outbox/{item_id}/ack",status_code=204)
async def ack(item_id:int,x_zalo_bridge_secret:str|None=Header(default=None)):_auth(x_zalo_bridge_secret);await zalo_repository.mark_outbox_sent(item_id);return Response(status_code=204)
