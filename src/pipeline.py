"""两段式流水线：注册（生产者） ‖ 收尾（消费者）。

为什么分两段
------------
三个阶段的时间形状完全不同：

| 阶段 | 动作 | 典型耗时 | 瓶颈在哪 |
|------|------|----------|----------|
| 生产者 | 建邮箱 → discovery → age → magic-link → **等信** → 校验链接 | 3~8s | **SMTP 投递 + 收信轮询** |
| 消费者 | set-password → 密码登录 → 会话/资料/额度校验 | 1~2s | 纯 HTTP，很快 |

把"等信"放在生产者池里并发跑，消费者池只做快活 —— 这样**邮件的等待时间被
并行化**，而不是被串行地加在关键路径上。

⚠ 与 Intern-Register-Tool 的**关键差异**：那边分两段的根本理由是"浏览器很贵，
不能让注册并发度牵着浏览器并发度走"。**本项目零浏览器**（整条链路纯 HTTP，
实测不需要验证码 / WAF 解盾），所以这里的拆分理由是 **I/O 等待的形状不同**
（等外部 SMTP vs. 打站点 API），不是资源稀缺。
别把那边"浏览器并发 6 就够"的经验搬过来 —— 这里没有浏览器这个约束。

为什么"注册申请"要限速
----------------------
`discovery` / `signup-age` / `magic-link` 是三个**写**请求，而写请求是限流
最容易挂的地方（Intern 的 429 就挂在"写操作 + 突发"上）。
本项目实测 hark 对 magic-link **没有**突发限流（5 连发 / 2.5s 全 200），
所以 `HR_MIN_INTERVAL=0.5` 是**保守**值、不是实测结论 —— 详见 docs/protocol.md。
限速只卡**起点**（不持锁跨三个请求），否则生产者池就退化成串行了。

错误分类（`error_kind`）
-----------------------
| 值 | 含义 | 判据来源 |
|---|---|---|
| `""` | 没有错误 | — |
| `"quota"` | 服务端明确限流 / 配额（HTTP 429 或配额类业务码） | 服务端响应 |
| `"quota_guard"` | 本地守卫主动中止，**一个请求都没发** | 本地 |
| `"rejected"` | 服务端明确拒绝（非配额）：年龄门 / 密码策略 / 凭据错 / 链接被拒 | 服务端响应 |
| `"network"` | 网络 / 超时 / 5xx | 异常 |
| `"mail"` | 邮箱侧（建地址失败 / 读信读不出来） | 异常 |

🔴 **读点只认字段**（`error_kind_of`）：字段非空即权威，只有字段为空时才退化成
文本匹配（给手工构造的老记录兜底）。Intern-Register-Tool 的教训是：靠自己拼的
错误文案里也可能出现服务端的词（我们就把响应体原文附进了 `error`），
文本匹配分不清"服务端说的"和"我们引用的"。
"""

import json
import queue
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime

from common import config

from . import quota as _quota
from .hark import (
    CODE_INVALID_EMAIL_OR_PASSWORD,
    CODE_PASSWORD_ALREADY_SET,
    ERR_MAIL,
    ERR_NETWORK,
    ERR_QUOTA,
    ERR_QUOTA_GUARD,
    ERR_REJECTED,
    HarkClient,
    HarkError,
    HarkQuota,
    HarkRejected,
)
from .proxypool import SlotPool, SlotPoolEmpty
from .tempmail import TempMailClient, now_ms

__all__ = [
    "STAGES",
    "ERR_QUOTA",
    "ERR_QUOTA_GUARD",
    "ERR_REJECTED",
    "ERR_NETWORK",
    "ERR_MAIL",
    "AccountRecord",
    "error_kind_of",
    "run_batch",
]

# 阶段名与顺序。**单一真源**：`AccountRecord.stages` 的键、报告的列、
# 测试的断言都从这里取 —— 各写一份必然漂移（漂移的表现是"报告里少一列"，
# 而那不会报错）。
STAGES = ("mailbox", "register", "activate", "password", "login", "app")

# 连续多少次"服务端配额拒绝"就停整批。
# 🔴 取 2 而不是 1：单次 429 完全可能是抖动（Intern 的 `QUOTA_STREAK_STOP`
#    也是同一个理由）。但连续两次就值得停 —— 撞上配额墙时继续打只是白烧请求。
QUOTA_STREAK_STOP = 2

# 密码字符集。**刻意不用 `string.punctuation`**：里面含 `"` / `\` / `'` / `` ` ``，
# 而密码会被写进 JSON 台账、有时还要手打进浏览器 —— 那些字符在每一条路径上
# 都要被转义一次，是纯粹的麻烦来源（Intern 那边踩过手抄密码时看错字符）。
_PW_LOWER = "abcdefghijkmnopqrstuvwxyz"  # 去掉 l
_PW_UPPER = "ABCDEFGHJKLMNPQRSTUVWXYZ"  # 去掉 I O
_PW_DIGIT = "23456789"  # 去掉 0 1
_PW_SYMBOL = "!#$%&*+-=?@^_"


