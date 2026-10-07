#!/usr/bin/env python3
"""槽位池体检：每个出口的**真实出口 IP**、hark 可达性、以及登录限流是否按出口。

为什么必须"真的发一个请求"
==========================
🔴 **别用 TCP 连通性判断代理可用**。Intern-Register-Tool 实测过：本机开着
   Clash TUN 时，连接一个不可达端口只要 **0.02s** —— 那是连到了本地虚拟网卡，
   不是连到了代理。中国到美国不可能是 20ms。
   所以唯一可信的判据是"通过它拿到出口 IP"。

为什么还要单独测 hark 可达
==========================
一个代理能通 `api.ipify.org`，不代表能通 `hark.com`（代理商按域名做 ACL 是常事）。
本工具因此对每个出口打三件事，而不是一件：

    ① `api.ipify.org`          → 出口 IP（证明代理真的在工作）
    ② `hark.com/api/auth/account-discovery` → 目标站可达（只读，无副作用）
    ③ 出口是否两两不同         → 池子的**全部价值**就在这一条上

⚠ ② 用的是 `account-discovery`（只读、无副作用），**不是** `magic-link`：
  后者会真的发信、会消耗"注册申请"的突发预算。要测写路径请用
  `tools/probes/probe_magic_link.py`。

用法
====
    python tools/probes/probe_slots.py                  # 读 HR_PROXY_SLOTS / 文件
    python tools/probes/probe_slots.py --slots "http://127.0.0.1:7901,http://127.0.0.1:7902"
    python tools/probes/probe_slots.py --json state/slot_probe.json

退出码：0 = 全部出口可用且 IP 两两不同；1 = 有出口不可用；2 = 有出口 IP 重复；
        3 = 未配置槽位池
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

from _path import ROOT  # noqa: F401  （副作用：sys.path + 加载 .env）

from common import config  # noqa: E402
from src import redact  # noqa: E402
from src.hark import HarkClient, HarkError  # noqa: E402
from src.tempmail import now_ms  # noqa: E402

IP_ECHO = "https://api.ipify.org"


def probe_slot(url: str) -> dict:
    """一个出口 → `{slot, exit_ip, hark, error}`。**绝不抛**。"""
    import requests

    out = {"slot": url, "exit_ip": "", "hark": 0, "error": ""}
    s = requests.Session()
    s.trust_env = False  # 🔴 否则环境代理会静默盖掉 session.proxies
    s.proxies = {"http": url, "https": url}
    s.headers.update({"User-Agent": config.USER_AGENT})
    try:
        r = s.get(IP_ECHO, timeout=25)
        out["exit_ip"] = r.text.strip()
    except Exception as ex:  # noqa: BLE001
        out["error"] = f"取出口 IP 失败：{type(ex).__name__}: {str(ex)[:80]}"
        return out
    # 目标站可达性：只读端点，无副作用。
    try:
        c = HarkClient(proxy=url)
        c.discover(f"probe-{now_ms()}@{config.WORKER_DOMAIN or 'example.com'}")
        out["hark"] = 200
    except HarkError as ex:
        out["hark"] = getattr(ex, "status", 0)
        out["error"] = f"hark 探测：{ex}"
    except Exception as ex:  # noqa: BLE001
        out["error"] = f"hark 探测异常：{type(ex).__name__}"
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="槽位池体检（出口 IP / hark 可达 / 是否两两不同）")
    ap.add_argument("--slots", default="", help="逗号分隔的槽位；不填则读 HR_PROXY_SLOTS / 文件")
    ap.add_argument("--json", default=None, help="把报告写成 JSON（相对仓库根）")
    args = ap.parse_args(argv)

    if args.slots.strip():
        slots = [s.strip() for s in args.slots.split(",") if s.strip()]
    else:
        slots = config.proxy_slots()
    if not slots:
        print("✗ 没有配置槽位池（HR_PROXY_SLOTS / HR_PROXY_SLOTS_FILE 都是空的）")
        return 3

    print(f"═══ 槽位池体检：{len(slots)} 个出口 ═══")
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=min(8, len(slots))) as ex:
        rows = list(ex.map(probe_slot, slots))
    wall = time.time() - t0

    for r in rows:
        mark = "✓" if (r["exit_ip"] and r["hark"] == 200) else "✗"
        print(
            f"  {mark} {redact.redact_url(r['slot']):<40} "
            f"出口 {r['exit_ip'] or '(取不到)':<18} hark {r['hark'] or '-'}"
            + (f"  {r['error'][:80]}" if r["error"] else "")
        )

    ips = [r["exit_ip"] for r in rows if r["exit_ip"]]
    dup = {ip: n for ip, n in Counter(ips).items() if n > 1}
    bad = [r for r in rows if not r["exit_ip"] or r["hark"] != 200]

    report = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "wall_s": round(wall, 1),
        "slots": [{**r, "slot": redact.redact_url(r["slot"])} for r in rows],
        "distinct_ips": len(set(ips)),
        "duplicate_ips": dup,
    }
    print(
        f"\n不同出口 IP：{len(set(ips))} / {len(rows)}"
        + (f"　⚠ 重复：{dup}（这些槽位共用一个出口，池子的并发收益为 0）" if dup else "")
    )
    if ips:
        # 登录限流实测 3 次/10s 且按出口 ⇒ 池子把上限乘上"不同出口数"。
        print(
            f"登录理论上限 ≈ {config.LOGIN_MAX_PER_WINDOW * len(set(ips))} 次/"
            f"{config.LOGIN_WINDOW_S:g}s（实测 {config.LOGIN_MAX_PER_WINDOW} 次/"
            f"{config.LOGIN_WINDOW_S:g}s × {len(set(ips))} 个不同出口）"
        )

    if args.json:
        from pathlib import Path

        p = Path(args.json)
        if not p.is_absolute():
            p = ROOT / p
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"报告已写入：{p}")

    if dup:
        return 2
    if bad:
        return 1
    print("⇒ 全部出口可用，且出口 IP 两两不同。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
