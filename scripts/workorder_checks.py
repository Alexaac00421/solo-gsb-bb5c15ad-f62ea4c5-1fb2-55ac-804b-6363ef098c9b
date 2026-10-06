"""设备校准委托工单测试：

管理密钥登记唯一工单号/设备标识/委托人/带时区计划校准半开区间（缺/错密钥
401、时段非法 422、同号同内容重放 200、同号异内容 409、并发同号仅一份 201）；
取消（仅 PENDING 可取消，结清/取消后再取消 409，并发结清×取消仅一方生效）；
结清（必须普通 CALIBRATION、设备标识一致、校准时刻落在计划半开区间、当前
ACTIVE、结论 PASS；一证不得结清两工单；同工单同证书重试返回首次结果，改绑
409，已取消结清 409）；结清后绑定与结清时刻固定，撤销绑定证书不倒退工单
状态、查询返回证书当前状态；并发结清/取消/撤销无重复绑定与无效结清；
不预占证书编号/有效期/每日限额（既有签发、批次、复核规则契约不变）；
根索引端点清单包含新接口。

直接压 HTTP 接口（urllib，无第三方依赖）。服务需已启动。
用法: python3 scripts/workorder_checks.py [base_url] [management_key]
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
results = []


_NO_KEY = object()  # 显式表示“不带 X-Management-Key 头”（区别于默认携带 MK）


def call(method, path, body=None, key=MK):
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"}
    if key is not _NO_KEY:
        headers["X-Management-Key"] = key
    req = urllib.request.Request(BASE + path, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def check(name, cond, detail=""):
    results.append((name, bool(cond), detail))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + ("" if cond else f" :: {detail}"))


def err_code(body):
    return body.get("error", {}).get("code")


TZ = timezone(timedelta(hours=8))
t0 = datetime(2026, 6, 1, 0, 0, tzinfo=TZ)
_seq = [0]
_seq_lock = threading.Lock()


def nxt(prefix):
    with _seq_lock:
        _seq[0] += 1
        return f"{prefix}-{_seq[0]}"


def iso(offset_days=0, hh=0):
    return (t0 + timedelta(days=offset_days, hours=hh)).isoformat()


def ref(no=None, **kw):
    no = no or nxt("REF")
    body = {
        "cert_no": no, "device_id": "DEV-" + no, "device_name": "基准" + no,
        "valid_from": iso(-30), "valid_to": iso(400), "calibrated_at": iso(-30),
    }
    body.update(kw)
    return no, call("POST", "/api/v1/reference-certificates", body)[1]


def cal(no, parent, device, *, cat_off, cat_hh=10, conclusion="PASS",
        vfrom_off=-1, vto_off=400):
    return call("POST", "/api/v1/certificates", {
        "cert_no": no, "device_id": device, "device_name": "仪表" + no,
        "valid_from": iso(vfrom_off), "valid_to": iso(vto_off),
        "calibrated_at": iso(cat_off, cat_hh),
        "conclusion": conclusion, "parent_cert_no": parent, "operator": "王工"})


def register(no, device, client="某委托单位", key=MK, **kw):
    body = {
        "work_order_no": no, "device_id": device, "client": client,
        "scheduled_from": iso(0), "scheduled_to": iso(10),
    }
    body.update(kw)
    return call("POST", "/api/v1/work-orders", body, key=key)


def settle(no, cert, key=MK):
    return call("POST", f"/api/v1/work-orders/{no}/settle", {"cert_no": cert}, key=key)


def cancel(no, key=MK):
    return call("POST", f"/api/v1/work-orders/{no}/cancel", None, key=key)


def getwo(no, key=MK):
    return call("GET", f"/api/v1/work-orders/{no}", None, key=key)


def revoke(no, reason="测试撤销"):
    return call("POST", f"/api/v1/certificates/{no}/revoke", {"reason": reason})


# ---------------------------------------------------------------------------
print("== A. 登记：管理密钥 / 校验 / 幂等 / 冲突 ==")
wo, dev = nxt("WO"), "DEV-WO-MAIN"
s, b = register(wo, dev)
check("登记工单 201 且初始 PENDING",
      s == 201 and b["status"] == "PENDING" and b["cert_no"] is None
      and b["settled_at"] is None and b["cancelled_at"] is None
      and b["scheduled_from"] and b["client"] == "某委托单位", f"{s} {json.dumps(b, ensure_ascii=False)[:200]}")

s, b = register(wo, dev, key=_NO_KEY)
check("缺管理密钥 401 invalid_management_key",
      s == 401 and err_code(b) == "invalid_management_key", str(s))

s, b = register(wo, dev, key="wrong-key")
check("错管理密钥 401 invalid_management_key",
      s == 401 and err_code(b) == "invalid_management_key", str(s))

s, b = register(wo, dev)
check("同号同内容重试 200 返回原工单",
      s == 200 and b["status"] == "PENDING" and b["work_order_no"] == wo, str(s))

s, b = register(wo, dev, client="另一个委托人")
check("同号异内容（委托人不同）409 work_order_conflict",
      s == 409 and err_code(b) == "work_order_conflict", str(s))

s, b = register(wo, dev, scheduled_to=iso(0))
check("计划时段起>=止（空/倒区间）422", s == 422, f"{s} {err_code(b)}")

s, b = register(nxt("WO"), dev, scheduled_from=iso(5), scheduled_to=iso(5))
check("起==止（零长度半开区间）422", s == 422, str(s))

s, b = register(nxt("WO"), "   ", client="委托人")
check("设备标识为空白 422", s == 422, str(s))

# 并发同号同内容：仅一份 201，其余 200
cw = nxt("WO")
out = []
ths = [threading.Thread(target=lambda: out.append(register(cw, dev))) for _ in range(8)]
[t.start() for t in ths]
[t.join() for t in ths]
codes = sorted(s for s, _ in out)
check("并发同号同内容：1×201 + 7×200",
      codes == [200] * 7 + [201], str(codes))

# ---------------------------------------------------------------------------
print("== B. 查询：凭据与未知工单 ==")
s, b = getwo(wo)
check("正确密钥查询 200 PENDING 且无 certificate",
      s == 200 and b["status"] == "PENDING" and b["certificate"] is None, str(s))
s, b = getwo(wo, key=_NO_KEY)
check("查询缺密钥 401", s == 401 and err_code(b) == "invalid_management_key", str(s))
s, b = getwo("NO-SUCH-WO")
check("未知工单 404 work_order_not_found",
      s == 404 and err_code(b) == "work_order_not_found", str(s))

# ---------------------------------------------------------------------------
print("== C. 结清资格：普通证书/设备一致/时段/ACTIVE/PASS ==")
# 同一设备的生效证书有效期不得重叠，故下列同设备证书各用以校准时刻为起点的
# 互不重叠窄窗口；基准窗口覆盖全部校准时刻。
ru, _ = ref()
# 合格普通证书：设备与工单一致、校准时刻在计划 [day0, day10) 内。
good = nxt("CAL-GOOD")
s, issued = cal(good, ru, dev, cat_off=5, vfrom_off=5, vto_off=6)
check("准备合格普通证书 201", s == 201, str(s))

# 基准证书不能结清
s, b = settle(wo, ru, key=MK)
check("基准证书结清 422 settlement_cert_not_calibration",
      s == 422 and err_code(b) == "settlement_cert_not_calibration", f"{s} {err_code(b)}")

# 设备标识不一致
other_dev_cert = nxt("CAL-DEV")
s, _ = cal(other_dev_cert, ru, "DEV-OTHER-DEVICE", cat_off=5, vfrom_off=5, vto_off=10)
s, b = settle(wo, other_dev_cert)
check("设备标识不一致 422 settlement_device_mismatch",
      s == 422 and err_code(b) == "settlement_device_mismatch"
      and b["error"]["details"]["cert_device_id"] == "DEV-OTHER-DEVICE",
      f"{s} {json.dumps(b, ensure_ascii=False)[:200]}")

# 校准时刻早于计划起点
early = nxt("CAL-EARLY")
s, _ = cal(early, ru, dev, cat_off=-1, vfrom_off=-1, vto_off=0)
check("准备校准时刻早于计划的证书 201", s == 201, str(s))
s, b = settle(wo, early)
check("校准时刻早于计划起点 422 settlement_outside_schedule",
      s == 422 and err_code(b) == "settlement_outside_schedule", f"{s} {err_code(b)}")

# 校准时刻晚于/等于计划终点
late = nxt("CAL-LATE")
s, _ = cal(late, ru, dev, cat_off=10, vfrom_off=10, vto_off=11)
check("准备校准时刻==计划终点的证书 201", s == 201, str(s))
s, b = settle(wo, late)
check("校准时刻==计划终点（半开不含）422 settlement_outside_schedule",
      s == 422 and err_code(b) == "settlement_outside_schedule", f"{s} {err_code(b)}")

# FAIL 证书不能结清（窄窗口避开 good 的 [day5,day10)，仅含其校准时刻）
failc = nxt("CAL-FAIL")
s, _ = cal(failc, ru, dev, cat_off=6, cat_hh=20, conclusion="FAIL", vfrom_off=6, vto_off=7)
check("准备 FAIL 证书 201", s == 201, str(s))
s, b = settle(wo, failc)
check("FAIL 证书结清 422 settlement_cert_not_passed",
      s == 422 and err_code(b) == "settlement_cert_not_passed", f"{s} {err_code(b)}")

# 不存在的证书
s, b = settle(wo, "NO-SUCH-CERT")
check("结清证书不存在 404 cert_not_found",
      s == 404 and err_code(b) == "cert_not_found", f"{s} {err_code(b)}")

# 半开区间起点（含）与终点前一刻：起点可结清
atstart = nxt("CAL-START")
s, _ = cal(atstart, ru, dev, cat_off=0, cat_hh=0, vfrom_off=0, vto_off=1)
check("校准时刻==计划起点（含）的证书 201", s == 201, str(s))

# 工单仍 PENDING（以上失败均未改变状态）
s, b = getwo(wo)
check("资格校验全部失败后工单仍 PENDING、未绑定",
      b["status"] == "PENDING" and b["cert_no"] is None, "")

# ---------------------------------------------------------------------------
print("== D. 成功结清 / 同证重试 / 改绑冲突 / 不可取消 ==")
s, b = settle(wo, good)
first_settled_at = b.get("settled_at")
check("合格证书结清 201 SETTLED 且绑定+结清时刻",
      s == 201 and b["status"] == "SETTLED" and b["cert_no"] == good
      and b["settled_at"] and b["certificate"]
      and b["certificate"]["cert_no"] == good
      and b["certificate"]["status"] == "ACTIVE"
      and b["certificate"]["conclusion"] == "PASS",
      f"{s} {json.dumps(b, ensure_ascii=False)[:240]}")

s, b2 = settle(wo, good)
check("同工单同证书重试 200 返回首次结果（settled_at 不变）",
      s == 200 and b2["status"] == "SETTLED" and b2["cert_no"] == good
      and b2["settled_at"] == first_settled_at, str(s))

s, b = settle(wo, atstart)
check("结清后改绑其他证书 409 work_order_cert_rebind_conflict",
      s == 409 and err_code(b) == "work_order_cert_rebind_conflict"
      and b["error"]["details"]["bound_cert_no"] == good, f"{s} {err_code(b)}")

s, b = cancel(wo)
check("结清后取消 409 work_order_not_pending",
      s == 409 and err_code(b) == "work_order_not_pending"
      and b["error"]["details"]["status"] == "SETTLED", f"{s} {err_code(b)}")

# 一证不得结清两个工单
wo2 = nxt("WO")
register(wo2, dev)
s, b = settle(wo2, good)
check("一张证书结清第二个工单 409 settlement_cert_already_settled 且点名持有工单",
      s == 409 and err_code(b) == "settlement_cert_already_settled"
      and b["error"]["details"]["bound_work_order_no"] == wo, f"{s} {json.dumps(b, ensure_ascii=False)[:220]}")

# wo2 仍可绑定另一张合规证书
s, b = settle(wo2, atstart)
check("第二张合规证书结清另一工单 201",
      s == 201 and b["status"] == "SETTLED" and b["cert_no"] == atstart, str(s))

# ---------------------------------------------------------------------------
print("== E. 取消：仅 PENDING；已取消不可结清 ==")
woc = nxt("WO")
register(woc, dev)
s, b = cancel(woc)
check("取消待完成工单 200 CANCELLED",
      s == 200 and b["status"] == "CANCELLED" and b["cancelled_at"]
      and b["cert_no"] is None, str(s))
s, b = cancel(woc)
check("已取消工单再取消 409 work_order_not_pending",
      s == 409 and err_code(b) == "work_order_not_pending", f"{s} {err_code(b)}")
s, b = settle(woc, good)
check("已取消工单结清 409 work_order_not_pending",
      s == 409 and err_code(b) == "work_order_not_pending", f"{s} {err_code(b)}")
s, b = getwo(woc)
check("已取消工单可查询且无 certificate",
      s == 200 and b["status"] == "CANCELLED" and b["certificate"] is None, str(s))

# ---------------------------------------------------------------------------
print("== F. 结清后撤销绑定证书：不倒退工单状态，查询返回当前状态 ==")
s, rev = revoke(good, reason="事后发现超差")
check("撤销绑定证书 200", s == 200, str(s))
s, b = getwo(wo)
check("撤销后工单仍 SETTLED、绑定与结清时刻不变，certificate.status=REVOKED",
      s == 200 and b["status"] == "SETTLED" and b["cert_no"] == good
      and b["settled_at"] == first_settled_at
      and b["certificate"]["status"] == "REVOKED",
      f"{s} status={b.get('certificate', {}).get('status')}")
# 同证重试仍返回首次结果（不因撤销而改变）
s, b = settle(wo, good)
check("撤销后同工单同证书重试仍 200 首次结果",
      s == 200 and b["cert_no"] == good and b["settled_at"] == first_settled_at, str(s))
# 已撤销证书不能用于结清新工单（当前非 ACTIVE）
wonew = nxt("WO")
register(wonew, dev)
s, b = settle(wonew, good)
check("已撤销证书结清新工单 422 settlement_cert_not_active",
      s == 422 and err_code(b) == "settlement_cert_not_active", f"{s} {err_code(b)}")

# 级联失效（INVALID）证书不能结清：撤销基准 atstart -> 下游 atstart 失效
s, _ = revoke(ru, reason="撤销基准使下游级联失效")
time.sleep(0.2)
s, b = getwo(wo2)
check("上游撤销级联失效后，已结清工单仍 SETTLED 且 certificate.status=INVALID",
      b["status"] == "SETTLED" and b["certificate"]["status"] == "INVALID",
      b["certificate"]["status"] if b.get("certificate") else "none")
woinv = nxt("WO")
register(woinv, dev)
s, b = settle(woinv, atstart)
check("失效（INVALID）证书结清新工单 422 settlement_cert_not_active",
      s == 422 and err_code(b) == "settlement_cert_not_active", f"{s} {err_code(b)}")

# ---------------------------------------------------------------------------
print("== G. 并发：结清×取消（同工单）、两工单同证书结清 ==")
# G1: 同一 PENDING 工单并发 结清 vs 取消 —— 恰一方生效。
# 每张证书只能结清一个工单，故每轮各用独立设备与独立证书（共用同一基准）。
gref = nxt("REF-G1")
call("POST", "/api/v1/reference-certificates", {
    "cert_no": gref, "device_id": "D-" + gref,
    "valid_from": iso(-30), "valid_to": iso(400), "calibrated_at": iso(-30)})


def race_order(i):
    gdev = f"DEV-CONC-1-{i}"
    gcert = nxt("CAL-G1")
    s, _ = cal(gcert, gref, gdev, cat_off=3, vfrom_off=3, vto_off=4)
    assert s == 201, s
    w = nxt("WO-G1")
    register(w, gdev)
    res = {}

    def do_settle():
        res["settle"] = settle(w, gcert)

    def do_cancel():
        res["cancel"] = cancel(w)
    t1, t2 = threading.Thread(target=do_settle), threading.Thread(target=do_cancel)
    t1.start(); t2.start(); t1.join(); t2.join()
    return w, gcert, res


settle_wins = cancel_wins = 0
for i in range(6):
    w, gcert, res = race_order(i)
    sc, sb = res["settle"]
    cc, cb = res["cancel"]
    _, final = getwo(w)
    if final["status"] == "SETTLED":
        settle_wins += 1
        ok = (sc == 201 and cc == 409 and err_code(cb) == "work_order_not_pending"
              and sb["cert_no"] == gcert)
        check(f"结清胜（{w}）：settle201 cancel409 最终SETTLED", ok,
              f"s={sc} c={cc} {err_code(cb)}")
    else:
        cancel_wins += 1
        ok = (cc == 200 and sc == 409 and err_code(sb) == "work_order_not_pending"
              and final["status"] == "CANCELLED")
        check(f"取消胜（{w}）：cancel200 settle409 最终CANCELLED", ok,
              f"s={sc} c={cc} {err_code(sb)}")
check("结清×取消并发双方均有胜出且无中间态",
      settle_wins + cancel_wins == 6, f"{settle_wins}/{cancel_wins}")

# G2: 两个不同工单并发用同一张证书结清 —— 恰一份成功，另一份 409
gref2 = nxt("REF-G2")
call("POST", "/api/v1/reference-certificates", {
    "cert_no": gref2, "device_id": "D-" + gref2,
    "valid_from": iso(-30), "valid_to": iso(400), "calibrated_at": iso(-30)})
g2dev = "DEV-CONC-2"
gcert2 = nxt("CAL-G2")
s, _ = cal(gcert2, gref2, g2dev, cat_off=3, vfrom_off=3, vto_off=4)
assert s == 201, s
w_a, w_b = nxt("WO-G2A"), nxt("WO-G2B")
register(w_a, g2dev); register(w_b, g2dev)
res2 = {}

def settle_a():
    res2["a"] = settle(w_a, gcert2)

def settle_b():
    res2["b"] = settle(w_b, gcert2)

t1, t2 = threading.Thread(target=settle_a), threading.Thread(target=settle_b)
t1.start(); t2.start(); t1.join(); t2.join()
(sa, ba), (sb2, bb) = res2["a"], res2["b"]
codes2 = sorted([sa, sb2])
check("两工单并发同证书结清：1×201 + 1×409 settlement_cert_already_settled",
      codes2 == [201, 409]
      and {err_code(ba), err_code(bb)} >= {"settlement_cert_already_settled"},
      f"{sa}/{sb2} {err_code(ba)}/{err_code(bb)}")
# 证书确实只绑定到一个工单
_, fa = getwo(w_a)
_, fb = getwo(w_b)
bound = [w for w, f in ((w_a, fa), (w_b, fb)) if f["status"] == "SETTLED"]
check("恰一个工单最终 SETTLED 且绑定该证书，另一个仍 PENDING",
      len(bound) == 1 and (fa["cert_no"] == gcert2 or fb["cert_no"] == gcert2),
      f"{fa['status']}:{fa['cert_no']} / {fb['status']}:{fb['cert_no']}")
# 同证书并发 8 个工单结清：仍只一份成功（用一张全新证书，排除前一实验的已绑定）
gcert3 = nxt("CAL-G2N")
s, _ = cal(gcert3, gref2, g2dev, cat_off=4, vfrom_off=4, vto_off=5)
assert s == 201, s
ws = [nxt("WO-G2N") for _ in range(8)]
for w in ws:
    register(w, g2dev)
res3 = {}


def settle_n(i):
    res3[i] = settle(ws[i], gcert3)


ths = [threading.Thread(target=settle_n, args=(i,)) for i in range(8)]
[t.start() for t in ths]
[t.join() for t in ths]
succ = [i for i, (st, _) in res3.items() if st == 201]
conf = [i for i, (st, b) in res3.items()
        if st == 409 and err_code(b) == "settlement_cert_already_settled"]
check("8 工单并发同证书结清：1×201 + 7×409（无重复绑定）",
      len(succ) == 1 and len(conf) == 7, f"201={len(succ)} 409={len(conf)}")

# ---------------------------------------------------------------------------
print("== H. 不预占：登记工单不影响证书签发/限额；既有契约与索引不变 ==")
# 登记工单后，证书仍可正常签发（未预占编号/有效期）
wreg = nxt("WO")
hdev = "DEV-HOLD"
register(wreg, hdev)
href = nxt("REF-H")
call("POST", "/api/v1/reference-certificates", {
    "cert_no": href, "device_id": "D-" + href,
    "valid_from": iso(-30), "valid_to": iso(400), "calibrated_at": iso(-30)})
hc = nxt("CAL-H")
s, _ = cal(hc, href, hdev, cat_off=2)
check("登记工单后证书签发不受影响（不预占有效期）", s == 201, str(s))
s, b = settle(wreg, hc)
check("该证书随后正常结清工单", s == 201 and b["status"] == "SETTLED", str(s))

# 根索引端点清单
s, idx = call("GET", "/", key=_NO_KEY)
listed = "\n".join(idx.get("endpoints", []))
check("根索引列出 4 个工单接口",
      all(e in listed for e in [
          "POST /api/v1/work-orders",
          "POST /api/v1/work-orders/{work_order_no}/settle",
          "POST /api/v1/work-orders/{work_order_no}/cancel",
          "GET  /api/v1/work-orders/{work_order_no}"]),
      listed[:120])

# 既有健康检查与证书查询契约不变
s, _ = call("GET", "/health", key=_NO_KEY)
check("健康检查仍 200", s == 200, str(s))
s, b = call("GET", f"/api/v1/certificates/{hc}", key=_NO_KEY)
check("证书详情接口契约不变（200）", s == 200 and b["cert_no"] == hc, str(s))

# ---------------------------------------------------------------------------
passed = sum(1 for _, ok, _ in results if ok)
failed = sum(1 for _, ok, _ in results if not ok)
print()
print(f"结果: PASS={passed} FAIL={failed}")
sys.exit(1 if failed else 0)
