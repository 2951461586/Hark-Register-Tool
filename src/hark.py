"""Hark 协议客户端 —— 整条注册链路的全部 HTTP 交互。

协议来源
--------
**全部来自实测**（`hark.com-1.har` / `hark.com-2.har` 两份 HAR 复现 + 线上
逐条验证），不是从混淆 JS 里读出来的。完整证据链见 `docs/protocol.md`。

链路（7 步，全程纯 HTTP，**零浏览器**）
--------------------------------------
    ① POST {hark}/api/auth/account-discovery   {"identifier","kind":"email"}
         → {"exists":false,"hasPassword":false}
    ② POST {hark}/api/auth/signup-age          {"identifier","kind","birthday"}
         → {"accepted":true}                    ← 年龄门，**满 18 才过**
    ③ POST {hark}/api/auth/sign-in/magic-link  {"email","callbackURL",...}
         → {"status":true}                      ← 发信（含**不存在**的邮箱 ⇒ 注册）
    ④ 收信（CF Worker）→ 链接在 auth.hark.com/api/auth/magic-link/verify
    ⑤ GET  该链接 → 302 + Set-Cookie `__Secure-hark.session_token`
    ⑥ POST {hark}/api/auth/set-password        {"newPassword": ...}
         → {"status":true}                      ← **一次性**，二次调用报 PASSWORD_ALREADY_SET
    ⑦ POST {hark}/api/auth/sign-in/email       {"email","password"}
         → {"redirect":false,"token":...,"user":{...}}   ← 密码登录（真正的"能用"证明）

之后的只读校验（`app` 阶段）：get-session / profile / billing/summary /
conversations。

三条容易踩的坑
--------------
1. **两个域名**：站点 API 在 `hark.com`，auth API 在 `auth.hark.com`。
   cookie 的 `Domain=hark.com`（不带点）⇒ 两边共享会话；但 magic link 的
   校验端点**只在** auth 域上（邮件里给的就是它）。
2. **`__cf_bm` 是 CF 的 bot 管理 cookie**，与业务无关。它会被 `requests`
   一起存进 jar —— 无害，但**不要**把它当会话凭据写进台账。
3. **不带浏览器 UA 会被 CF 边缘直接 403**（`error code: 1010`，不是应用回的）。
   所以每个 session 都装 `config.USER_AGENT`。

线程模型
--------
`HarkClient` **不是线程安全的**（底层是 `requests.Session`）。本项目的用法是
"生产者线程建它 → 通过队列交给消费者线程 → 之后只有消费者碰它"，
即**顺序交接**，不存在并发访问。要改成并发共享就得换成每线程一个实例。
"""

import json
import time
from urllib.parse import urlparse

import requests

from common import config

# ── 错误分类 ──────────────────────────────────────────────────────
# 🔴 分类而不是靠文本匹配，理由与 Intern-Register-Tool 的 `error_kind` 一致：
#    我们自己拼的错误文案里也可能出现服务端的词（比如把响应体原文附进去），
#    文本匹配分不清"服务端说的"和"我们引用的"。**字段是权威，文本只兜底**。
ERR_QUOTA = "quota"
ERR_QUOTA_GUARD = "quota_guard"
ERR_REJECTED = "rejected"
ERR_NETWORK = "network"
ERR_MAIL = "mail"

# 会话 cookie 名（better-auth 约定 + `__Secure-` 前缀）。
SESSION_COOKIE = "__Secure-hark.session_token"
REGION_COOKIE = "__Secure-hark.region"
ACCESS_COOKIE = "__Secure-hark.access"

# 实测：`set-password` 是**一次性**的 —— 第二次调用返回
# `400 {"code":"PASSWORD_ALREADY_SET"}`。调用方必须能识别它，
# 否则"重试一次"会把一个**已经成功**的步骤报成失败。
CODE_PASSWORD_ALREADY_SET = "PASSWORD_ALREADY_SET"
CODE_PASSWORD_TOO_SHORT = "PASSWORD_TOO_SHORT"
CODE_CARD_VERIFICATION_REQUIRED = "CARD_VERIFICATION_REQUIRED"
CODE_INVALID_BIRTH_DATE = "INVALID_BIRTH_DATE"
CODE_INVALID_EMAIL_OR_PASSWORD = "INVALID_EMAIL_OR_PASSWORD"


