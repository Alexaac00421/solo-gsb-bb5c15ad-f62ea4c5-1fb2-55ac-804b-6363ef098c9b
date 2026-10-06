# 计量校准证书追溯服务

可离线运行的计量校准证书签发与**量值溯源链**管理服务。以基准（标准）设备的合格证书为
追溯起点，普通校准证书必须引用一张在**校准时刻有效且合格**的标准设备证书；撤销上游
证书时，其全部下游证书级联失效。离线采集的校准记录可作为**交接批次**整批签发：
批次键幂等、整批原子落库、批内证书可相互引用。已签发证书的设备名称、操作员、备注
录入有误时，可登记**更正声明**：声明按登记顺序生效、只增不改，原证书内容不受影响。
证书撤销后，客服可按**撤销批次追查已交付凭据的影响面**：哪些交付归档包中的
哪些目标证书，其归档时刻冻结的追溯链包含本批被撤销（含级联失效）的证书。
交付归档包还支持**邀请制签收**：交付方凭本地配置的管理密钥，按归档包编号、
收件人和未来截止时间创建签收邀请，得到邀请编号与**仅展示一次**的一次性随机
密钥（明文不落库）；收件人在截止前提交 `ACCEPT`/`REJECT`（拒收须填原因），
**每个归档包只允许一份定稿**，同邀请同内容重试（截止后亦然）返回首次结果；
`ACCEPT` 时若包内目标或其冻结追溯链中已有证书被撤销/失效则拒绝并点名受影响
目标，定稿记录可凭管理密钥查询。已定稿 `ACCEPT` 的归档包若事后受证书撤销
波及，撤销事务会按归档时冻结的追溯链为该包生成一份**撤销风险告知**（列出
撤销批次、受影响目标及链上命中编号，同包同批只记一份，未签收或 `REJECT`
不生成）；收件人凭**定稿邀请编号与原一次性密钥**查询、确认告知——密钥过期
不影响已签收后的确认，重复确认返回首次确认时间。
计量人员发现标准证书有待复核时，可凭管理密钥登记**临时停用**（非空原因），
停用从登记成功时刻起生效：停用期间新签发普通证书不得以该证书或其任何下游
证书为追溯依据（单张与交接批次、批内引用均强制校验，命中整批不落库）；
复核后凭管理密钥解除停用；可查询当前停用状态及历次停用/解除的时间与原因。
停用不改写证书内容、撤销状态与已冻结的交付快照。
需双人复核的普通校准证书可走**复核签发**：录入人凭本地配置的录入凭据提交
申请号与完整签发资料（申请仅保存待审内容，不占用证书编号或设备有效期），
另一身份的复核人凭独立凭据附原因**驳回**，或**批准**并在同一事务中按批准
当时的标准证书状态、临时停用与现有签发规则真正签发，将结论、复核人与证书
绑定；驳回后不能再批准，并发复核只允许一份定稿，同申请同操作重试返回原结果。
面向监管与内审，还可按设备**审计过去半开区间内的校准覆盖**：按时间顺序还原
完整、无重叠的合规与空窗片段，合规片段列出当时可用的合格普通证书编号。
实验室还可按**标准证书控制每日校准使用量**：管理员凭管理密钥为“证书编号 ×
UTC 日期”设定非负上限，成功时返回上限与实时已用次数（当日已签发数超过新
上限则拒绝调整，撤销或失效不退还已用次数）；普通证书按 `calibrated_at` 的
UTC 日期及**直接引用**的上游证书在真正签发时计一次，未配置上限的键沿用原
签发规则；单张签发、交接批次（批内多张相同键合并计数）与双人复核批准均在
同一事务原子扣额，超限拒绝本次签发并指出上游证书与日期，批次整批不落库、
复核申请保持待审；并发调整与签发按提交次序串行绝不超额，既有批次幂等重放
只返回首次结果、不重复计数。
计量机构接收**设备校准委托工单**：管理员凭管理密钥登记唯一工单号、设备标识、
委托人与带时区的计划校准半开区间（同号同内容重试返回原工单，同号异内容拒绝），
工单初始待完成、仅可在该状态取消；结清时提交工单号与已签发证书编号，只接受
设备标识一致、校准时刻落在计划时段内且当前 `ACTIVE`、`PASS` 的普通证书，一张
证书不得结清两个工单；结清后绑定与结清时刻固定（同工单同证书重试返回原结果，
改绑或取消报冲突），**不预占**证书编号、有效期或每日限额，既有单张、批次与
双人复核签发规则照常；证书日后撤销或失效**不倒退**工单状态，查询工单返回绑定
证书的当前状态。

- 技术栈：Python 3.11 · FastAPI · PostgreSQL 16 · psycopg3
- 关键业务不变量由**数据库约束 + 触发器 + 单事务**保证，不依赖应用层“先查后写”，
  因此在并发下依然成立。

---

## 目录结构

```
.
├── app/                 # FastAPI 应用
│   ├── main.py          # 入口、异常处理、健康检查
│   ├── routers.py       # HTTP 路由
│   ├── repository.py    # 核心业务规则（签发校验/成环检测/级联失效）
│   ├── schemas.py       # 请求/响应模型
│   ├── db.py            # 连接池
│   ├── config.py        # 环境变量配置
│   └── errors.py        # 统一业务错误
├── db/init.sql          # 数据库初始化（枚举、约束、触发器、视图、批次/更正/交付归档/签收/停用/复核申请/撤销风险告知/每日用量上限/委托工单表）
├── scripts/             # 冒烟测试脚本（可直接对运行中的服务执行）
├── Dockerfile
├── docker-compose.yml
└── requirements.txt
```

---

## 一键启动（Docker Compose）

```bash
docker compose up -d --build
```

首次启动时：

1. 启动 PostgreSQL 16，并在**空数据卷**上自动执行 `db/init.sql` 完成建表；
2. 等待数据库健康检查通过后启动 API。

服务地址与端口：

| 服务 | 地址 |
| --- | --- |
| API 基址 | <http://localhost:8080> |
| 健康检查 | <http://localhost:8080/health> |
| 交互式文档（Swagger） | <http://localhost:8080/docs> |
| OpenAPI 描述 | <http://localhost:8080/openapi.json> |
| PostgreSQL（仅绑定宿主机回环地址） | `127.0.0.1:5432` |

停止与清理：

```bash
docker compose down          # 停止，保留数据卷（证书记录保留）
docker compose down -v       # 停止并删除数据卷（下次启动重新初始化空库）
```

> 完全离线部署：在有网络的机器上先 `docker compose build`（或 `docker save` 导出
> `python:3.11-slim` 与 `postgres:16-alpine` 镜像），拷贝到内网环境 `docker compose up -d`
> 即可，运行期不访问任何外部地址。

---

## 配置

所有配置通过环境变量注入，可在项目根目录放置 `.env`（参考 `.env.example`）：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `APP_PORT` | `8080` | API 在宿主机暴露的端口 |
| `POSTGRES_USER` | `caltrace` | 数据库用户 |
| `POSTGRES_PASSWORD` | `caltrace` | 数据库密码（生产请修改） |
| `POSTGRES_DB` | `caltrace` | 数据库名 |
| `POSTGRES_PORT` | `127.0.0.1:5432` | 数据库在宿主机的绑定地址:端口 |
| `DB_HOST` / `DB_PORT` | `db` / `5432` | API 容器内连接数据库用（compose 已固定） |
| `DELIVERY_MANAGEMENT_KEY` | `caltrace-delivery-admin` | 管理密钥：创建签收邀请、查询定稿记录，标准证书临时停用的登记/解除/查询，标准证书每日校准使用量上限的设定/调整/查询，以及设备校准委托工单的登记/取消/结清/查询，均经 `X-Management-Key` 头校验；默认值仅供本机开发，生产必须覆盖为随机长密钥 |
| `CERTIFICATE_ENTRY_KEYS` | `entry-1=caltrace-entry-key` | 普通证书双人复核的**录入人凭据表**：逗号分隔，每项 `身份名=凭据`；提交申请经 `X-Entry-Key` 头校验。仅本地配置，默认值仅供本机开发 |
| `CERTIFICATE_REVIEWER_KEYS` | `reviewer-1=caltrace-review-key-1,reviewer-2=caltrace-review-key-2` | 普通证书双人复核的**复核人凭据表**：格式同上；驳回/批准经 `X-Reviewer-Key` 头校验。复核凭据必须与该申请的录入凭据不同（同凭据复核 `403`）；默认值仅供本机开发 |

### 不用 Compose、直连已有 PostgreSQL

```bash
pip install -r requirements.txt
psql "$DB_URL" -f db/init.sql          # 需 superuser 创建 btree_gist 扩展
DB_HOST=127.0.0.1 DB_PORT=5432 DB_NAME=caltrace DB_USER=caltrace DB_PASSWORD=secret \
  uvicorn app.main:app --host 0.0.0.0 --port 8080
```

要求 PostgreSQL 14+，当前库内可执行 `CREATE EXTENSION btree_gist`（contrib 自带）。

---

## 业务规则

1. **追溯起点**：基准设备证书（`kind=REFERENCE`）无上游引用，结论恒为合格。
2. **引用有效性**：普通证书（`kind=CALIBRATION`）必须引用一张标准设备证书，
   且该校准证书在本证书的**校准时刻**满足：状态生效、结论合格、校准时刻落在其
   有效期 `[valid_from, valid_to)` 内。已撤销（REVOKED）或已失效（INVALID）的
   证书不能再作为校准依据。
3. **引用链不可成环**：树状溯源结构 + 签发时防御性成环检测 + 递归 CTE `CYCLE` 保护。
4. **有效期不重叠**：同一设备处于 ACTIVE 的证书，有效期区间不得重叠
   （`EXCLUDE USING gist` 排他约束，半开区间 `[from, to)`，首尾相接允许）。
   证书被撤销/失效后区间释放，可重新签发。
5. **不可改写**：证书签发后业务字段禁止 UPDATE、禁止 DELETE（数据库触发器强制，
   直连数据库修改也会被拒绝）；状态只允许 `ACTIVE → REVOKED/INVALID`。
6. **级联失效**：撤销一张证书 → 其所有直接/间接下游 ACTIVE 证书在**同一事务**中
   置为 INVALID，受影响证书及完整引用链快照写入 `invalidations` 表，历史签发内容
   永久可查。
7. **并发安全**：
   - 相同证书编号并发签发 → 唯一约束保证仅一张成功，其余 `409 cert_duplicate`；
   - 同设备重叠区间并发签发 → 应用预检 `422 validity_overlap`，竞态穿透时由 GiST
     排他约束兜底返回 `409 validity_overlap_concurrent`；
   - 撤销与新签发并发 → 撤销事务持有被撤销证书行锁与全部下游行锁，
     新引用插入的外键检查会阻塞到撤销提交，随后按“引用已失效”被拒绝，
     不会出现“失效证书还能被引用”的窗口。
8. **批次签发（离线交接）**：`POST /api/v1/certificate-batches` 携带唯一批次键与
   非空证书列表，每项沿用单张签发的全部字段与规则；普通证书可引用库内证书或
   本批证书（**列表顺序不代表引用顺序**，服务按引用拓扑排序后逐张校验落库）。
   批内编号唯一、引用无环、上游当前生效且合格且校准时刻落在其有效期内、同设备
   生效有效期不重叠——**任一记录不合规则整批不落库**，错误响应以
   `details.cert_no` 定位出错编号。批次键为幂等键：同键同请求重放首次签发结果
   快照（即使证书事后被撤销），同键不同请求返回 `409 batch_conflict`；
   并发重复提交由批次键主键兜底，仅一个事务真正落库。
9. **更正声明（展示字段纠错）**：已签发证书的 `device_name` / `operator` / `remark`
   录入有误时，可登记更正声明（唯一声明编号 + 证书编号 + 非空修正字段集合 +
   非空原因）。仅这三个展示字段可更正；修正集合中字段值为 `null` 表示**显式置空**，
   与**未提交**的字段（保持原值）严格区分。每张证书的声明按成功登记顺序编号
   （`seq`，从 1 开始）并依序生效——同一字段后登记的声明覆盖先登记的。
   声明写入只增不改的 `correction_declarations` 表，**不改写原证书**：证书详情、
   追溯链、批次首次签发快照保持既有内容与响应契约；已撤销（REVOKED）或级联
   失效（INVALID）的证书同样可更正展示资料，其状态、有效期与签发、撤销判定
   不受影响。声明编号为幂等键：同编号同内容重试返回首次声明（HTTP 200），
   同编号不同内容返回 `409 correction_conflict`；同一证书的并发更正由证书行锁
   串行化，声明顺序不丢失、不乱序。
