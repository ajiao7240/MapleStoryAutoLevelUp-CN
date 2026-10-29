# -*- coding: utf-8 -*-
'''
测谎仪自动应答（lie detector auto-answer）—— 星星跟随验证版

背景与题型（2026-10-29 真机截图确认）
------------------------------------
怀旧服的「谎言探测仪」：别的玩家对你使用测谎仪道具后，画面弹出验证窗口。
规则原文（预告页）：「1 秒后，测谎小游戏开始。游戏开始前，请前往安全地点。」
「移动鼠标，放置在逐渐透明且移动的图形上。**根据鼠标光标位于图形上的
时间**决定通过/失败。」—— 纯**悬停跟随**，不用点击不用按键；星星场地里
散布着深色描边的五角星轮廓，只有目标星在渐隐 + 移动，光标贴得越久越接近
通过。星星彻底透明 = 失败，会被系统记为挂机嫌疑。

本模块与整个项目同一原则：**只看画面、只发鼠标移动**，不碰游戏进程。

  ① 检测：模板匹配找测谎弹窗的特征（lie_detector/popup.png，推荐框
     标题「谎言探测仪」—— 预告页和星星场地两个阶段它都在）
  ② 定位目标星：**帧差法** —— 背景星星是静态贴图、帧差恒为零；目标星在
     变透明/移动，帧间必然有差异。差分图里的非零连通域就是目标星，
     不依赖星星的形状、颜色、数量，背景多花都吃得下。
     （基线差分 ∪ 相邻差分互补：渐隐靠基线、移动靠相邻，见 _diff_blobs）
  ③ 跟随：每 0.1s 重新定位 → Interception 移动鼠标贴上去（带 ±2px 拟人
     抖动，人的跟随本来就不完美，帧率太贴太准反而像机器）。
  ④ 兜底：定位失败 / 超时**绝不乱动**，弹置顶窗把人叫醒，挂机静默，
     弹窗消失后自动恢复。

节律拟人（与拾取键的抖动同一个理由）：跟随位置带小噪声、更新周期取自
随机区间 —— 精确周期/精确轨迹在行为统计上一眼就是机器。

模板从哪来
----------
lie_detector/ 目录（出厂为空，必须用 tools/make_lie_detector_templates.py
从真机截图制作一次；1366x768 环境锁定，理论上做完可通用）：
  popup.png       弹窗特征模板（推荐框住弹窗上辨识度最高的一小块）
  templates.yaml  star_roi = 星星活动区（**客户区坐标**，即 get_img_frame()
                  裁剪后的坐标系；框的时候宁大勿小，目标星贴边跑出去就丢了）

可离线测试的部分
----------------
frame_diff_mask / blob_centroids / pick_nearest_blob / match_template_best
全是**纯函数**；LieDetectorGuard 的鼠标移动、告警、抑制全部走注入的 hooks
—— tools/verify_lie_detector.py 用假 hooks 和合成帧（静态星 + 一颗逐帧
变透明/移动的星）把整条链路钉死，不需要开游戏。
'''
import os
import random
import threading
import time

import cv2
import numpy as np
import yaml

from src.utils.logger import logger

try:  # 打包后 __file__ 落在 exe 旁的 _internal 里，必须走 APP_ROOT（= exe 所在目录）
    from src.utils.paths import APP_ROOT as REPO_ROOT
except Exception:                                       # noqa: BLE001
    REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ══════════════════════════════════════════════════════════════════════════
# 纯函数区（可离线单测，不碰任何外部状态）
# ══════════════════════════════════════════════════════════════════════════

def make_tpl_mask(tpl, white_thres=245):
    '''给模板生成「近白透明」掩码（TM_SQDIFF_NORMED 用）。

    弹窗特征模板从真机截图切下来时，若周围有接近纯白的底，把它从比对中
    排除 —— 与引擎其它模板匹配（find_pattern_sqdiff 内置机制）同一思想。
    '''
    gray = cv2.cvtColor(tpl, cv2.COLOR_BGR2GRAY) if tpl.ndim == 3 else tpl
    return (gray < white_thres).astype(np.uint8) * 255


