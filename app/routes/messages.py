"""Message routes."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.session import get_db
from app.dependencies.auth import get_current_user
from app.models.user import User
from app.schemas.common import APIResponse
from app.schemas.message import (
    MessageCreate,
    MessageLog,
    MessageOut,
    SendMessageResponse,
)
from app.services.message_service import MessageService

router = APIRouter(prefix="/conversations", tags=["messages"])


@router.post(
    "/{conversation_id}/messages",
    response_model=APIResponse[SendMessageResponse],
    status_code=status.HTTP_201_CREATED,
)
async def send_message(
    conversation_id: int,
    payload: MessageCreate,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> APIResponse[SendMessageResponse]:
    service = MessageService(session)
    user_msg, bot_msg = await service.send(
        conversation_id, payload.message, current_user.id
    )
    return APIResponse[SendMessageResponse](
        message="message sent",
        data=SendMessageResponse(
            user_message=MessageOut.model_validate(user_msg),
            bot_message=MessageOut.model_validate(bot_msg),
        ),
    )


@router.post(
    "/{conversation_id}/messages/log",
    response_model=APIResponse[SendMessageResponse],
    status_code=status.HTTP_201_CREATED,
)
async def log_messages(
    conversation_id: int,
    payload: MessageLog,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> APIResponse[SendMessageResponse]:
    service = MessageService(session)
    user_msg, bot_msg = await service.log_pair(
        conversation_id,
        payload.user_message,
        payload.bot_message,
        current_user.id,
        citations=payload.citations,
        sources=payload.sources,
    )
    return APIResponse[SendMessageResponse](
        message="messages logged",
        data=SendMessageResponse(
            user_message=MessageOut.model_validate(user_msg),
            bot_message=MessageOut.model_validate(bot_msg),
        ),
    )


@router.get(
    "/{conversation_id}/messages",
    response_model=APIResponse[list[MessageOut]],
)
async def list_messages(
    conversation_id: int,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> APIResponse[list[MessageOut]]:
    service = MessageService(session)
    items = await service.list_in_conversation(
        conversation_id, user_id=current_user.id, limit=limit, offset=offset
    )
    return APIResponse[list[MessageOut]](
        data=[MessageOut.model_validate(m) for m in items],
    )