10. **客户交付归档包（可交付凭据冻结）**：向客户交付一组校准证书时，以归档包
    编号 + 非空证书编号列表（**顺序即交付顺序**）冻结当时可交付的追溯凭据。
    仅接受：包内编号互不重复；目标均为 `CALIBRATION`；目标**及各自完整上游链**
    全部处于 `ACTIVE` 且结论 `PASS`。任一证书不存在或不合格，**整包不保存**，
    错误响应分类列出全部相关编号（`details.cert_nos` 及各分类）。成功后每张
    目标证书按提交顺序返回：**原始签发内容**、**从基准到目标的完整链**、按
    归档时刻**已登记更正声明合并后的展示字段**；连同创建时刻一起作为不可改写
    的快照存入 `delivery_archives` 表。归档包编号为幂等键：编号与有序列表均
    相同的重试返回首次快照（HTTP 200，即使证书事后被更正或撤销）；同编号
    不同列表（含仅顺序不同）返回 `409 archive_conflict`；并发同编号由主键
    兜底。快照对应**同一个一致的业务时点**：创建事务对目标及全部上游行按
    “基准→下游”顺序加共享行锁（`FOR SHARE`），与并发撤销（`FOR UPDATE`）、
    并发更正（目标行 `FOR UPDATE`）的加锁顺序一致，二者不会混合新旧状态——
    并发撤销/更正要么先于归档提交（归档按新状态校验或合并），要么被阻塞到
    归档提交之后（归档整体按旧状态快照）；理论等待环由死锁检测终止并重试。
11. **撤销影响面追查（已交付凭据）**：`GET /api/v1/revocations/{batch_id}/archive-impact`
    按撤销批次列出受影响的已交付归档凭据——凡目标证书在**归档时刻冻结的追溯链**
    （归档快照，非当前状态）中包含本批被撤销证书集合（被主动撤销的证书 +
    全部级联失效证书）中任一证书者，即入清单。每行输出：归档包编号、包内目标
    证书编号、原交付序位、被撤销证书在冻结链中的序位、归档时刻；两种序位均
    从 1 开始。同一目标在同一包内只列一次（链上命中多张本批证书也去重为一行），
    同一目标出现在多个归档包时分别列出，同包未受影响项不出现。清单直接读取
    归档快照，覆盖此前已存在的全部归档包；归档创建与撤销并发时按两者实际
    提交次序判定（撤销先提交则含本批证书的归档被拒、从不落库，归档先提交则
    其快照入清单），被拒创建的包不入清单。按（归档包编号, 原交付序位）稳定
    排序分页，相关表均只增不改，重复查询结果稳定；未知批次返回
    `404 revocation_not_found`，无受影响归档返回空页（`total=0`、`items=[]`）。
12. **设备校准覆盖审计（合规/空窗片段还原）**：
    `GET /api/v1/devices/{device_id}/coverage-audit?from=...&to=...` 审计指定
    设备在过去半开区间 `[from, to)` 内的校准覆盖，按时间顺序返回完整、无重叠
    的合规（`COMPLIANT`）与空窗（`GAP`）片段；相邻且状态与证书编号集合相同的
    片段合并，合规片段列出当时可用的合格普通证书编号。计入规则：仅该设备的
    **合格普通证书**（`CALIBRATION` 且 `PASS`）提供覆盖；每张证书的计入窗口为
    `[max(签发创建时刻, 有效期起点), min(有效期终点, 撤销/级联失效时刻))`——
    即证书从签发创建时刻与有效期起点中较晚者开始计入，到有效期终点或撤销、
    级联失效时刻中较早者退出。上游证书的有效期只按下游校准时刻的既有签发
    规则判断（签发时已校验），审计不再回溯重判：上游事后到期不取消下游覆盖，
    上游事后撤销则下游覆盖按其级联失效时刻截断。历史重签（撤销/失效释放区间
    后再签发，有效期可与原证书重叠）按各证书实际的签发与失效事件还原，不以
    当前状态替代历史状态；并发交错时同一时刻可能被多张证书覆盖，合规片段
    如实列出全部可用编号（升序）。未知设备返回整段空窗（不报 404）；
    起止必须携带时区、起点必须早于终点、终点不得晚于查询时刻，否则
    `422 invalid_audit_window`。响应中的时间统一为 UTC（带时区 ISO 8601，
    与输入为同一时刻）。
13. **标准证书临时停用（待复核）**：计量人员发现某张生效中的标准证书
    可凭本地配置的管理密钥（同一把 `X-Management-Key` /
    `DELIVERY_MANAGEMENT_KEY`）登记临时停用：输入证书编号与**非空原因**，
    停用从**登记成功（事务提交）时刻**起生效。停用期间，新签发的普通证书
    不得以该证书**或其任何下游证书**为追溯依据——单张签发与交接批次签发
    均强制校验，批次的外部引用与**批内引用**同样适用；命中时返回
    `422 reference_suspended`，错误详情指出**受阻证书**（被直接引用的证书）
    与**停用源**（链上最近的停用证书编号，可能是其上游的上游），批次整批
    不落库。停用/解除/并发签发在数据库行锁上串行化，严格按**提交次序**生效：
    停用先提交则随后提交的引用受阻签发必被拒，签发先提交则其证书落库
    （停用不溯及既往），理论等待环由死锁检测终止并重试
    （`409 issuance_busy` / `suspension_busy`，调用方可重试）。复核结束后
    可凭管理密钥与非空原因**解除停用**，该链即刻恢复为可引用状态；重复停用、
    在无生效停用时解除均返回冲突（`409 suspension_conflict` /
    `suspension_not_active`）。停用记录只增不改（解除字段一次性写入），查询
    展示**当前停用状态**及历次停用、解除的**时间和原因**。停用不触碰
    `certificates` 表：既有证书内容、`ACTIVE/REVOKED/INVALID` 状态、撤销记录、
    已冻结的交付归档快照均不因停用改写；撤销也不改写停用记录（撤销后停用
    记录仍独立存在，仍可解除）；同键同内容的批次重试仍返回**首次签发快照**。
    停用不是撤销：停用期间证书自身仍为 ACTIVE/PASS，不阻止新的交付归档、
    更正声明与签收 ACCEPT。
14. **交付签收（邀请 + 每包一份定稿）**：
    - **创建邀请**：交付方在 `X-Management-Key` 头中携带本地配置的管理密钥，
      以**已存在的归档包编号 + 收件人 + 未来截止时间**创建签收邀请。服务端
      生成邀请编号（`DINV-…`）与 192 位一次性随机密钥；密钥**仅在创建响应中
      明文出现一次**，库内只保存其 SHA-256（`key_hash`），明文不持久化、
      不记日志。归档包不存在（`404 archive_not_found`）、管理密钥缺失/错误
      （`401 invalid_management_key`）或截止时间非未来值
      （`422 invitation_deadline_past`）时拒绝；同一归档包可创建多份邀请。
    - **提交定稿**：收件人在**截止时刻之前**（不含整刻）提交邀请编号、
      一次性密钥与 `ACCEPT`/`REJECT` 结论。未知邀请 `404 invitation_not_found`、
      密钥错误 `401 invalid_receipt_key`；`REJECT` 必须携带非空白原因
      （`422 receipt_reason_required`），`ACCEPT` 不带原因。**每个归档包至多
      一份定稿**（数据库主键强制）：同一邀请、同一内容的重试原样返回首次
      定稿结果（HTTP 200，**截止之后重放亦然**）；不同内容或用其他邀请再
      提交一律 `409 receipt_conflict`。尚未定稿的邀请过期后，即使密钥正确
      也不能签收（`409 invitation_expired`，`ACCEPT`/`REJECT` 均如此）。
    - **ACCEPT 的撤销/失效复核**：定稿 `ACCEPT` 时，若包内**任一目标证书
      自身**、或其**归档冻结追溯链中的任一证书**当前已撤销（REVOKED）或
      失效（INVALID），则拒绝定稿并返回 `422 receipt_targets_revoked`，
      `details.affected_targets` 列出全部受影响的**包内目标**编号（排序
      去重）；`REJECT` 不受此限制，撤销/失效后仍可提交。状态以签收事务
      内读取的**当前**证书表为准，链拓扑取归档快照（“归档时合格、签收前
      链上被撤销”会被点名）。
    - **签收与撤销并发**：`ACCEPT` 事务对目标及全部上游链按“基准→下游”
      顺序加共享行锁（`FOR SHARE`，与归档创建一致），与并发撤销
      （`FOR UPDATE`）按**实际提交顺序**判定——撤销先提交则本次 ACCEPT
      点名受影响目标并拒绝（不留定稿），签收先提交则定稿成立。定稿一旦
      写入不可改写（只增不改触发器），**后续撤销不改写签收记录与归档
      快照**；`REJECT` 不触碰证书行，与撤销互不阻塞。
    - **管理查询**：凭管理密钥 `GET /api/v1/delivery-archives/{archive_no}/receipt`
      返回归档号、收件人、结论、拒收原因与签收时刻；密钥错误 `401`，
      归档包不存在 `404 archive_not_found`，归档包存在但尚未定稿
      `404 receipt_not_found`。
15. **普通证书双人复核签发（录入申请 + 另一身份复核定稿）**：
    - **凭据模型（本轮新增）**：在管理密钥之外新增两张**本地身份凭据表**
      （环境变量 `CERTIFICATE_ENTRY_KEYS` / `CERTIFICATE_REVIEWER_KEYS`，
      逗号分隔的 `身份名=凭据`）：录入人在 `X-Entry-Key` 头提交凭据，
      复核人在 `X-Reviewer-Key` 头提交凭据。凭据仅保存在本地配置、不入库，
      库内仅保存其 SHA-256（用于“双人不同凭据”校验），响应只回显身份名。
      录入凭据与复核凭据分属两张表、互不通用（用错角色的凭据按无效处理
      `401 invalid_identity_key`）；复核人凭据的指纹不得与该申请录入凭据
      相同，否则 `403 reviewer_must_differ`。
    - **提交申请**：录入人携带申请号（`application_no`）与完整普通证书签发
      资料提交。申请**仅保存待审内容**：不预占证书编号（`cert_duplicate`
      留待批准时判定）、不预占设备有效期（`validity_overlap` 留待批准时
      判定），因此引用尚不存在的父证书、当下区间重叠等都允许先提交。
      申请号为幂等键：同号同内容重试返回原申请（HTTP 200，状态随原申请），
      同号异内容 `409 review_request_conflict`；并发同号由主键兜底。
    - **驳回**：复核人 `decision=REJECT` 必须附非空白原因
      （`422 review_reason_required`）；申请一次性推进为 REJECTED，记录
      驳回结论、复核人、原因与时刻，**不产生证书**。**驳回后不能再批准**
      （任何再定稿均 `409 review_already_decided`，唯一例外是同复核人同
      结论同原因的重放返回首次结果）。
    - **批准 = 同事务真正签发**：`decision=APPROVE` 不得携带原因
      （`422 review_approve_reason_not_allowed`）。批准事务先锁定申请行，
      再在**同一事务内**按**批准当时**的标准证书状态、临时停用记录与现有
      全部签发规则执行与直接签发完全相同的校验和落库，成功后把申请推进为
      APPROVED，并将结论、复核人、定稿时刻与真正签发的证书编号及签发结果
      （证书+追溯链快照）绑定。任一步签发校验失败（`invalid_reference` /
      `reference_not_active` / `reference_suspended` /
      `reference_not_qualified` / `reference_out_of_validity` /
      `validity_overlap` / `cert_duplicate` /
      `validity_overlap_concurrent` 等，错误码与直接签发一致）整事务回滚：
      **申请仍为 PENDING、不产生证书**，可在状态变化后由复核人再次批准。
    - **定稿与重试**：每份申请至多一份定稿（申请行 `FOR UPDATE` 串行化，
      触发器强制只允许 `PENDING → APPROVED/REJECTED` 一次性推进，申请内容
      与定稿记录只增不改）。同一复核人同一操作（同结论、同驳回原因）重试
      返回首次结果（HTTP 200，即使已过任意时长）；不同操作、不同复核人再
      定稿一律 `409 review_already_decided`。
    - **查询**：录入人或复核人凭据均可
      `GET /api/v1/certificate-review-requests/{application_no}` 按申请号
      查询状态与结果：PENDING 仅待审内容；APPROVED 含签发证书与追溯链
      （首次签发结果快照，事后撤销不改写）；REJECTED 含驳回原因与复核人。
      凭据缺失/错误 `401`，申请不存在 `404 review_request_not_found`。
    - **不影响既有通道**：直接签发（`POST /api/v1/certificates`）、批次
      签发与全部证书查询/撤销契约保持不变；批准签发的证书与直接签发的
      证书在证书表中完全同形，可正常查询、撤销与级联失效。
16. **撤销风险告知（已签收归档的可追认告知）**：
    - **生成**：已定稿 `ACCEPT` 的交付归档包若受后续证书撤销波及，撤销事务
      在**同一事务**内为该包生成一份告知。命中判定与影响面追查同一口径：
      包内目标在**归档时刻冻结的追溯链**（归档快照，非当前状态）中包含本批
      被撤销证书集合（被主动撤销的证书 + 全部级联失效证书）中任一证书。
      每份告知列出**撤销批次**（批次号、被主动撤销的证书、撤销时刻）、
      **受影响目标**（含原交付序位）及**链上命中编号**（命中证书在冻结链
      中的序位，从 1 开始）；**同包同批只记一份**（`(归档包, 批次)` 唯一
      约束），**未签收或 REJECT 的归档不生成**。
    - **查询与确认**：收件人凭**定稿邀请编号 + 原一次性密钥**查询本归档的
      告知列表、并逐份确认。邀请的截止时刻不参与判定——**密钥过期不影响
      已签收后的查询与确认**；邀请未知 `404 invitation_not_found`、密钥
      错误 `401 invalid_receipt_key`、归档尚未定稿 `404 receipt_not_found`、
      用同一归档的**非定稿邀请**访问 `409 receipt_conflict`。确认一次性
      写入确认时刻（触发器强制只写一次）：首次确认 `201`，**重复确认返回
      首次确认时间**（`200`，并发确认同样恰一份首次）。
    - **并发次序**：签收与撤销并发仍按提交先后决定——签收先提交则撤销事务
      读到定稿并生成告知；撤销先提交则 `ACCEPT` 按现有规则
      （`422 receipt_targets_revoked`）拒绝、不产生定稿也不生成告知。
    - **只增不改**：告知内容（归档包/批次/命中明细/生成时刻）一经生成不可
      改写、不可删除，仅确认时刻可一次性写入；不改写归档快照、签收记录、
      撤销记录与影响面追查结果。
