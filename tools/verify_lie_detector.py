# -*- coding: utf-8 -*-
"""
verify_lie_detector — 测谎仪自动应答（星星跟随版）的离线自检，进 CI

覆盖四件事，全部不依赖游戏/驱动/窗口，可在 CI 裸跑：
  1. 纯函数：帧差掩码 / 连通域质心 / 最近邻挑选 / 弹窗模板匹配
  2. 合成帧全链路：深色背景 + 静态星星 + 一颗逐帧「变透明」（或移动）的目标星
     → 组装守卫 → 逐步推进时间，断言「检测 → 观察 → 锁定 → 跟随坐标 →
     弹窗消失 → 恢复」整条链；跟随坐标必须落在目标星身上
  3. 失败路径：星星区毫无变化 → observe_timeout → 转 FAILED 叫人（alert 被调）；
     弹窗消失后能从 FAILED 恢复 idle；hold 按住被正确松开
  4. 前台守卫：游戏不在前台时**不移动鼠标**；回前台后继续跟
     + 配置默认值 + 引擎接线（run_once 挂守卫、kb.suppressed、move_to/hold hooks）

合成帧的做法：程序画五角星（fillPoly，10 顶点）—— 形状与游戏 UI 不同
没有关系：帧差法找的是「帧间在变的区域」，验证的是差分管线 + 状态机本身，
不依赖星星长什么样。这正好也是帧差法在真机上的优势。
"""
import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ── Windows 模块 stub（macOS/Linux 上 import 会经 common 顶层 import win32*；
#    纯函数测试用不到它们，占位即可）───────────────────────────────────
for _mod in ('win32gui', 'win32con', 'win32api', 'win32process'):
    if _mod not in sys.modules:
        sys.modules[_mod] = types.ModuleType(_mod)

import cv2  # noqa: E402
import numpy as np  # noqa: E402
import yaml  # noqa: E402

from src.utils.lie_detector import (                       # noqa: E402
    LieDetectorGuard, make_tpl_mask, match_template_best,
    frame_diff_mask, blob_centroids, pick_nearest_blob,
    load_geo, create_guard)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FAILS = []


def check(name, ok, detail=""):
    tag = "PASS" if ok else "FAIL"
    line = f"[{tag}] {name}"
    if detail:
        line += f" —— {detail}"
    print(line)
    if not ok:
        FAILS.append(name)


# ───────────────────────────────────────────────────────── 1. 纯函数

def test_pure_functions():
    # 帧差：一块在变、其余静止
    a = np.zeros((60, 80), dtype=np.uint8)
    b = a.copy()
    b[20:30, 40:52] = 120                                # 这块"变了"
    mask = frame_diff_mask(a, b, diff_thres=18)
    blobs = blob_centroids(mask, min_area=15)
    check("帧差: 变化区检出唯一连通域", len(blobs) == 1, f"blobs={blobs}")
    if blobs:
        cx, cy, area = blobs[0]
        ok = 40 <= cx <= 52 and 20 <= cy <= 30 and area >= 15
        check("帧差: 质心落在变化区内", ok, f"({cx:.1f},{cy:.1f}) a={area}")
    # 帧差：完全相同的两帧 → 全零（背景星星的常态）
    mask2 = frame_diff_mask(a, a.copy(), diff_thres=18)
    check("帧差: 相同帧无信号", blob_centroids(mask2, 15) == [])
    # 帧差：小抖动（±几个灰阶）不该报 —— diff_thres 的意义
    c = a.astype(np.int16)
    c[10:14, 10:14] += 6                                 # 渲染噪声量级
    mask3 = frame_diff_mask(a, c.astype(np.uint8), diff_thres=18)
    check("帧差: ±6 灰阶的噪声不误报", blob_centroids(mask3, 15) == [])
    # 帧差：尺寸不一致 / None → None（防御）
    check("帧差: 尺寸不一致返回 None",
          frame_diff_mask(a, np.zeros((10, 10), np.uint8)) is None)
    check("帧差: None 输入返回 None", frame_diff_mask(None, a) is None)

    # 最近邻挑选（输入约定：blobs 已按面积降序 —— blob_centroids 的输出顺序）
    blobs3 = [(10.0, 10.0, 900), (100.0, 50.0, 300), (200.0, 60.0, 100)]
    p = pick_nearest_blob(blobs3, (99.0, 51.0))
    check("最近邻: 挑离上次位置最近的", p[:2] == (100.0, 50.0), str(p))
    check("最近邻: 无历史时取面积最大的（列表首位）",
          pick_nearest_blob(blobs3, None)[:2] == (10.0, 10.0))
    check("最近邻: 空列表返回 None", pick_nearest_blob([], None) is None)


