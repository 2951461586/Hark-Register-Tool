"""CF Worker 临时邮箱客户端。

远端 Worker（与 Intern-Register-Tool 共用同一台）已实现建地址、按收件人索引查信，
以及**规则驱动的链接提取**。

🔴 2026-10-08 状态变更（重要）：Worker 侧**已经**为 hark 配了提取规则
   （D1 `rules` 表，remark = "Hark 魔法链接（注册 / 登录）"），所以**新**邮件回来时
   `extracted_json` 会带链接。在此之前（2026-10-07 实测）它是 `[]`，链接只在正文里。
   本模块的正文抽取逻辑因此**保留** —— 它现在服务两类漏网情形：
     ① 规则落库之前就已存进 D1 的历史行；
     ② Worker 的规则缓存窗口（`RULES_TTL_MS = 60s`）内刚插入的新规则。
   两条路都走，见 `Mail.links`。

🔴 另一条与 hark 直接相关的运营风险（2026-10-07 实测）：D1 的 `emails` 表总量上限
   是 **100 行**（`MAX_MESSAGES_TOTAL`），而它是按 `id` 滚动的。当天一个 Grok 推广
   轰炸（~1 封/分钟）把 100 行占掉了 98 行，于是**别人的激活邮件被挤掉**：
   hark 的 7 行掉到 2 行。带 `extracted_json` 的行受独立窗口保护，
   所以“拿到链接就快读”是对的，而 `wait_for_link` 的轮询间隔 0.8s 也让它几乎不会踩到。

API（均需 X-Admin-Token）：
  POST /api/mailboxes           {"domain": "...", "count": N} -> {"emails": [...]}
  GET  /api/inbox?email=<addr>  按收件人列出邮件（走 idx_emails_to_address 索引）
  GET  /admin/all?limit=N       列出最新 N 封（全表，不支持按收件人过滤）
  GET  /admin/msg?id=&email=    单封邮件详情
  DELETE /admin/delete?id=&email=

踩坑记录（都是实测，不是推断）：
  - 鉴权头同时支持 `X-Admin-Token` 与 `Authorization: Bearer`，两个都带最稳
  - **收信一律走 `/api/inbox?email=`**，别用 `/admin/all`：
      · `/admin/all` 不支持按收件人过滤，每次读 N+1 行，烧 D1 读配额；
      · 更要命的是**窗口截断** —— 它返回"最新 N 条"，而 D1 retention 只留
        100 行，这张表会被同 Worker 上的别的项目灌满，我们的邮件一旦被挤出
        窗口就**永远读不到**。按收件人索引查则挤不掉。
  - 邮件实测在 magic-link 请求后 **1~3s** 到达（对比 OpenXLab 的 6.02s）
  - 5xx 必须**重试而不是判死**：Worker 会间歇性抛 Cloudflare `Error 1101`，
    而那时邮件其实好好躺在库里。轮询本来就是在等，多等几次的代价远小于
    丢掉一个**已经注册成功**的账号。4xx（401 凭据错 / 404）才是我们的问题。
"""

import html
import json
import re
import time
from dataclasses import dataclass, field

import requests

from common import config

# 链接提取：够用即可，不做 HTML 解析。
_URL_RE = re.compile(r"https?://[^\s\"'<>\)\]\}]+", re.I)
# 中英文标点常被粘在 URL 尾部，剪掉。
# 🔴 这条**必须**有：邮件正文里链接后面紧跟 `)` 或 `。`（实测 hark 的邮件
#    把同一个链接写了两次，一次在 Markdown 括号里、一次是裸 URL），
#    不剪的话 query 尾巴上会多一个字符，服务端直接判 token 无效。
_TRAILING = ".,;:!?、。，；：！？）】」』\"'"


