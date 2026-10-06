"""标准证书每日校准使用量上限测试：

管理密钥设定/调整非负上限（成功返回上限与已用次数、下调低于已用次数 422、
未知证书 404、缺/错密钥 401、未配置查询 404）；普通证书按 calibrated_at 的
UTC 日期与【直接上游】计一次（时区边界按 UTC 切日）；单张签发超限时 422
daily_quota_exceeded 且证书不落库；交接批次批内多张相同键合并计数、超限
整批不落库并点名上游与日期，已存在批次幂等重放只返回首次结果不重复计数；
双人复核批准在真正签发时扣额、超限申请保持 PENDING；撤销/失效不退还已用
次数；未配置上限沿用原签发规则；并发签发不超额、并发调整与签发按提交次序
串行；既有签发/批次/复核接口契约不变。

直接压 HTTP 接口（urllib，无第三方依赖）。服务需已启动。
用法: python3 scripts/quota_checks.py [base_url] [management_key] [entry_key] [reviewer_key]
"""
import json
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8080"
MK = sys.argv[2] if len(sys.argv) > 2 else "caltrace-delivery-admin"
ENTRY = sys.argv[3] if len(sys.argv) > 3 else "caltrace-entry-key"
REVIEWER = sys.argv[4] if len(sys.argv) > 4 else "caltrace-review-key-1"
results = []


def call(method, path, body=None, mgmt=None, entry=None, reviewer=None):
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"}
    if mgmt is not None:
        headers["X-Management-Key"] = mgmt
    if entry is not None:
        headers["X-Entry-Key"] = entry
    if reviewer is not None:
        headers["X-Reviewer-Key"] = reviewer
    req = urllib.request.Request(BASE + path, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def check(name, cond, detail=""):
    results.append((name, cond, detail))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + ("" if cond else f" :: {detail}"))


def err_code(body):
    return body.get("error", {}).get("code")


def err_detail(body):
    return body.get("error", {}).get("details", {})


TZ = timezone(timedelta(hours=8))
t0 = datetime(2026, 1, 1, 0, 0, tzinfo=TZ)
_seq = [0]
_seq_lock = threading.Lock()


def nxt(prefix):
    with _seq_lock:
        _seq[0] += 1
        return f"{prefix}-{_seq[0]}"


def iso(offset_days=0, hh=0):
    return (t0 + timedelta(days=offset_days, hours=hh)).isoformat()


# 校准时刻落在给定 UTC 日（东八区 08:00 = UTC 00:00）
CAT_UTC_DAY = 4  # UTC 日 = 2026-01-05（东八区 2026-01-05T08:00:00+08:00）


def ref(no=None, **kw):
    no = no or nxt("REF")
    body = {
        "cert_no": no, "device_id": "DEV-" + no, "device_name": "基准" + no,
        "valid_from": iso(0), "valid_to": iso(1500), "calibrated_at": iso(0),
    }
    body.update(kw)
    return no, call("POST", "/api/v1/reference-certificates", body)


def setq(parent, day, limit, key=MK):
    return call("PUT", f"/api/v1/certificates/{parent}/daily-quotas/{day}",
                {"daily_limit": limit}, mgmt=key)


def getq(parent, day, key=MK):
    return call("GET", f"/api/v1/certificates/{parent}/daily-quotas/{day}", mgmt=key)


def issue(no, parent, *, cat_day=CAT_UTC_DAY, cat_hh=8, conclusion="PASS",
          vfrom=0, vto=1200, device=None):
    return call("POST", "/api/v1/certificates", {
        "cert_no": no, "device_id": device or ("DEV-" + no), "device_name": "仪表" + no,
        "valid_from": iso(vfrom), "valid_to": iso(vto),
        "calibrated_at": iso(cat_day, cat_hh),
        "conclusion": conclusion, "parent_cert_no": parent})


