import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path

from self_inspection_core.api import route
from self_inspection_core.clock import MutableClock
from self_inspection_core.errors import ConflictError, NotFoundError, PermissionDenied, ValidationError
from self_inspection_core.inspection import InspectionService
from self_inspection_core.service import DomainService
from self_inspection_core.storage import Database

HASH_A = "a" * 64
HASH_B = "b" * 64
HASH_C = "c" * 64


class InspectionTest(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        # 2026-09-25 10:00 Asia/Shanghai
        self.clock = MutableClock(datetime(2026, 9, 25, 2, 0, tzinfo=timezone.utc))
        self.service = DomainService(self.database, self.clock)
        self.inspection = InspectionService(self.service)
        self.service.register_organization(request_id="r-org", actor_id="bootstrap",
                                           organization_id="o1", name="家具企业")
        self.service.register_actor(request_id="r-admin", actor_id="bootstrap", new_actor_id="a1",
                                    display_name="管理员", role="admin", organization_id="o1")
        self.service.register_actor(request_id="r-op", actor_id="a1", new_actor_id="op1",
                                    display_name="环保负责人", role="operator", organization_id="o1")
        self.service.register_actor(request_id="r-rv", actor_id="a1", new_actor_id="rv1",
                                    display_name="监管人员", role="reviewer", organization_id="o1")
        self.service.register_actor(request_id="r-au", actor_id="a1", new_actor_id="au1",
                                    display_name="审计员", role="auditor", organization_id="o1")
        self.service.register_site(request_id="r-site", actor_id="op1", site_id="s1",
                                   organization_id="o1", name="厂区", timezone_name="Asia/Shanghai")
        self.service.record_domain_data(request_id="r-w1", actor_id="op1", site_id="s1",
                                        category="workshop_registry", external_key="ws-1",
                                        data={"name": "喷漆车间"})
        self.service.record_domain_data(request_id="r-t1", actor_id="op1", site_id="s1",
                                        category="treatment_registry", external_key="tf-1",
                                        data={"name": "废气处理设施"})

    def tearDown(self):
        self.database.close()

    def _evidence(self, request_id="r-e1", item_key="workshop:ws-1", evidence_hash=HASH_A,
                  captured_at="2026-09-25T09:30:00+08:00", storage_ref="oss://b/1.jpg",
                  actor_id="op1"):
        return self.inspection.submit_evidence(
            request_id=request_id, actor_id=actor_id, site_id="s1", item_key=item_key,
            evidence_hash=evidence_hash, captured_at=captured_at, storage_ref=storage_ref)

    def test_checklist_generated_per_local_date(self):
        checklist = self.inspection.generate_checklist(request_id="r-c1", actor_id="op1", site_id="s1")
        self.assertEqual(1, checklist["version"])
        self.assertEqual("2026-09-25", checklist["local_date"])
        self.assertEqual(["treatment:tf-1", "workshop:ws-1"],
                         [item["item_key"] for item in checklist["items"]])
        again = self.inspection.generate_checklist(request_id="r-c2", actor_id="op1", site_id="s1")
        self.assertEqual(checklist["checklist_id"], again["checklist_id"])
        self.assertEqual(1, again["version"])

    def test_checklist_version_bumps_when_registry_changes(self):
        first = self.inspection.generate_checklist(request_id="r-c1", actor_id="op1", site_id="s1")
        self.service.record_domain_data(request_id="r-w2", actor_id="op1", site_id="s1",
                                        category="workshop_registry", external_key="ws-2",
                                        data={"name": "打磨车间"})
        second = self.inspection.generate_checklist(request_id="r-c3", actor_id="op1", site_id="s1")
        self.assertEqual(2, second["version"])
        self.assertNotEqual(first["checklist_id"], second["checklist_id"])
        self.assertEqual(3, second["item_count"])
        # 旧版本仍然保留，历史决定引用的版本可还原
        decision = self.inspection.record_decision(
            request_id="r-d1", actor_id="rv1", site_id="s1", local_date="2026-09-25",
            item_key="workshop:ws-2", action="request_supplement", note="请补证")
        self.assertEqual(2, decision["checklist_version"])

    def test_disabled_registry_record_is_excluded(self):
        self.service.record_domain_data(request_id="r-w3", actor_id="op1", site_id="s1",
                                        category="workshop_registry", external_key="ws-3",
                                        data={"name": "停用车间", "enabled": False})
        checklist = self.inspection.generate_checklist(request_id="r-c1", actor_id="op1", site_id="s1")
        self.assertEqual(["treatment:tf-1", "workshop:ws-1"],
                         [item["item_key"] for item in checklist["items"]])

    def test_same_request_replays_original_receipt(self):
        first = self._evidence()
        second = self._evidence()
        self.assertFalse(first["replayed"])
        self.assertTrue(second["replayed"])
        self.assertEqual(first["evidence_id"], second["evidence_id"])
        report = self.inspection.daily_report(site_id="s1", local_date="2026-09-25")
        item = next(i for i in report["items"] if i["item_key"] == "workshop:ws-1")
        self.assertEqual(1, item["version_count"])

    def test_request_id_rejects_changed_payload(self):
        self._evidence()
        with self.assertRaises(ConflictError):
            self._evidence(evidence_hash=HASH_B)

    def test_identical_content_is_deduplicated(self):
        first = self._evidence()
        duplicate = self._evidence(request_id="r-e2", captured_at="2026-09-25T01:30:00Z")
        self.assertTrue(duplicate["deduplicated"])
        self.assertEqual(first["evidence_id"], duplicate["evidence_id"])
        report = self.inspection.daily_report(site_id="s1", local_date="2026-09-25")
        item = next(i for i in report["items"] if i["item_key"] == "workshop:ws-1")
        self.assertEqual(1, item["version_count"])

    def test_conflicting_content_is_retained_not_overwritten(self):
        first = self._evidence()
        second = self._evidence(request_id="r-e2", evidence_hash=HASH_B,
                                captured_at="2026-09-25T09:45:00+08:00", storage_ref="oss://b/2.jpg")
        self.assertEqual(2, second["version"])
        self.assertTrue(second["conflict"])
        report = self.inspection.daily_report(site_id="s1", local_date="2026-09-25")
        item = next(i for i in report["items"] if i["item_key"] == "workshop:ws-1")
        self.assertTrue(item["has_conflict"])
        self.assertEqual(2, item["version_count"])
        versions = {v["version"]: v for v in item["evidence"]}
        self.assertEqual(HASH_A, versions[1]["evidence_hash"])
        self.assertEqual(HASH_B, versions[2]["evidence_hash"])
        self.assertEqual(first["evidence_id"], versions[1]["evidence_id"])

    def test_correction_allowed_before_deadline_and_rejected_after(self):
        self._evidence()
        correction = self.inspection.submit_correction(
            request_id="r-c1", actor_id="op1", site_id="s1", local_date="2026-09-25",
            item_key="workshop:ws-1", content="补充说明")
        self.assertEqual(1, correction["checklist_version"])
        self.clock.set(datetime(2026, 9, 25, 16, 1, tzinfo=timezone.utc))  # 次日 00:01 本地
        with self.assertRaises(ConflictError):
            self.inspection.submit_correction(
                request_id="r-c2", actor_id="op1", site_id="s1", local_date="2026-09-25",
                item_key="workshop:ws-1", content="超时说明")

    def test_decision_flow_dispute_accept_supplement(self):
        first = self._evidence()
        second = self._evidence(request_id="r-e2", evidence_hash=HASH_B,
                                captured_at="2026-09-25T09:45:00+08:00")
        dispute = self.inspection.record_decision(
            request_id="r-d1", actor_id="rv1", site_id="s1", local_date="2026-09-25",
            item_key="workshop:ws-1", action="dispute", note="两版凭据不一致")
        self.assertEqual(1, dispute["checklist_version"])
        report = self.inspection.daily_report(site_id="s1", local_date="2026-09-25")
        self.assertEqual(["workshop:ws-1"], report["summary"]["disputed_items"])
        accept = self.inspection.record_decision(
            request_id="r-d2", actor_id="rv1", site_id="s1", local_date="2026-09-25",
            item_key="workshop:ws-1", action="accept", evidence_id=second["evidence_id"])
        self.assertEqual("accept", accept["action"])
        supplement = self.inspection.record_decision(
            request_id="r-d3", actor_id="rv1", site_id="s1", local_date="2026-09-25",
            item_key="treatment:tf-1", action="request_supplement", note="请补充台账")
        self.assertEqual("request_supplement", supplement["action"])
        report = self.inspection.daily_report(site_id="s1", local_date="2026-09-25")
        items = {i["item_key"]: i for i in report["items"]}
        self.assertEqual("accepted", items["workshop:ws-1"]["resolution_state"])
        self.assertEqual(second["evidence_id"], items["workshop:ws-1"]["accepted_evidence_id"])
        self.assertFalse(items["workshop:ws-1"]["open_dispute"])
        self.assertEqual("supplement_requested", items["treatment:tf-1"]["resolution_state"])
        self.assertEqual([], report["summary"]["disputed_items"])
        self.assertEqual(first["evidence_id"], items["workshop:ws-1"]["evidence"][0]["evidence_id"])

    def test_operator_cannot_record_decision(self):
        self._evidence()
        with self.assertRaises(PermissionDenied):
            self.inspection.record_decision(
                request_id="r-d1", actor_id="op1", site_id="s1", local_date="2026-09-25",
                item_key="workshop:ws-1", action="dispute", note="越权")

    def test_reviewer_cannot_submit_evidence(self):
        with self.assertRaises(PermissionDenied):
            self._evidence(actor_id="rv1")

    def test_auditor_cannot_submit_evidence(self):
        with self.assertRaises(PermissionDenied):
            self._evidence(actor_id="au1")

    def test_cross_midnight_late_evidence_keeps_capture_date(self):
        self.clock.set(datetime(2026, 9, 25, 17, 30, tzinfo=timezone.utc))  # 次日 01:30 本地
        late = self._evidence(captured_at="2026-09-25T23:40:00+08:00")
        self.assertTrue(late["late"])
        self.assertEqual("2026-09-25", late["local_date"])
        report = self.inspection.daily_report(site_id="s1", local_date="2026-09-25")
        item = next(i for i in report["items"] if i["item_key"] == "workshop:ws-1")
        self.assertTrue(item["late"])
        self.assertGreater(item["late_by_seconds"], 0)
        self.assertIn("workshop:ws-1", report["summary"]["late_items"])
        reasons = {r["item_key"]: r["reason"] for r in report["summary"]["overdue_reasons"]}
        self.assertEqual("first_evidence_after_deadline", reasons["workshop:ws-1"])
        self.assertEqual("missing_evidence", reasons["treatment:tf-1"])

    def test_device_clock_skew_beyond_tolerance_rejected(self):
        with self.assertRaises(ValidationError):
            self._evidence(captured_at="2026-09-25T10:30:00+08:00")  # 未来 30 分钟
        within = self._evidence(captured_at="2026-09-25T10:04:00+08:00")  # 未来 4 分钟
        self.assertFalse(within["late"])

    def test_captured_at_requires_timezone(self):
        with self.assertRaises(ValidationError):
            self._evidence(captured_at="2026-09-25T09:30:00")

    def test_unknown_item_rejected(self):
        with self.assertRaises(NotFoundError):
            self._evidence(item_key="workshop:ws-9")

    def test_invalid_evidence_hash_rejected(self):
        with self.assertRaises(ValidationError):
            self._evidence(evidence_hash="not-a-hash")

    def test_accept_requires_existing_evidence(self):
        self._evidence()
        with self.assertRaises(NotFoundError):
            self.inspection.record_decision(
                request_id="r-d1", actor_id="rv1", site_id="s1", local_date="2026-09-25",
                item_key="workshop:ws-1", action="accept", evidence_id="0" * 32)
        with self.assertRaises(ValidationError):
            self.inspection.record_decision(
                request_id="r-d2", actor_id="rv1", site_id="s1", local_date="2026-09-25",
                item_key="workshop:ws-1", action="accept")

    def test_dispute_requires_note(self):
        self._evidence()
        with self.assertRaises(ValidationError):
            self.inspection.record_decision(
                request_id="r-d1", actor_id="rv1", site_id="s1", local_date="2026-09-25",
                item_key="workshop:ws-1", action="dispute")

    def test_cross_organization_access_denied(self):
        self.service.register_organization(request_id="r-org2", actor_id="a1",
                                           organization_id="o2", name="另一企业")
        self.service.register_actor(request_id="r-op2", actor_id="a1", new_actor_id="op2",
                                    display_name="他人", role="operator", organization_id="o2")
        with self.assertRaises(PermissionDenied):
            self._evidence(actor_id="op2")

    def test_daily_report_pending_then_overdue(self):
        report = self.inspection.daily_report(site_id="s1", local_date="2026-09-25")
        self.assertEqual("pending", report["status"])
        self.assertEqual(["treatment:tf-1", "workshop:ws-1"], report["summary"]["missing_items"])
        self.assertEqual([], report["summary"]["overdue_reasons"])
        self.clock.set(datetime(2026, 9, 25, 16, 1, tzinfo=timezone.utc))  # 次日 00:01 本地
        report = self.inspection.daily_report(site_id="s1", local_date="2026-09-25")
        self.assertEqual("overdue", report["status"])
        reasons = {r["item_key"]: r["reason"] for r in report["summary"]["overdue_reasons"]}
        self.assertEqual({"treatment:tf-1": "missing_evidence", "workshop:ws-1": "missing_evidence"},
                         reasons)
        self.assertEqual("2026-09-25T16:00:00Z", report["deadline"])

    def test_concurrent_first_submissions_keep_both_versions(self):
        barrier = threading.Barrier(2)
        results = []
        errors = []

        def submit(request_id, evidence_hash):
            try:
                barrier.wait(timeout=10)
                results.append(self._evidence(request_id=request_id, evidence_hash=evidence_hash))
            except Exception as exc:  # pragma: no cover - 失败时记录
                errors.append(exc)

        threads = [threading.Thread(target=submit, args=("r-e1", HASH_A)),
                   threading.Thread(target=submit, args=("r-e2", HASH_B))]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        self.assertEqual([], errors)
        self.assertEqual({1, 2}, {result["version"] for result in results})
        report = self.inspection.daily_report(site_id="s1", local_date="2026-09-25")
        item = next(i for i in report["items"] if i["item_key"] == "workshop:ws-1")
        self.assertEqual(2, item["version_count"])
        self.assertTrue(item["has_conflict"])

    def test_concurrent_identical_submissions_deduplicate(self):
        barrier = threading.Barrier(2)
        results = []
        errors = []

        def submit(request_id):
            try:
                barrier.wait(timeout=10)
                results.append(self._evidence(request_id=request_id))
            except Exception as exc:  # pragma: no cover - 失败时记录
                errors.append(exc)

        threads = [threading.Thread(target=submit, args=("r-e1",)),
                   threading.Thread(target=submit, args=("r-e2",))]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        self.assertEqual([], errors)
        self.assertEqual(1, len({result["evidence_id"] for result in results}))
        self.assertEqual(1, sum(1 for result in results if result["deduplicated"]))
        report = self.inspection.daily_report(site_id="s1", local_date="2026-09-25")
        item = next(i for i in report["items"] if i["item_key"] == "workshop:ws-1")
        self.assertEqual(1, item["version_count"])

    def test_timeline_is_append_only_and_verifiable(self):
        self._evidence()
        self.inspection.submit_correction(
            request_id="r-c1", actor_id="op1", site_id="s1", local_date="2026-09-25",
            item_key="workshop:ws-1", content="说明")
        self.inspection.record_decision(
            request_id="r-d1", actor_id="rv1", site_id="s1", local_date="2026-09-25",
            item_key="workshop:ws-1", action="dispute", note="存疑")
        timeline = self.inspection.daily_timeline(site_id="s1", local_date="2026-09-25")
        self.assertTrue(timeline["audit_valid"])
        actions = [event["action"] for event in timeline["events"]]
        self.assertEqual(["inspection.checklist_generated", "inspection.evidence_submitted",
                          "inspection.correction_submitted", "inspection.decision_recorded"], actions)
        sequences = [event["sequence"] for event in timeline["events"]]
        self.assertEqual(sorted(sequences), sequences)
        valid, _ = self.service.verify_audit()
        self.assertTrue(valid)

    def test_evidence_rejected_leaves_no_trace(self):
        with self.assertRaises(ValidationError):
            self._evidence(captured_at="2026-09-25T10:30:00+08:00")
        report = self.inspection.daily_report(site_id="s1", local_date="2026-09-25")
        item = next(i for i in report["items"] if i["item_key"] == "workshop:ws-1")
        self.assertEqual(0, item["version_count"])


class InspectionRestartTest(unittest.TestCase):
    def test_service_restart_keeps_conclusions(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "restart.sqlite3"
            database = Database(path)
            clock = MutableClock(datetime(2026, 9, 25, 2, 0, tzinfo=timezone.utc))
            service = DomainService(database, clock)
            inspection = InspectionService(service)
            service.register_organization(request_id="r-org", actor_id="bootstrap",
                                          organization_id="o1", name="家具企业")
            service.register_actor(request_id="r-admin", actor_id="bootstrap", new_actor_id="a1",
                                   display_name="管理员", role="admin", organization_id="o1")
            service.register_actor(request_id="r-op", actor_id="a1", new_actor_id="op1",
                                   display_name="环保负责人", role="operator", organization_id="o1")
            service.register_site(request_id="r-site", actor_id="op1", site_id="s1",
                                  organization_id="o1", name="厂区", timezone_name="Asia/Shanghai")
            service.record_domain_data(request_id="r-w1", actor_id="op1", site_id="s1",
                                       category="workshop_registry", external_key="ws-1",
                                       data={"name": "喷漆车间"})
            inspection.submit_evidence(request_id="r-e1", actor_id="op1", site_id="s1",
                                       item_key="workshop:ws-1", evidence_hash=HASH_A,
                                       captured_at="2026-09-25T09:30:00+08:00",
                                       storage_ref="oss://b/1.jpg")
            before = inspection.daily_report(site_id="s1", local_date="2026-09-25")
            database.close()

            reopened = Database(path)
            service2 = DomainService(reopened, MutableClock(datetime(2026, 9, 25, 2, 0,
                                                                     tzinfo=timezone.utc)))
            inspection2 = InspectionService(service2)
            after = inspection2.daily_report(site_id="s1", local_date="2026-09-25")
            self.assertEqual(before, after)
            self.assertEqual("complete", after["status"])
            valid, count = service2.verify_audit()
            self.assertTrue(valid)
            self.assertGreater(count, 0)
            # 重启后相同重试仍返回原回执
            replay = inspection2.submit_evidence(request_id="r-e1", actor_id="op1", site_id="s1",
                                                 item_key="workshop:ws-1", evidence_hash=HASH_A,
                                                 captured_at="2026-09-25T09:30:00+08:00",
                                                 storage_ref="oss://b/1.jpg")
            self.assertTrue(replay["replayed"])
            reopened.close()


class InspectionApiTest(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        self.clock = MutableClock(datetime(2026, 9, 25, 2, 0, tzinfo=timezone.utc))
        self.service = DomainService(self.database, self.clock)
        self.inspection = InspectionService(self.service)
        self.service.register_organization(request_id="r-org", actor_id="bootstrap",
                                           organization_id="o1", name="家具企业")
        self.service.register_actor(request_id="r-admin", actor_id="bootstrap", new_actor_id="a1",
                                    display_name="管理员", role="admin", organization_id="o1")
        self.service.register_actor(request_id="r-op", actor_id="a1", new_actor_id="op1",
                                    display_name="环保负责人", role="operator", organization_id="o1")
        self.service.register_actor(request_id="r-rv", actor_id="a1", new_actor_id="rv1",
                                    display_name="监管人员", role="reviewer", organization_id="o1")
        self.service.register_site(request_id="r-site", actor_id="op1", site_id="s1",
                                   organization_id="o1", name="厂区", timezone_name="Asia/Shanghai")
        self.service.record_domain_data(request_id="r-w1", actor_id="op1", site_id="s1",
                                        category="workshop_registry", external_key="ws-1",
                                        data={"name": "喷漆车间"})

    def tearDown(self):
        self.database.close()

    def _route(self, method, path, body=None, actor="op1"):
        return route(self.service, method, path, body, {"X-Actor-Id": actor}, self.inspection)

    def test_evidence_and_report_over_http(self):
        status, payload = self._route("POST", "/inspections/evidence", {
            "request_id": "r-e1", "site_id": "s1", "item_key": "workshop:ws-1",
            "evidence_hash": HASH_A, "captured_at": "2026-09-25T09:30:00+08:00",
            "storage_ref": "oss://b/1.jpg"})
        self.assertEqual(201, status)
        self.assertFalse(payload["replayed"])
        status, payload = self._route("POST", "/inspections/evidence", {
            "request_id": "r-e1", "site_id": "s1", "item_key": "workshop:ws-1",
            "evidence_hash": HASH_A, "captured_at": "2026-09-25T09:30:00+08:00",
            "storage_ref": "oss://b/1.jpg"})
        self.assertEqual(200, status)
        self.assertTrue(payload["replayed"])
        status, report = self._route("GET", "/inspections/daily?site_id=s1&date=2026-09-25")
        self.assertEqual(200, status)
        self.assertEqual("complete", report["status"])
        self.assertEqual(1, report["checklist_version"])

    def test_decision_and_timeline_over_http(self):
        self._route("POST", "/inspections/evidence", {
            "request_id": "r-e1", "site_id": "s1", "item_key": "workshop:ws-1",
            "evidence_hash": HASH_A, "captured_at": "2026-09-25T09:30:00+08:00",
            "storage_ref": "oss://b/1.jpg"})
        status, payload = self._route("POST", "/inspections/decisions", {
            "request_id": "r-d1", "site_id": "s1", "local_date": "2026-09-25",
            "item_key": "workshop:ws-1", "action": "dispute", "note": "存疑"}, actor="rv1")
        self.assertEqual(201, status)
        self.assertEqual("dispute", payload["action"])
        self.assertEqual(1, payload["checklist_version"])
        status, timeline = self._route("GET", "/inspections/timeline?site_id=s1&date=2026-09-25")
        self.assertEqual(200, status)
        self.assertTrue(timeline["audit_valid"])
        self.assertEqual(["inspection.checklist_generated", "inspection.evidence_submitted",
                          "inspection.decision_recorded"],
                         [event["action"] for event in timeline["events"]])

    def test_checklist_endpoint_over_http(self):
        status, payload = self._route("POST", "/inspections/checklists",
                                      {"request_id": "r-c1", "site_id": "s1"})
        self.assertEqual(201, status)
        self.assertEqual("2026-09-25", payload["local_date"])
        self.assertEqual(1, payload["item_count"])
        status, payload = self._route("POST", "/inspections/checklists",
                                      {"request_id": "r-c1", "site_id": "s1"})
        self.assertEqual(200, status)
        self.assertTrue(payload["replayed"])

    def test_domain_error_maps_to_http_status(self):
        status, payload = self._route("POST", "/inspections/evidence", {
            "request_id": "r-e1", "site_id": "s1", "item_key": "workshop:ws-9",
            "evidence_hash": HASH_A, "captured_at": "2026-09-25T09:30:00+08:00",
            "storage_ref": "oss://b/1.jpg"})
        self.assertEqual(404, status)
        self.assertEqual("not_found", payload["error"])
        status, payload = self._route("POST", "/inspections/evidence", {
            "request_id": "r-e2", "site_id": "s1", "item_key": "workshop:ws-1",
            "evidence_hash": HASH_A, "captured_at": "2026-09-25T10:30:00+08:00",
            "storage_ref": "oss://b/1.jpg"})
        self.assertEqual(400, status)
        self.assertEqual("validation_error", payload["error"])


if __name__ == "__main__":
    unittest.main()
