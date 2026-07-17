import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


ADAPTER_SOURCE = Path(__file__).resolve().parents[1] / "adapters" / "qmt" / "src"
if str(ADAPTER_SOURCE) not in sys.path:
    sys.path.insert(0, str(ADAPTER_SOURCE))

from ai_trade.broker.base import (  # noqa: E402
    BrokerEnvironment,
    BrokerOrderRequest,
    OrderSide,
    OrderStatus,
)
from ai_trade_qmt.adapter import (  # noqa: E402
    QMTReadOnlyBroker,
    QMTSettings,
    _QMTBindings,
    create_broker,
)


CONSTANTS = SimpleNamespace(
    STOCK_BUY=23,
    STOCK_SELL=24,
    ORDER_UNREPORTED=48,
    ORDER_WAIT_REPORTING=49,
    ORDER_REPORTED=50,
    ORDER_REPORTED_CANCEL=51,
    ORDER_PARTSUCC_CANCEL=52,
    ORDER_PART_CANCEL=53,
    ORDER_CANCELED=54,
    ORDER_PART_SUCC=55,
    ORDER_SUCCEEDED=56,
    ORDER_JUNK=57,
    ORDER_UNKNOWN=255,
)


class FakeAccount:
    def __init__(self, account_id, account_type):
        self.account_id = account_id
        self.account_type = account_type


class FakeTrader:
    instance = None
    connect_result = 0
    subscribe_result = 0

    def __init__(self, path, session_id):
        type(self).instance = self
        self.path = path
        self.session_id = session_id
        self.started = False
        self.stopped = False
        self.assets = SimpleNamespace(
            account_id="account-1234",
            cash=75_000.0,
            total_asset=100_000.0,
        )
        self.position_rows = [
            SimpleNamespace(
                account_id="account-1234",
                stock_code="510300.SH",
                volume=200.0,
                can_use_volume=100,
                avg_price=3.95,
                open_price=4.0,
                market_value=800.0,
            )
        ]
        timestamp = int(datetime(2026, 7, 17, 1, 30, tzinfo=timezone.utc).timestamp())
        self.order_rows = [
            SimpleNamespace(
                account_id="account-1234",
                stock_code="510300.SH",
                order_id=101,
                order_time=timestamp,
                order_type=CONSTANTS.STOCK_BUY,
                order_volume=100,
                traded_volume=0,
                price=4.1,
                traded_price=0.0,
                order_status=999,
                status_msg="",
                order_remark="",
            )
        ]
        self.trade_rows = [
            SimpleNamespace(
                account_id="account-1234",
                stock_code="510300.SH",
                order_id=100,
                traded_id="fill-1",
                traded_time=timestamp,
                order_type=CONSTANTS.STOCK_SELL,
                traded_volume=100,
                traded_price=4.0,
                commission=5.0,
                order_remark="",
            )
        ]

    def start(self):
        self.started = True

    def connect(self):
        return self.connect_result

    def subscribe(self, account):
        self.subscribed_account = account
        return self.subscribe_result

    def stop(self):
        self.stopped = True

    def query_stock_asset(self, account):
        return self.assets

    def query_stock_positions(self, account):
        return self.position_rows

    def query_stock_orders(self, account, cancelable_only=False):
        self.cancelable_only = cancelable_only
        return self.order_rows

    def query_stock_trades(self, account):
        return self.trade_rows


def _settings(path: Path) -> QMTSettings:
    return QMTSettings(
        userdata_path=path,
        account_id="account-1234",
        session_id=12345,
    )


def _bindings() -> _QMTBindings:
    FakeTrader.connect_result = 0
    FakeTrader.subscribe_result = 0
    return _QMTBindings(FakeTrader, FakeAccount, CONSTANTS)