def make_password(length: int | None = None) -> str:
    """生成一个必然满足服务端策略的密码。

    🔴 服务端实测只卡**长度**（`PASSWORD_TOO_SHORT`，阈值 8）—— 没观察到
      大小写/数字/符号的强制要求。但**本地仍按四类字符各取一个**：
      平台随时可能加策略，而"密码策略变更导致整批注册失败"是最冤的一种失败。
      代价为零，所以不省。
    """
    import secrets

    n = max(8, int(length or config.PASSWORD_LEN))
    pool = _PW_LOWER + _PW_UPPER + _PW_DIGIT + _PW_SYMBOL
    chars = [
        secrets.choice(_PW_LOWER),
        secrets.choice(_PW_UPPER),
        secrets.choice(_PW_DIGIT),
        secrets.choice(_PW_SYMBOL),
    ]
    chars += [secrets.choice(pool) for _ in range(n - len(chars))]
    # Fisher–Yates 洗牌。**必须洗**：不洗的话前四位永远是"小写+大写+数字+符号"，
    # 那是一个可被规则识别的形状。
    for i in range(len(chars) - 1, 0, -1):
        j = secrets.randbelow(i + 1)
        chars[i], chars[j] = chars[j], chars[i]
    return "".join(chars)


def _as_int(raw, default: int = 0) -> int:
    """宽容地取整数 —— 用于**服务端给的**字段（额度上限等）。

    ⚠ 这些值来自 hark 的 JSON，而我们不拥有那个 API：某天它把
      `dailyLimit` 从数字改成 `"8000000"` 或 `null`，`int()` 会抛
      `ValueError`，而那个异常会被 `classify()` 归到 `network`（可重试）——
      于是一次**形态变更**被伪装成网络抖动，重试多少次都一样。
      退回 0 的后果只是台账里那一列不好看，是安全方向的降级。
    """
    if isinstance(raw, bool):
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


@dataclass
class AccountRecord:
    """一个账号的台账记录。`to_json()` 的字段顺序就是这里写的顺序。"""

    email: str = ""
    password: str = ""
    user_id: str = ""
    # better-auth 的 session token（`<token>.<hmac>` 里的前段，与 get-session
    # 返回的 `session.token` 实测相等）。**取自密码登录那一步**，不是 magic link
    # 那条 —— 后者是注册过程的中间态。
    session_token: str = ""
    session_expires_at: str = ""
    region: str = ""
    has_app_access: bool | None = None
    plan: str = ""
    daily_limit: int = 0
    pool_limit: int = 0
    status: str = "failed"
    error: str = ""
    error_kind: str = ""
    stages: dict = field(default_factory=dict)
    timings: dict = field(default_factory=dict)
    birthday: str = ""
    created_at: str = ""
    proxy: str = ""
    # 撞 429 换过出口时的**轨迹**（`a → b`）。平时是空串。
    #
    # 🔴 为什么要单独一个字段：`proxy` 记的是**最终**出口（会话是在那里建的），
    #    而“曾经在 a 上被限流”是有价值的排查信息 —— Intern-Register-Tool 记
    #    `proxy_slot` 的初衷就是“事后能回答被封的到底是哪个出口”。
    #    把它写进 `error` 是错的（那是给失败用的，而成功记录里它是空的）。
    proxy_rotated: str = ""
    # **当前正在跑的阶段**。不是台账字段（`as_dict` 里没有它）。
    #
    # 🔴 为什么需要它：`stages` 里的值会被**提前**写进去（`register` 在
    #    discovery 之后就要记 "exists=…"），所以"第一个还是空的阶段"
    #    **不能**代表失败发生在哪一步（Intern 那边靠猜位置的写法会指错）。
    #    显式记一个游标，`_failed_stage()` 就不再是启发式。
    stage_now: str = ""

    def __post_init__(self):
        # 所有阶段先落成 ""（= 没跑到）。**不能**只在跑过时写键：
        # 报告要按 `STAGES` 逐列渲染，缺键会让"没跑到"和"不存在这个阶段"
        # 变成两件事，而它们对读者是同一件事。
        for name in STAGES:
            self.stages.setdefault(name, "")

    # ── 打标 ──────────────────────────────────────────────────
    def enter(self, name: str) -> None:
        """进入一个阶段。**每个阶段开始时调一次** —— 失败时才知道自己死在哪。"""
        self.stage_now = name

    def stage(self, name: str, value: str) -> None:
        """记一个阶段的结果。`value` 形如 `ok` / `ok(8M tokens/day)` / `failed: …`。"""
        self.stages[name] = value

    def mark_ok(self, name: str, note: str = "") -> None:
        self.stages[name] = f"ok({note})" if note else "ok"

    def fail(self, kind: str, message: str, stage: str | None = None) -> None:
        """打上失败标记。**所有失败路径都必须走这里**（否则读点会静默退化）。

        🔴 `stage` 给定时，只把**那个**阶段标成 failed —— 后面的阶段保持 ""
          （= 没跑到）。把整条链都标成 failed 会让人以为"后面几步也试过了"。
        """
        self.status = "failed"
        self.error_kind = kind
        self.error = message[:500]
        if stage:
            self.stages[stage] = "failed"

    def skip(self, kind: str, message: str) -> None:
        """没跑（本地守卫拦下 / 本批已停）。与 `failed` 的区别是**没发请求**。"""
        self.status = "skipped"
        self.error_kind = kind
        self.error = message[:500]
        for name in STAGES:
            if not self.stages.get(name):
                self.stages[name] = "skipped"

    def done(self) -> None:
        self.status = "success"
        self.error = ""
        self.error_kind = ""

    # ── 序列化 ────────────────────────────────────────────────
    def as_dict(self) -> dict:
        return {
            "email": self.email,
            "password": self.password,
            "user_id": self.user_id,
            "session_token": self.session_token,
            "session_expires_at": self.session_expires_at,
            "region": self.region,
            "has_app_access": self.has_app_access,
            "plan": self.plan,
            "daily_limit": self.daily_limit,
            "pool_limit": self.pool_limit,
            "status": self.status,
            "error": self.error,
            "error_kind": self.error_kind,
            "stages": dict(self.stages),
            "timings": dict(self.timings),
            "birthday": self.birthday,
            "created_at": self.created_at,
            "proxy": self.proxy,
            "proxy_rotated": self.proxy_rotated,
        }

    def to_json(self) -> str:
        # `ensure_ascii=False`：台账是给人看的，`\u4e2d` 那种形态没法读。
        return json.dumps(self.as_dict(), ensure_ascii=False)


