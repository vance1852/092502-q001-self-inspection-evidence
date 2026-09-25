# 企业环保自巡基础服务

维护企业、环保负责人、涉污场所与日常操作回执，为自巡业务提供统一主体、幂等写入和审计能力，并在其之上提供每日自巡证据模块。

系统采用 Python 标准库和 SQLite，可在单个 Linux 进程中运行。现有能力包括操作者与角色登记、场所台账、领域资料记录、请求幂等校验、哈希串联审计以及轻量 HTTP/JSON 接口。领域资料类别为：enterprise_profile、workshop_registry、treatment_registry、operator_assignment。所有写操作都在事务中完成，同一请求编号携带相同内容时返回原结果，内容发生变化时返回业务冲突。

## 每日自巡证据模块

在主体、场所、领域资料和审计链之上，按企业所在地日期生成应巡清单并管理当日凭据：

- **清单生成**：`workshop_registry` 中的涉污车间生成 `workshop_sealed`（车间密闭）项目，`treatment_registry` 中的治污设施生成 `treatment_running`（设施运行）项目；清单按场所时区的自然日生成，默认截止时间为当日本地午夜。台账变化后重新生成会升高清单版本，旧版本项目保留可查。
- **凭据提交**：接收结构化凭据摘要（64 位十六进制）、拍摄时刻（含时区）和存储引用。完全相同的重试返回原回执；同一项目的不同内容按版本号递增保留，绝不覆盖。跨午夜补传按服务端接收时间标记 `late`；拍摄时刻晚于接收时间或不在清单日期内时标记时钟异常标志，凭据仍保留。
- **纠正与监管**：企业可在截止前为凭据追加纠正说明；监管人员可发起争议、采信某个版本或要求补证。所有决定记录当时的清单版本，并写入哈希串联的审计时间线。
- **单日还原**：日报接口给出某天是否完成（complete / incomplete / overdue）、逾期原因（项目缺失或仅有逾期补传）以及仍有争议的凭据列表；时间线接口按审计链顺序还原当日全部事件。结论只依赖 SQLite 中的数据与当前时钟，服务重启后保持一致。

## 目录

- `src/self_inspection_core/`：领域模型、SQLite 存储、权限服务、审计链、每日自巡证据模块、HTTP 路由和离线验收；
- `tests/`：核心规则、事务边界、证据模块、接口路由和端到端验收测试。

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

命令会在临时 SQLite 数据库中登记操作者、场所和领域资料，生成两天的应巡清单，演练幂等重试、内容去重、冲突版本、截止前纠正、争议采信、跨午夜逾期补传和服务重启，成功时输出一行 `status` 为 `ok` 的 JSON 并以退出码 `0` 结束。

## 单日结论还原

```bash
PYTHONPATH=src python3 -m self_inspection_core.day_report --database self_inspection_core.sqlite3 --site site-001 --date 2026-09-25
```

命令输出该场所当日是否完成、为何逾期、哪些凭据仍有争议的 JSON 报告。

## HTTP 服务

```bash
PYTHONPATH=src python3 -m self_inspection_core.api --database self_inspection_core.sqlite3 --host 127.0.0.1 --port 8080
```

健康检查使用 `GET /health`。业务写入接口通过 `X-Actor-Id` 标识操作者，支持操作者、场所和领域资料的登记，以及审计事件查询。服务重启后，SQLite 中的业务状态和审计链继续保留。

自巡证据接口：

- `POST /inspection-plans`：生成或升版当日清单（`site_id`、`local_date` 可选、`cutoff_at` 可选）；
- `GET /inspection-plans`：按 `plan_id` 或 `site_id`+`local_date` 查询清单与应巡项目；
- `POST /inspection-evidence`：提交凭据（`item_key`、`evidence_hash`、`captured_at`、`storage_ref`）；
- `POST /inspection-corrections`：截止前为凭据追加纠正说明；
- `POST /inspection-disputes`：监管人员对凭据发起争议；
- `POST /inspection-decisions`：监管人员采信版本（`accept_version`）或要求补证（`request_supplement`）；
- `GET /inspection-day-report`：按 `site_id`+`local_date` 还原单日结论；
- `GET /inspection-timeline`：按 `plan_id` 还原不可改写的事件时间线。
