from src.states.base_state import State

class HuntingState(State):
    def on_enter(self):
        pass

    def on_exit(self):
        pass

    def check_transitions(self):
        # 国服怀旧服无符文系统，打怪状态不再需要转移到其它状态
        return None

    def on_frame(self):
        # 🧩 功能开关（2026-10-29 加，界面「功能开关」勾选框）：
        # 不勾的功能**整段跳过**，且必须把该功能残留的指令清掉 ——
        # cmd_* 是跨帧保持的实例属性，只跳过不清 = 关掉移动角色还在走
        # （跟 2026-09-12 修的「没怪还打空气」是同一类坑，别再踩）。
        #
        # ⚠️ 关掉移动时同时跳过卡死检测：角色是**用户故意**让它站着
        #（比如只挂 buff / 只喝药），不是卡住 —— 看门狗每 10s 弹一次
        # 置顶告警会把人烦死。
        move_on = self.bot.feature_on("move")
        if move_on:
            # 指令来源按挂机方式分派：
            # 2026-09-12：定点模式（stand）已按用户要求删除 —— 站着不动的收益
            # 不如"来回走两步顺便打怪"，统一走巡逻。指令来源只剩路线一条。
            self.bot.update_cmd_by_route()

            # Check if reach goal on route map
            self.bot.check_reach_goal()
        else:
            self.bot.cmd_move_x = 'none'
            self.bot.cmd_move_y = 'none'

        # Get attack commend by detecting mobs near players
        if self.bot.feature_on("attack"):
            self.bot.update_cmd_by_mob_detection()
        else:
            if self.bot.cmd_action in ("attack", "attack_aoe"):
                self.bot.cmd_action = 'none'

        # 卡死处理（2026-09-15 改）
        # 原来固定调 update_cmd_by_random（12 种组合**均匀乱抽**，不知道悬崖在哪）：
        # ① 窄平台上乱抽 = 直接掉下去（实测抽到 right none jump，右边正好是悬崖）；
        # ② 它把真因**盖住**了（随机跳把角色从 (100,122) 踹到 (100,118)，
        #    症状从"在原地蹭"变成"跳到另一层"，日志上看"它动了"，更难查）。
        # 现在按 config 的 watchdog.on_stuck 分派，默认 alert = **不乱动 + 弹置顶提示**。
        if move_on and self.bot.is_player_stuck():
            self.bot.handle_stuck()

        # send command to keyboard controller
        self.bot.kb.set_command(self.bot.cmd_move_x + ' ' + \
                                self.bot.cmd_move_y + ' ' + \
                                self.bot.cmd_action)

        # 记录本帧**实际发出**的左右指令（2026-09-12 加）
        # 供下一帧判断「是否已经转向」—— 见 update_cmd_by_mob_detection 的先转身后出招。
        # 必须在 set_command 之后更新，否则记到的是本帧被覆盖前的值。
        self.bot.cmd_move_x_last = self.bot.cmd_move_x