def error_kind_of(rec) -> str:
    """读点的**唯一**实现：字段非空即权威，只有字段为空时才退化成文本匹配。

    ⚠ 文本兜底只服务"手工构造的老记录"。新增失败路径时**必须在抛出点打标**，
      否则读点会静默退化成文本匹配 —— 而那种退化不报错，只是让分类悄悄变糊。
    """
    if isinstance(rec, dict):
        kind, err = rec.get("error_kind"), rec.get("error") or ""
    else:
        kind, err = rec.error_kind, rec.error or ""
    kind = (kind or "").strip()
    if kind:
        return kind
    text = str(err).lower()
    if "quota_guard" in text or "本地配额" in text:
        return ERR_QUOTA_GUARD
    if "429" in text or "quota" in text:
        return ERR_QUOTA
    return ""


def classify(ex: BaseException) -> str:
    """异常 → `error_kind`。**唯一映射点**（别在调用处各写一份 if/elif）。"""
    if isinstance(ex, HarkError):
        return ex.kind
    if isinstance(ex, _quota.QuotaExceeded):
        return ERR_QUOTA_GUARD
    return ERR_NETWORK


@dataclass
class Ticket:
    """生产者交给消费者的东西。

    🔴 **`client` 是"顺序交接"的**：生产者线程建它、用它校验 magic link，
      然后通过队列交给消费者线程，之后只有消费者碰它。
      `requests.Session` 不是线程安全的 —— 但这里不存在**并发**访问，
      只有顺序访问，所以安全。要改成共享就得上每线程一个实例。

    🔴 **`proxy` 跟着账号走完两段**：登录限流是按出口的，所以消费者必须知道
      这个账号在哪个出口上 —— 否则它会在出口 A 的窗口里给出口 B 的账号排队。
      一个账号一条链一个出口，这也让台账里的 `proxy` 字段真的能回答
      "这个账号当时走的哪个出口"。
    """

    rec: AccountRecord
    client: HarkClient
    mail_polls: int = 0
    proxy: str | None = None


