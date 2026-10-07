"""账号台账（`ledger/` 目录）的读写与合并。

台账长什么样
------------
    ledger/runs/2026-10-07/accounts-20261007-183012.json   ← **读源**（全量台账）
    ledger/runs/2026-10-07/accounts-20261007-180955.json   ← 上一份快照（回滚点）
    ledger/latest.json                                     ← **本批结果**（含失败/跳过）

两个文件职责**不同**，混用是本模块最大的坑：

  * `runs/` 里的快照 = **按 email 合并后的累计全量**；读源是**最新那一份**；
  * `latest.json`     = **最近一次落盘写进去的记录**（跑批 = 本批，含失败/跳过）。

🔴 为什么 `latest.json` 不能当读源
---------------------------------
它只含本批那几十条。拿它当读源 ⇒ 下一次运行合并的基准只有上一批 ⇒
**台账停止累积、每次跑批覆盖上一次**。Intern-Register-Tool 栽过两次，
第二次是 `run.py --count 1`（探测服务端是否解封）把 53 条台账**覆盖成 1 条**。

而"账号台账"在本项目是**唯一副本**：邮箱是临时邮箱（收不到找回信），
密码只存在于这里。台账一丢，那批账号就永久失去访问凭据 —— 且**不报错**。
所以取台账路径**一律**用 `ledger_path()`，别自己拼、也别拿 `latest.json` 顶替。

为什么要单起目录
----------------
台账同时是 `run.py --out` 的默认目标，所以**一次小规模运行就可能把它覆盖掉**。
按日期分目录 + 时间戳命名之后，快照本身就是可回滚点。

🔴 与 Intern-Register-Tool 的一处**刻意裁剪**
-------------------------------------------
那边还有 `merge_fragments()`（给 `tools/data/restore_results.py` 从散落来源
**重建**台账用）。本项目没有"重建"这条路 —— 没有可重建的来源（那边是
`export_keys` 的导出行），所以**不引入**。等真需要时再补，并连它的用例一起补
（那边的教训是：`merge_records` 与 `merge_fragments` 看着像重复，
其实降级行为必须不同，统一会丢数据）。
"""

import json
import re
import time
from pathlib import Path

from . import fsutil

# 台账目录：仓库根下的 `ledger/`（整个目录被 .gitignore 忽略 —— 里面是明文凭据）。
# ⚠ 用 `Path(__file__).resolve().parents[1]` 而不是 `Path.cwd()`：cwd 是调用者的，
#   从别的目录跑 `python tools/xxx.py` 会把台账写到别处去。
ROOT = Path(__file__).resolve().parents[1]
LEDGER_DIR = ROOT / "ledger"
RUNS_DIRNAME = "runs"
SNAPSHOT_STEM = "accounts"
# **本批结果**的文件名。⚠ 它不是台账、**不能当读源**（见模块 docstring）。
LAST_RUN_NAME = "latest.json"

# 快照文件名的**严格**形状：`accounts-YYYYMMDD-HHMMSS.json`。
# 🔴 用正则筛，不用 `glob("*.json")`：`runs/` 是个人工可写的目录，一个
#    `notes.json` / `accounts.json.bak` 落进去，按"最新"就会被当成台账读源 ——
#    于是缩水护栏的分母、报告的分母全跟着变，而且**全程不报错**。
SNAPSHOT_RE = re.compile(rf"^{SNAPSHOT_STEM}-(\d{{8}})-(\d{{6}})\.json$")

# 记录"优劣"排序：成功 > 跳过 > 失败。合并时不让失败盖掉成功。
_RANK = {"success": 2, "skipped": 1}


def rank(rec: dict) -> int:
    # `or ""` 不是多余的：`status` 缺失（或显式为 None）时，`dict.get` 的默认
    # 只是"没这个键"，而把 `None` 当**键**传给 `get` 在类型上就是错的。
    return _RANK.get(rec.get("status") or "", 0)


