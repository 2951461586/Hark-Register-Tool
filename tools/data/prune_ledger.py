#!/usr/bin/env python3
"""从台账里删掉指定状态的记录（默认只删 `failed`）。

为什么需要它
============
台账会累积失败记录：一次 magic-link 宕机、一次限流、一个收不到信的域名，
都会留下一条 `failed`。它们**有价值**（是排查证据），但攒多了会淹没真正的账号。

🔴 但"删记录"正是台账最该防的动作
--------------------------------
`ledger.save()` 有**防静默缩水护栏**（合并后条数变少就抛异常），理由是
"少数据但指标全绿"是本项目最坏的失败形态 —— 台账是账号的**唯一副本**。
所以本工具不绕过护栏，而是**显式地走另一条路**：

    `ledger.save(..., existing=None)` 的语义是"这次写的就是全量"（不是合并），
    于是护栏不参与 —— 那是**有意**的入口，不是漏洞。

配套三道保险：
  1. **只删 `failed`**（默认）。要删 `success` 必须再加 `--allow-success` ——
     防的是 `--status success` 这种手滑（那会删掉全部账号）。
  2. **先备份整本**（删之前的全量写进 `state/`），并在输出里给出路径。
  3. **打印删了哪些**（email + 原因），别让人事后猜。

用法
====
    python tools/data/prune_ledger.py                     # dry run，只删 failed
    python tools/data/prune_ledger.py --apply
    python tools/data/prune_ledger.py --status failed,skipped --apply

退出码：0 = 成功（含 dry run）；2 = 参数被拒（例如要删 success 但没给 --allow-success）；
        3 = 没有可删的记录
"""

from __future__ import annotations

import argparse
import json
import sys
import time

from _path import ROOT  # noqa: F401  （副作用：sys.path + 加载 .env）

from src import ledger  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="从台账里删掉指定状态的记录")
    ap.add_argument("--status", default="failed", help="要删的状态，逗号分隔（默认 failed）")
    ap.add_argument("--apply", action="store_true", help="真的写盘（默认 dry run）")
    ap.add_argument(
        "--allow-success",
        action="store_true",
        help="🔴 允许删 status=success 的记录（那等于删账号，必须显式给）",
    )
    args = ap.parse_args(argv)

    wanted = {s.strip() for s in args.status.split(",") if s.strip()}
    if not wanted:
        print("✗ --status 是空的")
        return 2
    if "success" in wanted and not args.allow_success:
        print(
            "✗ 拒绝执行：`--status` 里含 `success`，那会删掉**账号**（台账是唯一副本）。\n"
            "  确实要这么做就显式加 --allow-success。",
            file=sys.stderr,
        )
        return 2

    src = ledger.ledger_path()
    records = ledger.load_existing(src)
    doomed = [r for r in records if (r.get("status") or "") in wanted]
    kept = [r for r in records if (r.get("status") or "") not in wanted]

    print(f"读源：{ledger.repo_rel(src)}（{len(records)} 条）")
    print(f"要删的状态：{sorted(wanted)} ⇒ 命中 {len(doomed)} 条，保留 {len(kept)} 条\n")
    for r in doomed:
        print(
            f"  − {(r.get('email') or '(无邮箱)'):<44} "
            f"[{r.get('status')}/{r.get('error_kind') or '-'}] {str(r.get('error'))[:70]}"
        )
    if not doomed:
        print("没有可删的记录。")
        return 3

    if not args.apply:
        print("\n(dry run —— 加 --apply 才写盘；写盘前会先备份整本)")
        return 0

    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup = ROOT / "state" / f"ledger_before_prune_{stamp}.json"
    backup.parent.mkdir(parents=True, exist_ok=True)
    backup.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n整本已备份 -> {ledger.repo_rel(backup)}")

    # 🔴 `existing=None` = “这次写的就是全量”，护栏不参与 —— 这是**有意**的入口。
    snap = ledger.snapshot_path()
    written = ledger.save(snap, kept)
    ledger.save(ledger.last_run_path(), kept)
    print(f"新快照（读源）-> {ledger.repo_rel(written)}")
    print(f"本批结果     -> {ledger.repo_rel(ledger.last_run_path())}（整本重写 = 全量）")
    print(f"旧快照保留为回滚点 -> {ledger.repo_rel(src)}")

    check = ledger.load_existing(ledger.snapshot_path())
    dist: dict = {}
    for r in check:
        dist[str(r.get("status"))] = dist.get(str(r.get("status")), 0) + 1
    print(f"\n复核：{len(check)} 条，状态分布 {dist}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