def batch(key, certs):
    return call("POST", "/api/v1/certificate-batches",
                {"batch_key": key, "certificates": certs})


def batch_item(no, parent, *, cat_day=CAT_UTC_DAY, cat_hh=8):
    return {
        "cert_no": no, "kind": "CALIBRATION", "device_id": "DEV-" + no,
        "device_name": "批仪表" + no,
        "valid_from": iso(0), "valid_to": iso(1200),
        "calibrated_at": iso(cat_day, cat_hh),
        "conclusion": "PASS", "parent_cert_no": parent,
    }


def exists(no):
    s, _ = call("GET", f"/api/v1/certificates/{no}")
    return s == 200


# ---------------------------------------------------------------------------
print("== 0. 前置数据 ==")
r, (s, _) = ref()
check("基准证书 201", s == 201, str(s))
r2, (s, _) = ref()
check("基准证书2 201", s == 201, str(s))
DAY = "2026-01-05"

# ---------------------------------------------------------------------------
print("== A. 管理接口：密钥 / 设定 / 调整 / 查询 ==")
s, e = setq(r, DAY, 2, key=None)
check("缺管理密钥 401", s == 401 and err_code(e) == "invalid_management_key", f"{s} {err_code(e)}")
s, e = setq(r, DAY, 2, key="wrong-key")
check("错管理密钥 401", s == 401 and err_code(e) == "invalid_management_key", f"{s}")
s, e = setq("NO-SUCH-CERT", DAY, 2)
check("未知证书设限 404", s == 404 and err_code(e) == "cert_not_found", f"{s}")
s, b = setq(r, DAY, 2)
check("首次设定 201 返回上限/已用/剩余",
      s == 201 and b["daily_limit"] == 2 and b["used"] == 0 and b["remaining"] == 2
      and b["day"] == DAY and b["cert_no"] == r and b["created_at"] == b["updated_at"],
      json.dumps(b, ensure_ascii=False))
s, b2 = setq(r, DAY, 5)
check("调整既有配置 200，created_at 不变 updated_at 更新",
      s == 200 and b2["daily_limit"] == 5
      and b2["created_at"] == b["created_at"] and b2["updated_at"] >= b["updated_at"],
      json.dumps(b2, ensure_ascii=False))
s, _ = setq(r, DAY, 2)
check("调回上限 2 200", s == 200, str(s))
s, e = setq(r, DAY, -1)
check("负上限 422", s == 422, str(s))
s, e = call("PUT", f"/api/v1/certificates/{r}/daily-quotas/not-a-date",
            {"daily_limit": 1}, mgmt=MK)
check("非法日期 422", s == 422, str(s))
s, b = getq(r, DAY)
check("查询 200 含 used/remaining", s == 200 and b["daily_limit"] == 2, json.dumps(b, ensure_ascii=False))
s, e = getq(r, DAY, key="bad")
check("查询缺/错密钥 401", s == 401, str(s))
s, e = getq(r, "2026-01-06")
check("未配置日查询 404 quota_not_found 且 details 给出 used",
      s == 404 and err_code(e) == "quota_not_found" and err_detail(e).get("used") == 0,
      json.dumps(e, ensure_ascii=False))
s, e = getq("NO-SUCH-CERT", DAY)
check("未知证书查询 404 cert_not_found", s == 404 and err_code(e) == "cert_not_found", str(s))

# ---------------------------------------------------------------------------
print("== B. 单张签发扣额 / 超限拒绝不落库 / UTC 切日 / 未配置沿用原规则 ==")
a, bno = nxt("CAL"), nxt("CAL")
s, _ = issue(a, r)
check("限额内签发1 201", s == 201, str(s))
s, _ = issue(bno, r)
check("限额内签发2 201（用满）", s == 201, str(s))
s, body = getq(r, DAY)
check("已用次数=2 剩余=0", body["used"] == 2 and body["remaining"] == 0, json.dumps(body))
c = nxt("CAL")
s, e = issue(c, r)
check("超限签发 422 daily_quota_exceeded 并点名上游/日期/已用/上限",
      s == 422 and err_code(e) == "daily_quota_exceeded"
      and err_detail(e)["parent_cert_no"] == r and err_detail(e)["day"] == DAY
      and err_detail(e)["used"] == 2 and err_detail(e)["daily_limit"] == 2
      and err_detail(e)["cert_no"] == c,
      json.dumps(e, ensure_ascii=False))
