"""流水线的离线用例 —— **零网络请求**。

做法：`run_batch(mail_factory=…, client_factory=…)` 是**注入点**，测试传假客户端。
所以这里测的是**编排、打标、配额闸门**这三件事，而不是 HTTP。
（HTTP 的映射在 `test_hark_protocol.py`，链接解析在 `test_tempmail.py`。）

🔴 为什么这些用例值得写：整链失败时**唯一**能回答"卡在哪一步"的东西就是
   `stages` / `error_kind` 这两个字段。它们错了，复盘时看到的就是一句
   含糊的 "失败"，而人只能去猜。
"""

import time

import pytest

from common import config
from src import pipeline, quota
from src.hark import CODE_INVALID_EMAIL_OR_PASSWORD, HarkQuota, HarkRejected

LINK = "https://auth.hark.com/api/auth/magic-link/verify?token=t&hark_region=us"


# ── 假实现 ────────────────────────────────────────────────────────
class FakeMail:
    def __init__(
        self, link: str | None = LINK, last_error="", polls=1, address="fake-1@example.com"
    ):
        self.link = link
        self.last_error = last_error
        self.last_polls = polls
        self.address = address
        self.created = 0

    def create_mailbox(self, domain=None, count=1):
        self.created += 1
        return [self.address] if self.link is not None or True else []

    def wait_for_link(self, address, *keywords, **kw):
        return self.link


class FakeClient:
    """既是生产者那侧的 client，也是消费者登录用的 client。"""

    def __init__(
        self,
        *,
        raise_on=None,
        quota_on=None,
        sign_in_code="",
        set_password_code="",
        session_user=True,
        profile_email=True,
    ):
        self.raise_on = raise_on or ()
        self.quota_on = quota_on or ()
        self.sign_in_code = sign_in_code
        self.set_password_code = set_password_code
        self.session_user = session_user
        self.profile_email = profile_email
        self.calls = []
        self.email = ""
        self.password = ""

    def _maybe_raise(self, name):
        if name in self.quota_on:
            raise HarkQuota("HTTP 429 RATE_LIMITED", code="RATE_LIMITED", status=429)
        if name in self.raise_on:
            raise HarkRejected(f"{name} 被拒", code="NOPE", status=400)

    # 生产者侧
    def discover(self, identifier, kind="email"):
        self._maybe_raise("discover")
        self.calls.append("discover")
        return {"exists": False, "hasPassword": False}

    def signup_age(self, identifier, birthday, kind="email"):
        self._maybe_raise("signup_age")
        self.calls.append("signup_age")
        return {"accepted": True}

    def request_magic_link(self, email, callback_url=None):
        self._maybe_raise("request_magic_link")
        self.calls.append("request_magic_link")
        return {"status": True}

    def verify_magic_link(self, url):
        self._maybe_raise("verify_magic_link")
        self.calls.append("verify_magic_link")
        return {"__Secure-hark.session_token": "t.s"}

    # 消费者侧
    def set_password(self, new_password):
        self.calls.append("set_password")
        if self.set_password_code:
            raise HarkRejected("nope", code=self.set_password_code, status=400)
        self.password = new_password
        return {"status": True}

    def sign_in(self, email, password):
        self._maybe_raise("sign_in")
        self.calls.append("sign_in")
        self.email = email
        if self.sign_in_code:
            raise HarkRejected("bad", code=self.sign_in_code, status=401)
        return {"redirect": False, "token": "tok", "user": {"id": "uid-1"}}

    def session_token(self):
        return "tok"

    def region(self):
        return "us"

    def get_session(self):
        self._maybe_raise("get_session")
        if not self.session_user:
            return {}
        return {
            "user": {"id": "uid-1", "region": "us", "hasAppAccess": True},
            "session": {"expiresAt": "2026-11-06T09:49:31.733Z"},
        }

    def profile(self):
        self._maybe_raise("profile")
        return {"email": self.email} if self.profile_email else {}

    def billing_summary(self):
        self._maybe_raise("billing_summary")
        return {
            "plan": {"id": "free"},
            "meters": {"harkTokens": {"dailyLimit": 8_000_000, "poolLimit": 40_000_000}},
        }

    def conversations(self):
        self._maybe_raise("conversations")
        return {"conversations": []}