17. **标准证书每日校准使用量上限（管理员配置，按 UTC 日原子扣额）**：
    - **配置模型**：管理员凭本地管理密钥（与交付/停用共用同一把
      `X-Management-Key` / `DELIVERY_MANAGEMENT_KEY`）对“**被直接引用的
      上游证书编号 × UTC 日期**”配置非负整数上限
      （`PUT /api/v1/certificates/{cert_no}/daily-quotas/{day}`，
      `day=YYYY-MM-DD`）。首次配置返回 `201`，调整既有配置返回 `200`；
      响应给出 `daily_limit`、实时 `used`（已用次数）、`remaining`（剩余）。
      缺/错管理密钥 `401 invalid_management_key`，证书不存在
      `404 cert_not_found`，负数 `422`。**只有存在配置行的“证书×日期”才
      受控；未配置上限完全沿用原签发规则**（无配置行即无限制，签发事务
      仅做一次只读短路判断）。
    - **计数口径**：每张普通证书（`CALIBRATION`）在**真正签发（事务提交）**
      时，按其 `calibrated_at` 换算 **UTC 后的日历日期**、对其
      **直接引用**的上游证书（`parent_cert_no`）计**一次**。基准证书登记
      不计；间接上游（祖父及以上）不重复计；`PASS`/`FAIL` 均计；批内多张
      相同“上游×UTC 日”的证书**合并计数**。已用次数不单独落表，直接按
      `certificates` 中满足 `parent_cert_no` 与校准时刻 UTC 日的现存普通
      证书实时计数，因此**撤销（REVOKED）或级联失效（INVALID）不退还
      已用次数**，已用次数只增不减。
    - **调整下限保护**：配置/上调任意非负值都允许；下调（含改为 0）时若
      当日已签发次数大于新上限，返回 `422 quota_below_usage`（`details`
      给出 `used`/`daily_limit`/`day`/`cert_no`），本次不生效、原配置
      保持不变。上限为 0 表示当日完全禁止以该证书为直接上游的签发。
    - **真正签发时原子扣额**：
      * **单张签发**（`POST /api/v1/certificates`）与**复核批准**
        （批准在批准当时同事务真正签发）在校验上游有效性、停用、有效期后
        执行扣额；已用次数达到上限时返回 `422 daily_quota_exceeded`，
        `details` 点名 `parent_cert_no`、`day`（UTC）、`daily_limit`、
        `used` 与本次未签发的 `cert_no`。单张证书不落库；**复核申请保持
        PENDING**（不产生证书，上调上限或换日后可由复核人再次批准）；
        驳回不签发、不计数。
      * **交接批次**（`POST /api/v1/certificate-batches`）按拓扑序逐张
        校验落库，批内前序插入对后续计数可见：批内多张相同键合并计数，
        任一普通证书将使该“上游×UTC 日”用量超过上限即整体回滚——
        **整批不落库**（无批次快照、批内任何证书均不可见），错误详情同样
        点名上游证书与日期。已存在批次的**幂等重放只返回首次签发结果
        快照，不重新计数**（无论上限事后是否下调）。
    - **并发不超额**：限额调整事务先对目标证书行加 `FOR UPDATE` 再写
      配置；签发事务在签发校验中对其直接上游证书行持锁（单张/复核批准
      `FOR UPDATE`，批次按拓扑序逐张处理），并对限额行 `FOR UPDATE`
      复核与计数——调整与同键签发在同一把行锁上**按实际提交次序串行**：
      调整先提交则随后签发按新上限判定（下调为 0 即全部拒绝），签发先
      提交则其用量计入、调整端按新用量复核下限（低于已用次数的下调被
      拒）。任何交错下“已提交的以该上游为直接引用、校准 UTC 日相同的
      普通证书数”绝不超过当时已提交的上限；理论等待环由既有的死锁检测
      与整体重试（`409 issuance_busy` / `quota_busy`，调用方可重试）兜底。
    - **查询**：凭管理密钥
      `GET /api/v1/certificates/{cert_no}/daily-quotas/{day}` 返回配置与
      实时已用/剩余；该键未配置上限时 `404 quota_not_found`（`details`
      仍给出实时已用次数，便于排障），证书不存在 `404 cert_not_found`。
    - **不影响既有规则**：上限仅作用于“签发是否放行”，不改变证书内容、
      状态、追溯链、撤销/级联失效、停用、交付归档与覆盖审计；撤销或失效
      后上限配置依然存在并继续拦截新签发（此时引用会先按既有
      `reference_not_active` 规则被拒）。
18. **设备校准委托工单（登记/取消/结清/查询，管理密钥）**：
    - **登记**：管理员在 `X-Management-Key` 头中携带管理密钥（与交付/停用/
      限额共用同一把 `DELIVERY_MANAGEMENT_KEY`），以**唯一工单号**登记
      设备标识、委托人（非空白）与**带时区的计划校准时段**（半开区间
      `[scheduled_from, scheduled_to)`，起点必须早于终点）。登记只保存
      委托信息，**不预占**证书编号、设备有效期或每日限额——既有单张签发、
      交接批次与双人复核签发规则完全照常。工单初始为 `PENDING`（待完成）。
      工单号为幂等键：**同号同内容重试返回原工单**（HTTP 200），**同号
      异内容**返回 `409 work_order_conflict`；并发同号由主键兜底，仅一份
      首次登记（201），其余重放（200）。
    - **取消**：`POST /api/v1/work-orders/{no}/cancel` 仅 `PENDING` 工单
      可取消，取消后为 `CANCELLED` 并记录取消时刻；已结清、已取消工单再
      取消（或已取消再结清）返回 `409 work_order_not_pending`。
    - **结清**：`POST /api/v1/work-orders/{no}/settle` 提交工单号与**已
      签发证书编号**。只接受同时满足下列条件的证书，任一不满足按对应错误
      码拒绝且工单保持 `PENDING`：
      * 普通校准证书（`kind=CALIBRATION`）——基准证书 `422
        settlement_cert_not_calibration`；
      * **设备标识与工单登记一致**——`422 settlement_device_mismatch`
        （`details` 同时给出工单设备与证书设备）；
      * **校准时刻 `calibrated_at` 落在计划半开区间内**
        （`scheduled_from <= calibrated_at < scheduled_to`，时区按同一
        时刻比较）——越界（含恰等于终点）`422 settlement_outside_schedule`；
      * 证书**当前**为 `ACTIVE`——已撤销/失效 `422 settlement_cert_not_active`；
      * 校准结论为 **PASS**——`422 settlement_cert_not_passed`；
      * 证书不存在 `404 cert_not_found`、工单不存在 `404 work_order_not_found`。
      **一张证书不得结清两个工单**：数据库在 `cert_no WHERE status='SETTLED'`
      上的部分唯一索引兜底，并发结清同一张证书时仅一个事务成功，其余
      `409 settlement_cert_already_settled`（`details.bound_work_order_no`
      点名已持有的工单）。
    - **绑定固定与重试**：结清成功后状态为 `SETTLED`，证书编号与**结清
      时刻**一次性绑定、不可更改。**同工单同证书重试返回首次结清结果**
      （HTTP 200，结清时刻逐字段等于首次值，即使证书事后已撤销/失效）；
      对已结清工单提交**其他证书编号**返回
      `409 work_order_cert_rebind_conflict`；已结清工单**不可取消**。
    - **撤销/失效不倒退**：绑定仅记录结清当时满足条件；证书日后被
      撤销（REVOKED）或因上游撤销级联失效（INVALID）**不回退工单状态、
      不改写结清时刻与绑定编号**。查询工单返回的 `certificate` 为绑定
      证书的**当前**内容（`certificate.status` 实时为
      ACTIVE/REVOKED/INVALID）。
    - **并发**：结清与取消在工单行 `FOR UPDATE` 上按**提交次序**串行——
      先结清则取消报 `work_order_not_pending`，先取消则结清报
      `work_order_not_pending`，绝无“已取消又结清”或“已结清又取消”；
      结清事务在证书行锁内完成 ACTIVE/PASS/时段复核，与并发撤销按提交
      次序串行（撤销先提交则本次结清按非 ACTIVE 拒绝、工单保持 PENDING，
      无无效结清）；理论等待环由死锁检测终止后整体重试，耗尽
      `409 work_order_busy`（调用方可重试）。
    - **查询**：`GET /api/v1/work-orders/{no}` 凭管理密钥返回登记信息、
      状态、取消/结清时刻，以及（已结清时）绑定证书的当前内容。缺/错
      密钥 `401`，未知工单 `404 work_order_not_found`。
    - **只增不改**：工单登记内容与状态由触发器保护，仅允许
      `PENDING → SETTLED/CANCELLED` 一次性推进，禁止 DELETE 与任何字段
      改写；工单不触碰 `certificates` 表，不影响证书内容、追溯链、撤销、
      停用、交付归档、限额与覆盖审计。

证书状态：`ACTIVE` 生效中 / `REVOKED` 被主动撤销 / `INVALID` 因上游撤销级联失效。

---

## HTTP API

所有时间字段为带时区 ISO 8601，例如 `2026-03-01T09:00:00+08:00`。

### 1. 登记基准设备合格证书（追溯起点）

```bash
curl -sS -X POST http://localhost:8080/api/v1/reference-certificates \
  -H 'Content-Type: application/json' \
  -d '{
    "cert_no": "REF-MASTER-001",
    "device_id": "DEV-MASTER",
    "device_name": "实验室最高标准器",
    "valid_from": "2026-01-01T00:00:00+08:00",
    "valid_to":   "2027-01-01T00:00:00+08:00",
    "calibrated_at": "2026-01-01T09:00:00+08:00",
    "operator": "张工",
    "remark": "年度基准登记"
  }'
```

`calibrated_at` 可省略，缺省取 `valid_from`。响应 `201`，含证书与其追溯链。

### 2. 签发普通校准证书

```bash
curl -sS -X POST http://localhost:8080/api/v1/certificates \
  -H 'Content-Type: application/json' \
  -d '{
    "cert_no": "CAL-WS-001",
    "device_id": "DEV-WS-A",
    "device_name": "车间万用表A",
    "valid_from": "2026-03-01T00:00:00+08:00",
    "valid_to":   "2027-03-01T00:00:00+08:00",
    "calibrated_at": "2026-03-05T10:00:00+08:00",
    "conclusion": "PASS",
    "parent_cert_no": "REF-MASTER-001",
    "operator": "李工"
  }'
```

再向下签发一层（引用普通证书）：

```bash
curl -sS -X POST http://localhost:8080/api/v1/certificates \
  -H 'Content-Type: application/json' \
  -d '{
    "cert_no": "CAL-CUS-001",
    "device_id": "DEV-CUS-B",
    "device_name": "客户仪表B",
    "valid_from": "2026-04-01T00:00:00+08:00",
    "valid_to":   "2026-10-01T00:00:00+08:00",
    "calibrated_at": "2026-04-02T10:00:00+08:00",
    "conclusion": "PASS",
    "parent_cert_no": "CAL-WS-001"
  }'
```

### 2B. 设定/调整标准证书每日校准使用量上限（管理员，X-Management-Key）

管理员凭管理密钥为“被直接引用的上游证书编号 × UTC 日期”配置非负每日上限。
路径中的日期为 **UTC 日历日**（`YYYY-MM-DD`），与普通证书 `calibrated_at`
换算 UTC 后的日期一一对应；未配置上限的键沿用原签发规则（不受限）。

```bash
# 为 REF-MASTER-001 在 UTC 日期 2026-03-04 设定上限 2
curl -sS -X PUT http://localhost:8080/api/v1/certificates/REF-MASTER-001/daily-quotas/2026-03-04 \
  -H 'Content-Type: application/json' \
  -H 'X-Management-Key: caltrace-delivery-admin' \
  -d '{"daily_limit": 2}'
```

首次配置返回 `201`，调整既有配置返回 `200`，响应给出上限与实时已用/剩余：

```json
{
  "cert_no": "REF-MASTER-001",
  "day": "2026-03-04",
  "daily_limit": 2,
  "used": 0,
  "remaining": 2,
  "created_at": "2026-10-05T09:00:00Z",
  "updated_at": "2026-10-05T09:00:00Z"
}
```

注意 `calibrated_at` 按 **UTC** 切日：上例的普通证书若传
`2026-03-05T08:00:00+08:00`（= `2026-03-05T00:00:00Z`）计入 `2026-03-05`，
而 `2026-03-05T07:59:00+08:00`（= `2026-03-04T23:59:00Z`）仍计入
`2026-03-04`。