# ── 生产者：建邮箱 → 注册申请 → 等信 → 校验 ───────────────────────
def provision(
    rec: AccountRecord,
    *,
    mail,
    client: HarkClient,
    mail_domain: str | None = None,
    slot: str | None = None,
) -> Ticket:
    """跑完生产者那一段。成功返回 `Ticket`；失败**抛异常**（由调用方打标）。

    ⚠ 这里**不捕获异常**，是有意的：异常类型带着 `error_kind` 的分类信息，
      在这一层吞掉就得在这一层重新分类（= 第二份分类实现）。让调用方统一
      用 `classify()` 处理。
    """
    t0 = time.time()
    rec.enter("mailbox")
    # 密码在**生产者**这一侧就生成好，而不是等消费者那一步再生成 ——
    # `rec.password` 是台账里的一等字段，而“邮箱建出来了但密码是空的”
    # 是最坏的一种中间态（账号已存在、却无法用密码登录）。
    rec.password = make_password()
    emails = mail.create_mailbox(mail_domain, 1)
    if not emails:
        # Worker 少给地址时必须**立刻**失败：不检查的话下面会拿空串去注册，
        # 报错点离原因很远（服务端会说"邮箱格式不对"）。
        raise HarkRejected("邮箱 Worker 没返回地址（create_mailbox 返回空）", code="NO_MAILBOX")
    rec.email = emails[0]
    rec.timings["mailbox_ms"] = int((time.time() - t0) * 1000)
    rec.mark_ok("mailbox")

    # 注册申请：三个写请求。
    #
    # 🔴 闸门只卡 **`magic-link` 这一个请求**，不是整个阶段：实测只有它受
    #    per-IP 限流（`discovery` / `signup-age` 一直 200）。卡整个阶段会让
    #    两个**不受限**的请求也排队 —— 那是白等。
    t1 = time.time()
    rec.enter("register")
    disc = client.discover(rec.email)
    client.signup_age(rec.email, rec.birthday)
    waited = register_limiter(slot).acquire()
    sent_at = now_ms()  # 🔴 毫秒：与 Worker 的 received_at 同单位（见 tempmail.now_ms）
    client.request_magic_link(rec.email)
    rec.timings["register_ms"] = int((time.time() - t1) * 1000)
    if waited:
        rec.timings["register_wait_ms"] = int(waited * 1000)
    # `exists` 作为**成功注记**写在阶段值里（`ok(exists=False)`），而不是提前把
    # `stages["register"]` 写成 `exists=False` —— 后者会让“这个阶段是什么结果”
    # 变成一个需要解释的字符串，而阶段值只该回答“成没成”。
    rec.mark_ok("register", f"exists={bool(disc.get('exists'))}")

    # 等信 + 校验链接。**等信是整链最大的单项耗时**（SMTP + 轮询）。
    t2 = time.time()
    rec.enter("activate")
    link = mail.wait_for_link(rec.email, "magic-link/verify", since_ts=sent_at)
    rec.timings["mail_wait_ms"] = int((time.time() - t2) * 1000)
    if not link:
        # 🔴 两种失败必须分开报：`last_error` 非空 = **读不出来**（修法完全不同）。
        if getattr(mail, "last_error", ""):
            raise HarkRejected(f"邮箱读不出来：{mail.last_error}", code="MAIL_READ")
        raise HarkRejected(
            f"等 {config.MAIL_POLL_TIMEOUT}s 没等到 magic link 邮件"
            f"（打了 {getattr(mail, 'last_polls', 0)} 次收信接口）",
            code="MAIL_TIMEOUT",
        )
    # ⚠ `activate` 只在**校验成功之后**才标 ok。
    t3 = time.time()
    client.verify_magic_link(link)
    rec.timings["verify_ms"] = int((time.time() - t3) * 1000)
    # `activate_ms` 是**整个 activate 阶段**（等信 + 校验），而 `mail_wait_ms` /
    # `verify_ms` 是它的两个子项。两个层级都要：报告按 STAGES 逐列渲染需要前者，
    # 而“等信到底花了多少”只有后者能回答（那是整链最该被盯住的一项）。
    rec.timings["activate_ms"] = int((time.time() - t2) * 1000)
    rec.mark_ok("activate", "mail+verify")
    return Ticket(rec=rec, client=client, mail_polls=int(getattr(mail, "last_polls", 0)))


# ── 消费者：设密码 → 密码登录 → 校验能用 ──────────────────────────
class SlidingWindow:
    """滑动窗口限速器（**线程安全**）。

    为什么需要它（实测依据，不是防御性编程）
    ------------------------------------
    `sign-in/email` 实测限流是 **3 次 / 10s**（第 4 次开始 `429 RATE_LIMITED`，
    见 `common/config.LOGIN_MAX_PER_WINDOW` 的注释）。而 `--workers 4` 会让
    4 个账号在 ~2s 内各登录一次 ⇒ **第 4 个必然 429**，而 429 会被下游读成
    "账号坏了" —— 一个**假阴性**，比报错更坏。

    🔴 为什么用**滑动窗口**而不是"最小间隔"：平台限的是"10 秒内最多 3 次"，
      不是"每 3.33 秒一次"。用固定间隔（如 3.4s）时，只要有一次请求比预期
      快（缓存/重连），就可能把第 4 次挤进窗口而撞 429。滑动窗口与平台的
      判据同构，不会出现这种偏差。
    """

    def __init__(self, max_events: int, window_s: float):
        self.max_events = max(1, int(max_events))
        self.window_s = float(window_s)
        self._times: list[float] = []
        self._lock = threading.Lock()

    def acquire(self) -> float:
        """等到可以发一个请求为止，返回**实际等了多久**（秒）。

        ⚠ 持锁期间 `sleep`：这是有意的 —— 放锁会让两个线程同时看到
          "还有名额"然后一起发出去，限速就失效了。代价是并发度被这个
          上限压成 3/10s，而那正是平台的真实上限。
        """
        with self._lock:
            now = time.time()
            self._times = [t for t in self._times if now - t < self.window_s]
            wait = 0.0
            if len(self._times) >= self.max_events:
                wait = (self._times[0] + self.window_s) - now
                if wait > 0:
                    time.sleep(wait)
                now = time.time()
                self._times = [t for t in self._times if now - t < self.window_s]
            self._times.append(now)
            return max(0.0, wait)


# 限速器。**按出口分桶**。
#
# 🔴 实测的 3 次/10s 是**按客户端 IP** 的，所以每个出口各自有一张窗口：
#    5 个出口 = 15 次/10s。这是代理池在登录侧的**全部价值**，也是为什么
#    限速器必须按 slot 分桶 —— 共用一个全局窗口的话，加出口一点用都没有。
#    没配池子时 key 是 `""`，行为与加池子之前**完全一致**。
_LOGIN_LIMITERS: dict = {}
_REGISTER_LIMITERS: dict = {}
_LIMITER_LOCK = threading.Lock()


