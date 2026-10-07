"""收尾报告的**契约**用例 —— 零网络。

这块 stdout 是**逐字节契约**：人靠它判断"这批跑得怎么样"。所以它值得被钉住，
而不是"改完看一眼还行"。

⚠ 报告层只依赖 stdlib（`quota` / `config` / `error_kind_of` 全部注入），
  这一点由 `test_dependency_surface.py` 单独钉住。
"""

from src import report
from src.pipeline import STAGES, AccountRecord, error_kind_of


def make_rec(email, status="success", **kw):
    r = AccountRecord(email=email)
    r.created_at = "2026-01-01 00:00:00"
    for s in STAGES:
        r.stages[s] = "ok"
    r.timings = {
        "mailbox_ms": 100,
        "register_ms": 900,
        "activate_ms": 1500,
        "mail_wait_ms": 800,
        "verify_ms": 700,
        "password_ms": 200,
        "login_ms": 400,
        "app_ms": 500,
    }
    r.plan = "free"
    r.daily_limit = 8_000_000
    r.has_app_access = True
    if status == "success":
        r.done()
    else:
        r.fail(kw.get("kind", "rejected"), kw.get("error", "boom"), stage="login")
    return r


def render(records, **kw):
    lines = []
    report.render_batch_report(
        records,
        kw.pop("written", None),
        kw.pop("wall", 12.0),
        stages=STAGES,
        error_kind_of=error_kind_of,
        log=lines.append,
        **kw,
    )
    return "\n".join(lines)


def test_summary_line_has_counts_wallclock_and_per_account():
    out = render([make_rec("a@example.com"), make_rec("b@example.com")], workers=4)
    assert "成功 2 / 2" in out
    assert "墙钟 12.0s" in out
    assert "4 路并发 → 6.00s/账号" in out


def test_failure_line_carries_error_kind():
    out = render([make_rec("a@example.com", "failed", kind="quota", error="HTTP 429")])
    assert "[quota]" in out
    assert "HTTP 429" in out


def test_stage_table_lists_every_stage_with_a_sample():
    out = render([make_rec("a@example.com")])
    for s in STAGES:
        assert s in out, s
    # 等信是整链最大单项，必须单独拆出来，否则会被读成"校验慢"
    assert "其中等信" in out


def test_slowest_account_is_printed_not_the_first():
    """批量总时长由**最慢**那个决定，不是均值 —— 打印第一个会给出错误印象。"""
    fast = make_rec("fast@example.com")
    slow = make_rec("slow@example.com")
    slow.timings["activate_ms"] = 60_000
    slow.timings["mail_wait_ms"] = 59_000
    out = render([fast, slow])
    idx_slowest = out.index("最慢账号")
    assert "slow@example.com" in out[idx_slowest:]


def test_error_histogram_sorted_by_count():
    recs = [
        make_rec("a@example.com", "failed", kind="rejected"),
        make_rec("b@example.com", "failed", kind="rejected"),
        make_rec("c@example.com", "failed", kind="network"),
    ]
    out = render(recs)
    assert out.index("rejected") < out.index("network")


def test_has_app_access_false_is_flagged():
    """🔴 "注册成功但准入被拒"是最该防的假绿，必须单独标出来。"""
    rec = make_rec("a@example.com")
    rec.has_app_access = False
    assert "hasAppAccess=false" in render([rec])


def test_email_is_redacted_but_domain_kept():
    out = render(
        [make_rec("oai-1234567890abcdef@example.com")],
        redact_email=lambda s: "oai…@" + s.split("@")[-1],
    )
    assert "oai-1234567890abcdef" not in out
    assert "example.com" in out


def test_written_path_is_relativised():
    """输出里不能带盘符 + 用户名（贴进 issue 就泄漏）。"""
    out = render(
        [make_rec("a@example.com")], written="ledger/latest.json", repo_rel=lambda p: str(p)
    )
    assert "台账：ledger/latest.json" in out


def test_quota_line_only_when_given():
    assert "本地配额" not in render([make_rec("a@example.com")])
    assert "本地配额：3/20" in render([make_rec("a@example.com")], quota_line="3/20")


def test_empty_batch_does_not_crash():
    """一个账号都没跑（全被本地闸门拦下时 count 仍 > 0，但也要能渲染空批）。"""
    out = render([])
    assert "成功 0 / 0" in out


def test_report_tolerates_dict_records():
    """台账记录是 dict，本批记录是对象 —— 报告两边都要能吃。"""
    d = {"email": "a@example.com", "status": "failed", "error": "x", "error_kind": "mail"}
    assert "mail" in render([d])


def test_report_tolerates_hand_edited_timings():
    """台账是人工可读的 JSON。报告崩掉的代价特别高：那意味着这批跑了什么
    **完全看不到了**。"""
    d = {"email": "a@example.com", "status": "success", "timings": "oops"}
    assert "成功 1 / 1" in render([d])
    assert report.stage_stats([d], STAGES)[0][1] is None


def test_stage_stats_returns_median_and_max():
    # 三个样本（400 / 400 / 900）—— 两个样本的中位是它们的均值（650），
    # 那样测出来的数字不直观。
    r1, r2, r3 = (make_rec(f"{c}@example.com") for c in "abc")
    r3.timings["login_ms"] = 900
    # ⚠ 键是**阶段名**（`login`），不是 `timings` 里的键（`login_ms`）——
    #   两种叫法混在一起时调用处很容易写错。
    stats = dict((name, (med, mx)) for name, med, mx in report.stage_stats([r1, r2, r3], STAGES))
    assert stats["login"] == (400, 900)
    assert stats["mailbox"] == (100, 100)
