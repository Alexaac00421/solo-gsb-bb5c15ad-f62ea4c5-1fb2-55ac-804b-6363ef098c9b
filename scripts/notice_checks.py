"""撤销风险告知测试：已签收（ACCEPT）归档受撤销波及生成告知、收件人查询/确认、
同包同批一份、未签收/REJECT 不生成、密钥过期不影响确认、重复确认返回首次
确认时间、签收与撤销并发按提交次序、既有接口契约保持。

直接压 HTTP 接口（urllib，无第三方依赖）。服务需已启动。
用法: python3 scripts/notice_checks.py [base_url] [management_key]

依赖：DELIVERY_MANAGEMENT_KEY 默认为 test-admin-key（与其余 *_checks.py 相同，
直接对按默认/测试配置启动的服务运行即可）。
"""
import json
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8080"
MK = sys.argv[2] if len(sys.argv) > 2 else "test-admin-key"
results = []


def call(method, path, body=None, key=None):
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"}
    if key is not None:
        headers["X-Management-Key"] = key
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


TZ = timezone(timedelta(hours=8))
t0 = datetime(2026, 2, 1, 0, 0, tzinfo=TZ)
_seq = [0]


def nxt(prefix):
    _seq[0] += 1
    return f"{prefix}-{_seq[0]}"


def iso(offset_days=0):
    return (t0 + timedelta(days=offset_days)).isoformat()


def future(seconds=3600):
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()


def ref(no):
    return call("POST", "/api/v1/reference-certificates", {
        "cert_no": no, "device_id": "DEV-" + no, "device_name": "基准" + no,
        "valid_from": iso(0), "valid_to": iso(1500), "calibrated_at": iso(0)})


def cal(no, parent):
    return call("POST", "/api/v1/certificates", {
        "cert_no": no, "device_id": "DEV-" + no, "device_name": "仪表" + no,
        "valid_from": iso(2), "valid_to": iso(1200), "calibrated_at": iso(3),
        "conclusion": "PASS", "parent_cert_no": parent})


def make_chain(tag, depth=2):
    """基准 + depth-1 张普通证书链；返回 (基准号, [普通证书号...])。"""
    r = nxt("NR-" + tag)
    check(f"[{tag}] 基准 201", ref(r)[0] == 201)
    cs = []
    prev = r
    for _ in range(depth - 1):
        c = nxt("NC-" + tag)
        check(f"[{tag}] 普通证书 201", cal(c, prev)[0] == 201)
        cs.append(c)
        prev = c
    return r, cs


def make_archive(tag, cert_nos):
    no = nxt("NA-" + tag)
    s, _ = call("POST", "/api/v1/delivery-archives", {"archive_no": no, "cert_nos": cert_nos})
    check(f"[{tag}] 归档包 {no} 201", s == 201, f"got {s}")
    return no


def make_invite(archive_no, recipient="告知客户", deadline=None):
    return call("POST", "/api/v1/delivery-invitations", {
        "archive_no": archive_no, "recipient": recipient,
        "deadline": deadline or iso(1400)}, key=MK)


def accept(inv):
    return call("POST", "/api/v1/delivery-receipts",
                {"invitation_no": inv["invitation_no"], "key": inv["key"], "decision": "ACCEPT"})


def query_notices(inv):
    return call("POST", "/api/v1/delivery-revocation-notices/query",
                {"invitation_no": inv["invitation_no"], "key": inv["key"]})


def confirm_notice(notice_id, inv):
    return call("POST", f"/api/v1/delivery-revocation-notices/{notice_id}/confirm",
                {"invitation_no": inv["invitation_no"], "key": inv["key"]})


def hit_pairs(item):
    return [(h["cert_no"], h["chain_position"]) for h in item["hits"]]


