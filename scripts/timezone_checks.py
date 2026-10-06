"""无时区（按 UTC）与带偏移时间混用的校验回归：单张签发、交接批次、双人复核。

背景：valid_from 不带时区、valid_to 带偏移时，Pydantic 校验器直接比较
naive/aware datetime 抛 TypeError，客户端收到 500；修复约定为“无时区证书时间
视为 UTC”，按同一绝对时刻校验 valid_from < valid_to 及 calibrated_at 落在
半开区间 [valid_from, valid_to)；非法区间走既有 422 参数错误格式，不写入
证书或申请，批次整批不落库。

覆盖：
- 单张/基准：混用合法 201、混用非法 422（不 500）且证书不落库；
- 半开区间：calibrated_at 恰等于 valid_to 拒绝；
- 批次：混用非法项整批不落库；合法批次同键重放返回首次快照，异内容 409；
- 复核申请：混用非法 422 且申请不落库；混用合法提交后批准同事务真正签发；
  同申请号重放 200、异内容 409；
- UTC 日限额：显式偏移输入与 naive（按 UTC）输入计入同一 UTC 日期，
  达到上限后第 3 张 422 daily_quota_exceeded 且不落库。

直接压 HTTP 接口（urllib，无第三方依赖）。服务需已启动。
用法: python3 scripts/timezone_checks.py [base_url] [management_key] [entry_key] [reviewer_key]
"""
import json
import sys
import urllib.error
import urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8080"
MK = sys.argv[2] if len(sys.argv) > 2 else "caltrace-delivery-admin"
ENTRY = sys.argv[3] if len(sys.argv) > 3 else "caltrace-entry-key"
REV1 = sys.argv[4] if len(sys.argv) > 4 else "caltrace-review-key-1"
results = []


def call(method, path, body=None, headers=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        BASE + path, data=data, method=method,
        headers={"Content-Type": "application/json", **(headers or {})},
    )
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def check(name, cond, detail=""):
    results.append((name, cond, detail))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + ("" if cond else f" :: {detail}"))


def ref(cert_no, device_id, vf, vt, cal=None):
    return call("POST", "/api/v1/reference-certificates", {
        "cert_no": cert_no, "device_id": device_id,
        "valid_from": vf, "valid_to": vt, "calibrated_at": cal or vf,
    })


def cal(cert_no, device_id, parent, vf, vt, cal=None, conclusion="PASS"):
    return call("POST", "/api/v1/certificates", {
        "cert_no": cert_no, "device_id": device_id, "parent_cert_no": parent,
        "valid_from": vf, "valid_to": vt, "calibrated_at": cal or vf,
        "conclusion": conclusion,
    })


H_MGMT = {"X-Management-Key": MK}
H_ENTRY = {"X-Entry-Key": ENTRY}
H_REV1 = {"X-Reviewer-Key": REV1}

print("== 1. 基准证书：naive valid_from + 带偏移 valid_to（修复前 500）==")
s, body = ref(
    "REF-TZ-001", "DEV-TZ-REF",
    "2026-01-01T00:00:00",                    # naive -> UTC
    "2027-01-01T00:00:00+08:00",               # 2026-12-31T16:00Z
)
check("混用合法基准 201", s == 201, f"{s} {body}")
if s == 201:
    c = body["certificate"]
    check("valid_from 按 UTC 落库", c["valid_from"].startswith("2026-01-01T00:00:00"), c["valid_from"])
    check("valid_to 按同一绝对时刻换算为 UTC", c["valid_to"].startswith("2026-12-31T16:00:00"), c["valid_to"])

print("== 2. 单张签发：混用非法区间返回既有参数错误 422（不是 500），不落库 ==")
s, body = cal(
    "CAL-TZ-BAD-1", "DEV-TZ-X", "REF-TZ-001",
    "2027-01-01T00:00:00",                     # =2027-01-01Z
    "2026-01-01T00:00:00+08:00",               # =2025-12-31T16:00Z
)
check("绝对时刻倒挂 -> 422", s == 422, f"{s} {body}")
check("422 为既有 detail[] 参数错误格式", s == 422 and isinstance(body.get("detail"), list))
s, _ = call("GET", "/api/v1/certificates/CAL-TZ-BAD-1")
check("非法证书未落库", s == 404, f"GET -> {s}")

# 本地时钟数字递增、但换算 UTC 后绝对时刻倒挂
s, _ = cal(
    "CAL-TZ-BAD-2", "DEV-TZ-X", "REF-TZ-001",
    "2026-03-01T08:00:00",                     # 08:00Z
    "2026-03-01T09:00:00+08:00",               # 01:00Z
)
check("本地升序但绝对倒挂 -> 422", s == 422, f"{s}")

