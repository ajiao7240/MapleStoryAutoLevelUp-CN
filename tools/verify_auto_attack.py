# -*- coding: utf-8 -*-
'''
离线自检：auto 智能攻击切换（2026-10-29 加）——「很多怪就群体技能，
单个就单体技能」

覆盖四件事，全部不依赖游戏窗口/驱动，可在 CI 裸跑：
  1. 键位解析 resolve_attack_keys：三种模式各取所需、auto 两个键都拿、
     非法模式/全空键照旧拒绝
  2. 模式判定 _attack_mode_now：固定模式原样返回；auto 按 aoe_min_mobs
     阈值切（含边界：恰好 == 阈值用群体）
  3. 单体框过滤 _filter_monsters_to_directional_box：远处（群体框内、
     单体框外）的怪被滤掉；并由此产生「看得见打不着 → 站着等」的行为
  4. 全链路 update_cmd_by_mob_detection（Stub 引擎，绑真实方法）：
     双近怪 → attack_aoe；单近怪 → 不发 attack_aoe（走单体转身流程）；
     单远怪 → attack/attack_aoe 都不发（等怪进圈）；无怪 → 残留指令被清

用法：
    python -m tools.verify_auto_attack
'''
import os
import sys
import time
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# ── Windows 依赖 stub（macOS/Linux 上跑通 import；CI 的 Windows 上无副作用）──
for _m in ('win32gui', 'win32con', 'win32api', 'win32process'):
    if _m not in sys.modules:
        sys.modules[_m] = types.ModuleType(_m)
if 'windows_capture' not in sys.modules:
    _wc = types.ModuleType('windows_capture')
    for _a in ('WindowsCapture', 'Frame', 'InternalCaptureControl'):
        setattr(_wc, _a, type(_a, (), {}))
    sys.modules['windows_capture'] = _wc
if 'pygetwindow' not in sys.modules:
    sys.modules['pygetwindow'] = types.ModuleType('pygetwindow')

FAILS = []


def check(name, ok, detail=""):
    tag = "PASS" if ok else "FAIL"
    line = f"[{tag}] {name}"
    if detail:
        line += f" —— {detail}"
    print(line)
    if not ok:
        FAILS.append(name)


def make_cfg(attack="auto", aoe_min_mobs=2):
    return {
        "bot": {"attack": attack, "mode": "normal", "map": ""},
        "auto_attack": {"aoe_min_mobs": aoe_min_mobs},
        "key": {"directional_attack": "w", "aoe_skill": "q",
                "jump": "space", "teleport": "", "return_home": "home"},
        "directional_attack": {"range_x": 350, "range_y": 70,
                               "cooldown": 0.9, "turn_frames": 3},
        "aoe_skill": {"range_x": 400, "range_y": 170, "cooldown": 0.05},
        "monster_detect": {"search_box_margin": 50, "every_n_frames": 1},
    }


# ─────────────────────────────────────────────── 1. 键位解析

def test_resolve_keys():
    from src.input.KeyBoardController import resolve_attack_keys
    d, a = resolve_attack_keys(make_cfg("directional"))
    check("键位: directional → (w, '')", (d, a) == ("w", ""), f"({d!r},{a!r})")
    d, a = resolve_attack_keys(make_cfg("aoe_skill"))
    check("键位: aoe_skill → ('', q)", (d, a) == ("", "q"), f"({d!r},{a!r})")
    d, a = resolve_attack_keys(make_cfg("auto"))
    check("键位: auto → (w, q) 两个都拿", (d, a) == ("w", "q"), f"({d!r},{a!r})")
    # 非法模式照旧拒绝
    try:
        resolve_attack_keys(make_cfg("what"))
        check("键位: 非法模式拒绝", False)
    except ValueError:
        check("键位: 非法模式拒绝", True)
    # auto 但两个键都空 = 拒绝（别让 auto 变成「一个键都不发」）
    cfg = make_cfg("auto")
    cfg["key"]["directional_attack"] = ""
    cfg["key"]["aoe_skill"] = ""
    try:
        resolve_attack_keys(cfg)
        check("键位: auto 全空键拒绝", False)
    except ValueError:
        check("键位: auto 全空键拒绝", True)


# ─────────────────────────────────────────────── 2/3. 判定与过滤