check("超限证书不落库", not exists(c))
# 下调到已用次数以下被拒绝
s, e = setq(r, DAY, 1)
check("下调低于已用 422 quota_below_usage 且配置不变",
      s == 422 and err_code(e) == "quota_below_usage"
      and err_detail(e)["used"] == 2 and err_detail(e)["daily_limit"] == 1,
      json.dumps(e, ensure_ascii=False))
s, body = getq(r, DAY)
check("拒绝下调后上限仍为 2", body["daily_limit"] == 2, str(body.get("daily_limit")))
# 上调到 3 后可再签一张
s, _ = setq(r, DAY, 3)
check("上调到 3 200", s == 200, str(s))
s, _ = issue(c, r)
check("上调后第 3 张 201", s == 201, str(s))
s, e = issue(nxt("CAL"), r)
check("再次超限 422", s == 422 and err_code(e) == "daily_quota_exceeded", str(s))
# 0 上限：另一基准证书当日配置 0
s, _ = setq(r2, DAY, 0)
check("上限置 0 201/200（已用为0允许）", s in (200, 201), str(s))
z = nxt("CAL")
s, e = issue(z, r2)
check("上限0 当日签发 422 点名 r2",
      s == 422 and err_code(e) == "daily_quota_exceeded"
      and err_detail(e)["parent_cert_no"] == r2 and err_detail(e)["daily_limit"] == 0,
      json.dumps(e, ensure_ascii=False))
check("0 上限证书不落库", not exists(z))
# UTC 切日：东八区 2026-01-06T07:59 -> UTC 2026-01-05T23:59（仍属限额日）
zb = nxt("CAL")
s, e = call("POST", "/api/v1/certificates", {
    "cert_no": zb, "device_id": "DEV-" + zb,
    "valid_from": iso(0), "valid_to": iso(1200),
    "calibrated_at": (t0 + timedelta(days=5, hours=7, minutes=59)).isoformat(),
    "conclusion": "PASS", "parent_cert_no": r2})
check("UTC 边界前一分钟仍计入限额日 -> 422", s == 422 and err_code(e) == "daily_quota_exceeded", str(s))
za = nxt("CAL")
s, _ = call("POST", "/api/v1/certificates", {
    "cert_no": za, "device_id": "DEV-" + za,
    "valid_from": iso(0), "valid_to": iso(1200),
    "calibrated_at": (t0 + timedelta(days=5, hours=8)).isoformat(),
    "conclusion": "PASS", "parent_cert_no": r2})
check("UTC 边界整点（UTC 次日）未配置 -> 沿用原规则 201", s == 201, str(s))
# 带显式 UTC 偏移的校准时刻：2026-01-05T23:30:00+00:00 即限额日 UTC 当日
zn = nxt("CAL")
s, e = call("POST", "/api/v1/certificates", {
    "cert_no": zn, "device_id": "DEV-" + zn,
    "valid_from": "2026-01-01T00:00:00+00:00", "valid_to": "2029-01-01T00:00:00+00:00",
    "calibrated_at": "2026-01-05T23:30:00+00:00",
    "conclusion": "PASS", "parent_cert_no": r})
check("显式 UTC 校准时刻按 UTC 计当日 -> 422",
      s == 422 and err_code(e) == "daily_quota_exceeded", str(s))