# ─────────────────────────────── 2. 合成星星世界 + 全链路状态机

class FakeRng:
    '''钉死的随机源：uniform 取中点，位置抖动恒为 0（便于精确断言坐标）。'''
    def uniform(self, a, b):
        return (a + b) / 2.0


class FakeHooks:
    def __init__(self):
        self.moves = []
        self.holds = []
        self.suppressed = 0
        self.resumed = 0
        self.alerts = []
        self.foreground = True

    def as_dict(self):
        return {
            'suppress': self._suppress, 'resume': self._resume,
            'move_to': self._move_to, 'hold': self._hold,
            'foreground': lambda: self.foreground,
            'alert': self._alert,
        }

    def _suppress(self):
        self.suppressed += 1

    def _resume(self):
        self.resumed += 1

    def _move_to(self, x, y):
        self.moves.append((x, y))

    def _hold(self, down):
        self.holds.append(down)

    def _alert(self, text):
        self.alerts.append(text)


def star_points(cx, cy, r_out=14, r_in=5.5):
    '''五角星 10 顶点（外内半径交替）。

    r_out=14 ≈ 游戏 UI 里星星的量级 —— 星星太小时平移差分信号低于
    检出限，测的是"形状"而不是"大小"，按真实尺寸画才有代表性。
    '''
    pts = []
    for i in range(10):
        r = r_out if i % 2 == 0 else r_in
        ang = -np.pi / 2 + i * np.pi / 5
        pts.append((cx + r * np.cos(ang), cy + r * np.sin(ang)))
    return np.array(pts, dtype=np.int32)


def draw_star_scene(target_xy, target_alpha, static_stars=((100, 40), (160, 30), (220, 45)),
                    size=(300, 700), roi=(50, 100, 450, 220)):
    '''画一帧「星星弹窗区」：深底 + 静态星星（恒定）+ 一颗按 alpha 混合的目标星。

    alpha=1.0 完全亮，alpha 越低越透明（向背景色靠拢）—— 复刻「越来越透明」。
    目标星颜色与静态星同色系：帧差信号只来自透明度/位置的变化，
    这正是真机上区分目标星与背景贴图的方式。
    ⚠️ size=(高, 宽)；画布宽 700 —— 目标星测试里会一路右移到 ~500px，
       画布必须比 star_roi 更宽，否则星移出**画布**（fillPoly 直接不画）
       会把"目标丢失"场景和"合成器 bug"搅在一起（改宽高时踩过）。
    '''
    img = np.full((size[0], size[1], 3), 60, dtype=np.uint8)   # 深色底
    x0, y0, x1, y1 = roi
    # 弹窗底（浅一点，星星画在它上面）
    img[y0:y1, x0:x1] = 90
    star_color = np.array([80, 220, 250], dtype=np.float32)    # 黄星星(BGR)
    bg_color = np.array([90, 90, 90], dtype=np.float32)
    for (sx, sy) in static_stars:
        cv2.fillPoly(img, [star_points(sx, sy)], tuple(int(v) for v in star_color))
    if target_xy is not None:
        tx, ty = target_xy
        blended = (star_color * target_alpha + bg_color * (1 - target_alpha))
        cv2.fillPoly(img, [star_points(tx, ty)], tuple(int(v) for v in blended))
    return img


def add_popup_mark(img):
    '''在帧里贴弹窗特征标记（红蓝棋盘格，画面其它地方不会有）。'''
    patch = np.zeros((8, 8, 3), dtype=np.uint8)
    patch[0:4, 0:4] = (0, 0, 255)
    patch[0:4, 4:8] = (255, 0, 0)
    patch[4:8, 0:4] = (255, 0, 0)
    patch[4:8, 4:8] = (0, 0, 255)
    out = img.copy()
    out[20:28, 20:28] = patch
    return out, patch