print("== 3. calibrated_at 按同一绝对时刻落在半开区间 ==")
s, _ = cal(
    "CAL-TZ-EDGE", "DEV-TZ-E", "REF-TZ-001",
    "2026-03-01T00:00:00",
    "2027-03-01T00:00:00+08:00",              # 2027-02-28T16:00Z
    "2027-03-01T00:00:00+08:00",               # 恰为终点（不含）
)
check("calibrated_at == valid_to -> 422", s == 422, f"{s}")
s, body = cal(
    "CAL-TZ-OK-1", "DEV-TZ-OK1", "REF-TZ-001",
    "2026-03-01T00:00:00",
    "2027-03-01T00:00:00+08:00",
    "2026-12-31T15:59:00Z",                    # 终点前 1 分钟
)
check("终点前 1 分钟 -> 201", s == 201, f"{s} {str(body)[:160]}")
if s == 201:
    check("calibrated_at 绝对时刻不变",
          body["certificate"]["calibrated_at"].startswith("2026-12-31T15:59:00"))

print("== 4. 交接批次：混用非法项整批不落库 ==")
s, _ = call("POST", "/api/v1/certificate-batches", {
    "batch_key": "TZ-BATCH-BAD",
    "certificates": [
        {"kind": "CALIBRATION", "cert_no": "CAL-TZ-B0", "device_id": "DEV-TZ-B0",
         "parent_cert_no": "REF-TZ-001",
         "valid_from": "2026-04-01T00:00:00", "valid_to": "2026-10-01T00:00:00+08:00",
         "calibrated_at": "2026-04-02T10:00:00+08:00", "conclusion": "PASS"},
        {"kind": "CALIBRATION", "cert_no": "CAL-TZ-BBAD", "device_id": "DEV-TZ-B1",
         "parent_cert_no": "REF-TZ-001",
         "valid_from": "2027-01-01T00:00:00", "valid_to": "2026-01-01T00:00:00+08:00",
         "calibrated_at": "2027-01-01T00:00:00", "conclusion": "PASS"},
    ],
})
check("批次含混用非法项 -> 422", s == 422, f"{s}")
s0, _ = call("GET", "/api/v1/certificates/CAL-TZ-B0")
s1, _ = call("GET", "/api/v1/certificates/CAL-TZ-BBAD")
check("整批不落库（含合法项）", s0 == 404 and s1 == 404, f"{s0}/{s1}")
s, _ = call("GET", "/api/v1/certificate-batches/TZ-BATCH-BAD")
check("批次键不落库", s == 404, f"{s}")

print("== 5. 合法混用批次：同请求重放原结果、异内容冲突 ==")
batch = {
    "batch_key": "TZ-BATCH-OK",
    "certificates": [
        {"kind": "CALIBRATION", "cert_no": "CAL-TZ-BM1", "device_id": "DEV-TZ-BM1",
         "parent_cert_no": "REF-TZ-001",
         "valid_from": "2026-05-01T00:00:00", "valid_to": "2026-11-01T00:00:00+08:00",
         "calibrated_at": "2026-05-02T09:00:00+08:00", "conclusion": "PASS"},
        {"kind": "CALIBRATION", "cert_no": "CAL-TZ-BM2", "device_id": "DEV-TZ-BM2",
         "parent_cert_no": "REF-TZ-001",
         "valid_from": "2026-05-01T00:00:00Z", "valid_to": "2026-11-01T00:00:00Z",
         "calibrated_at": "2026-05-03T09:00:00", "conclusion": "PASS"},
    ],
}
s, b1 = call("POST", "/api/v1/certificate-batches", batch)
check("合法混用批次 201", s == 201, f"{s} {str(b1)[:160]}")
s, b2 = call("POST", "/api/v1/certificate-batches", batch)
check("同键同请求重放 200", s == 200, f"{s}")
check("重放返回首次结果快照", s == 200 and b2.get("items") == b1.get("items"))
diff = json.loads(json.dumps(batch))
diff["certificates"][1]["device_name"] = "异内容"
s, body = call("POST", "/api/v1/certificate-batches", diff)
check("同键异内容 -> 409 batch_conflict",
      s == 409 and body.get("error", {}).get("code") == "batch_conflict", f"{s} {body}")

print("== 6. 双人复核：混用非法申请不落库；合法申请批准真正签发 ==")
s, _ = call("POST", "/api/v1/certificate-review-requests", {
    "application_no": "RVW-TZ-BAD", "cert_no": "CAL-TZ-RVBAD", "device_id": "DEV-TZ-RV",
    "parent_cert_no": "REF-TZ-001", "conclusion": "PASS",
    "valid_from": "2027-01-01T00:00:00", "valid_to": "2026-01-01T00:00:00+08:00",
    "calibrated_at": "2027-01-01T00:00:00",
}, H_ENTRY)
check("混用非法申请 -> 422", s == 422, f"{s}")
s, _ = call("GET", "/api/v1/certificate-review-requests/RVW-TZ-BAD", headers=H_ENTRY)
check("非法申请不落库", s == 404, f"{s}")

