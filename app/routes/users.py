"""User profile routes.

Authentication is owned by Supabase. This router exposes the existing local
profile used by conversations and messages.
"""

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.session import get_db
from app.dependencies.auth import get_current_user
from app.models.user import User
from app.schemas.common import APIResponse
from app.schemas.user import UserOut, UserProfileUpdate
from app.services.user_service import UserService

router = APIRouter(prefix="/users", tags=["users"])


@router.get("/me", response_model=APIResponse[UserOut])
async def get_me(
    current_user: User = Depends(get_current_user),
) -> APIResponse[UserOut]:
    return APIResponse[UserOut](data=UserOut.model_validate(current_user))


@router.patch("/me", response_model=APIResponse[UserOut])
async def update_me(
    payload: UserProfileUpdate,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> APIResponse[UserOut]:
    user = await UserService(session).update_display_name(current_user, payload.name)
    return APIResponse[UserOut](
        message="display name updated successfully",
        data=UserOut.model_validate(user),
    )
