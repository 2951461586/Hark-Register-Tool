"""本地配额闸门的离线用例 —— **零网络请求**。

🔴 这一层最该钉住的不是"计数对不对"，而是**它在什么时候说假话**：
提示文案说"这一批会全部被跳过"、而实际 `--ignore-quota` 下全部成功，
是 Intern-Register-Tool 实测踩过的形态（提示说假话比没有提示更坏）。
"""

import time

import pytest

from common import config
from src import quota


def _write(lines):
    p = quota.state_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _entry(ts, ok=True, email="a@example.com"):
    import json

    return json.dumps({"ts": ts, "email": email, "ok": ok})


def test_empty_state_is_zero():
    st = quota.status()
    assert (st.used, st.attempts, st.left) == (0, 0, config.REG_QUOTA_MAX)
    assert not st.exhausted


def test_only_successes_count_toward_the_gate():
    """失败尝试要记账（供复盘看打了几枪），但**不烧额度** ——
    否则一次服务端抖动就能把额度白烧光。"""
    now = time.time()
    _write([_entry(now, ok=True), _entry(now, ok=False), _entry(now, ok=False)])
    st = quota.status(now=now)
    assert st.used == 1 and st.attempts == 3


def test_entries_outside_window_are_ignored():
    now = time.time()
    old = now - (config.REG_QUOTA_WINDOW_H + 1) * 3600
    _write([_entry(old, ok=True), _entry(now, ok=True)])
    assert quota.status(now=now).used == 1


def test_exhausted_raises_unless_ignored(monkeypatch):
    monkeypatch.setattr(config, "REG_QUOTA_MAX", 1)
    now = time.time()
    _write([_entry(now, ok=True)])
    with pytest.raises(quota.QuotaExceeded) as ex:
        quota.check(now=now)
    assert "未发出任何请求" in str(ex.value)
    assert quota.check(ignore=True, now=now).used == 1


def test_corrupt_lines_are_skipped_not_fatal():
    """半截行（写盘时被杀）只丢那一行，不丢整个文件。"""
    _write(["{ 半截", _entry(time.time(), ok=True), "", "not json at all"])
    assert quota.status().used == 1


def test_bad_ts_is_treated_as_outside_window():
    """手工改过的文件不该让整批炸在 `float()` 上。"""
    _write(['{"ts": "yesterday", "email": "x", "ok": true}'])
    assert quota.status().used == 0


def test_record_appends_and_is_readable():
    quota.record("a@example.com", ok=True)
    quota.record("b@example.com", ok=False, note="network")
    st = quota.status()
    assert st.used == 1 and st.attempts == 2


def test_minutes_until_free_rounds_up():
    """报"还差 0 分钟"但闸门仍然拦着，是最让人困惑的一种输出。"""
    monkeypatch_max = 1
    now = time.time()
    _write([_entry(now, ok=True)])
    st = quota.QuotaStatus(used=1, attempts=1, limit=monkeypatch_max, window_h=1.0, oldest_ts=now)
    assert st.minutes_until_free(now) == 60  # 整整 60 分钟，向上取整不该给 59


def test_shortfall_hint_is_silent_when_ignored():
    """🔴 开关打开时提示必须闭嘴 —— 否则它会说一句**假话**。"""
    assert quota.shortfall_hint(left=0, count=5, ignore=True) == ""
    assert "只剩 0 个名额" in quota.shortfall_hint(left=0, count=5, ignore=False)
    assert quota.shortfall_hint(left=5, count=5, ignore=False) == ""
    assert quota.shortfall_hint(left=9, count=5, ignore=False) == ""


def test_compaction_keeps_window_entries(monkeypatch):
    monkeypatch.setattr(config, "REG_QUOTA_MAX", 1)
    now = time.time()
    old = now - (config.REG_QUOTA_WINDOW_H + 1) * 3600
    _write([_entry(old, ok=True) for _ in range(300)] + [_entry(now, ok=True)])
    quota._maybe_compact()
    assert quota.status(now=now).used == 1
    assert len(quota.state_path().read_text(encoding="utf-8").splitlines()) == 1