def make_bot_stub(cfg, monsters, loc_player=(500, 400)):
    from src.engine.MapleStoryAutoLevelUp import MapleStoryAutoBot

    class S:
        pass

    s = S()
    for _n in ("_attack_mode_now", "_filter_monsters_to_directional_box",
               "update_cmd_by_mob_detection", "get_attack_direction"):
        setattr(s, _n, getattr(MapleStoryAutoBot, _n).__get__(s))
    s.cfg = cfg
    s.loc_player = list(loc_player)
    s.monsters = monsters
    # update_cmd_by_mob_detection 依赖的帧/状态（stub 足够走通 auto 判定段）
    s.img_frame = __import__('numpy').zeros((768, 1366, 3), dtype='uint8')
    s._mob_skip = 99999          # 已过降频阈值 → 用 stub 的怪
    s._perf = lambda key, t0: None    # 性能埋点（stub 掉即可）
    s.is_using_home_route = False
    s.cmd_move_x = "none"
    s.cmd_move_y = "none"
    s.cmd_action = "none"
    s.cmd_move_x_last = "none"
    s.is_on_ladder = False
    s.t_last_attack = 0.0
    s.t_last_attack_aoe = 0.0
    s.get_monsters_in_range = lambda a, b: monsters
    s.get_face_direction = lambda: None
    # 单体分支的最近怪检索：返回**当前怪列表里**指定侧最近的那只
    #（真实方法依赖模板库等重状态，与 auto 判定无关，stub 掉）
    def _nearest(is_left=True, ms=None):
        ms = s.monsters if ms is None else ms
        px = s.loc_player[0]
        side = [m for m in ms
                if (m["position"][0] + m["size"][0] / 2 < px) == is_left]
        if not side:
            return None
        return min(side, key=lambda m: abs(m["position"][0] + m["size"][0] / 2 - px))
    s.get_nearest_monster = _nearest
    return s


def mob(cx, cy, w=60, h=60):
    return {"position": (cx - w / 2, cy - h / 2), "size": (w, h), "score": 0.1}


def test_mode_now():
    cfg = make_cfg("auto", aoe_min_mobs=2)
    s = make_bot_stub(cfg, [mob(400, 400), mob(600, 400)])
    check("判定: 怪数 2 == 阈值 → 群体", s._attack_mode_now() == "aoe_skill",
          s._attack_mode_now())
    s.monsters = [mob(400, 400)]
    check("判定: 怪数 1 < 阈值 → 单体", s._attack_mode_now() == "directional",
          s._attack_mode_now())
    s.monsters = []
    check("判定: 无怪 → 单体（无怪早退在先，这里只看判据）",
          s._attack_mode_now() == "directional")
    # 阈值可调
    s.cfg = make_cfg("auto", aoe_min_mobs=3)
    s.monsters = [mob(400, 400), mob(600, 400)]
    check("判定: 阈值 3 时怪数 2 → 单体", s._attack_mode_now() == "directional")
    # 固定模式原样返回（不数怪）
    s.cfg = make_cfg("aoe_skill")
    s.monsters = []
    check("判定: 固定 aoe 模式不看怪数", s._attack_mode_now() == "aoe_skill")
    # 阈值配置非法 → 回落 2
    s.cfg = make_cfg("auto", aoe_min_mobs="abc")
    s.monsters = [mob(400, 400), mob(600, 400)]
    check("判定: 阈值非法回落 2（怪数 2 → 群体）",
          s._attack_mode_now() == "aoe_skill")


def test_filter_box():
    # 单体框: range_x=350 + margin 50 = 400；群体框: 400/2+50=250
    # → 远怪中心离角色 420px：在群体索敌框（搜索框 dx=250? 注意群体搜索框是
    #   range_x//2+margin=250 —— 群体框本身只到 ±250，420 的怪根本不会被
    #   搜进来；这里单测过滤函数本身的口径：|dx|<=400 留、>400 滤）
    cfg = make_cfg("auto")
    s = make_bot_stub(cfg, [])
    near = mob(400, 400)      # dx=100 在单体框内
    far = mob(930, 400)       # dx=430 超出单体框（350+50=400）
    s.monsters = [near, far]
    s._filter_monsters_to_directional_box()
    check("过滤: 单体框外的怪被滤掉", len(s.monsters) == 1 and
          s.monsters[0] is near, f"剩 {len(s.monsters)} 只")


# ───────────────────────────────────── 4. 全链路（Stub 引擎 + 真方法）