class QMTAdapterTests(unittest.TestCase):
    def test_adapter_package_never_bundles_vendor_binaries_or_xtquant(self):
        package_root = Path(__file__).resolve().parents[1] / "adapters" / "qmt"
        banned_suffixes = {".dll", ".dylib", ".pyd", ".so"}
        bundled_binaries = [
            path
            for path in package_root.rglob("*")
            if path.is_file() and path.suffix.lower() in banned_suffixes
        ]
        self.assertEqual(bundled_binaries, [])
        project_text = (package_root / "pyproject.toml").read_text(encoding="utf-8")
        self.assertIn(
            'qmt-readonly = "ai_trade_qmt:create_broker"', project_text
        )
        self.assertNotIn('"xtquant', project_text.casefold())

    def test_read_surface_maps_account_positions_orders_and_fills(self):
        with tempfile.TemporaryDirectory() as temporary:
            broker = QMTReadOnlyBroker(
                _settings(Path(temporary)), bindings=_bindings()
            )

            health = broker.health()
            self.assertTrue(health.connected)
            self.assertFalse(health.trading_session)
            self.assertEqual(broker.account().cash, 75_000.0)
            self.assertEqual(broker.account().equity, 100_000.0)

            positions = broker.positions()
            self.assertEqual(positions[0].symbol, "510300")
            self.assertEqual(positions[0].quantity, 200)
            self.assertEqual(positions[0].available_quantity, 100)
            self.assertEqual(positions[0].average_cost, 3.95)

            orders = broker.open_orders()
            self.assertTrue(FakeTrader.instance.cancelable_only)
            self.assertEqual(orders[0].status, OrderStatus.PENDING_SUBMIT)
            self.assertIn("kept open", orders[0].message)
            self.assertNotIn("account-1234", orders[0].client_order_id)

            fills = broker.fills()
            self.assertEqual(fills[0].side, OrderSide.SELL)
            self.assertEqual(fills[0].commission, 5.0)
            self.assertEqual(fills[0].tax, 0.0)
            self.assertIsNotNone(fills[0].filled_at.tzinfo)
            self.assertEqual(
                broker.fills(fills[0].filled_at + timedelta(seconds=1)), []
            )

            broker.close()
            self.assertTrue(FakeTrader.instance.stopped)

    def test_submit_and_cancel_fail_before_any_vendor_write_method(self):
        with tempfile.TemporaryDirectory() as temporary:
            broker = QMTReadOnlyBroker(
                _settings(Path(temporary)), bindings=_bindings()
            )
            order = BrokerOrderRequest(
                "client-1", "510300", OrderSide.BUY, 100, 4.0
            )
            self.assertFalse(hasattr(FakeTrader.instance, "order_stock"))
            self.assertFalse(hasattr(FakeTrader.instance, "cancel_order_stock"))
            with self.assertRaisesRegex(PermissionError, "read-only"):
                broker.submit_orders([order])
            with self.assertRaisesRegex(PermissionError, "read-only"):
                broker.cancel_order("101")
            broker.close()

    def test_cross_account_data_and_invalid_collections_fail_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            broker = QMTReadOnlyBroker(
                _settings(Path(temporary)), bindings=_bindings()
            )
            FakeTrader.instance.assets.account_id = "different-account"
            with self.assertRaisesRegex(RuntimeError, "different account"):
                broker.account()
            FakeTrader.instance.assets.account_id = "account-1234"
            FakeTrader.instance.position_rows = None
            with self.assertRaisesRegex(RuntimeError, "invalid collection"):
                broker.positions()
            broker.close()

    def test_connection_failure_stops_the_vendor_session(self):
        with tempfile.TemporaryDirectory() as temporary:
            FakeTrader.connect_result = -1
            with self.assertRaisesRegex(RuntimeError, "connection failed"):
                QMTReadOnlyBroker(
                    _settings(Path(temporary)),
                    bindings=_QMTBindings(FakeTrader, FakeAccount, CONSTANTS),
                )
            self.assertTrue(FakeTrader.instance.stopped)

    def test_settings_use_local_environment_without_persisting_values(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            python_path = root / "site-packages"
            (python_path / "xtquant").mkdir(parents=True)
            config = SimpleNamespace(
                raw={
                    "broker": {
                        "mode": "sandbox",
                        "adapter": "qmt-readonly",
                        "account_id": "account-1234",
                    }
                }
            )
            environment = {
                "AI_TRADE_QMT_USERDATA_PATH": str(root),
                "AI_TRADE_QMT_PYTHON_PATH": str(python_path),
                "AI_TRADE_QMT_SESSION_ID": "9876",
            }
            with patch.dict(os.environ, environment, clear=True):
                settings = QMTSettings.from_config(config)
            self.assertEqual(settings.userdata_path, root.resolve())
            self.assertEqual(settings.python_path, python_path.resolve())
            self.assertEqual(settings.session_id, 9876)

    def test_factory_rejects_live_mode_before_loading_xtquant(self):
        with self.assertRaisesRegex(PermissionError, "live mode"):
            create_broker(SimpleNamespace(raw={}), BrokerEnvironment.LIVE)


if __name__ == "__main__":
    unittest.main()