def login_limiter(slot: str | None = None) -> SlidingWindow:
    """取**某个出口**的登录限速器（懒建 —— 测试改了 config 也能生效）。"""
    return _limiter(_LOGIN_LIMITERS, slot, config.LOGIN_MAX_PER_WINDOW, config.LOGIN_WINDOW_S)


def register_limiter(slot: str | None = None) -> SlidingWindow:
    """取**某个出口**的注册申请（`magic-link`）限速器。

    🔴 与登录侧同构，理由是**实测**（见 `common/config.REGISTER_MAX_PER_WINDOW`）：
      注册侧的 429 也是**按 IP** 的，而且限的就是 `magic-link` 这一个端点
      （`discovery` / `signup-age` 不限）。

    ⚠ 它取代了旧的“每进程全局最小间隔”。旧的写法**两头都不对**：
      既不能阻止某个出口超限（5 个出口轮转时每个会跑到 12 次/分），
      又在小批量里白白把 5 个注册申请串行化成 4 秒。
    """
    return _limiter(
        _REGISTER_LIMITERS, slot, config.REGISTER_MAX_PER_WINDOW, config.REGISTER_WINDOW_S
    )


def _limiter(store: dict, slot: str | None, max_events: int, window_s: float) -> SlidingWindow:
    key = slot or ""
    with _LIMITER_LOCK:
        lim = store.get(key)
        if lim is None:
            lim = SlidingWindow(max_events, window_s)
            store[key] = lim
        return lim


def _rotate_login_slot(slot_pool, ticket, *, error: str) -> bool:
    """把这条链换到**另一个出口**。换成了返回 `True`。

    🔴 为什么是换出口，而不是原地重试（实测 2026-10-08）
    ------------------------------------------------
    登录侧的 429 有两种，其中一种是 **Cloudflare 边缘的限流**（响应体是 CF 的
    HTML 错误页，不是 better-auth 的 JSON）。实测：在**同一个出口**上退避重试
    4 次（跨 ~17s）**仍然 429**；而换一个出口立刻就能过。那 17s 是白等的。

    ⚠ 旧出口要**归还**（并按失败记账）—— 否则池子会逐渐少一个，而现象是
      “批量慢慢变慢/超时”，不是“池子漏了”。
    ⚠ `ticket.proxy` 必须一起改：消费者最后归还的是 `ticket.proxy` 指向的那个，
      不改就会**双重归还旧出口、而新出口永远漏在外面**。
    """
    if not slot_pool or len(slot_pool) < 2:
        return False
    old = ticket.proxy
    try:
        new = slot_pool.acquire(timeout=5)
    except SlotPoolEmpty:
        return False
    if not new or new == old:
        # 只借到同一个（或没借到）⇒ 换不动，让调用方退避重试。
        if new:
            slot_pool.release(new)
        return False
    slot_pool.release(old, ok=False, error=f"429 {error[:120]}")
    ticket.proxy = new
    return True


