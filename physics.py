# -*- coding: utf-8 -*-
"""大肥鱼桌宠 —— 独立物理模块（纯 Python，不依赖 Qt / PySide6）。

包含三部分：
1. `SpringFollower`：拖拽时用阻尼弹簧追随鼠标。
2. `PhysicsBody` + `step_body`：甩抛/重力/弹跳/摩擦/停稳。
3. `screen_at` / `clamp_rect` / `resolve_pair`：多屏边界与宠物互撞。

所有接口都用普通数字 / 字典，方便单元测试。
"""

import math

# ===== 默认物理参数（可被 config.json 的 physics 字段覆盖） =====
DEFAULT_PHYSICS = {
    "gravity": 2400.0,        # 像素/秒²
    "elasticity": 0.55,       # 反弹系数 0~1
    "friction": 4.2,          # 每秒速度衰减系数（v *= exp(-friction*dt)）
    "power": 1.0,             # 甩抛总力度倍率
    "top_bounce": True,       # 顶部是否反弹（False 则贴顶停住）
    "pet_collision": False,   # 宠物互撞开关
    "spring_stiffness": 320.0,  # 拖拽弹簧刚度
    "spring_damping": 26.0,     # 拖拽弹簧阻尼
}

# 物理常量（内部使用，不进配置）
THROW_WINDOW = 0.10          # 甩抛取最近 100ms 的速度
STOP_SPEED = 26.0            # 小于这个速度视为停稳（像素/秒）
MAX_THROW_SPEED = 4200.0     # 甩抛速度上限，避免飞出宇宙
LAND_SQUASH_SPEED = 420.0    # 落地速度超过它才做 Q 弹挤压
AIR_DRAG = 0.25              # 空中水平阻尼（每秒）
SLEEP_FRAMES = 6             # 连续这么多帧几乎不动就休眠


def sanitize_physics(raw, defaults=None):
    """把外部传进来的 physics 字典校验成合法值；返回 (cfg, bad_keys)。"""
    base = dict(defaults or DEFAULT_PHYSICS)
    cfg = dict(base)
    bad = []
    if not isinstance(raw, dict):
        if raw is not None:
            bad.append("physics")
        return cfg, bad

    for key in ("gravity", "friction", "spring_stiffness", "spring_damping"):
        v = raw.get(key)
        if isinstance(v, (int, float)) and not isinstance(v, bool) and v >= 0:
            cfg[key] = float(v)
        elif v is not None:
            bad.append("physics.%s" % key)

    v = raw.get("elasticity")
    if isinstance(v, (int, float)) and not isinstance(v, bool) and 0.0 <= v <= 1.0:
        cfg["elasticity"] = float(v)
    elif v is not None:
        bad.append("physics.elasticity")

    v = raw.get("power")
    if isinstance(v, (int, float)) and not isinstance(v, bool) and 0.0 <= v <= 10.0:
        cfg["power"] = float(v)
    elif v is not None:
        bad.append("physics.power")

    for key in ("top_bounce", "pet_collision"):
        v = raw.get(key)
        if isinstance(v, bool):
            cfg[key] = v
        elif v is not None:
            bad.append("physics.%s" % key)

    return cfg, bad


# ===== 屏幕 / 边界 =====

def _as_rect(r):
    """接受 dict(x,y,w,h) 或 4 元组，返回 (x, y, w, h)。"""
    if isinstance(r, dict):
        return (float(r["x"]), float(r["y"]), float(r["w"]), float(r["h"]))
    return (float(r[0]), float(r[1]), float(r[2]), float(r[3]))


def screen_at(rects, cx, cy):
    """返回包含点 (cx, cy) 的屏幕矩形；都没有就返回最近的那个。

    rects 是各屏幕的 *工作区* 列表。屏幕之间可能有空洞，所以找不到包含点的时候
    按中心距离取最近的，保证总能落到某块屏上。
    """
    if not rects:
        return None
    best = None
    best_d = None
    for r in rects:
        x, y, w, h = _as_rect(r)
        if x <= cx < x + w and y <= cy < y + h:
            return (x, y, w, h)
        mx = x + w / 2.0
        my = y + h / 2.0
        d = (cx - mx) ** 2 + (cy - my) ** 2
        if best_d is None or d < best_d:
            best_d = d
            best = (x, y, w, h)
    return best


