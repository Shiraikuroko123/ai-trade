import csv
import json
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from ai_trade.broker.base import (
    BrokerAccessLevel,
    BrokerAccount,
    BrokerCapabilities,
    BrokerEnvironment,
    BrokerHealth,
    BrokerOrderRequest,
    BrokerOrderSnapshot,
    BrokerPosition,
    BrokerRegistry,
    BrokerOperation,
    OrderSide,
    OrderStatus,
)
from ai_trade.broker.ledger import (
    append_order_events,
    initialize_broker_ledger_scope,
    reserve_order_intents,
    submitted_order_count,
    submitted_order_notional,
)
from ai_trade.broker.live import LiveOrderRouter
from ai_trade.broker.live_guard import (
    LIVE_CONFIRMATION,
    _live_configuration_fingerprint,
    assert_live_submission_allowed,
    evaluate_live_readiness,
)
from ai_trade.broker.mandate import (
    BrokerMandate,
    create_batch_approval,
    order_batch_fingerprint,
    write_batch_approval,
)
from ai_trade.broker.reconciliation import (
    ReconciliationIssue,
    append_reconciliation,
    audit_reconciliations,
    reconcile_account,
)
from ai_trade.broker.scope import create_broker_ledger_scope
from ai_trade.models import Bar, Instrument
from ai_trade.config import _validate_broker


class FakeBroker:
    adapter_name = "mock"
    environment = BrokerEnvironment.LIVE
    capabilities = BrokerCapabilities(
        adapter_name=adapter_name,
        access_level=BrokerAccessLevel.LIVE,
        operations=frozenset(
            {
                BrokerOperation.READ_ACCOUNT,
                BrokerOperation.READ_POSITIONS,
                BrokerOperation.READ_ORDERS,
                BrokerOperation.READ_FILLS,
                BrokerOperation.SUBMIT_ORDERS,
                BrokerOperation.CANCEL_ORDERS,
            }
        ),
        environments=frozenset({BrokerEnvironment.LIVE}),
        runtime_environment_verified=True,
        qualifying_reconciliation_supported=True,
    )

    def __init__(
        self,
        available_cash=100_000.0,
        available_quantity=100,
        account_id="account",
    ):
        self._account = BrokerAccount(
            account_id, "CNY", available_cash, available_cash, available_cash
        )
        self._positions = [
            BrokerPosition("510300", available_quantity, available_quantity, 10.0, 1000.0)
        ]

    def account(self):
        return self._account

    def positions(self):
        return self._positions


