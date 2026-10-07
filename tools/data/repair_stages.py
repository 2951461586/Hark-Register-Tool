#!/usr/bin/env python3
"""修台账里**被错误标记的阶段值**（带备份 + 新快照）。

背景：一个真实发生过的错误标记
==============================
某条记录的 `stages.activate` 是 `ok(mail+verify)`，但 hark 上**这个账号并不存在**
（`account-discovery` → `exists:false`，2026-10-08 实测）。

原因是一个**已经修掉的 bug**：当时 `provision()` 在真正调用
`verify_magic_link()` **之前**就把 `activate` 标成了 ok。那次的 `verify` 调用其实
失败了（URL 拼接 bug：`hark.comhttps://…`），但阶段值已经写上去了。

同一个 bug 还留下了第二处痕迹：`stages.password` 是 `failed`，而
**设密码那一步根本没跑过**（`_failed_stage()` 当时靠“第一个空阶段”猜，
而 activate 已被错误地标成 ok，于是它指到了 password）。所以两处要一起修：

    activate: "ok(mail+verify)" -> "failed"
    password: "failed"          -> ""            （= 没跑到）

⚠ 只改指定的那几处，其余字段一律不动（`error` 文本、`status`/`error_kind`
  都可能是对的）。要改多少记录就得先证明多少条 —— 所以这里**没有默认目标**，
  `--email` 与 `--set` 都是必填。

🔴 为什么不能只“顺手改一下文件”
--------------------------------
台账是账号的**唯一副本**，而 `ledger.save()` 有防静默缩水护栏（条数变少就抛）。
所以这里走正规路径：读读源 → 改 → **写一份新快照**（旧快照留着当回滚点），
并把改动前的原值备份成 JSON。

用法
====
    python tools/data/repair_stages.py --email <账号邮箱> --set <阶段>=<值> [--set …]
    # 例如：
    python tools/data/repair_stages.py --email a@example.com \\
        --set activate=failed --set password= --note "为什么这么改" --apply

⚠ 参数化的原因：这个需求**已经出现两次**（第二次是 `recover_accounts.py`
  补登之后留下的 `password: failed` 与 `login: ok` 自相矛盾）。
  写第二个一次性脚本不如把同一个工具参数化。

⚠ 不写默认邮箱的理由：早前版本把那个真实账号写在源码里当默认值 ——
  那是 🟠 基础设施标识（账号地址），按 `docs/security-conventions.md`
  不该进仓库。工具本来也不该“默认改某一条记录”。
"""

from __future__ import annotations

import argparse
import copy
import json
import sys

from _path import ROOT  # noqa: F401  （副作用：sys.path + 加载 .env）

from src import ledger  # noqa: E402


def _parse_sets(items) -> dict:
    """`["a=b", "c="]` → `{"a": "b", "c": ""}`。

    ⚠ 用 `partition("=")` 而不是 `split("=")`：阶段值里可能有 `=`
      （比如未来的注记），而 `split` 会把它切开或报错。
    """
    out: dict = {}
    for item in items or []:
        if "=" not in item:
            raise SystemExit(f"--set 的格式是 `阶段=值`，收到：{item!r}")
        key, _, val = item.partition("=")
        out[key.strip()] = val
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="修正台账里被错误标记的阶段值")
    ap.add_argument("--apply", action="store_true", help="真的写盘（默认 dry run）")
    ap.add_argument(
        "--email",
        required=True,
        help="目标邮箱。🔴 **没有默认值** —— 工具不该“默认改某一条记录”，"
        "而且那个地址属于基础设施标识，不进仓库。",
    )
    ap.add_argument(
        "--set",
        dest="sets",
        action="append",
        required=True,
        metavar="阶段=值",
        help="要改的阶段值，可重复（例：--set activate=failed --set password=）",
    )
    ap.add_argument("--note", default=None, help="写进 repaired 字段的说明（建议写清为什么）")
    args = ap.parse_args(argv)

    target = args.email
    patches = _parse_sets(args.sets)
    note = args.note or f"{__file__.rsplit('/', 1)[-1]} 手工修正：{patches}"

    src = ledger.ledger_path()
    records = ledger.load_existing(src)
    print(f"读源：{ledger.repo_rel(src)}（{len(records)} 条）")

    hit = [r for r in records if r.get("email") == target]
    if not hit:
        print(f"✗ 找不到 {target} —— 台账里没有这条记录？")
        return 2
    rec = hit[0]

    before = {k: (rec.get("stages") or {}).get(k) for k in patches}
    todo = {k: v for k, v in patches.items() if before[k] != v}
    print(f"\n目标：{target}")
    for k in patches:
        mark = "改" if k in todo else "已是目标值，跳过"
        print(f"  stages.{k}: {before[k]!r} -> {patches[k]!r}   [{mark}]")
    if not todo:
        print("\n没有要改的东西。")
        return 0

    # 修后的完整记录（只动这几处 + 一条可追溯的说明）。
    # ⚠ 用 `copy.deepcopy` 而不是 `json.loads(json.dumps(...))`：后者是一次
    #   隐式的序列化往返，除了多余之外还多一条会抛的路径（而台账里的值
    #   并不是我们自己造的，见 load_existing 的宽容策略）。
    fixed = copy.deepcopy(rec)
    fixed.setdefault("stages", {}).update(todo)
    fixed["repaired"] = note
    print(f"\n将写进 repaired：{note[:80]}…")

    if not args.apply:
        print("\n(dry run —— 加 --apply 才写盘)")
        return 0

    backup = ROOT / "state" / f"repair_stages_backup_{target.split('@')[0]}.json"
    backup.parent.mkdir(parents=True, exist_ok=True)
    backup.write_text(json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n原值已备份 -> {ledger.repo_rel(backup)}")

    merged = [fixed if r is rec else r for r in records]
    if len(merged) != len(records):  # pragma: no cover —— 到不了，但别用 assert
        # 不用 `assert`：断言会被 `python -O` **整个去掉**，那时这里会变成
        # “默默写盘”，而这是台账（账号的唯一副本）。
        raise RuntimeError(
            f"内部错误：修正后条数变了（{len(records)} -> {len(merged)}）—— 拒绝写盘"
        )
    snap = ledger.snapshot_path()
    # `existing=records`：条数没变，护栏不会拦；但它会证明"确实没缩水"。
    written = ledger.save(snap, merged, existing=records)
    print(f"新快照（读源）-> {ledger.repo_rel(written)}")
    print(f"旧快照保留为回滚点 -> {ledger.repo_rel(src)}")

    # 复核
    check = [r for r in ledger.load_existing(ledger.snapshot_path()) if r.get("email") == target][0]
    print("\n复核：")
    print(f"  stages={json.dumps(check.get('stages'), ensure_ascii=False)}")
    print(f"  status={check.get('status')} error_kind={check.get('error_kind')}")
    print(f"  repaired={'有' if check.get('repaired') else '无'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