def finish(ticket: Ticket, *, client_factory=None, slot_pool=None) -> AccountRecord:
    """跑完消费者那一段。**就地改 `rec` 并返回它**（失败也返回，不抛）。"""
    rec, verified = ticket.rec, ticket.client

    # ① 设密码 —— 用刚校验过 magic link 的那条会话。
    t0 = time.time()
    rec.enter("password")
    try:
        verified.set_password(rec.password)
        rec.mark_ok("password")
    except HarkRejected as ex:
        if ex.code == CODE_PASSWORD_ALREADY_SET:
            # 🔴 **不能**判失败：这一步是一次性的，重试必然撞这个码，
            #    而"已经设过了"正是我们想要的终态。但也不能悄悄当成功 ——
            #    若密码不是**我们**设的，台账里那列就是错的，所以标注出来。
            rec.mark_ok("password", "已存在(未覆盖)")
        else:
            rec.fail(ERR_REJECTED, f"set-password 被拒：{ex}", stage="password")
            rec.timings["password_ms"] = int((time.time() - t0) * 1000)
            return rec
    except HarkError as ex:
        rec.fail(classify(ex), f"set-password 失败：{ex}", stage="password")
        rec.timings["password_ms"] = int((time.time() - t0) * 1000)
        return rec
    rec.timings["password_ms"] = int((time.time() - t0) * 1000)

    # ② 密码登录 —— **换一条全新会话**。这一步才是"账号真能用"的证明：
    #    magic link 那条会话是注册过程的中间态，它成功不代表密码能用。
    t1 = time.time()
    rec.enter("login")
    login = (
        HarkClient(proxy=ticket.proxy) if client_factory is None else client_factory(ticket.proxy)
    )
    body = None
    waited = 0.0
    rotated = 0
    rotated_from: list[str] = []
    for attempt in range(config.LOGIN_RETRIES + 1):
        # 先过限速闸门，再发请求 —— 这是为了**不撞** 429，而不是撞了再救。
        # ⚠ 闸门是**按出口**的（见 login_limiter 的说明）。
        waited += login_limiter(ticket.proxy).acquire()
        try:
            body = login.sign_in(rec.email, rec.password)
            break
        except HarkQuota as ex:
            # 🔴 登录的 429 与"注册配额"**不是一回事**：前者是**自愈**的
            #    （窗口滑出就好，或者干脆是 CF 的边缘限流），所以这里重试，
            #    而不是像生产者那样把整批停掉。
            #
            # 🔴 重试的**第一步是换出口**，不是原地等：实测在同一个出口上
            #    重试 4 次（跨 ~17s）仍然 429，而换个出口立刻过。
            #    换不动（池子只有 1 个出口 / 别的都占着）才退避。
            if attempt >= config.LOGIN_RETRIES:
                rec.fail(
                    ERR_QUOTA,
                    f"密码登录持续被限流（已试 {attempt + 1} 次，换出口 {rotated} 次）：{ex}",
                    stage="login",
                )
                rec.timings["login_ms"] = int((time.time() - t1) * 1000)
                rec.timings["login_wait_ms"] = int(waited * 1000)
                return rec
            if _rotate_login_slot(slot_pool, ticket, error=str(ex)):
                rotated += 1
                rotated_from.append(rec.proxy or "(直连)")
                login = (
                    HarkClient(proxy=ticket.proxy)
                    if client_factory is None
                    else client_factory(ticket.proxy)
                )
                continue
            time.sleep(config.LOGIN_WINDOW_S / config.LOGIN_MAX_PER_WINDOW + 1.0)
        except HarkRejected as ex:
            if ex.code == CODE_INVALID_EMAIL_OR_PASSWORD:
                rec.fail(
                    ERR_REJECTED,
                    f"密码登录被拒（密码没生效？）：{ex}",
                    stage="login",
                )
            else:
                rec.fail(ERR_REJECTED, f"密码登录被拒：{ex}", stage="login")
            rec.timings["login_ms"] = int((time.time() - t1) * 1000)
            return rec
        except HarkError as ex:
            rec.fail(classify(ex), f"密码登录失败：{ex}", stage="login")
            rec.timings["login_ms"] = int((time.time() - t1) * 1000)
            return rec
    rec.timings["login_ms"] = int((time.time() - t1) * 1000)
    rec.timings["login_wait_ms"] = int(waited * 1000)
    if rotated_from:
        # 换过出口 ⇒ 台账里两个字段都要对得上：`proxy` 是**最终**那个，
        # `proxy_rotated` 把路径记下来。
        rec.proxy = ticket.proxy or rec.proxy
        rec.proxy_rotated = " → ".join([*rotated_from, ticket.proxy or "(直连)"])
    rec.session_token = login.session_token()
    rec.region = login.region()
    user = (body or {}).get("user") or {}
    rec.user_id = str(user.get("id") or "")
    rec.mark_ok("login")

    # ③ 只读校验：会话 / 资料 / 额度 / 会话列表。
    if not verify_app(rec, login):
        return rec

    rec.done()
    return rec


def verify_app(rec: AccountRecord, client) -> bool:
    """只读校验：会话 / 资料 / 额度 / 会话列表。返回 `True` = 全部通过。

    🔴 抽成独立函数是为了**只有一处实现**：`finish()`（注册链的收尾）与
      `tools/data/recover_accounts.py`（补做“已激活但链没走完”的账号）
      必须检查同样的东西 —— 否则“补完了”与“注册成功了”会变成两个不同的标准，
      而台账里那两行看不出区别。

    ⚠ **不调用 `rec.done()`**：那一步是调用方的（它还要决定写不写台账）。
      失败时已把 `rec` 打上标（`error_kind` + `stages.app = failed`）。
    """
    t0 = time.time()
    rec.enter("app")
    try:
        sess = client.get_session()
        if not sess.get("user"):
            rec.fail(ERR_REJECTED, "get-session 没有返回 user（会话没建立）", stage="app")
            return False
        rec.user_id = str((sess.get("user") or {}).get("id") or rec.user_id)
        rec.session_expires_at = str((sess.get("session") or {}).get("expiresAt") or "")
        rec.region = str((sess.get("user") or {}).get("region") or rec.region)
        # `admission.allowed` / `hasAppAccess` 是**准入**开关：实测新账号都是
        # true。万一某天平台开始放 waitlist，这里要能看出来 —— 否则会得到
        # 一堆"注册成功但用不了"的账号，而台账里看不出为什么。
        rec.has_app_access = bool((sess.get("user") or {}).get("hasAppAccess"))
        profile = client.profile()
        if not profile.get("email"):
            rec.fail(ERR_REJECTED, "profile 没有返回 email（资料接口不可用）", stage="app")
            return False
        bill = client.billing_summary()
        meters = (bill.get("meters") or {}).get("harkTokens") or {}
        rec.plan = str((bill.get("plan") or {}).get("id") or "")
        rec.daily_limit = _as_int(meters.get("dailyLimit"))
        rec.pool_limit = _as_int(meters.get("poolLimit"))
        client.conversations()
    except HarkError as ex:
        rec.fail(classify(ex), f"校验失败：{ex}", stage="app")
        return False
    finally:
        rec.timings["app_ms"] = int((time.time() - t0) * 1000)
    rec.mark_ok("app", f"{rec.daily_limit // 1000}K tokens/day" if rec.daily_limit else "")
    return True