def match_template_best(img, tpl, mask, threshold, last_loc=None,
                        local_radius=60):
    '''在 img 里找 tpl 的最佳匹配位置（SQDIFF_NORMED + 掩码，纯函数）。

    返回 (loc, score)：loc = 最佳匹配左上角；score 越小越像。

    为什么不用 common.find_pattern_sqdiff（功能等价）：那个模块**顶层**
    import 了 pywin32，把本模块一起拖进"只有 Windows 才能 import"的
    依赖链 —— 而本模块的全部判定逻辑要求可以离线自检（tools/verify_lie_detector.py
    在任何平台裸跑）。所以这里自带一份最小实现：
      · last_loc 给了就先在它附近搜（弹窗位置固定，第二次起省 90% 计算）；
        分数达标直接返回，不达标退回全图。
    '''
    h, w = tpl.shape[:2]
    if img.shape[0] < h or img.shape[1] < w:
        return None, 1.0
    if last_loc is not None:
        lx, ly = last_loc
        x0 = max(0, lx - local_radius)
        y0 = max(0, ly - local_radius)
        x1 = min(img.shape[1], lx + local_radius + w)
        y1 = min(img.shape[0], ly + local_radius + h)
        roi = img[y0:y1, x0:x1]
        if roi.shape[0] >= h and roi.shape[1] >= w:
            res = cv2.matchTemplate(roi, tpl, cv2.TM_SQDIFF_NORMED, mask=mask)
            res = np.nan_to_num(res, nan=1.0, posinf=1.0, neginf=1.0)
            # minMaxLoc 返回 (minVal, maxVal, minLoc, maxLoc) —— 取的是**最小**值
            score, _, loc, _ = cv2.minMaxLoc(res)
            if score < threshold:
                return (x0 + loc[0], y0 + loc[1]), float(score)
    res = cv2.matchTemplate(img, tpl, cv2.TM_SQDIFF_NORMED, mask=mask)
    res = np.nan_to_num(res, nan=1.0, posinf=1.0, neginf=1.0)
    score, _, loc, _ = cv2.minMaxLoc(res)
    return loc, float(score)


def frame_diff_mask(roi_a, roi_b, diff_thres=18, clean=True):
    '''两帧同位置 ROI 的差分掩码（纯函数）。

    Args:
        roi_a, roi_b: 同尺寸 BGR（或灰度）图
        diff_thres: 单通道像素差超过它才算"变了"。18 ≈ 静态贴图的压缩/
                    渲染抖动（±几个灰阶）不会误报，而透明度每步 8% 的
                    渐变（几十灰阶）稳稳命中。
        clean: 形态学清理。⚠️ **开运算 + 膨胀**，不是腐蚀（合成帧实测
               翻车过）：慢速移动的目标差分是一条**细带**（宽≈每步
               位移 5px），3x3 腐蚀把它啃得只剩十几个像素，恰好卡在
               min_area 检出限上、时有时无 —— 跟踪会间歇性丢信号。
               开运算（腐蚀+膨胀）同样能去掉孤立噪点，随后的**再膨胀
               一次**把细带加宽到稳定检出（质心不偏移，膨胀是对称的）。

    Returns:
        二值 mask（uint8 0/255）。差分为零（两帧完全一样）时全 0。
    '''
    if roi_a is None or roi_b is None:
        return None
    if roi_a.shape != roi_b.shape:
        return None
    a = roi_a if roi_a.ndim == 2 else cv2.cvtColor(roi_a, cv2.COLOR_BGR2GRAY)
    b = roi_b if roi_b.ndim == 2 else cv2.cvtColor(roi_b, cv2.COLOR_BGR2GRAY)
    diff = cv2.absdiff(a, b)
    mask = (diff > int(diff_thres)).astype(np.uint8) * 255
    if clean and mask.any():
        k = np.ones((3, 3), np.uint8)
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, k)   # 去孤立噪点
        mask = cv2.dilate(mask, k)                          # 细带加宽到可检出
    return mask


def blob_centroids(mask, min_area=15):
    '''差分掩码里的连通域质心（纯函数）。

    Returns:
        [(cx, cy, area), ...] 按面积从大到小排。cx/cy 是**浮点质心**
        （比外接框中心更贴星星的真实位置 —— 星星渐隐时差分区域是它
        变化的那部分，质心跟随最稳）。
    '''
    if mask is None:
        return []
    n, labels, stats, centroids = cv2.connectedComponentsWithStats(
        (mask > 0).astype(np.uint8), connectivity=8)
    out = []
    for i in range(1, n):                     # 0 号是背景
        area = int(stats[i, cv2.CC_STAT_AREA])
        if area < int(min_area):
            continue
        out.append((float(centroids[i][0]), float(centroids[i][1]), area))
    out.sort(key=lambda b: -b[2])
    return out