def now_ms() -> int:
    """当前时间（**毫秒**）。

    🔴 单位必须与 Worker 的 `received_at` 对齐 —— 实测它是**毫秒**
      （`1791375033474`，与同一条记录的 `receivedDateTime` ISO 串是同一时刻）。
      把秒当毫秒传进 `since_ts`，过滤会**全部失效**（所有邮件都 > 那个阈值），
      于是重跑时会拿到上一轮的旧链接 —— 而旧链接**仍然有效**（实测可重复
      校验并换发新 token），所以这个 bug 不会当场炸，只会让“重跑”变得不可靠。
    """
    return int(time.time() * 1000)


def _as_int(raw, default: int = 0) -> int:
    """宽容地取整数。**坏值退回默认**，不抛。

    ⚠ 必须宽容：`received_at` 是 Worker 侧拼进 JSON 的字段，而我们**不拥有**
      那个 Worker（它同时服务别的项目，字段形态随时可能变）。一个
      `"2026-10-07T09:49:31Z"` 之类的字符串不该让整条收信链路炸掉 ——
      时间戳只用来做 `since_ts` 过滤，取不到退化成 0（= 不过滤）是可接受的降级。
    """
    if isinstance(raw, bool):
        return default
    if isinstance(raw, int):
        return raw
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return default


def extract_links(text: str) -> list[str]:
    """从正文里抽 http(s) 链接，去重且保持出现顺序。

    `html.unescape` 是**必须**的：HTML 正文里的 `&amp;` 会让提取到的 URL
    在浏览器里打不开（查询串被截断 —— magic link 的 `token` / `callbackURL`
    恰好都在 query 里）。实测 hark 的邮件 `body`（纯文本）里 `&` 是原样的，
    但 `raw_html` 里是 `&amp;`，两条路都要能走。
    """
    out: list[str] = []
    seen: set[str] = set()
    for raw in _URL_RE.findall(html.unescape(text or "")):
        url = raw.rstrip(_TRAILING)
        if url and url not in seen:
            seen.add(url)
            out.append(url)
    return out


@dataclass
class Mail:
    id: str
    to_address: str
    from_address: str
    subject: str
    body: str
    extracted_json: str
    received_at: int
    raw_html: str = ""
    # 正文里抽出来的链接。放在字段里（而不是每次现算）是为了让
    # `links` 这个属性在"Worker 已给结构化链接"与"我们自己抽"两条路上同构。
    body_links: list[str] = field(default_factory=list)

    @property
    def links(self) -> list[str]:
        """候选链接：**Worker 给的结构化结果优先，正文抽取兜底**。

        🔴 两条路都要走，不能只信 `extracted_json`：实测（2026-10-07，当时 Worker
        还没为 hark 配规则）hark 的邮件 `extracted_json` 是 `[]`，
        只信它会让 `find_link` 恒返回 `None` ⇒ 每个账号都卡在"等激活邮件"。
        反过来也不能只信正文：Worker 的结构化结果是**服务端解析**的，
        比我们的正则更懂那封邮件的形状（Intern 那边 OpenXLab 的邮件就是
        只有结构化结果里有链接）。
        """
        out: list[str] = []
        seen: set[str] = set()
        try:
            items = json.loads(self.extracted_json or "[]")
        except (ValueError, TypeError):
            items = []
        for it in items:
            if isinstance(it, dict) and it.get("value"):
                u = str(it["value"])
                if u not in seen:
                    seen.add(u)
                    out.append(u)
        for u in self.body_links:
            if u not in seen:
                seen.add(u)
                out.append(u)
        return out

    def find_link(self, *keywords: str) -> str | None:
        """按关键词匹配链接（默认匹配激活 / 登录类）。"""
        kws = [k.lower() for k in keywords] or ["magic-link", "verify", "token"]
        for url in self.links:
            low = url.lower()
            if any(k in low for k in kws):
                return url
        return None


