"""数据访问与核心业务规则（全部在数据库事务中执行）。"""
import hashlib
import heapq
import hmac
import json
import secrets
from datetime import date, datetime, timezone

import psycopg
from psycopg.types.json import Jsonb

from .config import settings
from .errors import ApiError
from .schemas import CertificateIssueRequest

# v_certificates 视图的列顺序
_VIEW_COLS = (
    "cert_no, kind, device_id, device_name, calibrated_at, valid_from, valid_to, "
    "conclusion, parent_cert_no, operator, remark, status, revoked_reason, "
    "revoked_at, created_at"
)


# ---------------------------------------------------------------------------
# 公共查询
# ---------------------------------------------------------------------------

def _get_cert_row(conn, cert_no: str, lock: bool = False) -> dict | None:
    sql = f"SELECT {_VIEW_COLS} FROM v_certificates WHERE cert_no = %s"
    if lock:
        # 视图基于单表，FOR UPDATE 会落到底层 certificates 行上
        sql += " FOR UPDATE"
    return conn.execute(sql, (cert_no,)).fetchone()


def get_certificate(conn, cert_no: str) -> dict:
    row = _get_cert_row(conn, cert_no)
    if row is None:
        raise ApiError(404, "cert_not_found", f"证书不存在: {cert_no}")
    return row


def get_chain(conn, cert_no: str) -> dict:
    """返回从追溯起点（基准证书）到目标证书的完整引用链。"""
    if _get_cert_row(conn, cert_no) is None:
        raise ApiError(404, "cert_not_found", f"证书不存在: {cert_no}")

    sql = f"""
        WITH RECURSIVE walk AS (
            SELECT cert_no, parent_cert_no, 0 AS depth
            FROM certificates WHERE cert_no = %s
            UNION ALL
            SELECT c.cert_no, c.parent_cert_no, w.depth - 1
            FROM certificates c JOIN walk w ON c.cert_no = w.parent_cert_no
        )
        CYCLE cert_no SET is_cycle USING cycle_path
        SELECT w.depth, vc.cert_no, vc.kind, vc.device_id, vc.device_name,
               vc.calibrated_at, vc.valid_from, vc.valid_to, vc.conclusion,
               vc.parent_cert_no, vc.operator, vc.remark, vc.status,
               vc.revoked_reason, vc.revoked_at, vc.created_at
        FROM walk w JOIN v_certificates vc ON vc.cert_no = w.cert_no
        WHERE NOT w.is_cycle
        ORDER BY w.depth ASC, vc.cert_no ASC
    """
    nodes = [
        {"depth": r.pop("depth"), "certificate": r}
        for r in conn.execute(sql, (cert_no,)).fetchall()
    ]
    if not nodes:
        raise ApiError(404, "cert_not_found", f"证书不存在: {cert_no}")
    return {
        "cert_no": cert_no,
        "nodes": nodes,
        "root_cert_no": nodes[0]["certificate"]["cert_no"],
        "length": len(nodes),
    }


def list_certificates(
    conn,
    device_id: str | None = None,
    status: str | None = None,
    kind: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict:
    where, params = [], []
    if device_id:
        where.append("device_id = %s")
        params.append(device_id)
    if status:
        where.append("status = %s")
        params.append(status)
    if kind:
        where.append("kind = %s")
        params.append(kind)
    clause = ("WHERE " + " AND ".join(where)) if where else ""

    total = conn.execute(
        f"SELECT count(*) AS n FROM certificates {clause}", params
    ).fetchone()["n"]
    rows = conn.execute(
        f"SELECT {_VIEW_COLS} FROM v_certificates {clause} "
        f"ORDER BY created_at DESC, cert_no ASC LIMIT %s OFFSET %s",
        (*params, limit, offset),
    ).fetchall()
    return {"items": rows, "total": total, "limit": limit, "offset": offset}


def get_device_history(conn, device_id: str) -> list[dict]:
    seen = conn.execute(
        "SELECT 1 FROM certificates WHERE device_id = %s LIMIT 1", (device_id,)
    ).fetchone()
    if seen is None:
        raise ApiError(404, "device_not_found", f"设备无任何证书记录: {device_id}")
    return conn.execute(
        f"SELECT {_VIEW_COLS} FROM v_certificates WHERE device_id = %s "
        f"ORDER BY calibrated_at DESC, created_at DESC",
        (device_id,),
    ).fetchall()


def get_downstream(conn, cert_no: str) -> dict:
    if _get_cert_row(conn, cert_no) is None:
        raise ApiError(404, "cert_not_found", f"证书不存在: {cert_no}")
    sql = """
        WITH RECURSIVE d AS (
            SELECT cert_no, 1 AS depth, status
            FROM certificates WHERE parent_cert_no = %s
            UNION ALL
            SELECT c.cert_no, d.depth + 1, c.status
            FROM certificates c JOIN d ON c.parent_cert_no = d.cert_no
        )
        CYCLE cert_no SET is_cycle USING cycle_path
        SELECT cert_no, depth, status::text AS status
        FROM d WHERE NOT is_cycle
        ORDER BY depth ASC, cert_no ASC
    """
    return {"cert_no": cert_no, "descendants": conn.execute(sql, (cert_no,)).fetchall()}


# ---------------------------------------------------------------------------
# 证书签发
# ---------------------------------------------------------------------------

def _lock_parent_chain_share(conn, parent_no: str) -> None:
    """对被引用证书的完整上游链按“追溯起点→下游”顺序加 FOR SHARE 行锁。

    用于签发事务在读取“停用受阻链”之前与停用登记/解除事务（对同链证书
    持有 FOR UPDATE）串行化：停用已提交则随后的检查必见，停用尚未提交则
    被阻塞到签发提交之后，二者按实际提交次序生效，不存在混用时点或等待环
    （加锁顺序与撤销/停用/交付事务的链上加锁顺序一致）。
    """
    conn.execute(
        """
        WITH RECURSIVE walk AS (
            SELECT cert_no, parent_cert_no, 0 AS depth
            FROM certificates WHERE cert_no = %s
            UNION ALL
            SELECT c.cert_no, c.parent_cert_no, w.depth - 1
            FROM certificates c JOIN walk w ON c.cert_no = w.parent_cert_no
        )
        CYCLE cert_no SET is_cycle USING cycle_path
        SELECT c.cert_no
        FROM certificates c
        JOIN walk w ON w.cert_no = c.cert_no
        WHERE NOT w.is_cycle
        ORDER BY w.depth ASC, c.cert_no ASC
        FOR SHARE OF c
        """,
        (parent_no,),
    ).fetchall()


def _suspension_blocker(conn, parent_no: str, new_cert_no: str | None = None) -> dict | None:
    """被引用的标准证书（或其任一上游）若存在生效中的停用记录，返回受阻信息。

    命中时返回 blocked_cert_no（被引用的受阻证书，即直接 parent_no）、
    suspension_source（最近停用源证书编号）、suspension_id。调用方须已对
    完整上游链持有 FOR SHARE 锁（见 _lock_parent_chain_share）。
    """
    row = conn.execute(
        """
        WITH RECURSIVE walk AS (
            SELECT cert_no, parent_cert_no, 0 AS depth
            FROM certificates WHERE cert_no = %s
            UNION ALL
            SELECT c.cert_no, c.parent_cert_no, w.depth - 1
            FROM certificates c JOIN walk w ON c.cert_no = w.parent_cert_no
        )
        CYCLE cert_no SET is_cycle USING cycle_path
        SELECT s.suspension_id, s.cert_no AS source_cert_no
        FROM cert_suspensions s
        JOIN walk w ON w.cert_no = s.cert_no
        WHERE NOT w.is_cycle AND s.lifted_at IS NULL
        ORDER BY w.depth DESC, s.suspended_at DESC, s.suspension_id DESC
        LIMIT 1
        """,
        (parent_no,),
    ).fetchone()
    if row is None:
        return None
    return {
        "blocked_cert_no": parent_no,
        "suspension_source": row["source_cert_no"],
        "suspension_id": row["suspension_id"],
        "new_cert_no": new_cert_no,
    }


def _validate_parent(
    conn, parent_no: str, calibrated_at, new_cert_no: str | None = None
) -> dict:
    """被引用的标准设备证书：必须存在、生效中、合格，且校准时刻落在有效期内。"""
    _lock_parent_chain_share(conn, parent_no)
    parent = _get_cert_row(conn, parent_no, lock=True)
    if parent is None:
        raise ApiError(422, "invalid_reference", f"引用的标准证书不存在: {parent_no}")
    if parent["status"] != "ACTIVE":
        raise ApiError(
            422,
            "reference_not_active",
            f"引用的标准证书 {parent_no} 当前状态为 {parent['status']}，"
            "已撤销/失效证书不得作为校准依据",
        )
    blocker = _suspension_blocker(conn, parent_no, new_cert_no)
    if blocker is not None:
        details = {k: v for k, v in blocker.items() if v is not None}
        if new_cert_no is not None:
            details["cert_no"] = new_cert_no
        raise ApiError(
            422,
            "reference_suspended",
            f"证书 {parent_no} 处于临时停用中（停用源：{blocker['suspension_source']}）"
            "或其追溯链上游存在停用标准证书，停用期间不得作为新签发证书的追溯依据",
            details=details,
        )
    if parent["conclusion"] != "PASS":
        raise ApiError(
            422,
            "reference_not_qualified",
            f"引用的标准证书 {parent_no} 结论为不合格，不得作为校准依据",
        )
    covers = conn.execute(
        "SELECT tstzrange(valid_from, valid_to, '[)') @> %s::timestamptz AS ok FROM certificates "
        "WHERE cert_no = %s",
        (_as_utc(calibrated_at), parent_no),
    ).fetchone()
    if not covers or not covers["ok"]:
        raise ApiError(
            422,
            "reference_out_of_validity",
            f"校准时刻 {calibrated_at.isoformat()} 不在标准证书 {parent_no} 的"
            f"有效期 [{parent['valid_from'].isoformat()}, {parent['valid_to'].isoformat()}) 内",
        )
    return parent


def _assert_no_cycle(conn, parent_no: str, new_cert_no: str) -> None:
    """防御性成环检测：若新证书编号已出现在上游链中，则拒绝（正常情况下不会触发）。"""
    sql = """
        WITH RECURSIVE up AS (
            SELECT cert_no, parent_cert_no FROM certificates WHERE cert_no = %s
            UNION ALL
            SELECT c.cert_no, c.parent_cert_no
            FROM certificates c JOIN up u ON c.cert_no = u.parent_cert_no
        )
        SELECT 1 FROM up WHERE parent_cert_no = %s LIMIT 1
    """
    if conn.execute(sql, (parent_no, new_cert_no)).fetchone():
        raise ApiError(
            422,
            "reference_cycle",
            f"引用关系将形成环路（{new_cert_no} 已在 {parent_no} 的上游链中）",
        )


def _assert_no_overlap(conn, device_id: str, valid_from, valid_to, exclude_no: str | None) -> None:
    # 无时区输入按 UTC 处理：统一为带时区值后再构造 tstzrange，
    # 避免 naive datetime 被适配为 timestamp without time zone 而无匹配函数/操作符。
    valid_from = _as_utc(valid_from)
    valid_to = _as_utc(valid_to)
    row = conn.execute(
        """
        SELECT cert_no FROM certificates
        WHERE device_id = %s AND status = 'ACTIVE' AND cert_no IS DISTINCT FROM %s
          AND tstzrange(valid_from, valid_to, '[)') && tstzrange(%s, %s, '[)')
        LIMIT 1
        """,
        (device_id, exclude_no, valid_from, valid_to),
    ).fetchone()
    if row:
        raise ApiError(
            422,
            "validity_overlap",
            f"设备 {device_id} 已生效证书 {row['cert_no']} 的有效期与本次签发区间重叠"
            "（区间按半开 [valid_from, valid_to) 判定）",
            details={"conflict_cert_no": row["cert_no"]},
        )


def _raw_insert_certificate(conn, *, kind: str, conclusion: str, parent_no: str | None, req) -> None:
    """插入证书行；数据库层异常原样抛出，由调用方按场景转换。"""
    conn.execute(
        """
        INSERT INTO certificates (
            cert_no, kind, device_id, device_name, calibrated_at,
            valid_from, valid_to, conclusion, parent_cert_no, operator, remark
        ) VALUES (
            %s, %s::cert_kind, %s, %s, %s, %s, %s, %s::cert_conclusion, %s, %s, %s
        )
        """,
        (
            req.cert_no, kind, req.device_id, req.device_name, _as_utc(req.calibrated_at),
            _as_utc(req.valid_from), _as_utc(req.valid_to), conclusion,
            parent_no, req.operator, req.remark,
        ),
    )


def _insert_certificate(conn, *, kind: str, conclusion: str, parent_no: str | None, req) -> dict:
    try:
        _raw_insert_certificate(
            conn, kind=kind, conclusion=conclusion, parent_no=parent_no, req=req
        )
    except psycopg.errors.UniqueViolation:
        raise ApiError(409, "cert_duplicate", f"证书编号已存在: {req.cert_no}")
    except psycopg.errors.ExclusionViolation:
        # 并发事务抢先插入了重叠区间——排他约束兜底
        raise ApiError(
            409,
            "validity_overlap_concurrent",
            f"设备 {req.device_id} 已存在与本次有效期重叠的生效证书（并发签发冲突），"
            f"证书编号 {req.cert_no} 未签发",
        )
    except psycopg.errors.ForeignKeyViolation:
        raise ApiError(422, "invalid_reference", f"引用的标准证书不存在: {parent_no}")
    except psycopg.errors.CheckViolation as exc:
        raise ApiError(422, "rule_violation", f"签发被数据库完整性规则拒绝: {exc}")
    return get_certificate(conn, req.cert_no)


def _issue_certificate_in_tx(conn, req) -> dict:
    """在调用方事务内完成普通证书的全部签发校验与落库，并返回证书与追溯链。

    供单张直接签发（外层包一层事务）与复核申请批准（批准与申请状态推进、
    结果绑定必须在同一事务内：签发任一步失败则整事务回滚，申请仍为待审、
    不产生证书）共用。
    """
    _validate_parent(conn, req.parent_cert_no, req.calibrated_at, req.cert_no)
    _assert_daily_quota(
        conn, req.parent_cert_no, req.calibrated_at, req.cert_no,
    )
    _assert_no_cycle(conn, req.parent_cert_no, req.cert_no)
    _assert_no_overlap(conn, req.device_id, req.valid_from, req.valid_to, req.cert_no)
    cert = _insert_certificate(
        conn, kind="CALIBRATION", conclusion=req.conclusion,
        parent_no=req.parent_cert_no, req=req,
    )
    return {"certificate": cert, "chain": get_chain(conn, req.cert_no)}


def issue_certificate(conn, req) -> dict:
    """签发普通校准证书。

    停用/解除/撤销事务在追溯链上持 FOR UPDATE/FOR SHARE 锁，与并发签发的
    链上锁可能形成被死锁检测终止的等待环：检测到死锁后整体重试（重试事务
    按已提交的停用状态判定），耗尽则 409 issuance_busy（调用方可重试）。
    """
    try:
        with conn.transaction():
            return _issue_certificate_in_tx(conn, req)
    except psycopg.errors.DeadlockDetected:
        try:
            with conn.transaction():
                return _issue_certificate_in_tx(conn, req)
        except psycopg.errors.DeadlockDetected:
            try:
                with conn.transaction():
                    return _issue_certificate_in_tx(conn, req)
            except psycopg.errors.DeadlockDetected:
                raise ApiError(
                    409,
                    "issuance_busy",
                    f"证书 {req.cert_no} 的签发与并发停用/撤销竞争，重试后仍冲突，请稍后重试",
                    details={"cert_no": req.cert_no},
                )


def issue_reference(conn, req) -> dict:
    """登记基准设备合格证书（追溯起点，恒为合格、无上游）。"""
    with conn.transaction():
        _assert_no_overlap(conn, req.device_id, req.valid_from, req.valid_to, req.cert_no)
        cert = _insert_certificate(
            conn, kind="REFERENCE", conclusion="PASS", parent_no=None, req=req,
        )
    chain = get_chain(conn, req.cert_no)
    return {"certificate": cert, "chain": chain}


# ---------------------------------------------------------------------------
# 标准证书每日校准使用量上限（按 证书编号 × UTC 日期 配置，凭管理密钥）
#
# 计数口径：每张普通证书（CALIBRATION）在真正签发（事务提交）时，按其
# calibrated_at 的 UTC 日期对其【直接引用】的上游证书计一次；基准证书登记、
# 未配置上限的键均不受限制。撤销或级联失效不退还已用次数——已用次数即
# certificates 中满足 parent_cert_no/day 的现存普通证书行，只增不减。
#
# 并发正确性：
#   * 签发事务在 _validate_parent 中已持有直接上游证书行的 FOR UPDATE 锁
#     （单张/复核批准），批次按拓扑序逐张处理时每张普通证书的直接上游
#     （库内或本批）同样已持有该行的 FOR UPDATE 锁；
#   * 配置/调整事务先对目标证书行加 FOR UPDATE 再写限额，因此限额调整与
#     该证书的并发签发在同一行锁上按提交次序串行化：调整先提交则随后的
#     签发按新限额判定，签发先提交则其已用次数计入后调整端按新用量复核
#     （低于已用次数的下调被拒绝）；
#   * 未配置上限时 cert_daily_quotas 无行，扣额为只读计数短路，原签发
#     规则与时序完全不变；
#   * 调整事务与多父批次理论上的等待环由死锁检测终止后整体重试。
# ---------------------------------------------------------------------------

_QUOTA_COLS = "cert_no, day, daily_limit, created_at, updated_at"


def _utc_calibrated_day(value) -> date:
    """校准时刻换算 UTC 后的日历日期（计数与配置键一律以此为准）。"""
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=timezone.utc).date()
    return value.astimezone(timezone.utc).date()