@pytest.fixture(autouse=True)
def fast_pacing(monkeypatch):
    """把限速闸门全关掉：否则几十个用例会被串行化成一堆纯等待。

    🔴 **两类限速都要关**（登录 + 注册申请），它们都是**按出口**的滑动窗口：
      登录 3 次/10s、注册申请 4 次/60s。不关的话，十几个用例要跑 40s+，
      而那是**测试环境**的问题，不是代码的问题。

    ⚠ 两个限速器都是“首次调用时从 config **懒建**”的，所以要 **clear 字典**
      而不是把某个单例置 None —— 不重置的话，前一个用例建的实例会带着旧参数
      活下来，于是“我改了 config 却没生效”（实测踩过）。
    """
    monkeypatch.setattr(config, "LOGIN_MAX_PER_WINDOW", 1000)
    monkeypatch.setattr(config, "LOGIN_WINDOW_S", 10.0)
    monkeypatch.setattr(config, "LOGIN_RETRIES", 0)
    monkeypatch.setattr(config, "REGISTER_MAX_PER_WINDOW", 1000)
    monkeypatch.setattr(config, "REGISTER_WINDOW_S", 60.0)
    pipeline._LOGIN_LIMITERS.clear()
    pipeline._REGISTER_LIMITERS.clear()
    yield
    pipeline._LOGIN_LIMITERS.clear()
    pipeline._REGISTER_LIMITERS.clear()


def run(count=1, mail=None, client=None, **kw):
    clients = []

    def cf(proxy=None):
        c = client or FakeClient()
        c.proxy = proxy  # 记下这个假客户端被分到哪个出口
        clients.append(c)
        return c

    mails = []

    def mf():
        m = mail or FakeMail()
        mails.append(m)
        return m

    out = pipeline.run_batch(
        count=count,
        workers=kw.pop("workers", 2),
        producers=kw.pop("producers", 2),
        verbose=False,
        mail_factory=mf,
        client_factory=cf,
        **kw,
    )
    return out, mails, clients


# ── 顺链 ──────────────────────────────────────────────────────────
def test_happy_path_marks_every_stage():
    out, _, _ = run()
    rec = out[0]
    assert rec.status == "success" and rec.error_kind == ""
    assert all(rec.stages[s].startswith("ok") for s in pipeline.STAGES)
    assert rec.email == "fake-1@example.com"
    assert rec.password and len(rec.password) >= 8
    assert rec.user_id == "uid-1"
    assert rec.session_token == "tok"
    assert rec.region == "us"
    assert rec.plan == "free"
    assert rec.daily_limit == 8_000_000
    assert rec.has_app_access is True
    assert rec.stages["activate"] == "ok(mail+verify)"
    # 时间分解必须齐：`activate_ms` 是总，`mail_wait_ms`/`verify_ms` 是子项。
    for key in (
        "mailbox_ms",
        "register_ms",
        "activate_ms",
        "mail_wait_ms",
        "verify_ms",
        "password_ms",
        "login_ms",
        "app_ms",
    ):
        assert key in rec.timings, key


def test_password_is_generated_before_the_mailbox_exists():
    """🔴 密码在**生产者**侧生成 —— "邮箱建出来了但密码是空的"是最坏的中间态。"""
    out, _, _ = run()
    assert out[0].password


def test_password_meets_server_policy():
    for _ in range(50):
        pw = pipeline.make_password()
        assert len(pw) >= 8
        assert any(c.islower() for c in pw)
        assert any(c.isupper() for c in pw)
        assert any(c.isdigit() for c in pw)
        assert any(not c.isalnum() for c in pw)
    # 不能总是同一个（洗牌 + secrets）
    assert len({pipeline.make_password() for _ in range(20)}) == 20