```bash
# 上调为 3（200）
curl -sS -X PUT http://localhost:8080/api/v1/certificates/REF-MASTER-001/daily-quotas/2026-03-04 \
  -H 'Content-Type: application/json' \
  -H 'X-Management-Key: caltrace-delivery-admin' \
  -d '{"daily_limit": 3}'

# 查询配置与实时已用次数
curl -sS http://localhost:8080/api/v1/certificates/REF-MASTER-001/daily-quotas/2026-03-04 \
  -H 'X-Management-Key: caltrace-delivery-admin'
```

当日已签发 3 张后再下调到 2（撤销/失效不退还已用次数）返回
`422 quota_below_usage`，原配置不变；用满后新签发（单张/批准/批次）返回
`422 daily_quota_exceeded`，批次整批不落库、复核申请保持待审：

```json
{
  "error": {
    "code": "daily_quota_exceeded",
    "message": "上游标准证书 REF-MASTER-001 在 UTC 日期 2026-03-04 的校准签发已达每日上限 3（已用 3），证书 CAL-X 本次不予签发",
    "details": {
      "cert_no": "CAL-X",
      "parent_cert_no": "REF-MASTER-001",
      "day": "2026-03-04",
      "daily_limit": 3,
      "used": 3
    }
  }
}
```

交接批次的扣额示例（批内两张相同“上游×UTC 日”合并计数；把 `2` 改成 `3`
且当日上限仍为 2 时整批 `422`、不落任何证书；同批次键重放返回首次结果、
不重复计数）：

```bash
curl -sS -X POST http://localhost:8080/api/v1/certificate-batches \
  -H 'Content-Type: application/json' \
  -d '{
    "batch_key": "HANDOVER-2026-03-05-01",
    "certificates": [
      {"cert_no":"CAL-B-01","kind":"CALIBRATION","device_id":"DEV-B1",
       "valid_from":"2026-03-01T00:00:00Z","valid_to":"2027-03-01T00:00:00Z",
       "calibrated_at":"2026-03-04T10:00:00Z","conclusion":"PASS",
       "parent_cert_no":"REF-MASTER-001"},
      {"cert_no":"CAL-B-02","kind":"CALIBRATION","device_id":"DEV-B2",
       "valid_from":"2026-03-01T00:00:00Z","valid_to":"2027-03-01T00:00:00Z",
       "calibrated_at":"2026-03-04T11:00:00Z","conclusion":"PASS",
       "parent_cert_no":"REF-MASTER-001"}
    ]
  }'
```

### 3. 提交普通证书签发复核申请（录入人，X-Entry-Key）

需双人复核的普通校准证书，由录入人先提交**申请号 + 完整签发资料**。申请仅
保存待审内容：此时**不占用证书编号、不占用设备有效期**（编号占用、区间重叠、
父证书状态等全部留到批准当时判定），引用尚不存在的父证书也允许先提交。
录入凭据通过 `X-Entry-Key` 请求头提交（身份名仅回显在 `submitter` 中）：

```bash
curl -sS -X POST http://localhost:8080/api/v1/certificate-review-requests \
  -H 'Content-Type: application/json' \
  -H 'X-Entry-Key: caltrace-entry-key' \
  -d '{
    "application_no": "RVW-2026-10-05-001",
    "cert_no": "CAL-RV-001",
    "device_id": "DEV-RV-C",
    "device_name": "待复核万用表",
    "valid_from": "2026-03-01T00:00:00+08:00",
    "valid_to":   "2027-03-01T00:00:00+08:00",
    "calibrated_at": "2026-03-05T10:00:00+08:00",
    "conclusion": "PASS",
    "parent_cert_no": "REF-MASTER-001",
    "operator": "李工"
  }'
```

首次提交响应 `201`（此时 `review` / `certificate` / `chain` 均为 `null`）：

```json
{
  "application_no": "RVW-2026-10-05-001",
  "status": "PENDING",
  "request": {"cert_no": "CAL-RV-001", "conclusion": "PASS", "parent_cert_no": "REF-MASTER-001", "...": "完整签发资料"},
  "submitter": "entry-1",
  "submitted_at": "2026-10-05T01:10:00.123456+00:00",
  "review": null,
  "certificate": null,
  "chain": null
}
```

约定与拒绝场景：

- **申请号幂等**：同号同内容重试返回原申请（`200`，状态随原申请）；
  同号异内容 `409 review_request_conflict`；
- 录入凭据缺失/错误（含误用复核凭据）→ `401 invalid_identity_key`；
- 签发资料本身的结构校验（时间窗口、字段长度等）与直接签发一致，
  结构不合法 `422`；但父证书存在性、生效/合格/有效期、停用、区间重叠、
  编号占用等**业务校验不在提交时执行**。

### 4. 复核人驳回 / 批准（X-Reviewer-Key，批准同事务真正签发）

复核人凭**独立凭据**在 `X-Reviewer-Key` 头中提交结论。**驳回**须附非空原因：

```bash
curl -sS -X POST http://localhost:8080/api/v1/certificate-review-requests/RVW-2026-10-05-001/decision \
  -H 'Content-Type: application/json' \
  -H 'X-Reviewer-Key: caltrace-review-key-1' \
  -d '{"decision": "REJECT", "reason": "校准记录缺少环境温度项，退回补正"}'
```

**批准**（不得携带原因）——服务在同一事务内按**批准当时**的标准证书状态、
临时停用记录与现有全部签发规则真正签发，并把结论、复核人与证书绑定：

```bash
curl -sS -X POST http://localhost:8080/api/v1/certificate-review-requests/RVW-2026-10-05-001/decision \
  -H 'Content-Type: application/json' \
  -H 'X-Reviewer-Key: caltrace-review-key-1' \
  -d '{"decision": "APPROVE"}' | python3 -m json.tool
```

批准成功响应 `201`，与申请视图同构——`status=APPROVED`、`review` 记录
批准结论/复核人/时刻，`certificate` 与 `chain` 即真正签发结果（与第 2 节
直接签发响应同构）。随后 `GET /api/v1/certificates/CAL-RV-001` 与追溯链
查询均可正常访问，与直接签发的证书没有任何区别。

关键约定：

- **双人不同凭据**：复核凭据的指纹不得与该申请录入凭据相同，否则
  `403 reviewer_must_differ`；复核凭据缺失/错误 `401 invalid_identity_key`；
  申请不存在 `404 review_request_not_found`；
- **驳回**：`REJECT` 原因空白 `422 review_reason_required`；`APPROVE`
  携带原因 `422 review_approve_reason_not_allowed`；
- **批准校验失败返回具体原因，申请仍待审、不产生证书**：错误码与直接签发
  完全一致，例如父证书在等待期间被撤销 → `422 reference_not_active`、
  被临时停用 → `422 reference_suspended`、校准时刻超出有效期 →
  `422 reference_out_of_validity`、编号已被直接签发占用 → `409 cert_duplicate`、
  同设备区间重叠 → `422 validity_overlap`（并发兜底 `409 validity_overlap_concurrent`）。
  状态变化后（如停用解除）复核人可再次批准，无需重新提交申请；
- **驳回后不能再批准**：每份申请只允许一份定稿。同一复核人同一操作
  （同结论、同驳回原因）重试原样返回首次结果（`200`）；驳回后再批准、
  批准后改判、其他复核人再定稿一律 `409 review_already_decided`
  （`details` 给出首次定稿的复核人与结论）；
- **并发复核只允许一份定稿**：申请行 `FOR UPDATE` 串行化，多个并发
  APPROVE/REJECT 中恰一份成功，其余按“同操作重放/异操作冲突”处理；
  批准事务与停用/撤销在引用链上的等待环由死锁检测终止并重试，耗尽返回
  `409 review_busy`（申请仍为待审，可重试）。

### 5. 按申请号查询复核状态与结果

录入人或复核人凭据均可查询（任一角色的有效凭据）：

```bash
curl -sS http://localhost:8080/api/v1/certificate-review-requests/RVW-2026-10-05-001 \
  -H 'X-Entry-Key: caltrace-entry-key' | python3 -m json.tool
```

- `PENDING`：完整待审签发资料 + 录入人/提交时刻，`review`、`certificate`、
  `chain` 为 `null`；
- `APPROVED`：追加 `review`（批准/复核人/时刻）与 `certificate`、`chain`
  ——后者是批准当时的**首次签发结果快照**，证书事后被撤销也不改写；
- `REJECTED`：`review` 中给出驳回结论、复核人、非空原因与定稿时刻，
  `certificate` / `chain` 为 `null`（不产生证书）。

凭据缺失/错误 `401 invalid_identity_key`；申请不存在
`404 review_request_not_found`。

### 6. 查询追溯引用链

```bash
curl -sS http://localhost:8080/api/v1/certificates/CAL-CUS-001/chain | python3 -m json.tool
```

返回从基准到目标证书的有序节点链（`nodes[0]` 即追溯起点）。

### 7. 查询某证书的全部下游（撤销前影响面预览）

```bash
curl -sS http://localhost:8080/api/v1/certificates/REF-MASTER-001/downstream
```

### 8. 撤销证书（级联失效下游，返回受影响证书与引用链）

```bash
curl -sS -X POST http://localhost:8080/api/v1/certificates/REF-MASTER-001/revoke \
  -H 'Content-Type: application/json' \
  -d '{"reason": "年度比对发现超差", "operator": "质量负责人"}'
```

响应示例（节选）：

```json
{
  "batch_id": 1,
  "revoked": {"cert_no": "REF-MASTER-001", "status": "REVOKED", ...},
  "affected_count": 2,
  "affected": [
    {"cert_no": "CAL-WS-001",  "depth": 1, "chain": [{"cert_no": "REF-MASTER-001", ...}, {"cert_no": "CAL-WS-001", ...}]},
    {"cert_no": "CAL-CUS-001", "depth": 2, "chain": [{"cert_no": "REF-MASTER-001", ...}, {"cert_no": "CAL-WS-001", ...}, {"cert_no": "CAL-CUS-001", ...}]}
  ]
}
```

### 9. 查询证书 / 列表 / 设备历史 / 撤销批次

```bash
# 单张证书（无论 ACTIVE / REVOKED / INVALID，历史内容始终可查）
curl -sS http://localhost:8080/api/v1/certificates/CAL-CUS-001

# 列表，支持 device_id / status / kind 过滤与分页
curl -sS 'http://localhost:8080/api/v1/certificates?status=INVALID&limit=50&offset=0'

# 某设备的全部证书（含已撤销/已失效的历史记录）
curl -sS http://localhost:8080/api/v1/devices/DEV-CUS-B/certificates

# 撤销批次列表 / 详情
curl -sS http://localhost:8080/api/v1/revocations
curl -sS http://localhost:8080/api/v1/revocations/1
```

### 10. 批量签发交接批次（整批原子、批次键幂等）

离线采集的一组校准记录作为一个交接批次一次签发。每项通过 `kind` 区分基准
（`REFERENCE`）或普通（`CALIBRATION`）证书，字段与单张签发一致；普通证书的
`parent_cert_no` 可引用库内证书或本批证书，**列表顺序不代表引用顺序**：

```bash
curl -sS -X POST http://localhost:8080/api/v1/certificate-batches \
  -H 'Content-Type: application/json' \
  -d '{
    "batch_key": "HANDOFF-2026-10-03-001",
    "certificates": [
      {"kind": "CALIBRATION", "cert_no": "CAL-B-002", "device_id": "DEV-B-2",
       "valid_from": "2026-05-01T00:00:00+08:00", "valid_to": "2027-05-01T00:00:00+08:00",
       "calibrated_at": "2026-05-02T09:00:00+08:00", "conclusion": "PASS",
       "parent_cert_no": "CAL-B-001"},
      {"kind": "REFERENCE", "cert_no": "REF-B-000", "device_id": "DEV-B-0",
       "valid_from": "2026-01-01T00:00:00+08:00", "valid_to": "2027-01-01T00:00:00+08:00",
       "calibrated_at": "2026-01-01T09:00:00+08:00"},
      {"kind": "CALIBRATION", "cert_no": "CAL-B-001", "device_id": "DEV-B-1",
       "valid_from": "2026-03-01T00:00:00+08:00", "valid_to": "2027-03-01T00:00:00+08:00",
       "calibrated_at": "2026-03-02T09:00:00+08:00", "conclusion": "PASS",
       "parent_cert_no": "REF-B-000"}
    ]
  }'
```

首次成功响应 `201`（`items` 按请求列表顺序返回，各含从基准到自身的追溯链）：

```json
{
  "batch_key": "HANDOFF-2026-10-03-001",
  "count": 3,
  "issued_at": "2026-10-03T10:20:30.123456+00:00",
  "items": [
    {"certificate": {"cert_no": "CAL-B-002", ...},
     "chain": {"cert_no": "CAL-B-002", "root_cert_no": "REF-B-000", "length": 3, "nodes": [...]}},
    {"certificate": {"cert_no": "REF-B-000", ...}, "chain": {...}},
    {"certificate": {"cert_no": "CAL-B-001", ...}, "chain": {...}}
  ]
}
```

幂等与冲突约定：

- **同键同请求重试** → `200`，返回首次签发结果快照（即使证书事后被撤销）；
- **同键不同请求** → `409 batch_conflict`；
- **任一记录不合规** → 整批不落库，错误响应 `details.cert_no` 定位出错编号，
  错误码与单张签发一致（如 `reference_not_active`、`validity_overlap`），
  批内特有问题使用 `batch_cert_duplicate`（批内编号重复）与
  `reference_cycle`（批内引用成环）。

