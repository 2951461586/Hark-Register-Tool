#!/usr/bin/env python3
"""测出**注册侧 429** 是按什么键限的、额度多大、窗口多长。

为什么需要它
============
2026-10-08 的 10 批实测里，批次 6 出现 3 个注册侧 429，而它们**同一瞬间命中了
3 个不同出口**（7903/7905/7904）。当时据此判定“不是 per-IP”，并写进了 README ——
🔴 **那个结论是错的**，本工具的对照实验推翻了它：

    阶段 A：同出口第 6 次 `magic-link` → 429（discovery / signup-age 一直 200）
    阶段 B：B1 换收件域名仍 429   ⇒ 不是按域名键
            B2 **换出口立刻通过** ⇒ **是按 IP 键**
            B3 只打只读端点通过   ⇒ 只限写端点 `magic-link`

**当时推错的原因**：那 3 个出口各自在前 60s 里都积了 ~5 次（每批 1 次 × 5 批），
所以它们**同时**触顶。**同时性 ≠ 同一个键** —— 它只说明它们的**历史相同**。
一个“同时发生”的观测区分不了“共享一个计数器”与“各自独立但节奇一致”；
要定性必须**只变一个变量**（B1 变域名、B2 变出口）。

已知：额度 ≈ **5 次/分钟/IP**，限 `magic-link`，按客户端 IP。
本工具另外提供 `--measure-window` 去测**窗口长度**（之前只观察到 ≤43s 就恢复）。

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
    python tools/probes/probe_register_limit.py                  # 阶段 A+B（默认最多 40 轮）
    python tools/probes/probe_register_limit.py --measure-window # 测窗口长度
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
            out.append(
                {"call": name, "status": 200, "ms": int((time.time() - t0) * 1000), "error": ""}
            )
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


def _trip_quota(client, mail, domain, birthday, *, cap_s: float = 120.0, log=print):
    """打到 429 为止。返回 `(命中轮数, 命中信息)`；打不出来返回 `(n, None)`。"""
    t0 = time.time()
    n = 0
    while time.time() - t0 < cap_s:
        n += 1
        r = _one_round(client, mail, domain, birthday)
        hit = next((c for c in r if c.get("quota")), None)
        if hit:
            return n, hit
    return n, None


def measure_window(
    client, mail, domain: str, birthday: str, *, poll_s: float, cap_s: float, log=print
) -> dict:
    """测**窗口长度**：先打到 429，然后定时探一次直到通过。

    🔴 为什么这么测：突发的几个成功请求几乎同时（t≈0），所以“窗口滑出”的时刻
      ≈ “恢复通过”的时刻。轮询粒度就是这次测量的精度。

    ⚠ **一个没有完全排除的可能**：被 429 拒掉的那次尝试**是否也计入窗口**，
      我们不知道。如果计入，那每个失败的探测都会把窗口往后推，
      于是测出的值是一个**上界**。所以这里跑第二段**对照**：
      再打到 429，等 `测得值 + 5s`，只探一次 ——
      通过则与第一段自洽；仍 429 则说明失败探测确实会推窗口（真值更大）。
    """
    log("  第一段：打到 429 …")
    trips, hit = _trip_quota(client, mail, domain, birthday, cap_s=cap_s, log=log)
    if hit is None:
        log(f"  ✗ {trips} 轮内没打到 429，无法测窗口")
        return {"tripped": False, "trips": trips}
    log(f"  🔴 第 {trips} 轮命中（{hit['call']}）—— 开始按 {poll_s:.0f}s 粒度探测恢复")
    t_trip = time.time()
    probes = []
    recovered = None
    while time.time() - t_trip < cap_s:
        time.sleep(poll_s)
        r = _one_round(client, mail, domain, birthday)
        ok = not any(c.get("quota") for c in r)
        elapsed = round(time.time() - t_trip, 1)
        probes.append({"t": elapsed, "ok": ok})
        log(f"    t+{elapsed:>5.1f}s  {'通过 ✅' if ok else '仍 429'}")
        if ok:
            recovered = elapsed
            break

    control = None
    if recovered is not None:
        log(f"\n  第二段（对照）：再打到 429，然后等 {recovered + 5:.0f}s 只探一次 …")
        trips2, hit2 = _trip_quota(client, mail, domain, birthday, cap_s=cap_s, log=log)
        if hit2 is None:
            control = {"tripped": False}
            log("  （对照段没打到 429，跳过）")
        else:
            wait = recovered + 5.0
            log(f"  等 {wait:.0f}s …")
            time.sleep(wait)
            r = _one_round(client, mail, domain, birthday)
            ok = not any(c.get("quota") for c in r)
            control = {"tripped": True, "waited_s": wait, "passed": ok}
            log(
                f"  {'通过 ✅ 与第一段自洽' if ok else '仍 429 ⇒ 失败探测也会推窗口，真值 > 第一段'}"
            )

    return {
        "tripped": True,
        "trips_to_trip": trips,
        "recovered_after_s": recovered,
        "poll_s": poll_s,
        "probes": probes,
        "control": control,
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="测注册侧 429 的键")
    ap.add_argument("--max-calls", type=int, default=40, help="最多几轮注册申请（默认 40）")
    ap.add_argument(
        "--measure-window", action="store_true", help="改测**窗口长度**（不再做 B1/B2/B3 定性）"
    )
    ap.add_argument("--poll", type=float, default=10.0, help="测窗口时的探测粒度秒数（默认 10）")
    ap.add_argument(
        "--interval", type=float, default=0.0, help="轮之间的间隔秒数（默认 0 = 尽快打）"
    )
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

    if args.measure_window:
        win = measure_window(
            client, mail, domain, config.BIRTHDAY, poll_s=args.poll, cap_s=600.0, log=print
        )
        report = {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "mode": "measure_window",
            "domain": domain,
            "exits": [redact.redact_url(s) for s in slots],
            "window": win,
        }
        if args.json:
            from pathlib import Path

            p = Path(args.json)
            if not p.is_absolute():
                p = ROOT / p
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"\n报告已写入：{p}")
        if pool:
            pool.release(client.session.proxies.get("https"))
        return 0 if win.get("recovered_after_s") else 1

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
                got = (
                    s.get(
                        f"{config.WORKER_BASE.rstrip('/')}/api/domains",
                        headers={"User-Agent": config.USER_AGENT},
                        timeout=20,
                    )
                    .json()
                    .get("domains")
                    or []
                )
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
        #
        # 🔴 `cur`（本批用的出口）必须在**分支外**取：B3 也要用它。
        #    早前它在 `if` 里赋值，于是没配池子（或只有 1 个出口）时
        #    B3 直接 `UnboundLocalError` —— 而那条路径恰恰是**最常用**的
        #    （单出口跑探测）。这个 bug 是 `tests/test_tools_io.py` 抓到的：
        #    它把 `_one_round` 换掉、不给池子，于是走到了那条分支。
        cur = client.session.proxies.get("https")
        if pool and len(pool) > 1:
            other = pool.acquire()
            c2 = HarkClient(proxy=other)
            r = _one_round(c2, mail, domain, bd)
            hit = next((c for c in r if c.get("quota")), None)
            probes["B2_other_exit"] = {"slot": redact.redact_url(other or ""), "quota": bool(hit)}
            print(
                f"  B2 换出口 {redact.redact_url(other or '')}（同域名）: {'仍 429' if hit else '**通过**'}"
            )
            pool.release(other)
        else:
            probes["B2_other_exit"] = {"slot": "", "quota": None}
            print("  B2 换出口：池子里不到 2 个出口，跳过（这条对照测不了）")

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