class SubmittingBroker(FakeBroker):
    def __init__(self, *args, fail=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.fail = fail
        self.submission_calls = 0

    def health(self):
        return BrokerHealth(
            True,
            True,
            "ready",
            datetime.now(timezone.utc),
        )

    def submit_orders(self, orders):
        self.submission_calls += 1
        if self.fail:
            raise RuntimeError("uncertain broker submission")
        now = datetime.now(timezone.utc)
        return [
            BrokerOrderSnapshot(
                order.client_order_id,
                f"broker-{order.client_order_id}",
                order.symbol,
                order.side,
                order.quantity,
                0,
                order.limit_price,
                None,
                OrderStatus.SUBMITTED,
                now,
            )
            for order in orders
        ]


class FakeMarket:
    def active_symbols(self, on_date):
        return ("510300",)

    def instrument(self, symbol):
        return Instrument(
            symbol,
            "沪深300ETF",
            "SH",
            "equity",
            lot_size=100,
            price_limit_pct=0.10,
            tick_size=0.01,
        )

    def trading_status(self, symbol, on_date):
        return SimpleNamespace(status="normal", tradable=True, price_limit_pct=0.10)

    def previous_bar(self, symbol, on_date):
        return Bar(date(2024, 1, 2), 10.0, 10.0, 10.1, 9.9, 1000, 10_000)


def _router(root: Path, broker=None):
    config = SimpleNamespace(
        raw={
            "broker": {
                "max_order_notional": 5_000.0,
                "max_daily_notional": 10_000.0,
            }
        },
        broker_orders_file=root / "orders.csv",
    )
    return LiveOrderRouter(config, broker or FakeBroker())


def _live_router(root: Path, broker):
    config = SimpleNamespace(
        raw={
            "broker": {
                "mode": "live",
                "adapter": "mock",
                "account_id": "account",
                "max_order_notional": 5_000.0,
                "max_daily_notional": 10_000.0,
            }
        },
        broker_orders_file=root / "orders.csv",
        broker_fills_file=root / "fills.csv",
        broker_ledger_scope_file=root / "ledger-scope.json",
        live_batch_approval_file=root / "batch-approval.json",
    )
    return LiveOrderRouter(config, broker)


def _order(
    order_id="order-1",
    symbol="510300",
    side=OrderSide.BUY,
    quantity=100,
    price=10.0,
):
    return BrokerOrderRequest(order_id, symbol, side, quantity, price)


def _mandate() -> BrokerMandate:
    return BrokerMandate(
        allowed_symbols=frozenset({"510300"}),
        allowed_sides=frozenset({OrderSide.BUY, OrderSide.SELL}),
        max_order_notional=5_000.0,
        max_daily_notional=10_000.0,
        max_orders_per_day=5,
    )


def _readiness() -> dict[str, object]:
    return {
        "config_fingerprint": "a" * 64,
        "authorization": {"mandate": _mandate().public_dict()},
    }


def _authorization(config, paper_fingerprint: str, expires_at: datetime) -> dict:
    approved_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    return {
        "schema_version": 2,
        "approved": True,
        "approved_by": "local-owner",
        "approved_at": approved_at.isoformat(),
        "adapter": "mock",
        "account_id": "account",
        "config_fingerprint": _live_configuration_fingerprint(
            config, paper_fingerprint
        ),
        "expires_at": expires_at.isoformat(),
        "mandate": _mandate().public_dict(),
    }


class BrokerTests(unittest.TestCase):
    def test_order_ledger_is_idempotent_and_tracks_daily_notional(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "orders.csv"
            event = BrokerOrderSnapshot(
                "order-1",
                "broker-1",
                "510300",
                OrderSide.BUY,
                100,
                0,
                10.0,
                None,
                OrderStatus.SUBMITTED,
                datetime(2024, 1, 3, 9, 30, tzinfo=timezone.utc),
            )
            append_order_events(path, [event])
            append_order_events(path, [event])
            with path.open("r", encoding="utf-8", newline="") as handle:
                self.assertEqual(len(list(csv.DictReader(handle))), 1)
            self.assertEqual(submitted_order_notional(path, date(2024, 1, 3)), 1000.0)

    def test_reconciliation_is_idempotent_and_bound_to_configuration(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "reconciliation.csv"
            for index in range(5):
                kwargs = {
                    "on_date": date(2024, 1, 1) + timedelta(days=index),
                    "adapter": "mock",
                    "account_id": "account",
                    "config_fingerprint": "current",
                    "expected_cash": 1000.0,
                    "broker_cash": 1000.0,
                    "issues": [],
                }
                append_reconciliation(path, **kwargs)
                append_reconciliation(path, **kwargs)
            current = audit_reconciliations(path, "mock", "account", 5, "current")
            stale = audit_reconciliations(path, "mock", "account", 5, "stale")
            self.assertTrue(current["eligible"])
            self.assertEqual(current["clean_sessions"], 5)
            self.assertFalse(stale["eligible"])
            self.assertEqual(stale["clean_sessions"], 0)

    def test_live_readiness_rejects_expired_authorization(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = SimpleNamespace(
                raw={
                    "broker": {
                        "mode": "live",
                        "adapter": "mock",
                        "account_id": "account",
                        "sandbox_minimum_reconciliations": 5,
                        "max_order_notional": 5000,
                        "max_daily_notional": 10000,
                    }
                },
                broker_reconciliation_file=root / "reconciliation.csv",
                live_authorization_file=root / "authorization.json",
                live_batch_approval_file=root / "batch-approval.json",
                live_kill_switch_file=root / "kill-switch",
            )
            for index in range(5):
                append_reconciliation(
                    config.broker_reconciliation_file,
                    on_date=date(2024, 1, 1) + timedelta(days=index),
                    adapter="mock",
                    account_id="account",
                    config_fingerprint="fingerprint",
                    expected_cash=1000,
                    broker_cash=1000,
                    issues=[],
                )
            authorization = _authorization(
                config,
                "fingerprint",
                datetime.now(timezone.utc) - timedelta(minutes=1),
            )
            config.live_authorization_file.write_text(
                json.dumps(authorization), encoding="utf-8"
            )
            audit = {
                "eligible_for_broker_sandbox": True,
                "config_fingerprint": "fingerprint",
            }
            with (
                patch.object(BrokerRegistry, "available", return_value=("mock",)),
                patch.object(
                    BrokerRegistry,
                    "capabilities",
                    return_value=FakeBroker.capabilities,
                ),
                patch(
                    "ai_trade.broker.live_guard._config_fingerprint",
                    return_value="fingerprint",
                ),
                patch.dict(
                    "os.environ", {"AI_TRADE_LIVE_CONFIRMATION": LIVE_CONFIRMATION}
                ),
            ):
                expired = evaluate_live_readiness(config, audit)
                self.assertFalse(expired["checks"]["authorization_valid"])
                authorization["expires_at"] = (
                    datetime.now(timezone.utc) + timedelta(hours=1)
                ).isoformat().replace("+00:00", "Z")
                config.live_authorization_file.write_text(
                    json.dumps(authorization), encoding="utf-8"
                )
                ready = evaluate_live_readiness(config, audit)
                config.raw["broker"]["max_daily_notional"] = 20_000
                changed_limits = evaluate_live_readiness(config, audit)
            self.assertTrue(ready["live_ready"])
            self.assertFalse(changed_limits["checks"]["authorization_valid"])

    def test_live_readiness_rejects_stale_paper_configuration(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = SimpleNamespace(
                raw={
                    "broker": {
                        "mode": "live",
                        "adapter": "mock",
                        "account_id": "account",
                        "sandbox_minimum_reconciliations": 5,
                    }
                },
                broker_reconciliation_file=root / "reconciliation.csv",
                live_authorization_file=root / "authorization.json",
                live_batch_approval_file=root / "batch-approval.json",
                live_kill_switch_file=root / "kill-switch",
            )
            for index in range(5):
                append_reconciliation(
                    config.broker_reconciliation_file,
                    on_date=date(2024, 1, 1) + timedelta(days=index),
                    adapter="mock",
                    account_id="account",
                    config_fingerprint="current",
                    expected_cash=1000,
                    broker_cash=1000,
                    issues=[],
                )
            config.live_authorization_file.write_text(
                json.dumps(
                    _authorization(
                        config,
                        "current",
                        datetime.now(timezone.utc) + timedelta(hours=1),
                    )
                ),
                encoding="utf-8",
            )
            with (
                patch.object(BrokerRegistry, "available", return_value=("mock",)),
                patch.object(
                    BrokerRegistry,
                    "capabilities",
                    return_value=FakeBroker.capabilities,
                ),
                patch(
                    "ai_trade.broker.live_guard._config_fingerprint",
                    return_value="current",
                ),
                patch.dict(
                    "os.environ", {"AI_TRADE_LIVE_CONFIRMATION": LIVE_CONFIRMATION}
                ),
            ):
                readiness = evaluate_live_readiness(
                    config,
                    {
                        "eligible_for_broker_sandbox": True,
                        "config_fingerprint": "stale",
                    },
                )
            self.assertFalse(readiness["checks"]["paper_configuration_current"])
            self.assertFalse(readiness["checks"]["paper_gate_passed"])
            self.assertFalse(readiness["live_ready"])

    def test_live_readiness_rejects_a_read_only_adapter(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = SimpleNamespace(
                raw={
                    "broker": {
                        "mode": "live",
                        "adapter": "qmt-readonly",
                        "account_id": "account",
                        "sandbox_minimum_reconciliations": 5,
                        "max_order_notional": 5_000,
                        "max_daily_notional": 10_000,
                    }
                },
                broker_reconciliation_file=root / "reconciliation.csv",
                live_authorization_file=root / "authorization.json",
                live_batch_approval_file=root / "batch-approval.json",
                live_kill_switch_file=root / "kill-switch",
            )
            read_only = BrokerCapabilities(
                adapter_name="qmt-readonly",
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
            with (
                patch.object(
                    BrokerRegistry, "available", return_value=("qmt-readonly",)
                ),
                patch.object(
                    BrokerRegistry, "capabilities", return_value=read_only
                ),
                patch(
                    "ai_trade.broker.live_guard._config_fingerprint",
                    return_value="paper",
                ),
            ):
                readiness = evaluate_live_readiness(
                    config,
                    {
                        "eligible_for_broker_sandbox": True,
                        "config_fingerprint": "paper",
                    },
                )
            self.assertTrue(readiness["checks"]["adapter_installed"])
            self.assertFalse(readiness["checks"]["adapter_live_capable"])
            self.assertEqual(
                readiness["adapter_capabilities"]["access_level"], "read_only"
            )
            self.assertFalse(readiness["live_ready"])

    def test_router_enforces_active_universe_lots_ticks_limits_and_positions(self):
        with tempfile.TemporaryDirectory() as temporary:
            router = _router(Path(temporary))
            market = FakeMarket()
            on_date = date(2024, 1, 3)
            result = router.validate([_order()], market, on_date)
            self.assertEqual(result["batch_notional"], 1000.0)
            cases = (
                (_order(symbol="510500"), "active universe"),
                (_order(quantity=50), "lot size"),
                (_order(price=10.005), "tick size"),
                (_order(price=11.01), "daily range"),
                (_order(side=OrderSide.SELL, quantity=200), "available broker position"),
                (
                    BrokerOrderRequest(
                        "metadata-order",
                        "510300",
                        OrderSide.BUY,
                        100,
                        10.0,
                        metadata={"routing": "adapter-defined"},
                    ),
                    "metadata is unsupported",
                ),
            )
            for order, message in cases:
                with self.subTest(message=message):
                    with self.assertRaisesRegex(ValueError, message):
                        router.validate([order], market, on_date)

            with self.assertRaisesRegex(ValueError, "Cumulative sell quantity"):
                router.validate(
                    [
                        _order("sell-1", side=OrderSide.SELL),
                        _order("sell-2", side=OrderSide.SELL),
                    ],
                    market,
                    on_date,
                )

    def test_router_counts_prior_orders_and_available_cash(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            router = _router(root, FakeBroker(available_cash=500.0))
            with self.assertRaisesRegex(ValueError, "available cash"):
                router.validate([_order()], FakeMarket(), date(2024, 1, 3))

            router = _router(root)
            prior = BrokerOrderSnapshot(
                "prior",
                "broker-prior",
                "510300",
                OrderSide.BUY,
                900,
                0,
                10.0,
                None,
                OrderStatus.SUBMITTED,
                datetime(2024, 1, 3, 9, 0, tzinfo=timezone.utc),
            )
            append_order_events(router.config.broker_orders_file, [prior])
            with self.assertRaisesRegex(ValueError, "daily notional"):
                router.validate([_order(quantity=200)], FakeMarket(), date(2024, 1, 3))

    def test_order_intent_reservation_blocks_retries_and_daily_limit_races(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "orders.csv"
            on_date = date(2024, 1, 3)
            self.assertEqual(
                reserve_order_intents(
                    path,
                    [_order("first", quantity=900)],
                    on_date,
                    10_000,
                ),
                9_000,
            )
            self.assertEqual(submitted_order_count(path, on_date), 1)
            with self.assertRaisesRegex(RuntimeError, "already exists"):
                reserve_order_intents(
                    path,
                    [_order("first")],
                    on_date,
                    10_000,
                )
            with self.assertRaisesRegex(ValueError, "daily notional"):
                reserve_order_intents(
                    path,
                    [_order("second", quantity=200)],
                    on_date,
                    10_000,
                )
            with self.assertRaisesRegex(ValueError, "daily order count"):
                reserve_order_intents(
                    path,
                    [_order("third")],
                    on_date,
                    20_000,
                    max_daily_orders=1,
                )

    def test_uncertain_submission_remains_reserved_and_cannot_be_retried(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            broker = SubmittingBroker(fail=True)
            router = _live_router(root, broker)
            on_date = datetime.now(timezone(timedelta(hours=8))).date()
            with (
                patch(
                    "ai_trade.broker.live.assert_live_submission_allowed",
                    return_value=_readiness(),
                ),
                patch(
                    "ai_trade.broker.live.consume_batch_approval",
                    return_value={"approval_id": "approval_" + "f" * 32},
                ),
            ):
                with self.assertRaisesRegex(RuntimeError, "uncertain broker submission"):
                    router.submit([_order()], FakeMarket(), on_date, {})
                with self.assertRaisesRegex(RuntimeError, "already exists"):
                    router.submit([_order()], FakeMarket(), on_date, {})
            self.assertEqual(broker.submission_calls, 1)
            with router.config.broker_orders_file.open(
                "r", encoding="utf-8", newline=""
            ) as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(rows[0]["status"], OrderStatus.PENDING_SUBMIT.value)

    def test_router_rejects_wrong_live_account_before_submission(self):
        with tempfile.TemporaryDirectory() as temporary:
            broker = SubmittingBroker(account_id="wrong-account")
            router = _live_router(Path(temporary), broker)
            on_date = datetime.now(timezone(timedelta(hours=8))).date()
            with patch(
                "ai_trade.broker.live.assert_live_submission_allowed",
                return_value=_readiness(),
            ):
                with self.assertRaisesRegex(RuntimeError, "account does not match"):
                    router.submit([_order()], FakeMarket(), on_date, {})
            self.assertEqual(broker.submission_calls, 0)

    def test_router_rejects_mismatched_ledger_scope_before_adapter_io(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            broker = SubmittingBroker()
            broker.account = MagicMock(wraps=broker.account)
            broker.positions = MagicMock(wraps=broker.positions)
            broker.health = MagicMock(wraps=broker.health)
            router = _live_router(root, broker)
            initialize_broker_ledger_scope(
                router.config.broker_ledger_scope_file,
                router.config.broker_orders_file,
                router.config.broker_fills_file,
                create_broker_ledger_scope(
                    adapter="mock",
                    account_id="different-account",
                    environment=BrokerEnvironment.LIVE,
                    config_fingerprint="a" * 64,
                    orders_path=router.config.broker_orders_file,
                    fills_path=router.config.broker_fills_file,
                ),
            )
            on_date = datetime.now(timezone(timedelta(hours=8))).date()

            with patch(
                "ai_trade.broker.live.assert_live_submission_allowed",
                return_value=_readiness(),
            ):
                with self.assertRaisesRegex(RuntimeError, "does not match"):
                    router.submit([_order()], FakeMarket(), on_date, {})

            broker.account.assert_not_called()
            broker.positions.assert_not_called()
            broker.health.assert_not_called()
            self.assertEqual(broker.submission_calls, 0)

    def test_router_consumes_an_exact_one_time_batch_approval(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            broker = SubmittingBroker()
            router = _live_router(root, broker)
            order = _order()
            on_date = datetime.now(timezone(timedelta(hours=8))).date()
            readiness = _readiness()
            batch_fingerprint = order_batch_fingerprint(
                [order],
                on_date=on_date,
                adapter="mock",
                account_id="account",
                config_fingerprint=str(readiness["config_fingerprint"]),
            )
            approval = create_batch_approval(
                approved_by="local-owner",
                adapter="mock",
                account_id="account",
                config_fingerprint=str(readiness["config_fingerprint"]),
                batch_fingerprint=batch_fingerprint,
            )
            write_batch_approval(router.config.live_batch_approval_file, approval)
            with patch(
                "ai_trade.broker.live.assert_live_submission_allowed",
                return_value=readiness,
            ):
                submitted = router.submit([order], FakeMarket(), on_date, {})
            self.assertEqual(len(submitted), 1)
            self.assertEqual(broker.submission_calls, 1)
            self.assertFalse(router.config.live_batch_approval_file.exists())
            self.assertTrue(router.config.broker_ledger_scope_file.exists())
            scope_text = router.config.broker_ledger_scope_file.read_text(
                encoding="utf-8"
            )
            self.assertNotIn('"account_id"', scope_text)
            self.assertNotIn('"account"', scope_text)
            self.assertEqual(
                len(list(root.glob("batch-approval.*.consumed.json"))), 1
            )
            with router.config.broker_orders_file.open(
                "r", encoding="utf-8", newline=""
            ) as handle:
                rows = list(csv.DictReader(handle))
            intent = next(
                row for row in rows if row["status"] == OrderStatus.PENDING_SUBMIT.value
            )
            self.assertIn(str(approval["approval_id"]), intent["message"])
            self.assertIn(batch_fingerprint, intent["message"])

    def test_router_rejects_changed_batch_without_reserving_an_intent(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            broker = SubmittingBroker()
            router = _live_router(root, broker)
            on_date = datetime.now(timezone(timedelta(hours=8))).date()
            readiness = _readiness()
            approved_order = _order(quantity=100)
            submitted_order = _order(quantity=200)
            approval = create_batch_approval(
                approved_by="local-owner",
                adapter="mock",
                account_id="account",
                config_fingerprint=str(readiness["config_fingerprint"]),
                batch_fingerprint=order_batch_fingerprint(
                    [approved_order],
                    on_date=on_date,
                    adapter="mock",
                    account_id="account",
                    config_fingerprint=str(readiness["config_fingerprint"]),
                ),
            )
            write_batch_approval(router.config.live_batch_approval_file, approval)
            with (
                patch(
                    "ai_trade.broker.live.assert_live_submission_allowed",
                    return_value=readiness,
                ),
                self.assertRaisesRegex(PermissionError, "exact order batch"),
            ):
                router.submit([submitted_order], FakeMarket(), on_date, {})
            self.assertFalse(router.config.broker_orders_file.exists())
            self.assertTrue(router.config.live_batch_approval_file.exists())
            self.assertEqual(broker.submission_calls, 0)

    def test_final_live_gate_can_stop_a_reserved_order(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            broker = SubmittingBroker()
            router = _live_router(root, broker)
            on_date = datetime.now(timezone(timedelta(hours=8))).date()
            with (
                patch(
                    "ai_trade.broker.live.assert_live_submission_allowed",
                    side_effect=(
                        _readiness(),
                        RuntimeError("kill switch activated"),
                    ),
                ),
                patch(
                    "ai_trade.broker.live.consume_batch_approval",
                    return_value={"approval_id": "approval_" + "f" * 32},
                ),
            ):
                with self.assertRaisesRegex(RuntimeError, "kill switch activated"):
                    router.submit([_order()], FakeMarket(), on_date, {})
            self.assertEqual(broker.submission_calls, 0)
            self.assertEqual(
                submitted_order_notional(router.config.broker_orders_file, on_date),
                1000,
            )

    def test_submission_gate_uses_authoritative_paper_audit(self):
        config = SimpleNamespace()
        authoritative = {
            "account_id": "account",
            "config_fingerprint": "current",
        }
        with (
            patch.dict(
                "os.environ", {"AI_TRADE_LIVE_CONFIRMATION": LIVE_CONFIRMATION}
            ),
            patch(
                "ai_trade.broker.live_guard.audit_paper",
                return_value=authoritative,
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "does not match"):
                assert_live_submission_allowed(
                    config,
                    {"account_id": "other", "config_fingerprint": "current"},
                    SimpleNamespace(),
                )

    def test_broker_configuration_rejects_missing_identity_and_path_collisions(self):
        with self.assertRaisesRegex(ValueError, "adapter"):
            _validate_broker({"mode": "live", "account_id": "account"})
        with self.assertRaisesRegex(ValueError, "must differ"):
            _validate_broker(
                {
                    "mode": "disabled",
                    "orders_file": "state/shared.csv",
                    "fills_file": "state/shared.csv",
                }
            )
        with self.assertRaisesRegex(ValueError, "ledger_scope_file"):
            _validate_broker(
                {
                    "mode": "disabled",
                    "orders_file": "state/shared.json",
                    "ledger_scope_file": "state/shared.json",
                }
            )

    def test_broker_configuration_rejects_resolved_path_aliases(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            shared = root / "state" / "broker_orders.csv"
            for fills_file in (
                "state/nested/../broker_orders.csv",
                str(shared),
            ):
                with self.subTest(fills_file=fills_file), self.assertRaisesRegex(
                    ValueError, "fills_file must differ from broker.orders_file"
                ):
                    _validate_broker(
                        {
                            "orders_file": "state/broker_orders.csv",
                            "fills_file": fills_file,
                        },
                        project_root=root,
                    )

    def test_live_configuration_fingerprint_binds_the_scope_manifest_path(self):
        first = SimpleNamespace(
            raw={"broker": {"ledger_scope_file": "state/first-scope.json"}}
        )
        second = SimpleNamespace(
            raw={"broker": {"ledger_scope_file": "state/second-scope.json"}}
        )
        self.assertNotEqual(
            _live_configuration_fingerprint(first, "paper"),
            _live_configuration_fingerprint(second, "paper"),
        )

    def test_conflicting_reconciliation_fails_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "reconciliation.csv"
            kwargs = {
                "on_date": date(2024, 1, 3),
                "adapter": "mock",
                "account_id": "account",
                "config_fingerprint": "current",
                "expected_cash": 1000.0,
                "broker_cash": 1000.0,
                "issues": [],
            }
            append_reconciliation(path, **kwargs)
            kwargs["issues"] = [ReconciliationIssue("cash", "CNY", 1000, 900)]
            with self.assertRaisesRegex(RuntimeError, "Conflicting reconciliation"):
                append_reconciliation(path, **kwargs)
            audit = audit_reconciliations(path, "mock", "account", 1, "current")
            self.assertFalse(audit["eligible"])
            self.assertTrue(audit["errors"])

    def test_reconciliation_sums_duplicate_broker_position_rows(self):
        account = BrokerAccount("account", "CNY", 1000, 1000, 1200)
        rows = [
            BrokerPosition("510300", 100, 100, 10, 1000),
            BrokerPosition("510300", 100, 100, 10, 1000),
        ]
        self.assertEqual(reconcile_account(1000, {"510300": 200}, account, rows), [])

    def test_registry_supports_legacy_entry_point_mapping(self):
        point = SimpleNamespace(name="mock")
        with patch(
            "ai_trade.broker.base.metadata.entry_points",
            return_value={BrokerRegistry.ENTRY_POINT_GROUP: (point,)},
        ):
            self.assertIs(BrokerRegistry.discover()["mock"], point)

    def test_registry_rejects_undeclared_adapter_before_loading_factory(self):
        point = SimpleNamespace(name="mock", load=MagicMock())
        with (
            patch.object(BrokerRegistry, "discover", return_value={"mock": point}),
            patch.object(BrokerRegistry, "discover_capabilities", return_value={}),
            self.assertRaisesRegex(PermissionError, "environment"),
        ):
            BrokerRegistry.create(
                "mock", SimpleNamespace(), BrokerEnvironment.SANDBOX
            )
        point.load.assert_not_called()

    def test_undeclared_capabilities_fail_closed_before_live_broker_reads(self):
        with tempfile.TemporaryDirectory() as temporary:
            broker = FakeBroker()
            broker.capabilities = BrokerCapabilities(adapter_name="mock")
            router = _router(Path(temporary), broker)
            with self.assertRaisesRegex(PermissionError, "environment"):
                router.validate([_order()], FakeMarket(), date(2024, 1, 3))


if __name__ == "__main__":
    unittest.main()