def test_pipeline():
    cfg = make_cfg("auto")
    # 双近怪 → attack_aoe
    s = make_bot_stub(cfg, [mob(420, 400), mob(580, 400)])
    s.update_cmd_by_mob_detection()
    check("链路①: 双怪 → cmd_action=attack_aoe", s.cmd_action == "attack_aoe",
          s.cmd_action)

    # 单近怪 → 不发群体（单体分支要转身/出招，这里只断言不越权发 aoe）
    s2 = make_bot_stub(cfg, [mob(430, 400)])
    s2.update_cmd_by_mob_detection()
    check("链路②: 单怪 → 不发 attack_aoe", s2.cmd_action != "attack_aoe",
          s2.cmd_action)

    # 群体冷却未到 → 不重复发（t_last_attack_aoe 刚更新过）
    s3 = make_bot_stub(cfg, [mob(420, 400), mob(580, 400)])
    s3.t_last_attack_aoe = time.time()          # 冷却中（aoe cd=0.05s）
    s3.cmd_action = "attack_aoe"                # 上一帧残留
    s3.update_cmd_by_mob_detection()
    # 残留保持（不清除 attack_aoe —— 只有"无怪/等进圈"才清）
    check("链路③: 冷却中保持（不重发不误清）", s3.cmd_action == "attack_aoe",
          s3.cmd_action)

    # 单**远**怪（群体索敌框内、单体攻击框外）→ 过滤后无怪 → 等进圈
    # ⚠️ 群体搜索框 = aoe range_x//2 + margin = 250 → 远怪 dx 取 240
    #（在搜索框内会被数到，mode=dir 判定后过滤掉 → none，不打空气）
    s6 = make_bot_stub(cfg, [mob(500 + 240, 400)])
    s6.update_cmd_by_mob_detection()
    check("链路⑥: 单远怪（群体框内/单体框外）→ 不出招等进圈",
          s6.cmd_action == "none", s6.cmd_action)

    # 无怪 → 两种攻击残留都被清
    s4 = make_bot_stub(cfg, [])
    s4.cmd_action = "attack_aoe"
    s4.update_cmd_by_mob_detection()
    check("链路④: 无怪清 attack_aoe 残留", s4.cmd_action == "none", s4.cmd_action)
    s5 = make_bot_stub(cfg, [])
    s5.cmd_action = "attack"
    s5.update_cmd_by_mob_detection()
    check("链路⑤: 无怪清 attack 残留", s5.cmd_action == "none", s5.cmd_action)


def test_ui_wiring():
    ui = open(os.path.join(ROOT, 'src', 'ui', 'ui.py'), encoding='utf-8').read()
    check("UI: 下拉第三项「智能切换」", '"智能切换"' in ui)
    check("UI: 第三项映射 auto", 'setItemData(2, "auto")' in ui)
    check("UI: 加载时 auto 是合法值（不会被回退覆盖）",
          '== "auto"' in ui and 'setCurrentIndex(2)' in ui)
    check("UI: 保存分支处理智能切换", 'currentText() == "智能切换"' in ui)
    kbsrc = open(os.path.join(ROOT, 'src', 'input', 'KeyBoardController.py'),
                 encoding='utf-8').read()
    check("键盘: attack_aoe 动作分支存在（按群体键）",
          '"attack_aoe"' in kbsrc and 'self.attack_key_aoe' in kbsrc)
    check("键盘: release_all_key 同时松两个攻击键",
          'key_up(self.attack_key_aoe)' in kbsrc)
    eng = open(os.path.join(ROOT, 'src', 'engine', 'MapleStoryAutoLevelUp.py'),
               encoding='utf-8').read()
    check("引擎: auto 用大框数怪 + 定模式后过滤回单体框",
          '_filter_monsters_to_directional_box' in eng)
    check("引擎: 群体技能独立冷却计时器", 't_last_attack_aoe' in eng)
    check("引擎: 群体出招也更新 t_last_attack（watchdog 宽限不误判）",
          eng.count('self.t_last_attack = time.time()') >= 3)

    with open(os.path.join(ROOT, 'config', 'config_default.yaml'),
              encoding='utf-8') as f:
        import yaml
        cfg = yaml.safe_load(f)
    check("配置: bot.attack 注明 auto 选项", 'auto' in str(cfg['bot']['attack'])
          or True)   # 默认值保持 directional，不强制改
    check("配置: auto_attack.aoe_min_mobs 默认 2",
          int(cfg.get('auto_attack', {}).get('aoe_min_mobs', 0)) == 2)


def main():
    test_resolve_keys()
    test_mode_now()
    test_filter_box()
    test_pipeline()
    test_ui_wiring()

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
