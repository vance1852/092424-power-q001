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
    def test_latest_down_streak_counts_changes_between_settlement_days(self) -> None:
        streak = latest_streak([
            PricePoint("2026-09-18", Decimal("108"), "r18"),
            PricePoint("2026-09-19", Decimal("105"), "r19"),
            PricePoint("2026-09-20", Decimal("102"), "r20"),
            PricePoint("2026-09-21", Decimal("98"), "r21"),
        ])
        self.assertEqual(streak.direction, "down")
        # 四个报价点之间只有三次相邻结算日变化，而不是四个报价点。
        self.assertEqual(streak.changes, 3)
        self.assertEqual(streak.start_date, "2026-09-18")
        self.assertEqual(streak.start_close, Decimal("108"))
        self.assertEqual(streak.end_date, "2026-09-21")
        self.assertEqual(streak.end_close, Decimal("98"))
        self.assertEqual(streak.percent_change, Decimal("-9.2593"))
        self.assertEqual(streak.start_revision, "r18")
        self.assertEqual(streak.end_revision, "r21")
        self.assertFalse(streak.truncated)

    def test_six_declines_across_seven_points_are_six_changes(self) -> None:
        streak = latest_streak([
            PricePoint(f"2026-09-{day:02d}", Decimal(str(close)))
            for day, close in enumerate((108, 105, 102, 100, 98, 96, 94), start=18)
        ])
        self.assertEqual(streak.direction, "down")
        self.assertEqual(streak.changes, 6)
        self.assertEqual(streak.start_date, "2026-09-18")
        self.assertEqual(streak.start_close, Decimal("108"))
        self.assertEqual(streak.end_close, Decimal("94"))

    def test_up_streak_counts_edges_and_uses_first_edge_base(self) -> None:
        streak = latest_streak([
            PricePoint("2026-09-18", Decimal("94")),
            PricePoint("2026-09-19", Decimal("96")),
            PricePoint("2026-09-20", Decimal("100")),
        ])
        self.assertEqual(streak.direction, "up")
        self.assertEqual(streak.changes, 2)
        self.assertEqual(streak.start_date, "2026-09-18")
        self.assertEqual(streak.start_close, Decimal("94"))
        self.assertEqual(streak.end_close, Decimal("100"))

    def test_flat_is_one_change_and_breaks_a_down_run(self) -> None:
        streak = latest_streak([
            PricePoint("2026-09-18", Decimal("108")),
            PricePoint("2026-09-19", Decimal("105")),
            PricePoint("2026-09-20", Decimal("105")),
        ])
        self.assertEqual(streak.direction, "flat")
        self.assertEqual(streak.changes, 1)
        self.assertEqual(streak.start_date, "2026-09-19")
        self.assertEqual(streak.end_date, "2026-09-20")
        self.assertEqual(streak.start_close, Decimal("105"))
        self.assertEqual(streak.end_close, Decimal("105"))
        self.assertEqual(streak.percent_change, Decimal("0.0000"))

    def test_rebound_breaks_the_down_streak(self) -> None:
        streak = latest_streak([
            PricePoint("2026-09-18", Decimal("108")),
            PricePoint("2026-09-19", Decimal("105")),
            PricePoint("2026-09-20", Decimal("106")),
        ])
        self.assertEqual(streak.direction, "up")
        self.assertEqual(streak.changes, 1)
        self.assertEqual(streak.start_date, "2026-09-19")

    def test_single_point_has_no_change(self) -> None:
        self.assertIsNone(latest_streak([PricePoint("2026-09-18", Decimal("108"))]))
        self.assertIsNone(latest_streak([]))

    def test_streak_marks_window_truncation_from_preceding_point(self) -> None:
        window = [
            PricePoint("2026-09-22", Decimal("102")),
            PricePoint("2026-09-23", Decimal("98")),
            PricePoint("2026-09-24", Decimal("94")),
        ]
        preceded = latest_streak(
            window, preceding_point=PricePoint("2026-09-21", Decimal("105"))
        )
        self.assertEqual(preceded.changes, 2)
        self.assertTrue(preceded.truncated)
        bounded = latest_streak(
            window, preceding_point=PricePoint("2026-09-21", Decimal("100"))
        )
        self.assertEqual(bounded.changes, 2)
        self.assertFalse(bounded.truncated)

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

    def test_price_summary_six_declines_revision_and_window(self) -> None:
        for day, close in ((18, "108"), (19, "105"), (20, "102"), (21, "100"), (22, "98"), (23, "96"), (24, "94")):
            self.quote(day, close)
        summary = self.service.price_summary("PEAK_VALLEY")
        streak = summary["latest_streak"]
        self.assertEqual(streak["direction"], "down")
        self.assertEqual(streak["changes"], 6)
        self.assertEqual(streak["start_date"], "2026-09-18")
        self.assertEqual(streak["start_close"], "108")
        self.assertEqual(streak["end_close"], "94")
        self.assertEqual(streak["start_revision"], "r-18")
        self.assertEqual(streak["end_revision"], "r-24")
        self.assertFalse(streak["truncated"])
        self.assertEqual(summary["latest"]["source_revision"], "r-24")

        # 同日修订：摘要采用最新来源版本并按修订报价重算累计跌幅。
        self.service.record_quote("plan", {"market_index": "PEAK_VALLEY", "trade_date": "2026-09-24", "close_cny": "94.5", "source_revision": "r-24-corrected", "observed_at": "2026-09-24T22:30:00Z"})
        revised = self.service.price_summary("PEAK_VALLEY")["latest_streak"]
        self.assertEqual(revised["changes"], 6)
        self.assertEqual(revised["end_close"], "94.5")
        self.assertEqual(revised["end_revision"], "r-24-corrected")
        self.assertEqual(revised["percent_change"], "-12.5000")

        # 查询窗口截断：最近三点内是两次下跌，但窗口外仍同向延续。
        window = self.service.price_summary("PEAK_VALLEY", sessions=3)["latest_streak"]
        self.assertEqual(window["changes"], 2)
        self.assertEqual(window["start_date"], "2026-09-22")
        self.assertTrue(window["truncated"])

        # 平盘打断：新增一个结算日且报价与前一日相同，只计一次平盘变化。
        self.service.record_quote("plan", {"market_index": "PEAK_VALLEY", "trade_date": "2026-09-25", "close_cny": "94.5", "source_revision": "r-25", "observed_at": "2026-09-25T21:00:00Z"})
        flat = self.service.price_summary("PEAK_VALLEY")["latest_streak"]
        self.assertEqual(flat["direction"], "flat")
        self.assertEqual(flat["changes"], 1)
        self.assertEqual(flat["start_date"], "2026-09-24")
        self.assertEqual(flat["end_date"], "2026-09-25")

        # 不足两个点：不存在相邻结算日变化，连续段为空。
        self.assertIsNone(self.service.price_summary("PEAK_VALLEY", sessions=1)["latest_streak"])

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
