from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from nightagents.db.models import AssetRecord, AssetRelationshipRecord, EngagementRecord
from nightagents.db.repositories import AuditRepository, OutboxRepository


class RedirectorLifecycleService:
    def __init__(self, db: Session):
        self.db = db
        self.audit = AuditRepository(db)
        self.outbox = OutboxRepository(db)

    def register(
        self,
        engagement_id: str,
        redirector_id: str,
        domain: str,
        logical_route_id: str,
        provider_resource_id: str,
        mythic_profile: str,
        failover_group: str,
    ) -> AssetRecord:
        if self.db.get(EngagementRecord, engagement_id) is None:
            raise ValueError("engagement not found")
        redirector = self._asset(engagement_id, "redirector", redirector_id, redirector_id)
        domain_asset = self._asset(engagement_id, "domain", domain, domain)
        route = self._asset(engagement_id, "logical_route", logical_route_id, logical_route_id)
        provider = self._asset(engagement_id, "provider_resource", provider_resource_id, provider_resource_id)
        profile = self._asset(engagement_id, "mythic_profile", mythic_profile, mythic_profile)
        failover = self._asset(engagement_id, "failover_group", failover_group, failover_group)
        self._rel(engagement_id, redirector.id, "serves", domain_asset.id)
        self._set_route_target(engagement_id, route.id, redirector.id)
        self._rel(engagement_id, redirector.id, "controlled_by", provider.id)
        self._rel(engagement_id, redirector.id, "forwards_to", profile.id)
        self._rel(engagement_id, redirector.id, "member_of", failover.id)
        redirector.state = "registered"
        self.outbox.add("redirector", redirector.id, "redirector.registered", {"redirector_id": redirector.id})
        self.audit.append(
            "lifecycle-controller",
            "redirector.registered",
            "asset",
            redirector.id,
            {"domain": domain, "logical_route": logical_route_id},
            engagement_id,
        )
        self.db.commit()
        return redirector

    def record_health(
        self,
        engagement_id: str,
        redirector_id: str,
        healthy: bool,
        evidence: dict,
    ) -> AssetRecord:
        redirector = self.db.scalar(
            select(AssetRecord).where(
                AssetRecord.engagement_id == engagement_id,
                AssetRecord.asset_type == "redirector",
                AssetRecord.external_id == redirector_id,
            )
        )
        if redirector is None:
            raise ValueError("redirector not found")
        redirector.state = "active" if healthy else "degraded"
        redirector.version += 1
        redirector.attributes_json = {
            **redirector.attributes_json,
            "health_verified": healthy,
            "health_evidence": evidence,
            "health_observed_at": datetime.now(UTC).isoformat(),
        }
        self.audit.append(
            "health-reconciler",
            "redirector.health_recorded",
            "asset",
            redirector.id,
            {"healthy": healthy, "evidence": evidence},
            engagement_id,
        )
        self.db.commit()
        return redirector

    def healthy_failover(self, engagement_id: str, redirector_id: str) -> AssetRecord | None:
        redirector = self.db.scalar(
            select(AssetRecord).where(
                AssetRecord.engagement_id == engagement_id,
                AssetRecord.asset_type == "redirector",
                AssetRecord.external_id == redirector_id,
            )
        )
        if redirector is None:
            return None
        membership = self.db.scalar(
            select(AssetRelationshipRecord).where(
                AssetRelationshipRecord.source_asset_id == redirector.id,
                AssetRelationshipRecord.relationship == "member_of",
            )
        )
        if membership is None:
            return None
        peer_ids = list(
            self.db.scalars(
                select(AssetRelationshipRecord.source_asset_id).where(
                    AssetRelationshipRecord.target_asset_id == membership.target_asset_id,
                    AssetRelationshipRecord.relationship == "member_of",
                    AssetRelationshipRecord.source_asset_id != redirector.id,
                )
            )
        )
        return self.db.scalar(
            select(AssetRecord).where(
                AssetRecord.id.in_(peer_ids),
                AssetRecord.state == "active",
            )
        )

    def _asset(self, engagement_id: str, asset_type: str, external_id: str, name: str) -> AssetRecord:
        asset = self.db.scalar(
            select(AssetRecord).where(
                AssetRecord.engagement_id == engagement_id,
                AssetRecord.asset_type == asset_type,
                AssetRecord.external_id == external_id,
            )
        )
        if asset is None:
            asset = AssetRecord(
                engagement_id=engagement_id,
                asset_type=asset_type,
                external_id=external_id,
                name=name,
            )
            self.db.add(asset)
            self.db.flush()
        return asset

    def _rel(self, engagement_id: str, source: str, relationship: str, target: str) -> None:
        existing = self.db.scalar(
            select(AssetRelationshipRecord).where(
                AssetRelationshipRecord.source_asset_id == source,
                AssetRelationshipRecord.relationship == relationship,
                AssetRelationshipRecord.target_asset_id == target,
            )
        )
        if existing is None:
            self.db.add(
                AssetRelationshipRecord(
                    engagement_id=engagement_id,
                    source_asset_id=source,
                    relationship=relationship,
                    target_asset_id=target,
                )
            )
        elif existing.valid_until is not None:
            existing.valid_from = datetime.now(UTC)
            existing.valid_until = None

    def _set_route_target(self, engagement_id: str, route_id: str, redirector_id: str) -> None:
        active = self.db.scalar(
            select(AssetRelationshipRecord).where(
                AssetRelationshipRecord.source_asset_id == route_id,
                AssetRelationshipRecord.relationship == "targets",
                AssetRelationshipRecord.valid_until.is_(None),
            )
        )
        if active and active.target_asset_id == redirector_id:
            return
        if active:
            active.valid_until = datetime.now(UTC)
        self._rel(engagement_id, route_id, "targets", redirector_id)
