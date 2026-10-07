"""全局配置。

所有密钥类信息集中在此，可通过环境变量或项目根的 `.env` 文件覆盖。

🔴 凭据**不写死在代码里** —— 本项目托管在公开仓库，写死等于直接泄漏。
   首次使用：`cp .env.example .env`，填入真实值。`.env` 已在 .gitignore 中。

与 Intern-Register-Tool 的差异（都是刻意的）
==========================================
1. **前缀换成 `HR_`**。两台工具可能共用同一台 CF Worker，但配置各归各的：
   共用前缀会让"我只改了 hark 的配额上限"变成"顺手把 intern 的也改了"。
2. **没有 `IR_MAILBOX_KIND` 那套可插拔邮箱源**。那套存在的唯一理由是
   OpenXLab 有**域名门**（`A0232`），逼得四个源轮流上；hark 实测**没有域名门**
   （your-mail-domain 直接过），所以这里只有 Worker 一个源 —— 不引入用不上的抽象。
3. **没有代理槽位池**。槽位池是为了绕 **IP 维度**的注册封禁（OpenXLab 实测
   累计配额触顶后 > 68h 不恢复）。hark 侧**尚未观测到任何 IP 封禁**，
   所以只保留一条可选 `HR_PROXY`；等真观测到再谈池子（见 docs/protocol.md）。
"""

import os
from pathlib import Path


def _load_dotenv(path: Path) -> None:
    """极简 `.env` 解析（stdlib 实现，不引 python-dotenv 依赖）。

    只填充「尚未存在于 os.environ」的键 —— 真实环境变量优先级更高，
    这样临时覆盖（`HR_REG_QUOTA_MAX=1 python run.py`）依然生效。
    """
    if not path.is_file():
        return
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = val


ROOT = Path(__file__).resolve().parents[1]
_load_dotenv(ROOT / ".env")


def _env_int(name: str, default: int) -> int:
    """读整数环境变量。**坏值退回默认**，不抛。

    ⚠ 刻意不抛：这些是"行为参数"，一个手滑写进去的 `HR_PASSWORD_LEN=abc`
    不该让整个工具起不来 —— 它会在启动校验之后、第一个请求之前以
    `ValueError: invalid literal for int()` 的形态炸在很深的地方。
    """
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


# ── CF Worker 临时邮箱（必填）─────────────────────────────────────
WORKER_BASE = os.getenv("HR_WORKER_BASE", "").strip()
# ⚠ 默认留空。缺它时由 `validate()` 在入口处报错，而不是静默发一堆 401。
WORKER_ADMIN_TOKEN = os.getenv("HR_WORKER_ADMIN_TOKEN", "").strip()
WORKER_DOMAIN = os.getenv("HR_WORKER_DOMAIN", "").strip()

# ── Hark 站点 ─────────────────────────────────────────────────────
# 🔴 **两个域名不是笔误**：站点 API 在 hark.com，auth API 在 auth.hark.com。
#    实测 cookie 的 Domain 是 `hark.com`（不带点），所以两边共享会话；
#    但 magic link 的校验端点只在 auth.hark.com 上（邮件里给的就是它）。
HARK_BASE = os.getenv("HR_HARK_BASE", "").strip() or "https://hark.com"
AUTH_BASE = os.getenv("HR_AUTH_BASE", "").strip() or "https://auth.hark.com"

# ── 注册资料 ──────────────────────────────────────────────────────
# 生日。年龄门实测：**满 18 才 accepted**，未满 18 返回
# `403 CARD_VERIFICATION_REQUIRED`（要绑卡），非法日期返回
# `400 INVALID_BIRTH_DATE`。所以这个默认值是"显然成年"的日期。
BIRTHDAY = os.getenv("HR_BIRTHDAY", "").strip() or "1995-01-01"
# 密码长度。服务端硬要求 >= 8（实测 `abc`/`Ab1!` → `PASSWORD_TOO_SHORT`，
# 而 8 位会通过长度检查进入下一步）。
PASSWORD_LEN = max(8, _env_int("HR_PASSWORD_LEN", 20))