review = {
    "application_no": "RVW-TZ-001", "cert_no": "CAL-TZ-RV1", "device_id": "DEV-TZ-RV1",
    "parent_cert_no": "REF-TZ-001", "conclusion": "PASS",
    "valid_from": "2026-06-01T00:00:00",                    # naive UTC
    "valid_to": "2026-12-01T00:00:00+08:00",               # 2026-11-30T16:00Z
    "calibrated_at": "2026-06-02T10:00:00+08:00",            # 2026-06-02T02:00Z
}
s, _ = call("POST", "/api/v1/certificate-review-requests", review, H_ENTRY)
check("混用合法申请 201", s == 201, f"{s}")
s, _ = call("POST", "/api/v1/certificate-review-requests", review, H_ENTRY)
check("同申请号同内容重放 200", s == 200, f"{s}")
diff = json.loads(json.dumps(review))
diff["device_name"] = "异内容"
s, body = call("POST", "/api/v1/certificate-review-requests", diff, H_ENTRY)
check("同申请号异内容 -> 409 review_request_conflict",
      s == 409 and body.get("error", {}).get("code") == "review_request_conflict", f"{s} {body}")
s, body = call("POST", "/api/v1/certificate-review-requests/RVW-TZ-001/decision",
               {"decision": "APPROVE"}, H_REV1)
check("批准 201 同事务真正签发", s == 201, f"{s} {str(body)[:200]}")
if s == 201:
    cert = body["certificate"]
    check("批准证书 valid_to=2026-11-30T16:00Z",
          cert["valid_to"].startswith("2026-11-30T16:00:00"), cert["valid_to"])
    check("批准证书 calibrated_at=2026-06-02T02:00Z",
          cert["calibrated_at"].startswith("2026-06-02T02:00:00"), cert["calibrated_at"])

print("== 7. UTC 日限额：显式偏移与 naive 输入同一绝对时刻计入同一 UTC 日 ==")
s, body = call("PUT", "/api/v1/certificates/REF-TZ-001/daily-quotas/2026-08-15",
               {"daily_limit": 2}, H_MGMT)
check("配置 UTC 2026-08-15 上限 2", s == 201, f"{s} {body}")
# +08: 08-15T07:59 = UTC 08-14T23:59 -> 前一日（无配置，放行）
s, body = cal("CAL-TZ-Q0", "DEV-TZ-Q0", "REF-TZ-001",
               "2026-08-01T00:00:00Z", "2026-09-01T00:00:00Z",
               "2026-08-15T07:59:00+08:00")
check("+08 07:59 计入 UTC 08-14 放行", s == 201, f"{s} {str(body)[:160]}")
# naive 08-15T00:00 -> UTC 08-15（第 1 次）
s, _ = cal("CAL-TZ-Q1", "DEV-TZ-Q1", "REF-TZ-001",
           "2026-08-01T00:00:00", "2026-09-01T00:00:00",
           "2026-08-15T00:00:00")
check("naive 08-15T00:00 计入 UTC 08-15（第1次）", s == 201, f"{s}")
# +08: 08-15T08:00 = UTC 08-15T00:00（第 2 次，同一绝对时刻同日）
s, _ = cal("CAL-TZ-Q2", "DEV-TZ-Q2", "REF-TZ-001",
           "2026-08-01T00:00:00Z", "2026-09-01T00:00:00Z",
           "2026-08-15T08:00:00+08:00")
check("+08 08:00 同一绝对时刻同日（第2次）", s == 201, f"{s}")
s, body = cal("CAL-TZ-Q3", "DEV-TZ-Q3", "REF-TZ-001",
               "2026-08-01T00:00:00Z", "2026-09-01T00:00:00Z",
               "2026-08-15T12:00:00+08:00")
check("第 3 次 -> 422 daily_quota_exceeded",
      s == 422 and body.get("error", {}).get("code") == "daily_quota_exceeded",
      f"{s} {str(body)[:200]}")
if s == 422:
    d = body["error"]["details"]
    check("details: day=2026-08-15 used=2",
          d.get("day") == "2026-08-15" and d.get("used") == 2, str(d))
s, _ = call("GET", "/api/v1/certificates/CAL-TZ-Q3")
check("超限证书不落库", s == 404, f"{s}")
s, body = call("GET", "/api/v1/certificates/REF-TZ-001/daily-quotas/2026-08-15", headers=H_MGMT)
check("限额查询 used=2 remaining=0",
      s == 200 and body.get("used") == 2 and body.get("remaining") == 0, f"{s} {body}")

fails = [n for n, ok, _ in results if not ok]
print(f"\n结果: PASS={len(results) - len(fails)} FAIL={len(fails)}")
if fails:
    print("失败项:", fails)
    sys.exit(1)