STAR_ROI = [50, 100, 450, 220]          # 与 draw_star_scene 的 roi 一致


def build_guard(patch, cfg_over=None):
    cfg = {
        'enabled': True, 'detect_interval': 0.5, 'detect_thres': 0.05,
        'observe_interval': 0.3, 'observe_timeout': 5.0,
        'track_interval': 0.1, 'track_timeout': 45.0, 'track_jitter_px': 0.0,
        'hold_button': False, 'diff_thres': 18, 'min_blob_area': 15,
        'fail_alert': True,
    }
    cfg.update(cfg_over or {})
    hooks = FakeHooks()
    guard = LieDetectorGuard(cfg, patch.copy(), STAR_ROI,
                             hooks.as_dict(), rng=FakeRng())
    return guard, hooks


def fading_frames(t0_alpha=1.0, step=0.12, target_xy=(280, 150)):
    '''生成逐帧变透明的弹窗帧序列。'''
    frames = []
    alpha = t0_alpha
    for _ in range(30):
        img = draw_star_scene(target_xy, alpha)
        frames.append(add_popup_mark(img)[0])
        alpha = max(0.0, alpha - step)
    return frames


def test_full_pipeline_fading():
    """目标星逐帧变透明：检测 → 观察 → 锁定 → 跟随 → 弹窗消失 → 恢复。"""
    _, patch = add_popup_mark(draw_star_scene(None, 1.0))
    guard, hooks = build_guard(patch)
    frames = fading_frames()                     # 30 帧，alpha 1.0 → 0
    plain = draw_star_scene(None, 1.0)           # 无弹窗标记帧（恢复用）

    t = 0.0
    check("① idle: 无弹窗帧返回 idle", guard.step(plain, t) == 'idle')
    check("② idle: 间隔未到不检测", guard.step(frames[0], t + 0.1) == 'idle')
    check("③ 检测: 弹窗帧进入 handling", guard.step(frames[0], t + 0.6) == 'handling')
    check("③ 检测: 抑制键盘被调用（且只一次）", hooks.suppressed == 1)
    # 观察期：星渐隐信号随时间累积，循环喂帧直到锁定（observe_timeout 内）
    now, fi = t + 0.65, 0
    while now < t + 6.0 and guard.phase == guard.OBSERVE:
        guard.step(frames[min(fi, len(frames) - 1)], now)
        now += 0.3
        fi += 1
    check("④ 观察: 渐隐信号累积后锁定进入跟随", guard.phase == guard.TRACK,
          f"phase={guard.phase} 采样 {fi} 帧")
    # 跟随：mouse_to 收到目标星附近坐标（帧坐标 = roi 原点 + roi 内质心）
    n0 = len(hooks.moves)
    while now < t + 8.0 and guard.phase == guard.TRACK:
        guard.step(frames[min(fi, len(frames) - 1)], now)
        now += 0.1
        fi += 1
    check("⑤ 跟随: 鼠标移动被触发", len(hooks.moves) > n0,
          f"moves={len(hooks.moves)}")
    if hooks.moves:
        # 目标星中心 (280,150)，跟随点应落在其半径 ~10px 邻域
        mx, my = hooks.moves[-1]
        near = abs(mx - 280) <= 15 and abs(my - 150) <= 15
        check("⑤ 跟随: 坐标贴着目标星（±15px 内）", near,
              f"last=({mx:.0f},{my:.0f}) 目标=(280,150)")
    # 弹窗消失 → 成功恢复
    got = guard.step(plain, now)
    check("⑥ 完成: 弹窗消失 → 恢复 idle", got == 'idle', got)
    check("⑥ 完成: 抑制/恢复配对", hooks.suppressed == 1 and hooks.resumed == 1)
    check("⑥ 完成: 悬停模式全程不按键", hooks.holds == [])


