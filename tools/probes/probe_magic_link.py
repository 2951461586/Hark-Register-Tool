#!/usr/bin/env python3
"""探测 `sign-in/magic-link` 端点：**是服务端故障，还是我们的出口被封了**。

为什么需要这个探针
==================
2026-10-07 实测到一次"整批注册全失败"，`error_kind=network`，
错误是 `HTTP 500：(空响应体)`，而且**只有 magic-link 这一个端点**在 500 ——
同一时刻、同一出口，`account-discovery` 与 `signup-age` 都还是 200。

这两种原因的**修法完全相反**，而现象一模一样：

| 真实原因 | 现象 | 修法 |
|---|---|---|
| 服务端发信链路挂了 | 所有出口、所有收件人、新号老号都 500 | 等；别烧配额 |
| 我们的**出口 IP** 被这个端点限流 | 只有这个出口 500，换出口就 200 | 换出口 |

所以判据必须是**对照实验**，而不是"看错误码"：

    控制组：account-discovery（同一出口、同一时刻）
    对照组：sign-in/magic-link
    变量  ：出口 IP（`--proxy` 给多个）

三种结论：

* 控制组也失败        → 出口整体不可用 / 站点故障（**先别跑批量**）
* 控制组 200、目标全 500（含换出口）→ **端点级故障**（服务端最可能）
* 控制组 200、目标部分 500         → **出口 IP 维度**的限流 ⇒ 换出口有用

用法
====
    python tools/probes/probe_magic_link.py
    python tools/probes/probe_magic_link.py --proxy http://127.0.0.1:7901
    python tools/probes/probe_magic_link.py --proxy a:1,b:2 --json state/probe.json

退出码：0 = magic-link 可用；1 = 目标端点失败；2 = 控制组就失败了；
        3 = 结果混合（部分出口可用 —— 那说明是出口维度）
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
from src.hark import HarkClient, HarkError  # noqa: E402
from src.tempmail import now_ms  # noqa: E402


def probe_controls(proxy: str | None) -> tuple:
    """控制组：`account-discovery` + `signup-age`。返回 `(ok, detail)`。

    ⚠ 用**一个全新的地址**：`account-discovery` 对不存在的地址也返回 200，
      但它返回的 `exists` 会污染判断（若拿真账号的地址，成功也可能是"账号存在"
      这条别的分支给的）。全新地址让它走"新号"这条最常见的路径。
    """
    addr = f"probe-{now_ms()}@{config.WORKER_DOMAIN or 'example.com'}"
    c = HarkClient(proxy=proxy)
    try:
        c.discover(addr)
        c.signup_age(addr, config.BIRTHDAY)
    except HarkError as ex:
        return False, f"{type(ex).__name__}: {ex}"
    return True, "account-discovery + signup-age 均 200"


def probe_target(proxy: str | None, email: str | None = None) -> tuple:
    """对照组：`sign-in/magic-link`。返回 `(ok, status, detail)`。"""
    addr = email or f"probe-{now_ms()}@{config.WORKER_DOMAIN or 'example.com'}"
    c = HarkClient(proxy=proxy)
    try:
        c.request_magic_link(addr)
    except HarkError as ex:
        return False, getattr(ex, "status", 0), f"{type(ex).__name__}: {ex}"
    return True, 200, "200"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="探测 magic-link 端点（对照实验）")
    ap.add_argument("--proxy", default="", help="逗号分隔的出口列表（不填 = 只用直连）")
    ap.add_argument("--email", default=None, help="目标端点用的收件人（默认每次新生成）")
    ap.add_argument("--json", default=None, help="把报告写成 JSON（相对仓库根）")
    args = ap.parse_args(argv)

    egresses: list[tuple] = [("direct", None)]
    for item in (args.proxy or "").split(","):
        item = item.strip()
        if item:
            egresses.append((item, item))

    report: dict = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "egresses": [],
        "verdict": "",
    }
    print("═══ magic-link 端点探测 ═══")
    control_ok, control_detail = probe_controls(None)
    print(f"控制组（直连）：{'✓' if control_ok else '✗'} {control_detail}")
    report["control"] = {"ok": control_ok, "detail": control_detail}

    results = []
    for label, px in egresses:
        ok, status, detail = probe_target(px, args.email)
        results.append({"egress": label, "ok": ok, "status": status, "detail": detail})
        print(f"  目标端点 [{label}]：{'✓' if ok else '✗'} HTTP {status} {detail[:120]}")
        report["egresses"] = results
        time.sleep(0.5)

    oks = [r for r in results if r["ok"]]
    if not control_ok:
        report["verdict"] = "control_failed"
        print("\n⇒ 控制组就失败了：**出口整体不可用或站点故障**。先别跑批量。")
        code = 2
    elif len(oks) == len(results):
        report["verdict"] = "ok"
        print("\n⇒ magic-link 可用。")
        code = 0
    elif oks:
        report["verdict"] = "egress_specific"
        print(
            f"\n⇒ **出口维度**：{len(oks)}/{len(results)} 个出口可用。"
            f"换出口有用 —— 把可用的那个写进 HR_PROXY，或给它加槽位。"
        )
        code = 3
    else:
        report["verdict"] = "endpoint_down"
        print(
            "\n⇒ 控制组 200、目标端点**全部**失败 ⇒ 大概率是**端点级故障**"
            "（服务端发信链路），不是我们被封。\n"
            "   注意：若所有 `--proxy` 其实共用同一个出口 IP（实测常见：\n"
            "   本地 Clash 的混合端口对外仍是同一个 IP），这个结论就**不成立** ——\n"
            "   先确认各出口的 `api.ipify.org` 真的不同，再下判断。"
        )
        code = 1

    if args.json:
        p = Path(args.json)
        if not p.is_absolute():
            p = ROOT / p
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"报告已写入：{p}")
    return code


if __name__ == "__main__":
    sys.exit(main())
