"""依赖面契约：**测试链上允许出现哪些第三方包**。

为什么需要它（这条契约原来只是注释里的假设）
============================================
CI 的 test job 装的是 `pytest` + `requests` 两样。而"测试真的只需要这两样"这件事，
在很长一段时间里**只写在注释里** —— 于是本地全绿、CI 红，因为本地早就装过别的东西。

> 同一个坑在 Intern-Register-Tool 那边炸过 **3 次**：它一度以为"测试链上零第三方
> 依赖"，实测发现 `tests/*` → `src.pipeline` → `src.discovery` → `import requests`，
> 而 `playwright` 确实不在收集路径上（那是函数体内的延迟 import）。
> **一半的假设是对的**，所以它一直没被发现 —— 直到 CI 连续红。

本项目的依赖面更窄（运行依赖**只有 requests**，整链零浏览器），所以这条边界
既好守、也值得守：它是"零浏览器"这个卖点唯一可执行的证据。

判据
----
`tests/test_dependency_surface.py` 把仓库里每个 `.py` 的 import 抽出来，逐个分类：

    stdlib（`sys.stdlib_module_names`） / 本仓库自己的包 / 白名单 {"requests"} / 其余

**"其余"一律失败。** 于是"某人加了一个依赖"会当场变红，而不是等 CI。

⚠ 三处必须同步（改一处就要改全）：
    1. 本文件的 `ALLOWED_THIRD_PARTY`
    2. `.github/workflows/ci.yml` 的"安装测试依赖"那一步
    3. `pyproject.toml` 的 `dependencies` / `optional-dependencies.dev`
"""

import ast
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

# 运行期唯一允许的第三方包。⚠ 与 CI / pyproject 三处同步。
ALLOWED_RUNTIME = {"requests"}

# 测试额外允许 pytest。
#
# 🔴 两个白名单**分开**是这条契约的重点，而不是洁癖：`src/`、`common/`、`tools/`、
#    `run.py` 一旦多出一个依赖，“零浏览器 + 只依赖 requests”这个卖点就没了，
#    而 `tests/` 用 pytest 天经地义。合成一个白名单就只能取并集 —— 那时
#    “运行期多了一个依赖”再也拦不住。
ALLOWED_TESTS = ALLOWED_RUNTIME | {"pytest"}

# 本仓库自己的顶层包/模块（不是第三方）。
LOCAL_TOP_LEVEL = {
    "src",
    "common",
    # tools/ 的脚本靠这两个副作用导入把仓库根与 tools/ 加进 sys.path。
    "_bootstrap",
    "_path",
}


def _tool_module_names() -> set:
    """`tools/**/*.py` 的模块名（不含 `.py`）。

    🔴 为什么从**文件系统**推，而不是写一份名单：早前这里是硬编码的一串工具名，
      而“新加一个工具就得记得回来补一行”是必然会漏的 —— 实测：加完
      `prune_ledger.py` 后这个闸门当场报 `prune_ledger` 是第三方包。
      那种失败还算好（它红得及时），但它把人引向“去补名单”而不是“去检查依赖”。
      从目录推就不会漂：**新增工具自动被认作本地**，
      而闸门的真正职责（拦住真正的第三方包）一点没减弱。
    """
    return {p.stem for p in (ROOT / "tools").rglob("*.py")}

# 这些模块是"便宜层"：不碰网络、只依赖 stdlib + 本仓库。
# 🔴 为什么单独钉住它们：`tests/test_report.py` / `test_ledger_merge.py` 只 import
#    这一层，于是那几十条用例能在**不装 requests** 的环境里跑。一旦有人往
#    report/ledger/quota 里塞一个 `import requests`，那些用例就会悄悄变重 ——
#    而"悄悄"是这个项目最不能接受的一种失败。
CHEAP_MODULES = ("src/report.py", "src/ledger.py", "src/quota.py", "src/redact.py", "src/fsutil.py")


def _iter_py_files():
    for sub in ("src", "common", "tools", "tests"):
        yield from sorted((ROOT / sub).rglob("*.py"))
    yield ROOT / "run.py"


