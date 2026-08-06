"""Org/team management - JWT-authenticated (dashboard), operating on the
caller's own organization from their session (no cross-org access)."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends

from ...core.errors import ConflictError, ForbiddenError, NotFoundError
from ...db.session import get_db
from ...dependencies import CurrentSession, get_current_session
from ...models.organization import Organization, OrganizationRole
from ...repositories.organizations import OrganizationRepository
from ...repositories.users import UserRepository
from ...schemas.organization import InviteMemberRequest, OrganizationMemberOut, OrganizationOut, UpdateMemberRoleRequest

router = APIRouter(prefix="/v1/organizations", tags=["organizations"])
_orgs = OrganizationRepository()
_users = UserRepository()

_MANAGE_ROLES = {OrganizationRole.OWNER, OrganizationRole.ADMIN}


def _require_manager(session: CurrentSession) -> None:
    if session.membership.role not in _MANAGE_ROLES:
        raise ForbiddenError("Only organization owners/admins can manage members.")


@router.get("/me", response_model=OrganizationOut)
def get_my_organization(session: CurrentSession = Depends(get_current_session)) -> Organization:
    return session.organization


@router.get("/me/members", response_model=list[OrganizationMemberOut])
def list_members(session: CurrentSession = Depends(get_current_session), db=Depends(get_db)):
    return _orgs.list_members(db, session.organization.id)


@router.post("/me/members", response_model=OrganizationMemberOut, status_code=201)
def add_member(
    payload: InviteMemberRequest, session: CurrentSession = Depends(get_current_session), db=Depends(get_db)
):
    _require_manager(session)
    target_user = _users.get_by_email(db, payload.email)
    if target_user is None:
        raise NotFoundError(
            f"No account exists for {payload.email!r} yet - they need to sign up before being added to this organization."
        )
    if _orgs.get_membership(db, organization_id=session.organization.id, user_id=target_user.id) is not None:
        raise ConflictError("This user is already a member of the organization.")

    member = _orgs.add_member(db, organization_id=session.organization.id, user_id=target_user.id, role=payload.role)
    db.commit()
    db.refresh(member)
    return member


@router.patch("/me/members/{user_id}", response_model=OrganizationMemberOut)
def update_member_role(
    user_id: uuid.UUID, payload: UpdateMemberRoleRequest, session: CurrentSession = Depends(get_current_session), db=Depends(get_db)
):
    _require_manager(session)
    membership = _orgs.get_membership(db, organization_id=session.organization.id, user_id=user_id)
    if membership is None:
        raise NotFoundError("Membership not found.")
    membership.role = payload.role
    db.commit()
    db.refresh(membership)
    return membership


@router.delete("/me/members/{user_id}", status_code=204)
def remove_member(user_id: uuid.UUID, session: CurrentSession = Depends(get_current_session), db=Depends(get_db)) -> None:
    _require_manager(session)
    membership = _orgs.get_membership(db, organization_id=session.organization.id, user_id=user_id)
    if membership is None:
        raise NotFoundError("Membership not found.")
    if membership.role is OrganizationRole.OWNER:
        remaining_owners = [
            m for m in _orgs.list_members(db, session.organization.id)
            if m.role is OrganizationRole.OWNER and m.user_id != user_id
        ]
        if not remaining_owners:
            raise ConflictError("Cannot remove the last owner of an organization.")
    db.delete(membership)
    db.commit()