# ---------------------------------------------------------------------------
print("== 0. 前置数据（多条独立链 + 不同定稿状态的归档包） ==")
# A：三层链，两个 ACCEPT 归档（同链不同包）
rA, (cA1, cA2) = make_chain("A", 3)
aA = make_archive("A", [cA1, cA2])
aA2 = make_archive("A2", [cA2])
s, invA = make_invite(aA, recipient="客户A")
check("[A] 邀请 201", s == 201)
s, invA2 = make_invite(aA2, recipient="客户A2")
check("[A2] 邀请 201", s == 201)
check("[A] ACCEPT 201", accept(invA)[0] == 201)
check("[A2] ACCEPT 201", accept(invA2)[0] == 201)
# A 包上的第二份邀请（非定稿邀请，用于凭据校验）
s, invA_alt = make_invite(aA, recipient="客户A-副本")
check("[A] 同一归档可再建邀请", s == 201 and invA_alt["invitation_no"] != invA["invitation_no"])
# B：单层链，ACCEPT（后续才撤销，用于并发确认与跨包校验）
rB, (cB,) = make_chain("B")
aB = make_archive("B", [cB])
s, invB = make_invite(aB, recipient="客户B")
check("[B] ACCEPT 201", accept(invB)[0] == 201)
# C：REJECT 定稿
rC, (cC,) = make_chain("C")
aC = make_archive("C", [cC])
s, invC = make_invite(aC, recipient="客户C")
s, rr = call("POST", "/api/v1/delivery-receipts", {
    "invitation_no": invC["invitation_no"], "key": invC["key"],
    "decision": "REJECT", "reason": "客户拒收"})
check("[C] REJECT 201", s == 201 and rr["decision"] == "REJECT", f"got {s}")
# D：仅建邀请，不定稿
rD, (cD,) = make_chain("D")
aD = make_archive("D", [cD])
s, invD = make_invite(aD, recipient="客户D")
check("[D] 邀请 201（保持未定稿）", s == 201)
# E：ACCEPT 但永不撤销（对照组，不应有告知）
rE, (cE,) = make_chain("E")
aE = make_archive("E", [cE])
s, invE = make_invite(aE, recipient="客户E")
check("[E] ACCEPT 201", accept(invE)[0] == 201)

# ---------------------------------------------------------------------------
print("== A. 撤销波及 ACCEPT 归档 -> 生成告知（同包同批一份，内容完整） ==")
s, rbA = call("POST", f"/api/v1/certificates/{rA}/revoke", {"reason": "基准超差", "operator": "qa"})
check("撤销基准 200", s == 200, f"got {s}")
batchA = rbA["batch_id"]

s, qA = query_notices(invA)
check("查询告知 200", s == 200, f"got {s}")
check("定稿信息回显（归档号/邀请/收件人/结论）",
      qA["archive_no"] == aA and qA["invitation_no"] == invA["invitation_no"]
      and qA["recipient"] == "客户A" and qA["decision"] == "ACCEPT",
      json.dumps({k: qA.get(k) for k in ("archive_no", "invitation_no", "recipient", "decision")}))
check("同包同批只记一份告知", qA["total"] == 1 and len(qA["items"]) == 1, str(qA.get("total")))
nA = qA["items"][0]
check("告知列出撤销批次与被撤销证书",
      nA["batch_id"] == batchA and nA["revoked_cert_no"] == rA and bool(nA["revoked_at"]),
      json.dumps({k: nA.get(k) for k in ("batch_id", "revoked_cert_no")}))
check("告知生成时刻存在、初始未确认", bool(nA["created_at"]) and nA["confirmed_at"] is None)
check("受影响目标按原交付序位列出（两个目标）",
      [i["cert_no"] for i in nA["items"]] == [cA1, cA2]
      and [i["delivery_position"] for i in nA["items"]] == [1, 2],
      json.dumps(nA["items"], ensure_ascii=False))
check("链上命中编号含被撤销证书与级联失效证书（1 起始链上序位）",
      hit_pairs(nA["items"][0]) == [(rA, 1), (cA1, 2)]
      and hit_pairs(nA["items"][1]) == [(rA, 1), (cA1, 2), (cA2, 3)],
      json.dumps(nA["items"], ensure_ascii=False))

s, qA2 = query_notices(invA2)
check("同批波及另一归档包：各自生成一份（notice_id 不同、批次相同）",
      s == 200 and qA2["total"] == 1
      and qA2["items"][0]["notice_id"] != nA["notice_id"]
      and qA2["items"][0]["batch_id"] == batchA, f"{s} {qA2.get('total')}")
check("另一归档包告知内容（单目标、交付序位 1）",
      [i["cert_no"] for i in qA2["items"][0]["items"]] == [cA2]
      and qA2["items"][0]["items"][0]["delivery_position"] == 1
      and hit_pairs(qA2["items"][0]["items"][0]) == [(rA, 1), (cA1, 2), (cA2, 3)])
nA2 = qA2["items"][0]

s, qB0 = query_notices(invB)
check("未受波及的 ACCEPT 归档为空清单", s == 200 and qB0["total"] == 0 and qB0["items"] == [])
s, qE0 = query_notices(invE)
check("对照组（永不撤销）为空清单", s == 200 and qE0["total"] == 0)
s, qA_again = query_notices(invA)
check("重复查询结果稳定（仍一份、内容一致）", s == 200 and qA_again == qA)

