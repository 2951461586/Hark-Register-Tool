"""`src/tempmail.py` 的离线用例 —— **零网络请求**。

这里钉的是"链接提取"这一件事。它看着琐碎，但它是整条链路**唯一**的解析步骤：
hark 的邮件 `extracted_json` 实测是 `[]`，所以 magic link 完全靠我们自己从正文里
抽。抽错一个字符（多一个 `)`、`&` 没反转义）的后果是**每个账号都失败**，
而且报错会指向"服务端拒绝链接"，看起来像服务端的问题。
"""

import json

from src.tempmail import Mail, _as_int, extract_links, now_ms

# 真实邮件的正文形状（实测 `body` 字段，链接出现两次：一次在 Markdown 括号里、
# 一次是裸 URL）。**逐字复刻**，不简化 —— 简化过的样本测不出"尾部标点"那类 bug。
REAL_BODY = """Use this link to sign in to your Hark account

Sign in to Hark

Use the button below to sign in to Hark. The link expires in 24 hours.

Sign in to Hark (https://auth.hark.com/api/auth/magic-link/verify?token=ZbUFkEMpONTYoembPYyzCzLFWXUdAtEf&callbackURL=https%3A%2F%2Fhark.com%2Fset-password&errorCallbackURL=https%3A%2F%2Fhark.com%2Flogin&hark_region=us)

Is the button not working? Copy and paste this URL into your browser:

https://auth.hark.com/api/auth/magic-link/verify?token=ZbUFkEMpONTYoembPYyzCzLFWXUdAtEf&callbackURL=https%3A%2F%2Fhark.com%2Fset-password&errorCallbackURL=https%3A%2F%2Fhark.com%2Flogin&hark_region=us

If you did not ask to sign in to Hark, you can safely ignore this email.
"""

LINK = (
    "https://auth.hark.com/api/auth/magic-link/verify?token=ZbUFkEMpONTYoembPYyzCzLFWXUdAtEf"
    "&callbackURL=https%3A%2F%2Fhark.com%2Fset-password"
    "&errorCallbackURL=https%3A%2F%2Fhark.com%2Flogin&hark_region=us"
)


def _mail(body="", html="", extracted="[]"):
    return Mail(
        id="1",
        to_address="a@example.com",
        from_address="0101@mailer.hark.com",
        subject="Your Hark sign-in link",
        body=body,
        extracted_json=extracted,
        received_at=now_ms(),
        raw_html=html,
        body_links=extract_links(f"{body}\n{html}"),
    )


def test_extract_links_real_body_dedupes_and_keeps_query():
    links = extract_links(REAL_BODY)
    assert links == [LINK], links
    # 🔴 query 必须**完整**：少一个参数服务端会 302 到错误页而不是发会话。
    assert "token=" in links[0] and "callbackURL=" in links[0] and "hark_region=us" in links[0]


def test_extract_links_unescapes_html_and_trims_punctuation():
    html = (
        '<a href="https://auth.hark.com/api/auth/magic-link/verify?token=t&amp;'
        'callbackURL=x">Sign in</a>.'
    )
    assert extract_links(html) == [
        "https://auth.hark.com/api/auth/magic-link/verify?token=t&callbackURL=x"
    ]


def test_extract_links_trims_chinese_punctuation():
    # 中文标点粘在尾部：`。` / `）` 都要剪掉，否则 token 尾巴上多一个字符。
    assert extract_links("见 https://hark.com/x?t=1。") == ["https://hark.com/x?t=1"]
    assert extract_links("（https://hark.com/x?t=1）") == ["https://hark.com/x?t=1"]


def test_mail_links_merges_worker_extraction_and_body():
    """两条路都要走：Worker 结构化结果优先，正文兜底，且**不重复**。"""
    m = _mail(body=REAL_BODY, extracted=json.dumps([{"value": "https://hark.com/other"}]))
    assert m.links[0] == "https://hark.com/other"
    assert LINK in m.links
    assert len(m.links) == len(set(m.links))


def test_mail_links_tolerates_broken_extracted_json():
    """Worker 的 `extracted_json` 坏了不该让 `links` 抛 —— 正文那条路还在。"""
    m = _mail(body=REAL_BODY, extracted="{not json")
    assert m.find_link("magic-link/verify") == LINK


def test_find_link_returns_none_when_no_match():
    m = _mail(body="nothing here")
    assert m.find_link("magic-link/verify") is None
    assert m.links == []


def test_as_int_is_tolerant():
    """`received_at` 来自**我们并不拥有**的 Worker，形态随时可能变。"""
    assert _as_int(1791375033474) == 1791375033474
    assert _as_int("1791375033474") == 1791375033474
    assert _as_int(None) == 0
    assert _as_int("2026-10-07T09:49:31Z") == 0
    assert _as_int(True) == 0  # bool 是 int 的子类，但这里它没有时间含义


def test_now_ms_is_milliseconds():
    """单位必须与 Worker 的 `received_at` 一致（毫秒）—— 差 1000 倍时过滤会全失效。"""
    v = now_ms()
    assert v > 1e12  # 秒级时间戳约 1.79e9；毫秒级约 1.79e12
