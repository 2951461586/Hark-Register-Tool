#!/usr/bin/env python3
"""台账账号**存活复核** —— 用台账里的密码重新登录一次。

为什么要有这个工具
==================
"注册时返回 200" 不等于 "这个账号现在还能用"。本项目吃过教训的形态是
（Intern-Register-Tool 那边）：接口返回 200 + 一个凭据字符串，而那个凭据
**根本不能用**。要证明"真能用"必须**再走一次登录**。

判据（两级，都要过）：

    ① POST /api/auth/sign-in/email  → 200 + user.id        （密码还有效）
    ② GET  /api/billing/summary     → 200 + meters          （账号有额度、没被停）

🔴 **文件级防静默缩水**（比"某个账号坏了"高一层）
-----------------------------------------------
默认输入是**台账读源**（`ledger/runs/<日期>/accounts-<时间戳>.json`），
而不是某个导出快照。为什么这点必须强调：导出的 CSV/JSON 是**某次快照**，
不随批次刷新。快照停在几天前时，逐行过滤一切正常，但**连分母本身都是错的** ——
跑出"7/7 存活"这种**没测到却像全绿**的结论（Intern 那边实测过
"53/53 存活"而台账里已有 605 把）。

所以本工具：
  * stdout 打印 `台账 N 个 / 本次复核 M 个`；
  * artifact（`--json`）里带 `coverage` 块（`ledger_n` / `checked_n` /
    `uncovered` / `covers_ledger` + 来源路径，**相对仓库根**）；
  * 覆盖不足时**退出码 3**（与 `run.py` 的防静默缩水护栏同码）——
    只打印不改退出码的话，脚本化调用读到的是"成功"。

只想核验一个子集时用 `--allow-partial` 显式放行（退出码才回到 0）。

用法
====
    python tools/ops/verify_accounts.py                    # 全量复核台账
    python tools/ops/verify_accounts.py --limit 5           # 只抽 5 个（需 --allow-partial）
    python tools/ops/verify_accounts.py --json state/verify.json
    python tools/ops/verify_accounts.py --in ledger/latest.json

退出码：0 = 全部复核过且都活；1 = 有账号已失效；2 = 没有可复核的记录；
        3 = 覆盖不足（未加 --allow-partial）
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

from _path import ROOT  # noqa: F401  （副作用：sys.path + 加载 .env）

from common import config  # noqa: E402
from src import ledger, redact  # noqa: E402
from src.hark import HarkClient, HarkError, HarkQuota  # noqa: E402
from src.pipeline import login_limiter  # noqa: E402


def _verifiable(r) -> bool:
    """这条记录**值不值得复核**。

    🔴 判据是「密码真的设上去了」（`stages.password` 以 `ok` 开头），
      而不是「有 password 字段」。两者差别很大：注册失败的那些记录里
      `password` 是我们**生成但从未提交**的（甚至可能压根没建号），
      把它们算进分母会得到一堆 `INVALID_EMAIL_OR_PASSWORD` ——
      那不是"账号坏了"，是"这个账号从来没成过"。
      把两者混在一起，报告会变成一句**错误结论**："你的账号全挂了"。
    """
    stages = r.get("stages")
    pw_stage = str((stages or {}).get("password") or "") if isinstance(stages, dict) else ""
    return pw_stage.startswith("ok") or (r.get("status") or "") == "success"


def _pick(records, limit: int, offset: int) -> tuple:
    """挑出可复核的记录，返回 `(本批要复核的, 可复核总数, 被排除数)`。

    被排除的是"注册没走完"的记录（见 `_verifiable`）—— 把它们排除是
    **为了让分母说真话**，而不是为了把失败藏起来（数量会打印出来）。
    """
    usable = [
        r
        for r in records
        if isinstance(r, dict) and r.get("email") and r.get("password") and _verifiable(r)
    ]
    excluded = sum(
        1
        for r in records
        if isinstance(r, dict) and r.get("email") and r.get("password") and not _verifiable(r)
    )
    return usable[offset : offset + limit if limit else None], len(usable), excluded


def check_one(rec: dict) -> dict:
    """复核一条记录 → `{email, alive, why, plan, daily_limit, ms, waited_ms}`。

    🔴 **两级判据，且必须把"限流"与"账号坏了"分开**：
      * 一级：`sign-in/email` 200 + `user.id`（密码还有效）
      * 二级：`billing/summary` 200（账号真的可用，而不是"只是密码对"）

    🔴 `429` **绝不是"这个账号坏了"** —— 实测 `sign-in/email` 的限流是
      **3 次 / 10s**，而本工具默认会把整本台账连成一片打过去。
      把 429 当成 dead 会得到一堆**假阴性**（"你的账号全挂了"），
      而真实情况只是我们在打自己。所以：先过限速闸门，撞到 429 就退避重试。
    """
    t0 = time.time()
    out = {
        "email": rec.get("email", ""),
        "alive": False,
        "why": "",
        "plan": "",
        "daily_limit": 0,
        "ms": 0,
        "waited_ms": 0,
    }
    c = HarkClient(proxy=config.HR_PROXY)
    waited = 0.0
    body = None
    for attempt in range(config.LOGIN_RETRIES + 1):
        waited += login_limiter().acquire()
        try:
            body = c.sign_in(rec["email"], rec["password"])
            break
        except HarkQuota as ex:
            if attempt >= config.LOGIN_RETRIES:
                out["why"] = f"持续被限流（已试 {attempt + 1} 次）[{ex.kind}] {ex}"
                out["ms"] = int((time.time() - t0) * 1000)
                out["waited_ms"] = int(waited * 1000)
                return out
            time.sleep(config.LOGIN_WINDOW_S / config.LOGIN_MAX_PER_WINDOW + 1.0)
        except HarkError as ex:
            out["why"] = f"登录失败 [{ex.kind}] {ex}"
            out["ms"] = int((time.time() - t0) * 1000)
            out["waited_ms"] = int(waited * 1000)
            return out
    out["waited_ms"] = int(waited * 1000)
    user = (body or {}).get("user") or {}
    if not user.get("id"):
        out["why"] = "登录返回 200 但没有 user.id"
        out["ms"] = int((time.time() - t0) * 1000)
        return out
    try:
        # 第二级：能读到额度才说明账号真的可用（而不是"只是密码对"）。
        bill = c.billing_summary()
    except HarkError as ex:
        out["why"] = f"额度接口失败 [{ex.kind}] {ex}"
        out["ms"] = int((time.time() - t0) * 1000)
        return out
    meters = (bill.get("meters") or {}).get("harkTokens") or {}
    out["alive"] = True
    out["plan"] = str((bill.get("plan") or {}).get("id") or "")
    try:
        out["daily_limit"] = int(meters.get("dailyLimit") or 0)
    except (TypeError, ValueError):
        out["daily_limit"] = 0
    out["ms"] = int((time.time() - t0) * 1000)
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="台账账号存活复核（用台账里的密码重新登录）")
    ap.add_argument(
        "--in",
        dest="src",
        default=None,
        help="输入台账文件（默认 = 台账读源 ledger.ledger_path()）",
    )
    ap.add_argument("--limit", type=int, default=0, help="最多复核几个（0 = 全部）")
    ap.add_argument("--offset", type=int, default=0, help="从第几个开始（分片用）")
    ap.add_argument(
        "--allow-partial",
        action="store_true",
        help="显式放行「只复核子集」（否则覆盖不足返回退出码 3）",
    )
    ap.add_argument("--json", default=None, help="把报告写成 JSON")
    args = ap.parse_args(argv)

    path = args.src or ledger.ledger_path()
    all_records = ledger.load_existing(path)
    todo, total_usable, excluded = _pick(all_records, args.limit, args.offset)
    if not todo:
        print(f"✗ 台账里没有可复核的记录（{ledger.repo_rel(path)}）—— 先跑 python run.py")
        return 2

    print(
        f"台账：{ledger.repo_rel(path)}（共 {len(all_records)} 条，"
        f"可复核 {total_usable} 条，本次复核 {len(todo)} 条）"
    )
    if excluded:
        # 🔴 把排除数**打出来**，而不是静默缩小分母 —— 那正是本工具存在的理由。
        print(f"  （另有 {excluded} 条注册未走完（密码没设上），**不计入**存活率）")
    results = []
    for i, rec in enumerate(todo, 1):
        r = check_one(rec)
        results.append(r)
        mark = "✓" if r["alive"] else "✗"
        note = (
            f"{r['plan']}｜{r['daily_limit'] // 1000}K tokens/天" if r["alive"] else r["why"][:110]
        )
        print(f"  {mark} {redact.redact_email(r['email']):<40} {note}")
        _ = i

    alive = sum(1 for r in results if r["alive"])
    # 覆盖判据：这次复核的条数 vs 台账里**可复核**的条数（分母不能是快照自己的）。
    covered = args.offset + len(todo) >= total_usable
    report = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "ledger": ledger.repo_rel(path),
        "total": len(results),
        "alive": alive,
        "dead": len(results) - alive,
        "excluded_not_completed": excluded,
        "results": results,
        "coverage": {
            "ledger_n": total_usable,
            "checked_n": len(todo),
            "uncovered": max(0, total_usable - args.offset - len(todo)),
            "covers_ledger": covered,
        },
    }
    print(
        f"\n存活 {alive} / {len(results)}"
        f"（台账里可复核 {total_usable} 条，本次覆盖 {len(todo)} 条）"
    )
    if not covered and not args.allow_partial:
        print("⚠ 本次只覆盖了台账的一部分 —— 结论**不覆盖全量**。要显式放行请加 --allow-partial。")

    if args.json:
        # 相对路径一律相对**仓库根**（与 `--in` 的语义一致）。
        # 绝对路径原样用 —— 导出到仓库外是合法需求。
        p = Path(args.json)
        if not p.is_absolute():
            p = ROOT / p
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"报告已写入：{ledger.repo_rel(p) or p}")

    if not covered and not args.allow_partial:
        return 3
    return 0 if alive == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