# ── 失败路径的打标 ────────────────────────────────────────────────
def test_mail_timeout_marks_activate_and_no_password_set():
    out, _, clients = run(mail=FakeMail(link=None))
    rec = out[0]
    assert rec.status == "failed" and rec.error_kind == "rejected"
    assert rec.stages["activate"] == "failed"
    assert rec.stages["password"] == ""  # 后面没跑到
    assert "set_password" not in clients[0].calls
    assert "magic link" in rec.error


def test_mail_read_error_says_so_explicitly():
    """ "邮件没到"与"读不出来"的修法完全不同 —— 文案必须分开。"""
    out, _, _ = run(mail=FakeMail(link=None, last_error="邮箱 Worker 持续读不出来（3 次）"))
    assert "读不出来" in out[0].error


def test_password_already_set_is_treated_as_ok_but_annotated():
    """🔴 set-password 是一次性的：重试必然撞 PASSWORD_ALREADY_SET。
    判失败会把一个**已完成**的步骤报成坏的；悄悄当成功又会让台账里
    那一列可能是别人设的密码。所以：当成功，但标注出来。"""
    out, _, _ = run(client=FakeClient(set_password_code="PASSWORD_ALREADY_SET"))
    rec = out[0]
    assert rec.status == "success"
    assert "已存在" in rec.stages["password"]


def test_sign_in_rejection_marks_login():
    out, _, _ = run(client=FakeClient(sign_in_code=CODE_INVALID_EMAIL_OR_PASSWORD))
    rec = out[0]
    assert rec.status == "failed" and rec.error_kind == "rejected"
    assert rec.stages["login"] == "failed"
    assert rec.stages["app"] == ""


def test_missing_session_user_marks_app():
    out, _, _ = run(client=FakeClient(session_user=False))
    rec = out[0]
    assert rec.status == "failed" and rec.error_kind == "rejected"
    assert rec.stages["app"] == "failed"


def test_profile_without_email_marks_app():
    out, _, _ = run(client=FakeClient(profile_email=False))
    assert out[0].stages["app"] == "failed"


def test_rejected_in_producer_marks_register_stage():
    out, _, _ = run(client=FakeClient(raise_on=("signup_age",)))
    rec = out[0]
    assert rec.error_kind == "rejected"
    assert rec.stages["mailbox"].startswith("ok")
    assert rec.stages["register"] == "failed"
    assert rec.stages["activate"] == ""


# ── 服务端配额 → 停整批 ───────────────────────────────────────────
def test_server_quota_is_tagged_quota():
    out, _, _ = run(client=FakeClient(quota_on=("discover",)))
    assert out[0].error_kind == "quota"


def test_consecutive_server_quota_stops_the_rest_without_requests(monkeypatch):
    """连续两次配额拒绝 ⇒ 剩余任务标 `quota_guard`（**一个请求都没发**）。

    两者必须分开：`quota` = 服务端说的；`quota_guard` = 我们主动停的。
    混成一个值之后，报告里就分不清"被封了几个"和"有几个没敢试"。
    """
    out, _, clients = run(
        count=4, producers=1, workers=1, client=FakeClient(quota_on=("discover",))
    )
    kinds = [r.error_kind for r in out]
    assert kinds.count("quota") == 2, kinds
    assert kinds.count("quota_guard") == 2, kinds
    # 后两个不该发过任何请求
    assert len(clients) == 2
    skipped = [r for r in out if r.error_kind == "quota_guard"]
    assert all(r.status == "skipped" for r in skipped)
    assert all(r.stages["mailbox"] == "skipped" for r in skipped)