def pick_nearest_blob(blobs, last_xy):
    '''多个候选连通域里挑离上次位置最近的（惯性跟踪，纯函数）。

    目标星是"同一颗在变"，帧间位移远小于它到别的星的距离 → 最近邻
    永远选得对；last_xy 为 None 时取最大（差分最显著）的那个。
    '''
    if not blobs:
        return None
    if last_xy is None:
        return blobs[0]
    lx, ly = last_xy
    return min(blobs, key=lambda b: (b[0] - lx) ** 2 + (b[1] - ly) ** 2)


# ══════════════════════════════════════════════════════════════════════════
# 告警弹窗（叫人兜底用；模式抄自 stuck_alert：daemon 线程 + 防重入 + 冷却）
# ══════════════════════════════════════════════════════════════════════════

_alert_lock = threading.Lock()
_alert_open = False
_t_last_alert = 0.0


def _alert_worker(title, text):
    global _alert_open, _t_last_alert
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(
            0, text, title, 0x40040)     # MB_SETFOREGROUND | MB_INFORMATION
    except Exception as e:                                  # noqa: BLE001
        logger.warning(f"[测谎告警] 弹窗失败（只留日志）：{e}")
    finally:
        with _alert_lock:
            _alert_open = False
            _t_last_alert = time.time()


def alert_user(text, cooldown=60.0):
    '''置顶弹窗把人叫醒（打日志永远做；非 Windows 只打日志）。

    cooldown 内不重复弹；上一个弹窗没关之前也不弹第二个 ——
    测谎弹窗可能一直开着等人，告警刷屏只会让人更烦躁。
    '''
    logger.warning("[测谎告警] " + str(text).replace('\n', ' | '))
    import sys
    if sys.platform != 'win32':
        return False
    global _alert_open
    with _alert_lock:
        if _alert_open:
            return False
        if time.time() - _t_last_alert < float(cooldown):
            return False
        _alert_open = True
    threading.Thread(target=_alert_worker,
                     args=("⚠ 测谎仪需要你手动处理", text), daemon=True).start()
    return True


# ══════════════════════════════════════════════════════════════════════════
# LieDetectorGuard —— 挂机主循环每帧调用的状态机（星星跟随版）
# ══════════════════════════════════════════════════════════════════════════

