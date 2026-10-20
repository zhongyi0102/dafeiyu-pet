# -*- coding: utf-8 -*-
"""audit_all.py —— 批量审计 sprites/anim 下所有动作，输出一张可排序的质量表。

判定「好/差」的标准（客观、可复现）：
  闭合度 loop    首尾帧差异，越小越能循环（<0.30 好）
  节奏方差 rhythm 帧差异的变异系数，越小越均匀（<0.80 好）
  差异比 ratio   最大帧差异 / 最小帧差异，大 = 有跳帧（<8 好）
  脚底漂移 drift 最大最小脚底线差（<=3px 好）

综合结论：
  优秀 = 四项全好
  可用 = 闭合度 & 节奏方差达标（轻微跳帧可接受）
  较差 = 闭合度或节奏方差明显不达标（建议删除或重转）
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from anim_audit import audit, collect  # noqa: E402


def grade(r):
    if not r:
        return "无效", 0
    loop = r["loop"]
    rhythm = r["rhythm"]
    diffs = r["diffs"]
    ratio = (max(diffs) / max(min(diffs), 1e-4)) if diffs else 99
    drift = max(r["bottoms"]) - min(r["bottoms"])

    score = 0
    if loop < 0.30:
        score += 1
    if rhythm < 0.80:
        score += 1
    if ratio < 8:
        score += 1
    if drift <= 3:
        score += 1

    if score == 4:
        return "优秀", score
    if score == 3 and loop < 0.40 and rhythm < 0.90:
        return "可用", score
    return "较差", score


def main(argv):
    root = argv[1] if len(argv) > 1 else os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "sprites", "anim")
    rows = []
    for name in sorted(os.listdir(root)):
        d = os.path.join(root, name)
        if not os.path.isdir(d):
            continue
        paths = collect([d])
        if len(paths) < 2:
            continue
        r = audit(paths, name=name, verbose=False)
        if not r:
            continue
        loop = r["loop"]
        rhythm = r["rhythm"]
        diffs = r["diffs"]
        ratio = (max(diffs) / max(min(diffs), 1e-4)) if diffs else 99
        drift = max(r["bottoms"]) - min(r["bottoms"])
        g, score = grade(r)
        rows.append((g, score, name, r["n"], loop, rhythm, ratio, drift))

    order = {"优秀": 0, "可用": 1, "较差": 2, "无效": 3}
    rows.sort(key=lambda x: (order.get(x[0], 9), -x[1], x[4]))

    print("%-6s %-16s %4s %7s %7s %7s %6s" % ("评级", "动作", "帧", "闭合", "节奏", "差异比", "脚漂"))
    print("-" * 66)
    for g, score, name, n, loop, rhythm, ratio, drift in rows:
        print("%-6s %-16s %4d %7.3f %7.2f %7.1f %6d"
              % (g, name, n, loop, rhythm, ratio, drift))

    good = [r[2] for r in rows if r[0] in ("优秀", "可用")]
    bad = [r[2] for r in rows if r[0] == "较差"]
    print()
    print("优秀/可用: %d 个" % len(good))
    print("   " + " ".join(good))
    print("较差(建议删或重转): %d 个" % len(bad))
    print("   " + " ".join(bad) if bad else "   （无）")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