def clamp_rect(rect, w, h, allow_outside=False):
    """把窗口 (w,h) 限制在 rect 内；rect 小于窗口时贴左上。"""
    x, y, rw, rh = _as_rect(rect)
    max_x = x + rw - w
    max_y = y + rh - h
    return (x, y, max_x, max_y)


def band_x(rects, cy, h):
    """与给定竖向范围重叠的所有屏幕，其 x 方向的并集边界。

    这样横向甩抛可以穿过相邻屏幕，又不会飞出所有显示器。
    """
    els = [_as_rect(r) for r in rects or []]
    overlap = [r for r in els if not (r[1] + r[3] <= cy or r[1] >= cy + h)]
    if not overlap:
        overlap = els
    if not overlap:
        return (0.0, 1920.0)
    return (min(r[0] for r in overlap), max(r[0] + r[2] for r in overlap))


def band_y(rects, cx, w):
    """与给定横向范围重叠的所有屏幕，其 y 方向的并集边界（纵向跨屏用）。"""
    els = [_as_rect(r) for r in rects or []]
    overlap = [r for r in els if not (r[0] + r[2] <= cx or r[0] >= cx + w)]
    if not overlap:
        overlap = els
    if not overlap:
        return (0.0, 1080.0)
    return (min(r[1] for r in overlap), max(r[1] + r[3] for r in overlap))


class SpringFollower:
    """阻尼弹簧追随：拖拽时宠物平滑追上鼠标目标，而不是硬贴。"""

    def __init__(self, stiffness=320.0, damping=26.0):
        self.stiffness = float(stiffness)
        self.damping = float(damping)
        self.pos = [0.0, 0.0]
        self.vel = [0.0, 0.0]
        self.target = [0.0, 0.0]
        self.inited = False

    def start(self, pos, target=None):
        self.pos = [float(pos[0]), float(pos[1])]
        self.target = [float((target or pos)[0]), float((target or pos)[1])]
        self.vel = [0.0, 0.0]
        self.inited = True

    def set_target(self, target):
        self.target = [float(target[0]), float(target[1])]

    def step(self, dt):
        """推进一帧，返回当前位置。"""
        if not self.inited:
            return tuple(int(round(v)) for v in self.pos)
        dt = max(0.0005, min(0.05, float(dt)))
        for i in (0, 1):
            diff = self.target[i] - self.pos[i]
            acc = diff * self.stiffness - self.vel[i] * self.damping
            self.vel[i] += acc * dt
        for i in (0, 1):
            self.pos[i] += self.vel[i] * dt
        return tuple(int(round(v)) for v in self.pos)

    def settled(self, eps=0.6):
        return (abs(self.target[0] - self.pos[0]) < eps and
                abs(self.target[1] - self.pos[1]) < eps and
                abs(self.vel[0]) < eps * 12 and abs(self.vel[1]) < eps * 12)


class WobbleSpring:
    """落地/撞墙时的形变弹簧：给 Q 弹挤压用，返回 0~1 的形变量。"""

    def __init__(self, stiffness=180.0, damping=12.0):
        self.stiffness = float(stiffness)
        self.damping = float(damping)
        self.value = 0.0
        self.vel = 0.0

    def kick(self, amount=1.0):
        # 用 sqrt(k) 归一化初速度，使振幅≈amount（不随刚度变化）
        self.vel -= abs(float(amount)) * math.sqrt(max(1.0, self.stiffness))

    def step(self, dt):
        dt = max(0.0005, min(0.05, float(dt)))
        acc = -self.value * self.stiffness - self.vel * self.damping
        self.vel += acc * dt
        self.value += self.vel * dt
        if abs(self.value) < 1e-4 and abs(self.vel) < 1e-3:
            self.value = 0.0
            self.vel = 0.0
        return self.value

    def reset(self):
        self.value = 0.0
        self.vel = 0.0