class HarkError(Exception):
    """链路错误的基类。`kind` 是结构化类别（见上面的 ERR_* 常量）。"""

    kind = ERR_NETWORK

    def __init__(self, message: str, *, code: str = "", status: int = 0):
        super().__init__(message)
        self.code = code
        self.status = status


class HarkRejected(HarkError):
    """服务端**明确拒绝**（4xx，非配额）：年龄门、密码策略、凭据错……"""

    kind = ERR_REJECTED


class HarkQuota(HarkError):
    """限流 / 配额：HTTP 429，或响应体里的配额类业务码。

    ⚠ hark 侧到目前为止**没有实测到**这个分支（见 docs/protocol.md
      「限流与配额：实测」）。留着它是为了"观测到就能停下"，而不是假装已知。
    """

    kind = ERR_QUOTA


class HarkNetwork(HarkError):
    """网络 / 超时 / 5xx —— **可重试**的一类。"""

    kind = ERR_NETWORK


# 配额类业务码（实测未出现，先按"看起来像配额"的措辞兜底）。
_QUOTA_CODES = frozenset({"TOO_MANY_REQUESTS", "RATE_LIMITED", "QUOTA_EXCEEDED"})


def _json_or_none(r):
    """响应 → JSON dict；不是 JSON 就返回 None（**不抛**）。

    单独抽出来是为了把「是不是 JSON」与「状态码多少」解耦：
    CF 边缘拦截时返回的是 HTML 错误页（`error code: 1010`），
    而应用层错误是 JSON —— 两者必须分开判，否则会得到
    `JSONDecodeError` 这种指向错误位置的异常。
    """
    try:
        d = r.json()
    except ValueError:
        return None
    return d if isinstance(d, dict) else None