# ── 编排 ──────────────────────────────────────────────────────────
def run_batch(
    *,
    count: int,
    workers: int = 4,
    producers: int = 4,
    mail_domain: str | None = None,
    birthday: str | None = None,
    verbose: bool = True,
    ignore_quota: bool = False,
    mail_factory=None,
    client_factory=None,
    slot_pool=None,
    log=print,
) -> list[AccountRecord]:
    """跑 `count` 个账号。返回**本批**记录（含失败 / 跳过，顺序=完成顺序）。

    并发结构：

        ┌─ 生产者池（producers 路）──────────────┐   ┌─ 消费者池（workers 路）──┐
        │ 建邮箱 → 注册申请 → 等信 → 校验链接     │──▶│ 设密码 → 密码登录 → 校验   │
        └────────────────────────────────────────┘ 队列 └─────────────────────────┘

    `mail_factory` / `client_factory` 是**注入点**：默认造真的客户端，
    测试传假的 —— 这样编排、打标、配额逻辑都能**离线**测（零网络请求）。
    """
    birthday = birthday or config.BIRTHDAY
    mail_factory = mail_factory or (lambda: TempMailClient())
    client_factory = client_factory or (lambda proxy=None: HarkClient(proxy=proxy))
    # 槽位池：**由调用方拥有**（`run.py` 建它、传进来、跑完读它的统计）。
    # 不在这里建的原因：返回值是 `list[AccountRecord]`，没法把统计挂在上面
    # （`list` 不能设属性），而用模块全局会在同进程跑多批时被后一批覆盖。
    # 没传就按配置建一个（测试 / 库调用方便）—— 空池 ⇒ `acquire()` 返回 None
    # ⇒ 退回 `HR_PROXY` / 直连，行为与加池子之前一致。
    if slot_pool is None:
        slot_pool = SlotPool(config.proxy_slots(), timeout=config.PROXY_SLOT_TIMEOUT)

    q: queue.Queue = queue.Queue(maxsize=max(2, producers))
    done: list[AccountRecord] = []
    lock = threading.Lock()
    stop = threading.Event()
    streak = {"n": 0}
    sentinel = object()

    def say(msg: str) -> None:
        if verbose:
            log(msg)

    def record_out(rec: AccountRecord) -> None:
        """一条记录完工 —— **所有出口都走这里**（成功 / 失败 / 跳过）。

        🔴 配额计数也在这里记，而不是在消费者里 —— 放在消费者里的话，
          **生产者阶段**就失败的账号（等不到邮件 / 服务端拒绝）根本不会落账，
          于是“打了几枪”这个数永远偏小。实测踩到：一个等信超时的账号
          在计数文件里找不到。

        🔴 `skipped` **不记账**：那是本地闸门拦下的（一个请求都没发），
          把它算进“失败尝试”会把分母弄脏。
        """
        with lock:
            done.append(rec)
        if rec.status != "skipped":
            _quota.record(rec.email, ok=(rec.status == "success"), note=rec.error_kind)

    def producer(idx: int) -> None:
        rec = AccountRecord(birthday=birthday)
        rec.created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        # ① 本地闸门。**在发任何请求之前** —— 这就是 `quota_guard` 的判据。
        #
        # 🔴 这里先把"该不该跑"判成一个字符串，**出了锁**再记账：
        #    `record_out()` 自己也要拿 `lock`，而 `threading.Lock` **不可重入**
        #    ⇒ 在 `with lock:` 里调它就是**自锁**（整个批次永久挂死，
        #    而且挂在"所有账号都已跑完"之后，看起来像卡死）。
        #    这个 bug 是实测撞出来的：本地闸门那条路 + 本批已停那条路
        #    各自在锁内调了 `record_out`，而正常路径没调，所以只有
        #    "连续配额拒绝之后"才会现形。
        reason = None
        with lock:
            if stop.is_set():
                reason = "本批已因连续配额拒绝而停止（未发请求）"
            else:
                try:
                    _quota.check(ignore=ignore_quota)
                except _quota.QuotaExceeded as ex:
                    reason = str(ex)
        if reason is not None:
            rec.skip(ERR_QUOTA_GUARD, reason)
            record_out(rec)
            return
        # ② 借一个出口。**在闸门之后、第一个请求之前** —— 被本地闸门拦下的
        #    任务不该占着出口，也不该把出口的使用次数记上一笔。
        #
        # 🔴 借不到槽位（等超时）按 `network` 记在**这个账号**头上，
        #    而不是把整批停掉：出口忙是局部问题，Intern-Register-Tool 的
        #    `AllSlotsDead` 那种全局终止是为“槽位进程压根没起来”准备的，
        #    而那种情况应当在启动预检里就报出来。
        try:
            slot = slot_pool.acquire()
        except SlotPoolEmpty as ex:
            rec.fail(ERR_NETWORK, f"代理槽位池：{ex}", stage="mailbox")
            record_out(rec)
            return
        rec.proxy = slot or config.HR_PROXY
        mail = mail_factory()
        client = client_factory(slot)
        try:
            ticket = provision(
                rec,
                mail=mail,
                client=client,
                mail_domain=mail_domain,
                slot=slot,
            )
        except _quota.QuotaExceeded as ex:  # 注入的假客户端也可能抛
            rec.skip(ERR_QUOTA_GUARD, str(ex))
            slot_pool.release(slot, ok=None)
            record_out(rec)
            return
        except Exception as ex:  # noqa: BLE001
            kind = classify(ex)
            stage = _failed_stage(rec)
            rec.fail(kind, f"{type(ex).__name__}: {ex}" if kind == ERR_NETWORK else str(ex), stage)
            # 只有 `network` 才可能真的是出口的问题 —— 把“年龄门被拒”
            # 记到出口头上，会让每个出口的失败数变成噪音，报告就没人看了。
            slot_pool.release(slot, ok=(False if kind == ERR_NETWORK else None), error=rec.error)
            # 注意：这里**不在** `with lock:` 里调 `record_out`（同上的自锁陷阱）。
            if kind == ERR_QUOTA:
                with lock:
                    streak["n"] += 1
                    n = streak["n"]
                    tripped = n >= QUOTA_STREAK_STOP
                    if tripped:
                        stop.set()
                if tripped:
                    log(f"  ⛔ 连续 {n} 次服务端配额拒绝 —— 停止本批剩余任务（不发请求）")
            else:
                with lock:
                    streak["n"] = 0
            record_out(rec)
            return
        with lock:
            streak["n"] = 0
        say(
            f"  ▸ {rec.email} 邮箱+链接就绪"
            f"（出口 {slot or '直连'}，等信 {rec.timings.get('mail_wait_ms', 0)}ms，"
            f"轮询 {ticket.mail_polls} 次）"
        )
        # 槽位**不在这里归还** —— 消费者还要用它做密码登录（登录限流是按出口的）。
        ticket.proxy = slot
        q.put(ticket)

    def consumer() -> None:
        while True:
            item = q.get()
            try:
                if item is sentinel:
                    return
                rec = finish(item, client_factory=client_factory, slot_pool=slot_pool)
                if rec.status == "success":
                    say(f"  ✓ {rec.email} 注册成功（{_fmt_ms(rec)}）")
                else:
                    say(f"  ✗ {rec.email} 失败 [{rec.error_kind}] {rec.error[:120]}")
                # 出口的成败由**整条链**的结果决定（而不是只看注册那一段）。
                slot_pool.release(
                    item.proxy,
                    ok=(
                        True
                        if rec.status == "success"
                        else (False if rec.error_kind == ERR_NETWORK else None)
                    ),
                    error=rec.error,
                )
                record_out(rec)
            finally:
                q.task_done()

    consumers = [threading.Thread(target=consumer, daemon=True) for _ in range(max(1, workers))]
    for th in consumers:
        th.start()
    # 🔴 生产者并发必须**真的**受 `--producers` 约束：手写 `count` 个线程再逐个
    #    `start()` 的话，那个参数只是个装饰 —— 而“参数没生效”不会报错，
    #    只会让限流变成一句空话（实测：`--producers 1` 跑出了 4 路并发）。
    #    `ThreadPoolExecutor` 的 `max_workers` 才是真正的闸门。
    with ThreadPoolExecutor(max_workers=max(1, producers)) as pool:
        list(pool.map(producer, range(count)))
    # 🔴 哨兵数量 = 消费者数量：少一个就有线程永远 `get()` 阻塞，
    #    `run_batch` 直接挂死（而且是在"全部账号都已跑完"之后才挂）。
    for _ in consumers:
        q.put(sentinel)
    for th in consumers:
        th.join()
    return done


def _failed_stage(rec: AccountRecord) -> str | None:
    """失败发生在哪一步 —— **靠游标，不靠猜**。

    🔴 `AccountRecord.enter()` 在每个阶段开始时把 `stage_now` 设成那个阶段。
      不能用“第一个还是空的阶段”代替：`stages` 里的值会被提前写（例如
      `activate` 会在拿到邮件后先标上等信耗时），而“第一个空阶段”会把
      一次**校验链接失败**报成**设密码失败** —— 那是一个**错误结论**，
      比“不知道”坏得多。

    游标为空（比如注入的假客户端在第一个阶段之前就抛）时，退回“第一个空阶段”。
    """
    if rec.stage_now:
        return rec.stage_now
    for name in STAGES:
        if not rec.stages.get(name):
            return name
    return None


def _fmt_ms(rec: AccountRecord) -> str:
    total = sum(int(v) for v in rec.timings.values())
    parts = " ".join(f"{k[:-3]}={v}ms" for k, v in rec.timings.items() if v)
    return f"合计 {total}ms｜{parts}"