### 11. 查询签发批次（首次签发结果快照）

```bash
curl -sS http://localhost:8080/api/v1/certificate-batches/HANDOFF-2026-10-03-001 | python3 -m json.tool
```

返回与签发响应同构的快照，不受事后撤销影响；批次不存在返回 `404 batch_not_found`。

### 12. 登记更正声明（纠正展示字段录入错误）

证书签发后业务字段不可改写；若 `device_name` / `operator` / `remark` 录入有误，
通过更正声明纠错（仅这三个展示字段可更正）：

```bash
curl -sS -X POST http://localhost:8080/api/v1/correction-declarations \
  -H 'Content-Type: application/json' \
  -d '{
    "declaration_no": "CORR-2026-0001",
    "cert_no": "CAL-WS-001",
    "corrections": {
      "device_name": "车间万用表A（FLUKE-17B）",
      "operator": null
    },
    "reason": "设备名称少录型号，操作员误填需置空"
  }'
```

- `corrections` 为**非空**修正字段集合：只接受 `device_name` / `operator` / `remark`；
  字段值为 `null` 表示**显式置空**，**未提交**的字段保持原值，两者严格区分；
- 首次登记响应 `201`；同编号同内容重试响应 `200`，返回首次声明（不重复登记）；
  同编号不同内容响应 `409 correction_conflict`；
- 已撤销 / 已失效证书同样可更正，其状态、有效期与签发、撤销判定不受影响。

响应（`200`/`201`）包含声明顺序、原证书、声明历史与合并后的当前展示：

```json
{
  "declaration": {
    "declaration_no": "CORR-2026-0001",
    "cert_no": "CAL-WS-001",
    "seq": 1,
    "corrections": {"device_name": "车间万用表A（FLUKE-17B）", "operator": null},
    "reason": "设备名称少录型号，操作员误填需置空",
    "created_at": "2026-10-03T10:25:30.123456+00:00"
  },
  "certificate": {"cert_no": "CAL-WS-001", "device_name": "车间万用表A", "operator": "李工", ...},
  "declarations": [
    {"declaration_no": "CORR-2026-0001", "seq": 1, ...}
  ],
  "display": {"device_name": "车间万用表A（FLUKE-17B）", "operator": null, "remark": null}
}
```

其中 `certificate` 始终是**签发时的原始内容**（不受更正影响）；`declarations` 为该证书
全部声明按登记顺序（`seq`）升序排列；`display` 为按序合并后的当前展示——同一字段
后登记的声明覆盖先登记的。

### 13. 查询证书的更正视图（声明历史 + 合并展示）

```bash
curl -sS http://localhost:8080/api/v1/certificates/CAL-WS-001/corrections | python3 -m json.tool
```

返回 `cert_no`、`certificate`（原证书）、`declarations`（声明历史，按 `seq` 升序）与
`display`（合并后的当前展示）；证书不存在返回 `404 cert_not_found`。

### 14. 按声明编号查询

```bash
curl -sS http://localhost:8080/api/v1/correction-declarations/CORR-2026-0001 | python3 -m json.tool
```

返回首次登记的声明及所属证书的当前更正视图（与登记响应同构）；
声明不存在返回 `404 declaration_not_found`。

> 注意：`GET /api/v1/certificates/{cert_no}`、追溯链与批次快照始终返回签发时的
> 原始字段值；合并后的展示值只通过上述更正相关接口与交付归档包快照获取。

### 15. 创建客户交付归档包（冻结可交付追溯凭据）

向客户交付一组校准证书时，以归档包编号和**非空、有序、编号互不重复**的证书
编号列表冻结当时可交付的凭据。服务校验每张目标证书均为 `CALIBRATION`，且
目标及其**完整上游链**全部 `ACTIVE`、结论 `PASS`；任一不达标则整包不保存，
错误体一次性分类点名全部相关编号：

```bash
curl -sS -X POST http://localhost:8080/api/v1/delivery-archives \
  -H 'Content-Type: application/json' \
  -d '{
    "archive_no": "DELIVERY-2026-10-03-001",
    "cert_nos": ["CAL-WS-001", "CAL-CUS-001"]
  }'
```

首次成功响应 `201`，`items` **按提交顺序**返回每张目标证书的：
`certificate`（原始签发内容）、`chain`（从基准到目标的完整引用链）、
`display`（按归档时刻已登记更正声明合并后的展示字段）：

```json
{
  "archive_no": "DELIVERY-2026-10-03-001",
  "count": 2,
  "created_at": "2026-10-03T10:40:30.123456+00:00",
  "items": [
    {
      "cert_no": "CAL-WS-001",
      "certificate": {"cert_no": "CAL-WS-001", "device_name": "车间万用表A", "status": "ACTIVE", ...},
      "chain": {"cert_no": "CAL-WS-001", "root_cert_no": "REF-MASTER-001", "length": 2, "nodes": [...]},
      "display": {"device_name": "车间万用表A（FLUKE-17B）", "operator": null, "remark": null}
    },
    {
      "cert_no": "CAL-CUS-001",
      "certificate": {...},
      "chain": {"length": 3, ...},
      "display": {"device_name": "客户仪表B", "operator": "王工", "remark": null}
    }
  ]
}
```

校验不通过示例（包中混入不存在/失效/不合格证书时，整包拒绝且不落库）：

```json
{
  "error": {
    "code": "archive_not_deliverable",
    "message": "存在不存在或不满足交付条件的目标证书……整包未保存。相关编号: CAL-X, CAL-Y",
    "details": {
      "archive_no": "DELIVERY-2026-10-03-001",
      "cert_nos": ["CAL-X", "CAL-Y"],
      "missing": ["CAL-X"],
      "not_calibration": [],
      "target_not_active": ["CAL-Y"],
      "target_not_qualified": [],
      "chain_not_eligible": ["CAL-Y"]
    }
  }
}
```

幂等、冲突与并发约定：

- **归档包编号 + 有序列表均相同的重试** → `200`，返回**首次创建时刻的快照**；
  即使此后证书被更正或撤销，快照内容（含链节点状态与当时的合并展示）不变；
- **同编号不同列表**（含仅元素顺序不同、子集/超集）→ `409 archive_conflict`；
- 列表内编号重复 → `422 archive_cert_duplicate`；
- 与并发撤销/更正相遇时，快照对应同一个一致的业务时点，不会混合新旧状态：
  撤销/更正先提交则本包按新状态判定（成功则整体新状态快照，否则整包拒绝），
  本包先提交则整体冻结旧状态；并发同编号重试由主键保证仅一个快照。

### 16. 查询交付归档包（创建时刻快照）

```bash
curl -sS http://localhost:8080/api/v1/delivery-archives/DELIVERY-2026-10-03-001 | python3 -m json.tool
```

返回与创建响应同构的快照，不受事后更正/撤销影响；归档包编号未知返回
`404 archive_not_found`。

### 17. 按撤销批次追查已交付凭据影响面（分页）

证书撤销后，客服凭撤销响应中的 `batch_id` 追查该批撤销波及了哪些已交付的
归档凭据。以第 5 节撤销 `REF-MASTER-001` 得到的 `batch_id=1` 为例：

```bash
curl -sS 'http://localhost:8080/api/v1/revocations/1/archive-impact?limit=50&offset=0' | python3 -m json.tool
```

响应示例（假设第 12 节的归档包 `DELIVERY-2026-10-03-001` 已交付）：

```json
{
  "batch_id": 1,
  "revoked_cert_no": "REF-MASTER-001",
  "revoked_at": "2026-10-03T11:02:03.123456+00:00",
  "total": 2,
  "limit": 50,
  "offset": 0,
  "items": [
    {
      "archive_no": "DELIVERY-2026-10-03-001",
      "cert_no": "CAL-WS-001",
      "delivery_position": 1,
      "revoked_chain_position": 1,
      "archived_at": "2026-10-03T10:40:30.123456+00:00"
    },
    {
      "archive_no": "DELIVERY-2026-10-03-001",
      "cert_no": "CAL-CUS-001",
      "delivery_position": 2,
      "revoked_chain_position": 1,
      "archived_at": "2026-10-03T10:40:30.123456+00:00"
    }
  ]
}
```

每行 = 一个归档包内的一张受影响目标证书：

| 字段 | 含义 |
| --- | --- |
| `archive_no` | 归档包编号 |
| `cert_no` | 包内目标证书编号 |
| `delivery_position` | 原交付序位（提交列表中的位置，从 1 开始） |
| `revoked_chain_position` | 本批被撤销证书在该目标**冻结追溯链**中的序位（链首基准为 1） |
| `archived_at` | 归档时刻（归档包创建时间） |

入列与分页约定：

- 目标的**冻结链**（归档快照中的 `chain`，非当前状态）包含本批被撤销证书
  集合（被主动撤销的证书 + 全部级联失效证书）中任一证书即入列；
- 同一目标在同一包内只列一次；同一目标出现在多个归档包时分别列出；
  同包未受影响项不出现；
- 覆盖此前已存在的全部归档包；与归档创建并发时按实际提交次序判定，
  被拒创建的包不入清单；
- 按 `(archive_no, delivery_position)` 稳定排序，`limit`（1–200，默认 50）
  与 `offset`（默认 0）分页；`total` 为全部命中数（空页/末页之后也如实返回）；
- 批次不存在返回 `404 revocation_not_found`；无受影响归档返回
  `200` 空页（`total=0`、`items=[]`）。

### 18. 审计设备校准覆盖（过去半开区间的合规/空窗片段）

面向监管与内审：按设备与带时区的起止时刻，审计过去半开区间 `[from, to)`
内的校准覆盖。按时间顺序返回完整、无重叠的合规（`COMPLIANT`）与空窗
（`GAP`）片段；合规片段列出当时可用的合格普通证书编号（升序），相邻且
状态与编号集合相同的片段已合并：

```bash
curl -sS 'http://localhost:8080/api/v1/devices/DEV-WS-A/coverage-audit?from=2026-03-01T00:00:00%2B08:00&to=2026-10-03T00:00:00%2B08:00' \
  | python3 -m json.tool
```

（`from`/`to` 为查询参数，需 URL 编码时区偏移中的 `+`；上例即
`from=2026-03-01T00:00:00+08:00`、`to=2026-10-03T00:00:00+08:00`。）

响应示例（该设备证书 `CAL-WS-001` 于 2026-03-05 签发创建、2026-09-10 被撤销，
撤销后于 2026-09-20 重签 `CAL-WS-002`）：

```json
{
  "device_id": "DEV-WS-A",
  "from": "2026-02-28T16:00:00+00:00",
  "to": "2026-10-02T16:00:00+00:00",
  "segments": [
    {"from": "2026-02-28T16:00:00+00:00", "to": "2026-03-05T02:00:00+00:00",
     "status": "GAP", "cert_nos": []},
    {"from": "2026-03-05T02:00:00+00:00", "to": "2026-09-10T03:30:00+00:00",
     "status": "COMPLIANT", "cert_nos": ["CAL-WS-001"]},
    {"from": "2026-09-10T03:30:00+00:00", "to": "2026-09-20T06:00:00+00:00",
     "status": "GAP", "cert_nos": []},
    {"from": "2026-09-20T06:00:00+00:00", "to": "2026-10-02T16:00:00+00:00",
     "status": "COMPLIANT", "cert_nos": ["CAL-WS-002"]}
  ]
}
```

判定与参数约定：

- 每张合格普通证书的计入窗口为
  `[max(签发创建时刻, 有效期起点), min(有效期终点, 撤销/级联失效时刻))`，
  再裁剪到审计区间；上游事后到期不取消下游覆盖，上游事后撤销按下游的
  级联失效时刻截断；历史重签按实际签发与失效事件还原（详见业务规则 12）；
- 片段首尾相接、完整覆盖 `[from, to)`；空窗片段的 `cert_nos` 为空列表；
- 未知设备（或无合格普通证书的设备）返回 `200` 与整段空窗，不报 404；
- 起止必须携带时区、起点必须早于终点、终点不得晚于查询时刻，否则
  `422 invalid_audit_window`；响应中的时间统一为 UTC（与输入为同一时刻）。

### 19. 创建交付签收邀请（交付方，管理密钥）

交付方按已创建的归档包编号、收件人和未来截止时间创建签收邀请。管理密钥通过
`X-Management-Key` 请求头提交（对应服务端环境变量 `DELIVERY_MANAGEMENT_KEY`）：

```bash
curl -sS -X POST http://localhost:8080/api/v1/delivery-invitations \
  -H 'Content-Type: application/json' \
  -H 'X-Management-Key: caltrace-delivery-admin' \
  -d '{
    "archive_no": "DELIVERY-2026-10-03-001",
    "recipient": "某某检测有限公司-质检部",
    "deadline": "2026-10-20T18:00:00+08:00"
  }' | python3 -m json.tool
```

响应 `201`。**`key` 为一次性随机密钥，仅此一次明文返回，遗失无法找回/重置**，
需通过带外安全渠道（如加密邮件）转交收件人；库内只保存其 SHA-256：