class LieDetectorGuard:
    '''测谎仪守卫。主循环每帧 step(img, now)，返回 "idle" 或 "handling"。

    返回 "handling" 的含义：测谎弹窗正在处理（找星 / 跟随 / 等人），
    调用方**本帧不要再发任何移动/攻击指令**（引擎侧：清空指令并跳过决策）。

    所有副作用（鼠标移动 / 按住 / 告警 / 抑制键盘线程）都走注入的 hooks ——
    离线自检传假 hooks 即可全链路验证，不需要驱动和游戏。

    状态流转：
        idle ──检测到弹窗──▶ observe ──帧差锁定目标星──▶ track
          ▲                   │                            │
          │                   │observe_timeout             │track_timeout
          │                   │（完全没变化）               │（跟了很久弹窗还在）
          │                   ▼                            ▼
          │                 failed ◀──────────────────── failed（叫人）
          └────────── 任何阶段弹窗消失 → 恢复挂机（failed 等人处理完也是这条出口）

    观察期（observe）的设计：
        弹窗刚弹出时目标星可能还没开始变透明（入场动画），立刻找找不到。
        每 observe_interval 收一帧 ROI，与上一帧做差分；连续两次差分都
        有信号 → 锁定。observe_timeout 内一点变化都没有 → 判定"这不是
        会变化的题型 / 识别区不对"→ 叫人（绝不瞎指鼠标）。

    跟随期（track）的设计：
        每 track_interval 用最近两帧的差分重新定位目标（最近邻惯性），
        鼠标移过去（坐标带 ±jitter_px 抖动）。差分暂时没信号（星星
        透明度变化间歇/已经稳定）就**保持原位**——人的手也是停在原地。
        hold_button=True 时整个跟随期按住左键（若真机验证需要按住，
        默认悬停）。
    '''

    IDLE = 'idle'
    OBSERVE = 'observe'
    TRACK = 'track'
    FAILED = 'failed'

    def __init__(self, cfg, popup_tpl, star_roi, hooks, rng=random):
        #: cfg: cfg["lie_detector"] 段（引擎已做深合并）
        self.cfg = cfg
        self.popup_tpl = popup_tpl
        #: 弹窗模板的近白透明掩码
        self._popup_mask = make_tpl_mask(popup_tpl)
        #: 星星活动区（帧坐标 [x0, y0, x1, y1]）
        self.star_roi = [int(v) for v in star_roi]
        self.hooks = hooks
        self.rng = rng                    # 注入随机源：离线测试可钉死序列
        # ── 参数（一次读出，别在循环里反复 get）──────────────────────
        c = cfg if isinstance(cfg, dict) else {}
        self.detect_interval = float(c.get('detect_interval', 0.5))
        self.detect_thres = float(c.get('detect_thres', 0.05))
        self.observe_interval = float(c.get('observe_interval', 0.3))
        self.observe_timeout = float(c.get('observe_timeout', 5.0))
        self.track_interval = float(c.get('track_interval', 0.1))
        self.track_timeout = float(c.get('track_timeout', 45.0))
        self.track_jitter_px = float(c.get('track_jitter_px', 2.0))
        #: 运动连续性约束（px）：一次更新里目标位置的最大允许跳变。
        #: 星星是连续移动的，帧间"瞬移"只可能来自误锁定（典型：星星跑出
        #: 活动区后，基线差分在它的**出发点**留下残影，最近邻挑中残影）。
        #: 跳变超过它 = 拒绝本次更新、鼠标保持原位（跟丢交 track_timeout 兜底）。
        self.track_max_jump = float(c.get('track_max_jump', 40.0))
        self.hold_button = bool(c.get('hold_button', False))
        self.diff_thres = int(c.get('diff_thres', 18))
        self.min_blob_area = int(c.get('min_blob_area', 15))
        #: 整幅切换防御：最大 blob 占 ROI 面积超过该比例 = 画面整体变了
        #:（说明页→星星场地），重置基线而不是把它当目标星
        self.max_blob_ratio = float(c.get('max_blob_ratio', 0.30))
        self.fail_alert = bool(c.get('fail_alert', True))
        # ── 运行状态 ──────────────────────────────────────────────
        self.phase = self.IDLE
        self._t_last_detect = 0.0
        self._popup_last_loc = None       # 弹窗模板上次命中位置（局部搜索加速）
        self._t_phase = 0.0               # 进入当前阶段的时间
        self._t_next = 0.0                # 下一个动作（采样/跟随）的时间
        self._roi_prev = None             # 上一帧的星星区（相邻差分用）
        self._roi_base = None             # 基线帧：弹窗刚打开、星星最亮那一刻
        self._target_xy = None            # 目标星在 ROI 内的质心坐标
        self._hold_down = False           # 当前是否按住左键
        self._n_follow = 0                # 本轮跟随更新次数（日志统计用）
        self._n_answered = 0              # 历史成功次数（日志统计用）

    # ── 对外主入口 ─────────────────────────────────────────────────
    def step(self, img, now=None):
        '''每帧调用。返回 "idle"（无事）或 "handling"（本帧别发挂机指令）。'''
        if now is None:
            now = time.time()
        try:
            if self.phase == self.IDLE:
                self._step_idle(img, now)
            elif self.phase == self.OBSERVE:
                self._step_observe(img, now)
            elif self.phase == self.TRACK:
                self._step_track(img, now)
            elif self.phase == self.FAILED:
                self._step_failed(img, now)
        except Exception as e:                             # noqa: BLE001
            # 守卫崩了不能把挂机整个拖死 —— 退回 idle 并大声报错
            logger.error(f"[测谎] 状态机异常（退回待机，请提 issue 附日志）：{e}")
            self._release_hold()
            self._resume_idle()
        return self.IDLE if self.phase == self.IDLE else 'handling'

    # ── 各阶段 ─────────────────────────────────────────────────────
    def _step_idle(self, img, now):
        if now - self._t_last_detect < self.detect_interval:
            return
        self._t_last_detect = now
        if self._detect_popup(img):
            logger.warning("[测谎] ⚠ 检测到测谎仪弹窗，暂停挂机、开始找目标星星")
            self.hooks['suppress']()
            self._roi_prev = None
            self._roi_base = None         # 基线 = 观察期采到的第一帧（星星最亮）
            self._target_xy = None
            self._set_phase(self.OBSERVE, now)
            self._t_next = now                # 第一帧就采样

    def _step_observe(self, img, now):
        if not self._detect_popup(img):
            logger.info("[测谎] 弹窗已消失（未跟随即关闭），恢复挂机")
            self._resume_idle()
            return
        if now < self._t_next:
            return
        self._t_next = now + self.observe_interval
        roi = self._crop_star_roi(img)
        if self._roi_base is None:
            # 第一帧只当基线，差分无从比起
            self._roi_base = roi
            self._roi_prev = roi
            return
        blobs, _adj, base_reset = self._diff_blobs(roi)
        if base_reset:
            # 说明页→星星场地的切换：观察从新画面重新计时，别把
            # 切换耗时算进"毫无变化"的超时里
            self._t_phase = now
            return
        if blobs:
            self._target_xy = (blobs[0][0], blobs[0][1])
            logger.info(f"[测谎] 锁定变化中的星星（ROI 内坐标 "
                        f"({self._target_xy[0]:.0f},{self._target_xy[1]:.0f})，"
                        f"差分面积 {blobs[0][2]}px），开始跟随")
            self._set_phase(self.TRACK, now)
            self._t_next = now + self._track_gap()
            if self.hold_button:
                self._press_hold()
            return
        if now - self._t_phase >= self.observe_timeout:
            self._fail(now, why=f"{self.observe_timeout:.0f}s 内星星区毫无帧间变化"
                                 "（题型与预期不符？或 star_roi 框错？）")

    def _step_track(self, img, now):
        if not self._detect_popup(img):
            self._n_answered += 1
            logger.warning(f"[测谎] ✅ 弹窗已关闭，跟随完成"
                           f"（本轮跟随 {self._n_follow} 次，累计成功 "
                           f"{self._n_answered} 次），恢复挂机")
            self._release_hold()
            self._resume_idle()
            return
        if now - self._t_phase >= self.track_timeout:
            self._release_hold()
            self._fail(now, why=f"跟随 {self.track_timeout:.0f}s 后弹窗仍在"
                                "（没跟住或题型超时），转为叫人")
            return
        if now < self._t_next:
            return
        self._t_next = now + self._track_gap()
        # 前台守卫：鼠标移动是屏幕绝对坐标，游戏不在前台时移动没有意义
        #（还会把别的东西点乱），冻结等键盘线程把游戏切回前台。
        if not self.hooks['foreground']():
            return
        roi = self._crop_star_roi(img)
        blobs, adj_blobs, base_reset = self._diff_blobs(roi)
        if base_reset:
            return          # 画面整体切换：基线已重置，鼠标保持原位等新信号
        # 优先相邻差分（只报星星此刻真身，无幽灵）；纯渐隐不动时它
        # 没信号，退回联合差分（基线差分锁的是渐隐星本体位置）
        picked = pick_nearest_blob(adj_blobs or blobs, self._target_xy)
        if picked is not None:
            nx, ny = picked[0], picked[1]
            if self._target_xy is not None:
                jump = abs(nx - self._target_xy[0]) + abs(ny - self._target_xy[1])
                if jump > self.track_max_jump:
                    # 帧间瞬移 = 大概率不是真身（星星出界后基线差分留在
                    # 出发点的残影最典型）—— 拒绝跳过去，保持原位。
                    # 连续拒绝不会累积成死锁：真目标回来时差分会先在
                    # 原位附近重新出现（连续小步），跳变自然落回限内。
                    logger.info(f"[测谎] 差分位置帧间跳变 {jump:.0f}px 超过 "
                                f"{self.track_max_jump:.0f}px（疑似残影/目标出界），"
                                "本次不更新、鼠标保持原位")
                    return
            self._target_xy = (nx, ny)
            self._move_mouse_to_target()
        # 差分全无信号（星星彻底透明后稳定/题型间歇）—— 鼠标保持原位，
        # 跟丢判定靠上面的 track_timeout 兜底，不在这里瞎猜新目标。

    def _step_failed(self, img, now):
        # 只等人：弹窗消失 = 人处理完了 → 恢复挂机
        if not self._detect_popup(img):
            logger.info("[测谎] 弹窗已被手动处理，恢复挂机")
            self._release_hold()
            self._resume_idle()
            return
        if self.fail_alert:
            self.hooks['alert'](
                "检测到测谎仪弹窗，但自动跟随失败（找不到变化的星星 / 跟随后超时）。\n"
                "挂机已暂停，**请立刻切到游戏手动完成验证** ——\n"
                "星星会越来越透明，彻底消失即判定失败。\n"
                "（人处理完弹窗消失后，挂机会自动恢复）")

    # ── 动作原语 ───────────────────────────────────────────────────
    def _track_gap(self):
        '''跟随更新间隔：配置 ±20% 抖动（拟人，见模块头说明）。'''
        base = self.track_interval
        return base * self.rng.uniform(0.8, 1.2)

    def _move_mouse_to_target(self):
        '''把鼠标移到目标星（帧坐标 + 抖动）。'''
        if self._target_xy is None:
            return
        jx = self.rng.uniform(-self.track_jitter_px, self.track_jitter_px)
        jy = self.rng.uniform(-self.track_jitter_px, self.track_jitter_px)
        fx = self.star_roi[0] + self._target_xy[0] + jx
        fy = self.star_roi[1] + self._target_xy[1] + jy
        self.hooks['move_to'](fx, fy)
        self._n_follow += 1
        if self._n_follow == 1 or self._n_follow % 50 == 0:
            logger.info(f"[测谎] 已跟随 {self._n_follow} 次"
                        f"（目标帧坐标 {fx:.0f},{fy:.0f}）")

    def _press_hold(self):
        if not self._hold_down:
            self.hooks['hold'](True)
            self._hold_down = True

    def _release_hold(self):
        if self._hold_down:
            try:
                self.hooks['hold'](False)
            except Exception:                               # noqa: BLE001
                pass          # 松键失败不能卡住状态机
            self._hold_down = False

    def _fail(self, now, why):
        logger.error(f"[测谎] {why}；放弃自动跟随、转为叫人")
        self._set_phase(self.FAILED, now)
        if self.fail_alert:
            self.hooks['alert'](
                f"检测到测谎仪弹窗，但自动跟随失败（{why}）。\n"
                "挂机已暂停，**请立刻切到游戏手动完成验证** ——\n"
                "星星会越来越透明，彻底消失即判定失败。\n"
                "（人处理完弹窗消失后，挂机会自动恢复）")

    def _set_phase(self, phase, now):
        self.phase = phase
        self._t_phase = now

    def _resume_idle(self):
        self.phase = self.IDLE
        self._roi_prev = None
        self._roi_base = None
        self._target_xy = None
        self._n_follow = 0
        self.hooks['resume']()

    # ── 视觉原语 ───────────────────────────────────────────────────
    def _diff_blobs(self, roi):
        '''差分定位 → (联合差分 blobs, 相邻差分 blobs, 是否刚重置基线)。

        三条差分的分工（只用相邻差分是错的，合成帧实测翻车）：
          · **相邻差分**（上一采样 vs 当前）：只报星星**此刻真身**的位置
            （它在动），是跟踪期的首选信号。
          · **基线差分**（弹窗刚打开的**第一帧** vs 当前）：对缓慢渐隐
            敏感 —— 透明度每步变 12% 在灰度上只有 ~15 灰阶，低于
            diff_thres(18)，相邻差分一个信号都没有；而星星比基线淡
            的事实会**累积**，基线差分持续存在且越来越大。
          · **联合**（两者取或）：观察期锁定用（渐隐/移动通吃）。

        ⚠️ **幽灵 blob**（为什么跟踪期不能只用联合差分）：基线差分会在
        星星的**初始位置**永远报一块"变化"（它从那里离开/变淡过）。
        若跟踪因前台切换冻结几秒、星星移远了，恢复后最近邻会挑中
        初始位置的幽灵块并**锁死在那**直到超时 —— 所以跟踪期优先信
        相邻差分（真身），它没信号（纯渐隐不动）才退回联合差分。

        ⚠️ **整幅切换防御**（2026-10-29 真机截图确认有预告阶段）：
        弹窗先显示「1 秒后测谎小游戏开始」的说明页，然后才整体切换成
        星星场地。若基线建立在说明页上，切换后**整幅 ROI 都是差分**，
        最大连通域会糊成一大块（贴着弹窗边缘），最近邻挑它 = 鼠标
        乱指。所以：联合 blobs 的最大块超过 ROI 的 max_blob_ratio 时，
        判定为"画面整体变了"，**重置基线到当前帧**、返回空（调用方
        按无信号处理，观察计时也应重开 —— 见第三个返回值）。
        '''
        m_adj = frame_diff_mask(self._roi_prev, roi, self.diff_thres)
        m_base = frame_diff_mask(self._roi_base, roi, self.diff_thres)
        self._roi_prev = roi
        adj_blobs = blob_centroids(m_adj, self.min_blob_area)
        mask = None
        if m_adj is not None and m_base is not None:
            mask = cv2.bitwise_or(m_adj, m_base)
        else:
            mask = m_adj if m_adj is not None else m_base
        blobs = blob_centroids(mask, self.min_blob_area)
        area_roi = float(roi.shape[0]) * float(roi.shape[1])
        if blobs and area_roi > 0 and \
                blobs[0][2] > area_roi * self.max_blob_ratio:
            logger.info("[测谎] 星星区整幅变化（说明页→星星场地的切换？），"
                        "重置差分基线后重新观察")
            self._roi_base = roi
            return [], [], True
        return blobs, adj_blobs, False

    def _detect_popup(self, img):
        '''弹窗特征模板匹配。返回 True/False；命中时记住位置供下次局部搜索。'''
        if img is None or self.popup_tpl is None:
            return False
        loc, score = match_template_best(
            img, self.popup_tpl, self._popup_mask,
            self.detect_thres, last_loc=self._popup_last_loc)
        if loc is not None and score < self.detect_thres:
            self._popup_last_loc = loc
            return True
        return False

    def _crop_star_roi(self, img):
        x0, y0, x1, y1 = self.star_roi
        x0 = max(0, x0)
        y0 = max(0, y0)
        x1 = min(img.shape[1], x1)
        y1 = min(img.shape[0], y1)
        return img[y0:y1, x0:x1]