class VelocityTracker:
    """记录最近 N 秒的位置样本，用来算甩抛速度。"""

    def __init__(self, window=THROW_WINDOW):
        self.window = float(window)
        self.samples = []

    def clear(self):
        self.samples = []

    def add(self, x, y, t):
        self.samples.append((float(t), float(x), float(y)))
        cut = float(t) - self.window
        self.samples = [s for s in self.samples if s[0] >= cut]

    def velocity(self, now=None):
        """返回最近 window 秒内的平均速度 (vx, vy)，单位像素/秒。"""
        if len(self.samples) < 2:
            return (0.0, 0.0)
        t_end = float(now if now is not None else self.samples[-1][0])
        cut = t_end - self.window
        win = [s for s in self.samples if s[0] >= cut]
        if len(win) < 2:
            win = self.samples[-2:]
        t0, x0, y0 = win[0]
        t1, x1, y1 = win[-1]
        dt = t1 - t0
        if dt <= 1e-6:
            return (0.0, 0.0)
        return ((x1 - x0) / dt, (y1 - y0) / dt)


class PhysicsBody:
    """一个可甩抛的方块：位置是窗口左上角，带速度与休眠状态。"""

    def __init__(self, x=0.0, y=0.0, vx=0.0, vy=0.0):
        self.x = float(x)
        self.y = float(y)
        self.vx = float(vx)
        self.vy = float(vy)
        self.asleep = True
        self._idle = 0
        self.on_ground = False
        self.last_impact = 0.0   # 上一次撞地/撞墙的速度，用于触发挤压

    def place(self, x, y):
        self.x = float(x)
        self.y = float(y)
        self.vx = 0.0
        self.vy = 0.0
        self.asleep = True
        self._idle = 0
        self.on_ground = False
        self.last_impact = 0.0

    def throw(self, vx, vy, power=1.0):
        self.vx = max(-MAX_THROW_SPEED, min(MAX_THROW_SPEED, float(vx) * float(power)))
        self.vy = max(-MAX_THROW_SPEED, min(MAX_THROW_SPEED, float(vy) * float(power)))
        self.asleep = False
        self._idle = 0
        self.last_impact = 0.0