def _count_daily_used(conn, parent_no: str, day: date) -> int:
    """已用次数：以 parent_no 为直接上游、校准时刻 UTC 日期为 day 的现存
    普通证书数量（含本事务已插入、尚未提交的行；撤销/失效不退还）。"""
    row = conn.execute(
        """
        SELECT count(*) AS n
        FROM certificates
        WHERE parent_cert_no = %s
          AND (calibrated_at AT TIME ZONE 'UTC')::date = %s
        """,
        (parent_no, day),
    ).fetchone()
    return row["n"]


def _assert_daily_quota(conn, parent_no: str, calibrated_at, cert_no: str) -> None:
    """签发扣额：仅当存在 (直接上游, UTC 日) 限额行时校验。

    调用方必须已持有 parent_no 证书行的 FOR UPDATE 行锁（见
    _validate_parent / 批次拓扑插入），与限额调整事务按提交次序串行化。
    超限抛出 422 daily_quota_exceeded，调用方事务整体回滚（单张不签发、
    批次整批不落库、复核申请保持待审）。
    """
    day = _utc_calibrated_day(calibrated_at)
    quota = conn.execute(
        f"SELECT {_QUOTA_COLS} FROM cert_daily_quotas "
        "WHERE cert_no = %s AND day = %s FOR UPDATE",
        (parent_no, day),
    ).fetchone()
    if quota is None:
        # 未配置上限：沿用原签发规则
        return
    used = _count_daily_used(conn, parent_no, day)
    if used >= quota["daily_limit"]:
        raise ApiError(
            422,
            "daily_quota_exceeded",
            f"上游标准证书 {parent_no} 在 UTC 日期 {day.isoformat()} 的校准签发"
            f"已达每日上限 {quota['daily_limit']}（已用 {used}），"
            f"证书 {cert_no} 本次不予签发",
            details={
                "cert_no": cert_no,
                "parent_cert_no": parent_no,
                "day": day.isoformat(),
                "daily_limit": quota["daily_limit"],
                "used": used,
            },
        )


