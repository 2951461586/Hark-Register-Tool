"""台账合并规则的离线用例 —— **零网络请求**。

台账已经丢过两次（Intern-Register-Tool 的账），而且两次都是**静默**的。
所以这里宁可多写：规则本身是"数学上保证字段只增不减"，用例要能证明它。
"""

import json

import pytest

from src import ledger


def rec(email, status="success", **kw):
    d = {"email": email, "status": status}
    d.update(kw)
    return d


# ── 合并：不丢历史 ────────────────────────────────────────────────
def test_old_records_not_in_this_batch_are_kept():
    old = [rec("a@example.com"), rec("b@example.com")]
    merged, kept, added, upgraded = ledger.merge_records(old, [rec("c@example.com")])
    assert kept == 2 and added == 1 and upgraded == 0
    assert {r["email"] for r in merged} == {"a@example.com", "b@example.com", "c@example.com"}


def test_failure_does_not_overwrite_success():
    """服务端抖一下不该把好账号标成坏 —— rank 门控。"""
    old = [rec("a@example.com", "success", password="Pw#1")]
    new = [rec("a@example.com", "failed", error="boom", error_kind="network")]
    merged, _, _, upgraded = ledger.merge_records(old, new)
    assert merged[0]["status"] == "success"
    assert merged[0]["password"] == "Pw#1"
    assert upgraded == 0


def test_success_upgrades_failure():
    old = [rec("a@example.com", "failed", error="boom", error_kind="network")]
    new = [rec("a@example.com", "success", password="Pw#1")]
    merged, _, _, upgraded = ledger.merge_records(old, new)
    assert merged[0]["status"] == "success"
    assert upgraded == 1


def test_same_rank_takes_union_so_no_field_is_lost():
    """🔴 这条钉的是 Intern-Register-Tool 那次事故：增量记录（**没有 `status`**）
    ⇒ rank 恒为 0，与另一条 rank=0 的记录同级 ⇒ "整条替换"会让字段永远写不进去，
    而打印出来的一切都"正常"。并集是唯一没有这个漏洞的规则。"""
    old = [{"email": "a@example.com", "password": "Pw#1", "session_token": "tok"}]
    new = [{"email": "a@example.com", "timings": {"login_ms": 900}}]
    merged, _, _, upgraded = ledger.merge_records(old, new)
    assert upgraded == 1
    assert merged[0]["password"] == "Pw#1"
    assert merged[0]["session_token"] == "tok"
    assert merged[0]["timings"] == {"login_ms": 900}


def test_lower_rank_record_does_not_touch_the_higher_rank_one():
    """降级**完全不动**（不是"合并但保留旧值"）。

    🔴 这条与上一条是同一枚硬币的两面，必须一起钉：运行期合并里 rank 更低的
      记录往往是**一次失败的尝试**，它的 `error` / 中间态字段不该挂到一个
      已经成功的账号上。`merge_fragments`（从散落来源重建台账）才是"降级也补
      缺口"—— 本项目**没有**那条路，因为那需要一份可重建的来源。
      两者看着像重复，降级行为却必须不同。
    """
    old = [rec("a@example.com", "success", password="Pw#1")]
    new = [{"email": "a@example.com", "timings": {"login_ms": 900}}]
    merged, _, _, upgraded = ledger.merge_records(old, new)
    assert upgraded == 0
    assert merged[0] == old[0], "降级记录不该产生任何改动"


def test_same_rank_new_value_wins():
    old = [rec("a@example.com", "success", region="us")]
    new = [rec("a@example.com", "success", region="eu")]
    merged, *_ = ledger.merge_records(old, new)
    assert merged[0]["region"] == "eu"


def test_records_without_email_are_kept_verbatim():
    old = [{"status": "skipped", "error": "本地配额", "error_kind": "quota_guard"}]
    merged, kept, added, _ = ledger.merge_records(old, [{"status": "skipped"}])
    assert kept == 1 and added == 1 and len(merged) == 2


def test_duplicate_emails_in_existing_are_collapsed():
    old = [rec("a@example.com", password="first"), rec("a@example.com", password="second")]
    merged, kept, *_ = ledger.merge_records(old, [])
    assert kept == 1 and len(merged) == 1 and merged[0]["password"] == "first"


def test_merge_is_idempotent():
    """同一批跑两次不该改变台账内容（幂等）。"""
    old = [rec("a@example.com", "success", password="Pw#1")]
    new = [rec("a@example.com", "success", password="Pw#1")]
    once, *_ = ledger.merge_records(old, new)
    twice, *_ = ledger.merge_records(once, new)
    assert once == twice


# ── 防静默缩水护栏 ────────────────────────────────────────────────
def test_save_refuses_to_shrink(tmp_path, sample_ledger):
    p = tmp_path / "x.json"
    with pytest.raises(ValueError) as ex:
        ledger.save(p, sample_ledger[:1], existing=sample_ledger)
    assert "防静默缩水" in str(ex.value)
    assert not p.exists(), "护栏触发时不该留下半个文件"


