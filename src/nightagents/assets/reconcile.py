from sqlalchemy import select
from sqlalchemy.orm import Session

from nightagents.contracts.common import ObservationStatus
from nightagents.contracts.mythic import CallbackInventory
from nightagents.db.models import AssetRecord, AssetRelationshipRecord, EngagementRecord
from nightagents.db.repositories import AuditRepository


class MythicInventoryReconciler:
    """Registers observed callbacks and profiles without inferring edge attribution."""

    def __init__(self, db: Session):
        self.db = db
        self.audit = AuditRepository(db)

    def reconcile(self, engagement_id: str, inventory: CallbackInventory) -> dict[str, int | str]:
        engagement = self.db.get(EngagementRecord, engagement_id)
        if engagement is None:
            raise ValueError("engagement not found")
        if inventory.status is ObservationStatus.UNAVAILABLE:
            raise ValueError("Mythic inventory unavailable")
        created = 0
        updated = 0
        for callback in inventory.callbacks:
            profile, profile_created = self._asset(
                engagement_id, "mythic_profile", callback.c2_profile, callback.c2_profile
            )
            item, item_created = self._asset(
                engagement_id, "mythic_callback", callback.callback_id, callback.callback_id
            )
            item.state = "active" if callback.active else "inactive"
            item.attributes_json = {
                "payload_type": callback.payload_type,
                "last_seen": callback.last_seen.isoformat(),
                "expected_checkin_seconds": callback.expected_checkin_seconds,
                "route_attribution": "unverified" if callback.route_id is None else "observed",
                "observed_route_id": callback.route_id,
            }
            self._relationship(engagement_id, item.id, "uses_profile", profile.id)
            created += int(profile_created) + int(item_created)
            updated += int(not item_created)
        self.audit.append(
            "mythic-reconciler",
            "mythic.inventory_reconciled",
            "engagement",
            engagement_id,
            {"callbacks": len(inventory.callbacks), "created_assets": created, "updated_callbacks": updated},
            engagement_id,
        )
        self.db.commit()
        return {"status": "reconciled", "callbacks": len(inventory.callbacks), "created": created, "updated": updated}

    def _asset(
        self, engagement_id: str, asset_type: str, external_id: str, name: str
    ) -> tuple[AssetRecord, bool]:
        asset = self.db.scalar(
            select(AssetRecord).where(
                AssetRecord.engagement_id == engagement_id,
                AssetRecord.asset_type == asset_type,
                AssetRecord.external_id == external_id,
            )
        )
        if asset:
            return asset, False
        asset = AssetRecord(
            engagement_id=engagement_id,
            asset_type=asset_type,
            external_id=external_id,
            name=name,
            state="observed",
        )
        self.db.add(asset)
        self.db.flush()
        return asset, True

    def _relationship(self, engagement_id: str, source: str, relationship: str, target: str) -> None:
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