```json
{
  "invitation_no": "DINV-9f2c1a7b3e6d4850",
  "archive_no": "DELIVERY-2026-10-03-001",
  "recipient": "某某检测有限公司-质检部",
  "deadline": "2026-10-20T10:00:00+00:00",
  "created_at": "2026-10-04T01:10:00.123456+00:00",
  "key": "Yk8-3pQ7sV2xR9mN4bL6tW0cF5hJ1dG8uZaX7eKoMnA"
}
```

拒绝场景：

- 管理密钥头缺失或错误 → `401 invalid_management_key`；
- 归档包不存在 → `404 archive_not_found`；
- `deadline` 不晚于当前时刻 → `422 invitation_deadline_past`；
- 收件人为空白 → `422 invitation_recipient_required`。

同一归档包可创建多份邀请（例如首次邀请过期后重新邀约）；邀请只增不改。

### 20. 收件人提交签收结论（ACCEPT / REJECT）

收件人在截止前用邀请编号与一次性密钥提交结论，**不需要**管理密钥。
接收（ACCEPT）：

```bash
curl -sS -X POST http://localhost:8080/api/v1/delivery-receipts \
  -H 'Content-Type: application/json' \
  -d '{
    "invitation_no": "DINV-9f2c1a7b3e6d4850",
    "key": "Yk8-3pQ7sV2xR9mN4bL6tW0cF5hJ1dG8uZaX7eKoMnA",
    "decision": "ACCEPT"
  }' | python3 -m json.tool
```

拒收（REJECT）必须填写原因：

```bash
curl -sS -X POST http://localhost:8080/api/v1/delivery-receipts \
  -H 'Content-Type: application/json' \
  -d '{
    "invitation_no": "DINV-9f2c1a7b3e6d4850",
    "key": "Yk8-3pQ7sV2xR9mN4bL6tW0cF5hJ1dG8uZaX7eKoMnA",
    "decision": "REJECT",
    "reason": "开箱发现封条破损，且证书 CAL-CUS-001 已被通知暂停使用"
  }'
```

首次定稿响应 `201`，字段即归档号、收件人、结论、原因与签收时刻：

```json
{
  "archive_no": "DELIVERY-2026-10-03-001",
  "invitation_no": "DINV-9f2c1a7b3e6d4850",
  "recipient": "某某检测有限公司-质检部",
  "decision": "ACCEPT",
  "reason": null,
  "signed_at": "2026-10-04T01:22:33.123456+00:00"
}
```

定稿与重试约定（**每包一份定稿**）：

- **同一邀请 + 同一内容重试** → `200`，原样返回首次定稿（`signed_at` 不变），
  **即使重试发生在截止时刻之后**；
- 同一邀请提交**不同内容**（如先 ACCEPT 再 REJECT）、或用**其他邀请**再提交
  → `409 receipt_conflict`，错误体 `details.finalized_invitation_no` /
  `finalized_decision` 给出首次定稿信息；
- 邀请未知 `404 invitation_not_found`、一次性密钥错误 `401 invalid_receipt_key`、
  REJECT 缺原因 `422 receipt_reason_required`；
- **尚未定稿**且已过截止（整刻即过期）→ `409 invitation_expired`，
  此时即使密钥正确也不能签收（ACCEPT/REJECT 均拒绝）。

ACCEPT 的撤销/失效复核——若包内目标或其冻结追溯链中已有证书被撤销/失效：

```json
{
  "error": {
    "code": "receipt_targets_revoked",
    "message": "接收(ACCEPT)被拒绝：包内目标证书或其冻结追溯链中已有证书被撤销或失效；可改用 REJECT 提交（须填写原因）。受影响目标: CAL-CUS-001",
    "details": {
      "archive_no": "DELIVERY-2026-10-03-001",
      "invitation_no": "DINV-9f2c1a7b3e6d4850",
      "affected_targets": ["CAL-CUS-001"]
    }
  }
}
```

该状态为 `422`，`affected_targets` 为受影响的**包内目标**编号（排序去重；
多目标包只点名受影响者）。此时 `REJECT`（带原因）仍可正常提交。签收与撤销
并发时按实际提交顺序判定，定稿后的撤销不改写签收记录与归档快照。

### 21. 凭管理密钥查询定稿记录

```bash
curl -sS http://localhost:8080/api/v1/delivery-archives/DELIVERY-2026-10-03-001/receipt \
  -H 'X-Management-Key: caltrace-delivery-admin' | python3 -m json.tool
```

返回与定稿响应同构的记录（归档号、邀请编号、收件人、结论、原因、签收时刻）。
管理密钥错误 `401 invalid_management_key`；归档包不存在 `404 archive_not_found`；
归档包存在但尚未定稿 `404 receipt_not_found`。注意：归档**快照**仍通过无需
密钥的 `GET /api/v1/delivery-archives/{archive_no}` 获取（既有语义不变）；
定稿记录仅交付方凭管理密钥查询。

### 22. 登记标准证书临时停用（待复核，管理密钥）

计量人员发现生效中的标准证书数据存疑、有待复核时，凭管理密钥登记临时停用。
管理密钥通过 `X-Management-Key` 请求头提交（与签收功能共用环境变量
`DELIVERY_MANAGEMENT_KEY`）；停用从**登记成功（事务提交）时刻**起生效，
需提供非空白原因：

```bash
curl -sS -X POST http://localhost:8080/api/v1/certificates/REF-MASTER-001/suspensions \
  -H 'Content-Type: application/json' \
  -H 'X-Management-Key: caltrace-delivery-admin' \
  -d '{"cert_no": "REF-MASTER-001", "reason": "年度比对发现数据异常，暂停作为溯源依据待复核"}' \
  | python3 -m json.tool
```

响应 `201`，含本次停用事件、证书当前内容、当前停用状态与历次事件历史：

```json
{
  "cert_no": "REF-MASTER-001",
  "certificate": {"cert_no": "REF-MASTER-001", "status": "ACTIVE", ...},
  "currently_suspended": true,
  "active": {
    "suspension_id": 1,
    "action": "SUSPEND",
    "reason": "年度比对发现数据异常，暂停作为溯源依据待复核",
    "at": "2026-10-04T03:10:00.123456+00:00",
    "lifted_at": null,
    "lift_reason": null
  },
  "history": [ {"suspension_id": 1, "action": "SUSPEND", "reason": "...", "at": "...", ...} ],
  "suspension": {"suspension_id": 1, "action": "SUSPEND", "reason": "...", "at": "...", ...}
}
```

拒绝场景：

- 管理密钥头缺失或错误 → `401 invalid_management_key`；
- 证书不存在 → `404 cert_not_found`；
- 原因缺失或纯空白 → `422 suspension_reason_required`；
- 证书不是生效中（已撤销/失效）→ `409 cert_not_active`；
- 已存在生效中的停用记录（重复停用）→ `409 suspension_conflict`。

### 23. 复核后解除临时停用（管理密钥）

复核结束、证书恢复可用时，凭管理密钥与非空原因解除停用。解除成功后该证书
及其下游链可立即重新作为新签发证书的追溯依据：

```bash
curl -sS -X POST http://localhost:8080/api/v1/certificates/REF-MASTER-001/suspension-lift \
  -H 'Content-Type: application/json' \
  -H 'X-Management-Key: caltrace-delivery-admin' \
  -d '{"cert_no": "REF-MASTER-001", "reason": "复核完成，比对结果合格，恢复使用"}' \
  | python3 -m json.tool
```

响应 `200`，与停用查询同构（`currently_suspended=false`、`active=null`，
`history` 中追加 `action=LIFT` 的解除事件，`lift` 字段为本次解除事件）。
无生效中的停用记录（重复解除）→ `409 suspension_not_active`；其余前置
校验（`401` / `404` / `422 suspension_reason_required`）与登记停用一致。

**停用期间签发拦截（单张与批次一致）**：停用生效后，新签发普通证书直接引用
被停用证书、或引用其任何下游证书（停用源沿引用链上溯命中），均返回
`422 reference_suspended`，错误详情同时指出受阻证书与停用源：

```json
{
  "error": {
    "code": "reference_suspended",
    "message": "证书 CAL-WS-001 处于临时停用中（停用源：REF-MASTER-001）或其追溯链上游存在停用标准证书，停用期间不得作为新签发证书的追溯依据",
    "details": {
      "new_cert_no": "CAL-CUS-009",
      "cert_no": "CAL-WS-001",
      "blocked_cert_no": "CAL-WS-001",
      "suspension_source": "REF-MASTER-001",
      "suspension_id": 1
    }
  }
}
```

交接批次签发同样逐张校验：引用库内受阻链（`details.cert_no` 定位批内出错
编号）或**批内证书**间接引用库内停用源（乱序提交也按引用拓扑校验）均整批
拒绝、**整批不落库**（无批次快照、批内证书均不存在）。停用解除后被拒的
单张/批次请求即可正常签发；同键同内容批次重放仍返回**首次签发快照**。

### 24. 查询停用状态与历次停用/解除（管理密钥）

```bash
curl -sS http://localhost:8080/api/v1/certificates/REF-MASTER-001/suspensions \
  -H 'X-Management-Key: caltrace-delivery-admin' | python3 -m json.tool
```

返回 `cert_no`、`certificate`（证书当前真实内容）、`currently_suspended`
（是否有生效停用）、`active`（当前生效停用事件，无则 `null`）与 `history`
（历次停用/解除，按登记顺序升序；每条停用事件含 `SUSPEND`，解除后追加
同编号 `LIFT` 事件，各自给出时间与原因）。证书不存在 →
`404 cert_not_found`，密钥缺失/错误 → `401 invalid_management_key`。

停用**不改写**任何既有事实：证书详情/追溯链仍是原始签发内容，
`ACTIVE/REVOKED/INVALID` 状态不变（撤销与停用互不改写，可并存），
撤销批次与级联失效记录不变，已冻结交付归档快照与批次首次签发快照不变。

### 25. 收件人查询撤销风险告知（定稿邀请编号 + 一次性密钥）

已定稿 `ACCEPT` 的归档包若受后续撤销波及，撤销事务会在撤销提交时为该包
生成一份**撤销风险告知**（同包同批只记一份；未签收或 `REJECT` 不生成）。
收件人凭**定稿邀请编号 + 创建邀请时一次性展示的随机密钥**查询本归档的
告知列表（不需要管理密钥；密钥过期不影响已签收后的查询）。以第 20 节
已 `ACCEPT` 的归档包、第 8 节撤销 `REF-MASTER-001`（`batch_id=1`）为例：

```bash
curl -sS -X POST http://localhost:8080/api/v1/delivery-revocation-notices/query \
  -H 'Content-Type: application/json' \
  -d '{
    "invitation_no": "DINV-9f2c1a7b3e6d4850",
    "key": "Yk8-3pQ7sV2xR9mN4bL6tW0cF5hJ1dG8uZaX7eKoMnA"
  }' | python3 -m json.tool
```

响应 `200` 示例（`items` 按告知编号升序；每份告知列出撤销批次、受影响
目标及链上命中编号）：

```json
{
  "archive_no": "DELIVERY-2026-10-03-001",
  "invitation_no": "DINV-9f2c1a7b3e6d4850",
  "recipient": "某某检测有限公司-质检部",
  "decision": "ACCEPT",
  "total": 1,
  "items": [
    {
      "notice_id": 1,
      "archive_no": "DELIVERY-2026-10-03-001",
      "batch_id": 1,
      "revoked_cert_no": "REF-MASTER-001",
      "revoked_at": "2026-10-04T03:00:00+00:00",
      "items": [
        {"cert_no": "CAL-WS-001", "delivery_position": 1,
         "hits": [{"cert_no": "REF-MASTER-001", "chain_position": 1},
                  {"cert_no": "CAL-WS-001", "chain_position": 2}]},
        {"cert_no": "CAL-CUS-001", "delivery_position": 2,
         "hits": [{"cert_no": "REF-MASTER-001", "chain_position": 1},
                  {"cert_no": "CAL-WS-001", "chain_position": 2},
                  {"cert_no": "CAL-CUS-001", "chain_position": 3}]}
      ],
      "created_at": "2026-10-04T03:00:00+00:00",
      "confirmed_at": null
    }
  ]
}
```

告知内容字段：

| 字段 | 含义 |
| --- | --- |
| `notice_id` | 告知编号（确认时使用） |
| `batch_id` / `revoked_cert_no` / `revoked_at` | 撤销批次号、被主动撤销的证书、撤销时刻 |
| `items[].cert_no` / `delivery_position` | 受影响的包内目标证书及其原交付序位（从 1 开始） |
| `items[].hits[]` | 冻结追溯链上命中的本批证书编号及链上序位（链首基准为 1，含被撤销证书与级联失效证书） |
| `created_at` / `confirmed_at` | 告知生成时刻 / 收件人确认时刻（未确认为 `null`） |

约定与拒绝场景：

- 命中判定按**归档时刻冻结的追溯链**（与第 17 节影响面追查同一口径）；
  同一归档被多个撤销批次先后波及时各生成一份，按 `notice_id` 升序返回；
- 邀请未知 `404 invitation_not_found`、一次性密钥错误 `401 invalid_receipt_key`、
  归档尚未定稿 `404 receipt_not_found`、用同一归档的**非定稿邀请**查询
  `409 receipt_conflict`（`details.finalized_invitation_no` 给出定稿邀请）；