def _quota_response(conn, row: dict) -> dict:
    """限额配置行 -> API 视图；used/remaining 实时计算（撤销/失效不退还）。"""
    used = _count_daily_used(conn, row["cert_no"], row["day"])
    return {
        "cert_no": row["cert_no"],
        "day": row["day"],
        "daily_limit": row["daily_limit"],
        "used": used,
        "remaining": max(row["daily_limit"] - used, 0),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _set_quota_once(conn, cert_no: str, day: date, daily_limit: int):
    """单次事务尝试设置/调整上限，返回 (视图, 是否首次配置)。

    先锁定目标证书行（与并发签发共用同一把行锁），再 upsert 限额行，
    随后在同一行锁快照下复核已用次数：当日已签发数超过新上限则回滚拒绝。
    """
    with conn.transaction():
        cert = conn.execute(
            "SELECT 1 FROM certificates WHERE cert_no = %s FOR UPDATE",
            (cert_no,),
        ).fetchone()
        if cert is None:
            raise ApiError(404, "cert_not_found", f"证书不存在: {cert_no}")
        existing = conn.execute(
            f"SELECT {_QUOTA_COLS} FROM cert_daily_quotas "
            "WHERE cert_no = %s AND day = %s FOR UPDATE",
            (cert_no, day),
        ).fetchone()
        created = existing is None
        now = datetime.now(timezone.utc)
        if created:
            row = conn.execute(
                """
                INSERT INTO cert_daily_quotas (cert_no, day, daily_limit, created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s)
                RETURNING """ + _QUOTA_COLS,
                (cert_no, day, daily_limit, now, now),
            ).fetchone()
        else:
            row = conn.execute(
                """
                UPDATE cert_daily_quotas
                SET daily_limit = %s, updated_at = %s
                WHERE cert_no = %s AND day = %s
                RETURNING """ + _QUOTA_COLS,
                (daily_limit, now, cert_no, day),
            ).fetchone()
        used = _count_daily_used(conn, cert_no, day)
        if used > daily_limit:
            raise ApiError(
                422,
                "quota_below_usage",
                f"UTC 日期 {day.isoformat()} 已以证书 {cert_no} 为直接上游签发 "
                f"{used} 张普通证书，新上限 {daily_limit} 低于已用次数："
                "撤销或失效不退还已用次数，不能下调到已用次数以下",
                details={
                    "cert_no": cert_no,
                    "day": day.isoformat(),
                    "daily_limit": daily_limit,
                    "used": used,
                },
            )
        return _quota_response(conn, row), created


def set_certificate_daily_quota(
    conn, cert_no: str, day: date, daily_limit: int, management_key: str | None
) -> tuple[dict, bool]:
    """凭管理密钥为证书编号与 UTC 日期设定/调整非负上限，返回 (视图, 是否首次配置)。

    - 管理密钥缺失/错误 401 invalid_management_key；
    - 证书不存在 404 cert_not_found（可为基准或普通证书，任何证书都可作为
      被直接引用的上游）；
    - 当日已签发次数超过新上限时 422 quota_below_usage（撤销/失效不退还，
      故只能上调至不小于已用次数）；
    - 成功响应给出上限与实时已用次数（used/remaining）；
    - 并发调整与同键签发在证书行锁 + 限额行锁上按提交次序串行化，绝不超额；
      理论等待环由死锁检测终止后整体重试（409 quota_busy）。
    """
    _require_management_key(management_key)
    for attempt in range(3):
        try:
            return _set_quota_once(conn, cert_no, day, daily_limit)
        except ApiError:
            raise
        except psycopg.errors.DeadlockDetected:
            if attempt == 2:
                raise ApiError(
                    409,
                    "quota_busy",
                    f"证书 {cert_no} 在 {day.isoformat()} 的限额调整与并发签发竞争，"
                    "重试后仍冲突，请稍后重试",
                    details={"cert_no": cert_no, "day": day.isoformat()},
                )
    raise ApiError(409, "quota_busy", "限额调整失败，请稍后重试")


def get_certificate_daily_quota(
    conn, cert_no: str, day: date, management_key: str | None
) -> dict:
    """凭管理密钥查询某证书×UTC 日期的上限配置与实时已用次数。

    管理密钥缺失/错误 401；证书不存在 404 cert_not_found；
    该键未配置上限 404 quota_not_found（响应仍给出实时已用次数）。
    """
    _require_management_key(management_key)
    if conn.execute(
        "SELECT 1 FROM certificates WHERE cert_no = %s", (cert_no,)
    ).fetchone() is None:
        raise ApiError(404, "cert_not_found", f"证书不存在: {cert_no}")
    row = conn.execute(
        f"SELECT {_QUOTA_COLS} FROM cert_daily_quotas WHERE cert_no = %s AND day = %s",
        (cert_no, day),
    ).fetchone()
    if row is None:
        used = _count_daily_used(conn, cert_no, day)
        raise ApiError(
            404,
            "quota_not_found",
            f"证书 {cert_no} 在 UTC 日期 {day.isoformat()} 未配置每日上限"
            "（当前沿用原签发规则，无上限）",
            details={"cert_no": cert_no, "day": day.isoformat(), "used": used},
        )
    return _quota_response(conn, row)


# ---------------------------------------------------------------------------
# 批次签发（离线采集交接批次：整批原子落库、批次键幂等）
# ---------------------------------------------------------------------------

def _json_default(o):
    if isinstance(o, (datetime, date)):
        return o.isoformat()
    return str(o)


def _json_dumps(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, default=_json_default)


def _canonical_request(req) -> tuple[str, dict]:
    """规范化请求：排序键的紧凑 JSON 及其 SHA-256，用于“同键是否同请求”判定。"""
    doc = req.model_dump(mode="json")
    canonical = json.dumps(doc, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest(), doc


def _get_issue_batch_row(conn, batch_key: str) -> dict | None:
    return conn.execute(
        "SELECT batch_key, request_hash, response, cert_nos, created_at "
        "FROM issue_batches WHERE batch_key = %s",
        (batch_key,),
    ).fetchone()


def _replay_or_conflict(existing: dict, batch_key: str, req_hash: str) -> dict:
    """批次键已存在：同请求 → 重放首次签发结果快照；不同请求 → 409 冲突。"""
    if existing["request_hash"] != req_hash:
        raise ApiError(
            409,
            "batch_conflict",
            f"批次键 {batch_key} 已用于一份内容不同的签发请求；"
            "同一批次键只允许重放完全相同的请求",
            details={"batch_key": batch_key},
        )
    return existing["response"]


def _trace_cycle(by_no: dict, start: str) -> list[str]:
    """沿批内父引用从 start 向上走，返回成环路径（首尾相接）。"""
    seen: dict[str, int] = {}
    path: list[str] = []
    cur = start
    while cur not in seen:
        seen[cur] = len(path)
        path.append(cur)
        nxt = by_no[cur].parent_cert_no
        if nxt not in by_no:  # 理论不可达（未出队节点必有批内父），兜底
            return [start, start]
        cur = nxt
    return path[seen[cur]:] + [cur]


def _topo_sort_batch(items: list) -> list:
    """批内引用拓扑排序（父先子后，同层按编号排序保证确定性）；成环则拒绝。"""
    by_no = {it.cert_no: it for it in items}
    indeg = {it.cert_no: 0 for it in items}
    children: dict[str, list[str]] = {it.cert_no: [] for it in items}
    for it in items:
        if it.kind == "CALIBRATION" and it.parent_cert_no in by_no:
            indeg[it.cert_no] += 1
            children[it.parent_cert_no].append(it.cert_no)
    ready = [no for no, d in indeg.items() if d == 0]
    heapq.heapify(ready)
    ordered = []
    while ready:
        no = heapq.heappop(ready)
        ordered.append(by_no[no])
        for ch in children[no]:
            indeg[ch] -= 1
            if indeg[ch] == 0:
                heapq.heappush(ready, ch)
    if len(ordered) != len(items):
        start = next(no for no, d in indeg.items() if d > 0)
        cycle = _trace_cycle(by_no, start)
        raise ApiError(
            422,
            "reference_cycle",
            f"批内引用关系成环: {' -> '.join(cycle)}",
            details={"cert_no": cycle[0], "cycle": cycle},
        )
    return ordered


def _validate_batch(conn, req) -> list:
    """整批预检：批内编号唯一、不与库内冲突、引用可解析、批内无环。

    返回拓扑排序后的条目列表；任一不合规抛出 ApiError（整批不落库，
    出错编号置于 details.cert_no）。
    """
    items = list(req.certificates)
    # 1) 批内编号唯一
    seen: set[str] = set()
    for it in items:
        if it.cert_no in seen:
            raise ApiError(
                422,
                "batch_cert_duplicate",
                f"批次内证书编号重复: {it.cert_no}",
                details={"cert_no": it.cert_no},
            )
        seen.add(it.cert_no)
    # 2) 不得与库内已有证书编号冲突
    nos = [it.cert_no for it in items]
    existing = {
        r["cert_no"]
        for r in conn.execute(
            "SELECT cert_no FROM certificates WHERE cert_no = ANY(%s)", (nos,)
        ).fetchall()
    }
    for it in items:
        if it.cert_no in existing:
            raise ApiError(
                409,
                "cert_duplicate",
                f"证书编号已存在: {it.cert_no}（整批未签发）",
                details={"cert_no": it.cert_no},
            )
    # 3) 引用解析：普通证书的上游必须在本批或库内
    by_no = {it.cert_no: it for it in items}
    ext_parents = sorted(
        {
            it.parent_cert_no
            for it in items
            if it.kind == "CALIBRATION" and it.parent_cert_no not in by_no
        }
    )
    found = set()
    if ext_parents:
        found = {
            r["cert_no"]
            for r in conn.execute(
                "SELECT cert_no FROM certificates WHERE cert_no = ANY(%s)",
                (ext_parents,),
            ).fetchall()
        }
    for it in items:
        if (
            it.kind == "CALIBRATION"
            and it.parent_cert_no not in by_no
            and it.parent_cert_no not in found
        ):
            raise ApiError(
                422,
                "invalid_reference",
                f"证书 {it.cert_no} 引用的标准证书不存在: {it.parent_cert_no}",
                details={"cert_no": it.cert_no, "parent_cert_no": it.parent_cert_no},
            )
    # 4) 批内引用无环并拓扑排序（列表顺序不代表引用顺序）
    return _topo_sort_batch(items)


def _insert_batch_items(conn, ordered: list) -> None:
    """按拓扑序逐张校验并插入，复用单张签发的全部规则（上游有效性、区间重叠）。

    批内上游先于下游插入，因此下游对“本批证书”的引用与对库内证书的引用
    走完全相同的校验路径。数据库层唯一/排他冲突不在此转换，交由批次级统一
    处理，以便区分“并发同键重放”与“真实冲突”。
    """
    for it in ordered:
        try:
            if it.kind == "CALIBRATION":
                _validate_parent(conn, it.parent_cert_no, it.calibrated_at, it.cert_no)
                # 每张普通证书按 (直接上游, calibrated_at 的 UTC 日) 计一次；
                # 批内多张相同键的证书在此逐张扣额并合并计数（同事务前序插入
                # 对后续计数可见），超限即整批回滚、不落任何证书。
                _assert_daily_quota(
                    conn, it.parent_cert_no, it.calibrated_at, it.cert_no
                )
            _assert_no_overlap(
                conn, it.device_id, it.valid_from, it.valid_to, it.cert_no
            )
        except ApiError as exc:
            exc.details = {**exc.details, "cert_no": it.cert_no}
            raise
        _raw_insert_certificate(
            conn,
            kind=it.kind,
            conclusion="PASS" if it.kind == "REFERENCE" else it.conclusion,
            parent_no=None if it.kind == "REFERENCE" else it.parent_cert_no,
            req=it,
        )


def _build_batch_response(conn, req, issued_at: datetime) -> dict:
    """组装批次结果：批次键 + 各证书及其从基准到自身的追溯链（按请求顺序）。"""
    items = []
    for it in req.certificates:
        items.append(
            {
                "certificate": get_certificate(conn, it.cert_no),
                "chain": get_chain(conn, it.cert_no),
            }
        )
    return {
        "batch_key": req.batch_key,
        "count": len(items),
        "issued_at": issued_at,
        "items": items,
    }


def _batch_cert_duplicate_error(conn, req) -> ApiError:
    """唯一冲突且批次键不存在 → 与并发事务的证书编号冲突，定位冲突编号。"""
    nos = [it.cert_no for it in req.certificates]
    existing = {
        r["cert_no"]
        for r in conn.execute(
            "SELECT cert_no FROM certificates WHERE cert_no = ANY(%s)", (nos,)
        ).fetchall()
    }
    for it in req.certificates:
        if it.cert_no in existing:
            return ApiError(
                409,
                "cert_duplicate",
                f"证书编号已存在: {it.cert_no}（与并发签发冲突，整批未签发）",
                details={"cert_no": it.cert_no},
            )
    return ApiError(
        409, "cert_duplicate", f"批次 {req.batch_key} 与并发签发冲突，整批未签发"
    )


def _batch_overlap_error(conn, req) -> ApiError:
    """排他约束兜底：与并发事务（单张签发或另一批次）在同设备有效期上重叠。"""
    for it in req.certificates:
        row = conn.execute(
            """
            SELECT cert_no FROM certificates
            WHERE device_id = %s AND status = 'ACTIVE'
              AND tstzrange(valid_from, valid_to, '[)') && tstzrange(%s, %s, '[)')
            LIMIT 1
            """,
            (it.device_id, _as_utc(it.valid_from), _as_utc(it.valid_to)),
        ).fetchone()
        if row:
            return ApiError(
                409,
                "validity_overlap_concurrent",
                f"设备 {it.device_id} 已生效证书 {row['cert_no']} 与证书 {it.cert_no} "
                "的有效期重叠（并发签发冲突，整批未签发）",
                details={"cert_no": it.cert_no, "conflict_cert_no": row["cert_no"]},
            )
    return ApiError(
        409,
        "validity_overlap_concurrent",
        f"批次 {req.batch_key} 与并发签发在有效期上重叠，整批未签发",
    )


def issue_batch(conn, req) -> tuple[dict, bool]:
    """批量签发交接批次，返回 (响应体, 是否首次签发)。

    - 整批原子：任一记录不合规 → 全部回滚，响应以 details.cert_no 定位出错编号及原因；
    - 批次键幂等：同键同请求 → 重放首次签发结果快照（即使证书事后被撤销）；
      同键不同请求 → 409 batch_conflict；
    - 并发同键提交由 issue_batches 主键兜底：后到事务回滚后按已提交结果重放/判冲突；
    - 与单张签发、撤销并发时，沿用同一套行锁、唯一约束与排他约束，规则一致。
    """
    req_hash, request_doc = _canonical_request(req)
    for attempt in range(3):
        try:
            return _issue_batch_once(conn, req, req_hash, request_doc)
        except psycopg.errors.DeadlockDetected:
            # 与并发停用/解除/撤销在引用链上的加锁等待环由死锁检测终止，整体重试
            if attempt == 2:
                raise ApiError(
                    409,
                    "issuance_busy",
                    f"批次 {req.batch_key} 的签发与并发停用/撤销竞争，重试后仍冲突，"
                    "整批未签发，请稍后重试",
                    details={"batch_key": req.batch_key},
                )
        except psycopg.errors.UniqueViolation:
            # 事务已整体回滚：并发同键提交，或与并发事务的证书编号冲突
            existing = _get_issue_batch_row(conn, req.batch_key)
            if existing is not None:
                return _replay_or_conflict(existing, req.batch_key, req_hash), False
            raise _batch_cert_duplicate_error(conn, req)
        except psycopg.errors.ExclusionViolation:
            raise _batch_overlap_error(conn, req)
        except psycopg.errors.ForeignKeyViolation:
            raise ApiError(
                422, "invalid_reference", "引用的标准证书不存在（并发变更），整批未签发"
            )
        except psycopg.errors.CheckViolation as exc:
            raise ApiError(422, "rule_violation", f"签发被数据库完整性规则拒绝: {exc}")
    raise ApiError(409, "issuance_busy", f"批次 {req.batch_key} 签发重试耗尽，整批未签发")


def _issue_batch_once(conn, req, req_hash: str, request_doc: dict) -> tuple[dict, bool]:
    with conn.transaction():
        existing = _get_issue_batch_row(conn, req.batch_key)
        if existing is not None:
            return _replay_or_conflict(existing, req.batch_key, req_hash), False
        ordered = _validate_batch(conn, req)
        _insert_batch_items(conn, ordered)
        issued_at = datetime.now(timezone.utc)
        response = _build_batch_response(conn, req, issued_at)
        conn.execute(
            """
            INSERT INTO issue_batches
                (batch_key, request_hash, request, response, cert_nos, created_at)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (
                req.batch_key,
                req_hash,
                Jsonb(request_doc),
                Jsonb(response, dumps=_json_dumps),
                [it.cert_no for it in req.certificates],
                issued_at,
            ),
        )
        return response, True


def get_issue_batch(conn, batch_key: str) -> dict:
    """按批次键查询首次签发结果（与签发响应同构的快照）。"""
    row = _get_issue_batch_row(conn, batch_key)
    if row is None:
        raise ApiError(404, "batch_not_found", f"签发批次不存在: {batch_key}")
    return row["response"]


# ---------------------------------------------------------------------------
# 更正声明（展示字段纠错：设备名称 / 操作员 / 备注）
# ---------------------------------------------------------------------------

# 仅允许更正的展示字段及其长度上限（与签发模型一致）
CORRECTABLE_FIELDS = ("device_name", "operator", "remark")
_CORRECTION_MAX_LEN = {"device_name": 256, "operator": 128, "remark": 1024}

_DECL_COLS = "declaration_no, cert_no, seq, corrections, reason, created_at"


def _validate_correction_request(req) -> dict:
    """校验修正字段集合与更正原因；返回提交原样的 corrections（区分显式置空与未提交）。"""
    corrections = dict(req.corrections)
    if not corrections:
        raise ApiError(
            422, "correction_empty",
            "修正字段集合不能为空：至少提交一个待更正的展示字段",
        )
    unknown = sorted(set(corrections) - set(CORRECTABLE_FIELDS))
    if unknown:
        raise ApiError(
            422, "correction_field_not_allowed",
            f"仅允许更正展示字段 {', '.join(CORRECTABLE_FIELDS)}；"
            f"收到不允许更正的字段: {', '.join(unknown)}",
            details={"fields": unknown},
        )
    for field, value in corrections.items():
        if value is not None and len(value) > _CORRECTION_MAX_LEN[field]:
            raise ApiError(
                422, "correction_field_invalid",
                f"字段 {field} 长度超出上限 {_CORRECTION_MAX_LEN[field]} 字符",
                details={"field": field, "max_length": _CORRECTION_MAX_LEN[field]},
            )
    if not req.reason or not req.reason.strip():
        raise ApiError(422, "correction_reason_required", "更正原因不能为空")
    return corrections


def _correction_request_hash(cert_no: str, corrections: dict, reason: str) -> str:
    """规范化请求指纹：键序无关；显式置空（null）与未提交该字段是不同的请求。"""
    canonical = json.dumps(
        {"cert_no": cert_no, "corrections": corrections, "reason": reason},
        sort_keys=True, ensure_ascii=False, separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _get_declaration_row(conn, declaration_no: str, with_hash: bool = False) -> dict | None:
    cols = _DECL_COLS + (", request_hash" if with_hash else "")
    return conn.execute(
        f"SELECT {cols} FROM correction_declarations WHERE declaration_no = %s",
        (declaration_no,),
    ).fetchone()


def _merged_display(certificate: dict, declarations: list[dict]) -> dict:
    """按登记顺序（seq 升序）将声明合并到原证书的展示字段上。

    同一字段后登记的声明覆盖先登记的；值为 None 表示显式置空，
    未提交的字段不参与合并。
    """
    display = {field: certificate[field] for field in CORRECTABLE_FIELDS}
    for decl in declarations:
        display.update(decl["corrections"])
    return display


def _correction_view(conn, cert_no: str) -> dict:
    """证书的更正视图：原证书 + 按登记顺序排列的声明历史 + 合并后的当前展示。"""
    certificate = get_certificate(conn, cert_no)
    declarations = conn.execute(
        f"SELECT {_DECL_COLS} FROM correction_declarations "
        "WHERE cert_no = %s ORDER BY seq ASC",
        (cert_no,),
    ).fetchall()
    return {
        "certificate": certificate,
        "declarations": declarations,
        "display": _merged_display(certificate, declarations),
    }


def _correction_response(conn, existing_row: dict, req_hash: str) -> dict:
    """声明编号已登记：同内容 → 返回首次声明及当前更正视图；异内容 → 409。"""
    row = dict(existing_row)
    row.pop("request_hash")
    if existing_row["request_hash"] != req_hash:
        raise ApiError(
            409, "correction_conflict",
            f"声明编号 {row['declaration_no']} 已用于一份内容不同的更正声明；"
            "同一声明编号只允许重放完全相同的请求",
            details={"declaration_no": row["declaration_no"]},
        )
    return {"declaration": row, **_correction_view(conn, row["cert_no"])}


def register_correction(conn, req) -> tuple[dict, bool]:
    """登记更正声明，返回 (响应体, 是否首次登记)。

    - 仅 device_name / operator / remark 可更正；修正集合非空、原因非空；
    - 已撤销（REVOKED）/已失效（INVALID）证书同样可更正展示资料，
      状态、有效期与签发/撤销判定不受影响（不触碰 certificates 表）；
    - 声明编号幂等：同编号同内容重试返回首次声明（不重复登记），
      同编号不同内容返回 409 correction_conflict；
    - 并发安全：登记前对被更正证书行加 FOR UPDATE 锁，同一证书的并发更正
      在此串行化，seq 按成功登记顺序分配，不丢失、不乱序；不同证书的相同
      声明编号并发提交由主键兜底，后到事务回滚后按已提交内容重放/判冲突。
    """
    corrections = _validate_correction_request(req)
    req_hash = _correction_request_hash(req.cert_no, corrections, req.reason)
    try:
        with conn.transaction():
            locked = conn.execute(
                "SELECT cert_no FROM certificates WHERE cert_no = %s FOR UPDATE",
                (req.cert_no,),
            ).fetchone()
            if locked is None:
                raise ApiError(404, "cert_not_found", f"证书不存在: {req.cert_no}")

            existing = _get_declaration_row(conn, req.declaration_no, with_hash=True)
            if existing is not None:
                return _correction_response(conn, existing, req_hash), False

            seq = conn.execute(
                "SELECT COALESCE(MAX(seq), 0) + 1 AS next_seq "
                "FROM correction_declarations WHERE cert_no = %s",
                (req.cert_no,),
            ).fetchone()["next_seq"]
            conn.execute(
                """
                INSERT INTO correction_declarations
                    (declaration_no, cert_no, seq, corrections, reason, request_hash)
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (
                    req.declaration_no, req.cert_no, seq,
                    Jsonb(corrections), req.reason, req_hash,
                ),
            )
            declaration = _get_declaration_row(conn, req.declaration_no)
    except psycopg.errors.UniqueViolation:
        # 并发同声明编号（多见于不同证书并行提交）：主键兜底，按已提交内容重放或判冲突
        existing = _get_declaration_row(conn, req.declaration_no, with_hash=True)
        if existing is not None:
            return _correction_response(conn, existing, req_hash), False
        raise
    return {"declaration": declaration, **_correction_view(conn, req.cert_no)}, True


def get_certificate_corrections(conn, cert_no: str) -> dict:
    """按证书查询更正视图：原证书、声明历史（按登记顺序）与合并后的当前展示。"""
    return {"cert_no": cert_no, **_correction_view(conn, cert_no)}


def get_correction_declaration(conn, declaration_no: str) -> dict:
    """按声明编号查询：首次登记的声明 + 所属证书的当前更正视图。"""
    row = _get_declaration_row(conn, declaration_no)
    if row is None:
        raise ApiError(404, "declaration_not_found", f"更正声明不存在: {declaration_no}")
    return {"declaration": row, **_correction_view(conn, row["cert_no"])}


# ---------------------------------------------------------------------------
# 撤销与级联失效
# ---------------------------------------------------------------------------

def _gather_active_descendants(conn, root_no: str) -> list[dict]:
    """找出所有状态仍为 ACTIVE 的下游证书。

    path 为“相对被撤销证书”的路径（不含 root 本身），
    完整追溯链在写入快照时与 root 自身的上游链拼接得到。
    """
    sql = """
        WITH RECURSIVE d AS (
            SELECT cert_no, 1 AS depth, ARRAY[cert_no] AS path
            FROM certificates
            WHERE parent_cert_no = %s AND status = 'ACTIVE'
            UNION ALL
            SELECT c.cert_no, d.depth + 1, d.path || c.cert_no
            FROM certificates c JOIN d ON c.parent_cert_no = d.cert_no
            WHERE c.status = 'ACTIVE'
        )
        CYCLE cert_no SET is_cycle USING cycle_path
        SELECT cert_no, depth, path FROM d
        WHERE NOT is_cycle
    """
    return conn.execute(sql, (root_no,)).fetchall()


def _root_prefix_chain(conn, root_no: str) -> list[str]:
    """被撤销证书自身的链：从追溯起点（基准）到该证书，含两端。"""
    rows = conn.execute(
        """
        WITH RECURSIVE up AS (
            SELECT cert_no, parent_cert_no, 0 AS depth
            FROM certificates WHERE cert_no = %s
            UNION ALL
            SELECT c.cert_no, c.parent_cert_no, up.depth - 1
            FROM certificates c JOIN up ON c.cert_no = up.parent_cert_no
        )
        SELECT cert_no FROM up ORDER BY depth ASC
        """,
        (root_no,),
    ).fetchall()
    return [r["cert_no"] for r in rows]


def revoke_certificate(conn, cert_no: str, reason: str, operator: str | None) -> dict:
    """主动撤销一张生效证书，并级联失效其全部下游证书。

    并发正确性：
      1. 先对被撤销证书加 FOR UPDATE 行锁；新插入的下游证书在外键检查时需获取
         该行的 FOR KEY SHARE 锁，会被阻塞到本事务提交之后；
      2. 递归收集下游并按 cert_no 排序加行锁后再次收集，直到集合不再扩大，
         覆盖“撤销进行中恰有下游证书提交”的竞态；
      3. 所有 UPDATE/INSERT 在同一事务中提交。
    """
    with conn.transaction():
        root = _get_cert_row(conn, cert_no, lock=True)
        if root is None:
            raise ApiError(404, "cert_not_found", f"证书不存在: {cert_no}")
        if root["status"] != "ACTIVE":
            raise ApiError(
                409,
                "cert_not_active",
                f"证书 {cert_no} 当前状态为 {root['status']}，"
                "仅生效中的证书可被撤销（已失效证书不能重复撤销）",
            )

        locked: set[str] = set()
        descendants: list[dict] = []
        # 循环直到“收集结果”与“已锁定集合”一致，保证不会漏掉新提交的下游
        while True:
            rows = _gather_active_descendants(conn, cert_no)
            fresh = [r for r in rows if r["cert_no"] not in locked]
            if not fresh:
                descendants = rows
                break
            nos = sorted(r["cert_no"] for r in fresh)
            conn.execute(
                "SELECT cert_no FROM certificates "
                "WHERE cert_no = ANY(%s) ORDER BY cert_no FOR UPDATE",
                (nos,),
            )
            locked.update(nos)

        batch = conn.execute(
            "INSERT INTO revocations (cert_no, reason, operator) "
            "VALUES (%s, %s, %s) RETURNING batch_id, revoked_at",
            (cert_no, reason, operator),
        ).fetchone()

        # 被撤销证书本身
        conn.execute(
            """
            UPDATE certificates
            SET status = 'REVOKED', revoked_reason = %s, revoked_at = %s
            WHERE cert_no = %s
            """,
            (reason, batch["revoked_at"], cert_no),
        )

        if descendants:
            # 完整引用链快照：追溯起点 -> 被撤销证书 -> 下游证书
            prefix = _root_prefix_chain(conn, cert_no)

            # 全部下游证书级联失效
            conn.execute(
                """
                UPDATE certificates AS c SET
                    status = 'INVALID',
                    revoked_reason = %s,
                    revoked_at = %s
                FROM unnest(%s::text[]) AS d(cert_no)
                WHERE c.cert_no = d.cert_no
                """,
                (
                    f"上游证书 {cert_no} 已撤销，引用链级联失效",
                    batch["revoked_at"],
                    [r["cert_no"] for r in descendants],
                ),
            )
            # 保存不可改写的失效明细与引用链快照
            full_chains = [(prefix, r) for r in descendants]
            with conn.cursor() as cur:
                cur.executemany(
                    """
                    INSERT INTO invalidations (batch_id, cert_no, chain, position)
                    VALUES (%s, %s, %s, %s)
                    """,
                    [
                        (
                            batch["batch_id"],
                            r["cert_no"],
                            [*prefix, *r["path"]],
                            len(prefix) + len(r["path"]),
                        )
                        for prefix, r in full_chains
                    ],
                )

        # 已定稿 ACCEPT 且受本批撤销波及的交付归档包：各生成一份撤销风险告知
        # （同事务、按归档冻结链判定；并发 ACCEPT 与本撤销按提交次序串行化，
        # 签收先提交则此处读到定稿并生成告知，撤销先提交则 ACCEPT 随后被拒、
        # 不产生定稿，自然不生成告知）。
        _generate_revocation_notices(conn, batch["batch_id"])

    # 事务提交后组装返回内容
    revoked = get_certificate(conn, cert_no)
    affected = _build_invalidated_items(
        conn, batch["batch_id"], batch["revoked_at"], cert_no
    )
    return {
        "batch_id": batch["batch_id"],
        "revoked": revoked,
        "reason": reason,
        "operator": operator,
        "revoked_at": batch["revoked_at"],
        "affected_count": len(affected),
        "affected": affected,
    }


def _build_invalidated_items(conn, batch_id, revoked_at, root_no: str) -> list[dict]:
    rows = conn.execute(
        "SELECT cert_no, chain FROM invalidations WHERE batch_id = %s "
        "ORDER BY position, cert_no",
        (batch_id,),
    ).fetchall()
    if not rows:
        return []
    all_nos = {no for r in rows for no in r["chain"]}
    cert_map = {
        c["cert_no"]: c
        for c in conn.execute(
            f"SELECT {_VIEW_COLS} FROM v_certificates WHERE cert_no = ANY(%s)",
            (list(all_nos),),
        ).fetchall()
    }
    items = []
    for r in rows:
        chain = [cert_map[no] for no in r["chain"] if no in cert_map]
        items.append(
            {
                "cert_no": r["cert_no"],
                "chain": chain,
                # 深度按链上相对被撤销证书的层数计
                "depth": len(r["chain"]) - 1,
            }
        )
    return items


def get_revocation(conn, batch_id: int) -> dict:
    head = conn.execute(
        """
        SELECT r.batch_id, r.cert_no, r.reason, r.operator, r.revoked_at,
               (SELECT count(*) FROM invalidations i WHERE i.batch_id = r.batch_id) AS affected_count
        FROM revocations r WHERE r.batch_id = %s
        """,
        (batch_id,),
    ).fetchone()
    if head is None:
        raise ApiError(404, "revocation_not_found", f"撤销批次不存在: {batch_id}")
    cert_no = head.pop("cert_no")
    return {
        **head,
        "revoked": get_certificate(conn, cert_no),
        "affected": _build_invalidated_items(conn, batch_id, head["revoked_at"], cert_no),
    }


def list_revocations(conn, limit: int, offset: int) -> dict:
    total = conn.execute("SELECT count(*) AS n FROM revocations").fetchone()["n"]
    items = conn.execute(
        """
        SELECT r.batch_id, r.cert_no, r.reason, r.operator, r.revoked_at,
               (SELECT count(*) FROM invalidations i WHERE i.batch_id = r.batch_id) AS affected_count
        FROM revocations r
        ORDER BY r.batch_id DESC
        LIMIT %s OFFSET %s
        """,
        (limit, offset),
    ).fetchall()
    return {"items": items, "total": total, "limit": limit, "offset": offset}


# ---------------------------------------------------------------------------
# 客户交付归档包（冻结创建时刻的可交付追溯凭据：原始签发内容 + 追溯链 + 合并展示）
# ---------------------------------------------------------------------------

def _delivery_request_hash(cert_nos: list[str]) -> tuple[str, dict]:
    """规范化归档请求：有序列表原样参与指纹（顺序不同即不同请求）。"""
    doc = {"cert_nos": list(cert_nos)}
    canonical = json.dumps(doc, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest(), doc


def _get_delivery_archive_row(conn, archive_no: str) -> dict | None:
    return conn.execute(
        "SELECT archive_no, request_hash, request, response, cert_nos, created_at "
        "FROM delivery_archives WHERE archive_no = %s",
        (archive_no,),
    ).fetchone()


def _delivery_replay_or_conflict(existing: dict, archive_no: str, req_hash: str) -> dict:
    """归档包编号已存在：同有序列表 → 重放首次快照；不同列表 → 409 冲突。"""
    if existing["request_hash"] != req_hash:
        raise ApiError(
            409,
            "archive_conflict",
            f"归档包编号 {archive_no} 已用于一份证书列表不同的交付请求；"
            "同一归档包编号只允许重放完全相同的有序列表",
            details={"archive_no": archive_no},
        )
    return existing["response"]


def _lock_delivery_universe(conn, cert_nos: list[str]) -> None:
    """锁定目标证书及其全部上游链（共享锁），保证整包对应同一个业务时点。

    加锁顺序按“从追溯起点到下游”的深度升序、同深度按编号升序——与撤销事务
    （被撤销证书先于下游、且总是祖先先于后代）及更正事务（仅锁目标行）的
    加锁顺序一致或为其链上的一段，因此与并发撤销/更正之间不会形成等待环：
    撤销/更正事务在本事务释放锁前无法提交，锁后的再读取看到的是统一状态，
    快照不会混合新旧状态。FOR SHARE 不阻塞签发事务的外键检查（FOR KEY
    SHARE 与之兼容），只阻塞会改状态/封印字段的撤销事务（FOR UPDATE）。
    """
    sql = """
        WITH RECURSIVE targets AS (
            SELECT DISTINCT cert_no
            FROM unnest(%s::text[]) AS t(cert_no)
        ),
        walk AS (
            SELECT cert_no, parent_cert_no, 0 AS dist
            FROM certificates
            WHERE cert_no IN (SELECT cert_no FROM targets)
            UNION ALL
            SELECT c.cert_no, c.parent_cert_no, w.dist + 1
            FROM certificates c JOIN walk w ON c.cert_no = w.parent_cert_no
        ),
        universe AS (
            SELECT cert_no, MIN(dist) AS dist
            FROM walk GROUP BY cert_no
        )
        SELECT c.cert_no
        FROM certificates c
        JOIN universe u ON u.cert_no = c.cert_no
        ORDER BY u.dist DESC, c.cert_no ASC
        FOR SHARE OF c
    """
    conn.execute(sql, (cert_nos,)).fetchall()


def _evaluate_delivery_targets(conn, cert_nos: list[str]) -> dict:
    """在统一锁定后校验每张目标证书：存在、CALIBRATION、自身与全链 ACTIVE/PASS。

    汇总全部不达标编号（整包拒绝时一次性指出），各分类按编号排序。
    """
    rows = {
        r["cert_no"]: r
        for r in conn.execute(
            f"SELECT {_VIEW_COLS} FROM v_certificates WHERE cert_no = ANY(%s)",
            (cert_nos,),
        ).fetchall()
    }
    missing, wrong_kind, not_active, not_qualified, chain_bad = [], [], [], [], []
    for no in cert_nos:
        row = rows.get(no)
        if row is None:
            missing.append(no)
            continue
        if row["kind"] != "CALIBRATION":
            # 基准证书不是可交付的“普通校准证书”，整包层面按目标不合格指出
            wrong_kind.append(no)
        if row["status"] != "ACTIVE":
            not_active.append(no)
        if row["conclusion"] != "PASS":
            not_qualified.append(no)

    # 仅对“存在且类型正确”的目标检查其完整上游链
    chain_sql = """
        WITH RECURSIVE walk AS (
            SELECT cert_no, parent_cert_no, 0 AS depth
            FROM certificates WHERE cert_no = %s
            UNION ALL
            SELECT c.cert_no, c.parent_cert_no, w.depth - 1
            FROM certificates c JOIN walk w ON c.cert_no = w.parent_cert_no
        )
        CYCLE cert_no SET is_cycle USING cycle_path
        SELECT vc.cert_no, vc.status, vc.conclusion
        FROM walk w JOIN v_certificates vc ON vc.cert_no = w.cert_no
        WHERE NOT w.is_cycle AND w.depth < 0
    """
    for no in cert_nos:
        row = rows.get(no)
        if row is None or row["kind"] != "CALIBRATION":
            continue
        bad_nodes = [
            r["cert_no"]
            for r in conn.execute(chain_sql, (no,)).fetchall()
            if r["status"] != "ACTIVE" or r["conclusion"] != "PASS"
        ]
        if bad_nodes:
            chain_bad.append(no)

    problems: set[str] = set(missing) | set(wrong_kind) | set(not_active)
    problems |= set(not_qualified) | set(chain_bad)
    return {
        "missing": sorted(missing),
        "wrong_kind": sorted(wrong_kind),
        "target_not_active": sorted(not_active),
        "target_not_qualified": sorted(not_qualified),
        "chain_not_eligible": sorted(chain_bad),
        "all": sorted(problems),
    }


def _not_deliverable_error(req, problems: dict) -> ApiError:
    categories = {
        "missing": problems["missing"],
        "not_calibration": problems["wrong_kind"],
        "target_not_active": problems["target_not_active"],
        "target_not_qualified": problems["target_not_qualified"],
        "chain_not_eligible": problems["chain_not_eligible"],
    }
    return ApiError(
        422,
        "archive_not_deliverable",
        "存在不存在或不满足交付条件的目标证书：仅接受目标均为 CALIBRATION，"
        "且目标及各自完整上游链均处于 ACTIVE、结论 PASS 的组合；整包未保存。"
        f"相关编号: {', '.join(problems['all'])}",
        details={"archive_no": req.archive_no, "cert_nos": problems["all"], **categories},
    )


def _build_delivery_response(conn, req, created_at: datetime) -> dict:
    """组装创建时刻快照：按提交顺序给出原始签发内容、追溯链与合并展示。"""
    items = []
    for no in req.cert_nos:
        certificate = get_certificate(conn, no)
        chain = get_chain(conn, no)
        declarations = conn.execute(
            f"SELECT {_DECL_COLS} FROM correction_declarations "
            "WHERE cert_no = %s ORDER BY seq ASC",
            (no,),
        ).fetchall()
        items.append({
            "cert_no": no,
            "certificate": certificate,
            "chain": chain,
            "display": _merged_display(certificate, declarations),
        })
    return {
        "archive_no": req.archive_no,
        "count": len(items),
        "created_at": created_at,
        "items": items,
    }


def _create_delivery_archive_once(conn, req, req_hash: str, request_doc: dict):
    """单次事务尝试；返回 (响应体, 是否首次创建)。唯一冲突交由外层处理。"""
    with conn.transaction():
        existing = _get_delivery_archive_row(conn, req.archive_no)
        if existing is not None:
            return _delivery_replay_or_conflict(existing, req.archive_no, req_hash), False
        _lock_delivery_universe(conn, req.cert_nos)
        problems = _evaluate_delivery_targets(conn, req.cert_nos)
        if problems["all"]:
            raise _not_deliverable_error(req, problems)
        created_at = datetime.now(timezone.utc)
        response = _build_delivery_response(conn, req, created_at)
        conn.execute(
            """
            INSERT INTO delivery_archives
                (archive_no, request_hash, request, response, cert_nos, created_at)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (
                req.archive_no,
                req_hash,
                Jsonb(request_doc),
                Jsonb(response, dumps=_json_dumps),
                list(req.cert_nos),
                created_at,
            ),
        )
        return response, True


def create_delivery_archive(conn, req) -> tuple[dict, bool]:
    """冻结客户交付凭据，返回 (响应体, 是否首次创建)。

    - 整包条件：目标编号互不重复、均为 CALIBRATION，且目标及各自完整上游链
      均 ACTIVE、结论 PASS；任一不满足整包不保存，错误详情分类列出相关编号；
    - 一致时点：目标与全部上游行在同一事务内按祖先优先顺序加 FOR SHARE 锁，
      并发撤销（FOR UPDATE）与并发更正（目标行 FOR UPDATE）要么先于本包提交
      （本包按新状态校验/合并），要么被阻塞到本包提交后（本包按旧状态快照），
      整包不混合新旧状态；
    - 幂等：同编号同有序列表重试返回首次快照（200，即使事后更正或撤销），
      同编号不同列表返回 409 archive_conflict；并发同编号由主键兜底，
      后到事务回滚后按已提交结果重放/判冲突；
    - 与撤销事务理论上的加锁等待环由死锁检测兜底，回滚后整体重试。
    """
    seen: set[str] = set()
    duplicates: set[str] = set()
    for no in req.cert_nos:
        (duplicates if no in seen else seen).add(no)
    if duplicates:
        raise ApiError(
            422,
            "archive_cert_duplicate",
            f"归档包内证书编号必须互不重复，重复编号: {', '.join(sorted(duplicates))}",
            details={"archive_no": req.archive_no, "cert_nos": sorted(duplicates)},
        )
    req_hash, request_doc = _delivery_request_hash(req.cert_nos)
    for attempt in range(3):
        try:
            return _create_delivery_archive_once(conn, req, req_hash, request_doc)
        except psycopg.errors.DeadlockDetected:
            # 与并发撤销/更正理论上的加锁等待环由死锁检测终止：整体回滚后重试
            if attempt == 2:
                raise ApiError(
                    409,
                    "archive_busy",
                    f"归档包 {req.archive_no} 与并发撤销/更正竞争，重试后仍冲突，请稍后重试",
                    details={"archive_no": req.archive_no},
                )
        except psycopg.errors.UniqueViolation:
            # 并发同编号提交：主键兜底，后到事务回滚后按已提交结果重放/判冲突
            existing = _get_delivery_archive_row(conn, req.archive_no)
            if existing is not None:
                return _delivery_replay_or_conflict(existing, req.archive_no, req_hash), False
            raise


def get_delivery_archive(conn, archive_no: str) -> dict:
    """按归档包编号查询创建时刻快照；未知编号报不存在。"""
    row = _get_delivery_archive_row(conn, archive_no)
    if row is None:
        raise ApiError(404, "archive_not_found", f"归档包不存在: {archive_no}")
    return row["response"]


# ---------------------------------------------------------------------------
# 交付签收：邀请（一次性随机密钥）与定稿记录（每包唯一）
# ---------------------------------------------------------------------------

_INVITATION_NO_PREFIX = "DINV"
_RECEIPT_KEY_BYTES = 24  # 192 位一次性随机密钥


def _require_management_key(provided: str | None) -> None:
    """校验交付方管理密钥（本地配置、不落库）。恒定时间比较，避免计时侧信道。"""
    if not provided or not hmac.compare_digest(provided, settings.delivery_management_key):
        raise ApiError(
            401,
            "invalid_management_key",
            "管理密钥缺失或错误：创建签收邀请、查询定稿记录须在 X-Management-Key 头"
            "中携带本地配置的交付管理密钥",
        )


def _hash_receipt_key(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def _as_utc(value: datetime) -> datetime:
    """无时区时间按 UTC 处理（与 schemas 文档约定一致），统一为带时区 UTC。"""
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _get_invitation_row(conn, invitation_no: str) -> dict | None:
    return conn.execute(
        """
        SELECT invitation_no, archive_no, recipient, deadline, key_hash, created_at
        FROM delivery_invitations WHERE invitation_no = %s
        """,
        (invitation_no,),
    ).fetchone()


def _receipt_row(row: dict) -> dict:
    return {
        "archive_no": row["archive_no"],
        "invitation_no": row["invitation_no"],
        "recipient": row["recipient"],
        "decision": row["decision"],
        "reason": row["reason"],
        "signed_at": row["signed_at"],
    }


def _get_receipt_by_archive(conn, archive_no: str) -> dict | None:
    row = conn.execute(
        """
        SELECT archive_no, invitation_no, recipient, decision, reason, signed_at
        FROM delivery_receipts WHERE archive_no = %s
        """,
        (archive_no,),
    ).fetchone()
    return _receipt_row(row) if row is not None else None


def create_delivery_invitation(conn, req, management_key: str | None) -> tuple[dict, bool]:
    """创建交付签收邀请，返回 (响应体, 是否首次创建)。

    - 先验管理密钥（401 invalid_management_key），再验归档包存在
      （404 archive_not_found）、截止时间为未来值（422 invitation_deadline_past）
      与收件人非空白（422 invitation_recipient_required）；
    - 服务端生成邀请编号与一次性随机密钥（secrets，192 位）：明文仅出现在
      本次响应中，库内仅存 SHA-256（key_hash），明文不持久化、不记日志；
    - 同一归档包可创建多份邀请；邀请一经创建只增不改；
    - 邀请编号极小概率并发碰撞时由主键兜底重试生成新编号。
    """
    _require_management_key(management_key)
    recipient = req.recipient.strip()
    if not recipient:
        raise ApiError(422, "invitation_recipient_required", "收件人不能为空")
    deadline = _as_utc(req.deadline)
    if not deadline > datetime.now(timezone.utc):
        raise ApiError(
            422,
            "invitation_deadline_past",
            "签收截止时间必须是未来时刻（须晚于当前时刻）",
            details={"archive_no": req.archive_no, "deadline": deadline.isoformat()},
        )
    for _ in range(5):
        invitation_no = f"{_INVITATION_NO_PREFIX}-{secrets.token_hex(8)}"
        key = secrets.token_urlsafe(_RECEIPT_KEY_BYTES)
        with conn.transaction():
            if _get_delivery_archive_row(conn, req.archive_no) is None:
                raise ApiError(
                    404, "archive_not_found", f"归档包不存在: {req.archive_no}"
                )
            try:
                row = conn.execute(
                    """
                    INSERT INTO delivery_invitations
                        (invitation_no, archive_no, recipient, deadline, key_hash)
                    VALUES (%s, %s, %s, %s, %s)
                    RETURNING created_at
                    """,
                    (
                        invitation_no,
                        req.archive_no,
                        recipient,
                        deadline,
                        _hash_receipt_key(key),
                    ),
                ).fetchone()
            except psycopg.errors.UniqueViolation:
                # 邀请编号极小概率碰撞：换一组新编号/密钥重试
                continue
            return {
                "invitation_no": invitation_no,
                "archive_no": req.archive_no,
                "recipient": recipient,
                "deadline": deadline,
                "created_at": row["created_at"],
                "key": key,
            }, True
    raise ApiError(500, "invitation_busy", "邀请编号生成连续碰撞，请稍后重试")


def _receipt_request_hash(invitation_no: str, decision: str, reason: str | None) -> str:
    """规范化提交指纹：同一邀请下区分结论与拒收原因；密钥不参与内容判定。"""
    canonical = json.dumps(
        {"invitation_no": invitation_no, "decision": decision, "reason": reason},
        sort_keys=True, ensure_ascii=False, separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _receipt_blockers(conn, archive: dict) -> list[str]:
    """ACCEPT 前置校验：包内任一目标自身、或其归档冻结追溯链中的任一证书
    当前已撤销（REVOKED）或失效（INVALID），即返回受影响的包内目标编号。

    状态读取的是当前 certificates 表（非快照）；链拓扑取归档快照，因此
    “归档时合格、签收前链上证书被撤销/级联失效”的目标会被点名。
    结果按目标编号排序去重。
    """
    targets: dict[str, set[str]] = {}
    for item in archive["response"]["items"]:
        no = item["cert_no"]
        targets[no] = {
            node["certificate"]["cert_no"] for node in item["chain"]["nodes"]
        }
        # 防御：保证目标自身恒在待查集合中（链按定义含目标，这里兜底）
        targets[no].add(no)
    universe = sorted({c for chain in targets.values() for c in chain})
    bad = {
        r["cert_no"]
        for r in conn.execute(
            "SELECT cert_no FROM certificates WHERE cert_no = ANY(%s) AND status <> 'ACTIVE'",
            (universe,),
        ).fetchall()
    }
    return sorted(no for no, chain in targets.items() if chain & bad)


def _receipt_conflict(existing: dict, invitation_no: str, req_hash: str) -> dict:
    """已定稿：同一邀请且同内容 → 重放首次定稿；否则 409 receipt_conflict。"""
    if (
        existing["invitation_no"] == invitation_no
        and existing.get("request_hash") == req_hash
    ):
        return _receipt_row(existing)
    raise ApiError(
        409,
        "receipt_conflict",
        f"归档包 {existing['archive_no']} 已有定稿记录（邀请 "
        f"{existing['invitation_no']}，结论 {existing['decision']}）："
        "每个归档包只允许一份定稿；仅同一邀请同一内容的重试返回首次结果，"
        "不同内容或其他邀请再提交一律冲突。",
        details={
            "archive_no": existing["archive_no"],
            "finalized_invitation_no": existing["invitation_no"],
            "finalized_decision": existing["decision"],
        },
    )


def _submit_receipt_once(
    conn, req, req_hash: str, *, key_hash: str, now: datetime
) -> tuple[dict, bool]:
    """单次事务尝试；返回 (定稿记录, 是否首次定稿)。"""
    with conn.transaction():
        invitation = _get_invitation_row(conn, req.invitation_no)
        if invitation is None:
            raise ApiError(
                404, "invitation_not_found", f"签收邀请不存在: {req.invitation_no}"
            )
        if not hmac.compare_digest(invitation["key_hash"], key_hash):
            raise ApiError(
                401,
                "invalid_receipt_key",
                f"一次性签收密钥错误（邀请 {req.invitation_no}）",
                details={"invitation_no": req.invitation_no},
            )

        # 先看是否已定稿：同邀请同内容（含截止后重放）原样返回首次结果
        existing = conn.execute(
            """
            SELECT archive_no, invitation_no, recipient, decision, reason,
                   signed_at, request_hash
            FROM delivery_receipts WHERE archive_no = %s
            """,
            (invitation["archive_no"],),
        ).fetchone()
        if existing is not None:
            return _receipt_conflict(existing, req.invitation_no, req_hash), False

        # 未定稿：截止（含整刻）后不能签收，密钥正确也不行
        if now >= invitation["deadline"]:
            raise ApiError(
                409,
                "invitation_expired",
                f"签收邀请 {req.invitation_no} 已于 {invitation['deadline'].isoformat()} 截止，"
                "尚未定稿的邀请过期后不能签收",
                details={
                    "invitation_no": req.invitation_no,
                    "archive_no": invitation["archive_no"],
                    "deadline": invitation["deadline"].isoformat(),
                    "submitted_at": now.isoformat(),
                },
            )

        reason = req.reason
        if req.decision == "REJECT":
            if not reason or not reason.strip():
                raise ApiError(
                    422, "receipt_reason_required", "拒收(REJECT)必须填写非空拒收原因"
                )
        else:
            reason = None

        # 每包一份定稿：锁定归档包行，串行化同一包的并发定稿尝试
        locked = conn.execute(
            "SELECT archive_no FROM delivery_archives WHERE archive_no = %s FOR UPDATE",
            (invitation["archive_no"],),
        ).fetchone()
        if locked is None:  # 理论不可达（邀请外键引用归档包），兜底
            raise ApiError(
                404, "archive_not_found", f"归档包不存在: {invitation['archive_no']}"
            )

        if req.decision == "ACCEPT":
            archive = _get_delivery_archive_row(conn, invitation["archive_no"])
            # 与归档创建相同的整链 FOR SHARE 锁定（追溯起点→下游顺序）：
            # 并发撤销(FOR UPDATE)要么先提交（随后读取到非 ACTIVE，点名拒绝），
            # 要么被阻塞到本事务提交之后（签收先于撤销成立），按提交次序判定。
            _lock_delivery_universe(conn, archive["cert_nos"])
            blockers = _receipt_blockers(conn, archive)
            if blockers:
                raise ApiError(
                    422,
                    "receipt_targets_revoked",
                    "接收(ACCEPT)被拒绝：包内目标证书或其冻结追溯链中已有证书被撤销"
                    "或失效；可改用 REJECT 提交（须填写原因）。受影响目标: "
                    + ", ".join(blockers),
                    details={
                        "archive_no": invitation["archive_no"],
                        "invitation_no": req.invitation_no,
                        "affected_targets": blockers,
                    },
                )

        conn.execute(
            """
            INSERT INTO delivery_receipts
                (archive_no, invitation_no, recipient, decision, reason, request_hash, signed_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            (
                invitation["archive_no"],
                req.invitation_no,
                invitation["recipient"],
                req.decision,
                reason,
                req_hash,
                now,
            ),
        )
        return {
            "archive_no": invitation["archive_no"],
            "invitation_no": req.invitation_no,
            "recipient": invitation["recipient"],
            "decision": req.decision,
            "reason": reason,
            "signed_at": now,
        }, True


def submit_delivery_receipt(conn, req) -> tuple[dict, bool]:
    """收件人在截止前提交签收结论，返回 (定稿记录, 是否首次定稿)。

    - 邀请不存在 → 404 invitation_not_found；一次性密钥错误 →
      401 invalid_receipt_key；未定稿且已过截止（含整刻）→
      409 invitation_expired（过期/失效密钥均不能签收）；
    - REJECT 必须填写非空原因（422 receipt_reason_required）；
    - ACCEPT 时包内目标自身或其冻结追溯链中任一证书当前已撤销/失效 →
      422 receipt_targets_revoked，details.affected_targets 点名受影响目标，
      REJECT 不受此限制仍可提交；
    - 每包一份定稿（delivery_receipts.archive_no 主键）：同一邀请同内容重试
      （截止后亦然）返回首次定稿（HTTP 200）；不同内容或其他邀请再提交 →
      409 receipt_conflict；并发定稿由主键兜底，后到事务回滚后按已提交
      记录重放/判冲突；
    - 与撤销并发按提交顺序判定：ACCEPT 事务对目标及全链按祖先优先加
      FOR SHARE 锁（经 _receipt_blockers 的前置整链锁），撤销先提交则
      本次 ACCEPT 点名受影响目标而拒绝，签收先提交则定稿成立；定稿后
      的撤销不改写签收记录与归档快照（两表只增不改）。
    """
    req_hash = _receipt_request_hash(req.invitation_no, req.decision, req.reason)
    key_hash = _hash_receipt_key(req.key)
    for attempt in range(3):
        try:
            return _submit_receipt_once(conn, req, req_hash, key_hash=key_hash,
                                        now=datetime.now(timezone.utc))
        except psycopg.errors.DeadlockDetected:
            # 与并发撤销的加锁等待环由死锁检测终止：整体回滚后重试
            if attempt == 2:
                raise ApiError(
                    409,
                    "receipt_busy",
                    f"签收 {req.invitation_no} 与并发撤销竞争，重试后仍冲突，请稍后重试",
                    details={"invitation_no": req.invitation_no},
                )
        except psycopg.errors.UniqueViolation:
            # 并发定稿：主键兜底，后到事务回滚后按已提交记录重放/判冲突
            invitation = _get_invitation_row(conn, req.invitation_no)
            if invitation is None:
                raise ApiError(
                    404, "invitation_not_found", f"签收邀请不存在: {req.invitation_no}"
                )
            existing = conn.execute(
                """
                SELECT archive_no, invitation_no, recipient, decision, reason,
                       signed_at, request_hash
                FROM delivery_receipts WHERE archive_no = %s
                """,
                (invitation["archive_no"],),
            ).fetchone()
            if existing is not None:
                return _receipt_conflict(existing, req.invitation_no, req_hash), False
            raise
    raise ApiError(500, "receipt_busy", "签收提交失败，请稍后重试")


def get_delivery_receipt(conn, archive_no: str, management_key: str | None) -> dict:
    """凭管理密钥按归档包编号查询定稿记录。

    管理密钥错误 → 401 invalid_management_key；归档包不存在 →
    404 archive_not_found；归档包存在但尚未定稿 → 404 receipt_not_found。
    """
    _require_management_key(management_key)
    if _get_delivery_archive_row(conn, archive_no) is None:
        raise ApiError(404, "archive_not_found", f"归档包不存在: {archive_no}")
    receipt = _get_receipt_by_archive(conn, archive_no)
    if receipt is None:
        raise ApiError(
            404,
            "receipt_not_found",
            f"归档包尚无签收录制: {archive_no}",
            details={"archive_no": archive_no},
        )
    return receipt


# ---------------------------------------------------------------------------
# 撤销批次的已交付凭据影响面（按归档包 + 包内目标证书逐行列出）
# ---------------------------------------------------------------------------

def get_revocation_archive_impact(conn, batch_id: int, limit: int, offset: int) -> dict:
    """按撤销批次追查已交付归档凭据的影响面（只读，覆盖全部已存在归档包）。

    清单行 = 一个归档包内的一张受影响目标证书：该目标在归档时刻冻结的追溯链
    （快照中的 chain.nodes）包含本批被撤销证书集合中的任一证书——集合含被主动
    撤销的证书（revocations.cert_no）与全部级联失效的下游证书（invalidations）。

    - 同一目标在同一包内只列一次（其链上可能命中多个本批证书，去重为一行）；
      同一目标出现在多个归档包时分别列出；同包未受影响项不出现；
    - 序位均从 1 开始：delivery_position 为原交付序位（提交列表顺序），
      revoked_chain_position 为本批被撤销证书在冻结链中的序位——失效证书必为
      被撤销证书的下游，故链中命中任一失效证书时，被撤销证书必在同一链上，
      该序位恒有定义；
    - 冻结链取自归档快照（response.items），不依赖当前证书状态，因此覆盖此前
      已存在的全部归档包，且不受事后撤销/更正影响；
    - 归档创建与撤销并发时按两者实际提交次序判定：撤销先提交则归档按新状态
      校验（含本批证书的包被拒、从不落库），归档先提交则其快照入清单；
      被拒创建的包不会出现在清单中；
    - delivery_archives / invalidations / revocations 均只增不改，按
      (archive_no, delivery_position) 稳定排序分页，重复查询结果稳定。
    """
    head = conn.execute(
        "SELECT batch_id, cert_no, revoked_at FROM revocations WHERE batch_id = %s",
        (batch_id,),
    ).fetchone()
    if head is None:
        raise ApiError(404, "revocation_not_found", f"撤销批次不存在: {batch_id}")

    rows = conn.execute(
        """
        WITH affected AS (
            -- 本批被撤销证书集合：被主动撤销的证书 + 级联失效的全部下游证书
            SELECT cert_no FROM invalidations WHERE batch_id = %s
            UNION ALL
            SELECT cert_no FROM revocations WHERE batch_id = %s
        ),
        delivered AS (
            -- 各归档包的目标证书及其冻结追溯链；ordinality 即原交付序位（从 1 开始）
            SELECT d.archive_no,
                   d.created_at AS archived_at,
                   t.ordinality AS delivery_position,
                   t.item ->> 'cert_no' AS cert_no,
                   t.item -> 'chain' -> 'nodes' AS nodes
            FROM delivery_archives d
            CROSS JOIN LATERAL jsonb_array_elements(d.response -> 'items')
                 WITH ORDINALITY AS t(item, ordinality)
        ),
        hits AS (
            -- 冻结链命中本批集合的目标；被撤销证书在链中的序位（从 1 开始）
            SELECT dl.archive_no, dl.cert_no, dl.delivery_position, dl.archived_at,
                   MIN(n.ordinality) FILTER (
                       WHERE n.node -> 'certificate' ->> 'cert_no' = %s
                   ) AS revoked_chain_position
            FROM delivered dl
            CROSS JOIN LATERAL jsonb_array_elements(dl.nodes)
                 WITH ORDINALITY AS n(node, ordinality)
            WHERE n.node -> 'certificate' ->> 'cert_no' IN (SELECT cert_no FROM affected)
            GROUP BY dl.archive_no, dl.cert_no, dl.delivery_position, dl.archived_at
        ),
        page AS (
            SELECT archive_no, cert_no, delivery_position, revoked_chain_position,
                   archived_at
            FROM hits
            ORDER BY archive_no ASC, delivery_position ASC
            LIMIT %s OFFSET %s
        )
        -- 单语句同时给出总数与本页：空页（offset 越过末页）时 total 仍为全部命中数
        SELECT p.archive_no, p.cert_no, p.delivery_position, p.revoked_chain_position,
               p.archived_at, t.total
        FROM (SELECT count(*) AS total FROM hits) t
        LEFT JOIN page p ON true
        """,
        (batch_id, batch_id, head["cert_no"], limit, offset),
    ).fetchall()

    total = rows[0]["total"] if rows else 0
    items = [
        {
            "archive_no": r["archive_no"],
            "cert_no": r["cert_no"],
            "delivery_position": r["delivery_position"],
            "revoked_chain_position": r["revoked_chain_position"],
            "archived_at": r["archived_at"],
        }
        for r in rows
        if r["archive_no"] is not None
    ]
    return {
        "batch_id": batch_id,
        "revoked_cert_no": head["cert_no"],
        "revoked_at": head["revoked_at"],
        "total": total,
        "limit": limit,
        "offset": offset,
        "items": items,
    }


# ---------------------------------------------------------------------------
# 撤销风险告知：已定稿 ACCEPT 的交付归档包受后续撤销波及时，由撤销事务在同一
# 事务内生成（每 归档包×批次 一份）；收件人凭定稿邀请编号与一次性密钥查询、
# 确认。告知只增不改，确认时刻一次性写入；不改写归档快照、签收与撤销记录。
# ---------------------------------------------------------------------------

_NOTICE_COLS = (
    "n.notice_id, n.archive_no, n.batch_id, n.items, n.created_at, n.confirmed_at, "
    "r.cert_no AS revoked_cert_no, r.revoked_at"
)
_NOTICE_FROM = (
    "FROM delivery_revocation_notices n "
    "JOIN revocations r ON r.batch_id = n.batch_id"
)


def _generate_revocation_notices(conn, batch_id: int) -> None:
    """在撤销事务内，为已定稿 ACCEPT 且受本批撤销波及的归档包各生成一份告知。

    命中判定与影响面追查同一口径：目标证书在归档时刻冻结的追溯链（归档快照
    中的 chain.nodes，非当前状态）包含本批被撤销证书集合（被主动撤销的证书 +
    全部级联失效证书）中任一证书。每份告知列出受影响目标（含原交付序位）及
    其冻结链上命中的本批证书编号（含 1 起始链上序位）。同包同批只记一份
    （(archive_no, batch_id) 唯一约束兜底）；未签收或 REJECT 的归档不生成。
    """
    conn.execute(
        """
        WITH affected AS (
            -- 本批被撤销证书集合：被主动撤销的证书 + 级联失效的全部下游证书
            SELECT cert_no FROM invalidations WHERE batch_id = %s
            UNION ALL
            SELECT cert_no FROM revocations WHERE batch_id = %s
        ),
        delivered AS (
            -- 已定稿 ACCEPT 的归档包：各目标证书及其冻结追溯链（ordinality 即原交付序位）
            SELECT d.archive_no,
                   t.ordinality AS delivery_position,
                   t.item ->> 'cert_no' AS cert_no,
                   t.item -> 'chain' -> 'nodes' AS nodes
            FROM delivery_archives d
            JOIN delivery_receipts r
              ON r.archive_no = d.archive_no AND r.decision = 'ACCEPT'
            CROSS JOIN LATERAL jsonb_array_elements(d.response -> 'items')
                 WITH ORDINALITY AS t(item, ordinality)
        ),
        hits AS (
            -- 冻结链命中本批集合的目标：受影响目标及链上命中编号（序位从 1 开始）
            SELECT dl.archive_no, dl.cert_no, dl.delivery_position,
                   jsonb_agg(
                       jsonb_build_object(
                           'cert_no', n.node -> 'certificate' ->> 'cert_no',
                           'chain_position', n.ordinality
                       ) ORDER BY n.ordinality
                   ) AS hits
            FROM delivered dl
            CROSS JOIN LATERAL jsonb_array_elements(dl.nodes)
                 WITH ORDINALITY AS n(node, ordinality)
            WHERE n.node -> 'certificate' ->> 'cert_no' IN (SELECT cert_no FROM affected)
            GROUP BY dl.archive_no, dl.cert_no, dl.delivery_position
        )
        INSERT INTO delivery_revocation_notices (archive_no, batch_id, items)
        SELECT h.archive_no, %s,
               jsonb_agg(
                   jsonb_build_object(
                       'cert_no', h.cert_no,
                       'delivery_position', h.delivery_position,
                       'hits', h.hits
                   ) ORDER BY h.delivery_position
               )
        FROM hits h
        GROUP BY h.archive_no
        ON CONFLICT (archive_no, batch_id) DO NOTHING
        """,
        (batch_id, batch_id, batch_id),
    )


def _notice_view(row: dict) -> dict:
    return {
        "notice_id": row["notice_id"],
        "archive_no": row["archive_no"],
        "batch_id": row["batch_id"],
        "revoked_cert_no": row["revoked_cert_no"],
        "revoked_at": row["revoked_at"],
        "items": row["items"],
        "created_at": row["created_at"],
        "confirmed_at": row["confirmed_at"],
    }


def _notice_access(conn, invitation_no: str, key: str) -> dict:
    """收件人访问撤销风险告知的凭据校验：定稿邀请编号 + 原一次性密钥。

    - 邀请不存在 → 404 invitation_not_found；一次性密钥错误 → 401 invalid_receipt_key；
    - 归档包尚未定稿 → 404 receipt_not_found；定稿的是同一归档的另一份邀请 →
      409 receipt_conflict（details 给出定稿邀请编号与结论）；
    - 邀请的截止时刻不参与判定：密钥过期不影响已签收后的查询与确认。
    """
    invitation = _get_invitation_row(conn, invitation_no)
    if invitation is None:
        raise ApiError(
            404, "invitation_not_found", f"签收邀请不存在: {invitation_no}"
        )
    if not hmac.compare_digest(invitation["key_hash"], _hash_receipt_key(key)):
        raise ApiError(
            401,
            "invalid_receipt_key",
            f"一次性签收密钥错误（邀请 {invitation_no}）",
            details={"invitation_no": invitation_no},
        )
    receipt = conn.execute(
        """
        SELECT archive_no, invitation_no, recipient, decision, reason, signed_at
        FROM delivery_receipts WHERE archive_no = %s
        """,
        (invitation["archive_no"],),
    ).fetchone()
    if receipt is None:
        raise ApiError(
            404,
            "receipt_not_found",
            f"归档包尚无签收录制: {invitation['archive_no']}",
            details={"archive_no": invitation["archive_no"]},
        )
    if receipt["invitation_no"] != invitation_no:
        raise ApiError(
            409,
            "receipt_conflict",
            f"归档包 {receipt['archive_no']} 已由邀请 {receipt['invitation_no']} 定稿："
            "查询与确认撤销风险告知须使用定稿邀请编号及其一次性密钥",
            details={
                "archive_no": receipt["archive_no"],
                "finalized_invitation_no": receipt["invitation_no"],
                "finalized_decision": receipt["decision"],
            },
        )
    return {"invitation": invitation, "receipt": receipt}


def list_delivery_revocation_notices(conn, req) -> dict:
    """收件人凭定稿邀请编号与一次性密钥查询本归档包的撤销风险告知列表（只读）。

    REJECT 或未受任何撤销波及的归档返回空清单（total=0）；告知按 notice_id 升序。
    """
    access = _notice_access(conn, req.invitation_no, req.key)
    invitation, receipt = access["invitation"], access["receipt"]
    rows = conn.execute(
        f"SELECT {_NOTICE_COLS} {_NOTICE_FROM} WHERE n.archive_no = %s "
        "ORDER BY n.notice_id ASC",
        (invitation["archive_no"],),
    ).fetchall()
    items = [_notice_view(r) for r in rows]
    return {
        "archive_no": invitation["archive_no"],
        "invitation_no": invitation["invitation_no"],
        "recipient": receipt["recipient"],
        "decision": receipt["decision"],
        "total": len(items),
        "items": items,
    }


def confirm_delivery_revocation_notice(conn, notice_id: int, req) -> tuple[dict, bool]:
    """收件人确认一份撤销风险告知，返回 (告知, 是否首次确认)。

    确认一次性写入 confirmed_at（触发器强制只写一次）；重复确认（含并发确认）
    返回首次确认时间（HTTP 200）。告知不存在、或不属于该定稿邀请对应的归档包
    → 404 notice_not_found；凭据校验与查询一致。
    """
    with conn.transaction():
        access = _notice_access(conn, req.invitation_no, req.key)
        invitation = access["invitation"]
        row = conn.execute(
            f"SELECT {_NOTICE_COLS} {_NOTICE_FROM} WHERE n.notice_id = %s",
            (notice_id,),
        ).fetchone()
        if row is None or row["archive_no"] != invitation["archive_no"]:
            raise ApiError(
                404,
                "notice_not_found",
                f"撤销风险告知不存在: {notice_id}",
                details={"notice_id": notice_id},
            )
        confirmed = conn.execute(
            """
            UPDATE delivery_revocation_notices
            SET confirmed_at = now()
            WHERE notice_id = %s AND confirmed_at IS NULL
            RETURNING confirmed_at
            """,
            (notice_id,),
        ).fetchone()
        if confirmed is None:
            # 已确认过（含并发确认被阻塞后重估）：原样返回首次确认时间
            row = conn.execute(
                f"SELECT {_NOTICE_COLS} {_NOTICE_FROM} WHERE n.notice_id = %s",
                (notice_id,),
            ).fetchone()
            return _notice_view(row), False
        row["confirmed_at"] = confirmed["confirmed_at"]
        return _notice_view(row), True


# ---------------------------------------------------------------------------
# 标准证书临时停用（待复核）与解除：停用期间该证书及任何下游证书不得作为
# 新签发普通证书的追溯依据。停用/解除不触碰 certificates 表——证书内容、
# ACTIVE/REVOKED/INVALID 状态、已冻结的交付归档快照均不因停用改写。
# ---------------------------------------------------------------------------

_SUSPENSION_COLS = "suspension_id, cert_no, reason, suspended_at, lifted_at, lift_reason"


def _active_suspension_row(conn, cert_no: str) -> dict | None:
    return conn.execute(
        f"SELECT {_SUSPENSION_COLS} FROM cert_suspensions "
        "WHERE cert_no = %s AND lifted_at IS NULL LIMIT 1",
        (cert_no,),
    ).fetchone()


def _gather_active_descendants_nos(conn, cert_no: str) -> list[str]:
    """当前所有直接/间接下游证书编号（含已撤销/失效者，仅用于加锁范围）。"""
    rows = conn.execute(
        """
        WITH RECURSIVE d AS (
            SELECT cert_no FROM certificates WHERE parent_cert_no = %s
            UNION ALL
            SELECT c.cert_no
            FROM certificates c JOIN d ON c.parent_cert_no = d.cert_no
        )
        CYCLE cert_no SET is_cycle USING cycle_path
        SELECT cert_no FROM d WHERE NOT is_cycle
        """,
        (cert_no,),
    ).fetchall()
    return sorted(r["cert_no"] for r in rows)


def _suspension_event(row: dict) -> dict:
    """一行停用记录展开为时间线事件。"""
    events = [
        {
            "suspension_id": row["suspension_id"],
            "action": "SUSPEND",
            "reason": row["reason"],
            "at": row["suspended_at"],
            "lifted_at": None,
            "lift_reason": None,
        }
    ]
    if row["lifted_at"] is not None:
        events.append(
            {
                "suspension_id": row["suspension_id"],
                "action": "LIFT",
                "reason": row["lift_reason"],
                "at": row["lifted_at"],
                "lifted_at": row["lifted_at"],
                "lift_reason": row["lift_reason"],
            }
        )
    return events


def _suspension_view(conn, cert_no: str) -> dict:
    certificate = get_certificate(conn, cert_no)
    rows = conn.execute(
        f"SELECT {_SUSPENSION_COLS} FROM cert_suspensions "
        "WHERE cert_no = %s ORDER BY suspension_id ASC",
        (cert_no,),
    ).fetchall()
    history: list[dict] = []
    active_event = None
    for row in rows:
        events = _suspension_event(row)
        if row["lifted_at"] is None:
            active_event = events[0]
        history.extend(events)
    return {
        "cert_no": cert_no,
        "certificate": certificate,
        "currently_suspended": active_event is not None,
        "active": active_event,
        "history": history,
    }


def suspend_certificate(conn, cert_no: str, reason: str, management_key: str | None) -> dict:
    """登记标准证书临时停用（待复核）。

    - 管理密钥经 X-Management-Key 头校验（与交付管理共用同一把本地密钥）；
      密钥缺失/错误 → 401 invalid_management_key；
    - 只接受生效证书：证书不存在 → 404 cert_not_found；证书非 ACTIVE
      （已撤销/失效）→ 409 cert_not_active；
    - 原因必须非空白 → 422 suspension_reason_required；
    - 重复停用（已有生效中的停用记录）→ 409 suspension_conflict；
    - 停用从登记成功（事务提交）时刻起生效：先对被停用证书加 FOR UPDATE，
      再按编号排序锁定其全部当前下游，并经部分唯一索引兜底；并发签发事务
      对上游链持 FOR SHARE 锁，停用提交前无法插入引用该链的新证书，
      停用提交后其受阻检查必见本记录——按实际提交次序生效。
    """
    _require_management_key(management_key)
    if not reason or not reason.strip():
        raise ApiError(422, "suspension_reason_required", "停用原因不能为空")
    reason = reason.strip()
    try:
        with conn.transaction():
            row = conn.execute(
                "SELECT cert_no, status FROM certificates WHERE cert_no = %s FOR UPDATE",
                (cert_no,),
            ).fetchone()
            if row is None:
                raise ApiError(404, "cert_not_found", f"证书不存在: {cert_no}")
            if row["status"] != "ACTIVE":
                raise ApiError(
                    409,
                    "cert_not_active",
                    f"证书 {cert_no} 当前状态为 {row['status']}，仅生效中的证书可登记停用",
                    details={"cert_no": cert_no, "status": row["status"]},
                )
            if _active_suspension_row(conn, cert_no) is not None:
                raise ApiError(
                    409,
                    "suspension_conflict",
                    f"证书 {cert_no} 已存在生效中的临时停用记录，不能重复停用；"
                    "请先复核并解除停用",
                    details={"cert_no": cert_no},
                )
            # 与撤销事务相同的防御性下游收集循环：收集→锁定→再收集，直到集合
            # 不再扩大，覆盖“停用进行中恰有下游证书提交”的竞态。
            locked: set[str] = set()
            while True:
                fresh = [no for no in _gather_active_descendants_nos(conn, cert_no)
                         if no not in locked]
                if not fresh:
                    break
                conn.execute(
                    "SELECT cert_no FROM certificates "
                    "WHERE cert_no = ANY(%s) ORDER BY cert_no FOR UPDATE",
                    (fresh,),
                ).fetchall()
                locked.update(fresh)
            suspended = conn.execute(
                "INSERT INTO cert_suspensions (cert_no, reason) "
                "VALUES (%s, %s) RETURNING " + _SUSPENSION_COLS,
                (cert_no, reason),
            ).fetchone()
    except psycopg.errors.UniqueViolation:
        # 并发停用同一证书：部分唯一索引兜底，后到事务回滚后按冲突处理
        existing = _active_suspension_row(conn, cert_no)
        if existing is not None:
            raise ApiError(
                409,
                "suspension_conflict",
                f"证书 {cert_no} 已存在生效中的临时停用记录（并发登记），不能重复停用",
                details={"cert_no": cert_no, "suspension_id": existing["suspension_id"]},
            )
        raise ApiError(
            409,
            "cert_not_active",
            f"证书 {cert_no} 当前不是生效状态（并发变更），仅生效中的证书可登记停用",
            details={"cert_no": cert_no},
        )
    except psycopg.errors.DeadlockDetected:
        raise ApiError(
            409,
            "suspension_busy",
            f"证书 {cert_no} 的停用登记与并发签发/撤销竞争，重试后仍冲突，请稍后重试",
            details={"cert_no": cert_no},
        )
    body = _suspension_view(conn, cert_no)
    body["suspension"] = _suspension_event(suspended)[0]
    return body


def lift_certificate(conn, cert_no: str, reason: str, management_key: str | None) -> dict:
    """复核后解除标准证书临时停用。

    - 管理密钥缺失/错误 → 401；解除原因非空白 → 422 suspension_reason_required；
    - 证书不存在 → 404 cert_not_found；无生效中的停用记录（重复解除）→
      409 suspension_not_active；
    - 解除事务先对被停用证书的完整链（根→目标顺序）加 FOR SHARE，再锁定
      生效停用记录行（FOR UPDATE），与并发签发的链上 SHARE 锁按提交次序
      判定：解除先提交则受阻检查放行，签发先提交则被阻塞到解除提交之后；
    - 停用/解除记录只增不改（触发器强制解除字段一次性写入）。
    """
    _require_management_key(management_key)
    if not reason or not reason.strip():
        raise ApiError(422, "suspension_reason_required", "解除停用原因不能为空")
    reason = reason.strip()
    for attempt in range(3):
        try:
            with conn.transaction():
                cert = conn.execute(
                    "SELECT 1 FROM certificates WHERE cert_no = %s", (cert_no,)
                ).fetchone()
                if cert is None:
                    raise ApiError(404, "cert_not_found", f"证书不存在: {cert_no}")
                active = _active_suspension_row(conn, cert_no)
                if active is None:
                    raise ApiError(
                        409,
                        "suspension_not_active",
                        f"证书 {cert_no} 没有生效中的临时停用记录，无需解除（不能重复解除）",
                        details={"cert_no": cert_no},
                    )
                # 与并发签发按提交次序串行化（详见函数文档）
                _lock_parent_chain_share(conn, cert_no)
                lifted = conn.execute(
                    """
                    UPDATE cert_suspensions
                    SET lifted_at = now(), lift_reason = %s
                    WHERE suspension_id = %s AND lifted_at IS NULL
                    RETURNING """ + _SUSPENSION_COLS,
                    (reason, active["suspension_id"]),
                ).fetchone()
                if lifted is None:
                    # 并发解除已抢先提交：整体回滚，按“无生效停用”判冲突
                    raise ApiError(
                        409,
                        "suspension_not_active",
                        f"证书 {cert_no} 的停用已被并发解除（不能重复解除）",
                        details={"cert_no": cert_no},
                    )
            body = _suspension_view(conn, cert_no)
            body["lift"] = _suspension_event(lifted)[1]
            return body
        except ApiError:
            raise
        except psycopg.errors.DeadlockDetected:
            if attempt == 2:
                raise ApiError(
                    409,
                    "suspension_busy",
                    f"证书 {cert_no} 的停用解除与并发签发/撤销竞争，重试后仍冲突，请稍后重试",
                    details={"cert_no": cert_no},
                )


def get_certificate_suspension(conn, cert_no: str, management_key: str | None) -> dict:
    """查询证书的当前停用状态与历次停用/解除的时间和原因（须凭管理密钥）。

    证书不存在 → 404 cert_not_found；密钥缺失/错误 → 401。
    从无停用记录的证书同样可查：currently_suspended=false、active=null、
    history=[]，证书内容为当前真实内容（撤销/失效仍如实展示）。
    """
    _require_management_key(management_key)
    return _suspension_view(conn, cert_no)


# ---------------------------------------------------------------------------
# 设备校准覆盖审计（过去半开区间内的合规/空窗片段，只读）
# ---------------------------------------------------------------------------

def get_device_coverage_audit(conn, device_id: str, qfrom: datetime, qto: datetime) -> dict:
    """审计设备在半开区间 [from, to) 内的校准覆盖，按时间顺序返回完整、无重叠的
    合规（COMPLIANT）与空窗（GAP）片段。

    - 计入覆盖的只有该设备的合格普通证书（kind=CALIBRATION 且 conclusion=PASS）；
    - 每张证书的计入窗口为 [max(签发创建时刻, 有效期起点),
      min(有效期终点, 撤销/级联失效时刻))，再裁剪到审计区间；
    - 上游证书的有效性在签发时已按下游校准时刻校验，本审计不再回溯重判：
      上游事后到期不影响下游覆盖；上游事后撤销已通过下游证书自身的级联失效
      时刻（certificates.revoked_at，撤销事务内与级联失效同事务写入）截断；
    - 历史重签（撤销/失效释放区间后再签发，有效期可与原证书重叠）按各证书
      实际的签发创建与失效事件还原，不以当前状态替代历史状态；并发交错时
      同一时刻可能被多张证书覆盖，片段如实列出全部可用编号（升序）；
    - 相邻且状态与证书编号集合相同的片段合并；空窗片段 cert_nos 为空列表；
    - 未知设备（或无任何合格普通证书的设备）返回整段空窗，不报 404；
    - 起止必须携带时区、起点必须早于终点、终点不得晚于查询时刻，否则 422；
      响应中的时间统一为 UTC（带时区 ISO 8601，与输入为同一时刻）。
    """
    for label, value in (("from", qfrom), ("to", qto)):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ApiError(
                422,
                "invalid_audit_window",
                "审计起止时刻必须携带时区（如 2026-03-01T09:00:00+08:00）；"
                f"参数 {label}={value.isoformat()} 缺少时区",
                details={
                    "device_id": device_id,
                    "from": qfrom.isoformat(),
                    "to": qto.isoformat(),
                },
            )
    start = qfrom.astimezone(timezone.utc)
    end = qto.astimezone(timezone.utc)
    if not start < end:
        raise ApiError(
            422,
            "invalid_audit_window",
            "审计起点必须早于终点（半开区间 [from, to)）："
            f"from={qfrom.isoformat()}, to={qto.isoformat()}",
            details={
                "device_id": device_id,
                "from": qfrom.isoformat(),
                "to": qto.isoformat(),
            },
        )
    now = datetime.now(timezone.utc)
    if end > now:
        raise ApiError(
            422,
            "invalid_audit_window",
            "审计终点晚于查询时刻，只能审计已经过去的区间："
            f"to={qto.isoformat()}, now={now.isoformat()}",
            details={
                "device_id": device_id,
                "from": qfrom.isoformat(),
                "to": qto.isoformat(),
                "now": now.isoformat(),
            },
        )

    rows = conn.execute(
        """
        SELECT cert_no, valid_from, valid_to, created_at, revoked_at
        FROM certificates
        WHERE device_id = %s AND kind = 'CALIBRATION' AND conclusion = 'PASS'
        ORDER BY cert_no ASC
        """,
        (device_id,),
    ).fetchall()

    # 每张合格普通证书的计入窗口（裁剪到审计区间后非空才参与扫描）
    windows: list[tuple[datetime, datetime, str]] = []
    for r in rows:
        w_start = max(
            r["created_at"].astimezone(timezone.utc),
            r["valid_from"].astimezone(timezone.utc),
        )
        w_end = r["valid_to"].astimezone(timezone.utc)
        if r["revoked_at"] is not None:
            # 撤销（REVOKED）或级联失效（INVALID）时刻，二者都记录在 revoked_at
            w_end = min(w_end, r["revoked_at"].astimezone(timezone.utc))
        lo, hi = max(w_start, start), min(w_end, end)
        if lo < hi:
            windows.append((lo, hi, r["cert_no"]))

    # 扫描线：审计区间端点 + 全部窗口边界；相邻边界之间的可用证书集合恒定
    boundaries = sorted({start, end} | {b for lo, hi, _ in windows for b in (lo, hi)})
    segments: list[dict] = []
    for a, b in zip(boundaries, boundaries[1:]):
        active = sorted(no for lo, hi, no in windows if lo <= a < hi)
        status = "COMPLIANT" if active else "GAP"
        if (
            segments
            and segments[-1]["status"] == status
            and segments[-1]["cert_nos"] == active
        ):
            segments[-1]["to"] = b  # 相邻且状态与编号相同 → 合并
        else:
            segments.append({"from": a, "to": b, "status": status, "cert_nos": active})

    return {"device_id": device_id, "from": start, "to": end, "segments": segments}


# ---------------------------------------------------------------------------
# 普通证书双人复核签发：录入人提交待审申请（不占编号/有效期），另一身份的
# 复核人驳回（附原因）或批准——批准与真正签发在同一事务内按“批准当时”的
# 标准证书状态、临时停用与现有签发规则完成，结论、复核人与证书绑定。
# ---------------------------------------------------------------------------

_REVIEW_COLS = (
    "application_no, request_hash, request, status, submitter, submitter_key_hash, "
    "submitted_at, reviewer, decision_reason, reviewed_at, cert_no, result"
)


def _identify(provided: str | None, identities: dict[str, str], *, role: str) -> tuple[str, str]:
    """按本地身份凭据表认证，返回 (身份名, 凭据 SHA-256)。

    凭据仅本地配置、不落库；库内只持久化 SHA-256 指纹（用于双人不同凭据
    校验），响应只回显身份名。恒定时间比较，避免计时侧信道。
    """
    if not provided:
        raise ApiError(
            401,
            "invalid_identity_key",
            f"缺少{role}凭据：须在请求头中携带本地配置的{role}身份凭据",
        )
    for name, secret in identities.items():
        if hmac.compare_digest(provided, secret):
            return name, hashlib.sha256(provided.encode("utf-8")).hexdigest()
    raise ApiError(
        401,
        "invalid_identity_key",
        f"{role}身份凭据无效：凭据不在本地配置的{role}凭据表中",
    )


def _get_review_row(conn, application_no: str, lock: bool = False) -> dict | None:
    sql = f"SELECT {_REVIEW_COLS} FROM certificate_review_requests WHERE application_no = %s"
    if lock:
        sql += " FOR UPDATE"
    return conn.execute(sql, (application_no,)).fetchone()


def _review_view(row: dict) -> dict:
    """复核申请行 -> API 视图；批准结果从 result 快照还原（不依赖证书当前状态）。"""
    review = None
    if row["status"] in ("APPROVED", "REJECTED"):
        review = {
            "decision": "APPROVE" if row["status"] == "APPROVED" else "REJECT",
            "reviewer": row["reviewer"],
            "reason": row["decision_reason"],
            "reviewed_at": row["reviewed_at"],
        }
    body = {
        "application_no": row["application_no"],
        "status": row["status"],
        "request": row["request"],
        "submitter": row["submitter"],
        "submitted_at": row["submitted_at"],
        "review": review,
        "certificate": None,
        "chain": None,
    }
    if row["status"] == "APPROVED" and row["result"] is not None:
        body["certificate"] = row["result"]["certificate"]
        body["chain"] = row["result"]["chain"]
    return body


def submit_certificate_review(conn, req, entry_key: str | None) -> tuple[dict, bool]:
    """录入人提交普通证书签发复核申请，返回 (申请视图, 是否首次提交)。

    - 录入凭据经 X-Entry-Key 校验（401 invalid_identity_key）；
    - 申请只保存待审内容：不预占证书编号（cert_duplicate 等在批准时才判定）、
      不预占设备有效期（validity_overlap 在批准时才判定），因此引用尚不存在、
      区间当下重叠等都允许先提交，能否签发以“批准当时”的状态为准；
    - 申请号幂等：同号同内容重试返回原申请（200，状态随原申请），
      同号异内容返回 409 review_request_conflict；并发同号由主键兜底。
    """
    submitter, _ = _identify(entry_key, settings.entry_identities, role="录入人")
    req_hash, request_doc = _canonical_request(req)
    try:
        with conn.transaction():
            existing = _get_review_row(conn, req.application_no)
            if existing is not None:
                if existing["request_hash"] != req_hash:
                    raise ApiError(
                        409,
                        "review_request_conflict",
                        f"申请号 {req.application_no} 已用于一份签发资料不同的复核申请；"
                        "同一申请号只允许重放完全相同的请求",
                        details={"application_no": req.application_no},
                    )
                return _review_view(existing), False
            conn.execute(
                """
                INSERT INTO certificate_review_requests
                    (application_no, request_hash, request, status,
                     submitter, submitter_key_hash)
                VALUES (%s, %s, %s, 'PENDING', %s, %s)
                """,
                (
                    req.application_no,
                    req_hash,
                    Jsonb(request_doc),
                    submitter,
                    hashlib.sha256((entry_key or "").encode("utf-8")).hexdigest(),
                ),
            )
            row = _get_review_row(conn, req.application_no)
        return _review_view(row), True
    except psycopg.errors.UniqueViolation:
        # 并发同号提交：主键兜底，后到事务回滚后按已提交申请重放/判冲突
        existing = _get_review_row(conn, req.application_no)
        if existing is None:
            raise
        if existing["request_hash"] != req_hash:
            raise ApiError(
                409,
                "review_request_conflict",
                f"申请号 {req.application_no} 已用于一份签发资料不同的复核申请（并发提交）",
                details={"application_no": req.application_no},
            )
        return _review_view(existing), False


def _decision_request_hash(decision: str, reason: str | None) -> str:
    """规范化复核操作指纹：结论与驳回原因参与判定；复核人身份另行强校验。"""
    canonical = json.dumps(
        {"decision": decision, "reason": reason},
        sort_keys=True, ensure_ascii=False, separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _decided_conflict(existing: dict) -> ApiError:
    """已定稿申请的再次定稿一律冲突（驳回后不能再批准，批准后不能改判）。"""
    return ApiError(
        409,
        "review_already_decided",
        f"申请 {existing['application_no']} 已由复核人 {existing['reviewer']} "
        f"{'批准' if existing['status'] == 'APPROVED' else '驳回'}："
        "每份申请只允许一份定稿，驳回后不能再批准、批准后不能改判；"
        "同复核人同操作重试才返回首次结果。",
        details={
            "application_no": existing["application_no"],
            "status": existing["status"],
            "finalized_reviewer": existing["reviewer"],
            "finalized_decision": "APPROVE" if existing["status"] == "APPROVED" else "REJECT",
        },
    )


def _decide_review_once(conn, application_no: str, req, reviewer: str, reviewer_key_hash: str, now: datetime):
    """单次定稿事务；返回 (申请视图, 是否首次定稿)。

    批准路径：先锁申请行（串行化并发定稿），再在同一事务内执行与直接签发
    完全相同的校验与落库（_issue_certificate_in_tx），最后把申请推进为
    APPROVED 并绑定证书与结果快照——任一步失败整事务回滚：申请仍 PENDING、
    无证书产生，错误码/原因与直接签发一致。
    """
    with conn.transaction():
        row = _get_review_row(conn, application_no, lock=True)
        if row is None:
            raise ApiError(
                404, "review_request_not_found",
                f"复核申请不存在: {application_no}",
            )
        if row["status"] != "PENDING":
            # 同复核人 + 同操作（结论、原因一致）的重放返回首次结果；其余冲突。
            same_decision = (
                row["reviewer"] == reviewer
                and (
                    (row["status"] == "APPROVED" and req.decision == "APPROVE"
                     and not req.reason)
                    or (row["status"] == "REJECTED" and req.decision == "REJECT"
                        and req.reason is not None
                        and row["decision_reason"] == req.reason.strip())
                )
            )
            if same_decision:
                return _review_view(row), False
            raise _decided_conflict(row)

        # 双人复核：复核凭据不得与录入凭据为同一把
        if hmac.compare_digest(row["submitter_key_hash"], reviewer_key_hash):
            raise ApiError(
                403,
                "reviewer_must_differ",
                "复核人不得与录入人使用同一把身份凭据：普通证书签发须经双人复核",
                details={"application_no": application_no, "submitter": row["submitter"]},
            )

        reason = None
        if req.decision == "REJECT":
            if not req.reason or not req.reason.strip():
                raise ApiError(
                    422, "review_reason_required",
                    "驳回(REJECT)必须填写非空驳回原因；批准(APPROVE)请使用 decision=APPROVE",
                )
            reason = req.reason.strip()
            conn.execute(
                """
                UPDATE certificate_review_requests
                SET status = 'REJECTED', reviewer = %s, decision_reason = %s, reviewed_at = %s
                WHERE application_no = %s
                """,
                (reviewer, reason, now, application_no),
            )
        else:
            if req.reason:
                raise ApiError(
                    422, "review_approve_reason_not_allowed",
                    "批准(APPROVE)不得填写驳回原因；驳回请使用 decision=REJECT 并填写原因",
                )
            # 批准当时按现有规则真正签发（状态/停用/有效期/编号占用均在此刻校验）。
            issue_req = CertificateIssueRequest.model_validate(row["request"])
            result = _issue_certificate_in_tx(conn, issue_req)
            conn.execute(
                """
                UPDATE certificate_review_requests
                SET status = 'APPROVED', reviewer = %s, reviewed_at = %s,
                    cert_no = %s, result = %s
                WHERE application_no = %s
                """,
                (
                    reviewer, now, issue_req.cert_no,
                    Jsonb(result, dumps=_json_dumps), application_no,
                ),
            )
        row = _get_review_row(conn, application_no)
        return _review_view(row), True


def decide_certificate_review(conn, application_no: str, req, reviewer_key: str | None) -> tuple[dict, bool]:
    """复核人驳回/批准复核申请，返回 (申请视图, 是否首次定稿)。

    - 复核凭据经 X-Reviewer-Key 校验（401 invalid_identity_key），且不得与
      录入凭据相同（403 reviewer_must_differ）；
    - 申请不存在 404 review_request_not_found；REJECT 原因空白 422
      review_reason_required；APPROVE 带原因 422 review_approve_reason_not_allowed；
    - 批准与真正签发同事务：签发校验失败时错误码与直接签发完全一致
      （invalid_reference / reference_not_active / reference_suspended /
      reference_not_qualified / reference_out_of_validity / validity_overlap /
      cert_duplicate / validity_overlap_concurrent …），申请仍 PENDING、不产生证书；
    - 驳回后不能再批准、批准后不能改判：仅同复核人同操作（同结论同原因）
      重试返回首次结果（200），其余再定稿 409 review_already_decided；
    - 并发定稿由申请行 FOR UPDATE 串行化，只有一份定稿；与停用/撤销的链上
      等待环由死锁检测终止后整体重试，耗尽 409 review_busy。
    """
    reviewer, key_hash = _identify(reviewer_key, settings.reviewer_identities, role="复核人")
    if req.decision == "REJECT" and req.reason is not None:
        # 规范化原因（去首尾空白）参与同操作重放判定与落库
        req = req.model_copy(update={"reason": req.reason.strip()})
    for attempt in range(3):
        try:
            return _decide_review_once(
                conn, application_no, req, reviewer, key_hash, datetime.now(timezone.utc)
            )
        except psycopg.errors.DeadlockDetected:
            if attempt == 2:
                raise ApiError(
                    409,
                    "review_busy",
                    f"申请 {application_no} 的复核与并发停用/撤销竞争，重试后仍冲突，"
                    "申请仍为待审，请稍后重试",
                    details={"application_no": application_no},
                )
    raise ApiError(409, "review_busy", "复核定稿失败，请稍后重试")


def get_certificate_review(conn, application_no: str, provided_key: str | None) -> dict:
    """按申请号查询状态与结果；录入人或复核人凭据均可查询（仅本服务内部角色）。

    凭据缺失/错误 401 invalid_identity_key；申请不存在 404 review_request_not_found。
    PENDING 返回待审内容与提交信息；APPROVED 额外返回签发证书与追溯链
    （首次签发结果快照）；REJECTED 返回驳回原因与复核人。
    """
    try:
        _identify(provided_key, settings.entry_identities, role="录入人")
    except ApiError as entry_err:
        try:
            _identify(provided_key, settings.reviewer_identities, role="复核人")
        except ApiError:
            raise entry_err
    row = _get_review_row(conn, application_no)
    if row is None:
        raise ApiError(
            404, "review_request_not_found", f"复核申请不存在: {application_no}"
        )
    return _review_view(row)


# ---------------------------------------------------------------------------
# 设备校准委托工单：管理员凭管理密钥登记唯一工单号、设备标识、委托人与带
# 时区的计划校准时段（半开区间），并取消/结清/查询。登记不预占证书编号、
# 有效期或每日限额；结清只接受设备一致、校准时刻落在计划时段内、当前
# ACTIVE 且 PASS 的普通证书，一张证书至多结清一个工单（部分唯一索引兜底）。
# 结清绑定固定（同工单同证书重放首次结果，改绑/取消冲突）；证书日后撤销/
# 失效不倒退工单状态，查询实时返回证书当前状态。
# ---------------------------------------------------------------------------

_WORK_ORDER_COLS = (
    "work_order_no, device_id, client, scheduled_from, scheduled_to, "
    "request_hash, status, cert_no, settled_at, cancelled_at, created_at"
)


def _get_work_order_row(conn, work_order_no: str, lock: bool = False) -> dict | None:
    sql = f"SELECT {_WORK_ORDER_COLS} FROM calibration_work_orders WHERE work_order_no = %s"
    if lock:
        sql += " FOR UPDATE"
    return conn.execute(sql, (work_order_no,)).fetchone()


def _work_order_view(conn, row: dict) -> dict:
    """工单行 -> API 视图；结清时嵌入绑定证书的【当前】内容（实时状态）。"""
    body = {
        "work_order_no": row["work_order_no"],
        "status": row["status"],
        "device_id": row["device_id"],
        "client": row["client"],
        "scheduled_from": row["scheduled_from"],
        "scheduled_to": row["scheduled_to"],
        "created_at": row["created_at"],
        "cert_no": row["cert_no"],
        "settled_at": row["settled_at"],
        "cancelled_at": row["cancelled_at"],
        "certificate": None,
    }
    if row["cert_no"] is not None:
        # 绑定不可解除，但证书行仅可被撤销（不删除），因此总能读到当前内容
        body["certificate"] = _get_cert_row(conn, row["cert_no"])
    return body


def register_work_order(conn, req, management_key: str | None) -> tuple[dict, bool]:
    """登记设备校准委托工单，返回 (工单视图, 是否首次登记)。

    - 管理密钥缺失/错误 401 invalid_management_key（先于一切业务校验）；
    - 计划时段非半开有效区间 422（Pydantic 层 invalid_*）；
    - 工单号幂等：同号同内容重试返回原工单（200），同号异内容
      409 work_order_conflict；并发同号由主键兜底。
    """
    _require_management_key(management_key)
    req_hash, request_doc = _canonical_request(req)
    scheduled_from = _as_utc(req.scheduled_from)
    scheduled_to = _as_utc(req.scheduled_to)
    try:
        with conn.transaction():
            existing = _get_work_order_row(conn, req.work_order_no)
            if existing is not None:
                if existing["request_hash"] != req_hash:
                    raise ApiError(
                        409,
                        "work_order_conflict",
                        f"工单号 {req.work_order_no} 已用于一份内容不同的委托工单；"
                        "同一工单号只允许重放完全相同的登记请求",
                        details={"work_order_no": req.work_order_no},
                    )
                return _work_order_view(conn, existing), False
            conn.execute(
                """
                INSERT INTO calibration_work_orders
                    (work_order_no, device_id, client, scheduled_from, scheduled_to,
                     request_hash, status)
                VALUES (%s, %s, %s, %s, %s, %s, 'PENDING')
                """,
                (
                    req.work_order_no, req.device_id, req.client,
                    scheduled_from, scheduled_to, req_hash,
                ),
            )
            row = _get_work_order_row(conn, req.work_order_no)
        return _work_order_view(conn, row), True
    except psycopg.errors.UniqueViolation:
        # 并发同号登记：主键兜底，后到事务回滚后按已提交工单重放/判冲突
        existing = _get_work_order_row(conn, req.work_order_no)
        if existing is None:
            raise
        if existing["request_hash"] != req_hash:
            raise ApiError(
                409,
                "work_order_conflict",
                f"工单号 {req.work_order_no} 已用于一份内容不同的委托工单（并发登记）",
                details={"work_order_no": req.work_order_no},
            )
        return _work_order_view(conn, existing), False


def _settle_work_order_once(conn, work_order_no: str, cert_no: str, now: datetime) -> tuple[dict, bool]:
    """单次结清事务，返回 (工单视图, 是否首次结清)。

    先锁工单行（串行化同工单的并发结清/取消），再锁证书行（与并发撤销
    按提交次序串行），并在证书行锁内完成全部结清资格校验，保证不会把
    撤销/失效中的证书绑定进来。
    """
    with conn.transaction():
        order = _get_work_order_row(conn, work_order_no, lock=True)
        if order is None:
            raise ApiError(404, "work_order_not_found", f"工单不存在: {work_order_no}")
        if order["status"] == "SETTLED":
            # 同工单同证书重试返回首次结果；改绑其他证书报冲突。
            if order["cert_no"] == cert_no:
                return _work_order_view(conn, order), False
            raise ApiError(
                409,
                "work_order_cert_rebind_conflict",
                f"工单 {work_order_no} 已绑定证书 {order['cert_no']} 并结清："
                "结清绑定固定，不得改绑其他证书；同工单同证书重试返回首次结果",
                details={
                    "work_order_no": work_order_no,
                    "bound_cert_no": order["cert_no"],
                    "request_cert_no": cert_no,
                },
            )
        if order["status"] == "CANCELLED":
            raise ApiError(
                409,
                "work_order_not_pending",
                f"工单 {work_order_no} 已取消（{order['cancelled_at'].isoformat()}），"
                "已取消工单不得结清（只有待完成工单可结清）",
                details={
                    "work_order_no": work_order_no,
                    "status": "CANCELLED",
                    "cancelled_at": order["cancelled_at"].isoformat(),
                },
            )

        cert = _get_cert_row(conn, cert_no, lock=True)
        if cert is None:
            raise ApiError(404, "cert_not_found", f"证书不存在: {cert_no}")
        if cert["kind"] != "CALIBRATION":
            raise ApiError(
                422,
                "settlement_cert_not_calibration",
                f"证书 {cert_no} 为基准证书（REFERENCE），工单只接受普通校准证书结清",
                details={"work_order_no": work_order_no, "cert_no": cert_no, "kind": cert["kind"]},
            )
        if cert["device_id"] != order["device_id"]:
            raise ApiError(
                422,
                "settlement_device_mismatch",
                f"证书 {cert_no} 的设备标识 {cert['device_id']} 与工单 {work_order_no} "
                f"登记的设备标识 {order['device_id']} 不一致，不得用于结清",
                details={
                    "work_order_no": work_order_no,
                    "cert_no": cert_no,
                    "work_order_device_id": order["device_id"],
                    "cert_device_id": cert["device_id"],
                },
            )
        scheduled_from = _as_utc(order["scheduled_from"])
        scheduled_to = _as_utc(order["scheduled_to"])
        calibrated_at = _as_utc(cert["calibrated_at"])
        if not (scheduled_from <= calibrated_at < scheduled_to):
            raise ApiError(
                422,
                "settlement_outside_schedule",
                f"证书 {cert_no} 的校准时刻 {calibrated_at.isoformat()} 不在工单 "
                f"{work_order_no} 的计划校准时段 "
                f"[{scheduled_from.isoformat()}, {scheduled_to.isoformat()}) 内",
                details={
                    "work_order_no": work_order_no,
                    "cert_no": cert_no,
                    "calibrated_at": calibrated_at.isoformat(),
                    "scheduled_from": scheduled_from.isoformat(),
                    "scheduled_to": scheduled_to.isoformat(),
                },
            )
        if cert["status"] != "ACTIVE":
            raise ApiError(
                422,
                "settlement_cert_not_active",
                f"证书 {cert_no} 当前状态为 {cert['status']}："
                "仅生效中（ACTIVE）的普通证书可结清工单",
                details={"work_order_no": work_order_no, "cert_no": cert_no, "status": cert["status"]},
            )
        if cert["conclusion"] != "PASS":
            raise ApiError(
                422,
                "settlement_cert_not_passed",
                f"证书 {cert_no} 校准结论为 {cert['conclusion']}："
                "仅结论合格（PASS）的普通证书可结清工单",
                details={
                    "work_order_no": work_order_no,
                    "cert_no": cert_no,
                    "conclusion": cert["conclusion"],
                },
            )

        # 部分唯一索引保证“一张证书至多结清一个工单”：并发下若该证书已被
        # 其他工单抢先绑定，UPDATE 在保存点子事务内抛唯一冲突，回滚保存点
        # （外层事务与工单行锁仍有效）后读取持有工单并返回明确冲突。
        holder_no = None
        try:
            with conn.transaction():
                conn.execute(
                    """
                    UPDATE calibration_work_orders
                    SET status = 'SETTLED', cert_no = %s, settled_at = %s
                    WHERE work_order_no = %s AND status = 'PENDING'
                    """,
                    (cert_no, now, work_order_no),
                )
        except psycopg.errors.UniqueViolation:
            holder = conn.execute(
                "SELECT work_order_no FROM calibration_work_orders "
                "WHERE cert_no = %s AND status = 'SETTLED'",
                (cert_no,),
            ).fetchone()
            holder_no = holder["work_order_no"] if holder else None
        if holder_no is not None:
            raise ApiError(
                409,
                "settlement_cert_already_settled",
                f"证书 {cert_no} 已用于结清工单 {holder_no}："
                "一张证书不得结清两个工单",
                details={
                    "work_order_no": work_order_no,
                    "cert_no": cert_no,
                    "bound_work_order_no": holder_no,
                },
            )
        row = _get_work_order_row(conn, work_order_no)
        return _work_order_view(conn, row), True


def settle_work_order(conn, work_order_no: str, req, management_key: str | None) -> tuple[dict, bool]:
    """结清工单：绑定一张已签发的合格普通证书与结清时刻，返回 (视图, 是否首次结清)。

    - 管理密钥缺失/错误 401 invalid_management_key；
    - 工单不存在 404 work_order_not_found；证书不存在 404 cert_not_found；
    - 非普通证书 422 settlement_cert_not_calibration；设备标识不一致
      422 settlement_device_mismatch；校准时刻不在计划半开区间
      422 settlement_outside_schedule；证书非 ACTIVE
      422 settlement_cert_not_active；结论非 PASS 422 settlement_cert_not_passed；
    - 同工单改绑其他证书 409 work_order_cert_rebind_conflict；已取消工单结清
      409 work_order_not_pending；证书已结清其他工单（含并发）
      409 settlement_cert_already_settled（一张证书不得结清两个工单）；
    - 同工单同证书重试返回首次结果（200，绑定证书事后撤销/失效亦原样返回）；
    - 并发结清/取消由工单行 FOR UPDATE 串行化；并发撤销由证书行锁按提交
      次序串行（撤销先提交则本次按非 ACTIVE 拒绝，工单保持 PENDING）；
      理论等待环由死锁检测终止后整体重试，耗尽 409 work_order_busy。
    """
    _require_management_key(management_key)
    cert_no = req.cert_no
    for attempt in range(3):
        try:
            return _settle_work_order_once(
                conn, work_order_no, cert_no, datetime.now(timezone.utc)
            )
        except ApiError:
            raise
        except psycopg.errors.DeadlockDetected:
            if attempt == 2:
                raise ApiError(
                    409,
                    "work_order_busy",
                    f"工单 {work_order_no} 的结清与并发取消/撤销竞争，重试后仍冲突，"
                    "工单保持待完成，请稍后重试",
                    details={"work_order_no": work_order_no, "cert_no": cert_no},
                )
    raise ApiError(409, "work_order_busy", "工单结清失败，请稍后重试")


def cancel_work_order(conn, work_order_no: str, management_key: str | None) -> dict:
    """取消待完成工单（仅 PENDING 可取消），返回工单视图。

    管理密钥缺失/错误 401；工单不存在 404 work_order_not_found；
    已结清/已取消工单再取消 409 work_order_not_pending（details 给出当前
    状态；结清工单只能查询、不可取消或改绑）。并发结清/取消由工单行
    FOR UPDATE 串行化：先到者胜，后到者按已推进状态报冲突。
    """
    _require_management_key(management_key)
    with conn.transaction():
        row = _get_work_order_row(conn, work_order_no, lock=True)
        if row is None:
            raise ApiError(404, "work_order_not_found", f"工单不存在: {work_order_no}")
        if row["status"] != "PENDING":
            raise ApiError(
                409,
                "work_order_not_pending",
                f"工单 {work_order_no} 当前状态为 {row['status']}："
                "只有待完成（PENDING）工单可取消（已结清不得取消或改绑）",
                details={"work_order_no": work_order_no, "status": row["status"]},
            )
        now = datetime.now(timezone.utc)
        conn.execute(
            """
            UPDATE calibration_work_orders
            SET status = 'CANCELLED', cancelled_at = %s
            WHERE work_order_no = %s AND status = 'PENDING'
            """,
            (now, work_order_no),
        )
        row = _get_work_order_row(conn, work_order_no)
        return _work_order_view(conn, row)


def get_work_order(conn, work_order_no: str, management_key: str | None) -> dict:
    """按工单号查询：登记信息、状态，以及绑定证书的当前内容（含当前状态）。

    管理密钥缺失/错误 401；工单不存在 404 work_order_not_found。
    PENDING 不携带证书；SETTLED 嵌入绑定证书的【当前】内容——证书日后
    撤销（REVOKED）或失效（INVALID）只在 certificate.status 实时反映，
    不倒退工单状态、不改写 settled_at 与绑定编号；CANCELLED 不携带证书。
    """
    _require_management_key(management_key)
    row = _get_work_order_row(conn, work_order_no)
    if row is None:
        raise ApiError(404, "work_order_not_found", f"工单不存在: {work_order_no}")
    return _work_order_view(conn, row)