# 不同 UTC 日不受该限额影响
s, _ = issue(nxt("CAL"), r, cat_day=8)
check("其他 UTC 日签发 201", s == 201, str(s))
# FAIL 结论同样计扣（cat_day 默认即限额日）
f = nxt("CAL")
s, e = issue(f, r)
check("FAIL 证书超限同样扣额拦截",
      s == 422 and err_code(e) == "daily_quota_exceeded", str(s))
check("FAIL 超限不落库", not exists(f))

# ---------------------------------------------------------------------------
print("== C. 撤销/失效不退还已用次数 ==")
s, rev = call("POST", f"/api/v1/certificates/{a}/revoke",
              {"reason": "上限用满后撤销一张，已用不退还", "operator": "test"})
check("撤销已计数证书 200", s == 200, str(s))
s, body = getq(r, DAY)
check("撤销后 used 不减少（仍=3）", body["used"] == 3, json.dumps(body, ensure_ascii=False))
s, e = issue(nxt("CAL"), r)
check("撤销不退额：新签发仍 422", s == 422 and err_code(e) == "daily_quota_exceeded", str(s))
# 撤销 r2 本身导致下游级联失效后，其当日 0 上限仍拦截新签发（限额独立存在）
s, _ = call("POST", f"/api/v1/certificates/{r2}/revoke", {"reason": "撤销基准2"})
check("撤销基准2 200", s == 200, str(s))
s, e = issue(nxt("CAL"), r2, cat_day=5, cat_hh=9)
check("上游已撤销时签发按既有规则先报引用失效（422 reference_not_active），不产生配额副作用",
      s == 422 and err_code(e) == "reference_not_active", f"{s} {err_code(e)}")

# ---------------------------------------------------------------------------
print("== D. 交接批次：合并计数 / 整批不落库 / 幂等重放不重复计数 ==")
rd, _ = ref()
s, _ = setq(rd, DAY, 2)
check("批次基准设限 2 200/201", s in (200, 201), str(s))
k1 = nxt("BATCH")
items = [batch_item(nxt("BC"), rd), batch_item(nxt("BC"), rd), batch_item(nxt("BC"), rd)]
bc_nos = [it["cert_no"] for it in items]
s, e = batch(k1, items)
check("批内3张同键超上限 -> 422 daily_quota_exceeded",
      s == 422 and err_code(e) == "daily_quota_exceeded"
      and err_detail(e).get("parent_cert_no") == rd and err_detail(e).get("day") == DAY,
      json.dumps(e, ensure_ascii=False))
