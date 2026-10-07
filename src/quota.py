"""本地注册配额保护（保守闸门，**不是**服务端权威计量）。

它解决什么问题
--------------
Intern-Register-Tool 那边实测到的失败形态是：**服务端有一层"累计配额"**，
它不在速率上体现（单账号也会被拒），而且恢复窗口很长（> 68h）。
那种墙一旦撞上，本地并发参数、重试、换邮箱域**全都没用** —— 只能停下等。

所以这里有一道**本地**闸门：跑之前先看"窗口内已经成功注册了几个"，
超了就**一个请求都不发**地退出（退出码 2）。

🔴 三条必须写清楚的限制
----------------------
1. **hark 侧还没有实测到这个墙**。默认上限 20 是"没有证据时的保守猜测"，
   不是测出来的触顶点（对比 Intern：那边的 40 是撞墙撞出来的）。
   所以它的作用目前只是"防手滑跑 500 个"，不声称等于服务端上限。
2. **它是本地的**：换机器 / 删 `state/` 即重置。所以它拦得住"我自己跑太猛"，
   拦不住"别人也在跑"。
3. **它只在成功时计数**（`used` = 窗口内 status=success 的条数）。
   失败尝试另计（`attempts`），供复盘看"打了几枪"，但不进闸门 ——
   否则一次服务端抖动就能把额度白白烧掉。

窗口取 24h 的理由
-----------------
Intern 那边实测恢复窗口 **> 68.2h**（跨 3 个自然日仍被封），
所以 24h 对它是**偏短**的；但对 hark，既然上限本身就是猜的，
窗口取 24h 至少保证"一天内不会无限打"。等真观测到 hark 的墙，再按实测改
（改的地方只有 `REG_QUOTA_WINDOW_H` 的默认值 + docs/protocol.md 的记录）。
"""

import json
import math
import time
from dataclasses import dataclass
from pathlib import Path

from common import config

from . import fsutil


class QuotaExceeded(RuntimeError):
    """本地闸门拦下了这一批。**一个请求都没发**。"""


def state_path() -> Path:
    """计数文件。`HR_QUOTA_STATE` 可覆盖（自检脚本靠它做隔离）。

    ⚠ 在**调用时**读环境变量，不受导入顺序影响 —— 与 `config` 的常量不同，
      这样测试可以在 import 之后改环境变量而不必重载模块。
    """
    override = (__import__("os").getenv("HR_QUOTA_STATE") or "").strip()
    if override:
        return Path(override).expanduser()
    return Path(__file__).resolve().parents[1] / "state" / "register_quota.jsonl"


@dataclass
class QuotaStatus:
    used: int
    attempts: int
    limit: int
    window_h: float
    oldest_ts: float

    @property
    def exhausted(self) -> bool:
        return self.used >= self.limit

    @property
    def left(self) -> int:
        return max(0, self.limit - self.used)

    def minutes_until_free(self, now: float | None = None) -> int:
        """还要等几分钟才能空出一个名额（`oldest_ts` 滑出窗口那一刻）。

        窗口内一条记录都没有时返回 0。取整**向上**（`ceil`）—— 报"还差 0 分钟"
        但闸门仍然拦着，是最让人困惑的一种输出。

        ⚠ 不能用 `int(remain // 60) + 1`：`remain` 恰好是 60 的整数倍时会多报
          一分钟（实测：`oldest_ts == now`、窗口 1h 时报 61 而不是 60）。
        """
        if not self.oldest_ts:
            return 0
        now = time.time() if now is None else now
        remain = (self.oldest_ts + self.window_h * 3600) - now
        if remain <= 0:
            return 0
        return int(math.ceil(remain / 60.0))

    def describe(self) -> str:
        return (
            f"{self.used}/{self.limit} 个（窗口 {self.window_h:g}h，"
            f"失败尝试 {self.attempts - self.used} 次）"
        )


def _as_ts(raw) -> float:
    """宽容地取时间戳。**坏值退回 0**，不抛。

    ⚠ 这个文件是**本地**的，可能被人手改过（比如手工塞一行进去看闸门行为）。
      一个 `"ts": "yesterday"` 不该让整批任务炸在 `float()` 上 ——
      退回 0 的后果只是这条记录被当成窗口外，是安全方向的降级。
    """
    try:
        return float(raw)
    except (TypeError, ValueError):
        return 0.0


