"""`src/hark.py` 的离线用例 —— **零网络请求**。

做法：把 `HarkClient.session.request` 换成一个返回**构造好的假响应**的函数。
这样测的是"状态码 + 响应体 → 异常类型"的映射本身，而不是网络。
"""

import pytest

from src.hark import (
    CODE_PASSWORD_ALREADY_SET,
    HarkClient,
    HarkNetwork,
    HarkQuota,
    HarkRejected,
    _short,
)


class FakeResp:
    def __init__(self, status=200, body=None, text="", headers=None):
        self.status_code = status
        self._body = body
        self.text = text or ("" if body is None else str(body))
        self.headers = headers or {}

    def json(self):
        if self._body is None:
            raise ValueError("not json")
        return self._body


def client_with(responses, calls=None):
    """造一个 `HarkClient`，它的 HTTP 层被换成**按顺序**返回 `responses`。"""
    c = HarkClient()
    it = iter(responses)

    def fake_request(*args, **kw):
        # 用 `*args` 而不是 `(method, url, **kw)`：`requests.Session.request`
        # 有一长串关键字参数，签名窄了会被静态检查判成“不兼容的赋值”
        # （运行时其实无所谓，但一个永远红的告警会把真问题淹掉）。
        method, url = (args + (None, None))[:2]
        if calls is not None:
            calls.append({"method": method, "url": url, **kw})
        return next(it)

    c.session.request = fake_request
    return c


# ── URL 拼接 ──────────────────────────────────────────────────────
def test_absolute_url_is_not_prefixed():
    """🔴 这条钉的是一个真实踩过的 bug：`verify_magic_link` 传的是**完整 URL**，
    而 `_url()` 会再拼一次 base ⇒ `hark.comhttps://auth.hark.com/...`。
    requests 报的是 `host='hark.comhttps'`，一眼看不出是谁拼错的。"""
    c = HarkClient()
    assert c._url("https://auth.hark.com/api/auth/magic-link/verify?t=1") == (
        "https://auth.hark.com/api/auth/magic-link/verify?t=1"
    )
    assert c._url("/api/profile") == "https://hark.com/api/profile"
    assert (
        c._url("/api/auth/get-session", auth=True) == "https://auth.hark.com/api/auth/get-session"
    )


# ── 错误映射 ──────────────────────────────────────────────────────
def test_429_maps_to_quota():
    c = client_with([FakeResp(429, {"code": "RATE_LIMITED", "message": "slow down"})])
    with pytest.raises(HarkQuota) as ex:
        c.discover("a@example.com")
    assert ex.value.kind == "quota"
    assert ex.value.status == 429


def test_quota_business_code_maps_to_quota_even_with_200():
    """配额类业务码**不一定**配 429 —— 实测平台习惯把业务码放 body 里。"""
    c = client_with([FakeResp(200, {"code": "QUOTA_EXCEEDED"})])
    with pytest.raises(HarkQuota):
        c.profile()


def test_4xx_maps_to_rejected_and_keeps_code():
    c = client_with([FakeResp(400, {"code": CODE_PASSWORD_ALREADY_SET, "message": "already"})])
    with pytest.raises(HarkRejected) as ex:
        c.set_password("x" * 12)
    assert ex.value.kind == "rejected"
    assert ex.value.code == CODE_PASSWORD_ALREADY_SET


def test_5xx_maps_to_network():
    c = client_with([FakeResp(502, None, text="<html>bad gateway</html>")])
    with pytest.raises(HarkNetwork):
        c.get_session()


def test_transport_error_maps_to_network():
    import requests

    c = HarkClient()

    def boom(*a, **kw):
        raise requests.Timeout("timed out")

    c.session.request = boom
    with pytest.raises(HarkNetwork):
        c.profile()


def test_non_json_error_body_is_shortened():
    """CF 边缘的错误页是**整页 HTML**。原样塞进台账会让 `error` 变成几 KB。"""
    long_html = "<html>" + ("x" * 5000) + "</html>"
    c = client_with([FakeResp(403, None, text=long_html)])
    with pytest.raises(HarkRejected) as ex:
        c.profile()
    assert len(ex.value.args[0]) < 400


def test_short_prefers_json_message():
    assert _short({"message": "boom", "code": "X"}, FakeResp(400)) == "boom"
    assert "code" in _short({"code": "X"}, FakeResp(400))


def test_short_says_so_when_body_is_empty():
    """hark 的 5xx 实测是 `text/plain` + **空 body**。不处理的话错误文案退化成
    `HTTP 500：`（冒号后什么都没有），而人看到会以为是**我们**没填信息。"""
    out = _short(None, FakeResp(500, None, text="", headers={"Content-Type": "text/plain"}))
    assert "空响应体" in out


