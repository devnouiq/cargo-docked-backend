from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict

from ..models.organization import OrganizationRole


class OrganizationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    slug: str
    created_at: datetime


class OrganizationMemberOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    user_id: uuid.UUID
    role: OrganizationRole
    created_at: datetime


class InviteMemberRequest(BaseModel):
    email: str
    role: OrganizationRole = OrganizationRole.MEMBER


class UpdateMemberRoleRequest(BaseModel):
    role: OrganizationRole
