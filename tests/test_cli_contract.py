"""CLI 契约用例：**`run.py` 传给下游的每个关键字参数都必须真实存在**。

为什么需要它（这条用例是被一次误报逼出来的）
------------------------------------------
静态分析器在这台机器上会把 `run.py` 的 `from src import report` 解析到
**另一个项目**的 `src/report.py`（本仓库的父目录下有 3 个兄弟项目都用了
包名 `src`：Hark / Intern / FiimeOPC）。于是它稳定地报：

    run.py:241  No parameter named "slot_pool"
    run.py:242  No parameter named "redact_url"

而这两个参数在本项目里**确实存在**（`src/report.py` 的签名里有），
运行时也真的跑得通（10 批 50 个账号都走这个调用点）。也就是说，
这是一条**每次改 run.py 都会出现的假红线** —— 而假红线的代价不是噪音，
是让人学会忽略闸门（本项目对"闸门说假话"的容忍度是零）。

所以这里把那条静态检查**用正确的模块解析重做一遍**：
用 pytest 的 `pythonpath = ["."]`（根在**本项目**）import 真的模块，
再把 `run.py` 里那个调用点的关键字参数逐个对签名核一遍。
假的报不了，真的（比如有人改了 `render_batch_report` 的签名却忘了改调用点）
一定报得出来。
"""

import ast
import inspect
from pathlib import Path

from src import report
from src.pipeline import run_batch

ROOT = Path(__file__).resolve().parents[1]


def _run_py_tree() -> ast.Module:
    return ast.parse((ROOT / "run.py").read_text(encoding="utf-8"))


def _call_kwargs(tree: ast.Module, func_attr: str) -> set:
    """找调用，返回它用到的**关键字**参数名集合。

    ⚠ 两种形态都要认：`_report.render_batch_report(...)`（属性调用）与
      `run_batch(...)`（**裸名字**调用）。只认前者的话，后者会被报成
      "run.py 里找不到对 run_batch() 的调用" —— 一条因为**辅助函数太窄**
      而出现的假失败（实测踩到）。
    """
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        hit = (isinstance(fn, ast.Attribute) and fn.attr == func_attr) or (
            isinstance(fn, ast.Name) and fn.id == func_attr
        )
        if hit:
            return {kw.arg for kw in node.keywords if kw.arg}
    raise AssertionError(f"run.py 里找不到对 {func_attr}() 的调用")


def test_run_py_kwargs_match_render_batch_report():
    """🔴 这条就是那条假红线的**正确版本**：解析的是本项目的 report。"""
    tree = _run_py_tree()
    used = _call_kwargs(tree, "render_batch_report")
    accepted = set(inspect.signature(report.render_batch_report).parameters)
    unknown = used - accepted
    assert not unknown, (
        f"run.py 传了 report.render_batch_report 不认的参数：{sorted(unknown)}；"
        f"实际接受 {sorted(accepted)}"
    )
    # 反向也要看一眼：**仅关键字且无默认值**的参数必须都传到。
    # ⚠ 不能把 `records` / `written_path` / `wall_seconds` 也算进来 —— 它们是
    #   `POSITIONAL_OR_KEYWORD`，`run.py` 是**按位置**传的，不会出现在 `used` 里
    #   （实测：一开始把条件写成“无默认值”，当场报 'wall_seconds' 缺失）。
    required = {
        name
        for name, p in inspect.signature(report.render_batch_report).parameters.items()
        if p.kind is inspect.Parameter.KEYWORD_ONLY and p.default is inspect.Parameter.empty
    }
    assert required <= used, f"run.py 漏传了必填的关键字参数：{sorted(required - used)}"


def test_run_py_kwargs_match_run_batch():
    """同一类漂移在流水线入口也要能被抓住。"""
    tree = _run_py_tree()
    used = _call_kwargs(tree, "run_batch")
    accepted = set(inspect.signature(run_batch).parameters)
    assert not (used - accepted), f"run.py 传了 run_batch 不认的参数：{sorted(used - accepted)}"


def test_report_module_is_this_projects_report():
    """把"解析到哪个 report"这件事钉住 —— 假红线的根因就在这儿。

    ⚠ 这条断言在**本仓库的解析环境**里恒成立（pytest 的 pythonpath 是 "."）；
      它的价值是：哪天有人把项目结构改成能被误解析的样子，这里会先响。
    """
    assert Path(report.__file__).resolve().parent == (ROOT / "src").resolve()
    # 本项目的签名有这两个参数（另一个项目的没有 —— 那是误报的来源）。
    params = set(inspect.signature(report.render_batch_report).parameters)
    assert {"slot_pool", "redact_url"} <= params
