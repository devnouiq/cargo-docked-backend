from __future__ import annotations

import uuid

from sqlalchemy.orm import Session

from ..models.organization import Organization, OrganizationMember, OrganizationRole


class OrganizationRepository:
    def create(self, db: Session, *, name: str, slug: str) -> Organization:
        org = Organization(name=name, slug=slug)
        db.add(org)
        db.flush()
        return org

    def get(self, db: Session, organization_id: uuid.UUID) -> Organization | None:
        return db.get(Organization, organization_id)

    def get_by_slug(self, db: Session, slug: str) -> Organization | None:
        return db.query(Organization).filter_by(slug=slug).one_or_none()

    def add_member(
        self, db: Session, *, organization_id: uuid.UUID, user_id: uuid.UUID, role: OrganizationRole
    ) -> OrganizationMember:
        member = OrganizationMember(organization_id=organization_id, user_id=user_id, role=role)
        db.add(member)
        db.flush()
        return member

    def get_membership(self, db: Session, *, organization_id: uuid.UUID, user_id: uuid.UUID) -> OrganizationMember | None:
        return (
            db.query(OrganizationMember)
            .filter_by(organization_id=organization_id, user_id=user_id)
            .one_or_none()
        )

    def list_members(self, db: Session, organization_id: uuid.UUID) -> list[OrganizationMember]:
        return db.query(OrganizationMember).filter_by(organization_id=organization_id).all()
