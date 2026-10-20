# -*- coding: utf-8 -*-
"""physics.py 单元测试（纯 Python，不依赖 Qt）。直接跑：python test_physics.py"""

import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import physics as P

ok = []


def check(name, cond, extra=""):
    ok.append((name, bool(cond)))
    print(("PASS " if cond else "FAIL ") + name + ((" | " + str(extra)) if extra else ""))


SCREEN = (0, 0, 1920, 1040)      # 工作区（扣掉任务栏）
SCREEN2 = (1920, 0, 1280, 1000)  # 右侧副屏
W, H = 218, 311                   # 宠物窗口尺寸

print("=== 参数校验 ===")
cfg, bad = P.sanitize_physics(None)
check("缺省用默认值", cfg["gravity"] == P.DEFAULT_PHYSICS["gravity"] and bad == [])
cfg2, bad2 = P.sanitize_physics({"gravity": -5, "elasticity": 3.0, "power": 99,
                                 "top_bounce": "yes", "pet_collision": 1,
                                 "friction": "x", "junk": 1})
check("非法值回落默认", cfg2["gravity"] == P.DEFAULT_PHYSICS["gravity"]
      and cfg2["elasticity"] == P.DEFAULT_PHYSICS["elasticity"]
      and cfg2["power"] == P.DEFAULT_PHYSICS["power"]
      and cfg2["top_bounce"] is True and cfg2["pet_collision"] is False)
check("非法字段被记录", "physics.gravity" in bad2 and "physics.elasticity" in bad2
      and "physics.power" in bad2 and "physics.top_bounce" in bad2,
      bad2)
cfg3, bad3 = P.sanitize_physics({"gravity": 500, "elasticity": 0.8, "power": 1.5,
                                 "top_bounce": False, "pet_collision": True,
                                 "friction": 2.0})
check("合法值生效", cfg3["gravity"] == 500 and cfg3["elasticity"] == 0.8
      and cfg3["power"] == 1.5 and cfg3["top_bounce"] is False
      and cfg3["pet_collision"] is True, cfg3)
check("合法值无告警", bad3 == [], bad3)

print("=== 屏幕选择 ===")
rects = [SCREEN, SCREEN2]
check("点在第一屏", P.screen_at(rects, 100, 100) == SCREEN)
check("点在第二屏", P.screen_at(rects, 2000, 100) == SCREEN2)
# 屏幕之间有空洞（x=1900 在第二屏外但离它最近）
sel = P.screen_at([(0, 0, 1920, 1040), (2000, 0, 1280, 1000)], 1950, 500)
check("空洞里选最近的屏", sel in ((0, 0, 1920, 1040), (2000, 0, 1280, 1000)), sel)
check("空列表返回 None", P.screen_at([], 0, 0) is None)

print("=== 阻尼弹簧拖拽 ===")
sp = P.SpringFollower(stiffness=320, damping=26)
sp.start((0, 0), (0, 0))
sp.set_target((300, 200))
positions = [sp.step(1 / 50.0) for _ in range(120)]
check("弹簧收敛到目标", abs(positions[-1][0] - 300) <= 2 and abs(positions[-1][1] - 200) <= 2,
      positions[-1])
check("中途有滞后（弹簧而非硬贴）", positions[0][0] < 60, positions[0])
dists = []
for p in positions:
    dists.append(abs(p[0] - 300) + abs(p[1] - 200))
check("距离单调下降到很小", dists[-1] < 4 and max(dists) == dists[0], (dists[0], dists[-1]))
check("settled 最终为真", sp.settled(eps=3.0))

print("=== 甩抛速度采样 ===")
vt = P.VelocityTracker(0.10)
for i in range(11):          # 100ms 内移动 100px
    vt.add(i * 10, 0, i * 0.01)