# ---------------------------------------------------------------------------
print("== B. 未签收或 REJECT 不生成告知 ==")
s, _ = call("POST", f"/api/v1/certificates/{rC}/revoke", {"reason": "C链撤销"})
check("撤销 C 链 200", s == 200)
s, _ = call("POST", f"/api/v1/certificates/{rD}/revoke", {"reason": "D链撤销"})
check("撤销 D 链 200", s == 200)
s, qC = query_notices(invC)
check("REJECT 归档可查询但不生成告知", s == 200 and qC["total"] == 0
      and qC["decision"] == "REJECT", f"{s} {qC.get('total')}")
s, e = query_notices(invD)
check("未签收归档查询 404 receipt_not_found", s == 404 and err_code(e) == "receipt_not_found",
      f"{s} {err_code(e)}")

# ---------------------------------------------------------------------------
print("== C. 查询凭据校验：邀请/密钥/定稿邀请 ==")
s, e = call("POST", "/api/v1/delivery-revocation-notices/query",
            {"invitation_no": "DINV-0000000000000000", "key": "whatever"})
check("未知邀请 404", s == 404 and err_code(e) == "invitation_not_found", f"{s} {err_code(e)}")
s, e = call("POST", "/api/v1/delivery-revocation-notices/query",
            {"invitation_no": invA["invitation_no"], "key": "bad-secret"})
check("错误一次性密钥 401", s == 401 and err_code(e) == "invalid_receipt_key", f"{s} {err_code(e)}")
s, e = query_notices(invA_alt)
check("非定稿邀请查询 409 receipt_conflict", s == 409 and err_code(e) == "receipt_conflict",
      f"{s} {err_code(e)}")
check("冲突详情点名定稿邀请", e["error"]["details"]["finalized_invitation_no"] == invA["invitation_no"]
      and e["error"]["details"]["finalized_decision"] == "ACCEPT",
      json.dumps(e.get("error", {}).get("details", {}), ensure_ascii=False))

# ---------------------------------------------------------------------------
print("== D. 确认告知：首次 201、重复返回首次确认时间、并发仅一份首次 ==")
s, e = call("POST", f"/api/v1/delivery-revocation-notices/{nA['notice_id']}/confirm",
            {"invitation_no": invA["invitation_no"], "key": "bad-secret"})
check("错误密钥确认 401", s == 401 and err_code(e) == "invalid_receipt_key", f"{s} {err_code(e)}")
s, e = confirm_notice(999999999, invA)
check("未知告知 404 notice_not_found", s == 404 and err_code(e) == "notice_not_found",
      f"{s} {err_code(e)}")
s, e = confirm_notice(nA["notice_id"], invB)
check("跨归档确认 404 notice_not_found", s == 404 and err_code(e) == "notice_not_found",
      f"{s} {err_code(e)}")
s, e = call("POST", f"/api/v1/delivery-revocation-notices/{nA['notice_id']}/confirm",
            {"invitation_no": invA_alt["invitation_no"], "key": invA_alt["key"]})
check("非定稿邀请确认 409 receipt_conflict", s == 409 and err_code(e) == "receipt_conflict",
      f"{s} {err_code(e)}")

s, c1 = confirm_notice(nA["notice_id"], invA)
check("首次确认 201 且写入确认时刻", s == 201 and bool(c1["confirmed_at"]), f"got {s}")
check("确认响应即告知本体（批次/目标/命中明细不变）",
      c1["notice_id"] == nA["notice_id"] and c1["batch_id"] == batchA
      and [i["cert_no"] for i in c1["items"]] == [cA1, cA2]
      and hit_pairs(c1["items"][0]) == [(rA, 1), (cA1, 2)])
first_confirmed_at = c1["confirmed_at"]
s, c2 = confirm_notice(nA["notice_id"], invA)
check("重复确认 200 返回首次确认时间", s == 200 and c2["confirmed_at"] == first_confirmed_at,
      f"{s} {c2.get('confirmed_at')} != {first_confirmed_at}")
s, qA2nd = query_notices(invA)
check("查询回显确认时刻", qA2nd["items"][0]["confirmed_at"] == first_confirmed_at)

# 并发确认另一份告知：恰 1 个 201，其余 200，确认时间全同
M = 8
bar = threading.Barrier(M)
outs = []
olk = threading.Lock()


def confirm_worker():
    bar.wait()
    s, b = confirm_notice(nA2["notice_id"], invA2)
    with olk:
        outs.append((s, b))


