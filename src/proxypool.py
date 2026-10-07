"""槽位代理池：把"一个出口"变成"N 个出口"。

为什么需要它（**理由是实测数字，不是"多出口更安全"**）
--------------------------------------------------
`sign-in/email` 的限流实测是 **3 次 / 10s**（见 `common/config.LOGIN_MAX_PER_WINDOW`），
而 better-auth 的限流是**按客户端 IP** 计的。所以：

    --workers 4             → 4 个账号在 ~2s 内各登录一次 ⇒ 第 4 个必然 429
    --workers 4 + 5 个出口  → 每个出口各有一张 3/10s 的窗口 ⇒ 15 次/10s

只调并发是没用的 —— 那是同一张窗口里的三个名额。**出口数量才是那个乘数**，
这一点与 Intern-Register-Tool 的结论同形（那边也写着"真正的约束是有多少个
不同出口 IP"），但**理由不同**：那边是 IP 维度的**注册封禁**，hark 侧至今
**没有任何封禁证据**（见 docs/protocol.md）。

🔴 刻意**不做**冷却 / 指数退避（对比 Intern 的 `IR_PROXY_COOLDOWN`）
-----------------------------------------------------------------
那边的退避是为"出口已被封"设计的：被封的出口再打只是加深封禁，所以要晾着。
本项目没有观测到任何出口被封，**在没有证据时加冷却就是猜**，而且它会直接
降低吞吐（把可用出口晾着）。所以这里只**记账**（`ok` / `failed` / `last_error`），
让报告能回答"哪个出口失败多"；等真观测到封禁再谈退避。

⚠ 也不做"健康检查式"的预检：`acquire()` 拿到一个已经死掉的端口时，
失败会**记在那个账号的账上**（`error_kind=network`），报告里能看出来。
预检会多打一次请求，而且"TCP 能连"并不等于"代理可用"（Intern 实测：
本机 Clash TUN 下连一个不可达端口只要 0.02s —— 那是连到本地虚拟网卡）。
"""

import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass


class SlotPoolEmpty(RuntimeError):
    """等不到空闲槽位（超时）。调用方按失败记账，**不是**整批终止。"""


@dataclass
class SlotStats:
    url: str
    used: int = 0
    ok: int = 0
    failed: int = 0
    last_error: str = ""
    last_used: float = 0.0

    @property
    def label(self) -> str:
        """给报告用的短标签：`7901` 这类端口号，取不到就退回整串。"""
        raw = self.url.rsplit("/", 1)[-1]
        return raw.rsplit(":", 1)[-1] or self.url


class SlotPool:
    """线程安全的槽位池。空池 = 单代理行为（`acquire()` 返回 `None`）。

    🔴 空池**不报错**：没配 `HR_PROXY_SLOTS` 时整条链路照跑（走 `HR_PROXY`
      或直连）。让"没配池子"变成启动失败，会让这个可选功能变成必填。
    """

    def __init__(self, slots, *, timeout: float = 240.0):
        self._all = list(slots or [])
        self._free = list(self._all)
        self._stats = {u: SlotStats(url=u) for u in self._all}
        self._timeout = float(timeout)
        self._cv = threading.Condition()

    def __len__(self) -> int:
        return len(self._all)

    def __bool__(self) -> bool:
        return bool(self._all)

    # ── 借出 / 归还 ────────────────────────────────────────────
    def acquire(self, timeout: float | None = None):
        """借一个槽位（**最少使用优先**）。空池返回 `None`。

        🔴 最少使用优先，而不是"按顺序取第一个空闲的"：登录限流是**按出口**的，
          轮询取用会让出口 1 在别人都闲着的时候先把 3/10s 用满，
          总吞吐反而更低。按使用次数排序才能把 N 张窗口都用上。

        ⚠ 排序键里带 `last_used`：`used` 相同时让"最久没用过的"先上，
          这样新加的槽位不会一直排在队尾。
        """
        if not self._all:
            return None
        limit = self._timeout if timeout is None else float(timeout)
        deadline = time.time() + limit
        with self._cv:
            while not self._free:
                remain = deadline - time.time()
                if remain <= 0:
                    raise SlotPoolEmpty(
                        f"等 {limit:.0f}s 没等到空闲槽位（{len(self._all)} 个都在用）"
                    )
                self._cv.wait(remain)
            self._free.sort(key=lambda u: (self._stats[u].used, self._stats[u].last_used))
            url = self._free.pop(0)
            self._stats[url].used += 1
            self._stats[url].last_used = time.time()
            return url

    def release(self, url, *, ok: bool | None = None, error: str = "") -> None:
        """归还槽位。`ok=None` 表示"没跑完"（不计成功也不计失败）。"""
        if not url:
            return
        with self._cv:
            st = self._stats.get(url)
            if st is not None:
                if ok is None:
                    pass  # 没跑完（被本地闸门拦下 / 中途抛异常）—— 不计成败
                elif ok:
                    st.ok += 1
                else:
                    st.failed += 1
                    st.last_error = str(error)[:200]
            if url not in self._free:
                self._free.append(url)
            self._cv.notify()

    @contextmanager
    def slot(self, timeout: float | None = None):
        """`with pool.slot() as url:` —— 出错也会归还（否则一个异常就把出口漏掉）。"""
        url = self.acquire(timeout)
        try:
            yield url
        finally:
            self.release(url)

    # ── 报告 ──────────────────────────────────────────────────
    def stats(self) -> list:
        return [self._stats[u] for u in self._all]

    def describe(self) -> str:
        if not self._all:
            return "未配置（单出口）"
        parts = [
            f"{s.label}:{s.used}" + (f"(✗{s.failed})" if s.failed else "") for s in self.stats()
        ]
        return f"{len(self._all)} 个出口 · " + " ".join(parts)