class TempMailClient:
    """CF Worker 邮箱客户端。**线程不安全**（每个任务一个实例）。"""

    def __init__(
        self,
        base: str | None = None,
        token: str | None = None,
        timeout: float | None = None,
    ):
        self.base = (base or config.WORKER_BASE).rstrip("/")
        self.token = token or config.WORKER_ADMIN_TOKEN
        self.timeout = float(timeout or config.REQUEST_TIMEOUT)
        self.session = requests.Session()
        # 🔴 邮箱 Worker **默认不走代理**（`HR_PROXY_MAIL=1` 可强制打开）。
        # 理由：收信轮询是整链的瓶颈（激活邮件到达本身就要 1~3s），绕道代理
        # 只会更慢；而 Cloudflare Worker 不关心我们的出口 IP，走代理没有收益。
        # 代理只该挂在**被封的那一侧**（hark）。
        if config.PROXY_MAIL:
            config.apply_proxy(self.session)
        # 上一次 `wait_for_mail` 失败的原因（"" = 没有异常，纯粹是邮件没到）。
        # 用来把"邮件没到"和"邮箱服务读不出来"分开 —— 两者的修法完全不同。
        self.last_error = ""
        # 上一次 `wait_for_mail` 打了几次收信接口（含 5xx 重试）。
        # 🔴 为什么要数这个：收信背后是 D1，这个 N 是判断"我们是不是把 D1
        #    读限额打满的元凶"的唯一凭据（走索引查询时每次读 0~1 行）。
        self.last_polls = 0
        self.last_http_errors = 0
        self.session.headers.update(
            {
                "X-Admin-Token": self.token,
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/json",
            }
        )

    # ── 邮箱管理 ──────────────────────────────────────────────
    def create_mailbox(self, domain: str | None = None, count: int = 1) -> list[str]:
        """新建 `count` 个地址，返回邮箱列表。

        ⚠ 返回列表长度可能**小于** `count`（Worker 侧生成失败时静默少给）。
          调用方必须自己检查长度 —— 少给会被下游当成"邮箱建不出来"，
          而真实原因（域名配错 / D1 写配额）会被这条笼统错误盖住。
        """
        r = self.session.post(
            f"{self.base}/api/mailboxes",
            json={"domain": domain or config.WORKER_DOMAIN, "count": count},
            timeout=self.timeout,
        )
        r.raise_for_status()
        data = r.json()
        if not data.get("ok"):
            raise RuntimeError(f"create_mailbox failed: {data}")
        return data.get("emails", []) or []

    # ── 邮件读取 ──────────────────────────────────────────────
    def list_mails(self, limit: int | None = None, email: str | None = None) -> list[Mail]:
        """列邮件。

        🔴 **`email` 给定 → 走 `/api/inbox?email=`**（服务端按
        `idx_emails_to_address` 索引过滤，实测读 0~1 行）。
        不给 → 退回 `/admin/all?limit=N`（全表，读 N+1 行）。
        为什么收信必须带 `email` 见模块 docstring。
        """
        if email:
            r = self.session.get(
                f"{self.base}/api/inbox", params={"email": email}, timeout=self.timeout
            )
        else:
            r = self.session.get(
                f"{self.base}/admin/all",
                params={"limit": limit or 50},
                timeout=self.timeout,
            )
        r.raise_for_status()
        raw = r.json().get("messages", []) or []
        out: list[Mail] = []
        for m in raw:
            if not isinstance(m, dict):
                continue
            body = str(m.get("body") or m.get("body_text") or "")
            raw_html = str(m.get("raw_html") or "")
            out.append(
                Mail(
                    id=str(m.get("id", "")),
                    to_address=str(m.get("to_address", "")),
                    from_address=str(m.get("from_address", "")),
                    subject=str(m.get("subject", "")),
                    body=body,
                    extracted_json=str(m.get("extracted_json") or "[]"),
                    received_at=_as_int(m.get("received_at")),
                    raw_html=raw_html,
                    # 🔴 `body` 与 `raw_html` **都**抽一遍并合并：
                    #    实测有些邮件只有一个字段非空，而链接恰恰可能在另一个里。
                    #    `extract_links` 会去重，所以重复拼接不会产生重复链接。
                    body_links=extract_links(f"{body}\n{raw_html}"),
                )
            )
        return out

    def wait_for_mail(
        self,
        address: str,
        sender_contains: str = "hark",
        timeout: int | None = None,
        interval: float | None = None,
        since_ts: int = 0,
        limit: int | None = None,
    ) -> Mail | None:
        """轮询等待目标地址的邮件。

        失败语义（与 Intern-Register-Tool 对齐，三个邮箱源同一套）：
        `last_error` 为空 = **邮件没到**（该继续等）；非空 = **读不出来**（该修）。

        🔴 `since_ts` 是"只看这之后到的信"。注册链路**必须**传它：
          同一个地址在补跑 / 重试时收件箱里可能还躺着上一轮的 magic link，
          不设下限就会拿到一个**已经用过或已过期**的链接。
        """
        timeout = timeout or config.MAIL_POLL_TIMEOUT
        interval = interval or config.MAIL_POLL_INTERVAL
        deadline = time.time() + timeout
        want = (sender_contains or "").lower()
        self.last_error = ""
        self.last_polls = 0
        self.last_http_errors = 0
        errs = 0
        last_err = ""

        while time.time() < deadline:
            self.last_polls += 1
            try:
                mails = self.list_mails(limit=limit, email=address)
            except requests.HTTPError as ex:
                code = ex.response.status_code if ex.response is not None else 0
                # 4xx（401 凭据错 / 404）才是**我们**的问题，必须立刻失败 ——
                # 继续轮询只会把"配置错了"伪装成"邮件一直没到"。
                if code and code < 500:
                    raise
                errs += 1
                self.last_http_errors = errs
                last_err = f"HTTP {code}"
                time.sleep(min(interval * (1 + errs // 5), 5.0))
                continue
            except Exception as ex:  # noqa: BLE001
                # 网络层失败同样按"读不出来"处理并重试：轮询的代价远小于
                # 把一个已注册成功的账号判失败。
                errs += 1
                self.last_http_errors = errs
                last_err = f"{type(ex).__name__}: {ex}"
                time.sleep(min(interval * (1 + errs // 5), 5.0))
                continue
            for m in mails:
                if since_ts and m.received_at and m.received_at < since_ts:
                    continue
                if want and want not in m.from_address.lower() and want not in m.subject.lower():
                    continue
                return m
            time.sleep(interval)
        if errs:
            self.last_error = (
                f"邮箱 Worker 持续读不出来（{errs} 次，最近 {last_err}）—— 不是邮件没到，是读不出来"
            )
        return None

    def wait_for_link(self, address: str, *keywords: str, **kw) -> str | None:
        """等一封带指定关键词链接的邮件，返回那条链接。

        ⚠ 与 `wait_for_mail` 的差别不只是"多抽一次链接"：这里**继续等下一封**，
          而不是拿到第一封就返回。理由：收件箱里可能先到一封**无关**的邮件
          （hark 的营销信 / 上一轮的旧链接），拿到它再 `find_link` 会返回
          `None`，于是把一次本该成功的等待变成失败。
        """
        timeout = kw.pop("timeout", None) or config.MAIL_POLL_TIMEOUT
        interval = kw.pop("interval", None) or config.MAIL_POLL_INTERVAL
        deadline = time.time() + timeout
        self.last_error = ""
        self.last_polls = 0
        self.last_http_errors = 0
        while time.time() < deadline:
            mail = self.wait_for_mail(
                address, timeout=max(1, int(deadline - time.time())), interval=interval, **kw
            )
            if mail is None:
                return None
            link = mail.find_link(*keywords)
            if link:
                return link
            # 这封没链接（或链接不是我们要的）→ 抬高 `since_ts` 再看下一封。
            if mail.received_at:
                kw["since_ts"] = mail.received_at + 1
        return None