check("超限批次整批不落库（3 张均不存在）", not any(exists(n) for n in bc_nos))
s, b = call("GET", f"/api/v1/certificate-batches/{k1}")
check("超限批次无首次结果快照 404", s == 404, str(s))
s, body = getq(rd, DAY)
check("整批回滚后 used=0", body["used"] == 0, json.dumps(body))
# 2 张同键 = 恰好上限 -> 整批成功（合并计数为 2）
k2 = nxt("BATCH")
ok_items = [batch_item(nxt("BC"), rd), batch_item(nxt("BC"), rd)]
ok_nos = [it["cert_no"] for it in ok_items]
s, b = batch(k2, ok_items)
check("批内2张恰满上限 -> 201 整批成功", s == 201 and b["count"] == 2, f"{s}")
check("整批 2 张均落库", all(exists(n) for n in ok_nos))
s, body = getq(rd, DAY)
check("批次合并计数 used=2", body["used"] == 2, json.dumps(body))
# 再提交一张的批次超限 -> 整批不落库
k3 = nxt("BATCH")
one = [batch_item(nxt("BC"), rd)]
s, e = batch(k3, one)
check("用满后新批次 1 张仍 422", s == 422 and err_code(e) == "daily_quota_exceeded", str(s))
check("新批次整批不落库", not exists(one[0]["cert_no"]))
# 已存在批次幂等重放：返回首次结果、不重复计数（即使上限已无法再签）
s, b = batch(k2, ok_items)
check("成功批次同键重放 200 且 count=2", s == 200 and b["count"] == 2, str(s))
s, body = getq(rd, DAY)
check("重放不重复计数 used 仍=2", body["used"] == 2, str(body.get("used")))
# 同键异内容 409（既有契约）
diff = [dict(ok_items[0])]
diff[0]["device_name"] = "改过的内容"
s, e = batch(k2, diff)
check("同键异内容 409 batch_conflict", s == 409 and err_code(e) == "batch_conflict", str(s))
# 基准证书（REFERENCE）不计入任何上游额度；混合批次中基准 + 限额内普通证书
k4 = nxt("BATCH")
ref_item = {
    "cert_no": nxt("BREF"), "kind": "REFERENCE", "device_id": "DEV-MIX-REF",
    "valid_from": iso(200), "valid_to": iso(1400), "calibrated_at": iso(200),
}
s, e = batch(k4, [ref_item])
check("仅含基准证书的批次不受额度限制 -> 201", s == 201, f"{s} {err_code(e) if isinstance(e, dict) else ''}")
# 批内引用：普通证书引用本批另一普通证书——按其直接上游（本批证书，未配置）
# 计数；本批证书又直接引用 rd（已用满）时，按 rd 计数的那张必须受阻，整批不落库
k5 = nxt("BATCH")
inner_no, outer_no = nxt("IN"), nxt("OUT")
mix_items = [
    batch_item(inner_no, rd),  # 直接上游 rd：rd 已满 -> 受阻
    {
        "cert_no": outer_no, "kind": "CALIBRATION", "device_id": "DEV-" + outer_no,
        "valid_from": iso(0), "valid_to": iso(1200),
        "calibrated_at": iso(CAT_UTC_DAY, 8),
        "conclusion": "PASS", "parent_cert_no": inner_no,
    },
]
s, e = batch(k5, mix_items)
check("批内链：根引用已用满上游 -> 422 整批不落库",
      s == 422 and err_code(e) == "daily_quota_exceeded"
      and err_detail(e).get("cert_no") == inner_no,
      json.dumps(e, ensure_ascii=False)[:300])
check("批内链整批不落库", not exists(inner_no) and not exists(outer_no))

# ---------------------------------------------------------------------------
print("== E. 双人复核批准：真正签发时扣额，超限保持 PENDING ==")
rr, _ = ref()
s, _ = setq(rr, DAY, 1)
check("复核基准设限 1 200/201", s in (200, 201), str(s))
ap1, ac1 = nxt("APL"), nxt("RVW")
s, b = call("POST", "/api/v1/certificate-review-requests", {
    "application_no": ap1, "cert_no": ac1, "device_id": "DEV-" + ac1,
    "device_name": "待复核1", "valid_from": iso(0), "valid_to": iso(1200),
    "calibrated_at": iso(CAT_UTC_DAY, 8), "conclusion": "PASS",
    "parent_cert_no": rr}, entry=ENTRY)
check("提交申请1 201 PENDING", s == 201 and b["status"] == "PENDING", str(s))
s, b = call("POST", f"/api/v1/certificate-review-requests/{ap1}/decision",
            {"decision": "APPROVE"}, reviewer=REVIEWER)
check("批准1 201 真正签发", s == 201 and b["status"] == "APPROVED" and b["certificate"], f"{s}")
ap2, ac2 = nxt("APL"), nxt("RVW")
s, _ = call("POST", "/api/v1/certificate-review-requests", {
    "application_no": ap2, "cert_no": ac2, "device_id": "DEV-" + ac2,
    "device_name": "待复核2", "valid_from": iso(0), "valid_to": iso(1200),
    "calibrated_at": iso(CAT_UTC_DAY, 8), "conclusion": "PASS",
    "parent_cert_no": rr}, entry=ENTRY)
s, e = call("POST", f"/api/v1/certificate-review-requests/{ap2}/decision",
            {"decision": "APPROVE"}, reviewer=REVIEWER)
