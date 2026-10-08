"""`tools/` 的 **I/O 路径**用例 —— 零网络、零真实副作用。

与 `tests/test_tools.py` 的分工
------------------------------
那个文件测的是**纯函数**（`needs_recovery` / `_parse_sets` / `_verifiable` / `_pick`）；
这个文件测的是**会写东西的路径**：

    prune_ledger      写台账（删记录）
    repair_stages     写台账（改阶段值）
    recover_accounts  写台账（把 failed 升级成 success）+ 两条补登分支
    verify_accounts   只读 + **退出码**（覆盖率不足必须是 3）
    probe_*           结论与退出码（不碰网络：把探测函数换掉）

🔴 为什么这些必须有自动化覆盖
---------------------------
它们都**写台账**，而台账是账号的**唯一副本**（邮箱是临时的、收不到找回信）。
手测一次"看起来对"证明不了"下次不会把 78 条写成 10 条" ——
而 `ledger.save()` 的防静默缩水护栏只拦"合并后变少"，**拦不住**
"参数传错、压根没合并"。所以这里逐个断言**写出来的条数与内容**。

⚠ `sys.path` 那一段是必须的（同 `test_tools.py` 的说明）：`tools/<子目录>/*.py`
  里的 `from _path import ROOT` 就是"把 tools/ 加进 sys.path"的实现。
"""

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
for _sub in ("tools/data", "tools/ops", "tools/probes"):
    _p = str(ROOT / _sub)
    if _p not in sys.path:
        sys.path.insert(0, _p)

import cf_service_doctor  # noqa: E402
import probe_domains  # noqa: E402
import probe_magic_link  # noqa: E402
import probe_register_limit  # noqa: E402
import probe_slots  # noqa: E402
import prune_ledger  # noqa: E402
import recover_accounts  # noqa: E402
import repair_stages  # noqa: E402
import verify_accounts  # noqa: E402

from src import (  # noqa: E402
    ledger,
    pipeline,
)
from src.hark import HarkQuota, HarkRejected  # noqa: E402
from src.proxypool import SlotPool  # noqa: E402


# ── 夹具：一本三行的台账（2 成功 + 1 失败）──────────────────────
@pytest.fixture
def seeded(write_ledger):
    """写一本台账并返回它的路径。conftest 的 `isolate_state` 已把
    `ledger.LEDGER_DIR` 指到 tmp，所以 `ledger.ledger_path()` 读得到它。"""
    records = [
        {
            "email": "ok1@example.com",
            "password": "Pw#1",
            "status": "success",
            "stages": {
                "mailbox": "ok",
                "register": "ok",
                "activate": "ok",
                "password": "ok",
                "login": "ok",
                "app": "ok",
            },
        },
        {
            "email": "ok2@example.com",
            "password": "Pw#2",
            "status": "success",
            "stages": {
                "mailbox": "ok",
                "register": "ok",
                "activate": "ok",
                "password": "ok",
                "login": "ok",
                "app": "ok",
            },
        },
        {
            # 第三条刻意做成**真实发生过的那种形态**：`activate` 被错误标成 ok
            # （当时 verify 其实失败了），而 `password` 写着 failed（它从没跑过）。
            # 这样它同时是：
            #   · `recover_accounts` 的待补对象（activate=ok 且 status≠success）
            #   · `repair_stages` 的修复对象（两个阶段值都需要改）
            "email": "bad@example.com",
            "password": "",
            "status": "failed",
            "error": "等 120s 没等到 magic link 邮件",
            "error_kind": "rejected",
            "stages": {
                "mailbox": "ok",
                "register": "ok",
                "activate": "ok(mail+verify)",
                "password": "failed",
                "login": "",
                "app": "",
            },
        },
    ]
    p = write_ledger(records)
    return p


# ── prune_ledger ────────────────────────────────────────────────
def test_prune_refuses_success_without_the_explicit_flag(seeded, capsys):
    assert prune_ledger.main(["--status", "success", "--apply"]) == 2
    assert "拒绝执行" in capsys.readouterr().err
    assert len(ledger.load_existing(ledger.ledger_path())) == 3, "被拒时不该动台账"


