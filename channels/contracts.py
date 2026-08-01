from pydantic import BaseModel, Field


class ZaloMessageRequest(BaseModel):
    account_id: str = Field(min_length=1)
    sender_id: str = Field(min_length=1)
    conversation_id: str = Field(min_length=1)
    message_id: str = Field(min_length=1)
    text: str = Field(min_length=1, max_length=20_000)


class ZaloMessageResponse(BaseModel):
    messages: list[str]
    provider: str | None = None
