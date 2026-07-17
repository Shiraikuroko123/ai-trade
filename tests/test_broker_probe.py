import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

from ai_trade.broker.base import (
    Broker,
    BrokerAccessLevel,
    BrokerAccount,
    BrokerCapabilities,
    BrokerEnvironment,
    BrokerFill,
    BrokerHealth,
    BrokerOrderRequest,
    BrokerOrderSnapshot,
    BrokerOperation,
    BrokerPosition,
    OrderSide,
    OrderStatus,
)
from ai_trade.broker.probe import (
    available_broker_adapters,
    compare_configured_broker,
    probe_configured_broker,
)


class ReadOnlyBroker(Broker):
    adapter_name = "read-only-test"
    environment = BrokerEnvironment.SANDBOX
    capabilities = BrokerCapabilities(
        adapter_name=adapter_name,
        access_level=BrokerAccessLevel.READ_ONLY,
        operations=frozenset(
            {
                BrokerOperation.READ_ACCOUNT,
                BrokerOperation.READ_POSITIONS,
                BrokerOperation.READ_ORDERS,
                BrokerOperation.READ_FILLS,
            }
        ),
        environments=frozenset({BrokerEnvironment.SANDBOX}),
    )
    read_only = True
    runtime_environment_verified = False
    qualifying_reconciliation_supported = False
    fill_commission_complete = False
    fill_tax_complete = False

    def __init__(self, account_id="broker-account"):
        self.account_id = account_id
        self.closed = False

    def health(self):
        return BrokerHealth(True, False, "read-only", datetime.now(timezone.utc))

    def account(self):
        return BrokerAccount(self.account_id, "CNY", 1000.0, 1000.0, 2000.0)

    def positions(self):
        return [BrokerPosition("510300", 100, 100, 10.0, 1000.0)]

    def open_orders(self):
        return [
            BrokerOrderSnapshot(
                "external-1",
                "broker-1",
                "510300",
                OrderSide.BUY,
                100,
                0,
                10.0,
                None,
                OrderStatus.SUBMITTED,
                datetime.now(timezone.utc),
            )
        ]

    def fills(self, since=None):
        return [
            BrokerFill(
                "fill-1",
                "broker-1",
                "external-1",
                "510300",
                OrderSide.BUY,
                100,
                10.0,
                0.0,
                0.0,
                datetime.now(timezone.utc),
            )
        ]

    def submit_orders(
        self, orders: list[BrokerOrderRequest]
    ) -> list[BrokerOrderSnapshot]:
        raise PermissionError

    def cancel_order(self, broker_order_id: str) -> BrokerOrderSnapshot:
        raise PermissionError

    def close(self):
        self.closed = True


def _config(account_id="broker-account", mode="sandbox"):
    return SimpleNamespace(
        raw={
            "broker": {
                "mode": mode,
                "adapter": "read-only-test",
                "account_id": account_id,
            }
        }
    )


class BrokerProbeTests(unittest.TestCase):
    def test_probe_is_masked_read_only_and_never_records_evidence(self):
        broker = ReadOnlyBroker()
        with (
            patch(
                "ai_trade.broker.probe.BrokerRegistry.capabilities",
                return_value=broker.capabilities,
            ),
            patch(
                "ai_trade.broker.probe.BrokerRegistry.create", return_value=broker
            ),
        ):
            result = probe_configured_broker(_config())

        self.assertTrue(broker.closed)
        self.assertTrue(result["authority"]["read_only"])
        self.assertFalse(result["authority"]["order_submission_available"])
        self.assertFalse(
            result["evidence"]["qualifying_reconciliation_recorded"]
        )
        self.assertNotIn("broker-account", str(result))
        self.assertTrue(result["account"]["account_hint"].endswith("ount"))
        self.assertEqual(result["open_orders"][0]["status"], "SUBMITTED")

    def test_compare_reports_diagnostics_without_writing_reconciliation(self):
        broker = ReadOnlyBroker()
        state = {
            "account_id": "paper-account",
            "cash": 900.0,
            "positions": {"510300": 200},
        }
        with (
            patch(
                "ai_trade.broker.probe.BrokerRegistry.capabilities",
                return_value=broker.capabilities,
            ),
            patch(
                "ai_trade.broker.probe.BrokerRegistry.create", return_value=broker
            ),
            patch("ai_trade.broker.probe.paper_status", return_value=state),
        ):
            result = compare_configured_broker(_config())

        self.assertTrue(broker.closed)
        self.assertTrue(result["diagnostic_only"])
        self.assertFalse(result["matches_account_and_positions"])
        self.assertFalse(result["qualifying_reconciliation_recorded"])
        self.assertEqual({issue["kind"] for issue in result["issues"]}, {"cash", "position"})

    def test_mismatched_account_closes_connection_and_fails(self):
        broker = ReadOnlyBroker(account_id="other-account")
        with (
            patch(
                "ai_trade.broker.probe.BrokerRegistry.capabilities",
                return_value=broker.capabilities,
            ),
            patch(
                "ai_trade.broker.probe.BrokerRegistry.create", return_value=broker
            ),
            self.assertRaisesRegex(RuntimeError, "does not match"),
        ):
            probe_configured_broker(_config())
        self.assertTrue(broker.closed)

    def test_probe_rejects_non_sandbox_mode_before_discovery(self):
        with (
            patch("ai_trade.broker.probe.BrokerRegistry.create") as create,
            self.assertRaisesRegex(RuntimeError, "sandbox"),
        ):
            probe_configured_broker(_config(mode="live"))
        create.assert_not_called()

    def test_available_adapter_output_is_stable(self):
        capabilities = BrokerCapabilities(
            adapter_name="qmt-readonly",
            access_level=BrokerAccessLevel.READ_ONLY,
            operations=frozenset({BrokerOperation.READ_ACCOUNT}),
            environments=frozenset({BrokerEnvironment.SANDBOX}),
        )
        with patch(
            "ai_trade.broker.probe.BrokerRegistry.descriptions",
            return_value=(capabilities,),
        ):
            result = available_broker_adapters()
        self.assertEqual(result["adapters"], ["qmt-readonly"])
        self.assertEqual(result["capabilities"][0]["access_level"], "read_only")
        self.assertEqual(result["capabilities"][0]["operations"], ["read_account"])
        self.assertEqual(result["discovery_group"], "ai_trade.brokers")

    def test_probe_rejects_undeclared_read_operations_before_broker_io(self):
        capabilities = BrokerCapabilities(
            adapter_name="read-only-test",
            access_level=BrokerAccessLevel.READ_ONLY,
            operations=frozenset({BrokerOperation.READ_ACCOUNT}),
            environments=frozenset({BrokerEnvironment.SANDBOX}),
        )
        with (
            patch(
                "ai_trade.broker.probe.BrokerRegistry.capabilities",
                return_value=capabilities,
            ),
            patch("ai_trade.broker.probe.BrokerRegistry.create") as create,
            self.assertRaisesRegex(PermissionError, "read_fills"),
        ):
            probe_configured_broker(_config())
        create.assert_not_called()


if __name__ == "__main__":
    unittest.main()
