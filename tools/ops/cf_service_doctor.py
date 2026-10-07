#!/usr/bin/env python3
"""邮箱 Worker / D1 服务端体检 —— 从**服务端**判断"邮箱还能不能用来跑批量"。

为什么要有这个工具
==================
Intern-Register-Tool 踩过的坑：Worker 的 `/admin/all` 报 500，客户端只能看到
一个 Cloudflare 的 `Error 1101 worker_threw_exception` 页面 —— **异常文本拿不到**，
于是"到底是代码错、数据错、还是 D1 配额超了"完全靠猜。而靠猜的代价很实在：
上一轮把"配额超限"误判成"代码还有 bug"，白折腾半天。

这些答案在 CF 的 API 里全都有，而且都是**权威数字**，不是推断。

🔴 三条必须记住的判据（都是实测出来的）
--------------------------------------
1. **D1 免费额度**：rows read **5,000,000 / 天**、rows written **100,000 / 天**，
   **UTC 00:00 重置**（= 北京 08:00）。超了之后 D1 直接拒答，错误码 `7500`；
   Worker 里没 try/catch 的话，客户端看到的就是 500 + Error 1101。
2. **拦的是"扫描行数"，不是"返回行数"**。所以"返回空"**不等于**"没被拦"：
   `SELECT ... WHERE to_address='<不存在>'` 走索引 → rows_read=0（可用）；
   而 `SELECT COUNT(*)` / `ORDER BY id DESC LIMIT 1` 要扫表 → 直接 7500。
   ⇒ 判断收信端点是否可用，不能只看它 200，要看它返回的是 **0 条**还是被顶掉。
3. **不带浏览器 User-Agent 的请求会被 CF 边缘直接 403**（body 是
   `error code: 1010`，不是 Worker 回的）。用 python-urllib / requests 裸打
   会误判成"服务挂了"。必须带 UA。

🔴 关于"token 有效性"的判据 —— 别打错端点
----------------------------------------
本项目用的是**账户级** token（`cfat_` 前缀）。实测：

    GET /accounts                → 200（能列出账户）        ✅ 真判据
    GET /user/tokens/verify      → 401 Invalid API Token    ❌ 假判据

`/user/tokens/verify` 是**用户级**端点，账户级 token 本来就调不了它。
拿它当判据会把一个好好的 token 判死，然后人就会去重新签发一个 —— 白折腾。
**判据一律是 `/accounts`。**

用法
====
    # 凭据走 .env（HR_CF_*）或环境变量；**不落进仓库**
    python tools/ops/cf_service_doctor.py
    python tools/ops/cf_service_doctor.py --hours 8
    python tools/ops/cf_service_doctor.py --json state/service_doctor.json

退出码：0 = 邮箱服务可用；1 = 被 D1 配额限制（等重置）；2 = token 无效；
        3 = 其他（网络 / 端点异常）
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import urllib.error
import urllib.request

from _path import ROOT  # noqa: F401  （副作用：sys.path + 加载 .env）

from common import config  # noqa: E402

CF_API = "https://api.cloudflare.com/client/v4"
# 🔴 必须带浏览器 UA（见 docstring 第 3 条）。
UA = config.USER_AGENT
# D1 免费额度（rows）。**权威数字**，不是估计。
D1_FREE_ROWS_READ = 5_000_000
D1_FREE_ROWS_WRITTEN = 100_000


def _get(url: str, token: str, timeout: float = 25.0) -> tuple:
    """GET → `(status, json|None, text)`。**不抛**（网络错误也变成 status=0）。"""
    req = urllib.request.Request(
        url, headers={"Authorization": f"Bearer {token}", "User-Agent": UA}
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8", "replace")
            status = r.status
    except urllib.error.HTTPError as ex:
        raw = ex.read().decode("utf-8", "replace")
        status = ex.code
    except Exception as ex:  # noqa: BLE001
        return 0, None, f"{type(ex).__name__}: {ex}"
    try:
        return status, json.loads(raw), raw
    except ValueError:
        return status, None, raw


def _post_json(url: str, token: str, payload: dict, timeout: float = 25.0) -> tuple:
    body = json.dumps(payload).encode()
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": UA,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8", "replace")
            status = r.status
    except urllib.error.HTTPError as ex:
        raw = ex.read().decode("utf-8", "replace")
        status = ex.code
    except Exception as ex:  # noqa: BLE001
        return 0, None, f"{type(ex).__name__}: {ex}"
    try:
        return status, json.loads(raw), raw
    except ValueError:
        return status, None, raw


def probe_token(token: str) -> tuple:
    """`(state, accounts, message)`，`state` ∈ `{"ok", "denied", "unknown"}`。

    判据是 `/accounts`（见模块 docstring）。

    🔴 **三态而不是两态**（实测踩到）：CF 自己的 API 也会返回 **502**
      （`error code: 502`，一秒钟就恢复）。把它归到"token 无效"会让退出码
      变成 2，而 2 的含义是"**去重新签发一个 token**" —— 于是一个人就会
      为一个跟 token 毫无关系的瞬时抖动去换凭据。
      ⇒ `401/403` 才是 denied；连不上 / 5xx / 非 JSON 一律 unknown（**降级继续**）。
    """
    status, data, text = _get(f"{CF_API}/accounts", token)
    if status == 0:
        return "unknown", [], f"连不上 CF API：{text}"
    if status in (401, 403):
        return (
            "denied",
            [],
            (
                f"token 被拒（HTTP {status}）—— 这是**权威判据**。\n"
                f"    ⚠ 别用 /user/tokens/verify 复核：账户级 token 在那个端点上"
                f"必然报 Invalid，那不代表 token 坏了。"
            ),
        )
    if not isinstance(data, dict) or not data.get("success"):
        # 5xx / 非 JSON ⇒ **结果未知**，不是"token 坏了"。
        return "unknown", [], f"CF API 暂时不可用（HTTP {status}）：{str(text)[:200]}"
    return "ok", data.get("result") or [], ""


def probe_worker(token: str, account: str, name: str) -> tuple:
    """`(ok, message)`：线上跑的到底是哪个版本。

    🔴 端点必须是 **`/workers/services/{name}`**，不是 `/workers/scripts/{name}` ——
      后者返回的是**脚本内容**（multipart/form-data），不是元数据。
      拿它当 JSON 解析会得到一句看不懂的 `HTTP 200` + 二进制垃圾
      （实测：报错里出现 `Content-Disposition: form-data`，
       而人会以为是权限或参数问题）。
    """
    status, data, text = _get(f"{CF_API}/accounts/{account}/workers/services/{name}", token)
    if status == 0 or not isinstance(data, dict):
        return False, f"读不到 Worker（HTTP {status}）：{str(text)[:160]}"
    if not data.get("success"):
        return False, f"读不到 Worker：{json.dumps(data.get('errors'), ensure_ascii=False)[:200]}"
    env = (data.get("result") or {}).get("default_environment") or {}
    return True, (
        f"modified_on={env.get('modified_on')} script_tag={str(env.get('script_tag'))[:12]}…"
    )


def probe_deployments(token: str, account: str, name: str) -> tuple:
    """`(ok, message)`：部署历史，并**标出回滚**。

    🔴 同一个 `version_id` 在连续部署里重复出现 = **有人回滚了**。
      这不是“没用的细节”：Worker 行为变化（比如某条路由开始 500）时，
      “是不是刚回滚过”是第一个该看的证据，而它**不在**任何客户端错误里。
    """
    status, data, text = _get(
        f"{CF_API}/accounts/{account}/workers/scripts/{name}/deployments", token
    )
    if status == 0 or not isinstance(data, dict) or not data.get("success"):
        return False, f"读不到部署历史（HTTP {status}）：{str(text)[:160]}"
    items = (data.get("result") or {}).get("deployments") or []
    if not items:
        return False, "没有部署记录（从未部署过？）"
    # 接口按时间**倒序**返回：`items[0]` 是当前生效的那次。
    latest = items[0]
    vid = ((latest.get("versions") or [{}])[0] or {}).get("version_id", "")
    older = [((d.get("versions") or [{}])[0] or {}).get("version_id") for d in items[1:6]]
    rolled = vid in older
    return True, (
        f"最近部署 {latest.get('created_on')} version_id={str(vid)[:8]}…"
        f"（共 {len(items)} 次）"
        + ("  ⚠ 这个 version_id 在更早的部署里出现过 ⇒ **发生过回滚**" if rolled else "")
    )


def probe_d1(token: str, account: str, db_id: str) -> tuple:
    """`(ok, message)`：库大小与 `read_replication`（判断读延迟用）。"""
    status, data, text = _get(f"{CF_API}/accounts/{account}/d1/database/{db_id}", token)
    if status == 0 or not isinstance(data, dict) or not data.get("success"):
        return False, f"读不到 D1（HTTP {status}）：{str(text)[:160]}"
    r = data.get("result") or {}
    return True, (
        f"name={r.get('name')} size={r.get('file_size')} "
        f"read_replication={json.dumps(r.get('read_replication'), ensure_ascii=False)}"
    )


_GRAPHQL = """
query($acct: String!, $since: Time!, $until: Time!) {
  viewer {
    accounts(filter: {accountTag: $acct}) {
      d1AnalyticsAdaptiveGroups(
        limit: 100
        filter: {date_geq: $since, date_leq: $until}
        orderBy: [date_ASC]
      ) {
        sum { rowsRead rowsWritten }
        dimensions { date }
      }
    }
  }
}
"""


def probe_d1_usage(token: str, account: str, days: int = 7) -> tuple:
    """`(ok, rows, message)`：每天/每小时的 rows_read / rows_written。

    这是判断"能不能安全跑批量"的**唯一权威依据** —— 客户端看到的
    `Error 1101` 只有到这一层才能变成"配额烧到百分之几"。
    """
    until = dt.datetime.now(dt.UTC)
    since = until - dt.timedelta(days=days)
    status, data, text = _post_json(
        f"{CF_API}/graphql",
        token,
        {
            "query": _GRAPHQL,
            "variables": {
                "acct": account,
                "since": since.strftime("%Y-%m-%d"),
                "until": until.strftime("%Y-%m-%d"),
            },
        },
    )
    if status == 0 or not isinstance(data, dict):
        return False, [], f"GraphQL 连不上（HTTP {status}）：{str(text)[:160]}"
    if data.get("errors"):
        return False, [], f"GraphQL 报错：{json.dumps(data['errors'], ensure_ascii=False)[:240]}"
    groups = (((data.get("data") or {}).get("viewer") or {}).get("accounts") or [{}])[0].get(
        "d1AnalyticsAdaptiveGroups"
    ) or []
    rows = [
        {
            "date": (g.get("dimensions") or {}).get("date"),
            "rowsRead": (g.get("sum") or {}).get("rowsRead", 0),
            "rowsWritten": (g.get("sum") or {}).get("rowsWritten", 0),
        }
        for g in groups
    ]
    return True, rows, ""


def probe_endpoints() -> list:
    """直连 Worker 的两条读路径 —— 判据是**内容**，不是状态码。

    ⚠ 两个地址都用**不存在的收件人**：
      * `/api/inbox?email=<不存在>` 走索引 ⇒ 配额用尽时仍应返回 0 条（可用）；
      * `/admin/all?limit=1` 要扫表 ⇒ 配额用尽时会被 7500 顶成 500。
      这一对正是"拦的是扫描行数不是返回行数"那条判据的活体验证。
    """
    base = config.WORKER_BASE.rstrip("/")
    token = config.WORKER_ADMIN_TOKEN
    probe_addr = f"none@{config.WORKER_DOMAIN or 'example.com'}"
    out = []
    for label, url in (
        ("/api/inbox?email=（索引查询）", f"{base}/api/inbox?email={probe_addr}"),
        ("/admin/all?limit=1（全表扫描）", f"{base}/admin/all?limit=1"),
    ):
        req = urllib.request.Request(
            url,
            headers={
                "X-Admin-Token": token,
                "Authorization": f"Bearer {token}",
                "User-Agent": UA,
                "Accept": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=25) as r:
                raw = r.read().decode("utf-8", "replace")
                status = r.status
        except urllib.error.HTTPError as ex:
            raw = ex.read().decode("utf-8", "replace")
            status = ex.code
        except Exception as ex:  # noqa: BLE001
            out.append({"endpoint": label, "status": 0, "verdict": f"连不上：{ex}"})
            continue
        verdict = ""
        if status == 200:
            try:
                d = json.loads(raw)
                n = len(d.get("messages") or [])
                verdict = f"可用（返回 {n} 条）"
            except ValueError:
                verdict = "200 但正文不是 JSON（被边缘改写？）"
        elif status == 403 and "1010" in raw:
            verdict = "被 CF 边缘 403（UA 判人机）—— 不是服务端故障"
        elif status >= 500:
            verdict = "服务端 5xx（D1 配额？见上面 rowsRead）"
        else:
            verdict = f"HTTP {status}"
        out.append({"endpoint": label, "status": status, "verdict": verdict})
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="邮箱 Worker / D1 服务端体检")
    ap.add_argument("--days", type=int, default=7, help="GraphQL 取多少天的用量（默认 7）")
    ap.add_argument("--json", default=None, help="把报告写成 JSON（含原始数字，便于事后对比）")
    args = ap.parse_args(argv)

    token = (os.getenv("HR_CF_API_TOKEN") or "").strip()
    account = (os.getenv("HR_CF_ACCOUNT_ID") or "").strip()
    worker = (os.getenv("HR_CF_WORKER_NAME") or "").strip()
    db_id = (os.getenv("HR_CF_D1_DATABASE_ID") or "").strip()

    report: dict = {"generated_at": dt.datetime.now().isoformat(timespec="seconds")}
    print("═══ 邮箱服务体检 ═══")
    print(f"Worker: {config.WORKER_BASE or '(未配置)'}  域名: {config.WORKER_DOMAIN or '(未配置)'}")

    if not token:
        print("⚠ 没给 HR_CF_API_TOKEN —— 只能跑「端点直连」那一半（下面）。")
    else:
        ok, accounts, msg = probe_token(token)
        report["token_state"] = ok
        if ok != "ok":
            # `denied` 与 `unknown` 都要继续跑端点直连 —— 后者只是一次抖动，
            # 而前者是“服务端侧的元数据读不到了”，但“邮箱还能不能收信”
            # 是**另一个**问题（端点探针直接回答它）。
            print(f"{'✗' if ok == 'denied' else '⚠'} CF API：{msg}")
        if ok == "ok":
            names = ", ".join(f"{a.get('name')}({a.get('id')})" for a in accounts)
            print(f"✓ CF API token 可用，账户：{names}")
            acct = account or (accounts[0].get("id") if accounts else "")
            report["account_id"] = acct
            if acct and worker:
                ok, msg = probe_worker(token, acct, worker)
                print(("✓ " if ok else "✗ ") + f"Worker {worker}：{msg}")
                report["worker_ok"] = ok
                ok, msg = probe_deployments(token, acct, worker)
                print(("✓ " if ok else "✗ ") + f"部署历史：{msg}")
            if acct and db_id:
                ok, msg = probe_d1(token, acct, db_id)
                print(("✓ " if ok else "✗ ") + f"D1：{msg}")
            if acct:
                ok, rows, msg = probe_d1_usage(token, acct, args.days)
                if not ok:
                    print(f"✗ D1 用量：{msg}")
                else:
                    report["d1_usage"] = rows
                    print(f"✓ D1 用量（近 {args.days} 天，rows_read / rows_written）")
                    over = False
                    for row in rows:
                        rd, rw = row["rowsRead"] or 0, row["rowsWritten"] or 0
                        pct = rd / D1_FREE_ROWS_READ * 100
                        flag = "  ← 超限" if rd > D1_FREE_ROWS_READ else ""
                        over = over or rd > D1_FREE_ROWS_READ
                        print(f"    {row['date']}  {rd:>10,} ({pct:5.1f}%)  {rw:>8,}{flag}")
                    report["d1_over_quota"] = over

    eps = probe_endpoints()
    report["endpoints"] = eps
    print("── 端点直连 ──")
    for e in eps:
        print(f"  {e['endpoint']}: HTTP {e['status']} — {e['verdict']}")

    if args.json:
        p = ROOT / args.json if not os.path.isabs(args.json) else args.json
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"报告已写入：{p}")

    # 退出码：先看 D1 是否超限（那是"能不能跑批量"的硬约束），再看端点。
    if report.get("d1_over_quota"):
        print("⇒ D1 rows_read 已超免费额度：**先别跑批量**（每次注册都在花全站共享的读配额）")
        return 1
    if token and report.get("token_state") == "denied":
        return 2
    bad = [e for e in eps if e["status"] != 200]
    if bad:
        print(f"⇒ 有 {len(bad)} 条端点不可用（见上）")
        return 3
    print("⇒ 邮箱服务可用。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
