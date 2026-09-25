"""每日自巡证据模块：清单生成、凭据提交、纠正、争议与裁决。

在主体、场所、领域资料与哈希审计链之上，按企业所在地日期生成应巡清单，
接收结构化凭据（摘要、拍摄时刻、存储引用），相同内容的重试返回原回执，
同一项目的不同内容作为新版本保留而不覆盖。企业可在截止前提交纠正说明，
监管人员可发起争议、采信某个版本或要求补证，所有决定记录当时的清单版本，
并写入哈希串联的审计时间线。模块方法返回可直接序列化的 JSON 字典。
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Callable
from zoneinfo import ZoneInfo

from .audit import append_event, canonical_json, digest
from .errors import ConflictError, NotFoundError, PermissionDenied, ValidationError
from .service import DomainService

ITEM_KINDS = {
    "workshop_registry": "workshop_sealed",
    "treatment_registry": "treatment_running",
}
CLOCK_SKEW_TOLERANCE = timedelta(minutes=5)
HASH_PATTERN = re.compile(r"^[0-9a-f]{64}$")
DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")
DECISION_TYPES = frozenset({"accept_version", "request_supplement"})


def _canonical(moment: datetime) -> str:
    """把时刻规范化为 UTC ISO 文本，保证同一时刻只有唯一写法。"""

    return moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


class EvidenceService(DomainService):
    """在基础服务之上提供每日自巡证据能力。"""

    # ---------- 基础工具 ----------

    def _site_row(self, connection, site_id: str):
        row = connection.execute("SELECT * FROM sites WHERE site_id=?", (site_id,)).fetchone()
        if row is None:
            raise NotFoundError("场所不存在")
        return row

    def _site_timezone(self, site) -> ZoneInfo:
        try:
            return ZoneInfo(site["timezone_name"])
        except Exception as exc:
            raise ValidationError("场所时区无效") from exc

    def _local_date(self, value: str) -> str:
        text = str(value).strip()
        if not DATE_PATTERN.fullmatch(text):
            raise ValidationError("local_date 必须是 YYYY-MM-DD 格式")
        try:
            date.fromisoformat(text)
        except ValueError as exc:
            raise ValidationError("local_date 不是有效日期") from exc
        return text

    def _parse_instant(self, value: str, field: str) -> datetime:
        text = str(value).strip()
        if text.endswith(("Z", "z")):
            text = text[:-1] + "+00:00"
        try:
            moment = datetime.fromisoformat(text)
        except ValueError as exc:
            raise ValidationError(f"{field} 必须是 ISO 8601 时间") from exc
        if moment.tzinfo is None:
            raise ValidationError(f"{field} 必须包含时区偏移")
        return moment.astimezone(timezone.utc)

    def _default_cutoff(self, timezone_info: ZoneInfo, local_date: str) -> datetime:
        day = date.fromisoformat(local_date)
        return datetime.combine(day + timedelta(days=1), time.min, tzinfo=timezone_info)

    def _require_enterprise(self, actor, site) -> None:
        self._require(actor, "admin", "operator")
        if actor.role != "admin" and actor.organization_id != site["organization_id"]:
            raise PermissionDenied("不能操作其他组织的场所")

    def _require_regulator(self, actor) -> None:
        self._require(actor, "admin", "reviewer")

    def _receipted(self, connection, *, request_id: str, action: str,
                   payload: dict[str, Any], create: Callable[[], dict[str, Any]]) -> dict[str, Any]:
        """请求级幂等：相同请求编号返回原回执，不同内容报冲突。"""

        request_id = self._identifier(request_id, "request_id")
        payload_hash = digest(payload)
        row = connection.execute(
            "SELECT * FROM request_receipts WHERE request_id=?", (request_id,)
        ).fetchone()
        if row:
            if row["action"] != action or row["payload_hash"] != payload_hash:
                raise ConflictError("request_id 已被不同内容使用")
            response = json.loads(row["response_json"])
            response["replayed"] = True
            return response
        response = create()
        response["request_id"] = request_id
        response["replayed"] = False
        connection.execute(
            "INSERT INTO request_receipts(request_id,action,payload_hash,resource_type,resource_id,response_json,created_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (request_id, action, payload_hash, response["resource_type"], response["resource_id"],
             canonical_json(response), self._now()),
        )
        return response

    def _resolve_plan(self, connection, *, plan_id: str | None,
                      site_id: str | None, local_date: str | None):
        if plan_id:
            row = connection.execute(
                "SELECT * FROM inspection_plans WHERE plan_id=?", (plan_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError("清单不存在")
            return row
        if site_id and local_date:
            row = connection.execute(
                "SELECT * FROM inspection_plans WHERE site_id=? AND local_date=?",
                (site_id, self._local_date(local_date)),
            ).fetchone()
            if row is None:
                raise NotFoundError("当日清单不存在")
            return row
        raise ValidationError("必须提供 plan_id 或同时提供 site_id 与 local_date")

    def _plan_row(self, connection, plan_id: str):
        row = connection.execute(
            "SELECT * FROM inspection_plans WHERE plan_id=?", (plan_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError("清单不存在")
        return row

    def _current_item(self, connection, plan, item_key: str):
        return connection.execute(
            "SELECT * FROM inspection_items WHERE plan_id=? AND plan_version=? AND item_key=?",
            (plan["plan_id"], plan["version"], item_key),
        ).fetchone()

    # ---------- 清单 ----------

    def _registry_items(self, connection, site_id: str) -> list[dict[str, str]]:
        rows = connection.execute(
            "SELECT category, external_key, payload_json FROM domain_records "
            "WHERE site_id=? AND category IN ('workshop_registry','treatment_registry') "
            "ORDER BY external_key",
            (site_id,),
        ).fetchall()
        items = []
        for row in rows:
            payload = json.loads(row["payload_json"])
            if not payload.get("enabled", True):
                continue
            kind = ITEM_KINDS[row["category"]]
            items.append({
                "item_key": f"{kind}:{row['external_key']}",
                "kind": kind,
                "title": str(payload.get("name") or row["external_key"]),
                "source_external_key": row["external_key"],
            })
        items.sort(key=lambda item: item["item_key"])
        return items

    def _plan_response(self, connection, plan_id: str, *, regenerated: bool) -> dict[str, Any]:
        plan = self._plan_row(connection, plan_id)
        rows = connection.execute(
            "SELECT item_key, kind, title, source_external_key FROM inspection_items "
            "WHERE plan_id=? AND plan_version=? ORDER BY item_key",
            (plan_id, plan["version"]),
        ).fetchall()
        return {
            "resource_type": "inspection_plan",
            "resource_id": plan_id,
            "plan_id": plan_id,
            "site_id": plan["site_id"],
            "local_date": plan["local_date"],
            "plan_version": plan["version"],
            "cutoff_at": plan["cutoff_at"],
            "generated_at": plan["generated_at"],
            "regenerated": regenerated,
            "items": [dict(row) for row in rows],
        }

    def generate_plan(self, *, request_id: str, actor_id: str, site_id: str,
                      local_date: str | None = None, cutoff_at: str | None = None) -> dict[str, Any]:
        """按场所所在地日期生成应巡清单；清单内容变化时升版。"""

        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            site = self._site_row(connection, site_id)
            self._require_enterprise(actor, site)
            timezone_info = self._site_timezone(site)
            if local_date is None:
                local_date = self.clock.now().astimezone(timezone_info).date().isoformat()
            local_date = self._local_date(local_date)
            if cutoff_at is None:
                cutoff = self._default_cutoff(timezone_info, local_date)
            else:
                cutoff = self._parse_instant(cutoff_at, "cutoff_at")
            cutoff_text = _canonical(cutoff)
            payload = {"actor_id": actor_id, "site_id": site_id,
                       "local_date": local_date, "cutoff_at": cutoff_text}

            def create() -> dict[str, Any]:
                plan = connection.execute(
                    "SELECT * FROM inspection_plans WHERE site_id=? AND local_date=?",
                    (site_id, local_date),
                ).fetchone()
                desired = self._registry_items(connection, site_id)
                if plan is None:
                    plan_id = uuid.uuid4().hex
                    now = self._now()
                    connection.execute(
                        "INSERT INTO inspection_plans(plan_id,site_id,local_date,version,cutoff_at,generated_by,generated_at) "
                        "VALUES(?,?,?,1,?,?,?)",
                        (plan_id, site_id, local_date, cutoff_text, actor_id, now),
                    )
                    for item in desired:
                        connection.execute(
                            "INSERT INTO inspection_items(item_id,plan_id,plan_version,item_key,kind,title,"
                            "source_external_key,created_at) VALUES(?,?,?,?,?,?,?,?)",
                            (uuid.uuid4().hex, plan_id, 1, item["item_key"], item["kind"],
                             item["title"], item["source_external_key"], now),
                        )
                    append_event(connection, actor_id=actor_id, action="inspection_plan.generated",
                                 resource_type="inspection_plan", resource_id=plan_id,
                                 detail={"plan_id": plan_id, "site_id": site_id, "local_date": local_date,
                                         "plan_version": 1, "cutoff_at": cutoff_text,
                                         "items": [item["item_key"] for item in desired]},
                                 occurred_at=now)
                    return self._plan_response(connection, plan_id, regenerated=False)
                if plan["cutoff_at"] != cutoff_text:
                    raise ValidationError("已生成清单的截止时间不可更改")
                current_rows = connection.execute(
                    "SELECT item_key FROM inspection_items WHERE plan_id=? AND plan_version=? ORDER BY item_key",
                    (plan["plan_id"], plan["version"]),
                ).fetchall()
                current_keys = [row["item_key"] for row in current_rows]
                desired_keys = [item["item_key"] for item in desired]
                if current_keys == desired_keys:
                    return self._plan_response(connection, plan["plan_id"], regenerated=False)
                new_version = plan["version"] + 1
                now = self._now()
                connection.execute(
                    "UPDATE inspection_plans SET version=? WHERE plan_id=?",
                    (new_version, plan["plan_id"]),
                )
                for item in desired:
                    connection.execute(
                        "INSERT INTO inspection_items(item_id,plan_id,plan_version,item_key,kind,title,"
                        "source_external_key,created_at) VALUES(?,?,?,?,?,?,?,?)",
                        (uuid.uuid4().hex, plan["plan_id"], new_version, item["item_key"], item["kind"],
                         item["title"], item["source_external_key"], now),
                    )
                append_event(connection, actor_id=actor_id, action="inspection_plan.regenerated",
                             resource_type="inspection_plan", resource_id=plan["plan_id"],
                             detail={"plan_id": plan["plan_id"], "site_id": site_id, "local_date": local_date,
                                     "plan_version": new_version,
                                     "added_items": [key for key in desired_keys if key not in current_keys],
                                     "removed_items": [key for key in current_keys if key not in desired_keys]},
                             occurred_at=now)
                return self._plan_response(connection, plan["plan_id"], regenerated=True)

            return self._receipted(connection, request_id=request_id,
                                   action="generate_inspection_plan", payload=payload, create=create)

    def get_plan(self, *, plan_id: str | None = None, site_id: str | None = None,
                 local_date: str | None = None) -> dict[str, Any]:
        """查询清单当前版本及应巡项目。"""

        connection = self.database.connection
        plan = self._resolve_plan(connection, plan_id=plan_id, site_id=site_id, local_date=local_date)
        return self._plan_response(connection, plan["plan_id"], regenerated=False)

    # ---------- 凭据 ----------

    def _evidence_response(self, row, *, deduplicated: bool) -> dict[str, Any]:
        return {
            "resource_type": "evidence_version",
            "resource_id": row["evidence_id"],
            "evidence_id": row["evidence_id"],
            "plan_id": row["plan_id"],
            "item_key": row["item_key"],
            "version_no": row["version_no"],
            "evidence_hash": row["evidence_hash"],
            "captured_at": row["captured_at"],
            "submitted_at": row["submitted_at"],
            "storage_ref": row["storage_ref"],
            "late": bool(row["late"]),
            "flags": json.loads(row["flags_json"]),
            "conflict": row["version_no"] > 1,
            "deduplicated": deduplicated,
            "plan_version": row["plan_version"],
        }

    def submit_evidence(self, *, request_id: str, actor_id: str, item_key: str,
                        evidence_hash: str, captured_at: str, storage_ref: str,
                        plan_id: str | None = None, site_id: str | None = None,
                        local_date: str | None = None, note: str | None = None) -> dict[str, Any]:
        """接收一条结构化凭据；相同内容重试返回原回执，不同内容保留为新版本。"""

        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            plan = self._resolve_plan(connection, plan_id=plan_id, site_id=site_id, local_date=local_date)
            site = self._site_row(connection, plan["site_id"])
            self._require_enterprise(actor, site)
            item_key = str(item_key).strip()
            if self._current_item(connection, plan, item_key) is None:
                raise NotFoundError("应巡项目不在当前清单版本中")
            hash_text = str(evidence_hash).strip().lower()
            if not HASH_PATTERN.fullmatch(hash_text):
                raise ValidationError("evidence_hash 必须是 64 位十六进制摘要")
            captured = self._parse_instant(captured_at, "captured_at")
            captured_text = _canonical(captured)
            storage_ref = self._text(storage_ref, "storage_ref")
            note_text = None if note is None else self._text(note, "note", 500)
            submitted = self.clock.now()
            submitted_text = _canonical(submitted)
            payload = {"actor_id": actor_id, "plan_id": plan["plan_id"], "item_key": item_key,
                       "evidence_hash": hash_text, "captured_at": captured_text,
                       "storage_ref": storage_ref, "note": note_text}

            def create() -> dict[str, Any]:
                existing = connection.execute(
                    "SELECT * FROM evidence_versions WHERE plan_id=? AND item_key=? AND evidence_hash=? "
                    "AND captured_at=? AND storage_ref=?",
                    (plan["plan_id"], item_key, hash_text, captured_text, storage_ref),
                ).fetchone()
                if existing is not None:
                    return self._evidence_response(existing, deduplicated=True)
                cutoff = self._parse_instant(plan["cutoff_at"], "cutoff_at")
                late = submitted >= cutoff
                flags = []
                if captured > submitted + CLOCK_SKEW_TOLERANCE:
                    flags.append("captured_in_future")
                timezone_info = self._site_timezone(site)
                if captured.astimezone(timezone_info).date().isoformat() != plan["local_date"]:
                    flags.append("captured_outside_plan_date")
                version_no = connection.execute(
                    "SELECT COALESCE(MAX(version_no),0)+1 AS next_no FROM evidence_versions "
                    "WHERE plan_id=? AND item_key=?",
                    (plan["plan_id"], item_key),
                ).fetchone()["next_no"]
                evidence_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO evidence_versions(evidence_id,plan_id,item_key,version_no,evidence_hash,"
                    "captured_at,storage_ref,note,submitted_by,submitted_at,plan_version,late,flags_json) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (evidence_id, plan["plan_id"], item_key, version_no, hash_text, captured_text,
                     storage_ref, note_text, actor_id, submitted_text, plan["version"],
                     1 if late else 0, canonical_json(flags)),
                )
                append_event(connection, actor_id=actor_id, action="evidence.submitted",
                             resource_type="evidence_version", resource_id=evidence_id,
                             detail={"plan_id": plan["plan_id"], "item_key": item_key,
                                     "version_no": version_no, "evidence_hash": hash_text,
                                     "captured_at": captured_text, "submitted_at": submitted_text,
                                     "storage_ref": storage_ref, "late": late, "flags": flags,
                                     "plan_version": plan["version"]},
                             occurred_at=submitted_text)
                row = connection.execute(
                    "SELECT * FROM evidence_versions WHERE evidence_id=?", (evidence_id,)
                ).fetchone()
                return self._evidence_response(row, deduplicated=False)

            return self._receipted(connection, request_id=request_id,
                                   action="submit_evidence", payload=payload, create=create)

    def submit_correction(self, *, request_id: str, actor_id: str,
                          evidence_id: str, note: str) -> dict[str, Any]:
        """在截止前为已提交凭据追加纠正说明。"""

        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            evidence = connection.execute(
                "SELECT * FROM evidence_versions WHERE evidence_id=?", (evidence_id,)
            ).fetchone()
            if evidence is None:
                raise NotFoundError("凭据不存在")
            plan = self._plan_row(connection, evidence["plan_id"])
            site = self._site_row(connection, plan["site_id"])
            self._require_enterprise(actor, site)
            note_text = self._text(note, "note", 500)
            now = self.clock.now()
            if now >= self._parse_instant(plan["cutoff_at"], "cutoff_at"):
                raise ConflictError("已超过当日截止时间，不能再提交纠正说明")
            payload = {"actor_id": actor_id, "evidence_id": evidence_id, "note": note_text}

            def create() -> dict[str, Any]:
                correction_id = uuid.uuid4().hex
                now_text = _canonical(now)
                connection.execute(
                    "INSERT INTO evidence_corrections(correction_id,evidence_id,plan_id,item_key,note,"
                    "plan_version,submitted_by,submitted_at) VALUES(?,?,?,?,?,?,?,?)",
                    (correction_id, evidence_id, plan["plan_id"], evidence["item_key"],
                     note_text, plan["version"], actor_id, now_text),
                )
                append_event(connection, actor_id=actor_id, action="evidence.correction_submitted",
                             resource_type="evidence_correction", resource_id=correction_id,
                             detail={"plan_id": plan["plan_id"], "item_key": evidence["item_key"],
                                     "evidence_id": evidence_id, "note": note_text,
                                     "plan_version": plan["version"]},
                             occurred_at=now_text)
                return {"resource_type": "evidence_correction", "resource_id": correction_id,
                        "correction_id": correction_id, "evidence_id": evidence_id,
                        "plan_id": plan["plan_id"], "item_key": evidence["item_key"],
                        "plan_version": plan["version"], "submitted_at": now_text}

            return self._receipted(connection, request_id=request_id,
                                   action="submit_correction", payload=payload, create=create)

    # ---------- 监管 ----------

    def raise_dispute(self, *, request_id: str, actor_id: str,
                      evidence_id: str, reason: str) -> dict[str, Any]:
        """监管人员对某条凭据发起争议。"""

        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require_regulator(actor)
            evidence = connection.execute(
                "SELECT * FROM evidence_versions WHERE evidence_id=?", (evidence_id,)
            ).fetchone()
            if evidence is None:
                raise NotFoundError("凭据不存在")
            plan = self._plan_row(connection, evidence["plan_id"])
            reason_text = self._text(reason, "reason", 500)
            payload = {"actor_id": actor_id, "evidence_id": evidence_id, "reason": reason_text}

            def create() -> dict[str, Any]:
                dispute_id = uuid.uuid4().hex
                now_text = self._now()
                connection.execute(
                    "INSERT INTO evidence_disputes(dispute_id,plan_id,item_key,evidence_id,reason,"
                    "plan_version,status,raised_by,raised_at) VALUES(?,?,?,?,?,?,'open',?,?)",
                    (dispute_id, plan["plan_id"], evidence["item_key"], evidence_id,
                     reason_text, plan["version"], actor_id, now_text),
                )
                append_event(connection, actor_id=actor_id, action="evidence.dispute_raised",
                             resource_type="evidence_dispute", resource_id=dispute_id,
                             detail={"plan_id": plan["plan_id"], "item_key": evidence["item_key"],
                                     "evidence_id": evidence_id, "dispute_id": dispute_id,
                                     "reason": reason_text, "plan_version": plan["version"]},
                             occurred_at=now_text)
                return {"resource_type": "evidence_dispute", "resource_id": dispute_id,
                        "dispute_id": dispute_id, "evidence_id": evidence_id,
                        "plan_id": plan["plan_id"], "item_key": evidence["item_key"],
                        "status": "open", "plan_version": plan["version"], "raised_at": now_text}

            return self._receipted(connection, request_id=request_id,
                                   action="raise_dispute", payload=payload, create=create)

    def decide_evidence(self, *, request_id: str, actor_id: str, plan_id: str,
                        item_key: str, decision: str, evidence_id: str | None = None,
                        note: str | None = None) -> dict[str, Any]:
        """监管人员采信某个版本或要求补证，决定引用当时的清单版本。"""

        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require_regulator(actor)
            plan = self._plan_row(connection, plan_id)
            item_key = str(item_key).strip()
            if self._current_item(connection, plan, item_key) is None:
                raise NotFoundError("应巡项目不在当前清单版本中")
            if decision not in DECISION_TYPES:
                raise ValidationError("decision 必须是 accept_version 或 request_supplement")
            note_text = None if note is None else self._text(note, "note", 500)
            if decision == "accept_version":
                if not evidence_id:
                    raise ValidationError("采信决定必须提供 evidence_id")
                evidence = connection.execute(
                    "SELECT * FROM evidence_versions WHERE evidence_id=?", (evidence_id,)
                ).fetchone()
                if evidence is None or evidence["plan_id"] != plan_id or evidence["item_key"] != item_key:
                    raise ValidationError("凭据不属于该清单项目")
            payload = {"actor_id": actor_id, "plan_id": plan_id, "item_key": item_key,
                       "decision": decision, "evidence_id": evidence_id, "note": note_text}

            def create() -> dict[str, Any]:
                decision_id = uuid.uuid4().hex
                now_text = self._now()
                resolved: list[str] = []
                if decision == "accept_version":
                    open_rows = connection.execute(
                        "SELECT dispute_id FROM evidence_disputes WHERE plan_id=? AND item_key=? AND status='open'",
                        (plan_id, item_key),
                    ).fetchall()
                    resolved = [row["dispute_id"] for row in open_rows]
                    connection.execute(
                        "UPDATE evidence_disputes SET status='resolved', resolved_by=?, resolved_at=?, "
                        "resolution='version_accepted' WHERE plan_id=? AND item_key=? AND status='open'",
                        (actor_id, now_text, plan_id, item_key),
                    )
                connection.execute(
                    "INSERT INTO evidence_decisions(decision_id,plan_id,item_key,decision_type,evidence_id,"
                    "note,plan_version,decided_by,decided_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (decision_id, plan_id, item_key, decision, evidence_id, note_text,
                     plan["version"], actor_id, now_text),
                )
                action = ("evidence.version_accepted" if decision == "accept_version"
                          else "evidence.supplement_requested")
                append_event(connection, actor_id=actor_id, action=action,
                             resource_type="evidence_decision", resource_id=decision_id,
                             detail={"plan_id": plan_id, "item_key": item_key,
                                     "decision_id": decision_id, "decision_type": decision,
                                     "evidence_id": evidence_id, "note": note_text,
                                     "plan_version": plan["version"],
                                     "resolved_disputes": resolved},
                             occurred_at=now_text)
                return {"resource_type": "evidence_decision", "resource_id": decision_id,
                        "decision_id": decision_id, "plan_id": plan_id, "item_key": item_key,
                        "decision_type": decision, "evidence_id": evidence_id,
                        "plan_version": plan["version"], "resolved_disputes": resolved,
                        "decided_at": now_text}

            return self._receipted(connection, request_id=request_id,
                                   action="decide_evidence", payload=payload, create=create)

    # ---------- 单日还原 ----------

    def day_report(self, *, site_id: str, local_date: str) -> dict[str, Any]:
        """还原某天是否完成、为何逾期、哪些凭据仍有争议。"""

        connection = self.database.connection
        self._site_row(connection, site_id)
        local_date = self._local_date(local_date)
        plan = connection.execute(
            "SELECT * FROM inspection_plans WHERE site_id=? AND local_date=?",
            (site_id, local_date),
        ).fetchone()
        if plan is None:
            raise NotFoundError("当日清单不存在")
        now = self.clock.now()
        cutoff = self._parse_instant(plan["cutoff_at"], "cutoff_at")
        window_open = now < cutoff
        items = connection.execute(
            "SELECT * FROM inspection_items WHERE plan_id=? AND plan_version=? ORDER BY item_key",
            (plan["plan_id"], plan["version"]),
        ).fetchall()
        report_items = []
        outstanding = []
        disputed = []
        for item in items:
            versions = connection.execute(
                "SELECT * FROM evidence_versions WHERE plan_id=? AND item_key=? ORDER BY version_no",
                (plan["plan_id"], item["item_key"]),
            ).fetchall()
            version_dicts = []
            on_time = False
            for version in versions:
                corrections = connection.execute(
                    "SELECT * FROM evidence_corrections WHERE evidence_id=? "
                    "ORDER BY submitted_at, correction_id",
                    (version["evidence_id"],),
                ).fetchall()
                version_dicts.append({
                    "evidence_id": version["evidence_id"],
                    "version_no": version["version_no"],
                    "evidence_hash": version["evidence_hash"],
                    "captured_at": version["captured_at"],
                    "submitted_at": version["submitted_at"],
                    "storage_ref": version["storage_ref"],
                    "note": version["note"],
                    "late": bool(version["late"]),
                    "flags": json.loads(version["flags_json"]),
                    "plan_version": version["plan_version"],
                    "corrections": [
                        {"correction_id": row["correction_id"], "note": row["note"],
                         "submitted_by": row["submitted_by"], "submitted_at": row["submitted_at"],
                         "plan_version": row["plan_version"]}
                        for row in corrections
                    ],
                })
                if not version["late"]:
                    on_time = True
            accepted_id = None
            supplement_requested = False
            decisions = connection.execute(
                "SELECT * FROM evidence_decisions WHERE plan_id=? AND item_key=? ORDER BY rowid",
                (plan["plan_id"], item["item_key"]),
            ).fetchall()
            for decision_row in decisions:
                if decision_row["decision_type"] == "accept_version":
                    accepted_id = decision_row["evidence_id"]
                    supplement_requested = False
                else:
                    supplement_requested = True
            open_disputes = connection.execute(
                "SELECT * FROM evidence_disputes WHERE plan_id=? AND item_key=? AND status='open' "
                "ORDER BY rowid",
                (plan["plan_id"], item["item_key"]),
            ).fetchall()
            if not versions:
                status = "missing"
            elif open_disputes:
                status = "disputed"
            elif accepted_id:
                status = "accepted"
            else:
                status = "submitted"
            if not on_time:
                entry: dict[str, Any] = {"item_key": item["item_key"],
                                         "reason": "missing" if not versions else "late"}
                if versions:
                    entry["first_submitted_at"] = versions[0]["submitted_at"]
                outstanding.append(entry)
            for dispute in open_disputes:
                disputed.append({"dispute_id": dispute["dispute_id"],
                                 "evidence_id": dispute["evidence_id"],
                                 "item_key": dispute["item_key"],
                                 "reason": dispute["reason"],
                                 "raised_by": dispute["raised_by"],
                                 "raised_at": dispute["raised_at"],
                                 "plan_version": dispute["plan_version"]})
            report_items.append({
                "item_key": item["item_key"],
                "kind": item["kind"],
                "title": item["title"],
                "status": status,
                "on_time": on_time,
                "accepted_evidence_id": accepted_id,
                "supplement_requested": supplement_requested,
                "open_disputes": len(open_disputes),
                "versions": version_dicts,
            })
        if all(item["on_time"] for item in report_items):
            day_status = "complete"
        elif window_open:
            day_status = "incomplete"
        else:
            day_status = "overdue"
        return {
            "site_id": site_id,
            "local_date": local_date,
            "plan_id": plan["plan_id"],
            "plan_version": plan["version"],
            "cutoff_at": plan["cutoff_at"],
            "generated_at": plan["generated_at"],
            "now": _canonical(now),
            "window_open": window_open,
            "day_status": day_status,
            "outstanding_items": outstanding,
            "disputed_evidence": disputed,
            "items": report_items,
        }

    def plan_timeline(self, plan_id: str) -> list[dict[str, Any]]:
        """按审计链顺序还原该清单的不可改写时间线。"""

        connection = self.database.connection
        self._plan_row(connection, plan_id)
        events = []
        for row in connection.execute("SELECT * FROM audit_events ORDER BY sequence"):
            detail = json.loads(row["detail_json"])
            if row["resource_id"] == plan_id or detail.get("plan_id") == plan_id:
                events.append({
                    "sequence": row["sequence"],
                    "event_id": row["event_id"],
                    "actor_id": row["actor_id"],
                    "action": row["action"],
                    "resource_type": row["resource_type"],
                    "resource_id": row["resource_id"],
                    "occurred_at": row["occurred_at"],
                    "detail": detail,
                    "event_hash": row["event_hash"],
                })
        return events
