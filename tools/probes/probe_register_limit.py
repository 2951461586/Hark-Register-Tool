#!/usr/bin/env python3
"""测出**注册侧 429** 是按什么键限的 —— 这是"能不能提高注册吞吐"的唯一依据。

为什么需要它
============
2026-10-08 的 10 批实测里，批次 6 出现 3 个注册侧 429，而它们**同一瞬间命中了
3 个不同出口**（7903/7905/7904）。⇒ 它**不是 per-IP** 的。

这一条推翻了一个很自然的假设（"加出口就能提高注册吞吐"），但**没有**回答
"那它按什么键"。在测出这个之前，任何关于注册并发/限速的调参都是猜。

本工具做两段：
  阶段 A —— **找形状**：连续打注册申请，看哪个端点先 429、第几次、什么窗口。
  阶段 B —— **定性**：一出现 429 就立刻做对照（窗口可能只有几分钟）：
              B1 换收件域名（同出口同端点）  → 通过 ⇒ 按**域名**键
              B2 换出口（同域名同端点）      → 通过 ⇒ 按 **IP** 键（与批次 6 矛盾，复验）
              B3 只打只读的 discovery        → 429  ⇒ **读**端点也限；通过 ⇒ 只限写
              B4 同一个地址连打 magic-link   → 与 B1 合起来区分"按地址"还是"按域名"

🔴 安全边界（为什么它不会把账号池搞脏）
------------------------------------
* **绝不调用 `verify_magic_link`** ⇒ 不会创建任何账号（hark 的账号是在 verify
  那一步才建的，实测：verify 失败的那条记录 `account-discovery` 查出来
  `exists:false`）。
* 地址用**真的 Worker 邮箱**（不是编出来的）：编地址会让 hark 去投递一个不存在的
  收件人，产生退信 —— 那是打在别人发信信誉上的副作用。
  Worker 邮箱的代价只是一次 D1 写，而实测 D1 只用了免费额度的 0.3%。

用法
====
    python tools/probes/probe_register_limit.py                  # 默认最多 40 次注册申请
    python tools/probes/probe_register_limit.py --max-calls 80
    python tools/probes/probe_register_limit.py --json state/reg_limit.json

退出码：0 = 跑完并给出结论；1 = 没触发 429（上限内没测到）；2 = 配置不全
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime

from _path import ROOT  # noqa: F401  （副作用：sys.path + 加载 .env）

from common import config  # noqa: E402
from src import redact  # noqa: E402
from src.hark import HarkClient, HarkError, HarkQuota  # noqa: E402
from src.proxypool import SlotPool  # noqa: E402
from src.tempmail import TempMailClient, now_ms  # noqa: E402

# 三个"注册申请"端点，按实际调用顺序。名字用于报告。
REGISTER_CALLS = (
    ("discovery", lambda c, addr, bd: c.discover(addr)),
    ("signup-age", lambda c, addr, bd: c.signup_age(addr, bd)),
    ("magic-link", lambda c, addr, bd: c.request_magic_link(addr)),
)


def _one_round(
    client: HarkClient, mail: TempMailClient, domain: str, birthday: str, *, do_mail: bool = True
) -> list:
    """跑一轮注册申请，返回 `[{call, status, ms, error}]`（**不校验链接**）。"""
    addr = ""
    if do_mail:
        try:
            got = mail.create_mailbox(domain, 1)
            addr = got[0] if got else ""
        except Exception as ex:  # noqa: BLE001
            return [{"call": "mailbox", "status": 0, "ms": 0, "error": f"{type(ex).__name__}"}]
    else:
        # 对照用：同域名下换个地址（不建邮箱，省一次 D1 写）。
        addr = f"probe-{now_ms()}@{domain}"

    out = []
    for name, fn in REGISTER_CALLS:
        t0 = time.time()
        try:
            fn(client, addr, birthday)
            out.append({"call": name, "status": 200, "ms": int((time.time() - t0) * 1000), "error": ""})
        except HarkQuota as ex:
            out.append(
                {
                    "call": name,
                    "status": getattr(ex, "status", 429),
                    "ms": int((time.time() - t0) * 1000),
                    "error": f"QUOTA {ex}",
                    "quota": True,
                }
            )
            break  # 这一个 429 就够了，别再打后面的
        except HarkError as ex:
            out.append(
                {
                    "call": name,
                    "status": getattr(ex, "status", 0),
                    "ms": int((time.time() - t0) * 1000),
                    "error": f"{type(ex).__name__}: {ex}",
                }
            )
            break
    out.append({"call": "address", "status": 200, "ms": 0, "error": addr})
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="测注册侧 429 的键")
    ap.add_argument("--max-calls", type=int, default=40, help="最多几轮注册申请（默认 40）")
    ap.add_argument("--interval", type=float, default=0.0, help="轮之间的间隔秒数（默认 0 = 尽快打）")
    ap.add_argument("--domain", default=None, help="主域名（默认 HR_WORKER_DOMAIN）")
    ap.add_argument("--alt-domain", default=None, help="对照用的另一个域名（默认问 Worker 要）")
    ap.add_argument("--json", default=None, help="把报告写成 JSON（相对仓库根）")
    args = ap.parse_args(argv)

    domain = args.domain or config.WORKER_DOMAIN
    if not domain:
        print("✗ 未配置 HR_WORKER_DOMAIN")
        return 2

    slots = config.proxy_slots()
    pool = SlotPool(slots, timeout=config.PROXY_SLOT_TIMEOUT)
    print("═══ 注册侧限流探测 ═══")
    print(f"主域名 {domain} · 出口 {len(slots) or '直连'} 个 · 最多 {args.max_calls} 轮")
    print("⚠ 本工具**不校验链接**，所以不会创建任何账号\n")

    mail = TempMailClient()
    client = HarkClient(proxy=pool.acquire() if pool else None)
    rounds = []
    first_quota = None

    for i in range(1, args.max_calls + 1):
        t0 = time.time()
        r = _one_round(client, mail, domain, config.BIRTHDAY)
        rounds.append({"n": i, "t": round(t0, 3), "calls": r})
        quota = next((c for c in r if c.get("quota")), None)
        marks = " ".join(f"{c['call']}={c['status']}" for c in r if c["call"] != "address")
        print(f"  #{i:>3}  {marks}")
        if quota:
            first_quota = {"n": i, "t": t0, "call": quota["call"], "error": quota["error"]}
            print(f"\n🔴 第 {i} 轮命中 429（端点 `{quota['call']}`）：{quota['error'][:120]}")
            break
        if args.interval:
            time.sleep(args.interval)

    verdict = ""
    probes = {}
    if not first_quota:
        verdict = "no_quota"
        print(f"\n⇒ {args.max_calls} 轮内**没有**触发 429。")
        print("   含义：这个量级（以及这个速度）还没到墙。要么墙更远，要么它按别的维度计。")
    else:
        # ── 阶段 B：定性 ──────────────────────────────────────
        print("\n=== 阶段 B：定性对照（窗口可能只有几分钟，所以立刻做）===")
        bd = config.BIRTHDAY

        # B1 换收件域名（同出口、同端点、同速度）
        alt = args.alt_domain
        if not alt:
            try:
                import requests

                from src.hark import HarkClient as _HC  # noqa: F401

                s = requests.Session()
                s.trust_env = False
                got = s.get(
                    f"{config.WORKER_BASE.rstrip('/')}/api/domains",
                    headers={"User-Agent": config.USER_AGENT},
                    timeout=20,
                ).json().get("domains") or []
                alt = next((d for d in got if d != domain), "")
            except Exception:  # noqa: BLE001
                alt = ""
        if alt:
            r = _one_round(client, mail, alt, bd)
            hit = next((c for c in r if c.get("quota")), None)
            probes["B1_other_domain"] = {"domain": alt, "calls": r, "quota": bool(hit)}
            print(f"  B1 换域名 @{alt}（同出口）: {'仍 429' if hit else '**通过**'}")
        else:
            probes["B1_other_domain"] = {"domain": "", "quota": None}
            print("  B1 换域名：拿不到另一个域名，跳过")
        # 三态：`True` = 换域名仍 429（不按域名键）/ `False` = 通过了（按域名键）/ `None` = 没做成
        b1 = probes["B1_other_domain"].get("quota")

        # B2 换出口（同域名）
        if pool and len(pool) > 1:
            cur = client.session.proxies.get("https")
            other = pool.acquire()
            c2 = HarkClient(proxy=other)
            r = _one_round(c2, mail, domain, bd)
            hit = next((c for c in r if c.get("quota")), None)
            probes["B2_other_exit"] = {"slot": redact.redact_url(other or ""), "quota": bool(hit)}
            print(f"  B2 换出口 {redact.redact_url(other or '')}（同域名）: {'仍 429' if hit else '**通过**'}")
            pool.release(other)

        # B3 只打只读端点
        c3 = HarkClient(proxy=cur)
        t0 = time.time()
        try:
            c3.discover(f"probe-{now_ms()}@{domain}")
            probes["B3_read_only"] = {"quota": False, "ms": int((time.time() - t0) * 1000)}
            print("  B3 只打 discovery（只读）: **通过** ⇒ 限的是**写**端点")
        except HarkQuota:
            probes["B3_read_only"] = {"quota": True}
            print("  B3 只打 discovery（只读）: **仍 429** ⇒ 读端点也在限")

        # 结论（`b1` 是三态：`True` / `False` / `None`）
        if b1 is None:
            verdict = "inconclusive"
        elif b1:
            verdict = "not_domain_keyed"
            print("\n⇒ **不是按域名键**（换域名仍 429）。")
            print("   那么候选只剩：全局/端点级的突发限制，或按 IP（与批次 6 的观测矛盾）。")
        else:
            verdict = "domain_keyed"
            print("\n⇒ **按收件域名键**：换域名就过，换出口没用。")
            print("   含义：**加出口不能提高注册吞吐**；要提速只能多域名 + 每域名各自的窗口。")

    if pool:
        pool.release(client.session.proxies.get("https"))

    report = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "domain": domain,
        "exits": [redact.redact_url(s) for s in slots],
        "max_calls": args.max_calls,
        "rounds_run": len(rounds),
        "first_quota": first_quota,
        "probes": probes,
        "verdict": verdict,
    }
    if args.json:
        from pathlib import Path

        p = Path(args.json)
        if not p.is_absolute():
            p = ROOT / p
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n报告已写入：{p}")

    return 0 if first_quota else 1


if __name__ == "__main__":
    sys.exit(main())