check("批准2超限 422 daily_quota_exceeded",
      s == 422 and err_code(e) == "daily_quota_exceeded", json.dumps(e, ensure_ascii=False)[:200])
check("超限证书未落库", not exists(ac2))
s, b = call("GET", f"/api/v1/certificate-review-requests/{ap2}", entry=ENTRY)
check("超限后申请仍 PENDING（可调整后再批准）",
      s == 200 and b["status"] == "PENDING" and b["certificate"] is None, json.dumps(b, ensure_ascii=False)[:200])
s, _ = setq(rr, DAY, 2)
check("上调上限到 2", s == 200, str(s))
s, b = call("POST", f"/api/v1/certificate-review-requests/{ap2}/decision",
            {"decision": "APPROVE"}, reviewer=REVIEWER)
check("上调后再次批准 201 签发", s == 201 and b["status"] == "APPROVED", f"{s}")
check("批准后证书落库", exists(ac2))
# 驳回不扣额
ap3, ac3 = nxt("APL"), nxt("RVW")
call("POST", "/api/v1/certificate-review-requests", {
    "application_no": ap3, "cert_no": ac3, "device_id": "DEV-" + ac3,
    "valid_from": iso(0), "valid_to": iso(1200),
    "calibrated_at": iso(CAT_UTC_DAY, 8), "conclusion": "PASS",
    "parent_cert_no": rr}, entry=ENTRY)
s, b = call("POST", f"/api/v1/certificate-review-requests/{ap3}/decision",
            {"decision": "REJECT", "reason": "资料不全"}, reviewer=REVIEWER)
check("驳回 201 REJECTED 不扣额", s == 201 and b["status"] == "REJECTED", str(s))
check("驳回不产生证书", not exists(ac3))
s, body = getq(rr, DAY)
check("驳回不计数 used=2", body["used"] == 2, json.dumps(body))

# ---------------------------------------------------------------------------
print("== F. 并发：N 路单张签发绝不超额（含既有 issuance_busy 竞争保护） ==")
rc, _ = ref()
CAP = 3
N = 6
s, _ = setq(rc, DAY, CAP)
check(f"并发基准设限 {CAP} 200/201", s in (200, 201), str(s))
statuses = []
sl = threading.Lock()
# 错开发车时机，降低同一行 SHARE->FOR UPDATE 锁升级的彼此对撞（既有 issuance_busy
# 死锁保护与本限额无关）；本用例只验证不变量：并发下成功签发数绝不超过上限。
def issue_worker(i):
    if i:
        time.sleep(0.05 * i)
    no = nxt("CC")
    st, _ = issue(no, rc)
    with sl:
        statuses.append((st, no))


threads = [threading.Thread(target=issue_worker, args=(i,)) for i in range(N)]
for t in threads:
    t.start()
for t in threads:
    t.join()
ok_n = [no for st, no in statuses if st == 201]
blocked_n = [no for st, no in statuses if st == 422]
busy_n = [st for st, no in statuses if st == 409]
other = sorted({st for st, _ in statuses} - {201, 422, 409})
check("成功签发数绝不超过上限",
      len(ok_n) <= CAP and not other,
      f"201={len(ok_n)} 422={len(blocked_n)} 409={len(busy_n)} 其他={other}")
check("未成功（422 或既有 409 竞争保护）的证书均未落库",
      not any(exists(no) for st, no in statuses if st != 201),
      "")
s, body = getq(rc, DAY)
check(f"并发后 used == 成功落库数且 <= {CAP}（绝不超额）",
      body["used"] == len(ok_n) and body["used"] <= CAP,
      json.dumps(body))

print("== G. 并发：限额调整 × 签发 按提交次序串行 ==")
rd2, _ = ref()
s, _ = setq(rd2, DAY, 100)
check("初始大限额 100 200/201", s in (200, 201), str(s))
outcomes = []
ol = threading.Lock()
start = threading.Event()