def test_full_pipeline_moving():
    """目标星不变透明但在移动：帧差同样锁定 + 持续更新跟随坐标。"""
    _, patch = add_popup_mark(draw_star_scene(None, 1.0))
    guard, hooks = build_guard(patch)
    t = 0.0
    pos = [280, 150]
    frames = []
    for i in range(40):                       # 每帧右移 5px
        frames.append(add_popup_mark(draw_star_scene(tuple(pos), 1.0))[0])
        pos[0] += 5
    plain = draw_star_scene(None, 1.0)
    guard.step(frames[0], t + 0.6)
    now, fi = t + 0.65, 0
    while now < t + 6.0 and guard.phase == guard.OBSERVE:
        guard.step(frames[min(fi, len(frames) - 1)], now)
        now += 0.3
        fi += 1
    check("移动①: 锁定移动星进入跟随", guard.phase == guard.TRACK, guard.phase)
    now, fi = t + 1.2, 2
    while now < t + 8.0 and guard.phase == guard.TRACK:
        guard.step(frames[min(fi, len(frames) - 1)], now)
        now += 0.1
        fi += 1
    check("移动②: 跟随坐标持续更新（>5 次）", len(hooks.moves) > 5,
          f"moves={len(hooks.moves)}")
    if len(hooks.moves) >= 2:
        moved = hooks.moves[-1][0] > hooks.moves[0][0]
        check("移动③: 跟随点跟着星星往右走", moved,
              f"first={hooks.moves[0][0]:.0f} last={hooks.moves[-1][0]:.0f}")
    guard.step(plain, now)
    check("移动④: 弹窗消失恢复 idle", guard.phase == guard.IDLE, guard.phase)


def test_failed_and_hold():
    """星星区毫无变化 → observe 超时叫人；hold_button=True 时按住/松开配对。"""
    _, patch = add_popup_mark(draw_star_scene(None, 1.0))
    # 场景 A：完全静态（目标星不动也不变透明）
    guard, hooks = build_guard(patch, cfg_over={'observe_timeout': 2.0})
    static = add_popup_mark(draw_star_scene((280, 150), 1.0))[0]
    t = 0.0
    guard.step(static, t + 0.6)
    now = t + 0.7
    while now < t + 30.0 and guard.phase != guard.FAILED:
        guard.step(static, now)
        now += 0.3
    check("失败A①: 静态场景 observe 超时转 FAILED", guard.phase == guard.FAILED,
          guard.phase)
    check("失败A②: 叫人告警触发", len(hooks.alerts) >= 1, f"alerts={len(hooks.alerts)}")
    check("失败A③: FAILED 期间鼠标不动", hooks.moves == [])
    plain = draw_star_scene(None, 1.0)
    got = guard.step(plain, now)
    check("失败A④: 弹窗消失 → 恢复 idle", got == 'idle', got)
    check("失败A⑤: 抑制/恢复配对", hooks.suppressed == 1 and hooks.resumed == 1)

    # 场景 B：hold_button=True —— 进入跟随时按下，结束时松开
    guard2, hooks2 = build_guard(patch, cfg_over={'hold_button': True})
    frames = fading_frames()
    guard2.step(frames[0], 0.6)
    now, fi = 0.65, 0
    while now < 6.0 and guard2.phase == guard2.OBSERVE:
        guard2.step(frames[min(fi, len(frames) - 1)], now)
        now += 0.3
        fi += 1
    check("按住B①: 进入跟随并按下左键", hooks2.holds == [True],
          f"holds={hooks2.holds} phase={guard2.phase}")
    guard2.step(plain, now)
    check("按住B②: 弹窗消失时松开左键", hooks2.holds == [True, False],
          f"holds={hooks2.holds}")


def test_track_timeout():
    """跟了很久弹窗还在（没跟住）→ track_timeout 叫人并松键。"""
    _, patch = add_popup_mark(draw_star_scene(None, 1.0))
    guard, hooks = build_guard(patch, cfg_over={
        'track_timeout': 3.0, 'hold_button': True})
    frames = fading_frames()
    guard.step(frames[0], 0.6)
    now, fi = 0.65, 0
    while now < 6.0 and guard.phase == guard.OBSERVE:
        guard.step(frames[min(fi, len(frames) - 1)], now)
        now += 0.3
        fi += 1                                 # 锁定
    # 之后一直给"完全静态"的弹窗帧（差分无信号、弹窗不消失）
    still = add_popup_mark(draw_star_scene((280, 150), 0.4))[0]
    now = 1.1
    while now < 30.0 and guard.phase == guard.TRACK:
        guard.step(still, now)
        now += 0.2
    check("超时①: track_timeout 后转 FAILED", guard.phase == guard.FAILED,
          guard.phase)
    check("超时②: 超时前松开了左键", False in hooks.holds or hooks.holds == [],
          f"holds={hooks.holds}")
    check("超时③: 叫人告警触发", len(hooks.alerts) >= 1)


