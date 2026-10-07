#!/usr/bin/env python3
"""域名门探测：Worker 的哪些收信域名**真的能收到 hark 的信**。

为什么需要它（实测踩到）
======================
有一个自有域名在 Worker 的 `DOMAINS` 列表里、邮箱也能建出来，
但 hark 的 magic link **从来没到**。而失败长这样：

    等 120s 没等到 magic link 邮件（打了 116 次收信接口）

—— 每个账号白等 **120 秒**，而且报错看起来像"邮箱服务读不出来"。
真正的原因是"这个域名收不到 hark 的信"。一次 120s 的代价，
本工具把它变成一次 35s 的对照实验。

🔴 本工具**不验证链接**，所以**不会创建任何账号**：hark 的账号是在
   `magic-link/verify` 那一步才建的（实测：verify 失败的那条记录
   `account-discovery` 查出来 `exists:false`）。所以探测域名是零副作用的。

判据
----
对每个域名：建一个地址 → 请求一次 magic link → 最多等 `--wait` 秒看邮件到没到。
三个结果分开报：

    ✓ 到了            → 这个域名可用
    ✗ 没到（超时）    → 不可用（或此刻投递异常）
    ⚠ 建不出地址      → Worker 不认这个域名

用法
====
    python tools/probes/probe_domains.py
    python tools/probes/probe_domains.py --domains a.example,b.example --wait 40
    python tools/probes/probe_domains.py --json state/domain_probe.json

退出码：0 = 全部可用；1 = 有域名收不到信；2 = 一个都没配
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor

from _path import ROOT  # noqa: F401  （副作用：sys.path + 加载 .env）

from common import config  # noqa: E402
from src.hark import HarkClient, HarkError  # noqa: E402
from src.tempmail import TempMailClient, now_ms  # noqa: E402

# 🔴 **刻意没有兜底域名**。早前版本写了个硬编码的自有域名当 FALLBACK ——
#    那是 🟠 基础设施标识（不该进仓库，见 docs/security-conventions.md），
#    而且行为上也是错的：Worker 拿不到域名时它会**默默去探一个写死的域名**，
#    得到一份看起来正常、实际与你当前配置无关的报告。
#    现在的做法是**大声报错**，让人显式给 `--domains`。


def worker_domains() -> list:
    """问 Worker 它认哪些域名（`GET /api/domains`）。拿不到就抛。"""
    import requests

    url = f"{config.WORKER_BASE.rstrip('/')}/api/domains"
    s = requests.Session()
    s.trust_env = False
    r = s.get(url, headers={"User-Agent": config.USER_AGENT, "Accept": "application/json"}, timeout=20)
    data = r.json()
    domains = list(data.get("domains") or [])
    if not domains:
        raise RuntimeError(
            f"Worker {url} 没有返回任何域名。\n"
            f"  修法：① 确认 HR_WORKER_BASE / HR_WORKER_ADMIN_TOKEN 是对的；\n"
            f"        ② 或者用 --domains 显式指定要探测的域名。"
        )
    return domains


def probe_domain(domain: str, wait: float) -> dict:
    """`{domain, address, mail, error}` —— **绝不抛**。"""
    out = {"domain": domain, "address": "", "mail": "", "error": ""}
    mail = TempMailClient()
    try:
        addrs = mail.create_mailbox(domain, 1)
    except Exception as ex:  # noqa: BLE001
        out["error"] = f"建地址失败：{type(ex).__name__}: {str(ex)[:80]}"
        return out
    if not addrs:
        out["error"] = "Worker 没返回地址（域名不在它的 DOMAINS 里？）"
        return out
    out["address"] = addrs[0]
    c = HarkClient()
    sent_at = now_ms()
    try:
        c.request_magic_link(out["address"])
    except HarkError as ex:
        out["error"] = f"请求 magic link 失败：{ex}"
        return out
    got = mail.wait_for_link(out["address"], "magic-link/verify", since_ts=sent_at, timeout=wait)
    if got:
        out["mail"] = got
    else:
        out["error"] = mail.last_error or f"等 {wait:.0f}s 邮件没到"
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="探测 Worker 的哪些域名能收到 hark 的信")
    ap.add_argument("--domains", default="", help="逗号分隔；不填则问 Worker 要全量")
    ap.add_argument("--wait", type=float, default=35.0, help="每个域名等多久（默认 35s）")
    ap.add_argument("--workers", type=int, default=4, help="并发探测数（默认 4）")
    ap.add_argument("--json", default=None, help="把报告写成 JSON（相对仓库根）")
    args = ap.parse_args(argv)

    domains = (
        [d.strip() for d in args.domains.split(",") if d.strip()]
        if args.domains.strip()
        else worker_domains()
    )
    if not domains:
        print("✗ 没有可探测的域名")
        return 2

    print(f"═══ 域名门探测：{len(domains)} 个域名，每个等 {args.wait:.0f}s ═══")
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=max(1, min(args.workers, len(domains)))) as ex:
        rows = list(ex.map(lambda d: probe_domain(d, args.wait), domains))
    wall = time.time() - t0

    ok, bad = [], []
    for r in rows:
        mark = "✓" if r["mail"] else "✗"
        (ok if r["mail"] else bad).append(r)
        note = "收到信" if r["mail"] else r["error"][:90]
        print(f"  {mark} {r['domain']:<20} {r['address']:<44} {note}")

    report = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "wall_s": round(wall, 1),
        "wait_s": args.wait,
        "usable": [r["domain"] for r in ok],
        "unusable": [r["domain"] for r in bad],
        "results": [
            {k: v for k, v in r.items() if k != "mail"} | {"got": bool(r["mail"])} for r in rows
        ],
    }
    print(f"\n可用 {len(ok)} / {len(rows)}：{', '.join(r['domain'] for r in ok) or '(无)'}")
    if bad:
        print(f"不可用：{', '.join(r['domain'] for r in bad)}")
        print("  ⚠ 用不可用的域名跑批量 = **每个账号白等一轮收信超时**（默认 120s）。")

    if args.json:
        from pathlib import Path

        p = Path(args.json)
        if not p.is_absolute():
            p = ROOT / p
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"报告已写入：{p}")

    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