ths = [threading.Thread(target=confirm_worker) for _ in range(M)]
for t in ths:
    t.start()
for t in ths:
    t.join()
codes = sorted(s for s, _ in outs)
check("并发确认：1×201 其余 200", codes == [200] * (M - 1) + [201], str(codes))
check("并发确认：确认时间全部相同（首次确认时间）",
      len({b["confirmed_at"] for _, b in outs}) == 1,
      str({b["confirmed_at"] for _, b in outs}))

# ---------------------------------------------------------------------------
print("== E. 密钥过期不影响已签收后的查询与确认 ==")
rF, (cF,) = make_chain("F")
aF = make_archive("F", [cF])
s, invF = make_invite(aF, recipient="客户F", deadline=future(2))
check("[F] 短截止邀请 201", s == 201)
check("[F] 截止前 ACCEPT 201", accept(invF)[0] == 201)
time.sleep(2.3)  # 邀请已过截止
s, rbF = call("POST", f"/api/v1/certificates/{rF}/revoke", {"reason": "F链撤销"})
check("[F] 撤销 200", s == 200)
s, qF = query_notices(invF)
check("密钥过期后仍可查询告知", s == 200 and qF["total"] == 1
      and qF["items"][0]["batch_id"] == rbF["batch_id"], f"{s} {qF.get('total')}")
s, cF1 = confirm_notice(qF["items"][0]["notice_id"], invF)
check("密钥过期后仍可确认 201", s == 201 and bool(cF1["confirmed_at"]), f"got {s}")
s, cF2 = confirm_notice(qF["items"][0]["notice_id"], invF)
check("过期后重复确认仍返回首次确认时间", s == 200 and cF2["confirmed_at"] == cF1["confirmed_at"])

# ---------------------------------------------------------------------------
print("== F. 并发：签收与撤销按提交次序（先签收生成告知，先撤销拒绝 ACCEPT） ==")
N = 16
chains = []
for i in range(N):
    rr, (cc,) = make_chain(f"R{i}")
    aa = make_archive(f"R{i}", [cc])
    s, ii = make_invite(aa)
    assert s == 201
    chains.append((rr, cc, aa, ii))
barrier = threading.Barrier(N * 2)
res = {}
rlock = threading.Lock()


def race_accept(idx, ii):
    barrier.wait()
    s, b = accept(ii)
    with rlock:
        res[idx] = ("accept", s, b.get("error", {}).get("code"))


def race_revoke(idx, rr):
    barrier.wait()
    s, b = call("POST", f"/api/v1/certificates/{rr}/revoke", {"reason": "并发撤销"})
    with rlock:
        res[(idx, "rv")] = (s, b.get("batch_id"))


threads = []
for idx, (rr, cc, aa, ii) in enumerate(chains):
    threads.append(threading.Thread(target=race_accept, args=(idx, ii)))
    threads.append(threading.Thread(target=race_revoke, args=(idx, rr)))
for t in threads:
    t.start()
for t in threads:
    t.join()

n_signed = n_revoked_first = 0
for idx, (rr, cc, aa, ii) in enumerate(chains):
    rv_status, rv_batch = res[(idx, "rv")]
    check(f"[{idx}] 撤销恒成功", rv_status == 200, str(res[(idx, "rv")]))
    _, a_status, a_code = res[idx]
    if a_status == 201:
        n_signed += 1
        s, q = query_notices(ii)
        check(f"[{idx}] 先签收：生成一份告知且批次对应", s == 200 and q["total"] == 1
              and q["items"][0]["batch_id"] == rv_batch
              and hit_pairs(q["items"][0]["items"][0]) == [(rr, 1), (cc, 2)],
              f"{s} total={q.get('total')}")
    else:
        n_revoked_first += 1
        check(f"[{idx}] 先撤销：ACCEPT 422 点名", a_status == 422
              and a_code == "receipt_targets_revoked", f"{a_status} {a_code}")
        s, e = query_notices(ii)
        check(f"[{idx}] 先撤销：无定稿无告知（404 receipt_not_found）",
              s == 404 and err_code(e) == "receipt_not_found", f"{s} {err_code(e)}")
check(f"并发次序两类结果均出现且守恒（{n_signed} 先签收 / {n_revoked_first} 先撤销）",
      n_signed + n_revoked_first == N and n_signed > 0 and n_revoked_first > 0,
      f"{n_signed}+{n_revoked_first}!={N}")