def test_intro_switch_defense():
    """真机流程：弹窗先显示「1秒后开始」说明页 → 整幅切换成星星场地。

    若无防御，说明页做基线时切换后**整幅 ROI 都是差分**，最大连通域
    糊成一大块 → 最近邻会把鼠标指到边缘大块上（乱指）。
    防御生效的表现：整幅变化被识别（blob > 30% ROI）→ 基线重置 →
    仍在观察期；星星开始渐隐后正常锁定目标星。
    """
    _, patch = add_popup_mark(draw_star_scene(None, 1.0))

    def intro_frame():
        """说明页：ROI 里整块都是与星星场地不同的内容（浅色+文字块）。"""
        img = draw_star_scene(None, 1.0)          # 深底（星星场地之外还是深底）
        x0, y0, x1, y1 = STAR_ROI
        img[y0:y1, x0:x1] = 200                   # 说明页：整块浅色
        cv2.rectangle(img, (x0 + 60, y0 + 40), (x0 + 300, y0 + 80),
                      40, -1)                     # 说明"文字"
        return add_popup_mark(img)[0]

    guard, hooks = build_guard(patch, cfg_over={'observe_timeout': 8.0})
    t = 0.0
    intro = intro_frame()
    guard.step(intro, t + 0.6)                    # 检测到弹窗（说明页阶段）
    guard.step(intro, t + 0.65)                   # 基线 = 说明页
    # 1 秒后切换成星星场地：第一次差分 = 整幅变化 → 必须重置基线而非锁定
    field = fading_frames()[0]
    guard.step(field, t + 1.0)
    check("切换①: 整幅变化不被当目标（仍在观察期）",
          guard.phase == guard.OBSERVE, guard.phase)
    check("切换②: 基线已重置（不会把说明页残留当参照）",
          guard._roi_base is not None)
    # 场地里目标星渐隐 → 正常锁定
    frames = fading_frames()
    now, fi = t + 1.3, 0
    while now < t + 10.0 and guard.phase == guard.OBSERVE:
        guard.step(frames[min(fi, len(frames) - 1)], now)
        now += 0.3
        fi += 1
    check("切换③: 基线重置后目标星正常锁定", guard.phase == guard.TRACK,
          guard.phase)
    # 跟随点贴目标星（不指边缘大块）
    while now < t + 14.0 and guard.phase == guard.TRACK:
        guard.step(frames[min(fi, len(frames) - 1)], now)
        now += 0.15
        fi += 1
    if hooks.moves:
        mx, my = hooks.moves[-1]
        check("切换④: 跟随点贴目标星（±20px），不是边缘大块",
              abs(mx - 280) <= 20 and abs(my - 150) <= 20,
              f"last=({mx:.0f},{my:.0f}) 目标=(280,150)")
    else:
        check("切换④: 跟随点贴目标星（±20px）", False, "一次 move 都没发生")


def test_foreground_guard():
    """跟随期游戏不在前台：不移动鼠标；回前台继续。"""
    _, patch = add_popup_mark(draw_star_scene(None, 1.0))
    guard, hooks = build_guard(patch)
    frames = fading_frames()
    guard.step(frames[0], 0.6)
    guard.step(frames[0], 0.65)
    guard.step(frames[1], 0.95)
    hooks.foreground = False
    for i in range(10):
        guard.step(frames[min(2 + i, len(frames) - 1)], 1.1 + i * 0.15)
    check("前台①: 不在前台不移动鼠标", hooks.moves == [], f"moves={hooks.moves}")
    check("前台②: 仍留在跟随阶段等前台回来", guard.phase == guard.TRACK,
          guard.phase)
    hooks.foreground = True
    now = 3.0
    while now < 12.0 and guard.phase == guard.TRACK:
        guard.step(frames[0], now)
        now += 0.15
    check("前台③: 回前台后恢复移动", len(hooks.moves) > 0, f"moves={len(hooks.moves)}")


# ───────────────────────────────────────── 3. 匹配原语 + 配置 + 接线