def step_body(body, dt, w, h, rects, physics=None):
    """推进一帧物理。返回 dict：
    moved / impact（撞击速度）/ landed（是否刚落地）/ bounced_x / bounced_y。

    rectangles 为各屏工作区列表；跨屏时自动切换到新屏的边界。
    """
    ph = physics or DEFAULT_PHYSICS
    g = float(ph.get("gravity", DEFAULT_PHYSICS["gravity"]))
    elas = float(ph.get("elasticity", DEFAULT_PHYSICS["elasticity"]))
    fric = float(ph.get("friction", DEFAULT_PHYSICS["friction"]))
    top_bounce = bool(ph.get("top_bounce", True))

    info = {"moved": False, "impact": 0.0, "landed": False,
            "bounced_x": False, "bounced_y": False}
    if body.asleep:
        return info

    dt = max(0.0005, min(0.05, float(dt)))
    body.vy += g * dt
    body.vx *= math.exp(-AIR_DRAG * dt)

    nx = body.x + body.vx * dt
    ny = body.y + body.vy * dt

    # 用窗口中心选屏（跨屏抛掷时自动换边界）
    cx = nx + w / 2.0
    cy = ny + h / 2.0
    rect = screen_at(rects, cx, cy)
    if rect is None:
        rect = (0.0, 0.0, 1920.0, 1080.0)
    rx, ry, rw, rh = _as_rect(rect)
    # 横向用与当前竖带重叠的所有屏并集（可跨屏），纵向同理
    bx0, bx1 = band_x(rects, ny, h)
    by0, by1 = band_y(rects, nx, w)
    left = min(rx, bx0)
    right = max(rx + rw, bx1) - w
    top = min(ry, by0)
    bottom = max(ry + rh, by1) - h
    if right < left:
        right = left
    if bottom < top:
        bottom = top

    landed_now = False
    # 水平
    if nx < left:
        nx = left
        if body.vx < 0:
            speed = abs(body.vx)
            body.vx = speed * elas
            info["bounced_x"] = True
            info["impact"] = max(info["impact"], speed)
    elif nx > right:
        nx = right
        if body.vx > 0:
            speed = abs(body.vx)
            body.vx = -speed * elas
            info["bounced_x"] = True
            info["impact"] = max(info["impact"], speed)

    # 垂直
    if ny < top:
        ny = top
        if body.vy < 0:
            speed = abs(body.vy)
            if top_bounce:
                body.vy = speed * elas
                info["bounced_y"] = True
                info["impact"] = max(info["impact"], speed)
            else:
                body.vy = 0.0
    elif ny > bottom:
        ny = bottom
        if body.vy > 0:
            speed = abs(body.vy)
            body.vy = -speed * elas
            info["impact"] = max(info["impact"], speed)
            info["bounced_y"] = True
            if speed * elas < LAND_SQUASH_SPEED * 0.35:
                body.vy = 0.0
                landed_now = True

    # 落地摩擦
    if ny >= bottom - 0.5 and abs(body.vy) < 1.0:
        body.vy = 0.0
        body.vx *= math.exp(-fric * dt)
        body.on_ground = True
        if abs(body.vx) < STOP_SPEED:
            body.vx = 0.0
            landed_now = True
    else:
        body.on_ground = False

    moved = (abs(nx - body.x) > 1e-6 or abs(ny - body.y) > 1e-6)
    body.x = nx
    body.y = ny
    info["moved"] = moved
    if info["impact"] > 0:
        body.last_impact = info["impact"]
    if landed_now:
        info["landed"] = True
        info["impact"] = max(info["impact"], body.last_impact)

    # 停稳判定
    if body.on_ground and abs(body.vx) < STOP_SPEED and abs(body.vy) < 1.0:
        body._idle += 1
        if body._idle >= SLEEP_FRAMES:
            body.asleep = True
            body.vx = 0.0
            body.vy = 0.0
            info["landed"] = True
    else:
        body._idle = 0
    return info


# ===== 宠物互撞 =====

def resolve_pair(a, b, w, h, elasticity=0.6, min_push=2.0):
    """两个宠物矩形重叠时互相弹开。

    a / b 形如 {"x","y","vx","vy"}（就地修改）。返回是否发生了碰撞。
    """
    ax, ay = float(a["x"]), float(a["y"])
    bx, by = float(b["x"]), float(b["y"])

    ox = min(ax + w, bx + w) - max(ax, bx)
    oy = min(ay + h, by + h) - max(ay, by)
    if ox <= 0 or oy <= 0:
        return False

    # 沿重叠更小的轴分离，推开的距离各一半
    if ox < oy:
        push = max(min_push, ox / 2.0)
        sign = 1.0 if ax <= bx else -1.0
        a["x"] = ax - push * sign
        b["x"] = bx + push * sign
        avx = float(a.get("vx", 0.0))
        bvx = float(b.get("vx", 0.0))
        rel = avx - bvx
        a["vx"] = bvx * elasticity - sign * abs(rel) * 0.5 - 60.0 * sign
        b["vx"] = avx * elasticity + sign * abs(rel) * 0.5 + 60.0 * sign
    else:
        push = max(min_push, oy / 2.0)
        sign = 1.0 if ay <= by else -1.0
        a["y"] = ay - push * sign
        b["y"] = by + push * sign
        avy = float(a.get("vy", 0.0))
        bvy = float(b.get("vy", 0.0))
        rel = avy - bvy
        a["vy"] = bvy * elasticity - sign * abs(rel) * 0.5 - 60.0 * sign
        b["vy"] = avy * elasticity + sign * abs(rel) * 0.5 + 60.0 * sign

    for body in (a, b):
        if "asleep" in body:
            body["asleep"] = False
        if "_idle" in body:
            body["_idle"] = 0
    return True