# ── 重试：只给幂等的写请求 ──────────────────────────────────────
def test_idempotent_write_retries_once_on_5xx():
    """实测：hark 的 magic-link 端点会**整段时间**返回 500。
    一次重试抹掉瞬时抖动；上限就是 1（把数字翻倍没有意义）。"""
    calls = []
    c = client_with([FakeResp(500, None, text=""), FakeResp(200, {"status": True})], calls)
    assert c.request_magic_link("a@example.com") == {"status": True}
    assert len(calls) == 2, "应当重试一次"


def test_retry_gives_up_after_one_extra_attempt():
    calls = []
    c = client_with([FakeResp(500, None, text=""), FakeResp(500, None, text="")], calls)
    with pytest.raises(HarkNetwork):
        c.request_magic_link("a@example.com")
    assert len(calls) == 2, "总共只该打两次"


def test_non_idempotent_calls_do_not_retry():
    """`set_password` / `sign_in` / `verify_magic_link` **一律不重试**：
    前两个是一次性的，而“第一次到底成没成”在 5xx 时是**未知**的。"""
    calls = []
    c = client_with([FakeResp(500, None, text="")], calls)
    with pytest.raises(HarkNetwork):
        c.set_password("x" * 12)
    assert len(calls) == 1


def test_quota_is_never_retried():
    """配额触顶时重试只是白打枪（而且每次都加深限流）。"""
    calls = []
    c = client_with([FakeResp(429, {"code": "RATE_LIMITED"})], calls)
    with pytest.raises(HarkQuota):
        c.request_magic_link("a@example.com")
    assert len(calls) == 1


# ── 会话凭据 ──────────────────────────────────────────────────────
def test_session_token_is_first_half_of_cookie():
    """cookie 是 `<token>.<hmac>`；台账里存的是前段（与 get-session 的
    `session.token` 实测相等）。"""
    c = HarkClient()
    c.session.cookies.set("__Secure-hark.session_token", "abc123.SIGNATURE%3D", domain="hark.com")
    c.session.cookies.set("__Secure-hark.region", "us", domain="hark.com")
    assert c.session_token() == "abc123"
    assert c.region() == "us"


def test_session_token_empty_when_not_logged_in():
    assert HarkClient().session_token() == ""


# ── 链接校验的防御 ────────────────────────────────────────────────
def test_verify_rejects_foreign_host():
    """🔴 防御：不能把任意 URL 塞进来 —— 那会把会话 cookie 打到第三方去。"""
    c = HarkClient()
    with pytest.raises(HarkRejected) as ex:
        c.verify_magic_link("https://evil.example.com/api/auth/magic-link/verify?token=1")
    assert ex.value.code == "BAD_LINK"


def test_verify_reports_when_no_session_cookie():
    c = client_with([FakeResp(200, {}, text="ok")])
    with pytest.raises(HarkRejected) as ex:
        c.verify_magic_link("https://auth.hark.com/api/auth/magic-link/verify?token=1")
    assert ex.value.code == "MAGIC_LINK_REJECTED"


def test_verify_does_not_follow_redirect():
    """`302` 的 Location 是前端页面（几百 KB HTML + JS chunk）—— 跟过去没有
    信息增量，只是白打一次站点。会话在 `Set-Cookie` 里。"""
    calls = []
    c = client_with(
        [FakeResp(302, {}, headers={"Location": "https://hark.com/set-password"})], calls
    )
    c.session.cookies.set("__Secure-hark.session_token", "t.s", domain="hark.com")
    c.verify_magic_link("https://auth.hark.com/api/auth/magic-link/verify?token=1")
    assert calls[0]["allow_redirects"] is False


# ── 限速闸门 ──────────────────────────────────────────────────────
def test_paced_sleep_is_gone():
    """🔴 断言那个“全局最小间隔”机制**真的被删干净了**。

    2026-10-08 实测证明：注册侧的 429 是**按 IP**、且只限 `magic-link` 端点，
    所以“每进程全局最小间隔”两头都不对（既挡不住单出口超限，又白把小批量
    串行化）。它被 `pipeline.register_limiter(slot)` 取代。

    留一个测已删函数的用例，只会让人以为那个机制还在 —— 所以这里反过来钉：
    **它不应该再存在**。
    """
    import src.hark as hark

    assert not hasattr(hark, "paced_sleep")
    assert not hasattr(hark, "wait_interval_default")