def _read_entries() -> list[dict]:
    """读计数文件 → 条目列表。**读不到就返回空**，不抛。

    🔴 静默降级是有意的，但代价必须说清楚：文件读不出来 ⇒ `used=0` ⇒
      闸门放行。这就是"状态文件是加速器不是数据"的那条约定（见 fsutil）。
      真正的高危失败在**台账**那一侧，那边是让它抛的。
    """
    p = state_path()
    if not p.is_file():
        return []
    out: list[dict] = []
    try:
        text = p.read_text(encoding="utf-8")
    except OSError:
        return []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except ValueError:
            # 半截行（写盘时被杀）只丢这一行，不丢整个文件。
            continue
        if isinstance(d, dict):
            out.append(d)
    return out


def status(now: float | None = None) -> QuotaStatus:
    """窗口内的计数。`now` 只为测试可注入，生产不该传。"""
    now = time.time() if now is None else now
    cutoff = now - config.REG_QUOTA_WINDOW_H * 3600
    entries = [e for e in _read_entries() if _as_ts(e.get("ts")) >= cutoff]
    used = [e for e in entries if e.get("ok")]
    oldest = min((_as_ts(e.get("ts")) for e in used), default=0.0)
    return QuotaStatus(
        used=len(used),
        attempts=len(entries),
        limit=config.REG_QUOTA_MAX,
        window_h=config.REG_QUOTA_WINDOW_H,
        oldest_ts=oldest,
    )


def check(*, ignore: bool = False, now: float | None = None) -> QuotaStatus:
    """闸门。超了抛 `QuotaExceeded`（除非 `ignore`）。

    🔴 `ignore` 是**显式**的：默认拦。理由同 Intern —— "以为服务端恢复了"
      而实际没恢复时，放行会白烧一整批（而且每次都加深封禁）。
    """
    st = status(now=now)
    if st.exhausted and not ignore:
        raise QuotaExceeded(
            f"本地配额已达上限：{st.describe()}。"
            f"还要等约 {st.minutes_until_free(now)} 分钟才有名额。\n"
            f"  这是**本地保护**（src/quota.py），不是服务端拒绝 —— 未发出任何请求。\n"
            f"  选项：① 等窗口滑出；② 调大 HR_REG_QUOTA_MAX；\n"
            f"        ③ 确信服务端没在拦，再加 --ignore-quota。"
        )
    return st


def record(email: str, *, ok: bool, ts: float | None = None, note: str = "") -> None:
    """追加一条记录。**写失败只告警，不抛**。

    ⚠ 与台账相反：计数文件丢了只影响下一轮的保守程度，不该让整批任务失败
      （Intern 的 `proxypool` / `quota` 是同一策略，`ledger` 是另一策略）。
    """
    p = state_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(
            {
                "ts": round(time.time() if ts is None else ts, 3),
                "email": email,
                "ok": bool(ok),
                "note": note[:120],
            },
            ensure_ascii=False,
        )
        with p.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError as ex:
        print(f"  ⚠ 配额计数写不进去（{type(ex).__name__}: {ex}）—— 本次不记账，闸门会偏松")
        return
    _maybe_compact()


def _maybe_compact() -> None:
    """行数远多于窗口所需时，重写文件只留窗口内的条目。

    🔴 只在**行数 > 4×上限**时才压缩：每次成功都重写一遍文件会让
      "写"变成 O(n) 并且放大"写到一半被杀"的窗口。
    """
    entries = _read_entries()
    if len(entries) <= max(200, config.REG_QUOTA_MAX * 4):
        return
    cutoff = time.time() - config.REG_QUOTA_WINDOW_H * 3600
    keep = [e for e in entries if _as_ts(e.get("ts")) >= cutoff]
    text = "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in keep)
    try:
        fsutil.atomic_write_text(state_path(), text)
    except OSError as ex:
        print(f"  ⚠ 配额计数压缩失败（{type(ex).__name__}: {ex}）—— 保持原文件")


def shortfall_hint(left: int, count: int, ignore: bool) -> str:
    """这一批跑不满时给一句**真话**。

    🔴 判据必须读 `ignore`：Intern 踩过 —— `--ignore-quota` 打开时提示仍然
      打"这一批会全部被跳过"，而实测 47/50 成功。**提示说假话**比没有提示更坏。
    """
    if count <= left or ignore:
        return ""
    return (
        f"  ⚠ 本批要 {count} 个，本地窗口内只剩 {left} 个名额 —— "
        f"多出来的会以 `quota_guard` 跳过（**不发请求**）。"
    )