# ── 行为参数 ──────────────────────────────────────────────────────
# 邮件实测在 magic-link 请求后 **1~3s** 到达；轮询间隔 0.8s 可在 1~2 次内命中，
# 而收信接口单次往返 ~0.3~0.6s，再密就只是在打 Cloudflare。
MAIL_POLL_INTERVAL = _env_float("HR_MAIL_POLL_INTERVAL", 0.8)
MAIL_POLL_TIMEOUT = _env_int("HR_MAIL_POLL_TIMEOUT", 120)
# ── 注册申请限流（🔴 实测：按 **IP**、限 `magic-link` 端点、约 5 次/分钟）──
#
# 实测方法与结论（2026-10-08，`tools/probes/probe_register_limit.py`）：
#   · 只打只读的 `discovery` —— 一直 200 ⇒ **限的是写端点**（`magic-link`）
#   · 同一个出口上第 6 次 `magic-link` → `429 RATE_LIMITED`
#   · 一旦 429：**换收件域名仍 429**（`edu.your-mail-domain`）⇒ 不是按域名键
#   · 一旦 429：**换出口立刻通过**（7901 → 7902）⇒ **是按 IP 键**
#   窗口长度未精确测出：观察到 ≤43s 就恢复（批次 7/8 的对照）。取 60s 偏保守。
#
# 🔴 这一条**推翻**了此前写进 README 的结论。当时的推理是“批次 6 里 3 个
#    **不同**出口同时 429 ⇒ 不是 per-IP” —— **那个推理是错的**：那 3 个出口
#    各自在前 60s 里都积了 ~5 次（每批 1 次 × 5 批），所以它们**同时**触顶。
#    同时性 ≠ 同一个键；它只说明它们的**历史相同**。
#
# ⇒ 加出口**确实**能提高注册吞吐（每出口一张窗口），上限 ≈ N × 4~5 次/分。
#    而旧的“全局 1 次/秒”**两头都不对**：既不能阻止某个出口超限
#    （5 个出口轮转时每个会跑到 12 次/分），又在小批量里白白把 5 个注册申请
#    串行化成 4 秒。所以它被**按出口的滑动窗口**取代了。
REGISTER_MAX_PER_WINDOW = max(1, _env_int("HR_REGISTER_MAX_PER_WINDOW", 4))
REGISTER_WINDOW_S = max(1.0, _env_float("HR_REGISTER_WINDOW_S", 60.0))
REQUEST_TIMEOUT = _env_float("HR_REQUEST_TIMEOUT", 30)

# ── 登录限流（**实测**：`sign-in/email` 约 3 次 / 10s）───────────────
# 🔴 这是本项目**唯一**测出准确数字的服务端限流。方法：同一个账号连打 5 次，
#    记录每一次的时刻与状态码：
#      第 1、2、3 次 → 200；第 4、5 次 → `429 RATE_LIMITED: Too many requests.`
#    （每个请求本身要 ~2.1s，所以前三次落在同一个 10s 窗口内、后两次被拦）
#    ⇒ 与 better-auth 的默认自定义规则 `"/sign-in/email": {window:10, max:3}` 完全吻合。
#
#    它必须被当成**硬约束**：`--workers 4` 会让 4 个账号在 ~2s 内各登录一次，
#    第 4 个必然 429 —— 而 429 会被下游读成"账号坏了"，那是一个**假阴性**
#    （Intern-Register-Tool 的 `check_keys_alive` 就是栽在这类假阴性上）。
#    所以消费者侧有滑动窗口限速 + 429 退避重试（见 `pipeline.finish`）。
LOGIN_MAX_PER_WINDOW = max(1, _env_int("HR_LOGIN_MAX_PER_WINDOW", 3))
LOGIN_WINDOW_S = max(1.0, _env_float("HR_LOGIN_WINDOW_S", 10.0))
# 撞到 429 后最多再试几次（每次退避一个窗口）。
# ⚠ 上限存在是因为"429 会自己好"只对**限流**成立；若平台改了策略（比如真的封了），
#    无限重试就是无限白打枪。
LOGIN_RETRIES = max(0, _env_int("HR_LOGIN_RETRIES", 3))

# 站点的 Cloudflare 边缘会按 UA 判人机：实测**不带浏览器 UA** 的裸
# python-urllib 请求被边缘直接 403（`error code: 1010`，不是应用回的）。
# 所以这里必须给一个真实的浏览器 UA，而不是 requests 的默认值。
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/154.0.0.0 Safari/537.36"
)