def snapshot_path(when=None) -> Path:
    """新快照的落点：`ledger/runs/<YYYY-MM-DD>/accounts-<YYYYMMDD-HHMMSS>.json`。

    按**本地日期**分目录（复盘时想的是"那天那批"），文件名用 `YYYYMMDD-HHMMSS`
    时间戳。`when` 传 epoch 秒（不传 = 现在）—— 存在的唯一理由是让测试能钉住
    路径格式，生产代码不该传。
    """
    t = time.localtime() if when is None else time.localtime(when)
    return (
        LEDGER_DIR
        / RUNS_DIRNAME
        / time.strftime("%Y-%m-%d", t)
        / f"{SNAPSHOT_STEM}-{time.strftime('%Y%m%d-%H%M%S', t)}.json"
    )


def snapshot_paths() -> list:
    """`runs/` 下所有**命名合规**的快照，按时间**升序**。

    排序键取**文件名里的时间戳**，不取 mtime —— mtime 会被 `cp -p` / 解压 /
    同步工具改掉，而文件名是落盘那一刻写死的。`YYYYMMDD-HHMMSS` 定长零填充，
    所以字典序就是时间序。
    """
    runs = LEDGER_DIR / RUNS_DIRNAME
    if not runs.is_dir():
        return []
    found = [p for p in runs.glob(f"*/{SNAPSHOT_STEM}-*.json") if SNAPSHOT_RE.match(p.name)]
    return sorted(found, key=lambda p: (p.parent.name, p.name))


def ledger_path() -> Path:
    """台账的**唯一读源**：`runs/` 里**最新的那份全量快照**。

    🔴 每次调用都**重新扫目录**，不要冻进模块级常量：那种"先读台账 → 跑 →
    再写回台账"的流程里，常量会一直指向**落盘之前**那份快照 ⇒
    写回去等于覆盖掉刚生成的新快照。

    没有任何快照时返回一个**永不存在的占位路径**：让 `load_existing()` 按契约
    返回 `[]`，而不是抛异常 —— 一个空的台账目录不该让工具起不来。
    """
    snaps = snapshot_paths()
    if snaps:
        return snaps[-1]
    return LEDGER_DIR / RUNS_DIRNAME / "__none__" / f"{SNAPSHOT_STEM}-none.json"


def last_run_path() -> Path:
    """**本批结果**的落点：`ledger/latest.json`。

    ⚠ 这不是台账、**不能当读源**。读台账一律用 `ledger_path()`。
    """
    return LEDGER_DIR / LAST_RUN_NAME


def is_ledger_path(path) -> bool:
    """这个路径是不是落在**项目台账目录**里（而不是随便一个导出文件）。

    判据是"在 `LEDGER_DIR` 底下"，**不是**"等于读源" —— 读源每次落盘都换名字，
    拿它做相等比较必然漏。
    """
    try:
        return Path(path).resolve().is_relative_to(LEDGER_DIR.resolve())
    except (OSError, ValueError):
        return False


def load_existing(path) -> list[dict]:
    """读现有台账。文件缺失 / 损坏 / 不是 list 都返回 `[]`，不抛异常。

    刻意不抛：一个坏掉的结果文件不该让整次运行失败 —— 那是"清理一次数据
    反而连活都干不了"。真正的护栏在 `save()` 的条数检查上。
    """
    p = Path(path)
    if not p.is_file():
        return []
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return []
    return [r for r in d if isinstance(r, dict)] if isinstance(d, list) else []


