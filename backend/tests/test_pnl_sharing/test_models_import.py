def test_models_import():
    from app.models.pnl_sharing import (
        PnlSharingAgreement,
        PnlSharingSettlement,
        AgreementStatus,
        SettlementMode,
        SettlementCadence,
        SharingSettlementStatus,
    )
    assert AgreementStatus.ACTIVE.value == "ACTIVE"
    assert SettlementMode.AUTO.value == "AUTO"
    assert SettlementCadence.MONTHLY.value == "MONTHLY"
    assert SharingSettlementStatus.SETTLED.value == "SETTLED"
