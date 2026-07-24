"""Conversation routes."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.session import get_db
from app.dependencies.auth import get_current_user
from app.models.user import User
from app.schemas.common import APIResponse
from app.schemas.conversation import ConversationCreate, ConversationOut
from app.services.conversation_service import ConversationService
from app.services.supabase_auth_service import UnauthorizedError

router = APIRouter(prefix="/conversations", tags=["conversations"])


@router.post(
    "",
    response_model=APIResponse[ConversationOut],
    status_code=status.HTTP_201_CREATED,
)
async def create_conversation(
    payload: ConversationCreate,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> APIResponse[ConversationOut]:
    service = ConversationService(session)
    convo = await service.create(current_user.id, payload.title)
    return APIResponse[ConversationOut](
        message="conversation created",
        data=ConversationOut.model_validate(convo),
    )


@router.get("/users/{user_id}", response_model=APIResponse[list[ConversationOut]])
async def list_user_conversations(
    user_id: int,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> APIResponse[list[ConversationOut]]:
    if user_id != current_user.id:
        raise UnauthorizedError("you cannot access another user's conversations")
    service = ConversationService(session)
    items = await service.list_for_user(user_id, limit=limit, offset=offset)
    return APIResponse[list[ConversationOut]](
        data=[ConversationOut.model_validate(c) for c in items],
    )
