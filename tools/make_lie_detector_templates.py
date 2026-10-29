# -*- coding: utf-8 -*-
'''
测谎仪模板制作工具 —— 两步截完「弹窗特征 + 星星活动区」

为什么需要它
------------
自动过测谎（src/utils/lie_detector.py，星星跟随版）靠两样素材工作：
  ① popup.png       弹窗特征模板（检测「测谎弹窗出现了没有」）
  ② templates.yaml  star_roi = 星星活动区（找「越来越透明的那颗星」的范围）
这两样要从**真机截图**量出来 —— 出厂目录是空的，本工具负责把它们做出来。

素材怎么来
----------
最省事的办法：找个朋友（或小号）对你用一次测谎仪，趁弹窗开着截图
（系统截图即可），把 png 传进来：
    python -m tools.make_lie_detector_templates --image 截图.png
不带 --image 会现场抓一帧游戏画面（游戏需窗口化运行中）。

窗口里的操作（全部鼠标，两步）
------------------------------
  第 1 步  左键拖拽：框住弹窗上**最有辨识度**的一小块 —— 推荐标题/
          边角文字（画面里其它地方不可能出现的那块）。
  第 2 步  左键拖拽：框住**整个星星活动区**（弹窗里所有星星都在的那块）。
          ⚠️ 宁大勿小：目标星会移动，框小了它跑出去就找不到了。
          回车 = 保存，s = 跳过重来，q/ESC = 退出不保存。

⚠️ 坐标系：本工具先按引擎同一套规则（crop_frame_to_client）把帧裁成
   **客户区坐标系**，之后框出来的坐标/模板与引擎运行时完全一致。
   ⚠️ 所以 --image 传入的必须是「整窗截图」（含标题栏），不要自己先裁过。

无人值守自检（跳过鼠标，直接给坐标 —— 验证工具本身）
------------------------------------------------------
    python -m tools.make_lie_detector_templates --image 截图.png \\
        --auto-popup 583,312,80,24 --auto-stars 500,380,370,260 --no-save
'''
import argparse
import os
import sys

import cv2
import numpy as np
import yaml

try:  # 打包后 __file__ 落在 exe 旁的 _internal 里，必须走 APP_ROOT（= exe 所在目录）
    from src.utils.paths import APP_ROOT as REPO_ROOT
except Exception:                                             # noqa: BLE001
    REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

# ⚠️ common 的函数（读图/裁剪/写盘）**按需** import 到函数体内：
#    common 顶层带 pywin32，顶层 import 会把本工具的可移植性一起拖没。

WIN = "lie_detector_maker"
OUT_DIR = os.path.join(REPO_ROOT, 'lie_detector')


# ------------------------------------------------------------------ 帧载入

def load_client_frame(image_path=None, window_title=None):
    '''拿到**客户区坐标系**的一帧（与引擎 get_img_frame 同一口径）。

    --image：读盘 → crop_frame_to_client 裁剪；
    现场抓：grab_game_frame 返回的也是原始帧 → 同样裁剪。
    '''
    from src.utils.common import (imread_unicode, load_yaml,
                                   resolve_title_bar_height,
                                   crop_frame_to_client)
    raw = None
    if image_path:
        raw = imread_unicode(image_path)
        if raw is None:
            print(f"[错误] 读不到截图：{image_path}")
            return None
    else:
        from tools.template_capture import grab_game_frame
        raw = grab_game_frame(window_title or '冒险岛')
        if raw is None:
            return None
    cfg = load_yaml(os.path.join('config', 'config_default.yaml'))
    gw = cfg.get('game_window', {})
    title_h = resolve_title_bar_height(gw, window_title)
    frame, msg = crop_frame_to_client(raw, tuple(gw['size']), title_h,
                                      tag='测谎模板')
    if frame is None:
        print(f"[错误] 截图裁剪失败：{msg}\n"
              f"       （传入的必须是**整窗截图**（含标题栏 {title_h}px），"
              f"不要预先裁剪过）")
        return None
    return frame


# ------------------------------------------------------------------ 主流程

def run(args):
    frame = load_client_frame(args.image, args.title)
    if frame is None:
        return 1
    print(f"[信息] 帧已就绪（客户区 {frame.shape[1]}x{frame.shape[0]}，"
          f"坐标系与引擎一致）")

    if args.auto_popup:
        return run_auto(frame, args)

    results = interactive_collect(frame)
    if results is None:
        print("[取消] 未保存任何东西。")
        return 1
    popup_tpl, star_roi = results

    if args.no_save:
        print("[信息] --no_save：只验证不落盘。")
        print(f"    popup 模板 {popup_tpl.shape[1]}x{popup_tpl.shape[0]}"
              f" / star_roi = {star_roi}")
        return 0
    return save_all(popup_tpl, star_roi)


