import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from self_inspection_core.api import route
from self_inspection_core.clock import StepClock
from self_inspection_core.errors import ConflictError, NotFoundError, PermissionDenied, ValidationError
from self_inspection_core.evidence import EvidenceService
from self_inspection_core.storage import Database

HASH_1 = "1" * 64
HASH_2 = "2" * 64
HASH_3 = "3" * 64
HASH_4 = "4" * 64


class EvidenceTest(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        self.clock = StepClock(datetime(2026, 9, 25, 1, 0, tzinfo=timezone.utc))
        self.service = EvidenceService(self.database, self.clock)
        self.service.register_organization(request_id="org", actor_id="bootstrap",
                                           organization_id="o1", name="家具企业")
        self.service.register_actor(request_id="admin", actor_id="bootstrap", new_actor_id="a1",
                                    display_name="管理员", role="admin", organization_id="o1")
        self.service.register_actor(request_id="operator", actor_id="a1", new_actor_id="op1",
                                    display_name="环保负责人", role="operator", organization_id="o1")
        self.service.register_actor(request_id="auditor", actor_id="a1", new_actor_id="au1",
                                    display_name="审计员", role="auditor", organization_id="o1")
        self.service.register_organization(request_id="org2", actor_id="a1",
                                           organization_id="o2", name="另一企业")
        self.service.register_actor(request_id="operator2", actor_id="a1", new_actor_id="op2",
                                    display_name="其他企业操作员", role="operator", organization_id="o2")
        self.service.register_organization(request_id="org-reg", actor_id="a1",
                                           organization_id="reg", name="监管局")
        self.service.register_actor(request_id="reviewer", actor_id="a1", new_actor_id="rev1",
                                    display_name="监管人员", role="reviewer", organization_id="reg")
        self.service.register_site(request_id="site", actor_id="op1", site_id="s1",
                                   organization_id="o1", name="生产车间", timezone_name="Asia/Shanghai")
        self.service.record_domain_data(request_id="wh", actor_id="op1", site_id="s1",
                                        category="workshop_registry", external_key="wh-1",
                                        data={"name": "喷漆车间", "enabled": True})
        self.service.record_domain_data(request_id="tr", actor_id="op1", site_id="s1",
                                        category="treatment_registry", external_key="tr-1",
                                        data={"name": "废气处理设施", "enabled": True})
        self.plan = self.service.generate_plan(request_id="plan", actor_id="op1",
                                               site_id="s1", local_date="2026-09-25")

    def tearDown(self):
        self.database.close()

    def _submit(self, request_id, item_key, evidence_hash, captured="2026-09-25T09:00:00+08:00",
                actor="op1", storage="device://cam/1.jpg"):
        return self.service.submit_evidence(request_id=request_id, actor_id=actor,
                                            plan_id=self.plan["plan_id"], item_key=item_key,
                                            evidence_hash=evidence_hash, captured_at=captured,
                                            storage_ref=storage)

    def test_plan_derives_items_from_registries(self):
        self.assertEqual(1, self.plan["plan_version"])
        self.assertEqual("2026-09-25T16:00:00Z", self.plan["cutoff_at"])
        keys = [item["item_key"] for item in self.plan["items"]]
        self.assertEqual(["treatment_running:tr-1", "workshop_sealed:wh-1"], keys)
        titles = {item["item_key"]: item["title"] for item in self.plan["items"]}
        self.assertEqual("喷漆车间", titles["workshop_sealed:wh-1"])

    def test_plan_replay_and_noop_regeneration(self):
        replay = self.service.generate_plan(request_id="plan", actor_id="op1",
                                            site_id="s1", local_date="2026-09-25")
        self.assertTrue(replay["replayed"])
        self.assertEqual(self.plan["plan_id"], replay["plan_id"])
        again = self.service.generate_plan(request_id="plan-2", actor_id="op1",
                                           site_id="s1", local_date="2026-09-25")
        self.assertFalse(again["regenerated"])
        self.assertEqual(1, again["plan_version"])

    def test_plan_version_bumps_when_registry_changes(self):
        self.service.record_domain_data(request_id="tr2", actor_id="op1", site_id="s1",
                                        category="treatment_registry", external_key="tr-2",
                                        data={"name": "废水处理站", "enabled": True})
        bumped = self.service.generate_plan(request_id="plan-3", actor_id="op1",
                                            site_id="s1", local_date="2026-09-25")
        self.assertTrue(bumped["regenerated"])
        self.assertEqual(2, bumped["plan_version"])
        self.assertEqual(3, len(bumped["items"]))
        actions = [event["action"] for event in self.service.plan_timeline(self.plan["plan_id"])]
        self.assertIn("inspection_plan.regenerated", actions)

    def test_plan_cutoff_cannot_change_on_regeneration(self):
        with self.assertRaises(ValidationError):
            self.service.generate_plan(request_id="plan-4", actor_id="op1", site_id="s1",
                                       local_date="2026-09-25", cutoff_at="2026-09-26T06:00:00+08:00")

    def test_identical_retry_returns_original_receipt(self):
        first = self._submit("ev-1", "workshop_sealed:wh-1", HASH_1)
        replay = self._submit("ev-1", "workshop_sealed:wh-1", HASH_1)
        self.assertFalse(first["replayed"])
        self.assertTrue(replay["replayed"])
        self.assertEqual(first["evidence_id"], replay["evidence_id"])
        dupe = self._submit("ev-1b", "workshop_sealed:wh-1", HASH_1)
        self.assertTrue(dupe["deduplicated"])
        self.assertEqual(first["evidence_id"], dupe["evidence_id"])
        report = self.service.day_report(site_id="s1", local_date="2026-09-25")
        versions = report["items"][1]["versions"]
        self.assertEqual(1, len(versions))

    def test_conflicting_content_is_kept_as_new_version(self):
        first = self._submit("ev-1", "workshop_sealed:wh-1", HASH_1)
        second = self._submit("ev-2", "workshop_sealed:wh-1", HASH_2)
        self.assertEqual(1, first["version_no"])
        self.assertEqual(2, second["version_no"])
        self.assertTrue(second["conflict"])
        report = self.service.day_report(site_id="s1", local_date="2026-09-25")
        versions = report["items"][1]["versions"]
        self.assertEqual([1, 2], [item["version_no"] for item in versions])
        self.assertEqual(HASH_1, versions[0]["evidence_hash"])

    def test_request_id_rejects_changed_payload(self):
        self._submit("ev-1", "workshop_sealed:wh-1", HASH_1)
        with self.assertRaises(ConflictError):
            self._submit("ev-1", "workshop_sealed:wh-1", HASH_2)

    def test_cross_midnight_backfill_is_late_but_attributed(self):
        self._submit("ev-1", "treatment_running:tr-1", HASH_1)
        self.clock.set(datetime(2026, 9, 26, 16, 30, tzinfo=timezone.utc))
        late = self._submit("ev-2", "workshop_sealed:wh-1", HASH_2,
                            captured="2026-09-25T23:50:00+08:00")
        self.assertTrue(late["late"])
        self.assertEqual([], late["flags"])
        report = self.service.day_report(site_id="s1", local_date="2026-09-25")
        self.assertEqual("overdue", report["day_status"])
        self.assertEqual([{"item_key": "workshop_sealed:wh-1", "reason": "late",
                           "first_submitted_at": late["submitted_at"]}],
                         report["outstanding_items"])

    def test_device_clock_skew_is_flagged_not_rejected(self):
        future = self._submit("ev-1", "workshop_sealed:wh-1", HASH_1,
                              captured="2026-09-25T10:00:00+08:00")
        self.assertIn("captured_in_future", future["flags"])
        mismatch = self._submit("ev-2", "treatment_running:tr-1", HASH_2,
                                captured="2026-09-24T20:00:00+08:00")
        self.assertIn("captured_outside_plan_date", mismatch["flags"])
        report = self.service.day_report(site_id="s1", local_date="2026-09-25")
        self.assertEqual("complete", report["day_status"])

    def test_correction_only_before_cutoff(self):
        evidence = self._submit("ev-1", "workshop_sealed:wh-1", HASH_1)
        correction = self.service.submit_correction(request_id="fix-1", actor_id="op1",
                                                    evidence_id=evidence["evidence_id"],
                                                    note="补拍角度说明")
        self.assertEqual(1, correction["plan_version"])
        self.clock.set(datetime(2026, 9, 26, 17, 0, tzinfo=timezone.utc))
        with self.assertRaises(ConflictError):
            self.service.submit_correction(request_id="fix-2", actor_id="op1",
                                           evidence_id=evidence["evidence_id"], note="逾期说明")

    def test_dispute_then_accept_resolves_and_references_plan_version(self):
        first = self._submit("ev-1", "workshop_sealed:wh-1", HASH_1)
        second = self._submit("ev-2", "workshop_sealed:wh-1", HASH_2)
        dispute = self.service.raise_dispute(request_id="dp-1", actor_id="rev1",
                                             evidence_id=second["evidence_id"], reason="画面模糊")
        self.assertEqual(1, dispute["plan_version"])
        report = self.service.day_report(site_id="s1", local_date="2026-09-25")
        self.assertEqual("disputed", report["items"][1]["status"])
        self.assertEqual(1, len(report["disputed_evidence"]))
        decision = self.service.decide_evidence(request_id="dc-1", actor_id="rev1",
                                                plan_id=self.plan["plan_id"],
                                                item_key="workshop_sealed:wh-1",
                                                decision="accept_version",
                                                evidence_id=first["evidence_id"])
        self.assertEqual([dispute["dispute_id"]], decision["resolved_disputes"])
        self.assertEqual(1, decision["plan_version"])
        report = self.service.day_report(site_id="s1", local_date="2026-09-25")
        item = report["items"][1]
        self.assertEqual("accepted", item["status"])
        self.assertEqual(first["evidence_id"], item["accepted_evidence_id"])
        self.assertEqual([], report["disputed_evidence"])

    def test_request_supplement_marks_item(self):
        self._submit("ev-1", "workshop_sealed:wh-1", HASH_1)
        self.service.decide_evidence(request_id="dc-1", actor_id="rev1",
                                     plan_id=self.plan["plan_id"], item_key="workshop_sealed:wh-1",
                                     decision="request_supplement", note="请补充设施铭牌照片")
        report = self.service.day_report(site_id="s1", local_date="2026-09-25")
        self.assertTrue(report["items"][1]["supplement_requested"])
        with self.assertRaises(ValidationError):
            self.service.decide_evidence(request_id="dc-2", actor_id="rev1",
                                         plan_id=self.plan["plan_id"], item_key="workshop_sealed:wh-1",
                                         decision="accept_version")

    def test_day_status_transitions(self):
        report = self.service.day_report(site_id="s1", local_date="2026-09-25")
        self.assertEqual("incomplete", report["day_status"])
        self.clock.set(datetime(2026, 9, 26, 17, 0, tzinfo=timezone.utc))
        report = self.service.day_report(site_id="s1", local_date="2026-09-25")
        self.assertEqual("overdue", report["day_status"])
        self.assertEqual({"missing"}, {item["reason"] for item in report["outstanding_items"]})
        self.assertEqual(2, len(report["outstanding_items"]))

    def test_concurrent_first_submissions_keep_both_versions(self):
        results = list(ThreadPoolExecutor(max_workers=2).map(
            lambda index: self._submit(f"ev-c{index}", "workshop_sealed:wh-1",
                                       HASH_1 if index == 0 else HASH_2),
            range(2),
        ))
        self.assertEqual({1, 2}, {item["version_no"] for item in results})
        report = self.service.day_report(site_id="s1", local_date="2026-09-25")
        self.assertEqual(2, len(report["items"][1]["versions"]))

    def test_concurrent_identical_submissions_dedupe(self):
        results = list(ThreadPoolExecutor(max_workers=2).map(
            lambda index: self._submit(f"ev-d{index}", "workshop_sealed:wh-1", HASH_1),
            range(2),
        ))
        self.assertEqual(1, len({item["evidence_id"] for item in results}))
        report = self.service.day_report(site_id="s1", local_date="2026-09-25")
        self.assertEqual(1, len(report["items"][1]["versions"]))

    def test_restart_preserves_conclusions(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "restart.sqlite3"
            database = Database(path)
            clock = StepClock(datetime(2026, 9, 25, 1, 0, tzinfo=timezone.utc))
            service = EvidenceService(database, clock)
            service.register_organization(request_id="org", actor_id="bootstrap",
                                          organization_id="o1", name="企业")
            service.register_actor(request_id="admin", actor_id="bootstrap", new_actor_id="a1",
                                   display_name="管理员", role="admin", organization_id="o1")
            service.register_site(request_id="site", actor_id="a1", site_id="s1",
                                  organization_id="o1", name="车间", timezone_name="Asia/Shanghai")
            service.record_domain_data(request_id="wh", actor_id="a1", site_id="s1",
                                       category="workshop_registry", external_key="wh-1",
                                       data={"name": "喷漆车间"})
            plan = service.generate_plan(request_id="plan", actor_id="a1",
                                         site_id="s1", local_date="2026-09-25")
            service.submit_evidence(request_id="ev", actor_id="a1", plan_id=plan["plan_id"],
                                  item_key="workshop_sealed:wh-1", evidence_hash=HASH_1,
                                  captured_at="2026-09-25T09:00:00+08:00", storage_ref="device://cam/1")
            before = service.day_report(site_id="s1", local_date="2026-09-25")
            database.close()
            restarted = EvidenceService(Database(path), clock)
            after = restarted.day_report(site_id="s1", local_date="2026-09-25")
            self.assertEqual(before, after)
            self.assertEqual("complete", after["day_status"])
            self.assertTrue(restarted.verify_audit()[0])
            restarted.database.close()

    def test_permissions(self):
        with self.assertRaises(PermissionDenied):
            self._submit("ev-x", "workshop_sealed:wh-1", HASH_1, actor="au1")
        with self.assertRaises(PermissionDenied):
            self._submit("ev-y", "workshop_sealed:wh-1", HASH_1, actor="op2")
        with self.assertRaises(PermissionDenied):
            self._submit("ev-z", "workshop_sealed:wh-1", HASH_1, actor="rev1")
        evidence = self._submit("ev-1", "workshop_sealed:wh-1", HASH_1)
        with self.assertRaises(PermissionDenied):
            self.service.raise_dispute(request_id="dp-x", actor_id="op1",
                                       evidence_id=evidence["evidence_id"], reason="越权")
        dispute = self.service.raise_dispute(request_id="dp-1", actor_id="rev1",
                                             evidence_id=evidence["evidence_id"], reason="跨组织监管")
        self.assertEqual("open", dispute["status"])

    def test_timeline_is_ordered_and_carries_plan_version(self):
        evidence = self._submit("ev-1", "workshop_sealed:wh-1", HASH_1)
        self.service.raise_dispute(request_id="dp-1", actor_id="rev1",
                                   evidence_id=evidence["evidence_id"], reason="存疑")
        self.service.decide_evidence(request_id="dc-1", actor_id="rev1",
                                     plan_id=self.plan["plan_id"], item_key="workshop_sealed:wh-1",
                                     decision="accept_version", evidence_id=evidence["evidence_id"])
        timeline = self.service.plan_timeline(self.plan["plan_id"])
        actions = [event["action"] for event in timeline]
        self.assertEqual(["inspection_plan.generated", "evidence.submitted",
                          "evidence.dispute_raised", "evidence.version_accepted"], actions)
        sequences = [event["sequence"] for event in timeline]
        self.assertEqual(sorted(sequences), sequences)
        for event in timeline[1:]:
            self.assertEqual(1, event["detail"]["plan_version"])
        self.assertTrue(self.service.verify_audit()[0])

    def test_validation(self):
        with self.assertRaises(ValidationError):
            self._submit("ev-b1", "workshop_sealed:wh-1", "not-a-hash")
        with self.assertRaises(ValidationError):
            self._submit("ev-b2", "workshop_sealed:wh-1", HASH_1, captured="2026-09-25 09:00:00")
        with self.assertRaises(NotFoundError):
            self._submit("ev-b3", "workshop_sealed:wh-9", HASH_1)
        with self.assertRaises(ValidationError):
            self.service.generate_plan(request_id="plan-b", actor_id="op1", site_id="s1",
                                       local_date="2026-9-5")
        with self.assertRaises(NotFoundError):
            self.service.day_report(site_id="s1", local_date="2026-09-30")

    def test_http_routes(self):
        headers = {"X-Actor-Id": "op1"}
        status, plan = route(self.service, "POST", "/inspection-plans",
                             {"request_id": "http-plan", "site_id": "s1", "local_date": "2026-09-26"},
                             headers)
        self.assertEqual(201, status)
        status, evidence = route(self.service, "POST", "/inspection-evidence",
                                 {"request_id": "http-ev", "plan_id": plan["plan_id"],
                                  "item_key": "workshop_sealed:wh-1", "evidence_hash": HASH_3,
                                  "captured_at": "2026-09-26T09:00:00+08:00",
                                  "storage_ref": "device://cam/9"},
                                 headers)
        self.assertEqual(201, status)
        status, again = route(self.service, "POST", "/inspection-evidence",
                              {"request_id": "http-ev", "plan_id": plan["plan_id"],
                               "item_key": "workshop_sealed:wh-1", "evidence_hash": HASH_3,
                               "captured_at": "2026-09-26T09:00:00+08:00",
                               "storage_ref": "device://cam/9"},
                              headers)
        self.assertEqual(200, status)
        self.assertEqual(evidence["evidence_id"], again["evidence_id"])
        status, report = route(self.service, "GET",
                               "/inspection-day-report?site_id=s1&local_date=2026-09-26", None)
        self.assertEqual(200, status)
        self.assertEqual("incomplete", report["day_status"])
        status, timeline = route(self.service, "GET",
                                 f"/inspection-timeline?plan_id={plan['plan_id']}", None)
        self.assertEqual(200, status)
        self.assertEqual(2, len(timeline["items"]))


if __name__ == "__main__":
    unittest.main()