# ── 本地闸门 ──────────────────────────────────────────────────────
def test_local_quota_guard_sends_no_request(monkeypatch):
    monkeypatch.setattr(config, "REG_QUOTA_MAX", 1)
    quota.record("already@example.com", ok=True)
    out, mails, clients = run(count=1)
    assert out[0].status == "skipped" and out[0].error_kind == "quota_guard"
    assert mails == [] and clients == [], "被本地闸门拦下时不该建邮箱、也不该建客户端"


def test_ignore_quota_bypasses_the_gate(monkeypatch):
    monkeypatch.setattr(config, "REG_QUOTA_MAX", 1)
    quota.record("already@example.com", ok=True)
    out, _, _ = run(count=1, ignore_quota=True)
    assert out[0].status == "success"


def test_success_is_recorded_in_quota_state():
    run(count=1)
    assert quota.status().used == 1


def test_failure_does_not_consume_quota():
    run(mail=FakeMail(link=None))
    st = quota.status()
    assert st.used == 0 and st.attempts == 1


# ── error_kind 读点 ───────────────────────────────────────────────
def test_error_kind_field_is_authoritative_over_text():
    """🔴 我们自己拼的文案里也会出现服务端的词（把响应体原文附进去），
    所以**字段非空即权威**，文本只给手工构造的老记录兜底。"""
    rec = pipeline.AccountRecord()
    rec.error = "quota guard: 已确认 429"
    rec.error_kind = "network"
    assert pipeline.error_kind_of(rec) == "network"


def test_error_kind_text_fallback():
    rec = pipeline.AccountRecord()
    rec.error = "本地配额已达上限"
    assert pipeline.error_kind_of(rec) == "quota_guard"
    rec2 = pipeline.AccountRecord()
    rec2.error = "HTTP 429"
    assert pipeline.error_kind_of(rec2) == "quota"


def test_error_kind_of_dict_records():
    """台账记录是 dict，本批记录是对象 —— 读点两边都要能吃。"""
    assert pipeline.error_kind_of({"error_kind": "mail"}) == "mail"
    assert pipeline.error_kind_of({"error": "HTTP 429"}) == "quota"


def test_classify_maps_exceptions():
    assert pipeline.classify(HarkQuota("x")) == "quota"
    assert pipeline.classify(HarkRejected("x")) == "rejected"
    assert pipeline.classify(quota.QuotaExceeded("x")) == "quota_guard"
    assert pipeline.classify(RuntimeError("x")) == "network"


# ── 记录序列化 ────────────────────────────────────────────────────
def test_record_json_round_trip_and_field_order():
    import json

    rec = pipeline.AccountRecord(email="a@example.com")
    rec.done()
    d = json.loads(rec.to_json())
    assert list(d) == list(rec.as_dict())
    assert list(d)[:3] == ["email", "password", "user_id"]
    assert set(d["stages"]) == set(pipeline.STAGES), "每个阶段都要有键（哪怕是空串）"


def test_skip_marks_every_stage_skipped():
    rec = pipeline.AccountRecord()
    rec.skip("quota_guard", "本地配额")
    assert rec.status == "skipped"
    assert all(rec.stages[s] == "skipped" for s in pipeline.STAGES)


def test_failed_stage_points_at_first_empty_stage():
    rec = pipeline.AccountRecord()
    rec.mark_ok("mailbox")
    rec.stages["register"] = "exists=False"  # 中间态也算"有值"
    rec.mark_ok("register")
    assert pipeline._failed_stage(rec) == "activate"


def test_failed_stage_uses_the_cursor_not_a_guess():
    """🔴 游标（`enter`）才是权威。靠"第一个空阶段"猜会把"校验链接失败"
    报成"设密码失败" —— 一个**错误结论**比"不知道"坏得多。"""
    rec = pipeline.AccountRecord()
    rec.mark_ok("mailbox")
    rec.mark_ok("register")
    rec.enter("activate")  # 死在这一步
    assert pipeline._failed_stage(rec) == "activate"