def interactive_collect(frame):
    '''两步交互：框弹窗特征 → 框星星活动区。'''
    state = {'box': None}

    def on_mouse(event, x, y, flags, param):
        if event == cv2.EVENT_LBUTTONDOWN:
            state['down'] = (x, y)
        elif event == cv2.EVENT_LBUTTONUP and 'down' in state:
            x0, y0 = state.pop('down')
            state['box'] = (min(x0, x), min(y0, y), max(x0, x) + 1, max(y0, y) + 1)

    cv2.namedWindow(WIN, cv2.WINDOW_NORMAL)
    cv2.setMouseCallback(WIN, on_mouse)

    steps = [
        ("第 1/2 步：框住弹窗特征（最有辨识度的一小块，推荐标题文字）",
         'popup'),
        ("第 2/2 步：框住整个星星活动区（所有星星都在的那块，宁大勿小）",
         'stars'),
    ]
    picked = {}
    for hint, key in steps:
        state['box'] = None
        while True:
            vis = frame.copy()
            # 中文提示放窗口标题（OpenCV 画布画不了中文；终端里也有完整引导）
            try:
                cv2.setWindowTitle(WIN, f"测谎模板制作 - {hint}")
            except Exception:                             # noqa: BLE001
                pass
            if state['box']:
                x0, y0, x1, y1 = state['box']
                cv2.rectangle(vis, (x0, y0), (x1, y1), (0, 255, 0), 2)
            cv2.imshow(WIN, vis)
            k = cv2.waitKey(30) & 0xFF
            if k == ord('q') or k == 27:
                cv2.destroyWindow(WIN)
                return None
            if k == 13:                                   # 回车 = 确认本步
                if not state['box']:
                    print("[提示] 还没框选（按住左键拖拽）。")
                    continue
                picked[key] = state['box']
                break
        cv2.destroyWindow(WIN)

    px0, py0, px1, py1 = picked['popup']
    popup_tpl = frame[py0:py1, px0:px1].copy()
    star_roi = [int(v) for v in picked['stars']]
    return popup_tpl, star_roi


# ------------------------------------------------------------------ 无人值守

def run_auto(frame, args):
    '''--auto-* 无头模式：直接给坐标，验证保存闭环（自检用）。'''
    def parse_box(s):
        x0, y0, w, h = [int(v) for v in s.split(',')]
        return (x0, y0, x0 + w, y0 + h)

    px0, py0, px1, py1 = parse_box(args.auto_popup)
    popup_tpl = frame[py0:py1, px0:px1].copy()
    star_roi = [int(v) for v in parse_box(args.auto_stars)]
    if args.no_save:
        print(f"[信息] --no_save：popup {popup_tpl.shape[1]}x{popup_tpl.shape[0]}"
              f" / star_roi = {star_roi}")
        return 0
    return save_all(popup_tpl, star_roi)


# ------------------------------------------------------------------ 保存

def save_all(popup_tpl, star_roi):
    from src.utils.common import imwrite_unicode
    if popup_tpl is None or popup_tpl.size == 0:
        print("[错误] 没有弹窗特征模板 —— 检测不了弹窗，不保存。")
        return 1
    os.makedirs(OUT_DIR, exist_ok=True)
    if not imwrite_unicode(os.path.join(OUT_DIR, 'popup.png'), popup_tpl):
        print("[错误] popup.png 写盘失败")
        return 1
    path = os.path.join(OUT_DIR, 'templates.yaml')
    with open(path, 'w', encoding='utf-8') as f:
        yaml.safe_dump({'star_roi': [int(v) for v in star_roi]}, f,
                       allow_unicode=True, sort_keys=False)
    print("\n[完成] 模板已保存到 lie_detector/：")
    print(f"    popup.png（弹窗特征，{popup_tpl.shape[1]}x{popup_tpl.shape[0]}）")
    print(f"    templates.yaml：star_roi = {star_roi}")
    print("    重启挂机即生效（引擎启动时会自动加载并打一条「[测谎] 守卫就绪」）。")
    return 0


def main():
    ap = argparse.ArgumentParser(
        description='制作测谎仪自动应答的模板（详见文件头说明）')
    ap.add_argument('--image', help='真机整窗截图 png（含标题栏；不传则现场抓帧）')
    ap.add_argument('--title', default='冒险岛', help='游戏窗口标题关键词（抓帧用）')
    ap.add_argument('--auto-popup', help='无头：弹窗特征框 x,y,w,h')
    ap.add_argument('--auto-stars', help='无头：星星活动区框 x,y,w,h')
    ap.add_argument('--no-save', action='store_true', help='只验证不落盘')
    args = ap.parse_args()
    if args.auto_popup and not args.auto_stars:
        print("[错误] --auto-popup 需要配合 --auto-stars")
        return 1
    try:
        return run(args)
    except KeyboardInterrupt:
        print("\n[取消] 未保存。")
        return 1


if __name__ == '__main__':
    sys.exit(main())
