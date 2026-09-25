# 企业环保自巡基础服务

维护企业、环保负责人、涉污场所与日常操作回执，为自巡业务提供统一主体、幂等写入和审计能力，并在其之上实现每日自巡证据模块：按企业所在地日期生成应巡清单，接收结构化凭据，保留冲突版本，支持纠正说明、监管争议、采信与补证，形成不可改写的时间线。

系统采用 Python 标准库和 SQLite，可在单个 Linux 进程中运行。现有能力包括操作者与角色登记、场所台账、领域资料记录、请求幂等校验、哈希串联审计以及轻量 HTTP/JSON 接口。领域资料类别为：enterprise_profile、workshop_registry、treatment_registry、operator_assignment。所有写操作都在事务中完成，同一请求编号携带相同内容时返回原结果，内容发生变化时返回业务冲突。

## 每日自巡证据规则

- **应巡清单**：按场所时区的当地日期生成，项目来源于该场所的 workshop_registry（涉污车间密闭）与 treatment_registry（治污设施运行）资料，`enabled: false` 的资料不参与。资料指纹变化时生成新的清单版本，旧版本保留；监管决定记录当时的清单版本。
- **凭据提交**：提交 `evidence_hash`（64 位小写十六进制摘要）、`captured_at`（必须带时区）与 `storage_ref`。归属日期由 `captured_at` 按场所时区推导，因此跨午夜补传仍计入拍摄当日。`captured_at` 比服务器时间快超过 300 秒视为设备时钟偏差并被拒绝。接收时间晚于当地日期截止时刻（次日 00:00，场所时区）的凭据标记 `late`。
- **幂等与冲突**：同一 `request_id` 携带相同内容返回原回执；不同请求编号但内容完全相同的重复提交去重并返回原凭据；同一项目的不同内容保留为新版本（version 递增）而不覆盖，项目进入冲突状态直至监管采信。
- **纠正说明**：企业（operator/admin）可在截止前为当日项目提交多条纠正说明，截止后提交返回冲突错误。
- **监管决定**：监管人员（reviewer/admin）可发起争议（dispute）、采信某个版本（accept，须指定属于该项目的 evidence_id）或要求补证（request_supplement）。争议与补证必须填写说明；项目的处置状态由最新决定决定。
- **时间线**：清单生成、凭据、纠正、决定全部写入哈希串联审计，`GET /inspections/timeline` 按当日过滤还原，`audit_valid` 可离线校验。
- **一致性**：写事务串行化，并发首交按到达顺序形成版本 1、2；服务重启后 SQLite 中的业务状态、回执与审计链继续保留，日结报告结论一致。

## 目录

- `src/self_inspection_core/`：领域模型、SQLite 存储、权限服务、审计链、每日自巡证据模块、HTTP 路由和离线验收；
- `tests/`：核心规则、事务边界、接口路由、自巡证据规则和端到端验收测试。

## 测试

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

## 构建检查

```bash
python3 -m compileall -q src tests
```

## 离线验收

```bash
PYTHONPATH=src python3 -m self_inspection_core.acceptance
```

命令会在临时 SQLite 数据库中登记操作者、场所和领域资料，核对幂等回执与审计链，并完整演练一天的自巡流程：生成清单、提交与重放凭据、保留冲突版本、截止前纠正、监管争议与采信、设备时钟偏差拦截、跨午夜补传、截止后纠正拒绝、服务重启后结论一致。成功时输出一行 `status` 为 `ok` 的 JSON（`inspection` 字段含各项检查）并以退出码 `0` 结束。

## HTTP 服务

```bash
PYTHONPATH=src python3 -m self_inspection_core.api --database self_inspection_core.sqlite3 --host 127.0.0.1 --port 8080
```

健康检查使用 `GET /health`。业务写入接口通过 `X-Actor-Id` 标识操作者，支持操作者、场所和领域资料的登记，以及审计事件查询。服务重启后，SQLite 中的业务状态和审计链继续保留。

### 每日自巡接口

- `POST /inspections/checklists`：生成（或返回）当日清单，可指定 `local_date`，缺省为场所当地当天；
- `POST /inspections/evidence`：提交凭据，字段为 `request_id`、`site_id`、`item_key`、`evidence_hash`、`captured_at`、`storage_ref`；
- `POST /inspections/corrections`：截止前提交纠正说明，字段为 `request_id`、`site_id`、`local_date`、`item_key`、`content`；
- `POST /inspections/decisions`：监管决定，字段为 `request_id`、`site_id`、`local_date`、`item_key`、`action`（dispute/accept/request_supplement）、`note`、`evidence_id`（采信时必填）；
- `GET /inspections/daily?site_id=&date=`：日结报告，还原某天是否完成、为何逾期（missing_evidence / first_evidence_after_deadline）、哪些凭据仍有争议；
- `GET /inspections/timeline?site_id=&date=`：当日不可改写的操作时间线（来自哈希链审计事件）。