class HarkClient:
    """一个账号一条链路一个实例。"""

    def __init__(
        self,
        *,
        base: str | None = None,
        auth_base: str | None = None,
        timeout: float | None = None,
        user_agent: str | None = None,
        proxy: str | None = None,
    ):
        self.base = (base or config.HARK_BASE).rstrip("/")
        self.auth_base = (auth_base or config.AUTH_BASE).rstrip("/")
        self.timeout = float(timeout or config.REQUEST_TIMEOUT)
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": user_agent or config.USER_AGENT,
                "Accept": "*/*",
                "Origin": self.base,
                "Referer": f"{self.base}/login",
            }
        )
        # 代理只挂 hark 这一侧（邮箱 Worker 不挂，见 tempmail 的说明）。
        config.apply_proxy(self.session, proxy)
        # 最近一次响应的状态码 / 业务码，供报告与错误打标使用。
        self.last_status = 0
        self.last_code = ""

    # ── 底层 ──────────────────────────────────────────────────
    def _url(self, path: str, *, auth: bool = False) -> str:
        # 🔴 已经是一条**绝对 URL** 时原样返回。`verify_magic_link` 传进来的
        #    就是邮件里那条完整链接（它在 auth.hark.com 上，而 `auth=False`
        #    的默认 base 是 hark.com）—— 拼接会得到 `hark.comhttps://auth…`
        #    这种畸形串，而 requests 的报错是 `host='hark.comhttps'`，
        #    一眼看不出是谁拼错的。
        if path.startswith("http://") or path.startswith("https://"):
            return path
        return (self.auth_base if auth else self.base) + path

    def _request(
        self,
        method: str,
        path: str,
        *,
        payload: dict | None = None,
        auth: bool = False,
        allow_redirects: bool = False,
        referer: str | None = None,
        retries: int = 0,
    ) -> requests.Response:
        """发一次请求，并把"错误"翻译成 `HarkError` 的某个子类。

        🔴 **判据顺序**：先看状态码，再看响应体。反过来的话，
        `429` 的响应体里如果没有 JSON，就会被当成"网络问题"（可重试），
        而配额触顶时重试只是白打枪。

        🔴 `retries` 只给**幂等**的调用用（`discover` / `signup_age` /
        `magic_link`）—— 它们的重放代价只是"多发一封信"。反过来，
        `set_password` / `sign_in` / `verify_magic_link` **一律不重试**：
        前两个是**一次性**的（重试会撞 `PASSWORD_ALREADY_SET` / 多建一条会话），
        而“第一次到底成没成”在 5xx 时是**未知**的。

        ⚠ 上限取 1（总共两次尝试），不取更多：实测 hark 的 magic-link
          端点会**整段时间**返回 500（空响应体），而重试只是把这个数字翻倍。
          重试是为了抹掉**瞬时**抖动，不是为了等一个宕机的服务。
        """
        last_exc: HarkNetwork | None = None
        for attempt in range(retries + 1):
            try:
                return self._once(
                    method,
                    path,
                    payload=payload,
                    auth=auth,
                    allow_redirects=allow_redirects,
                    referer=referer,
                )
            except HarkNetwork as ex:
                # 只重试 5xx / 传输层；配额与拒绝都不进这里（见上面的判据顺序）。
                last_exc = ex
                if attempt < retries:
                    time.sleep(1.5 * (attempt + 1))
        # 不用 `assert`：断言会被 `python -O` **整个去掉**，那时这里会变成
        # “默默地返回 None”，下游拿 None 当 response 用 —— 报错点离原因很远。
        if last_exc is None:  # pragma: no cover —— 循环至少跑一次，到不了这里
            raise HarkNetwork("请求未执行（内部错误）")
        raise last_exc

    def _once(
        self,
        method: str,
        path: str,
        *,
        payload: dict | None = None,
        auth: bool = False,
        allow_redirects: bool = False,
        referer: str | None = None,
    ) -> requests.Response:
        """单次请求（不重试）。见 `_request` 的说明。"""
        headers = {}
        if payload is not None:
            headers["Content-Type"] = "application/json"
        if referer:
            headers["Referer"] = referer
        try:
            r = self.session.request(
                method,
                self._url(path, auth=auth),
                json=payload,
                headers=headers or None,
                timeout=self.timeout,
                allow_redirects=allow_redirects,
            )
        except requests.RequestException as ex:
            raise HarkNetwork(f"{type(ex).__name__}: {ex}") from ex

        self.last_status = r.status_code
        body = _json_or_none(r)
        self.last_code = str((body or {}).get("code") or "")

        if r.status_code == 429 or self.last_code in _QUOTA_CODES:
            raise HarkQuota(
                f"HTTP {r.status_code} {self.last_code or 'RATE_LIMITED'}：{_short(body, r)}",
                code=self.last_code or "RATE_LIMITED",
                status=r.status_code,
            )
        if 500 <= r.status_code:
            raise HarkNetwork(f"HTTP {r.status_code}：{_short(body, r)}", status=r.status_code)
        if 400 <= r.status_code:
            raise HarkRejected(
                f"HTTP {r.status_code} {self.last_code or '?'}：{_short(body, r)}",
                code=self.last_code,
                status=r.status_code,
            )
        return r

    # ── ① 账号探测 ────────────────────────────────────────────
    def discover(self, identifier: str, kind: str = "email") -> dict:
        """这个邮箱在 hark 上存不存在。

        ⚠ **不存在的邮箱也返回 200**（`{"exists":false}`）—— 所以它不能当
          "注册前置校验"用，只能当"走注册还是走登录"的分支依据。
          实测连 `not-an-email` 这种串也是 200 + exists:false（服务端不校验格式）。
        """
        r = self._request(
            "POST",
            "/api/auth/account-discovery",
            payload={"identifier": identifier, "kind": kind},
            retries=1,
        )
        return _json_or_none(r) or {}

    # ── ② 年龄门 ──────────────────────────────────────────────
    def signup_age(self, identifier: str, birthday: str, kind: str = "email") -> dict:
        """提交生日过年龄门。

        实测判据（2026-10-07 逐岁扫）：
            age >= 18 → 200 {"accepted": true}
            age <  18 → 403 {"code":"CARD_VERIFICATION_REQUIRED"}（要绑卡）
            非法日期  → 400 {"code":"INVALID_BIRTH_DATE"}
        """
        r = self._request(
            "POST",
            "/api/auth/signup-age",
            payload={"identifier": identifier, "kind": kind, "birthday": birthday},
            retries=1,
        )
        return _json_or_none(r) or {}

    # ── ③ 发 magic link ───────────────────────────────────────
    def request_magic_link(self, email: str, callback_url: str | None = None) -> dict:
        """请求登录/注册邮件。

        🔴 `callbackURL` 必须指向 **`/set-password`**：新账号走完校验后会被
           `302` 到那里，而前端就是靠这个页面把"设密码"这步接上的。
           默认 `errorCallbackURL` 指回 `/login`（与 HAR 一致）。
        """
        payload = {
            "email": email,
            "callbackURL": callback_url or f"{self.base}/set-password",
            "errorCallbackURL": f"{self.base}/login",
        }
        r = self._request("POST", "/api/auth/sign-in/magic-link", payload=payload, retries=1)
        return _json_or_none(r) or {}

    # ── ⑤ 校验 magic link ─────────────────────────────────────
    def verify_magic_link(self, url: str) -> dict:
        """GET 邮件里的链接 → 拿到会话 cookie。

        🔴 **不跟随重定向**：`302` 的 `Location` 是 `https://hark.com/set-password`，
          那是一个**前端页面**（几百 KB 的 HTML + 一堆 JS chunk）。跟过去
          既没有信息增量，又白白多打一个站点的请求。会话在 `Set-Cookie` 里，
          拿到就够了。

        🔴 **链接必须是邮件里原样的那一条**（含 `callbackURL` / `hark_region`
          两个 query）。少一个参数服务端会 302 到错误页而不是发会话。
          实测第二次 GET 同一个链接**仍然有效**并会**换发**一个新的 session
          token —— 所以"重放旧链接"不是错误路径，但会浪费一次往返。
        """
        host = urlparse(url).hostname or ""
        if "hark.com" not in host:
            # 防御：不要把任意 URL 塞进这里（会带 cookie 打到第三方去）。
            raise HarkRejected(f"magic link 的 host 不是 hark.com：{host!r}", code="BAD_LINK")
        r = self._request("GET", url, allow_redirects=False)
        cookies = self.session.cookies.get_dict()
        if not any(k.endswith("session_token") for k in cookies):
            # 200 而不是 302 ⇒ 链接被拒（过期 / 已用尽 / 被改过）。
            raise HarkRejected(
                f"magic link 未换到会话（HTTP {r.status_code}，cookies={sorted(cookies)}）",
                code="MAGIC_LINK_REJECTED",
                status=r.status_code,
            )
        return cookies

    # ── ⑥ 设密码 ──────────────────────────────────────────────
    def set_password(self, new_password: str) -> dict:
        """用**刚校验过 magic link 的会话**设置密码。

        🔴 这一步是**一次性**的：第二次调用返回
          `400 {"code":"PASSWORD_ALREADY_SET"}`。调用方必须把"已经设过了"
          当成**成功**（否则重试会把一个已完成的步骤报成失败）。
        """
        r = self._request(
            "POST",
            "/api/auth/set-password",
            payload={"newPassword": new_password},
            referer=f"{self.base}/set-password",
        )
        return _json_or_none(r) or {}

    # ── ⑦ 密码登录 ────────────────────────────────────────────
    def sign_in(self, email: str, password: str) -> dict:
        """用密码登录 —— 这是"账号真能用"的**最强证明**。

        🔴 它**换一条全新的会话**（新 token、新 cookie），与 magic link 那条
          会话无关。所以台账里存的 `session_token` 应当取自这一步，
          而不是 magic link 那条 —— 后者是"注册过程的中间态"。
        """
        r = self._request(
            "POST",
            "/api/auth/sign-in/email",
            payload={"email": email, "password": password},
            referer=f"{self.base}/login",
        )
        return _json_or_none(r) or {}

    # ── 只读校验 ──────────────────────────────────────────────
    def get_session(self) -> dict:
        """当前会话。未登录时返回 `{}`（服务端给 200 + 空/ null）。"""
        r = self._request("GET", "/api/auth/get-session", referer=f"{self.base}/chat")
        return _json_or_none(r) or {}

    def profile(self) -> dict:
        r = self._request("GET", "/api/profile", referer=f"{self.base}/chat")
        return _json_or_none(r) or {}

    def billing_summary(self) -> dict:
        """额度结构。实测（免费档）：

            meters.harkTokens.dailyLimit  = 8,000,000   （每日，UTC 00:00 重置）
            meters.harkTokens.poolLimit   = 40,000,000  （每月池，月初重置）
            plan.id = "free", plan.name = "hark pro"

        ⚠ 这两个数字是**观测值**，不是承诺 —— 平台随时可能改。台账里按账号
          记下来，正是为了将来能回答"额度什么时候变的"。
        """
        r = self._request("GET", "/api/billing/summary", referer=f"{self.base}/chat")
        return _json_or_none(r) or {}

    def billing_options(self) -> dict:
        r = self._request(
            "POST",
            "/api/billing/options",
            payload={"storeContext": None},
            referer=f"{self.base}/chat",
        )
        return _json_or_none(r) or {}

    def conversations(self) -> dict:
        """会话列表。新账号实测已自带 1 条空会话（前端首屏建的）。"""
        r = self._request("GET", "/api/conversations", referer=f"{self.base}/chat")
        return _json_or_none(r) or {}

    # ── 会话凭据 ──────────────────────────────────────────────
    def session_token(self) -> str:
        """当前会话的 **token 明文部分**（`<token>.<hmac>` 里的前段）。

        ⚠ 这是 cookie 值的一半，**不是**可以直接当 Bearer 用的东西 ——
          better-auth 校验的是整条 cookie。取前段只是为了让台账里有一列
          可以人眼对照的标识（与 `get-session` 返回的 `session.token` 一致，
          实测相等）。
        """
        val = self.session.cookies.get(SESSION_COOKIE) or ""
        return val.split(".")[0] if val else ""

    def region(self) -> str:
        return self.session.cookies.get(REGION_COOKIE) or ""