# ── 注册配额保护（本地累计计数）───────────────────────────────────
# 🔴 这是**本地保护**，不是权威计量：换机器 / 删 state 文件都会重置。
#    被误拦时用 `run.py --ignore-quota` 或调大 `HR_REG_QUOTA_MAX` 放行。
#
# ⚠ 默认值 20 是**没有服务端证据时的保守猜测**，不是实测的触顶点
#    （对比 Intern-Register-Tool：那边的 40 是实测出来的，因为它撞过墙）。
#    hark 侧到目前为止**没观测到任何注册配额**，所以这里的数字只用来
#    防"手滑跑了 500 个"，不声称它等于服务端上限。
REG_QUOTA_MAX = _env_int("HR_REG_QUOTA_MAX", 20)
REG_QUOTA_WINDOW_H = _env_float("HR_REG_QUOTA_WINDOW_H", 24)

# ── 出口代理（可选）───────────────────────────────────────────────
# 格式（与常见代理商控制台一致）：
#     HR_PROXY=host:port:user:pass          # 单条
#     HR_PROXY=http://user:pass@host:port   # 也接受完整 URL
HR_PROXY = os.getenv("HR_PROXY", "").strip()
# 收信轮询是否也走代理（默认否）。轮询是整链瓶颈，绕代理只会更慢；
# 而 Cloudflare Worker 不关心我们的出口 IP，走代理没有任何收益。
PROXY_MAIL = os.getenv("HR_PROXY_MAIL", "").strip().lower() in ("1", "true", "yes", "on")

# ── 槽位代理池（多出口并发）────────────────────────────────
# 🔴 这是本项目的**并发杠杆**，而且理由与 Intern-Register-Tool **不同**：
#    那边用池子绕的是 **IP 维度的注册封禁**；hark 侧**没有任何封禁证据**。
#    这里的理由是一条**实测数字**：`sign-in/email` 的限流是 **3 次 / 10s**
#    （见 LOGIN_MAX_PER_WINDOW），而 better-auth 的限流是**按客户端 IP** 计的
#    ⇒ **每个出口各自有一个 3/10s 的窗口**，5 个出口就是 15 次/10s。
#    只靠调大 --workers 是没用的：那是同一张窗口里的三个名额。
#
# 来源优先级：`HR_PROXY_SLOTS_FILE` > `HR_PROXY_SLOTS`。槽位多时用文件。
HR_PROXY_SLOTS = os.getenv("HR_PROXY_SLOTS", "").strip()
HR_PROXY_SLOTS_FILE = os.getenv("HR_PROXY_SLOTS_FILE", "").strip()
# 等一个空闲槽位最多等多久（秒）。全部槽位都被占着时 `acquire()` 会阻塞。
# ⚠ 别设成 0/无穷：0 会让“全忙”瞬间变成一片假失败；无穷会让批量任务永远挂在那里。
PROXY_SLOT_TIMEOUT = _env_float("HR_PROXY_SLOT_TIMEOUT", 240)


def proxy_slots() -> list[str]:
    """读槽位清单。**没配置就返回空列表**（调用方据此退回单代理行为）。

    文件里允许一行一个，也允许逗号分隔；`#` 开头视为注释。

    🔴 注释必须**按行**判定，不能先把全文按逗号切分再逐项看 `#` ——
    后者会让注释行里的 ASCII 逗号切出**不带 `#` 的碎片**，碎片被当成槽位 URL
    （Intern-Register-Tool 实测：一行「# 端口是 7131,7119,7149」凭空产出 3 个
    假槽位，报错还是「端口 7119 的出口 IP 未知」，指向注释里的数字，极难回溯）。
    所以顺序是：先按行 → 丢掉空行/注释行 → 再在行内按逗号切。
    """
    raw = ""
    if HR_PROXY_SLOTS_FILE:
        p = Path(HR_PROXY_SLOTS_FILE)
        if not p.is_file():
            raise ValueError(f"HR_PROXY_SLOTS_FILE 指向的文件不存在：{p}")
        raw = p.read_text(encoding="utf-8")
    elif HR_PROXY_SLOTS:
        raw = HR_PROXY_SLOTS
    out: list[str] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        for item in line.split(","):
            item = item.strip()
            if item and not item.startswith("#"):
                out.append(item)
    return out