# ---------------------------------------------------------------------------
print("== G. 中段撤销命中编号 / 同一归档多批次多份告知 ==")
# 中段撤销：命中含被撤销证书与其级联失效下游
rG, (cG1, cG2) = make_chain("G", 3)
aG = make_archive("G", [cG2])
s, invG = make_invite(aG)
check("[G] ACCEPT 201", accept(invG)[0] == 201)
s, rbG = call("POST", f"/api/v1/certificates/{cG1}/revoke", {"reason": "中段撤销"})
check("[G] 中段撤销 200", s == 200)
s, qG = query_notices(invG)
check("中段撤销：告知命中被撤销证书与级联失效证书",
      s == 200 and qG["total"] == 1
      and qG["items"][0]["revoked_cert_no"] == cG1
      and hit_pairs(qG["items"][0]["items"][0]) == [(cG1, 2), (cG2, 3)],
      json.dumps(qG.get("items"), ensure_ascii=False))

# 同一归档先后被两个独立批次波及：各生成一份，按 notice_id 升序
rH1, (cH1,) = make_chain("H1")
rH2, (cH2,) = make_chain("H2")
aH = make_archive("H", [cH1, cH2])
s, invH = make_invite(aH)
check("[H] ACCEPT 201", accept(invH)[0] == 201)
s, rbH1 = call("POST", f"/api/v1/certificates/{rH1}/revoke", {"reason": "H1撤销"})
check("[H1] 撤销 200", s == 200)
s, qH1 = query_notices(invH)
check("第一批波及：仅点名受影响目标",
      s == 200 and qH1["total"] == 1
      and [i["cert_no"] for i in qH1["items"][0]["items"]] == [cH1]
      and hit_pairs(qH1["items"][0]["items"][0]) == [(rH1, 1), (cH1, 2)],
      json.dumps(qH1.get("items"), ensure_ascii=False))
s, rbH2 = call("POST", f"/api/v1/certificates/{rH2}/revoke", {"reason": "H2撤销"})
check("[H2] 撤销 200", s == 200)
s, qH2 = query_notices(invH)
check("第二批波及：同一归档累计两份告知（各对应其批次）",
      s == 200 and qH2["total"] == 2
      and [n["batch_id"] for n in qH2["items"]] == [rbH1["batch_id"], rbH2["batch_id"]]
      and qH2["items"][0]["notice_id"] < qH2["items"][1]["notice_id"],
      f"{s} total={qH2.get('total')}")
check("第二份告知仅含第二批受影响目标",
      [i["cert_no"] for i in qH2["items"][1]["items"]] == [cH2]
      and qH2["items"][1]["items"][0]["delivery_position"] == 2
      and hit_pairs(qH2["items"][1]["items"][0]) == [(rH2, 1), (cH2, 2)])

# ---------------------------------------------------------------------------
print("== H. 既有接口契约保持 ==")
s, snap = call("GET", f"/api/v1/delivery-archives/{aA}")
frozen = [n["certificate"]["status"] for n in snap["items"][0]["chain"]["nodes"]]
check("归档快照链状态仍冻结为 ACTIVE（不被撤销/告知改写）",
      s == 200 and frozen == ["ACTIVE"] * len(frozen), str(frozen))
s, q = call("GET", f"/api/v1/delivery-archives/{aA}/receipt", key=MK)
check("定稿记录不被撤销/告知改写", s == 200 and q["decision"] == "ACCEPT"
      and q["invitation_no"] == invA["invitation_no"])
s, rev = call("GET", f"/api/v1/revocations/{batchA}")
check("撤销批次详情契约不变", s == 200 and rev["batch_id"] == batchA
      and rev["revoked"]["cert_no"] == rA and rev["affected_count"] == 2, f"got {s}")
s, im = call("GET", f"/api/v1/revocations/{batchA}/archive-impact")
check("影响面追查契约不变（同批命中同样归档）", s == 200 and im["total"] == 3
      and {i["archive_no"] for i in im["items"]} == {aA, aA2}, f"{s} total={im.get('total')}")
s, idx = call("GET", "/")
eps = idx["endpoints"]
check("根索引列出告知查询/确认端点", s == 200
      and any(x.endswith("/delivery-revocation-notices/query") for x in eps)
      and any(x.endswith("/delivery-revocation-notices/{notice_id}/confirm") for x in eps),
      str([x for x in eps if "notice" in x]))

# ---------------------------------------------------------------------------
n_pass = sum(1 for _, ok, _ in results if ok)
n_fail = len(results) - n_pass
print(f"\n结果: PASS={n_pass} FAIL={n_fail}")
sys.exit(1 if n_fail else 0)