def lower_worker():
    start.wait()
    st, body = setq(rd2, DAY, 0)
    with ol:
        outcomes.append(("lower", st, body.get("used") if isinstance(body, dict) else None))


def issue_worker2(i):
    start.wait()
    if i:
        time.sleep(0.05 * i)
    no = nxt("CX")
    st, _ = issue(no, rd2)
    with ol:
        outcomes.append(("issue", st, no))


ts = [threading.Thread(target=lower_worker)] + [
    threading.Thread(target=issue_worker2, args=(i,)) for i in range(6)
]
for t in ts:
    t.start()
start.set()
for t in ts:
    t.join()
s, body = getq(rd2, DAY)
# 若下调成功：成功签发数必须恰等于下调时记录的 used；若下调被拒（已有>0），限额仍100
lower_rows = [o for o in outcomes if o[0] == "lower"]
issue_201 = [o for o in outcomes if o[0] == "issue" and o[1] == 201]
issue_422 = [o for o in outcomes if o[0] == "issue" and o[1] == 422]
issue_busy = [o for o in outcomes if o[0] == "issue" and o[1] == 409]
other_iss = sorted({o[1] for o in outcomes if o[0] == "issue"} - {201, 422, 409})
check("签发仅出现 201/422/409（既有 issuance_busy 竞争保护），无其他异常",
      not other_iss and all(lr[1] in (200, 422) for lr in lower_rows),
      json.dumps(outcomes, ensure_ascii=False, default=str)[:300])
if lower_rows and lower_rows[0][1] == 200:
    used_at_lower = lower_rows[0][2]
    check("下调为0成功：成功签发数 == 下调时已用数（按提交次序，绝不超额）",
          len(issue_201) == used_at_lower and body["used"] == used_at_lower,
          f"201={len(issue_201)} used_at_lower={used_at_lower} now={body['used']} "
          f"422={len(issue_422)} 409={len(issue_busy)}")
    check("下调后到达的签发要么 422 要么 409（竞争保护），无额外成功",
          len(issue_201) + len(issue_422) + len(issue_busy) == 6,
          f"{len(issue_201)}/{len(issue_422)}/{len(issue_busy)}")
else:
    check("下调被拒（已有签发>0）：used 与成功数一致且不超额",
          body["used"] == len(issue_201),
          json.dumps([lower_rows, body["used"]], ensure_ascii=False)[:300])
check("终态 used 与成功落库证书数一致", body["used"] == len(issue_201),
      f"used={body['used']} issued={len(issue_201)}")

# ---------------------------------------------------------------------------
print("== H. 未配置上限完全沿用原签发规则（批次/单张/契约） ==")
ru, _ = ref()
s, _ = issue(nxt("CAL"), ru)
check("未设限单张 201", s == 201, str(s))
ku = nxt("BATCH")
uitems = [batch_item(nxt("BU"), ru) for _ in range(3)]
s, b = batch(ku, uitems)
check("未设限批次3张 201", s == 201 and b["count"] == 3, f"{s}")
s, e = getq(ru, DAY)
check("未设限查询仍 404", s == 404 and err_code(e) == "quota_not_found", str(s))
# 根索引端点清单包含新接口
s, idx = call("GET", "/")
listed = "\n".join(idx.get("endpoints", []))
check("根索引列出 PUT/GET daily-quotas",
      "PUT  /api/v1/certificates/{cert_no}/daily-quotas/{day}" in listed
      and "GET  /api/v1/certificates/{cert_no}/daily-quotas/{day}" in listed,
      listed[:200])

# ---------------------------------------------------------------------------
passed = sum(1 for _, ok, _ in results if ok)
failed = sum(1 for _, ok, _ in results if not ok)
print()
print(f"结果: PASS={passed} FAIL={failed}")
sys.exit(1 if failed else 0)