def proxies(raw: str | None = None) -> dict | None:
    """把代理串解析成 requests 的 `proxies` 字典；空则返回 `None`。

    支持两种写法：
        host:port:user:pass          → http://user:pass@host:port
        scheme://user:pass@host:port → 原样使用（可指定 socks5）

    `raw` 传 None 时用全局 `HR_PROXY`。
    """
    raw = (HR_PROXY if raw is None else raw).strip()
    if not raw:
        return None
    if "://" not in raw:
        parts = raw.split(":")
        if len(parts) == 4:
            host, port, user, pwd = parts
            raw = f"http://{user}:{pwd}@{host}:{port}"
        elif len(parts) == 2:
            raw = f"http://{raw}"
        else:
            raise ValueError(
                f"代理串格式无法识别：{raw!r}（期望 host:port:user:pass "
                f"或 scheme://user:pass@host:port）"
            )
    return {"http": raw, "https": raw}


def apply_proxy(session, proxy: str | None = None) -> None:
    """把代理挂到 `requests.Session` 上（未配置则什么都不做）。

    🔴 **必须同时关掉 `trust_env`** —— 否则环境变量会**静默盖掉**
    `session.proxies`。机制在 `requests/sessions.py` 的
    `merge_environment_settings`：环境变量先 `setdefault` 进字典，
    而 `merge_setting(proxies, self.proxies)` **不覆盖已存在的键** ——
    于是 `session.proxies` 的优先级**低于**环境变量。

    现象特别隐蔽：请求**成功了**（200），只是**没走你指定的代理** ——
    既不报错也不告警，很容易误判成"代理已生效"。

    ⚠ 未配置代理时**不动 `trust_env`**：那样环境代理仍是默认出口，
      与加这个功能之前的行为一致。
    """
    px = proxies(proxy)
    if not px:
        return
    session.proxies = px
    session.trust_env = False


# ── 启动校验 ──────────────────────────────────────────────────────
def validate() -> list[str]:
    """返回缺失 / 有问题的必需配置项（空列表 = 就绪）。

    刻意**不在 import 时抛错** —— 那样连 `--help` 和离线分析都跑不起来。
    由入口显式调用，报错时直接给出修法。
    """
    missing = []
    if not WORKER_ADMIN_TOKEN:
        missing.append("HR_WORKER_ADMIN_TOKEN")
    if not WORKER_BASE:
        missing.append("HR_WORKER_BASE")
    if not WORKER_DOMAIN:
        missing.append("HR_WORKER_DOMAIN")
    # 槽位文件读不到是配置错误，但用“缺项”的形式报出来比抛 traceback 可读得多。
    try:
        proxy_slots()
    except (ValueError, OSError) as ex:
        missing.append(f"HR_PROXY_SLOTS_FILE（读不到：{ex}）")
    # 生日必须能解析，且年龄 >= 18 —— 否则服务端会在 signup-age 那步
    # 返回 CARD_VERIFICATION_REQUIRED，而那是**每个账号**都要撞一次的失败。
    # 提前到启动阶段报出来，比跑一半才发现便宜得多。
    if not valid_birthday(BIRTHDAY):
        missing.append(
            f"HR_BIRTHDAY（{BIRTHDAY!r} 解析不出，或年龄 < 18 —— "
            f"服务端会要求绑卡；格式 YYYY-MM-DD）"
        )
    return missing


def birthday_age(birthday: str, today=None):
    """`birthday` → 周岁；解析不出返回 `None`。

    ⚠ 只做**本地**判断，服务端自己算它的（实测服务端按当天日期算周岁，
      2008-10-07 在 2026-10-07 被判 18 岁、accepted）。
    """
    import datetime as _dt

    try:
        b = _dt.date.fromisoformat((birthday or "").strip())
    except ValueError:
        return None
    today = today or _dt.date.today()
    years = today.year - b.year - ((today.month, today.day) < (b.month, b.day))
    return years


def valid_birthday(birthday: str) -> bool:
    """本地判据：能解析 + 周岁 >= 18 + 不是未来日期。

    🔴 18 是**实测**出来的门槛（见 `BIRTHDAY` 的说明），不是猜的。
    """
    age = birthday_age(birthday)
    return age is not None and age >= 18
