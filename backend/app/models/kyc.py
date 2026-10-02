"""KYC submission + admin review.

Distinct from `User.kyc` (the embedded sub-doc that holds verified PAN /
Aadhaar / address text once approved). This collection tracks the full
submission lifecycle: documents uploaded, admin review queue, approval /
rejection with reason, and the audit timeline.
"""

from __future__ import annotations

from datetime import datetime

from beanie import Indexed, PydanticObjectId
from pymongo import ASCENDING, DESCENDING, IndexModel

from app.models._base import StrEnum, TimestampMixin


class KycStatus(StrEnum):
    PENDING = "PENDING"        # user submitted, waiting for admin
    APPROVED = "APPROVED"      # admin accepted
    REJECTED = "REJECTED"      # admin rejected — user can resubmit
    RESUBMIT = "RESUBMIT"      # admin requested specific changes


class KycIdProofType(StrEnum):
    PAN = "PAN"
    AADHAAR = "AADHAAR"
    PASSPORT = "PASSPORT"
    VOTER_ID = "VOTER_ID"
    DRIVING_LICENSE = "DRIVING_LICENSE"


class KycAddressProofType(StrEnum):
    AADHAAR = "AADHAAR"
    UTILITY_BILL = "UTILITY_BILL"
    BANK_STATEMENT = "BANK_STATEMENT"
    PASSPORT = "PASSPORT"
    DRIVING_LICENSE = "DRIVING_LICENSE"


class KycSubmission(TimestampMixin):
    user_id: Indexed(PydanticObjectId)  # type: ignore[valid-type]

    # Identity proof
    id_proof_type: KycIdProofType
    id_proof_number: str | None = None  # PAN / Aadhaar last-4 etc. (optional)
    id_proof_url: str  # /uploads/kyc/<user>/<uuid>.jpg

    # Address proof
    address_proof_type: KycAddressProofType
    address_proof_url: str
    address_text: str  # full address typed by the user

    status: KycStatus = KycStatus.PENDING

    # Review trail
    submitted_at: datetime
    reviewed_at: datetime | None = None
    reviewed_by: PydanticObjectId | None = None  # admin id
    admin_remark: str | None = None
    rejection_reason: str | None = None

    class Settings:
        name = "kyc_submissions"
        indexes = [
            IndexModel([("user_id", ASCENDING), ("status", ASCENDING)]),
            IndexModel([("status", ASCENDING), ("submitted_at", DESCENDING)]),
            IndexModel([("created_at", DESCENDING)]),
        ]