def test_prune_dry_run_writes_nothing(seeded):
    before = ledger.snapshot_paths()
    assert prune_ledger.main([]) == 0
    assert ledger.snapshot_paths() == before, "dry run 不该落新快照"


def test_prune_apply_removes_only_failed_and_backs_up(seeded, tmp_path, monkeypatch):
    # 备份写到 ROOT/state/ —— 测试里把它指到 tmp，别污染真实 state/
    monkeypatch.setattr(prune_ledger, "ROOT", tmp_path)
    assert prune_ledger.main(["--apply"]) == 0

    left = ledger.load_existing(ledger.ledger_path())
    assert len(left) == 2, f"应当只剩 2 条成功记录，实际 {len(left)}"
    assert {r["email"] for r in left} == {"ok1@example.com", "ok2@example.com"}

    backups = list((tmp_path / "state").glob("ledger_before_prune_*.json"))
    assert len(backups) == 1, "必须留一份整本备份"
    assert len(json.loads(backups[0].read_text(encoding="utf-8"))) == 3, "备份应当是删之前的三条"


def test_prune_exit_3_when_nothing_to_remove(write_ledger):
    write_ledger([{"email": "a@example.com", "status": "success"}])
    assert prune_ledger.main([]) == 3


# ── repair_stages ───────────────────────────────────────────────
def test_repair_dry_run_then_apply(seeded, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(repair_stages, "ROOT", tmp_path)
    args = ["--email", "bad@example.com", "--set", "activate=failed", "--set", "password="]

    before = ledger.snapshot_paths()
    assert repair_stages.main(args) == 0
    assert ledger.snapshot_paths() == before, "dry run 不该落新快照"

    assert repair_stages.main([*args, "--apply"]) == 0
    rec = [
        r for r in ledger.load_existing(ledger.ledger_path()) if r["email"] == "bad@example.com"
    ][0]
    assert rec["stages"]["activate"] == "failed"
    assert rec["stages"]["password"] == ""
    assert "repaired" in rec, "要留下可追溯的说明"
    assert len(ledger.load_existing(ledger.ledger_path())) == 3, "条数不该变"
    assert list((tmp_path / "state").glob("repair_stages_backup_*.json")), "要有原值备份"


def test_repair_unknown_email_is_exit_2(seeded):
    assert repair_stages.main(["--email", "nobody@example.com", "--set", "activate=failed"]) == 2


def test_repair_requires_email_and_set(seeded):
    """两个参数都是必填 —— 工具不该“默认改某一条记录”。"""
    with pytest.raises(SystemExit):
        repair_stages.main(["--set", "activate=failed"])
    with pytest.raises(SystemExit):
        repair_stages.main(["--email", "bad@example.com"])


# ── recover_accounts ────────────────────────────────────────────
class FakeHark:
    """假 HarkClient：只实现本工具用到的那几个方法。"""

    def __init__(self, *, exists=True, session_ok=True, proxy=None):
        self.exists = exists
        self.session_ok = session_ok
        self.proxy = proxy
        self.password_set = ""
        self.email = ""

    def discover(self, identifier, kind="email"):
        return {"exists": self.exists, "hasPassword": bool(self.password_set)}

    def request_magic_link(self, email, callback_url=None):
        return {"status": True}

    def verify_magic_link(self, url):
        return {}

    def set_password(self, new_password):
        self.password_set = new_password
        return {"status": True}

    def sign_in(self, email, password):
        self.email = email
        return {"user": {"id": "uid-1"}}

    def session_token(self):
        return "tok"

    def region(self):
        return "us"

    def get_session(self):
        return (
            {
                "user": {"id": "uid-1", "hasAppAccess": True, "region": "us"},
                "session": {"expiresAt": "2026-11-01T00:00:00Z"},
            }
            if self.session_ok
            else {}
        )

    def profile(self):
        return {"email": self.email or "x@example.com"}

    def billing_summary(self):
        return {"plan": {"id": "free"}, "meters": {"harkTokens": {"dailyLimit": 8_000_000}}}

    def conversations(self):
        return {"conversations": []}


class FakeMail:
    def __init__(
        self, link: str | None = "https://auth.hark.com/api/auth/magic-link/verify?token=t"
    ):
        self.link = link
        self.last_error = ""
        self.last_polls = 1

    def wait_for_link(self, address, *keywords, **kw):
        return self.link


def _ticket_rec(**kw):
    base = {
        "email": "recover@example.com",
        "password": "Pw#old",
        "status": "failed",
        "error_kind": "quota",
        "error": "密码登录持续被限流",
        "stages": {
            "mailbox": "ok",
            "register": "ok",
            "activate": "ok(mail+verify)",
            "password": "ok",
            "login": "failed",
            "app": "",
        },
    }
    base.update(kw)
    return base


def test_recover_one_with_password_logs_in_and_marks_success():
    pool = SlotPool(["http://slot:1"], timeout=1)
    rec = _ticket_rec()
    out, verdict = recover_accounts.recover_one(
        rec,
        pool,
        client_factory=lambda proxy=None: FakeHark(proxy=proxy),
        mail_factory=lambda: FakeMail(),
    )
    assert out["status"] == "success", verdict
    assert out["password"] == "Pw#old", "有密码时不该换密码"
    assert out["stages"]["login"] == "ok"
    assert out["stages"]["app"].startswith("ok")
    assert out["recovered"], "要留可追溯的说明"
    assert out["user_id"] == "uid-1"
    # 出口要归还（否则池子会逐渐少一个）
    assert pool.acquire() == "http://slot:1"


def test_recover_one_without_password_sets_one_via_magic_link():
    pool = SlotPool(["http://slot:1"], timeout=1)
    fake = FakeHark()
    out, verdict = recover_accounts.recover_one(
        _ticket_rec(password=""),
        pool,
        client_factory=lambda proxy=None: fake,
        mail_factory=lambda: FakeMail(),
    )
    assert out["status"] == "success", verdict
    assert out["password"], "应当补设了一个密码"
    assert len(out["password"]) >= 8
    assert out["stages"]["password"] == "ok(补设)", "阶段值必须跟着改（否则自相矛盾）"
    assert "补设" in out["recovered"]


def test_recover_one_gives_up_when_the_account_is_gone():
    pool = SlotPool(["http://slot:1"], timeout=1)
    out, verdict = recover_accounts.recover_one(
        _ticket_rec(),
        pool,
        client_factory=lambda proxy=None: FakeHark(exists=False),
        mail_factory=lambda: FakeMail(),
    )
    assert out["status"] == "failed", "账号不存在时不该改状态"
    assert "exists:false" in verdict


def test_recover_one_gives_up_when_the_mail_never_arrives():
    pool = SlotPool(["http://slot:1"], timeout=1)
    out, verdict = recover_accounts.recover_one(
        _ticket_rec(password=""),
        pool,
        client_factory=lambda proxy=None: FakeHark(),
        mail_factory=lambda: FakeMail(link=None),
    )
    assert out["status"] == "failed"
    assert "magic link" in verdict


def test_recover_main_writes_only_the_recovered_records(seeded, monkeypatch):
    """`main()` 的写盘路径：只把**补成功**的记录并回去，其余原样。"""
    write_ledger = None  # noqa: F841  （夹具已经写过台账了）
    recovered = dict(_ticket_rec(email="bad@example.com", password="Pw#new"))
    recovered["status"] = "success"
    recovered["recovered"] = "测试"
    monkeypatch.setattr(
        recover_accounts,
        "recover_one",
        lambda rec, pool, **kw: (recovered, "✓ 测试"),
    )
    assert recover_accounts.main(["--apply"]) == 0

    after = ledger.load_existing(ledger.ledger_path())
    assert len(after) == 3, f"条数不该变，实际 {len(after)}"
    got = {r["email"]: r for r in after}
    assert got["bad@example.com"]["status"] == "success"
    assert got["bad@example.com"]["password"] == "Pw#new"
    assert got["ok1@example.com"]["status"] == "success", "别的记录不该被动"


# ── verify_accounts：退出码与 coverage ─────────────────────────
def test_verify_exit_0_when_fully_covered_and_alive(seeded, monkeypatch, tmp_path):
    monkeypatch.setattr(
        verify_accounts,
        "check_one",
        lambda rec: {
            "email": rec["email"],
            "alive": True,
            "why": "",
            "plan": "free",
            "daily_limit": 8_000_000,
            "ms": 1,
            "waited_ms": 0,
        },
    )
    assert verify_accounts.main(["--json", str(tmp_path / "v.json")]) == 0
    rep = json.loads((tmp_path / "v.json").read_text(encoding="utf-8"))
    assert rep["coverage"]["covers_ledger"] is True
    assert rep["alive"] == 2 and rep["total"] == 2


def test_verify_exit_1_when_an_account_is_dead(seeded, monkeypatch):
    monkeypatch.setattr(
        verify_accounts,
        "check_one",
        lambda rec: {
            "email": rec["email"],
            "alive": False,
            "why": "401",
            "plan": "",
            "daily_limit": 0,
            "ms": 1,
            "waited_ms": 0,
        },
    )
    assert verify_accounts.main([]) == 1


def test_verify_exit_3_when_only_partially_covered(seeded, monkeypatch):
    """🔴 覆盖不足必须返回 3 —— 只打印不改退出码的话，脚本化调用读到的是“成功”。"""
    monkeypatch.setattr(
        verify_accounts,
        "check_one",
        lambda rec: {
            "email": rec["email"],
            "alive": True,
            "why": "",
            "plan": "free",
            "daily_limit": 8_000_000,
            "ms": 1,
            "waited_ms": 0,
        },
    )
    assert verify_accounts.main(["--limit", "1"]) == 3
    assert verify_accounts.main(["--limit", "1", "--allow-partial"]) == 0


def test_verify_exit_2_when_there_is_nothing_to_check(write_ledger):
    write_ledger(
        [
            {
                "email": "a@example.com",
                "password": "",
                "status": "failed",
                "stages": {"password": "failed"},
            }
        ]
    )
    assert verify_accounts.main([]) == 2


# ── probe_*：结论与退出码（探测函数换掉，不碰网络）────────────
def test_probe_domains_reports_usable_and_unusable(monkeypatch, tmp_path):
    monkeypatch.setattr(
        probe_domains,
        "probe_domain",
        lambda d, w: {
            "domain": d,
            "address": f"x@{d}",
            "mail": "link" if d == "good.test" else "",
            "error": "" if d == "good.test" else "等 35s 邮件没到",
        },
    )
    assert (
        probe_domains.main(
            ["--domains", "good.test,bad.test", "--wait", "1", "--json", str(tmp_path / "d.json")]
        )
        == 1
    )
    rep = json.loads((tmp_path / "d.json").read_text(encoding="utf-8"))
    assert rep["usable"] == ["good.test"] and rep["unusable"] == ["bad.test"]


def test_probe_slots_exit_2_on_duplicate_exit_ips(monkeypatch):
    """🔴 出口 IP 重复 ⇒ 池子的并发收益为 0，必须是**非 0** 退出码。"""
    monkeypatch.setattr(
        probe_slots,
        "probe_slot",
        lambda url: {"slot": url, "exit_ip": "10.0.0.1", "hark": 200, "error": ""},
    )
    assert probe_slots.main(["--slots", "http://a:1,http://b:2"]) == 2


def test_probe_slots_exit_0_when_all_distinct(monkeypatch):
    ips = iter(["10.0.0.1", "10.0.0.2"])
    monkeypatch.setattr(
        probe_slots,
        "probe_slot",
        lambda url: {"slot": url, "exit_ip": next(ips), "hark": 200, "error": ""},
    )
    assert probe_slots.main(["--slots", "http://a:1,http://b:2"]) == 0


def test_probe_register_limit_verdict_is_domain_keyed(monkeypatch):
    """B1 换域名**通过** ⇒ 按域名键。用假的 `_one_round` 驱动这条判定。"""
    calls = {"n": 0}

    def fake_round(client, mail, domain, birthday, **kw):
        calls["n"] += 1
        if domain != "main.test":  # B1 换域名 → 通过
            return [{"call": "magic-link", "status": 200, "ms": 1, "error": ""}]
        if calls["n"] >= 2:  # 主域名第 2 轮 → 429
            return [{"call": "magic-link", "status": 429, "ms": 1, "error": "QUOTA", "quota": True}]
        return [{"call": "magic-link", "status": 200, "ms": 1, "error": ""}]

    monkeypatch.setattr(probe_register_limit, "_one_round", fake_round)
    monkeypatch.setattr(probe_register_limit.config, "WORKER_DOMAIN", "main.test")
    monkeypatch.setattr(probe_register_limit.config, "proxy_slots", lambda: [])
    # ⚠ 必须显式给 --alt-domain：不给的话 B1 会去问 Worker 的 /api/domains
    #   —— 那是一次**真实网络请求**，会打破本文件“零网络”的前提
    #   （实测踩到：它拿回了真实域名列表，测试“通过”了但已经出了网）。
    assert probe_register_limit.main(["--max-calls", "3", "--alt-domain", "other.test"]) == 0


# ══ 补齐：cf_service_doctor（此前**零覆盖**）════════════════════
# 它整个工具都没被自动化碰过，而它里面有三处**刻意的判据**，每一条都对应一次
# 踩过的坑：token 三态、Worker 元数据的端点选择、部署历史里的回滚检测。


def _patch_get(monkeypatch, mapping):
    """把 `_get` 换成按 URL 片段返回预置响应的假实现。"""

    def fake_get(url, token, timeout=25.0):
        for frag, resp in mapping.items():
            if frag in url:
                return resp
        return (404, None, "not found")

    monkeypatch.setattr(cf_service_doctor, "_get", fake_get)


def test_doctor_token_is_tristate(monkeypatch):
    """🔴 判据是 `/accounts`，而且必须是**三态**。

    401/403 才是"token 无效"（去换凭据）；5xx / 连不上 / 非 JSON 是
    **结果未知**（降级继续跑端点探针）—— 否则 CF 自己的 502 会被读成
    "token 无效"，而人会为一个与 token 无关的瞬时抖动去重签凭据。
    """
    cases = [
        ((200, {"success": True, "result": [{"id": "acct", "name": "n"}]}, ""), "ok"),
        ((401, {"success": False}, ""), "denied"),
        ((403, {"success": False}, ""), "denied"),
        ((502, None, "error code: 502"), "unknown"),
        ((0, None, "SSLError: boom"), "unknown"),
        ((200, None, "<html>not json</html>"), "unknown"),
    ]
    for resp, want in cases:
        # ⚠ `_r=resp` 把当前值**绑进默认参数**：不绑的话 lambda 按引用捕获循环变量，
        #   一旦它在下一轮之后才被调用，就会用**最后一个** resp（ruff B023）。
        monkeypatch.setattr(cf_service_doctor, "_get", lambda *a, _r=resp, **k: _r)
        state, accounts, msg = cf_service_doctor.probe_token("t")
        assert state == want, f"{resp} -> {state}（期望 {want}）"
        if want == "ok":
            assert accounts == [{"id": "acct", "name": "n"}]
        else:
            assert msg, "失败时必须给一句人能读的话"


def test_doctor_worker_uses_the_services_endpoint(monkeypatch):
    """🔴 必须打 `/workers/services/{name}`。

    `/workers/scripts/{name}` 返回的是**脚本内容**（multipart），不是元数据 ——
    拿它当 JSON 解析会得到 `HTTP 200` + 二进制垃圾，而报错形态让人以为是权限问题。
    """
    seen = []

    def fake_get(url, token, timeout=25.0):
        seen.append(url)
        return (
            200,
            {
                "success": True,
                "result": {
                    "default_environment": {
                        "modified_on": "2026-09-26T13:12:09Z",
                        "script_tag": "ff3eb35ce97c448d",
                    }
                },
            },
            "",
        )

    monkeypatch.setattr(cf_service_doctor, "_get", fake_get)
    ok, msg = cf_service_doctor.probe_worker("t", "acct", "worker-name")
    assert ok and "modified_on" in msg and "ff3eb35ce97c" in msg
    assert seen and "/workers/services/worker-name" in seen[0], seen
    assert "/workers/scripts/" not in seen[0], "不许打 scripts 端点（那是脚本体）"


def test_doctor_deployments_flags_a_rollback(monkeypatch):
    """🔴 同一个 version_id 在更早的部署里出现过 ⇒ **发生过回滚**。

    这条不在任何客户端错误里，而 Worker 行为突变时它是第一个该看的证据。
    """

    def dep(vid, when):
        return {"created_on": when, "versions": [{"version_id": vid, "percentage": 100}]}

    # 回滚：最新的 vid 与更早的一次相同
    _patch_get(
        monkeypatch,
        {
            "deployments": (
                200,
                {
                    "success": True,
                    "result": {
                        "deployments": [
                            dep("v1", "2026-10-08"),
                            dep("v2", "2026-10-07"),
                            dep("v1", "2026-10-06"),
                        ]
                    },
                },
                "",
            )
        },
    )
    ok, msg = cf_service_doctor.probe_deployments("t", "acct", "w")
    assert ok and "回滚" in msg, msg

    # 正常：每个 vid 都不同
    _patch_get(
        monkeypatch,
        {
            "deployments": (
                200,
                {
                    "success": True,
                    "result": {
                        "deployments": [
                            dep("v3", "2026-10-08"),
                            dep("v2", "2026-10-07"),
                            dep("v1", "2026-10-06"),
                        ]
                    },
                },
                "",
            )
        },
    )
    ok, msg = cf_service_doctor.probe_deployments("t", "acct", "w")
    assert ok and "回滚" not in msg, msg


def test_doctor_deployments_handles_empty_history(monkeypatch):
    _patch_get(
        monkeypatch, {"deployments": (200, {"success": True, "result": {"deployments": []}}, "")}
    )
    ok, msg = cf_service_doctor.probe_deployments("t", "acct", "w")
    assert not ok and "没有部署记录" in msg


def test_doctor_d1_usage_parses_rows_and_reports_graphql_errors(monkeypatch):
    """GraphQL 的日期格式**必须是 `YYYY-MM-DD`**（实测踩过：传 ISO 串会得到
    `date format should be '2006-01-02'`，一个把 Go 时间模板原样吐回来的报错）。"""
    sent = {}

    def fake_post(url, token, payload, timeout=25.0):
        sent.update(payload["variables"])
        return (
            200,
            {
                "data": {
                    "viewer": {
                        "accounts": [
                            {
                                "d1AnalyticsAdaptiveGroups": [
                                    {
                                        "dimensions": {"date": "2026-10-07"},
                                        "sum": {"rowsRead": 12594, "rowsWritten": 1393},
                                    }
                                ]
                            }
                        ]
                    }
                }
            },
            "",
        )

    monkeypatch.setattr(cf_service_doctor, "_post_json", fake_post)
    ok, rows, msg = cf_service_doctor.probe_d1_usage("t", "acct", days=3)
    assert ok and rows == [{"date": "2026-10-07", "rowsRead": 12594, "rowsWritten": 1393}]
    for key in ("since", "until"):
        assert len(sent[key]) == 10 and sent[key][4] == "-", (
            f"{key}={sent[key]!r} 应当是 YYYY-MM-DD"
        )

    monkeypatch.setattr(
        cf_service_doctor,
        "_post_json",
        lambda *a, **k: (200, {"errors": [{"message": "date format should be '2006-01-02'"}]}, ""),
    )
    ok, rows, msg = cf_service_doctor.probe_d1_usage("t", "acct")
    assert not ok and rows == [] and "date format" in msg


# ══ 补齐：verify_accounts.check_one（之前只被 monkeypatch 掉）════
def _setup_verify(monkeypatch, *, dead=False, quota_first=0):
    """把 `verify_accounts.HarkClient` 换成假的，并返回它的**计数状态**。

    ⚠ 状态放在闭包里、由工厂函数捕获，而不是写成类的**可变类属性** ——
      后者是所有实例共享的一份全局状态（测试之间会互相污染，
      而且静态检查也会报 `mutable class attribute`）。
    """
    state = {"sign_ins": 0, "dead": dead, "quota_first": quota_first}

    class FakeClient:
        def __init__(self, proxy=None):
            self.proxy = proxy

        def sign_in(self, email, password):
            state["sign_ins"] += 1
            if state["sign_ins"] <= state["quota_first"]:
                raise HarkQuota("HTTP 429 RATE_LIMITED", code="RATE_LIMITED", status=429)
            if state["dead"]:
                raise HarkRejected("HTTP 401", code="INVALID_EMAIL_OR_PASSWORD", status=401)
            return {"user": {"id": "uid-1"}}

        def billing_summary(self):
            return {"plan": {"id": "free"}, "meters": {"harkTokens": {"dailyLimit": 8_000_000}}}

    monkeypatch.setattr(verify_accounts, "HarkClient", FakeClient)
    monkeypatch.setattr(verify_accounts.config, "LOGIN_WINDOW_S", 0.01)
    monkeypatch.setattr(verify_accounts.config, "LOGIN_MAX_PER_WINDOW", 100)
    monkeypatch.setattr(verify_accounts.config, "LOGIN_RETRIES", 2)
    pipeline._LOGIN_LIMITERS.clear()
    return state


def test_check_one_alive_reads_two_levels(monkeypatch):
    """两级判据：登录 200 **且** 能读到额度。"""
    _setup_verify(monkeypatch)
    out = verify_accounts.check_one({"email": "a@example.com", "password": "Pw#1"})
    assert out["alive"] is True and out["plan"] == "free" and out["daily_limit"] == 8_000_000
    assert out["why"] == ""


def test_check_one_dead_account_reports_the_reason(monkeypatch):
    _setup_verify(monkeypatch, dead=True)
    out = verify_accounts.check_one({"email": "a@example.com", "password": "Pw#bad"})
    assert out["alive"] is False
    assert "登录失败" in out["why"] and "rejected" in out["why"]


def test_check_one_retries_429_instead_of_calling_the_account_dead(monkeypatch):
    """🔴 这条钉的是一个**假阴性**：登录的 429 是自愈的（或者干脆是 CF 边缘限流），
    把它读成“账号坏了”会让复核工具报“你的账号全挂了”。"""
    state = _setup_verify(monkeypatch, quota_first=1)
    out = verify_accounts.check_one({"email": "a@example.com", "password": "Pw#1"})
    assert out["alive"] is True, out["why"]
    assert state["sign_ins"] == 2, "第一次 429 之后应当重试一次"


# ══ 补齐：probe_magic_link 的三种结论 ══════════════════════════
def test_probe_magic_link_verdicts(monkeypatch):
    """对照实验的三种结论 + 控制组失败，各自对应一个退出码。"""
    monkeypatch.setattr(probe_magic_link.time, "sleep", lambda s: None)
    cases = [
        # (控制组, 目标组, 期望退出码)
        ((True, "ok"), [(True, 200, "200")], 0),  # 都通
        ((True, "ok"), [(False, 500, "HarkNetwork")], 1),  # 端点级故障
        # 出口维度（混合）：`--proxy a,b` 会探 **3** 个出口（direct + a + b），
        # 所以预置的目标结果也必须是 3 个（少一个就是 StopIteration —— 实测踩到）。
        ((True, "ok"), [(True, 200, ""), (False, 429, "x"), (False, 429, "x")], 3),
        ((False, "控制组也挂了"), [(True, 200, "")], 2),  # 控制组失败
    ]
    for control, targets, want in cases:
        monkeypatch.setattr(probe_magic_link, "probe_controls", lambda p, _c=control: _c)
        it = iter(targets)
        monkeypatch.setattr(probe_magic_link, "probe_target", lambda p, e=None, _it=it: next(_it))
        got = probe_magic_link.main([] if len(targets) == 1 else ["--proxy", "a,b"])
        assert got == want, f"{control} {targets} -> {got}（期望 {want}）"


def test_probe_slots_exit_3_without_any_slots(monkeypatch):
    monkeypatch.setattr(probe_slots.config, "proxy_slots", lambda: [])
    assert probe_slots.main([]) == 3


def test_worker_domains_raises_instead_of_falling_back(monkeypatch):
    """🔴 拿不到域名必须**大声报错**。

    早前版本写死一个自有域名当兜底 —— 那既泄漏标识，又让工具**静默做错事**
    （探一个与当前配置无关的域名，报告看起来正常）。
    """

    class Resp:
        def json(self):
            return {"domains": []}

    monkeypatch.setattr("requests.Session.get", lambda self, *a, **k: Resp())
    with pytest.raises(RuntimeError) as ex:
        probe_domains.worker_domains()
    assert "没有返回任何域名" in str(ex.value)
    assert "--domains" in str(ex.value), "报错里要给出修法"


# ══ 补齐：probe_register_limit 的窗口测量与“没测到”路径 ════════
def test_trip_quota_returns_the_hitting_round(monkeypatch):
    calls = {"n": 0}

    def fake_round(client, mail, domain, birthday, **kw):
        calls["n"] += 1
        hit = calls["n"] >= 3
        return [
            {
                "call": "magic-link",
                "status": 429 if hit else 200,
                "ms": 1,
                "error": "QUOTA" if hit else "",
                **({"quota": True} if hit else {}),
            }
        ]

    monkeypatch.setattr(probe_register_limit, "_one_round", fake_round)
    n, hit = probe_register_limit._trip_quota(None, None, "d.test", "1995-01-01", cap_s=10)
    assert n == 3 and hit and hit["call"] == "magic-link"


def test_measure_window_reports_recovery_and_runs_the_control(monkeypatch):
    """`measure_window` 的编排：打到 429 → 轮询到通过 → 再打到 429 做对照。"""
    monkeypatch.setattr(probe_register_limit.time, "sleep", lambda s: None)
    seq = []

    def fake_round(client, mail, domain, birthday, **kw):
        seq.append(len(seq))
        # 1 成功；2 触顶；3/4 仍 429；5 通过；6 对照再触顶；7 对照探测通过
        hit = len(seq) in (2, 3, 4, 6)
        return [
            {
                "call": "magic-link",
                "status": 429 if hit else 200,
                "ms": 1,
                "error": "QUOTA" if hit else "",
                **({"quota": True} if hit else {}),
            }
        ]

    monkeypatch.setattr(probe_register_limit, "_one_round", fake_round)
    win = probe_register_limit.measure_window(
        None, None, "d.test", "1995-01-01", poll_s=10, cap_s=100, log=lambda *a: None
    )
    assert win["tripped"] is True
    assert win["recovered_after_s"] is not None, "应当测到恢复"
    assert len(win["probes"]) == 3 and win["probes"][-1]["ok"] is True
    assert win["control"]["passed"] is True


def test_measure_window_gives_up_when_it_never_trips(monkeypatch):
    monkeypatch.setattr(probe_register_limit.time, "sleep", lambda s: None)
    monkeypatch.setattr(
        probe_register_limit,
        "_one_round",
        lambda *a, **k: [{"call": "magic-link", "status": 200, "ms": 1, "error": ""}],
    )
    win = probe_register_limit.measure_window(
        None, None, "d.test", "1995-01-01", poll_s=1, cap_s=0.01, log=lambda *a: None
    )
    assert win["tripped"] is False


def test_probe_register_limit_exit_1_when_no_quota(monkeypatch):
    monkeypatch.setattr(
        probe_register_limit,
        "_one_round",
        lambda *a, **k: [{"call": "magic-link", "status": 200, "ms": 1, "error": ""}],
    )
    monkeypatch.setattr(probe_register_limit.config, "WORKER_DOMAIN", "main.test")
    monkeypatch.setattr(probe_register_limit.config, "proxy_slots", lambda: [])
    assert probe_register_limit.main(["--max-calls", "2"]) == 1


def test_recover_main_exit_2_when_nothing_to_recover(write_ledger):
    """没有待补的账号时返回 2（而不是“成功且什么都没做”）。"""
    write_ledger([{"email": "a@example.com", "status": "success", "stages": {"activate": "ok"}}])
    assert recover_accounts.main([]) == 2