# ── 登录限流（实测 3 次 / 10s）────────────────────────────────────
def test_sliding_window_allows_burst_then_blocks():
    w = pipeline.SlidingWindow(max_events=2, window_s=0.4)
    t0 = time.time()
    assert w.acquire() == 0.0
    assert w.acquire() == 0.0
    waited = w.acquire()  # 第 3 次必须等第一个滑出窗口
    elapsed = time.time() - t0
    assert waited > 0
    assert elapsed >= 0.35, elapsed


def test_sliding_window_is_not_a_fixed_interval():
    """🔴 用滑动窗口而不是"每 N 秒一次"：平台限的是"窗口内最多 3 次"，
    固定间隔在请求比预期快时会把第 4 次挤进窗口而撞 429。"""
    w = pipeline.SlidingWindow(max_events=3, window_s=0.5)
    t0 = time.time()
    for _ in range(3):
        w.acquire()
    assert time.time() - t0 < 0.1, "前 3 次应当立即通过（不排队）"


def test_login_limiter_is_a_process_singleton():
    """同一个出口必须拿到**同一个**限速器（否则每个线程一份 = 没限速）。"""
    pipeline._LOGIN_LIMITERS.clear()
    assert pipeline.login_limiter() is pipeline.login_limiter()
    assert pipeline.login_limiter("http://a:1") is pipeline.login_limiter("http://a:1")


def test_login_limiter_is_bucketed_per_slot():
    """🔴 限流是按**出口**的（实测 3 次/10s），所以限速器必须按出口分桶 ——
    共用一个全局窗口的话，加出口一点用都没有。"""
    pipeline._LOGIN_LIMITERS.clear()
    a = pipeline.login_limiter("http://a:1")
    b = pipeline.login_limiter("http://b:2")
    assert a is not b, "不同出口必须各有自己的窗口"
    # 两个窗口各自独立：占满 a 不该影响 b
    a._times = [time.time()] * 10
    assert pipeline.login_limiter("http://b:2").acquire() == 0.0


def test_register_limiter_is_bucketed_per_slot():
    """注册申请侧同理 —— 而这一条是 2026-10-08 **实测**出来的：
    注册侧的 429 也是按 IP 的（换出口立刻通过、换域名仍 429）。"""
    pipeline._REGISTER_LIMITERS.clear()
    a = pipeline.register_limiter("http://a:1")
    b = pipeline.register_limiter("http://b:2")
    assert a is not b
    assert a is pipeline.register_limiter("http://a:1")
    a._times = [time.time()] * 10
    assert pipeline.register_limiter("http://b:2").acquire() == 0.0


def test_register_and_login_limiters_are_independent():
    """两类限流是**两回事**（不同端点、不同额度），不能共用一张窗口。"""
    pipeline._LOGIN_LIMITERS.clear()
    pipeline._REGISTER_LIMITERS.clear()
    assert pipeline.register_limiter("s") is not pipeline.login_limiter("s")


def test_accounts_are_spread_across_slots_and_recorded():
    """🔴 台账里的 `proxy` 字段必须真的能回答"这个账号走的哪个出口"。"""
    from collections import Counter

    from src.proxypool import SlotPool

    slots = ["http://127.0.0.1:7901", "http://127.0.0.1:7902"]
    pool = SlotPool(slots, timeout=5)
    out, _, clients = run(count=2, workers=1, producers=1, slot_pool=pool)
    assert sorted(r.proxy for r in out) == sorted(slots)
    # ⚠ `clients` 里有 **4** 个：每个账号两个（生产者一个、消费者登录一个）。
    #   它们的出口必须**成对相同** —— 登录限流是按出口的，消费者若在别人的窗口里
    #   排队，池子就白加了。所以判据是“每个出口恰好出现两次”，不是“集合相等”。
    assert len(clients) == 4, f"2 个账号应当各建 2 个客户端，实际 {len(clients)}"
    assert Counter(c.proxy for c in clients) == Counter({slots[0]: 2, slots[1]: 2}), Counter(
        c.proxy for c in clients
    )
    st = {s.url: s for s in pool.stats()}
    assert all(st[u].ok == 1 for u in slots), "两个出口都该记上一次成功"


