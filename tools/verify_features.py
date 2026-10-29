# -*- coding: utf-8 -*-
'''
离线自检：🧩 功能开关（2026-10-29 加）——「勾什么跑什么」

覆盖四件事，全部不依赖游戏窗口/驱动，可在 CI 裸跑：
  1. feature_enabled 判据：缺段/缺键回落默认；字符串宽松解析（防手改配置判反）
  2. 状态机分派：HuntingState.on_frame 按开关跳过对应段落，**且清残留指令**
     （只跳过不清 = 关掉移动角色还在走，是「没怪打空气」同款坑）
  3. 键盘线程门控：buff / 拾取关掉后一个键都不发
  4. 热切换链路：bot.set_feature 改的同一个 dict，hunting / kb 下一帧都能看到
     + 配置默认值（全开 = 老配置行为零变化）+ UI 接线（勾选框组存在、
       保存/加载/热切换接线齐全）

用法：
    python -m tools.verify_features
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


# ─────────────────────────────────────────────── 1. 判据

def test_feature_enabled():
    from src.utils.common import feature_enabled
    check("判据: 缺段回落默认(开)", feature_enabled({}, "move") is True)
    check("判据: 缺键回落默认(开)", feature_enabled({'features': {}}, "move") is True)
    check("判据: 显式 False 关", feature_enabled({'features': {'move': False}}, "move") is False)
    check("判据: 字符串 false/off/0/no 都算关",
          feature_enabled({'features': {'move': 'off'}}, 'move') is False and
          feature_enabled({'features': {'move': '0'}}, 'move') is False and
          feature_enabled({'features': {'move': 'no'}}, 'move') is False)
    check("判据: 字符串 on/任意非否词算开",
          feature_enabled({'features': {'move': 'on'}}, 'move') is True and
          feature_enabled({'features': {'move': '1'}}, 'move') is True)
    check("判据: 非字典配置不炸（默认开）",
          feature_enabled(None, 'move') is True)


# ───────────────────────────────────── 2. 状态机分派（真实方法 + stub bot）

class BotStub:
    '''能走通 HuntingState.on_frame 的假 bot —— 记录各段有没有被调。'''

    def __init__(self, features=None):
        self.cfg = {"features": features or {}}
        self.calls = []
        self.cmd_move_x = "right"          # 故意留残留：关掉移动后必须被清
        self.cmd_move_y = "none"
        self.cmd_action = "attack"         # 故意留残留：关掉攻击后必须被清
        self.cmd_move_x_last = "none"

    def feature_on(self, name):
        from src.utils.common import feature_enabled
        return feature_enabled(self.cfg, name)

    def update_cmd_by_route(self):
        self.calls.append("move")

    def check_reach_goal(self):
        self.calls.append("goal")

    def update_cmd_by_mob_detection(self):
        self.calls.append("attack")

    def is_player_stuck(self):
        self.calls.append("stuck_check")
        return False

    def handle_stuck(self):
        self.calls.append("stuck_handle")

    class _KB:
        command = None

        def set_command(self, cmd):
            BotStub.last_command = cmd

    kb = _KB()
    last_command = None


def test_hunting_dispatch():
    from src.states.hunting import HuntingState

    # 全开：各段都跑（老行为）
    bot = BotStub()
    HuntingState(None, bot).on_frame()
    check("分派①: 全开时 move/goal/attack 都跑",
          {"move", "goal", "attack"} <= set(bot.calls), bot.calls)
    check("分派①: 指令原样下发", BotStub.last_command == "right none attack",
          BotStub.last_command)

    # 只关移动：路线/切段跳过、stuck 检测跳过、移动残留被清
    bot = BotStub(features={"move": False})
    HuntingState(None, bot).on_frame()
    check("分派②: 关移动 → 不调路线/切段/卡死检测",
          not ({"move", "goal", "stuck_check"} & set(bot.calls)), bot.calls)
    check("分派②: 关移动 → 攻击照常", "attack" in bot.calls, bot.calls)
    check("分派②: 关移动 → 残留方向指令被清（none none attack）",
          BotStub.last_command == "none none attack", BotStub.last_command)

    # 只关攻击：攻击段跳过、攻击残留被清、移动照常
    bot = BotStub(features={"attack": False})
    HuntingState(None, bot).on_frame()
    check("分派③: 关攻击 → 不调怪检测", "attack" not in bot.calls, bot.calls)
    check("分派③: 关攻击 → 移动照常", "move" in bot.calls, bot.calls)
    check("分派③: 关攻击 → 残留 attack 被清（right none none）",
          BotStub.last_command == "right none none", BotStub.last_command)

    # 全关：只剩指令下发（全 none）
    bot = BotStub(features={"move": False, "attack": False})
    HuntingState(None, bot).on_frame()
    check("分派④: 全关 → 指令全 none（站着，只剩喝药/测谎/buff）",
          BotStub.last_command == "none none none", BotStub.last_command)

    # attack_aoe 残留（智能切换的指令）同样要清
    bot = BotStub(features={"attack": False})
    bot.cmd_action = "attack_aoe"
    HuntingState(None, bot).on_frame()
    check("分派⑤: 关攻击 → attack_aoe 残留也被清",
          BotStub.last_command == "right none none", BotStub.last_command)


# ───────────────────────────────────── 3. 键盘线程门控（buff / 拾取）

def make_kb_stub(cfg, pickup_key="z"):
    from src.input.KeyBoardController import KeyBoardController
    kb = KeyBoardController.__new__(KeyBoardController)   # 不跑 __init__（会起线程）
    kb.cfg = cfg
    kb.pickup_key = pickup_key
    kb.pickup_cooldown = 2.0
    kb.t_last_pickup = 0.0
    kb._t_next_pickup = 1e-9          # 早已到点 → 门一开就会按
    kb.n_pickup_sent = 0
    kb.t_last_buff_cast = [0.0]
    kb.t_last_skill = 0.0
    kb.n_key_sent = 0
    kb._sent = []
    kb._press = lambda k: (kb._sent.append(k), setattr(kb, 'n_key_sent', kb.n_key_sent + 1))
    return kb


def test_kb_gates():
    from src.input.KeyBoardController import KeyBoardController

    # 拾取开关
    cfg = {"features": {"pickup": True},
           "key": {"pickup": "z", "pickup_cooldown": 2.0},
           "buff_skill": {"keys": [], "cooldown": [], "action_cooldown": 1}}
    kb = make_kb_stub(cfg)
    kb._try_pickup(time.time())
    check("键盘①: 拾取开着 → 到点发键", kb._sent == ["z"], kb._sent)

    cfg["features"]["pickup"] = False
    kb = make_kb_stub(cfg)
    kb._try_pickup(time.time())
    check("键盘②: 拾取关了 → 一个键不发", kb._sent == [], kb._sent)

    # buff 开关：直接验证门控表达式 + 源码接线（buff 循环在 run() 线程里，
    # 离线起不了线程，用源码断言钉住它被 feature_enabled 包住）
    kbsrc = open(os.path.join(ROOT, 'src', 'input', 'KeyBoardController.py'),
                 encoding='utf-8').read()
    check("键盘③: buff 循环被功能开关包住",
          'feature_enabled(self.cfg, "buff")' in kbsrc)
    check("键盘④: 拾取门在 _try_pickup 开头",
          kbsrc.index('feature_enabled(self.cfg, "pickup")') <
          kbsrc.index('if not self.pickup_key:'))


# ─────────────────────────────── 4. 热切换 + 配置 + UI 接线

def test_hot_toggle_and_wiring():
    from src.engine.MapleStoryAutoLevelUp import MapleStoryAutoBot

    class S:
        pass
    s = S()
    s.cfg = {"features": {"move": True}}
    s.set_feature = getattr(MapleStoryAutoBot, "set_feature").__get__(s)
    s.set_feature("move", False)
    s.set_feature("buff", True)
    check("热切换: set_feature 改的是同一个 cfg dict",
          s.cfg["features"] == {"move": False, "buff": True}, s.cfg["features"])

    # kb.cfg 与 bot.cfg 同引用 → 一处改两处看到（构造签名 KeyBoardController(self.cfg)）
    eng = open(os.path.join(ROOT, 'src', 'engine', 'MapleStoryAutoLevelUp.py'),
               encoding='utf-8').read()
    check("热切换: kb 拿的是 cfg 引用（非拷贝）",
          "KeyBoardController(self.cfg)" in eng)
    check("热切换: 引擎有 set_feature / feature_on",
          "def set_feature" in eng and "def feature_on" in eng)

    import yaml
    with open(os.path.join(ROOT, 'config', 'config_default.yaml'),
              encoding='utf-8') as f:
        cfg = yaml.safe_load(f)
    feats = cfg.get("features", {})
    check("配置: features 四项默认全开（老配置行为零变化）",
          all(feats.get(k) is True for k in ("move", "attack", "buff", "pickup")),
          feats)

    ui = open(os.path.join(ROOT, 'src', 'ui', 'ui.py'), encoding='utf-8').read()
    check("UI: 功能开关组存在（四个勾选框）",
          "create_features_gbox" in ui and ui.count("QCheckBox(label)") >= 1)
    check("UI: 勾选 → 热切换到引擎",
          "on_feature_toggled" in ui and "set_feature" in ui)
    check("UI: 加载时按配置纠正勾选态",
          "feature_enabled" in ui and "setChecked(" in ui)
    check("UI: 保存时写回 features 段", 'setdefault("features", {})' in ui)


def main():
    test_feature_enabled()
    test_hunting_dispatch()
    test_kb_gates()
    test_hot_toggle_and_wiring()

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