vx, vy = vt.velocity(0.10)
check("100ms 内 100px -> 1000px/s", 900 < abs(vx) < 1100, (round(vx, 1), round(vy, 1)))
vt.clear()
vt.add(0, 0, 0.0)
check("样本不足速度 0", vt.velocity() == (0.0, 0.0))
vt2 = P.VelocityTracker(0.10)
vt2.add(0, 0, 0.0)
vt2.add(50, 0, 0.5)     # 超出窗口的旧样本
vt2.add(60, 0, 0.52)
vx2, _ = vt2.velocity(0.52)
check("超出 100ms 的旧样本被丢弃", vx2 > 400, vx2)

print("=== 重力 / 反弹 / 摩擦 / 停稳 ===")
body = P.PhysicsBody(800, 100)
body.throw(0, 0)
body.asleep = False
impacts = []
for _ in range(400):
    info = P.step_body(body, 1 / 50.0, W, H, [SCREEN])
    if info["impact"] > 0:
        impacts.append(info["impact"])
    if body.asleep:
        break
check("受重力下落并停在地面", abs((body.y + H) - SCREEN[3]) < 2, (body.y, body.y + H))
check("最终停稳休眠", body.asleep is True)
check("发生过反弹", len(impacts) >= 1, len(impacts))
check("落点水平不下滑", abs(body.vx) < 1.0, body.vx)

# 反弹高度逐次降低
b2 = P.PhysicsBody(800, 0)
b2.throw(0, 0)
peak = []
last_y = b2.y
rising = False
for _ in range(600):
    info = P.step_body(b2, 1 / 60.0, W, H, [SCREEN])
    if b2.vy < 0:
        rising = True
    elif rising:
        peak.append(b2.y)
        rising = False
    if b2.asleep:
        break
check("弹跳高度逐步衰减(y 越大越低)", len(peak) >= 2 and peak[0] < peak[1], peak[:3])

# 甩抛水平位移
b3 = P.PhysicsBody(200, 200)
b3.throw(1500, -300, power=1.0)
x0 = b3.x
for _ in range(200):
    P.step_body(b3, 1 / 50.0, W, H, [SCREEN])
    if b3.asleep:
        break
check("甩抛后向右移动", b3.x > x0 + 50, (x0, b3.x))
check("甩抛后没有飞出屏幕", b3.x + W <= SCREEN[0] + SCREEN[2] + 1, b3.x + W)

# 力度倍率
b4 = P.PhysicsBody(200, 200)
b4.throw(1000, 0, power=2.0)
check("power 倍率生效", b4.vx > 1900, b4.vx)

# 顶部反弹开关
b5 = P.PhysicsBody(500, 5)
b5.throw(0, -2000)
top_hits = 0
for _ in range(60):
    info = P.step_body(b5, 1 / 50.0, W, H, [SCREEN], dict(P.DEFAULT_PHYSICS, top_bounce=True))
    if info["bounced_y"]:
        top_hits += 1
        break
check("顶部反弹开启时会弹", top_hits >= 1 and b5.y >= SCREEN[1] - 1, b5.y)

b6 = P.PhysicsBody(500, 5)
b6.throw(0, -2000)
for _ in range(30):
    P.step_body(b6, 1 / 50.0, W, H, [SCREEN], dict(P.DEFAULT_PHYSICS, top_bounce=False))
check("顶部反弹关闭时贴顶停住", b6.y >= SCREEN[1] - 1 and b6.vy >= -1, (b6.y, b6.vy))

# 小速度原地放下
b7 = P.PhysicsBody(700, 700)
b7.asleep = False
info = P.step_body(b7, 1 / 50.0, W, H, [SCREEN])
check("很小速度不会乱飞", abs(b7.vx) < 1)

print("=== 跨屏 ===")
b8 = P.PhysicsBody(1700, 300)
b8.throw(3000, -100)
visited = set()
for _ in range(600):
    P.step_body(b8, 1 / 50.0, W, H, [SCREEN, SCREEN2])
    visited.add(P.screen_at([SCREEN, SCREEN2], b8.x + W / 2, b8.y + H / 2))
    if b8.asleep:
        break