def test_slot_is_released_when_the_producer_fails():
    """🔴 生产者失败也必须归还出口 —— 漏一个，池子就会逐渐"少一个"，
    最后表现成"批量突然超时"，而不是"池子漏了"。"""
    from src.proxypool import SlotPool

    pool = SlotPool(["http://127.0.0.1:7901"], timeout=0.3)
    out, _, _ = run(count=1, mail=FakeMail(link=None), slot_pool=pool)
    assert out[0].status == "failed"
    assert pool.acquire() == "http://127.0.0.1:7901"  # 能再借到 = 已归还


def test_rejected_failure_does_not_blame_the_slot():
    """把"年龄门被拒"记到出口头上，会让每个出口的失败数变成噪音。"""
    from src.proxypool import SlotPool

    pool = SlotPool(["http://127.0.0.1:7901"], timeout=5)
    out, _, _ = run(count=1, client=FakeClient(raise_on=("signup_age",)), slot_pool=pool)
    assert out[0].error_kind == "rejected"
    st = pool.stats()[0]
    assert st.failed == 0 and st.used == 1, (st.failed, st.used)


def test_network_failure_is_charged_to_the_slot():
    from src.hark import HarkNetwork
    from src.proxypool import SlotPool

    class Net(FakeClient):
        def discover(self, identifier, kind="email"):
            raise HarkNetwork("HTTP 502")

    pool = SlotPool(["http://127.0.0.1:7901"], timeout=5)
    out, _, _ = run(count=1, client=Net(), slot_pool=pool)
    assert out[0].error_kind == "network"
    assert pool.stats()[0].failed == 1


def test_slot_pool_exhaustion_is_a_network_failure_not_a_crash():
    """借不到出口是**局部**问题（这个账号失败），不该把整批停掉。"""
    from src.proxypool import SlotPool

    pool = SlotPool(["http://127.0.0.1:7901"], timeout=0.2)
    held = pool.acquire()  # 先占住，让后续 acquire 超时
    out, _, _ = run(count=1, slot_pool=pool)
    pool.release(held)
    assert out[0].status == "failed" and out[0].error_kind == "network"
    assert "代理槽位池" in out[0].error


def test_login_429_is_retried_not_treated_as_dead(monkeypatch):
    """🔴 这条钉的是一个**假阴性**：登录的 429 是自愈的（窗口滑出就好），
    把它读成"账号坏了"会让复核工具报"你的账号全挂了"，而真实情况只是
    我们在打自己。实测：`sign-in/email` 限流 3 次 / 10s。"""
    monkeypatch.setattr(config, "LOGIN_WINDOW_S", 0.01)
    monkeypatch.setattr(config, "LOGIN_MAX_PER_WINDOW", 10)
    monkeypatch.setattr(config, "LOGIN_RETRIES", 2)
    pipeline._LOGIN_LIMITERS.clear()

    class Flaky(FakeClient):
        def __init__(self):
            super().__init__()
            self.n = 0

        def sign_in(self, email, password):
            self.n += 1
            if self.n == 1:
                raise HarkQuota("HTTP 429 RATE_LIMITED", code="RATE_LIMITED", status=429)
            return super().sign_in(email, password)

    out, _, _ = run(client=Flaky())
    rec = out[0]
    assert rec.status == "success", rec.error
    assert "login_wait_ms" in rec.timings


