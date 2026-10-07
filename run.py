"""CLI 入口。

用法：
  python run.py                          # 跑 1 个账号
  python run.py --count 6                # 跑 6 个（默认 4 路生产者 × 4 路消费者）
  python run.py --count 6 --workers 1 --producers 1    # 强制顺序执行（最保守）
  python run.py --count 6 --workers 8    # 收尾并发 8（纯 HTTP，很快）
  python run.py --out accounts.json      # 显式导出到指定文件（不落台账快照）
  python run.py --ignore-quota           # 跳过本地配额闸门

关于 --out（台账落盘）：
  **默认不填 = 写进台账目录** `ledger/`：
      ledger/runs/<日期>/accounts-<时间戳>.json   ← 合并后的**全量**快照（= 读源）
      ledger/latest.json                          ← **本批结果**（含失败 / 跳过）
  显式 `--out X` 则是"导出到别处"：只写 X、**不落快照**。

  ⚠ 台账的读源是**最新的那份全量快照**，**不是** `latest.json`。后者只含本批
    那几十条，拿它当读源 ⇒ 合并基准只剩上一批 ⇒ 台账停止累积、每次跑批覆盖
    上一次（Intern-Register-Tool 栽过两次）。详见 `src/ledger.py`。

  ⚠ 台账在本项目是账号的**唯一副本**：邮箱是临时邮箱（收不到找回信），
    密码只存在于台账里。所以它整个目录都在 `.gitignore` 中，且**不要**
    把它当临时文件清掉 —— 定期把 `ledger/` 另存一份到别处。

关于 --workers / --producers：
  **本项目零浏览器**（整条链路纯 HTTP，实测不需要验证码 / WAF 解盾），
  所以并发只受两件事约束：邮箱 Worker（建地址 + 收信轮询）和 hark 的写请求限流。

      --producers  同时在"建邮箱 → 申请 magic link → 等信"的账号数。
                   **等信是整链最大单项**（1~3s），所以这个数直接决定吞吐。
      --workers    同时在"设密码 → 密码登录 → 校验"的账号数。每账号 ~1s。

  ⚠ **实测数据尚未积累到能定"安全上限"的程度**（见 docs/protocol.md
    「限流与配额：实测」）。默认 4/4 是保守起点：先把
    **按出口的滑动窗口**（注册申请 4/60s、登录 3/10s，两者都是实测值）才是真正的闸门，
    并发只用来掩盖"等信"的等待。

退出码：
  0 = 至少有一个账号成功（或没有要跑的）
  1 = 本批**全部失败**（配置 / 网络 / 服务端拒绝）
  2 = 本地配额闸门拦下（**一个请求都没发**）
  3 = 台账防静默缩水护栏触发（拒绝写盘，避免丢账号）
  4 = 缺少必需配置
  5 = 内部错误（记录序列化失败）
"""

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import config  # noqa: E402
from src import cli as _cli  # noqa: E402
from src import ledger as _ledger  # noqa: E402
from src import quota as _quota  # noqa: E402
from src import redact  # noqa: E402
from src import report as _report  # noqa: E402
from src.pipeline import STAGES, error_kind_of, run_batch  # noqa: E402
from src.proxypool import SlotPool  # noqa: E402


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Hark 端到端注册（CF Worker 邮箱 + magic link + 密码登录）"
    )
    ap.add_argument("--count", type=int, default=1, help="注册账号数量")
    ap.add_argument(
        "--producers",
        type=int,
        default=4,
        help="生产者并发：同时在「建邮箱→申请 magic link→等信」的账号数（默认 4）",
    )
    ap.add_argument(
        "--workers",
        type=int,
        default=4,
        help="消费者并发：同时在「设密码→登录→校验」的账号数（默认 4）",
    )
    ap.add_argument("--mail-domain", default=None, help="邮箱域名（默认取 HR_WORKER_DOMAIN）")
    ap.add_argument(
        "--birthday",
        default=None,
        help=f"生日（默认 {config.BIRTHDAY}）。🔴 服务端要求满 18，否则要绑卡",
    )
    ap.add_argument(
        "--out",
        default=None,
        help="结果输出文件。**不填（默认）= 写进台账目录**："
        "ledger/runs/<日期>/accounts-<时间戳>.json（合并后的全量快照，也是读源）"
        "+ ledger/latest.json（本批结果）。显式给路径则只写那一个文件、不落快照。",
    )
    ap.add_argument(
        "--overwrite",
        action="store_true",
        help="只写本次结果、不合并历史（默认按 email 合并，防止一次小规模探测覆盖掉整个账号台账）",
    )
    ap.add_argument("--quiet", action="store_true", help="只输出汇总（不逐账号打进度）")
    ap.add_argument(
        "--ignore-quota",
        action="store_true",
        help="跳过本地配额保护（仅当确信服务端没在拦时用）",
    )
    args = ap.parse_args(argv)

    # ── 启动校验：缺凭据就立刻失败，别等跑了一半才发现全是 401 ──────
    missing = config.validate()
    if missing:
        print(f"✗ 缺少必需配置：{'、'.join(missing)}", file=sys.stderr)
        print(
            "  修法：cp .env.example .env 并填入真实值（.env 已在 .gitignore 中，不会进仓库）",
            file=sys.stderr,
        )
        return 4

    # ── 本地配额闸门（**在发任何请求之前**）────────────────────────
    st = _quota.status()
    print(f"本地配额：{st.describe()}  [state: {_quota.state_path()}]", flush=True)
    hint = _quota.shortfall_hint(st.left, args.count, args.ignore_quota)
    if hint:
        print(hint, flush=True)
    if st.exhausted and not args.ignore_quota:
        print(
            "  ⚠ 窗口内计数已达上限（这是**保守估计**，不是服务端拒绝）。\n"
            "    若确信服务端没在拦，可加 --ignore-quota 或调大 HR_REG_QUOTA_MAX。",
            flush=True,
        )
        print(
            "✗ 本地配额闸门拦下了这一批：**未发出任何请求**。"
            "（这是本地保守保护，不是服务端拒绝 —— 详见 src/quota.py）",
            file=sys.stderr,
        )
        return 2

    # ── 代理槽位池 ──────────────────────────────────────────
    # 先把它建出来，好让“池子里有什么”在**跑第一个请求之前**就可见。
    # 🔴 这里**不做 TCP 预检**：Intern-Register-Tool 实测过，本机开着 Clash TUN 时
    #    “连一个不可达端口只要 0.02s”—— 那是连到了本地虚拟网卡，
    #    TCP 能连并不等于代理可用。真要看出口，用 tools/probes/probe_slots.py。
    slots = config.proxy_slots()
    slot_pool = SlotPool(slots, timeout=config.PROXY_SLOT_TIMEOUT)
    if slots:
        print(
            f"🔀 槽位池：{len(slots)} 个出口（{config.HR_PROXY_SLOTS_FILE or 'HR_PROXY_SLOTS'}）",
            flush=True,
        )
        for i, url in enumerate(slots, 1):
            # ⚠ 槽位串可能是 `http://user:pass@host:port`，原样打印会把账密写进
            #   终端 / 重定向的日志。过一遍脱敏。
            print(f"     slot{i} {redact.redact_url(url)}", flush=True)
        print(
            f"   登录限流实测 {config.LOGIN_MAX_PER_WINDOW} 次/{config.LOGIN_WINDOW_S:g}s 且是**按出口**的"
            f" ⇒ 理论上限 ≈ {config.LOGIN_MAX_PER_WINDOW * len(slots)} 次/{config.LOGIN_WINDOW_S:g}s",
            flush=True,
        )
    else:
        print(
            "出口：未配置槽位池"
            + (f"，单代理 {redact.redact_url(config.HR_PROXY)}" if config.HR_PROXY else "（直连）"),
            flush=True,
        )

    t0 = time.time()
    results = run_batch(
        count=args.count,
        workers=args.workers,
        producers=args.producers,
        mail_domain=args.mail_domain,
        birthday=args.birthday,
        verbose=not args.quiet,
        ignore_quota=args.ignore_quota,
        slot_pool=slot_pool,
    )
    wall = time.time() - t0

    # ── 落盘 ──────────────────────────────────────────────────────
    # ⚠ 这里的 `json.loads` 只可能在**我们自己的** `AccountRecord.to_json()`
    #    出问题时抛 —— 那是内部不变量被破坏。**绝不静默丢记录**（少数据但指标
    #    全"正常"是最坏的失败），但要把肇事者说清楚。
    try:
        new_records = [json.loads(r.to_json()) for r in results]
    except json.JSONDecodeError as ex:
        print(f"✗ 内部错误：AccountRecord.to_json() 产出了非法 JSON：{ex}", file=sys.stderr)
        return 5

    out = Path(args.out) if args.out else None
    if args.overwrite:
        merged = new_records
        print(f"\n（--overwrite：只写本次 {len(merged)} 条，不合并历史）")
    else:
        # 🔴 读源用 `out or ledger_path()`：`--out` 不填时**必须**读台账读源。
        #    若这里退化成读"本次要写的那个新文件"，合并就等于没做 ——
        #    每次运行都从零开始，正是"`--count 1` 把 53 条覆盖成 1 条"那条路。
        #    ⚠ 也不能读 `latest.json`（本批结果）：它只含上一批，同样会让台账
        #      停止累积 —— 这正是它**不是**读源的原因。
        existing = _ledger.load_existing(out or _ledger.ledger_path())
        merged, kept, added, upgraded = _ledger.merge_records(existing, new_records)
        if kept:
            print(_cli.merge_summary_line(kept, added, upgraded, len(merged)))

    try:
        # 🔴 判据是 `out is None`（= 用户没给 `--out`），**不是**
        #    `ledger.is_ledger_path(out)`。两者在"显式 `--out` 指向 ledger/ 内部"
        #    这一种输入下**行为不同**：本入口走纯导出（不落快照，见 --out 的
        #    help 承诺）。这是**有意的**，别"顺手统一"。
        if out is None:
            # `new_records` 是**本批原始**结果（含失败 / 跳过）→ latest.json；
            # `merged` 是合并后的**全量** → 快照（也是新的读源）。
            snap, _last = _ledger.save_snapshot(
                merged, new_records, existing=[] if args.overwrite else None
            )
            written = snap
            print(
                f"\n台账已落盘：\n"
                f"  快照（读源） {_ledger.repo_rel(snap)}\n"
                f"  本批结果     {_ledger.repo_rel(_ledger.last_run_path())}"
                f"（{len(new_records)} 条）"
            )
        else:
            written = _ledger.save(out, merged, existing=[] if args.overwrite else None)
    except ValueError as ex:
        # 防静默缩水护栏（src/ledger）。少数据但指标全"正常"是最坏的失败，
        # 宁可报错退出也不要静默丢掉账号。
        print(f"✗ {ex}", file=sys.stderr)
        return 3

    # 收尾报告整块在 `src/report.py`（纯展示 + 依赖注入），这里只接线。
    #
    # ⚠ 下面这两个参数（`slot_pool` / `redact_url`）曾被静态分析报成
    #   “No parameter named …”，而它们**确实在** `src/report.py` 的签名里。
    #   根因是分析器读了该文件的**旧缓存**（旧签名里恰好有 stages / repo_rel /
    #   redact_email，只缺这两个 —— 与报错完全对应），不是代码问题：
    #   这一调用点在实测里跑过 50 个账号。
    #   真正的契约由 `tests/test_cli_contract.py` 动态钉住（用 AST 取调用点的
    #   关键字，对**本项目**的签名逐个核）—— 假红线的代价不是噪音，
    #   是让人学会忽略闸门，所以那条检查被用正确的模块解析重做了一遍。
    qline = _quota.status().describe()
    _report.render_batch_report(
        results,
        written,
        wall,
        stages=STAGES,
        error_kind_of=error_kind_of,
        quota_line=qline,
        workers=args.workers,
        repo_rel=_ledger.repo_rel,
        redact_email=redact.redact_email,
        slot_pool=slot_pool,
        redact_url=redact.redact_url,
    )

    ok = sum(1 for r in results if r.status == "success")
    if results and not ok:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