def _short(body, r) -> str:
    """错误正文的**短**摘要：优先 JSON 里的 message，其次截断的原始文本。

    ⚠ 截断是必须的：CF 边缘的错误页有整页 HTML，原样塞进 `error` 字段会让
      台账里出现几 KB 的 HTML（而且里面可能带 `__cf_bm` 之类的东西）。

    🔴 **空响应体必须说出来**：hark 的 5xx 实测是 `Content-Type: text/plain` +
      **空 body**，不处理的话错误文案会退化成 `HTTP 500：`（冒号后面什么都没有），
      而人看到这个会以为是**我们**没把信息填进去。
    """
    if isinstance(body, dict):
        msg = body.get("message") or body.get("error") or ""
        if msg:
            return str(msg)[:200]
        return json.dumps(body, ensure_ascii=False)[:200]
    text = (r.text or "").strip()
    if not text:
        ctype = (r.headers or {}).get("Content-Type", "")
        return f"(空响应体{'，' + ctype if ctype else ''})"
    return text[:200].replace("\n", " ")


# ⚠ 这里原有 `wait_interval_default()` / `paced_sleep()` 两个函数（“全局最小间隔”
#    闸门），已于 2026-10-08 **删除**：实测证明注册侧的 429 是**按 IP**、
#    且只限 `magic-link` 端点，所以那个“每进程全局最小间隔”两头都不对
#    （既挡不住单出口超限，又白把小批量串行化）。取代它的是
#    `pipeline.register_limiter(slot)` —— 与登录侧同构的**按出口滑动窗口**。
#    判据与实测见 `common/config.REGISTER_MAX_PER_WINDOW` 的注释。