def test_login_429_rotates_to_another_exit(monkeypatch):
    """🔴 撞 429 应当**换出口**，而不是在原地等。

    实测依据（2026-10-08）：登录侧的 429 里有一种是 **CF 边缘限流**，
    在**同一个出口**上退避重试 4 次（跨 ~17s）仍然 429，而换个出口立刻过。
    所以重试的第一步必须是换出口。
    """
    from src.proxypool import SlotPool

    monkeypatch.setattr(config, "LOGIN_WINDOW_S", 0.01)
    monkeypatch.setattr(config, "LOGIN_MAX_PER_WINDOW", 10)
    monkeypatch.setattr(config, "LOGIN_RETRIES", 2)
    pipeline._LOGIN_LIMITERS.clear()

    slots = ["http://a:1", "http://b:2"]
    pool = SlotPool(slots, timeout=2)

    class QuotaOnFirstExit(FakeClient):
        """在第一个出口上登录必 429，别的出口正常。"""

        def sign_in(self, email, password):
            if self.proxy == slots[0]:
                raise HarkQuota("HTTP 429 RATE_LIMITED", code="RATE_LIMITED", status=429)
            return super().sign_in(email, password)

    out, _, _ = run(count=1, workers=1, producers=1, client=QuotaOnFirstExit(), slot_pool=pool)
    rec = out[0]
    assert rec.status == "success", rec.error
    # `proxy` 是**最终**出口（会话建在那里），`proxy_rotated` 记下换过的路径。
    assert rec.proxy == slots[1], f"应当换到另一个出口，实际 {rec.proxy}"
    assert slots[0] in rec.proxy_rotated and slots[1] in rec.proxy_rotated, rec.proxy_rotated
    st = {s.url: s for s in pool.stats()}
    assert st[slots[0]].failed == 1, "被 429 的出口要按失败记账"
    assert st[slots[1]].ok == 1


def test_proxy_rotated_is_empty_when_no_rotation():
    """没换过出口时那个字段应当是空串（而不是写一句“没换”）。"""
    out, _, _ = run(count=1)
    assert out[0].proxy_rotated == ""


def test_rotation_is_skipped_with_a_single_exit(monkeypatch):
    """池子里只有一个出口时换不动 —— 应当**退回退避重试**，而不是死循环。"""
    from src.proxypool import SlotPool

    monkeypatch.setattr(config, "LOGIN_WINDOW_S", 0.02)
    monkeypatch.setattr(config, "LOGIN_MAX_PER_WINDOW", 100)
    monkeypatch.setattr(config, "LOGIN_RETRIES", 1)
    pipeline._LOGIN_LIMITERS.clear()

    pool = SlotPool(["http://only:1"], timeout=1)

    class QuotaOnce(FakeClient):
        def __init__(self):
            super().__init__()
            self.n = 0

        def sign_in(self, email, password):
            self.n += 1
            if self.n == 1:
                raise HarkQuota("HTTP 429", code="RATE_LIMITED", status=429)
            return super().sign_in(email, password)

    out, _, _ = run(count=1, client=QuotaOnce(), slot_pool=pool)
    assert out[0].status == "success", out[0].error
    assert out[0].proxy == "http://only:1"


def test_login_429_gives_up_after_the_cap(monkeypatch):
    """「429 会自己好」只对**限流**成立。若平台真封了，无限重试就是无限白打枪。"""
    monkeypatch.setattr(config, "LOGIN_WINDOW_S", 0.01)
    monkeypatch.setattr(config, "LOGIN_MAX_PER_WINDOW", 10)
    monkeypatch.setattr(config, "LOGIN_RETRIES", 1)
    pipeline._LOGIN_LIMITERS.clear()

    class AlwaysLimited(FakeClient):
        def sign_in(self, email, password):
            raise HarkQuota("HTTP 429 RATE_LIMITED", code="RATE_LIMITED", status=429)

    out, _, _ = run(client=AlwaysLimited())
    rec = out[0]
    assert rec.status == "failed"
    assert rec.error_kind == "quota"
    assert rec.stages["login"] == "failed"
    assert "持续被限流" in rec.error