- `REJECT` 或未受任何撤销波及的归档返回空清单（`total=0`、`items=[]`）；
- 邀请的截止时刻不参与判定：密钥过期不影响已签收后的查询。

### 26. 收件人确认撤销风险告知（重复确认返回首次确认时间）

收件人阅读告知后逐份确认（同样凭定稿邀请编号 + 一次性密钥，路径中为
`notice_id`）：

```bash
curl -sS -X POST http://localhost:8080/api/v1/delivery-revocation-notices/1/confirm \
  -H 'Content-Type: application/json' \
  -d '{
    "invitation_no": "DINV-9f2c1a7b3e6d4850",
    "key": "Yk8-3pQ7sV2xR9mN4bL6tW0cF5hJ1dG8uZaX7eKoMnA"
  }' | python3 -m json.tool
```

- 首次确认响应 `201`，返回告知本体并写入 `confirmed_at`；**重复确认响应
  `200`，原样返回首次确认时间**（确认时刻一次性写入，并发确认同样恰一份
  首次）；
- 告知不存在、或不属于该定稿邀请对应的归档包 → `404 notice_not_found`；
  凭据校验与查询接口一致（`401` / `404` / `409` 同第 25 节）；
- 告知内容（批次、受影响目标、命中明细、生成时刻）只增不改，确认不改写
  归档快照、签收记录与撤销记录。

### 27. 设备校准委托工单（登记/结清/取消/查询，X-Management-Key）

计量机构接收设备校准委托后，用工单追踪最终证书。登记、取消、结清、查询均
须在 `X-Management-Key` 头携带本地管理密钥（与交付/停用/限额共用）。

**(1) 登记工单** —— 唯一工单号 + 设备标识 + 委托人 + 带时区计划校准半开区间
`[scheduled_from, scheduled_to)`。登记不预占证书编号/有效期/每日限额，初始
`PENDING`。同号同内容重试返回原工单（`200`），同号异内容
`409 work_order_conflict`：

```bash
curl -sS -X POST http://localhost:8080/api/v1/work-orders \
  -H 'Content-Type: application/json' \
  -H 'X-Management-Key: caltrace-delivery-admin' \
  -d '{
    "work_order_no": "WO-2026-0001",
    "device_id": "DEV-WS-A",
    "client": "某精密制造有限公司",
    "scheduled_from": "2026-06-01T09:00:00+08:00",
    "scheduled_to":   "2026-06-03T18:00:00+08:00"
  }' | python3 -m json.tool
```

首次 `201`、重试 `200`，响应：

```json
{
  "work_order_no": "WO-2026-0001",
  "status": "PENDING",
  "device_id": "DEV-WS-A",
  "client": "某精密制造有限公司",
  "scheduled_from": "2026-06-01T01:00:00Z",
  "scheduled_to": "2026-06-03T10:00:00Z",
  "created_at": "2026-05-20T03:10:00Z",
  "cert_no": null,
  "settled_at": null,
  "cancelled_at": null,
  "certificate": null
}
```

**(2) 结清工单** —— 提交已签发证书编号，仅接受设备一致、校准时刻落在计划
半开区间内、当前 `ACTIVE` 且 `PASS` 的普通证书；一张证书不得结清两个工单。
首次 `201`，**同工单同证书重试 `200`**（返回首次结果）：

```bash
curl -sS -X POST http://localhost:8080/api/v1/work-orders/WO-2026-0001/settle \
  -H 'Content-Type: application/json' \
  -H 'X-Management-Key: caltrace-delivery-admin' \
  -d '{"cert_no": "CAL-WS-001"}' | python3 -m json.tool
```

```json
{
  "work_order_no": "WO-2026-0001",
  "status": "SETTLED",
  "device_id": "DEV-WS-A",
  "client": "某精密制造有限公司",
  "scheduled_from": "2026-06-01T01:00:00Z",
  "scheduled_to": "2026-06-03T10:00:00Z",
  "created_at": "2026-05-20T03:10:00Z",
  "cert_no": "CAL-WS-001",
  "settled_at": "2026-06-02T06:30:00Z",
  "cancelled_at": null,
  "certificate": {
    "cert_no": "CAL-WS-001",
    "kind": "CALIBRATION",
    "device_id": "DEV-WS-A",
    "calibrated_at": "2026-06-02T06:00:00Z",
    "conclusion": "PASS",
    "status": "ACTIVE"
  }
}
```

结清失败示例（设备标识不一致；其余资格错误码见“错误响应”表）：

```json
{
  "error": {
    "code": "settlement_device_mismatch",
    "message": "证书 CAL-X 的设备标识 DEV-OTHER 与工单 WO-2026-0001 登记的设备标识 DEV-WS-A 不一致，不得用于结清",
    "details": {
      "work_order_no": "WO-2026-0001",
      "cert_no": "CAL-X",
      "work_order_device_id": "DEV-WS-A",
      "cert_device_id": "DEV-OTHER"
    }
  }
}
```

**(3) 取消待完成工单** —— 仅 `PENDING` 可取消；已结清不得取消或改绑：

```bash
curl -sS -X POST http://localhost:8080/api/v1/work-orders/WO-2026-0002/cancel \
  -H 'X-Management-Key: caltrace-delivery-admin'
```

**(4) 查询工单** —— 返回登记信息、状态与（已结清时）绑定证书的**当前**内容；
证书日后撤销/失效只在 `certificate.status` 实时反映，不倒退工单状态：

```bash
curl -sS http://localhost:8080/api/v1/work-orders/WO-2026-0001 \
  -H 'X-Management-Key: caltrace-delivery-admin' | python3 -m json.tool
```

要点：登记/取消/结清/查询缺/错密钥一律 `401 invalid_management_key`；
未知工单 `404 work_order_not_found`；结清后证书被撤销，工单仍 `SETTLED`，
查询中 `certificate.status` 变为 `REVOKED`（或上游撤销导致的 `INVALID`），
`settled_at` 与 `cert_no` 保持首次绑定值。

---

## 错误响应

统一格式：`{"error": {"code": "...", "message": "...", "details": {...}}}`

| HTTP | code | 触发场景 |
| --- | --- | --- |
| 404 | `cert_not_found` / `device_not_found` / `revocation_not_found` / `batch_not_found` / `declaration_not_found` / `archive_not_found` / `invitation_not_found` / `receipt_not_found` / `notice_not_found` | 目标不存在（`receipt_not_found`：归档包存在但尚未定稿；`notice_not_found`：撤销风险告知不存在或不属于该定稿邀请对应的归档包） |
| 401 | `invalid_management_key` | 创建签收邀请、查询定稿记录，登记/解除/查询标准证书停用，或设定/调整/查询每日校准使用量上限时 `X-Management-Key` 缺失/错误 |
| 401 | `invalid_identity_key` | 提交复核申请时 `X-Entry-Key`、驳回/批准/查询复核时 `X-Reviewer-Key`（或录入凭据）缺失/无效（两角色凭据不通用） |
| 403 | `reviewer_must_differ` | 复核人与录入人使用了同一把身份凭据（普通证书签发须双人复核） |
| 404 | `review_request_not_found` | 按申请号定稿或查询时申请不存在 |
| 409 | `review_request_conflict` | 同一申请号提交了签发资料不同的复核申请 |
| 409 | `review_already_decided` | 申请已有定稿：驳回后再批准、批准后改判、或其他复核人再定稿（同复核人同操作重试返回首次结果） |
| 422 | `review_reason_required` | 驳回（REJECT）未填写或仅空白的驳回原因 |
| 422 | `review_approve_reason_not_allowed` | 批准（APPROVE）请求携带了驳回原因 |
| 409 | `review_busy` | 批准与并发停用/撤销竞争且死锁重试耗尽（申请仍为待审，可重试） |
| 401 | `invalid_receipt_key` | 提交签收，或查询/确认撤销风险告知时一次性密钥错误 |
| 409 | `suspension_conflict` | 证书已存在生效中的临时停用记录，重复停用被拒 |
| 409 | `suspension_not_active` | 证书没有生效中的临时停用记录（重复/并发解除） |
| 409 | `cert_not_active` | 撤销一张非 ACTIVE 证书，或对非生效（含已撤销/失效）证书登记停用 |
| 422 | `suspension_reason_required` | 登记停用或解除停用时原因为空或纯空白 |
| 422 | `reference_suspended` | 签发（单张/批次、含批内引用）引用了停用中证书或其任何下游证书（`details` 给出受阻证书 `blocked_cert_no` 与停用源 `suspension_source`，批次另附 `cert_no`） |
| 409 | `issuance_busy` / `suspension_busy` | 签发（单张/批次）或停用/解除与并发停用/撤销/签发竞争且死锁重试耗尽（防御性兜底，调用方可重试） |
| 422 | `daily_quota_exceeded` | 普通证书签发（单张/复核批准/批次）将使“直接上游证书 × calibrated_at 的 UTC 日”用量超过已配置的每日上限（`details` 点名 `parent_cert_no`、`day`、`daily_limit`、`used`、`cert_no`；批次整批不落库、复核申请保持 PENDING） |
| 422 | `quota_below_usage` | 设定/调整的非负新上限低于该“证书×UTC 日”已签发次数（撤销/失效不退还已用次数），本次调整不生效 |
| 409 | `quota_busy` | 限额调整与并发签发竞争且死锁重试耗尽（防御性兜底，调用方可重试） |
| 404 | `quota_not_found` | 查询的“证书×UTC 日”未配置每日上限（`details.used` 仍给出实时已用次数；未配置即沿用原签发规则） |
| 409 | `cert_duplicate` | 证书编号已存在（含并发重复签发、批次与库内冲突） |
| 409 | `batch_conflict` | 同一批次键提交了内容不同的签发请求 |
| 409 | `correction_conflict` | 同一声明编号提交了内容不同的更正请求 |
| 409 | `archive_conflict` | 同一归档包编号提交了证书列表不同（含仅顺序不同）的交付请求 |
| 409 | `archive_busy` | 归档与并发撤销/更正竞争且死锁重试耗尽（防御性兜底，调用方可重试） |
| 409 | `receipt_conflict` | 归档包已有定稿：不同内容重试、用其他邀请再提交，或用非定稿邀请查询/确认撤销风险告知（同邀请同内容重放返回首次结果） |
| 409 | `invitation_expired` | 尚未定稿的邀请已过截止时刻（密钥正确也不能签收） |
| 409 | `receipt_busy` | 签收与并发撤销竞争且死锁重试耗尽（防御性兜底，调用方可重试） |
| 422 | `invitation_deadline_past` | 创建邀请时截止时间不晚于当前时刻 |
| 422 | `invitation_recipient_required` | 创建邀请时收件人为空白 |
| 422 | `receipt_reason_required` | REJECT 未填写或仅空白的拒收原因 |
| 422 | `receipt_targets_revoked` | ACCEPT 时包内目标或冻结追溯链中已有证书撤销/失效（`details.affected_targets` 点名受影响包内目标；REJECT 不受此限） |
| 422 | `archive_cert_duplicate` | 归档包内证书编号重复 |
| 422 | `archive_not_deliverable` | 归档目标存在不达标项：不存在/非 CALIBRATION/目标或上游链非 ACTIVE/非 PASS（`details` 分类点名，整包未保存） |
| 422 | `batch_cert_duplicate` | 批次内证书编号重复 |
| 422 | `correction_empty` | 更正声明的修正字段集合为空 |
| 422 | `correction_field_not_allowed` | 更正了 device_name/operator/remark 之外的字段（`details.fields` 点名） |
| 422 | `correction_field_invalid` | 更正字段值越界（如长度超限） |
| 422 | `correction_reason_required` | 更正原因为空或纯空白 |
| 422 | `invalid_reference` | 引用的标准证书不存在（库内与本批均找不到） |
| 422 | `reference_not_active` | 引用了已撤销/已失效证书（失效证书不得作依据） |
| 422 | `reference_not_qualified` | 引用的标准证书结论为不合格 |
| 422 | `reference_out_of_validity` | 校准时刻不在标准证书有效期内 |
| 422 | `reference_cycle` | 引用关系将形成环路（含批内成环） |
| 422 | `validity_overlap` | 同设备已生效证书有效期重叠（含批内重叠） |
| 409 | `validity_overlap_concurrent` | 并发签发重叠，排他约束兜底拒绝 |
| 422 | `invalid_audit_window` | 覆盖审计起止无效：缺时区/起点不早于终点/终点晚于查询时刻 |
| 401 | `invalid_management_key` | 登记/取消/结清/查询委托工单时 `X-Management-Key` 缺失/错误（与交付/停用/限额共用同一把密钥） |
| 404 | `work_order_not_found` | 取消/结清/查询时工单不存在 |
| 409 | `work_order_conflict` | 同一工单号登记了内容不同的委托工单（同号异内容） |
| 409 | `work_order_not_pending` | 对非待完成工单操作：已结清工单再取消/改绑、已取消工单再取消或结清（`details.status` 给出当前状态） |
| 409 | `work_order_cert_rebind_conflict` | 已结清工单提交了另一张证书编号（绑定固定不可改绑；同工单同证书重试返回首次结果） |
| 409 | `settlement_cert_already_settled` | 证书已用于结清另一工单（含并发），一张证书不得结清两个工单（`details.bound_work_order_no` 点名持有工单） |
| 422 | `settlement_cert_not_calibration` | 用基准证书（REFERENCE）结清工单：仅接受普通校准证书 |
| 422 | `settlement_device_mismatch` | 证书设备标识与工单登记的设备标识不一致（`details` 给出两侧设备标识） |
| 422 | `settlement_outside_schedule` | 证书校准时刻不在工单计划半开区间 `[scheduled_from, scheduled_to)` 内（恰等于终点也算越界） |
| 422 | `settlement_cert_not_active` | 证书当前非 ACTIVE（已撤销/失效），不得结清工单 |
| 422 | `settlement_cert_not_passed` | 证书校准结论非 PASS（FAIL），不得结清工单 |
| 409 | `work_order_busy` | 工单结清与并发取消/撤销竞争且死锁重试耗尽（工单保持待完成，调用方可重试） |