check("跨屏抛掷途中进入过第二屏", SCREEN2 in visited, sorted(visited))
final = P.screen_at([SCREEN, SCREEN2], b8.x + W / 2, b8.y + H / 2)
check("跨屏后最终停在某个屏内", final in (SCREEN, SCREEN2), (round(b8.x), final))
check("跨屏后仍在最终屏边界内",
      b8.x + W <= final[0] + final[2] + 1 and b8.y + H <= final[1] + final[3] + 1,
      (round(b8.x + W), round(b8.y + H)))
check("跨屏后停稳", b8.asleep is True)

# 横向并集边界：横跨两屏时不能在第一屏右边缘就被挡住
bx0, bx1 = P.band_x([SCREEN, SCREEN2], 300, H)
check("横向并集覆盖两屏", bx0 == 0 and bx1 == 3200, (bx0, bx1))
by0, by1 = P.band_y([SCREEN, SCREEN2], 1800, W)
check("纵向并集取重叠屏", by0 == 0 and by1 == 1040, (by0, by1))

# 不同 DPI 的屏幕（尺寸不同）也能正确夹取
tall = (0, 0, 2560, 1400)
b9 = P.PhysicsBody(2000, 1000)
b9.throw(900, 0)
for _ in range(300):
    P.step_body(b9, 1 / 50.0, W, H, [tall])
    if b9.asleep:
        break
check("高 DPI 大屏也在边界内", b9.x + W <= tall[2] + 1 and b9.y + H <= tall[3] + 1,
      (round(b9.x + W), round(b9.y + H)))

print("=== 宠物互撞 ===")
a = {"x": 100, "y": 100, "vx": 0, "vy": 0, "asleep": True, "_idle": 5}
b = {"x": 120, "y": 100, "vx": 0, "vy": 0, "asleep": True, "_idle": 5}
hit = P.resolve_pair(a, b, W, H, elasticity=0.6)
check("重叠时判定碰撞", hit is True)
check("碰撞后被推开", abs(a["x"] - b["x"]) >= W - 1, (a["x"], b["x"]))
check("碰撞后唤醒", a["asleep"] is False and b["asleep"] is False)
check("碰撞后获得反向速度", a["vx"] * b["vx"] < 0 or abs(a["vx"]) > 1, (a["vx"], b["vx"]))

a2 = {"x": 100, "y": 100, "vx": 0, "vy": 0}
b2 = {"x": 600, "y": 100, "vx": 0, "vy": 0}
check("不重叠不处理", P.resolve_pair(a2, b2, W, H) is False)
check("不重叠位置不变", a2["x"] == 100 and b2["x"] == 600)

# 垂直方向重叠更小时沿 y 分离
a3 = {"x": 100, "y": 100, "vx": 0, "vy": 0}
b3 = {"x": 105, "y": 130, "vx": 0, "vy": 0}
P.resolve_pair(a3, b3, W, H)
# 横向重叠 213 / 纵向重叠 281 -> 沿横向分离更省
check("重叠较小轴分离", abs(a3["x"] - b3["x"]) >= W - 1, (a3["x"], b3["x"]))
a4 = {"x": 100, "y": 100, "vx": 0, "vy": 0}
b4 = {"x": 160, "y": 390, "vx": 0, "vy": 0}
P.resolve_pair(a4, b4, W, H)
check("竖向重叠更小时上下弹开", abs(a4["y"] - b4["y"]) >= H - 1, (a4["y"], b4["y"]))

print("=== 挤压弹簧 ===")
ws = P.WobbleSpring()
ws.kick(1.0)
vals = [ws.step(1 / 50.0) for _ in range(120)]
check("挤压有非零形变", max(abs(v) for v in vals) > 0.05, round(max(abs(v) for v in vals), 3))
check("挤压最终归零", abs(vals[-1]) < 1e-3, vals[-1])
ws.reset()
check("reset 后归零", ws.value == 0.0 and ws.vel == 0.0)

print("\nRESULT:", sum(1 for _, v in ok if v), "/", len(ok))
print("FAILED:", [n for n, v in ok if not v] or "none")
