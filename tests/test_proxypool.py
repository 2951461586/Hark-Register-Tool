"""槽位代理池的离线用例 —— **零网络请求**。

🔴 这一层要钉的不是"能借能还"，而是两件会**静默**变坏的事：

1. **均匀分摊**。登录限流是按出口的（3 次/10s），如果池子总是先把第一个出口
   用满，那 N 个出口的吞吐就等于 1 个 —— 而它不会报错，只会慢。
2. **异常路径也必须归还**。一个漏掉的出口会让池子逐渐"少一个"，
   最后 `acquire()` 卡到超时，而现象是"批量突然变慢/超时"，不是"池子漏了"。
"""

import threading
import time

import pytest

from src.proxypool import SlotPool, SlotPoolEmpty

SLOTS = ["http://127.0.0.1:7901", "http://127.0.0.1:7902", "http://127.0.0.1:7903"]


def test_empty_pool_returns_none_and_is_falsy():
    """没配池子不是错误 —— 整条链路要能照跑（走单代理 / 直连）。"""
    p = SlotPool([])
    assert not p
    assert len(p) == 0
    assert p.acquire() is None
    assert "未配置" in p.describe()


def test_acquire_release_roundtrip():
    p = SlotPool(SLOTS)
    url = p.acquire()
    assert url in SLOTS
    assert p.stats()[SLOTS.index(url)].used == 1
    p.release(url)
    assert p.acquire() is not None


def test_slots_are_handed_out_evenly_not_first_come_first_served():
    """🔴 这是池子的**全部价值**：N 个出口要一起用，而不是一个个用。"""
    p = SlotPool(SLOTS)
    got: list[str] = [u for u in (p.acquire() for _ in range(3)) if u]
    assert len(got) == 3, f"非空池不该返回 None：{got}"
    assert sorted(got) == sorted(SLOTS), f"三个出口应当各被借一次，实际 {got}"


def test_least_used_wins_after_release():
    p = SlotPool(SLOTS)
    a = p.acquire()
    p.release(a)
    b = p.acquire()
    # 刚还回去的那个已经被用过一次，所以应当借出**另一个**
    assert b != a, "应当优先借使用次数最少的出口"


def test_acquire_blocks_when_all_held_then_times_out():
    p = SlotPool(SLOTS[:1], timeout=0.3)
    p.acquire()
    t0 = time.time()
    with pytest.raises(SlotPoolEmpty) as ex:
        p.acquire()
    assert time.time() - t0 >= 0.25
    assert "没等到空闲槽位" in str(ex.value)


def test_release_wakes_a_waiter():
    """阻塞中的 `acquire()` 必须在归还时被唤醒 —— 否则池子会假死。"""
    p = SlotPool(SLOTS[:1], timeout=5)
    held = p.acquire()
    got = {}

    def waiter():
        got["url"] = p.acquire()

    th = threading.Thread(target=waiter)
    th.start()
    time.sleep(0.2)
    p.release(held)
    th.join(timeout=3)
    assert not th.is_alive()
    assert got["url"] == held


def test_context_manager_releases_on_exception():
    """🔴 `with` 里抛异常也必须归还 —— 否则一次失败就永久少一个出口。"""
    p = SlotPool(SLOTS[:1], timeout=0.2)
    with pytest.raises(ValueError):
        with p.slot():
            raise ValueError("boom")
    assert p.acquire() == SLOTS[0]  # 还能借到，说明确实归还了


def test_double_release_does_not_duplicate():
    """重复归还不能把同一个出口在空闲表里放两份 —— 那会让它被同时借给两个人。

    ⚠ 必须显式给 `timeout`：默认是 `HR_PROXY_SLOT_TIMEOUT`（240s），
      忘了给的话这个用例会真的等 4 分钟 —— 实测就踩了（整个套件从 7s 变 248s），
      而现象是“测试很慢”，看起来像环境问题。
    """
    p = SlotPool(SLOTS[:1], timeout=0.2)
    url = p.acquire()
    p.release(url)
    p.release(url)
    p.acquire()
    with pytest.raises(SlotPoolEmpty):
        p.acquire()


def test_stats_only_count_network_failures_when_asked():
    """`ok=None`（没跑完 / 被闸门拦下）既不算成功也不算失败。"""
    p = SlotPool(SLOTS)
    a = p.acquire()
    p.release(a, ok=True)
    b = p.acquire()
    p.release(b, ok=False, error="HTTP 500")
    c = p.acquire()
    p.release(c, ok=None)
    st = {s.url: s for s in p.stats()}
    assert st[a].ok == 1 and st[a].failed == 0
    assert st[b].failed == 1 and st[b].last_error == "HTTP 500"
    assert st[c].ok == 0 and st[c].failed == 0


def test_label_is_the_port():
    p = SlotPool(SLOTS)
    assert [s.label for s in p.stats()] == ["7901", "7902", "7903"]
    assert "7901" in p.describe()


def test_describe_flags_failed_slots():
    p = SlotPool(SLOTS[:1])
    u = p.acquire()
    p.release(u, ok=False, error="x")
    assert "✗" in p.describe()


def test_concurrent_acquire_never_hands_out_the_same_slot():
    """并发下同一个出口**绝不能**同时给两个人 —— 那等于没分出口。"""
    p = SlotPool(SLOTS, timeout=5)
    seen = []
    lock = threading.Lock()

    def worker():
        for _ in range(5):
            with p.slot() as url:
                with lock:
                    seen.append(url)
                time.sleep(0.005)

    ths = [threading.Thread(target=worker) for _ in range(6)]
    for t in ths:
        t.start()
    for t in ths:
        t.join(timeout=20)
    assert len(seen) == 30
    assert set(seen) == set(SLOTS)