> 批次签发中由单条记录触发的校验错误与单张签发同码，并在 `details.cert_no`
> 中给出出错证书编号（必要时附 `details.parent_cert_no` / `details.cycle` 等）。

---

## 自测

服务启动后执行（仅依赖 bash + curl / python3 标准库）：

```bash
scripts/smoke-test.sh                 # 17 项主流程与错误路径
python3 scripts/extra_checks.py       # 21 项并发/边界/中段撤销补充检查
python3 scripts/batch_checks.py       # 批次签发：原子性/乱序引用/幂等重放/键冲突/并发
python3 scripts/correction_checks.py  # 更正声明：顺序生效/显式置空/幂等/冲突/并发
python3 scripts/archive_checks.py     # 交付归档包：冻结快照/整包拒绝点名/重试/冲突/并发一致性
python3 scripts/impact_checks.py      # 撤销影响面：命中判定/序位/稳定分页/空页/并发提交次序
python3 scripts/receipt_checks.py     # 交付签收：邀请一次性密钥/截止/定稿幂等冲突/撤销拦截/并发次序
python3 scripts/coverage_checks.py    # 覆盖审计：计入窗口/撤销与级联截断/重签还原/空窗/参数错误
python3 scripts/suspension_checks.py  # 标准证书临时停用：登记/解除/查询/受阻链单张与批次拦截/不改写/提交次序并发
python3 scripts/review_checks.py      # 普通证书双人复核：凭据/待审不占用/驳回不可再批准/批准按当时规则真正签发/失败仍待审/幂等与并发定稿/契约不变
python3 scripts/notice_checks.py      # 撤销风险告知：ACCEPT 归档波及生成/同包同批一份/未签收与 REJECT 不生成/查询确认凭据/过期确认/并发次序
python3 scripts/quota_checks.py       # 标准证书每日用量上限：设定调整/下限保护/UTC 切日/单张批次合并计数整批不落库/复核待审/撤销不退/幂等不重复计数/并发不超额
python3 scripts/workorder_checks.py   # 设备校准委托工单：登记幂等冲突/取消仅待完成/结清五条件与一证一单/绑定固定/撤销不倒退/并发结清取消撤销
```

覆盖：基准登记、多级签发、无效引用、越界校准时刻、区间重叠（含首尾相接）、
同编号并发与重叠区间并发（3 线程仅 1 成功）、中段撤销、级联失效、失效证书
禁止引用、撤销后重签、历史可查、撤销批次回放，数据库层 UPDATE/DELETE 封印，
以及批次签发的整批原子性、批内乱序引用与拓扑校验、同键重放（含事后撤销）、
同键冲突、并发重复提交（5 线程仅 1 次首次签发）与批次查询；
更正声明的登记与顺序生效、显式置空与未提交的区分、原证书/追溯链/批次快照
不受更正影响、已撤销/已失效证书可更正且状态判定不变、同编号同内容重放
（含键序无关）、同编号异内容冲突、同一证书并发更正（8 线程 seq 1..8 不丢失
不乱序）、并发同编号（同内容仅 1 次登记、异内容仅 1 次成功）与全部错误路径；
交付归档包的创建快照（原始内容/完整链/合并展示/提交顺序）、同编号同有序列表
重放（首次 201、重试 200，事后更正或撤销快照不变）、同编号异列表（含仅顺序
不同）冲突、未知编号 404、整包拒绝且不落库（不存在/基准证书/FAIL/上游链撤销/
重复编号，多问题分类点名）、与并发撤销（24 组链各自归档×撤销，成功快照整链
ACTIVE，无新旧状态混合）、与并发更正（展示值只取某个已提交的一致合并时点）、
并发同编号（同列表 1 次创建其余重放、异列表仅 1 次创建其余 409）以及
150 线程撤销+更正+归档高强度混合压测（零死锁、零混合时点）；
撤销影响面追查的命中判定（被撤销证书与级联失效证书在冻结链中）、
两种 1 起始序位、按（归档包编号, 原交付序位）稳定分页与末页后空页、
同一目标多包分别列出与同包去重、未受影响项不出现、撤销后被拒/新建
归档不入清单、根撤销覆盖既有归档包、无影响批次空页、未知批次 404、
归档与撤销并发按实际提交次序判定（16 组链各自归档×撤销逐批核对），
以及既有归档快照/归档查询/撤销响应契约不变；
交付签收邀请的管理密钥校验（缺失/错误 401）、归档不存在 404、
截止非未来值 422、收件人空白 422、一次性随机密钥仅展示一次且库内仅存
SHA-256、同一归档多份邀请；签收录制的未知邀请 404、错误密钥 401、
REJECT 缺原因 422、同邀请同内容重放 200（含截止后重放，逐字段等于首次
结果）、同邀请异内容与其他邀请再提交 409、未定稿过期 409（ACCEPT/REJECT
均拒）、ACCEPT 时链上撤销/目标自身撤销/多目标包仅部分受影响的
`receipt_targets_revoked` 点名、REJECT 在撤销后仍可提交、归档快照与定稿
不被后续撤销改写、管理密钥查询（正确/错误密钥/未知归档/未定稿 404）；
并发方面覆盖 16 组链 ACCEPT×撤销同时提交的提交次序判定（先签收则定稿
ACCEPT 且不被随后撤销改写，先撤销则 ACCEPT 422 且不留定稿）、同邀请同内容
10 并发（1×201 其余 200、响应全同）、同邀请互异内容 10 并发（1×201 其余
409）、两份不同邀请并发（1×201 + 1×409 仅胜方留档），以及既有签发/撤销/
归档接口契约与根索引端点清单不变；
设备校准覆盖审计的计入窗口（签发创建时刻与有效期起点取较晚者起算，
有效期终点与撤销/级联失效时刻取较早者退出）、撤销截断与撤销后重叠
有效期重签按事件还原、级联失效时刻截断下游、上游事后到期不取消下游
覆盖、未来生效/签发时已过期/不合格/基准证书均不提供覆盖、未知设备
整段空窗、子区间端点裁剪、片段完整无重叠且相邻不重复、不同时区表示
同一时刻结果一致、起止无效/缺时区/终点晚于查询时刻报
`422 invalid_audit_window`，以及既有证书/设备历史/撤销响应契约不变；
标准证书临时停用的管理密钥校验（缺失/错误 401）、非生效证书停用 409、
原因空白 422、重复停用 409、无生效停用时解除 409、未知证书 404、停用后
当前状态与历次停用/解除（时间、原因、多轮追加）查询；停用期间单张签发
引用停用证书与其多级下游、批次外部引用与批内乱序间接引用（拓扑校验后
逐张拦截）均 `422 reference_suspended` 且错误详情点名受阻证书与停用源、
整批不落库（无批次快照、批内证书不可见），解除后签发恢复、同键同内容
批次仍返回首次签发快照；停用不改写证书内容/状态、撤销记录与级联失效、
已冻结交付快照、定稿记录、更正视图；停用期间交付归档/更正/ACCEPT 不受
影响，撤销与停用可并存且互不改写；停用/解除×签发的 32 路提交次序并发
（先停用则后到签发必拒、先签发则落库；先解除则放行、签发先到则 422），
以及既有签发/撤销/批次/归档/签收接口契约与根索引端点清单不变。
普通证书双人复核签发的录入/复核凭据校验（缺失/错误/角色串用 401、
同凭据复核 403 且申请仍待审）、申请提交（201 PENDING、待审不占证书编号
与设备有效期、父证书暂不存在也可提交）、申请号幂等（同内容重放 200、
异内容 409、并发同号同内容 1 份 201 其余 200）、驳回（空白原因 422、
REJECTED 绑定结论/复核人/原因/时刻且不产生证书、驳回后再批准与异原因
再驳回 409、同复核人同原因重放 200、其他复核人再提交 409）、批准
（同事务真正签发并返回与直接签发同构的证书与追溯链、事后证书可查可撤销、
批准后不可改判、同操作重放返回首次结果且签发时刻不变）、批准校验失败
（父不存在/已撤销/临时停用/编号占用/区间重叠/校准时刻超有效期分别返回
与直接签发一致的具体错误码，申请恒仍 PENDING 且无证书；停用解除后原申请
可再次批准成功）、按申请号查询（无/错凭据 401、未知申请 404、待审/批准/
驳回三视图、批准结果为首次签发快照且事后撤销不改写）、12 路并发混合
定稿（恰一份 201 其余 409、最终状态与胜方一致、批准胜方只签一张证书/
驳回胜方不产生证书）、8 路同复核人同操作并发（1×201 其余 200 且响应
全等），以及既有直接签发/查询/撤销/追溯链契约与根索引端点清单不变。
撤销风险告知的生成（已定稿 ACCEPT 归档受撤销波及生成一份、列出撤销批次/
受影响目标/链上命中编号含 1 起始序位、同包同批只记一份、同批多包各一份、
未签收与 REJECT 不生成、未波及不生成、中段撤销命中被撤销与级联失效证书、
同一归档先后被两批波及各一份且按编号升序）、查询凭据（未知邀请 404、
错误密钥 401、非定稿邀请 409、未定稿 404、REJECT 空清单、重复查询稳定）、
确认（首次 201 写入确认时刻、重复确认 200 返回首次确认时间、跨归档/未知
告知 404、8 路并发确认恰 1×201 其余 200 且确认时间全同、短截止邀请过期后
查询与确认不受影响）、16 组链签收×撤销并发按提交次序判定（先签收则生成
告知且批次对应，先撤销则 ACCEPT 422 且无定稿无告知），以及既有归档快照/
定稿记录/撤销批次/影响面追查契约与根索引端点清单不变。
标准证书每日校准使用量上限的管理密钥校验（缺失/错误 401）、未知证书
404、负上限与非法日期 422、首次配置 201 与调整 200（含 `created_at`
不变/`updated_at` 更新）、下调低于当日已用次数 `422 quota_below_usage`
且原配置不变、未配置日查询 `404 quota_not_found` 且 `details.used` 实时
给出；单张签发按直接上游与 `calibrated_at` 的 UTC 日计一次（含 UTC 切日
边界、显式 UTC 偏移、其他 UTC 日不受限、PASS/FAIL 均计、超限额证书不落库）、
撤销/失效不退还已用次数（撤销后新签发仍超限、上游撤销后先按
`reference_not_active` 拒绝）；交接批次批内多张相同键合并计数、超限整批
不落库且无首次快照、恰满上限整批成功、用满后新批次整批不落库、成功批次
同键重放 200 不重复计数、同键异内容仍 `409 batch_conflict`、仅基准证书
批次不受限、批内引用链按直接上游（本批/库内）逐键计数并整批拦截；双人
复核批准在真正签发时扣额、超限 422 且申请保持 PENDING、上调后原申请可
再次批准、驳回不计数；未配置上限的证书单张与批次完全沿用原签发规则；
并发签发成功落库数绝不超过上限（未成功者均不落库，既有 `issuance_busy`
竞争保护不影响该不变量）、并发“下调为 0 × 签发”按提交次序串行
（成功签发数恰等于下调时已用数，下调后到达的签发无额外成功），以及根
索引端点清单包含新接口、既有签发/批次/复核接口契约不变。
设备校准委托工单的管理密钥校验（缺失/错误 401）、计划半开区间校验
（倒区间/零长度/空白字段 422）、登记幂等（同号同内容 200、同号异内容
409、8 路并发同号 1×201 其余 200）、查询（未知工单 404、缺密钥 401）、
取消（仅 PENDING 可取消，结清/取消后再操作 409）、结清五条件分别命中
（基准证书/设备不一致/校准时刻早于起点与恰等于终点/FAIL/不存在，且全部
失败后工单仍 PENDING）、半开起点（含）可结清、成功结清 201 且返回绑定
证书当前内容、同工单同证书重试 200 且 `settled_at` 不变、改绑其他证书
409、结清后取消 409、一证结清第二工单 409 且点名持有工单、第二张证书可
结清另一工单；结清后撤销绑定证书工单仍 SETTLED 且证书状态实时为
REVOKED、撤销后同证重试仍返回首次结果、撤销证书结清新单 422、上游撤销
级联 INVALID 不倒退已结清工单且新单结清 422；并发结清×取消 6 组按提交
次序恰一方生效（无“已取消又结清/已结清又取消”）、两工单与 8 工单并发
同证书结清恰 1×201 其余 409（无重复绑定）；登记工单不预占（证书随后正常
签发与结清），以及根索引端点清单包含 4 个工单接口、健康检查与既有证书
查询契约不变（共 55 项）。
