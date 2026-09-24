from __future__ import annotations

import json
import sqlite3
import unittest
from datetime import datetime, timezone
from decimal import Decimal

from power_dispatch.api import JsonApplication
from power_dispatch.clock import FrozenClock
from power_dispatch.errors import Conflict, Forbidden
from power_dispatch.planning import AllocationRequest, PricePoint, allocate_capacity, latest_streak
from power_dispatch.service import SupplyService
from power_dispatch.risk import DemandBucket, inventory_coverage, mark_to_market, supply_gap


class PlanningTests(unittest.TestCase):
    def test_latest_down_streak_counts_changes_not_points(self) -> None:
        streak = latest_streak([
            PricePoint("2026-09-18", Decimal("108")),
            PricePoint("2026-09-19", Decimal("105")),
            PricePoint("2026-09-20", Decimal("102")),
            PricePoint("2026-09-21", Decimal("98")),
        ])
        # 四个报价点之间只发生三次下跌，起始报价仍是首个报价点。
        self.assertEqual(streak.direction, "down")
        self.assertEqual(streak.sessions, 3)
        self.assertEqual(streak.start_date, "2026-09-18")
        self.assertEqual(streak.start_close, Decimal("108"))
        self.assertEqual(streak.end_close, Decimal("98"))

    def test_flat_close_interrupts_directional_streak(self) -> None:
        streak = latest_streak([
            PricePoint("2026-09-18", Decimal("108")),
            PricePoint("2026-09-19", Decimal("105")),
            PricePoint("2026-09-20", Decimal("105")),
            PricePoint("2026-09-21", Decimal("103")),
        ])
        self.assertEqual(streak.direction, "down")
        self.assertEqual(streak.sessions, 1)
        self.assertEqual(streak.start_date, "2026-09-20")
        self.assertEqual(streak.percent_change, Decimal("-1.9048"))
        flat = latest_streak([
            PricePoint("2026-09-18", Decimal("100")),
            PricePoint("2026-09-19", Decimal("100")),
            PricePoint("2026-09-20", Decimal("100")),
        ])
        self.assertEqual(flat.direction, "flat")
        self.assertEqual(flat.sessions, 2)
        self.assertEqual(flat.percent_change, Decimal("0.0000"))
        self.assertIsNone(latest_streak([PricePoint("2026-09-18", Decimal("100"))]))

    def test_up_streak_starts_after_last_flat_interruption(self) -> None:
        streak = latest_streak([
            PricePoint("2026-09-18", Decimal("90")),
            PricePoint("2026-09-19", Decimal("92")),
            PricePoint("2026-09-20", Decimal("92")),
            PricePoint("2026-09-21", Decimal("95")),
            PricePoint("2026-09-22", Decimal("98")),
        ])
        self.assertEqual(streak.direction, "up")
        self.assertEqual(streak.sessions, 2)
        self.assertEqual(streak.start_date, "2026-09-20")

    def test_allocation_is_stable_and_does_not_exceed_capacity(self) -> None:
        rows = allocate_capacity(Decimal("100"), [
            AllocationRequest("later", Decimal("80"), 20, "2026-09-24T09:00:00Z"),
            AllocationRequest("first", Decimal("70"), 10, "2026-09-24T10:00:00Z"),
        ])
        self.assertEqual(rows[0]["nomination_id"], "first")
        self.assertEqual(rows[0]["allocated_mwh"], "70.000")
        self.assertEqual(rows[1]["allocated_mwh"], "30.000")

    def test_inventory_coverage_and_supply_gap(self) -> None:
        coverage = inventory_coverage(
            [{"facility_id": "terminal", "product": "gasoline-92", "available_mwh": "250"}],
            [DemandBucket("terminal", "gasoline-92", Decimal("100"), Decimal("20"))],
        )
        self.assertEqual(coverage[0]["coverage_days"], "2.30")
        self.assertTrue(coverage[0]["below_three_days"])
        gap = supply_gap(
            opening_inventory=Decimal("100"),
            confirmed_inbound=Decimal("30"),
            forecast_demand=Decimal("120"),
            protected_reserve=Decimal("40"),
        )
        self.assertEqual(gap["supply_gap"], "30.000")

    def test_mark_to_market_groups_deterministically(self) -> None:
        result = mark_to_market(
            [{"position_id": "p1", "market_index": "PEAK_VALLEY", "quantity_mwh": "100", "entry_price_cny": "105"}],
            {"PEAK_VALLEY": Decimal("98")},
        )
        self.assertEqual(result["unrealized_pnl_cny"], "-700.00")


class SupplyServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.clock = FrozenClock(datetime(2026, 9, 24, 8, 0, tzinfo=timezone.utc))
        self.service = SupplyService(self.connection, self.clock)
        for user_id, role in (("plan", "planner"), ("dispatch", "dispatcher"), ("risk", "risk"), ("audit", "auditor")):
            self.service.create_user(user_id, user_id, role)
        self.service.create_facility("plan", {"facility_id": "field-a", "name": "北部电厂", "kind": "storage", "timezone": "Asia/Shanghai", "capacity_mwh": "500000"})
        self.service.create_facility("plan", {"facility_id": "terminal-b", "name": "沿海终端", "kind": "terminal", "timezone": "Asia/Shanghai", "capacity_mwh": "800000"})
        self.service.create_route("plan", {"route_id": "pipe-a-b", "origin_id": "field-a", "destination_id": "terminal-b", "product": "crude", "daily_capacity": "100000", "loss_basis_points": 25, "transit_hours": 36})

    def tearDown(self) -> None:
        self.connection.close()

    def quote(self, day: int, close: str) -> dict[str, object]:
        return self.service.record_quote("plan", {"market_index": "PEAK_VALLEY", "trade_date": f"2026-09-{day}", "close_cny": close, "source_revision": f"r-{day}", "observed_at": f"2026-09-{day}T21:00:00Z"})

    def test_quote_revisions_preserve_history(self) -> None:
        first = self.quote(23, "98")
        second = self.service.record_quote("plan", {"market_index": "PEAK_VALLEY", "trade_date": "2026-09-23", "close_cny": "97.8", "source_revision": "r-23-corrected", "observed_at": "2026-09-23T22:00:00Z"})
        self.assertNotEqual(first["quote_id"], second["quote_id"])
        rows = self.connection.execute("SELECT * FROM market_index_quotes ORDER BY quote_id").fetchall()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[1]["supersedes_quote_id"], rows[0]["quote_id"])

    def test_summary_counts_six_drops_across_seven_points(self) -> None:
        for day, close in zip(range(18, 25), ("108", "105", "102", "100", "98", "96", "94")):
            self.quote(day, close)
        summary = self.service.price_summary("PEAK_VALLEY")
        streak = summary["latest_streak"]
        self.assertEqual(summary["observations"], 7)
        self.assertEqual((streak["direction"], streak["sessions"]), ("down", 6))
        self.assertEqual((streak["start_date"], streak["start_close"]), ("2026-09-18", "108"))
        self.assertEqual((streak["end_date"], streak["end_close"]), ("2026-09-24", "94"))
        self.assertEqual(streak["percent_change"], "-12.9630")
        self.assertEqual(streak["start_source_revision"], "r-18")
        self.assertEqual(streak["end_source_revision"], "r-24")
        self.assertFalse(summary["window"]["earlier_history"])

    def test_summary_same_day_revision_recomputes_streak_basis(self) -> None:
        for day, close in zip(range(18, 25), ("108", "105", "102", "100", "98", "96", "94")):
            self.quote(day, close)
        corrected = self.service.record_quote("plan", {"market_index": "PEAK_VALLEY", "trade_date": "2026-09-24", "close_cny": "93", "source_revision": "r-24-corrected", "observed_at": "2026-09-24T22:00:00Z"})
        streak = self.service.price_summary("PEAK_VALLEY")["latest_streak"]
        self.assertEqual(streak["sessions"], 6)
        self.assertEqual(streak["end_close"], "93")
        self.assertEqual(streak["percent_change"], "-13.8889")
        self.assertEqual(streak["end_quote_id"], corrected["quote_id"])
        self.assertEqual(streak["end_source_revision"], "r-24-corrected")
        self.assertIsNotNone(streak["start_quote_id"])

    def test_summary_truncated_window_counts_only_changes_inside(self) -> None:
        for day, close in zip(range(18, 25), ("108", "105", "102", "100", "98", "96", "94")):
            self.quote(day, close)
        summary = self.service.price_summary("PEAK_VALLEY", sessions=5)
        self.assertEqual(summary["observations"], 5)
        streak = summary["latest_streak"]
        self.assertEqual(streak["sessions"], 4)
        self.assertEqual(streak["start_date"], "2026-09-20")
        self.assertTrue(summary["window"]["earlier_history"])
        self.assertEqual(summary["window"]["requested_sessions"], 5)

    def test_summary_single_point_has_no_streak(self) -> None:
        self.quote(24, "94")
        summary = self.service.price_summary("PEAK_VALLEY")
        self.assertEqual(summary["observations"], 1)
        self.assertIsNone(summary["latest_streak"])
        self.assertEqual(summary["window"]["start_quote_id"], summary["window"]["end_quote_id"])
        self.assertFalse(summary["window"]["earlier_history"])

    def test_nomination_replay_and_payload_conflict(self) -> None:
        payload = {"nomination_id": "nom-1", "route_id": "pipe-a-b", "shipper_id": "refinery", "service_date": "2026-09-25", "requested_mwh": "80000", "priority": 10, "idempotency_key": "key-1"}
        first = self.service.submit_nomination("dispatch", payload)
        self.assertEqual(first, self.service.submit_nomination("dispatch", payload))
        changed = dict(payload, requested_mwh="81000")
        with self.assertRaises(Conflict):
            self.service.submit_nomination("dispatch", changed)

    def test_outage_reduces_allocation_and_transfer_consumes_inventory(self) -> None:
        self.service.announce_outage("risk", "pipe-a-b", "2026-09-25T00:00:00Z", "2026-09-25T23:59:59Z", "50", "检修")
        for number, requested, priority in ((1, "40000", 10), (2, "30000", 20)):
            self.service.submit_nomination("dispatch", {"nomination_id": f"nom-{number}", "route_id": "pipe-a-b", "shipper_id": f"shipper-{number}", "service_date": "2026-09-25", "requested_mwh": requested, "priority": priority, "idempotency_key": f"key-{number}"})
        allocation = self.service.allocate("dispatch", "pipe-a-b", "2026-09-25")
        self.assertEqual(allocation["available_capacity"], "50000.000")
        self.assertEqual(allocation["allocations"][1]["allocated_mwh"], "10000.000")
        self.service.add_inventory_lot("dispatch", {"lot_id": "lot-1", "facility_id": "field-a", "product": "crude", "grade": "PEAK_VALLEY", "quantity_mwh": "60000", "unit_cost_cny": "91", "received_at": "2026-09-24T06:00:00Z"})
        transfer = self.service.dispatch_transfer("dispatch", "transfer-1", "nom-1", "lot-1", 2)
        self.assertEqual(transfer["loaded_mwh"], "40000.000")
        self.assertEqual(self.service.inventory_lot("lot-1")["available_mwh"], "20000.000")

    def test_scenario_is_approved_and_replayed_by_input(self) -> None:
        self.quote(23, "98")
        self.service.add_inventory_lot("dispatch", {"lot_id": "lot-1", "facility_id": "field-a", "product": "crude", "grade": "PEAK_VALLEY", "quantity_mwh": "60000", "unit_cost_cny": "91", "received_at": "2026-09-24T06:00:00Z"})
        self.service.create_scenario("plan", {"scenario_id": "restart", "name": "机组检修恢复", "market_index_drop_percent": "9", "route_capacity_changes": {"pipe-a-b": "20"}, "demand_changes": {"field-a:crude": "-5"}})
        with self.assertRaises(Forbidden):
            self.service.approve_scenario("plan", "restart", 1)
        self.service.approve_scenario("risk", "restart", 1)
        first = self.service.run_scenario("plan", "restart", "2026-09-23")
        second = self.service.run_scenario("plan", "restart", "2026-09-23")
        self.assertFalse(first["replayed"])
        self.assertTrue(second["replayed"])
        self.assertEqual(first["run_id"], second["run_id"])

    def test_audit_chain_detects_tampering(self) -> None:
        self.assertTrue(self.service.audit_chain("audit")["valid"])
        self.connection.execute("UPDATE supply_audit_events SET payload_json='{}' WHERE event_id=1")
        self.assertFalse(self.service.audit_chain("audit")["valid"])

    def test_api_exposes_browser_free_boundary(self) -> None:
        app = JsonApplication(self.service)
        self.assertEqual(app.handle("GET", "/health").status, 200)
        response = app.handle("GET", "/quotes/summary/PEAK_VALLEY", {"X-Actor-Id": "plan"})
        self.assertEqual(response.status, 404)
        self.assertEqual(response.body["error"]["code"], "not_found")


if __name__ == "__main__":
    unittest.main()
