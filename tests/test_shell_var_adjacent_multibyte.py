"""shell 脚本里 `$VAR` 紧跟全角字符 ⇒ bash 3.2 把首字节读进变量名。

**这是可复现的生产缺陷，不是风格洁癖。** 2026-11-14 实测：

    tools/refresh_platform.sh: line 59: POD_LABEL?: unbound variable

`set -u` 之下这行让脚本当场 abort，而那一行本来要打印的是
「找不到 platform pod（label …）」——于是「pod 正在重启」这个它专门要报告的
情况，变成了一个看不懂的报错，且退出码从 2 变成 1（`--dry-run` 与失败的区分丢了）。

三处命中全在**失败分支**上，这是它长期没被发现的原因：平时不执行。
而且 macOS 自带的 `/bin/bash` 是 3.2.57，本机没有 `/opt/homebrew/bin/bash`，
launchd 的 PATH 也只解析到 3.2 ⇒ **定时作业跑的就是会中招的那个解释器**。

为什么扫全仓而不是只扫这一个文件：同一条链上还有别的 `.sh`，而这类缺陷的形态是
「出错时才出错」——它的代价不在这一次，在**下一次真出事时看不见真消息**。
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

#: `$NAME` 后面直接跟非 ASCII 字符（全角括号、全角逗号、中文……）。
_BAD = re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*)(?=[^\x00-\x7f])")


def _scripts() -> list[Path]:
    return [p for p in sorted(ROOT.rglob("*.sh")) if ".git" not in p.parts]


def test_shell_scripts_exist():
    """先证明有对象：一份都没扫到时，下面的循环永远绿。"""
    assert _scripts(), "没有扫到任何 .sh——判据等于没跑"


def test_no_var_directly_adjacent_to_a_multibyte_char():
    hits = []
    for path in _scripts():
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if line.lstrip().startswith("#"):
                continue  # 注释里写反例是合法的（本仓正有这么一段说明）
            for m in _BAD.finditer(line):
                hits.append(f"{path.relative_to(ROOT)}:{lineno}: ${m.group(1)}… "
                            f"→ 写成 ${{{m.group(1)}}}")
    assert not hits, (
        "这些位置 `$VAR` 后面直接跟了全角字符：bash 3.2 会把首字节读进变量名，"
        "set -u 下直接 abort（真消息丢失、退出码也错）:\n  " + "\n  ".join(hits)
    )