# ══════════════════════════════════════════════════════════════════════════
# 工厂：读模板目录 + 组装 hooks
# ══════════════════════════════════════════════════════════════════════════

def load_geo(dirpath):
    '''读 templates.yaml（star_roi）。'''
    path = os.path.join(dirpath, 'templates.yaml')
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return yaml.safe_load(f) or {}
    except Exception as e:                                  # noqa: BLE001
        logger.warning(f"[测谎] templates.yaml 读取失败：{e}")
        return {}


def create_guard(cfg, hooks=None, template_dir=None):
    '''组装守卫。模板没做全 → 返回 None（功能关闭），打一条日志说明缺什么。

    Args:
        cfg: **完整配置**（取 cfg['lie_detector'] 段；缺段 = 关闭）
        hooks: 回调 {suppress, resume, move_to, hold, foreground, alert}。
               缺省时用「只打日志」的假实现 —— 离线工具/自检用。
        template_dir: 模板目录，默认 <仓库根>/lie_detector
    '''
    sec = (cfg or {}).get('lie_detector') or {}
    if not sec.get('enabled', False):
        return None
    if template_dir is None:
        template_dir = os.path.join(REPO_ROOT, 'lie_detector')
    popup_path = os.path.join(template_dir, 'popup.png')
    popup_tpl = None
    if os.path.isfile(popup_path):
        # 按需 import（common 顶层带 pywin32，别让它拖累本模块的可移植性）
        from src.utils.common import imread_unicode
        popup_tpl = imread_unicode(popup_path)
    geo = load_geo(template_dir)
    star_roi = geo.get('star_roi')

    missing = []
    if popup_tpl is None:
        missing.append('popup.png（弹窗特征模板）')
    if not star_roi or len(star_roi) != 4:
        missing.append('templates.yaml 的 star_roi（星星活动区）')
    if missing:
        logger.info(
            "[测谎] 自动过测谎已启用但模板不完整，暂不生效。缺："
            + '、'.join(missing)
            + "。用 python -m tools.make_lie_detector_templates 制作后自动生效。")
        return None

    if hooks is None:
        hooks = {
            'suppress': lambda: logger.info("[测谎·离线] suppress"),
            'resume': lambda: logger.info("[测谎·离线] resume"),
            'move_to': lambda x, y: logger.info(f"[测谎·离线] move_to ({x:.0f},{y:.0f})"),
            'hold': lambda down: logger.info(f"[测谎·离线] hold {down}"),
            'foreground': lambda: True,
            'alert': alert_user,
        }
    logger.info(f"[测谎] 守卫就绪（星星跟随版）：星星活动区 {star_roi}")
    return LieDetectorGuard(sec, popup_tpl, star_roi, hooks)
