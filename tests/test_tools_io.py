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

import probe_domains  # noqa: E402
import probe_register_limit  # noqa: E402
import probe_slots  # noqa: E402
import prune_ledger  # noqa: E402
import recover_accounts  # noqa: E402
import repair_stages  # noqa: E402
import verify_accounts  # noqa: E402

from src import ledger  # noqa: E402
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
            "stages": {"mailbox": "ok", "register": "ok", "activate": "ok",
                       "password": "ok", "login": "ok", "app": "ok"},
        },
        {
            "email": "ok2@example.com",
            "password": "Pw#2",
            "status": "success",
            "stages": {"mailbox": "ok", "register": "ok", "activate": "ok",
                       "password": "ok", "login": "ok", "app": "ok"},
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
            "stages": {"mailbox": "ok", "register": "ok", "activate": "ok(mail+verify)",
                       "password": "failed", "login": "", "app": ""},
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
    rec = [r for r in ledger.load_existing(ledger.ledger_path()) if r["email"] == "bad@example.com"][0]
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
        return {"user": {"id": "uid-1", "hasAppAccess": True, "region": "us"},
                "session": {"expiresAt": "2026-11-01T00:00:00Z"}} if self.session_ok else {}

    def profile(self):
        return {"email": self.email or "x@example.com"}

    def billing_summary(self):
        return {"plan": {"id": "free"}, "meters": {"harkTokens": {"dailyLimit": 8_000_000}}}

    def conversations(self):
        return {"conversations": []}


class FakeMail:
    def __init__(self, link: str | None = "https://auth.hark.com/api/auth/magic-link/verify?token=t"):
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
        "stages": {"mailbox": "ok", "register": "ok", "activate": "ok(mail+verify)",
                   "password": "ok", "login": "failed", "app": ""},
    }
    base.update(kw)
    return base


def test_recover_one_with_password_logs_in_and_marks_success():
    pool = SlotPool(["http://slot:1"], timeout=1)
    rec = _ticket_rec()
    out, verdict = recover_accounts.recover_one(
        rec, pool, client_factory=lambda proxy=None: FakeHark(proxy=proxy),
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
        _ticket_rec(password=""), pool,
        client_factory=lambda proxy=None: fake, mail_factory=lambda: FakeMail(),
    )
    assert out["status"] == "success", verdict
    assert out["password"], "应当补设了一个密码"
    assert len(out["password"]) >= 8
    assert out["stages"]["password"] == "ok(补设)", "阶段值必须跟着改（否则自相矛盾）"
    assert "补设" in out["recovered"]


def test_recover_one_gives_up_when_the_account_is_gone():
    pool = SlotPool(["http://slot:1"], timeout=1)
    out, verdict = recover_accounts.recover_one(
        _ticket_rec(), pool,
        client_factory=lambda proxy=None: FakeHark(exists=False), mail_factory=lambda: FakeMail(),
    )
    assert out["status"] == "failed", "账号不存在时不该改状态"
    assert "exists:false" in verdict


def test_recover_one_gives_up_when_the_mail_never_arrives():
    pool = SlotPool(["http://slot:1"], timeout=1)
    out, verdict = recover_accounts.recover_one(
        _ticket_rec(password=""), pool,
        client_factory=lambda proxy=None: FakeHark(), mail_factory=lambda: FakeMail(link=None),
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
        recover_accounts, "recover_one",
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
        verify_accounts, "check_one",
        lambda rec: {"email": rec["email"], "alive": True, "why": "", "plan": "free",
                     "daily_limit": 8_000_000, "ms": 1, "waited_ms": 0},
    )
    assert verify_accounts.main(["--json", str(tmp_path / "v.json")]) == 0
    rep = json.loads((tmp_path / "v.json").read_text(encoding="utf-8"))
    assert rep["coverage"]["covers_ledger"] is True
    assert rep["alive"] == 2 and rep["total"] == 2


def test_verify_exit_1_when_an_account_is_dead(seeded, monkeypatch):
    monkeypatch.setattr(
        verify_accounts, "check_one",
        lambda rec: {"email": rec["email"], "alive": False, "why": "401", "plan": "",
                     "daily_limit": 0, "ms": 1, "waited_ms": 0},
    )
    assert verify_accounts.main([]) == 1


def test_verify_exit_3_when_only_partially_covered(seeded, monkeypatch):
    """🔴 覆盖不足必须返回 3 —— 只打印不改退出码的话，脚本化调用读到的是“成功”。"""
    monkeypatch.setattr(
        verify_accounts, "check_one",
        lambda rec: {"email": rec["email"], "alive": True, "why": "", "plan": "free",
                     "daily_limit": 8_000_000, "ms": 1, "waited_ms": 0},
    )
    assert verify_accounts.main(["--limit", "1"]) == 3
    assert verify_accounts.main(["--limit", "1", "--allow-partial"]) == 0


def test_verify_exit_2_when_there_is_nothing_to_check(write_ledger):
    write_ledger([{"email": "a@example.com", "password": "", "status": "failed",
                   "stages": {"password": "failed"}}])
    assert verify_accounts.main([]) == 2


# ── probe_*：结论与退出码（探测函数换掉，不碰网络）────────────
def test_probe_domains_reports_usable_and_unusable(monkeypatch, tmp_path):
    monkeypatch.setattr(
        probe_domains, "probe_domain",
        lambda d, w: {"domain": d, "address": f"x@{d}", "mail": "link" if d == "good.test" else "",
                      "error": "" if d == "good.test" else "等 35s 邮件没到"},
    )
    assert probe_domains.main(["--domains", "good.test,bad.test", "--wait", "1",
                               "--json", str(tmp_path / "d.json")]) == 1
    rep = json.loads((tmp_path / "d.json").read_text(encoding="utf-8"))
    assert rep["usable"] == ["good.test"] and rep["unusable"] == ["bad.test"]


def test_probe_slots_exit_2_on_duplicate_exit_ips(monkeypatch):
    """🔴 出口 IP 重复 ⇒ 池子的并发收益为 0，必须是**非 0** 退出码。"""
    monkeypatch.setattr(
        probe_slots, "probe_slot",
        lambda url: {"slot": url, "exit_ip": "10.0.0.1", "hark": 200, "error": ""},
    )
    assert probe_slots.main(["--slots", "http://a:1,http://b:2"]) == 2


def test_probe_slots_exit_0_when_all_distinct(monkeypatch):
    ips = iter(["10.0.0.1", "10.0.0.2"])
    monkeypatch.setattr(
        probe_slots, "probe_slot",
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