def merge_records(existing: list[dict], new: list[dict]):
    """按 `email` 合并，返回 `(merged, 原有条数, 新增, 覆盖数)`。

    规则：
      - 按 `email` 去重；没有 `email` 的记录原样保留（不参与去重）
      - 同一 email：**不让失败盖掉成功**（服务端抖一下不该把好账号标成坏）
      - 同一 email 且 rank 相同时：**取并集**（`{**旧, **新}`）——
        新值胜出，但旧记录里独有的字段一个都不丢
      - 老记录里本次没跑到的，**保留** —— 它们是历史，不是垃圾

    🔴 为什么同级要取并集，而不是"整条替换"或"比字段数"
    -------------------------------------------------
    Intern-Register-Tool 实测踩到：下游交回的是**增量字段**（没有 `status`）
    ⇒ rank 恒为 0，而旧记录 rank 要么 2 要么 0 ⇒ `rank(new) > rank(old)`
    **永远为假**：下游跑了半天，字段一个都没写进台账，而打印出来的一切都"正常"。
    这正是最该警惕的一类失败 —— **数据静默缩水，指标全绿**。

    也试过"比非空字段个数"，同样是启发式：两边字段数**相等**时照样丢字段
    （那边的自测 T9 当场抓到）。**并集没有这个漏洞** —— 它不靠猜，
    数学上保证字段只增不减。
    """
    idx: dict[str, int] = {}
    merged: list[dict] = []
    for rec in existing:
        email = rec.get("email")
        if email and email in idx:
            continue  # 同 email 的旧重复记录，留第一条
        if email:
            idx[email] = len(merged)
        merged.append(rec)

    kept = len(merged)
    added = upgraded = 0
    for rec in new:
        email = rec.get("email")
        if not email or email not in idx:
            if email:
                idx[email] = len(merged)
            merged.append(rec)
            added += 1
            continue
        pos = idx[email]
        cur = merged[pos]
        r_new, r_old = rank(rec), rank(cur)
        if r_new > r_old:
            merged[pos] = rec  # 升级：整体替换（如 failed→success）
            upgraded += 1
        elif r_new == r_old:
            union = {**cur, **rec}  # 同级：并集，新值胜出
            if union != cur:
                merged[pos] = union
                upgraded += 1
    return merged, kept, added, upgraded


def save(path, records: list[dict], *, existing: list | None = None) -> Path:
    """写台账（原子替换）。**默认带防静默缩水护栏**。

    `existing` 给定时，若 `len(records) < len(existing)` 就抛 `ValueError`。
    传 `existing=None`（默认）表示"这次写的就是全量，不做比较" ——
    调用方在**合并之后**调用本函数时应当把合并前的条数传进来。

    🔴 宁可报错也不静默丢账号：少数据但指标全"正常"是最坏的失败形态。
      要显式覆盖得用 `--overwrite`（那条路传 `existing=[]`）。
    """
    if existing is not None and len(records) < len(existing):
        raise ValueError(
            f"防静默缩水护栏：本次要写 {len(records)} 条，但原有 {len(existing)} 条"
            f" —— 拒绝写盘（会丢 {len(existing) - len(records)} 个账号）。\n"
            f"  若确实要覆盖，请显式用 --overwrite。"
        )
    return fsutil.atomic_write_json(Path(path), records)


def save_snapshot(
    records: list[dict], batch_records: list[dict], *, existing: list | None = None
) -> tuple:
    """落一次盘：写全量快照 + 刷本批结果，返回 `(快照路径, 本批路径)`。

    🔴 **写顺序是「先快照、后刷本批」**。反过来的话，第二步失败就变成
      "读源已更新、快照没留下" —— 这次落盘没有回滚点。

    🔴 `records` 是**合并后的全量**（新读源），`batch_records` 是**本批原始**
      结果（含失败 / 跳过）。两者职责不同，别互换：
        * 互换 ⇒ 台账停止累积（读源只剩本批）；
        * 只写一个 ⇒ 要么丢历史，要么 `latest.json` 看不出这批跑了什么。
    """
    snap = snapshot_path()
    save(snap, records, existing=existing)
    fsutil.atomic_write_json(last_run_path(), batch_records)
    return snap, last_run_path()


def repo_rel(path) -> str:
    """路径归一化成**相对仓库根**的 posix 串；不在仓库内则原样返回。

    🔴 输出里不能带盘符 + 用户名：那种串一旦被贴进 issue / 提交就会撞泄漏闸门。
      artifact 本身是 gitignored 的，但"它不会被提交"不是一条靠人守得住的规则。
    """
    if not path:
        return ""
    try:
        return Path(path).resolve().relative_to(ROOT).as_posix()
    except (ValueError, OSError):
        return str(path)