def test_save_allows_equal_or_growing(tmp_path, sample_ledger):
    p = tmp_path / "x.json"
    ledger.save(p, sample_ledger, existing=sample_ledger)
    assert json.loads(p.read_text(encoding="utf-8")) == sample_ledger


def test_save_without_existing_never_checks(tmp_path):
    """`existing=None`（默认）表示"这次写的就是全量" —— 显式覆盖那条路。"""
    p = tmp_path / "x.json"
    ledger.save(p, [rec("a@example.com")])
    assert p.is_file()


def test_corrupt_ledger_reads_as_empty_not_crash(tmp_path):
    p = tmp_path / "x.json"
    p.write_text("{ 这不是 JSON", encoding="utf-8")
    assert ledger.load_existing(p) == []


def test_ledger_of_wrong_shape_reads_as_empty(tmp_path):
    p = tmp_path / "x.json"
    p.write_text('{"email": "not-a-list"}', encoding="utf-8")
    assert ledger.load_existing(p) == []


def test_non_dict_entries_are_dropped(tmp_path):
    p = tmp_path / "x.json"
    p.write_text('[1, "x", {"email": "a@example.com"}]', encoding="utf-8")
    assert ledger.load_existing(p) == [{"email": "a@example.com"}]


# ── 快照命名 ──────────────────────────────────────────────────────
def test_snapshot_path_shape():
    p = ledger.snapshot_path(when=1767225600)  # 2026-01-01 00:00:00 UTC
    assert p.name.startswith("accounts-")
    assert ledger.SNAPSHOT_RE.match(p.name), p.name
    assert p.parent.parent.name == "runs"


def test_snapshot_glob_ignores_foreign_json(tmp_path, monkeypatch):
    """🔴 `runs/` 是个人工可写的目录。一个 `notes.json` 被当成"最新读源"，
    会让缩水护栏的分母、报告的分母全跟着变 —— 而且**全程不报错**。"""
    runs = tmp_path / "ledger" / "runs" / "2026-01-01"
    runs.mkdir(parents=True)
    (runs / "accounts-20260101-000001.json").write_text("[]", encoding="utf-8")
    (runs / "notes.json").write_text("[]", encoding="utf-8")
    (runs / "accounts-tmp.json").write_text("[]", encoding="utf-8")
    (runs / "accounts-20260101-000002.json.bak").write_text("[]", encoding="utf-8")
    monkeypatch.setattr(ledger, "LEDGER_DIR", tmp_path / "ledger")
    names = [p.name for p in ledger.snapshot_paths()]
    assert names == ["accounts-20260101-000001.json"]


def test_ledger_path_is_latest_and_nonexistent_when_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger, "LEDGER_DIR", tmp_path / "ledger")
    p = ledger.ledger_path()
    assert not p.exists(), "空台账目录不该让工具起不来，但要给一个不存在的占位路径"
    assert ledger.load_existing(p) == []


def test_ledger_path_reflects_new_snapshots(tmp_path, monkeypatch):
    """🔴 读源必须**每次重新扫目录**。冻进模块级常量会让"先读→跑→写回"
    把结果写回**旧快照**。"""
    monkeypatch.setattr(ledger, "LEDGER_DIR", tmp_path / "ledger")
    a = ledger.snapshot_path(when=1767225600)
    a.parent.mkdir(parents=True, exist_ok=True)
    a.write_text("[]", encoding="utf-8")
    assert ledger.ledger_path() == a
    b = ledger.snapshot_path(when=1767225660)
    b.write_text("[]", encoding="utf-8")
    assert ledger.ledger_path() == b


# ── 落盘顺序与 latest.json ────────────────────────────────────────
def test_save_snapshot_writes_full_to_snapshot_and_batch_to_latest(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger, "LEDGER_DIR", tmp_path / "ledger")
    full = [rec("a@example.com"), rec("b@example.com")]
    batch = [rec("b@example.com")]
    snap, last = ledger.save_snapshot(full, batch)
    assert json.loads(snap.read_text(encoding="utf-8")) == full
    assert json.loads(last.read_text(encoding="utf-8")) == batch
    assert last.name == "latest.json"


def test_latest_is_not_the_read_source(tmp_path, monkeypatch):
    """把 `latest.json` 当读源 ⇒ 台账停止累积、每次跑批覆盖上一次。"""
    monkeypatch.setattr(ledger, "LEDGER_DIR", tmp_path / "ledger")
    ledger.save_snapshot([rec("a@example.com")], [rec("a@example.com")])
    assert ledger.ledger_path() != ledger.last_run_path()
    assert ledger.ledger_path().name.startswith("accounts-")


def test_is_ledger_path(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger, "LEDGER_DIR", tmp_path / "ledger")
    assert ledger.is_ledger_path(tmp_path / "ledger" / "runs" / "x.json")
    assert not ledger.is_ledger_path(tmp_path / "elsewhere" / "x.json")


def test_repo_rel_strips_drive_and_root():
    """输出里不能带盘符 + 用户名（贴进 issue 就泄漏）。"""
    p = ledger.ROOT / "ledger" / "latest.json"
    assert ledger.repo_rel(p) == "ledger/latest.json"
    assert ledger.repo_rel(None) == ""