def test_match_config_wiring():
    img = add_popup_mark(draw_star_scene(None, 1.0))[0]
    _, patch = add_popup_mark(draw_star_scene(None, 1.0))
    mask = make_tpl_mask(patch)
    loc, score = match_template_best(img, patch, mask, 0.05)
    check("匹配: 弹窗模板命中（位置=画入的 20,20）",
          loc == (20, 20) and score < 0.05, f"loc={loc} score={score}")
    plain = draw_star_scene(None, 1.0)
    _, score2 = match_template_best(plain, patch, mask, 0.05)
    check("匹配: 无弹窗帧不命中", score2 >= 0.05, f"score={score2}")
    loc3, score3 = match_template_best(img, patch, mask, 0.05, last_loc=(21, 21))
    check("匹配: 局部搜索同样命中", loc3 == (20, 20) and score3 < 0.05, f"loc={loc3}")

    with open(os.path.join(ROOT, 'config', 'config_default.yaml'),
              encoding='utf-8') as f:
        cfg = yaml.safe_load(f)
    ld = cfg.get('lie_detector', {})
    check("配置: lie_detector 段存在且默认启用", ld.get('enabled') is True)
    check("配置: 帧差阈值默认 18", int(ld.get('diff_thres', 0)) == 18)
    check("配置: 整幅切换防御默认 30%", float(ld.get('max_blob_ratio', 0)) == 0.30)
    check("配置: 跟随超时默认 45s", float(ld.get('track_timeout', 0)) == 45.0)
    check("配置: 默认悬停不按键", ld.get('hold_button') is False)
    check("配置: 失败叫人默认开", ld.get('fail_alert') is True)

    eng = open(os.path.join(ROOT, 'src', 'engine', 'MapleStoryAutoLevelUp.py'),
               encoding='utf-8').read()
    check("引擎: run_once 挂了测谎守卫（最高优先级）",
          "self.lie_guard.step(self.img_frame" in eng)
    check("引擎: start() 创建守卫", "create_guard(self.cfg" in eng)
    check("引擎: move_to/hold 钩子在（帧坐标→屏幕坐标换算）",
          "_lie_move_to" in eng and "_lie_hold" in eng
          and "get_game_window_client_origin" in eng)
    check("引擎: 测谎期间保留自动喝药（弹窗不挡怪物攻击）",
          "self._step_bars_and_potions(self.img_frame)" in eng)

    kbsrc = open(os.path.join(ROOT, 'src', 'input', 'KeyBoardController.py'),
                 encoding='utf-8').read()
    check("键盘: suppressed 标志存在", "self.suppressed" in kbsrc)
    check("键盘: run() 循环尊重抑制（不发键）", "if self.suppressed:" in kbsrc)

    icpt = open(os.path.join(ROOT, 'src', 'input', 'InterceptionController.py'),
                encoding='utf-8').read()
    check("驱动: mouse_hold 封装存在（按住场景）", "def mouse_hold" in icpt)

    # 工厂的降级路径：模板目录不存在 → None（不抛异常）
    g = create_guard({'lie_detector': {'enabled': True}},
                     template_dir=os.path.join(ROOT, '__not_exist__'))
    check("工厂: 模板目录不存在 → None（功能静默跳过）", g is None)
    g2 = create_guard({'lie_detector': {'enabled': False}})
    check("工厂: enabled=false → None", g2 is None)
    check("工厂: 空目录 templates.yaml 读成 {}", load_geo(
        os.path.join(ROOT, '__not_exist__')) == {})

    # 制作工具：两步流程的源码接线（无头参数）
    tool = open(os.path.join(ROOT, 'tools', 'make_lie_detector_templates.py'),
                encoding='utf-8').read()
    check("工具: 无头参数 --auto-popup/--auto-stars 存在",
          "--auto-popup" in tool and "--auto-stars" in tool)
    check("工具: 保存 star_roi", "star_roi" in tool)


def main():
    test_pure_functions()
    test_full_pipeline_fading()
    test_full_pipeline_moving()
    test_failed_and_hold()
    test_track_timeout()
    test_intro_switch_defense()
    test_foreground_guard()
    test_match_config_wiring()

    print()
    if FAILS:
        print(f"共 {len(FAILS)} 项失败：")
        for f in FAILS:
            print(f"  - {f}")
        return 1
    print("全部通过 ✅")
    return 0


if __name__ == "__main__":
    sys.exit(main())