def _top_level_imports(path: Path) -> set:
    """文件里 import 的**顶层**名字（`from a.b import c` → `a`）。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out: set = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                out.add(a.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            # `from . import x` / `from .hark import y` → level>0，是本仓库内部
            if node.level == 0 and node.module:
                out.add(node.module.split(".")[0])
    return out


def _third_party(names: set) -> set:
    local = LOCAL_TOP_LEVEL | _tool_module_names()
    return {
        n
        for n in names
        if n not in sys.stdlib_module_names and n not in local and not n.startswith("_")
    }


def _allowed_for(path: Path) -> set:
    """按路径选白名单：`tests/` 下允许 pytest，其余地方不允许。"""
    rel = path.relative_to(ROOT)
    return ALLOWED_TESTS if rel.parts and rel.parts[0] == "tests" else ALLOWED_RUNTIME


def test_only_allowlisted_third_party_anywhere():
    """全仓库（含 tools/ 与 tests/）不得出现白名单以外的第三方包。"""
    offenders = {}
    for p in _iter_py_files():
        bad = _third_party(_top_level_imports(p)) - _allowed_for(p)
        if bad:
            offenders[str(p.relative_to(ROOT))] = sorted(bad)
    assert not offenders, (
        "出现了白名单之外的第三方依赖：\n  "
        + "\n  ".join(f"{k}: {v}" for k, v in offenders.items())
        + f"\n  运行期允许 {sorted(ALLOWED_RUNTIME)}、测试额外允许 pytest；"
        "要加依赖必须同时改 CI 的安装步骤与 pyproject（三处同步）。"
    )


@pytest.mark.parametrize("rel", CHEAP_MODULES)
def test_cheap_layer_does_not_pull_network_deps(rel):
    """🔴 便宜层不得依赖 requests —— 那几十条离线用例靠这条边界保持廉价。"""
    imports = _top_level_imports(ROOT / rel)
    assert "requests" not in imports, f"{rel} 引入了 requests（它应当是 stdlib-only 的）"
    assert not _third_party(imports), f"{rel} 引入了第三方包：{sorted(_third_party(imports))}"


def test_requirements_matches_the_allowlist():
    """`requirements.txt` 是**唯一**的运行依赖清单，必须与运行白名单一致。"""
    lines = [
        ln.strip()
        for ln in (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines()
        if ln.strip() and not ln.startswith("#")
    ]
    names = {ln.split(">=")[0].split("==")[0].strip().lower() for ln in lines}
    assert names == ALLOWED_RUNTIME, (
        f"requirements.txt 与 ALLOWED_RUNTIME 不一致：{sorted(names)} vs {sorted(ALLOWED_RUNTIME)}"
    )


def test_pyproject_runtime_deps_match_too():
    """第三处同步点：`pyproject.toml` 的 `dependencies`。

    ⚠ 用文本扫描而不是 `tomllib`：本用例要能在 3.11 之前/之后都跑，
      而且这里要的是"有没有多出一个名字"，不是完整的 TOML 语义。
    """
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    head = text.split("[project.optional-dependencies]")[0]
    deps_block = head.split("dependencies = [")[1].split("]")[0]
    names = set()
    for ln in deps_block.splitlines():
        ln = ln.strip().strip('",')
        if ln and not ln.startswith("#"):
            names.add(ln.split(">=")[0].split("==")[0].strip().lower())
    assert names == ALLOWED_RUNTIME, (
        f"pyproject 的运行依赖与白名单不一致：{sorted(names)} vs {sorted(ALLOWED_RUNTIME)}"
    )


def test_dev_deps_are_only_pytest_and_ruff():
    """开发依赖也要有边界：多一个就是多一份供应链风险，而且 CI 要跟着改。"""
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    block = text.split("[project.optional-dependencies]")[1].split("[")[0]
    names = set()
    for ln in block.splitlines():
        ln = ln.strip().strip('",')
        if ln and not ln.startswith("#") and "=" not in ln:
            names.add(ln.split(">=")[0].split("<")[0].strip().lower())
    assert names <= {"pytest", "ruff"}, f"多出来的开发依赖：{sorted(names - {'pytest', 'ruff'})}"
