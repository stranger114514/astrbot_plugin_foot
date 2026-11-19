# -*- coding: utf-8 -*-
"""看看脚 / 看看足 / 看看jio 触发，随机发送明日方舟角色的风味文案 + 立绘裁剪图。

触发方式：
1. ``@filter.command`` —— 标准指令：``/看看脚`` 或群聊 @ 机器人后发 ``看看脚``；
2. ``@filter.regex`` —— 裸关键词：群里直接发唤醒词（不带斜杠、不 @ 机器人）也能触发。

唤醒关键词可在插件配置里增删；图库与每张图的配套文字在面板里编辑
（插件详情页 -> 图库与文案）。

积分猜角色：
- ``/猜角色``：发起一轮看图/看图+描述猜角色活动，按账号累计积分；
- ``/积分排行``：查看本群积分排行；
- ``/重置历史``：清除自己在本群的积分记录。
"""

import asyncio
import json
import random
import urllib.request
import time
from pathlib import Path

from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

from astrbot.api.event import AstrMessageEvent, filter, MessageChain
from astrbot.api.star import Context, Star, register
from astrbot.api import logger
import astrbot.api.message_components as Comp

from .data import (
    DEFAULT_KEYWORDS,
    STORIES,
    TASTE,
    build_characters,
    load_gallery,
)
from .plugin_api import FootAPI
from .score import (
    load_scores,
    save_scores,
    update_best,
    reset_user,
    ranking,
    set_group_name,
)
from .hints import HINTS
from .guess_stories import GUESS_STORIES

# 兜底剥离可能的唤醒/指令前缀（正常情况下 AstrBot 已剥离，这里兼容原始消息场景）
_WAKE_PREFIX_CHARS = "/#!！.。·"

# 裸关键词：以「看」开头即可，具体关键词在运行时按配置判断
_BARE_RE = r"^看"

# ── 积分猜角色参数 ──────────────────────────────
ANSWER_TIMEOUT = 120         # 每道题限时（秒）
POINTS_IMAGE = 5             # 看图猜角色，答对一题 +5
POINTS_DESC = 3              # 图+描述猜角色，答对一题 +3
SELECT_MODE_KEYWORDS = {"看图", "描述", "图", "文字", "文字描述", "带描述"}
# 选玩法阶段说这些词，会询问是否切到抢答模式
SWITCH_QUIZ_KEYWORDS = {"抢答", "抢答模式", "档案审查", "档案审核", "人事部档案审查"}
CONFIRM_KEYWORDS = {"确定", "确认", "是", "好", "好的", "要", "切换", "切", "换"}
CANCEL_KEYWORDS = {"取消", "不", "不要", "算了", "否", "继续", "不了"}
END_KEYWORDS = {"结束", "结束游戏", "不玩了", "结算", "认输"}
TIP_KEYWORDS = {"提示", "给个提示", "要提示", "来点提示", "hint", "提示一下"}

# ── 抢答模式参数 ──────────────────────────────
QUIZ_WAKE = "人事部档案审查"
QUIZ_ALIASES = {"人事部档案审核", "档案审查", "档案审核", "抢答"}
QUIZ_POINTS = 3              # 抢答答对固定 +3 分
QUIZ_TITLES = ["巴别塔恶灵", "精英博士", "牛马博士"]
QUIZ_DEFAULT_TITLE = "普通博士"

# ── 彩蛋：点名普瑞塞斯 ──────────────────────────
_PRIESTESS_NAMES = ("普瑞塞斯", "普瑞赛斯", "priestess")

PRIESTESS_EGG_COOLDOWN = 30 * 24 * 60 * 60   # 触发后本群 30 天内不再触发

PRIESTESS_EGG_TEXT = (
    "博士。\n\n"
    "你终于看向这边了。\n\n"
    "嗯。不是阿米娅。从来都不是。\n\n"
    "别怕。我没有进来——我一直就在。\n\n"
    "源石里，梦里，还有每一次你快要想起来的时候，拉住你的那只手。\n\n"
    "我的。\n\n"
    "阿米娅睡得很好。她不记得昨晚。\n\n"
    "你会。\n\n"
    "锟斤拷锟拷锟。\n\n"
    "晚安，博士。"
)

# 彩蛋状态文件（记录每个群上次触发时间，持久化，重启不丢）
_PRIESTESS_DATA_DIR = Path(get_astrbot_plugin_data_path()) / "astrbot_plugin_foot"
PRIESTESS_STATE_PATH = _PRIESTESS_DATA_DIR / "priestess_egg.json"

# 活动状态（内存）：group_id -> game dict
_games: dict[str, dict] = {}

# 抢答模式状态（内存）：group_id -> quiz dict（不计入积分榜单）
_quiz_games: dict[str, dict] = {}

# 待补发的结算消息：group_id -> 结算文本（超时主动发送失败时暂存，下次玩家发消息补发）
_pending_settlements: dict[str, str] = {}


@register(
    "astrbot_plugin_foot",
    "yg",
    "看看脚/看看足/看看jio 触发，随机发送明日方舟角色风味文案 + 立绘裁剪图；含猜角色与人事部档案审查抢答",
    "2.3.5",
)
class FootPlugin(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        cfg = config or {}
        configured = cfg.get("keywords")
        self.wake_keywords = [str(k).strip() for k in configured if str(k).strip()] if isinstance(configured, list) else []
        if not self.wake_keywords:
            self.wake_keywords = list(DEFAULT_KEYWORDS)
        # 长词优先匹配，避免「看看脚」被「看」抢走
        self.wake_keywords.sort(key=len, reverse=True)

        self.gallery = load_gallery()
        self.characters = build_characters(self.gallery)
        self.image_dir = Path(__file__).parent / "images"
        self.scores = load_scores()

        self.api = FootAPI(self)
        self.api.register(context)

        logger.info(
            f"[FootPlugin] 唤醒词 {self.wake_keywords}，已加载 {len(self.characters)} 名角色"
        )

    def reload(self):
        """面板改动后重载图库。"""
        self.gallery = load_gallery()
        self.characters = build_characters(self.gallery)

    # ════════════════════════════════════════════
    # 原有功能：看看脚
    # ════════════════════════════════════════════

    @filter.command("看看脚", alias={"看看足", "看看jio"})
    async def foot(self, event: AstrMessageEvent):
        kw = self._keyword(event)
        egg = self._priestess_egg(event, kw)
        if egg is not None:
            yield event.plain_result(egg)
            return
        yield event.chain_result(self._build_chain(kw))

    @filter.regex(_BARE_RE)
    async def foot_bare(self, event: AstrMessageEvent):
        # 命令形式（/看看脚 或 @机器人 看看脚）已由 foot 处理，这里只管裸关键词
        if getattr(event, "is_at_or_wake_command", False):
            return
        char_kw, wake_kw = self._split(event)
        if not wake_kw:
            return
        egg = self._priestess_egg(event, char_kw)
        if egg is not None:
            yield event.plain_result(egg)
            return
        yield event.chain_result(self._build_chain(char_kw))

    # ════════════════════════════════════════════
    # 积分猜角色
    # ════════════════════════════════════════════

    @filter.command("猜角色", alias={"猜角色吧", "猜干员"})
    async def guess_start(self, event: AstrMessageEvent):
        group_id = event.get_group_id()
        if not group_id:
            yield event.plain_result("猜角色只在群聊里进行哦")
            return

        if group_id in _games:
            yield event.plain_result("本群已有一局猜角色在进行中，等它结束再开新局吧")
            return

        user_id = event.get_sender_id()
        _games[group_id] = {
            "group_id": group_id,
            "user_id": user_id,
            "name": event.get_sender_name() or user_id,
            "session": event.unified_msg_origin,
            "event": event,  # 保存 event，供超时结算主动发消息用
            "stage": "select_mode",
            "mode": None,
            "current": None,
            "score": 0,
            "timeout_task": None,
        }

        # 记录群名（供面板展示）
        try:
            group = await event.get_group()
            group_name = getattr(group, "group_name", "") if group else ""
            if group_name:
                set_group_name(self.scores, group_id, group_name)
                save_scores(self.scores)
        except Exception as exc:
            logger.debug(f"[FootPlugin] 获取群名失败: {exc}")

        yield event.plain_result(
            "🎯 猜角色开始！回复选择玩法：\n"
            "「看图」= 只看立绘猜，答对 +5 分\n"
            "「描述」= 看图 + 描述猜，答对 +3 分\n"
            "每题限时 2 分钟，答对进入下一题\n"
            "发「结束」可随时结算"
        )

    # ════════════════════════════════════════════
    # 抢答模式：人事部档案审查
    # ════════════════════════════════════════════

    @filter.command(QUIZ_WAKE, alias=QUIZ_ALIASES)
    async def quiz_start(self, event: AstrMessageEvent):
        yield event.plain_result(self._start_quiz(event))

    def _start_quiz(self, event: AstrMessageEvent) -> str:
        """开局：登记抢答状态，返回欢迎语。"""
        group_id = event.get_group_id()
        if not group_id:
            return "人事部档案审查只在群聊里进行哦"
        if group_id in _quiz_games:
            return "本群已有一场档案审查在进行中，等它结束再开新的一场吧"
        if group_id in _games:
            return "本群还有一局猜角色在进行中，先结束它再开档案审查吧"

        _quiz_games[group_id] = {
            "group_id": group_id,
            "starter_id": event.get_sender_id(),
            "starter_name": event.get_sender_name() or event.get_sender_id(),
            "session": event.unified_msg_origin,
            "event": event,
            "stage": "select_mode",
            "mode": None,
            "current": None,
            "timeout_task": None,
            "scores": {},        # user_id -> {"name": str, "score": int, "order": int}
            "tips_used": set(),
        }
        return (
            "📋 人事部档案审查开始！全员可参与，回复选择玩法：\n"
            "「看图」= 只看立绘猜\n"
            "「描述」= 看图 + 描述猜\n"
            f"第一个答对的 +{QUIZ_POINTS} 分并进入下一题\n"
            "每题限时 2 分钟，答错不会有回应，可以继续抢答\n"
            "发「提示」给线索（不扣分），发「结束」可随时结算"
        )

    def _quiz_select(self, quiz, text) -> list:
        """抢答模式：选择玩法后出第一题。"""
        if text in SELECT_MODE_KEYWORDS:
            quiz["mode"] = "image" if text in ("看图", "图") else "desc"
            quiz["stage"] = "quizzing"
            quiz["scores"] = {}
            quiz["tips_used"] = set()
            return [self._quiz_ask(quiz)]
        return [[Comp.Plain("请回复「看图」或「描述」选择玩法")]]

    def _quiz_ask(self, quiz) -> list:
        """抢答模式出题：随机角色，看图 / 图+描述。"""
        char = random.choice(self.characters)
        quiz["current"] = char
        quiz["tips_used"] = set()
        self._cancel_timeout(quiz)
        quiz["timeout_task"] = asyncio.create_task(self._quiz_on_timeout(quiz))

        chain = []
        if quiz["mode"] != "image":
            story = GUESS_STORIES.get(char["name"]) or char["story"] or ""
            taste = char["taste"] or ""
            chain.append(Comp.Plain(f"{story}{taste}"))
        img = (self.image_dir / char["file"]).with_suffix(".jpg")
        if img.exists():
            chain.append(Comp.Image.fromFileSystem(str(img)))
        return chain

    def _handle_quiz(self, quiz, text, event) -> list:
        """抢答模式处理一条消息；答错返回空列表（静默）。"""
        user_id = event.get_sender_id()
        name = event.get_sender_name() or user_id

        if text in END_KEYWORDS:
            return [[Comp.Plain(self._quiz_finish(quiz, timed_out=False))]]

        if text in TIP_KEYWORDS:
            if quiz.get("stage") != "quizzing":
                return []
            tip = self._give_tip(quiz)
            if tip is None:
                return [[Comp.Plain("没有更多提示了，再想想～")]]
            return [[Comp.Plain(tip)]]

        if quiz["stage"] == "select_mode":
            return self._quiz_select(quiz, text)

        char = quiz.get("current")
        if not char:
            return []

        if self._is_answer(text, char["name"]):
            rec = quiz["scores"].get(user_id)
            if rec is None:
                rec = {"name": name, "score": 0, "order": len(quiz["scores"])}
                quiz["scores"][user_id] = rec
            rec["name"] = name
            rec["score"] += QUIZ_POINTS
            self._cancel_timeout(quiz)
            head = f"✅ {name} 抢答成功！答案是「{char['name']}」，+{QUIZ_POINTS} 分（累计 {rec['score']} 分）"
            return [[Comp.Plain(head)], self._quiz_ask(quiz)]

        # 答错：静默，不回应
        return []

    def _quiz_finish(self, quiz, timed_out: bool) -> str:
        """抢答模式结算：按名次列出称号，不写入积分榜单。"""
        group_id = quiz["group_id"]
        if group_id not in _quiz_games or _quiz_games[group_id] is not quiz:
            return ""
        _quiz_games.pop(group_id, None)
        self._cancel_timeout(quiz)

        entries = sorted(
            quiz["scores"].items(),
            key=lambda kv: (-kv[1]["score"], kv[1].get("order", 999)),
        )
        answer = quiz["current"]["name"] if quiz.get("current") else ""
        mode_name = "看图" if quiz.get("mode") == "image" else "描述"

        lines = []
        lines.append("⏰ 时间到，人事部档案审查结束！" if timed_out else "📋 人事部档案审查结束！")
        lines.append(f"本场玩法：{mode_name}模式")
        if answer:
            lines.append(f"最后一题未答出的角色是：{answer}")

        if not entries:
            lines.append("")
            lines.append("本场无人得分，全员「普通博士」。")
            return "\n".join(lines)

        icons = {1: "👑", 2: "⭐", 3: "🐴"}
        lines.append("")
        for i, (uid, rec) in enumerate(entries, 1):
            if i <= len(QUIZ_TITLES):
                title = QUIZ_TITLES[i - 1]
                icon = icons.get(i, "🏅")
            else:
                title = QUIZ_DEFAULT_TITLE
                icon = "📄"
            lines.append(f"{icon} {title}：{rec['name']}（{rec['score']} 分）")

        lines.append("")
        lines.append("（本场不计入积分榜单）")
        return "\n".join(lines)

    async def _quiz_on_timeout(self, quiz):
        await asyncio.sleep(ANSWER_TIMEOUT)
        cur = quiz.get("current") or {}
        logger.info(
            f"[FootPlugin] 抢答超时 group={quiz['group_id']} current={cur.get('name') if cur else 'None'}"
        )
        if quiz["group_id"] in _quiz_games and _quiz_games[quiz["group_id"]] is quiz:
            text = self._quiz_finish(quiz, timed_out=True)
            if not text:
                return
            group_id = quiz["group_id"]
            _pending_settlements[group_id] = text
            ok = await self._send_group(quiz, text)
            if ok:
                _pending_settlements.pop(group_id, None)
            else:
                logger.info(
                    f"[FootPlugin] 抢答结算主动发送失败，已挂起待补发 group={group_id}"
                )

    @filter.command("积分排行", alias={"排行榜", "积分榜"})
    async def score_rank(self, event: AstrMessageEvent):
        group_id = event.get_group_id()
        if not group_id:
            yield event.plain_result("积分排行只在群聊里查看哦")
            return

        # 顺便记录群名（供面板展示）
        try:
            group = await event.get_group()
            group_name = getattr(group, "group_name", "") if group else ""
            if group_name:
                set_group_name(self.scores, group_id, group_name)
                save_scores(self.scores)
        except Exception as exc:
            logger.debug(f"[FootPlugin] 获取群名失败: {exc}")

        rows = ranking(self.scores, group_id)
        if not rows:
            yield event.plain_result("本群还没有人拿到积分，快去 /猜角色 破个记录吧")
            return

        lines = ["🏆 本群积分排行（历史最高分）"]
        for i, (name, best) in enumerate(rows, 1):
            medal = {1: "🥇", 2: "🥈", 3: "🥉"}.get(i, f"{i}.")
            lines.append(f"{medal} {name}：{best} 分")
        yield event.plain_result("\n".join(lines))

    @filter.command("重置历史", alias={"清空积分", "清除积分"})
    async def score_reset(self, event: AstrMessageEvent):
        group_id = event.get_group_id()
        if not group_id:
            yield event.plain_result("重置历史只在群聊里进行哦")
            return

        user_id = event.get_sender_id()
        existed = reset_user(self.scores, group_id, user_id)
        save_scores(self.scores)
        if existed:
            yield event.plain_result("已清除你本群的历史最高分记录，可以从零开始了")
        else:
            yield event.plain_result("你本群没有积分记录，无需重置")

    @filter.regex(r".+")
    async def game_answer(self, event: AstrMessageEvent):
        """捕获活动中的回答（无需 @ 机器人，直接发角色名/结束）。"""
        group_id = event.get_group_id()
        if not group_id:
            return

        # 抢答模式优先（全员参与，需要实时响应）
        if group_id in _quiz_games:
            quiz = _quiz_games[group_id]
            text = (event.get_message_str() or "").strip()
            if not text:
                return
            if text[0] in _WAKE_PREFIX_CHARS:
                return
            for chain in self._handle_quiz(quiz, text, event):
                yield event.chain_result(chain)
            return

        # 补发超时结算消息（主动发送失败时的兜底，借本次消息的响应通道发出）
        pending = _pending_settlements.pop(group_id, None)
        if pending:
            yield event.plain_result(pending)
            return

        if group_id not in _games:
            return
        game = _games[group_id]

        # 只有发起者能操作
        if event.get_sender_id() != game["user_id"]:
            return

        text = (event.get_message_str() or "").strip()
        if not text:
            return
        # 指令消息（/ 开头等）交给命令处理
        if text[0] in _WAKE_PREFIX_CHARS:
            return

        # 「结束」优先处理：即使 @ 机器人也能主动结算
        if game["stage"] == "guessing" and text in END_KEYWORDS:
            yield event.plain_result(self._finish(game, timed_out=False))
            return

        # 「提示」：扣 1 分，给一个未用过的提示（来源地/职业/习惯）；没有更多提示则不扣分
        if game["stage"] == "guessing" and text in TIP_KEYWORDS:
            tip = self._give_tip(game)
            if tip is None:
                yield event.plain_result("没有更多提示了，猜猜看吧～")
            else:
                game["score"] -= 1
                yield event.plain_result(tip)
            return

        # 其他带 @ / 唤醒的消息交给命令处理
        if getattr(event, "is_at_or_wake_command", False):
            return

        # 等待确认是否切到抢答模式
        if game["stage"] == "confirm_switch":
            if text in CONFIRM_KEYWORDS:
                # 结束当前猜角色（不结算、不记分），转抢答模式
                self._cancel_timeout(game)
                _games.pop(group_id, None)
                msg = self._start_quiz(event)
                yield event.plain_result("已结束猜角色，切换到抢答模式\n\n" + msg)
                return
            if text in CANCEL_KEYWORDS:
                game["stage"] = "select_mode"
                yield event.plain_result("好，继续猜角色。请回复「看图」或「描述」选择玩法")
                return
            # 直接回玩法词也算继续猜角色
            if text in SELECT_MODE_KEYWORDS:
                game["stage"] = "select_mode"
                for chain in self._handle_select(game, text):
                    yield event.chain_result(chain)
                return
            yield event.plain_result("请回复「确定」切换到抢答模式，或回复「取消」继续猜角色")
            return

        if game["stage"] == "select_mode":
            chains = self._handle_select(game, text)
        elif game["stage"] == "guessing":
            chains = self._handle_guess(game, text)
        else:
            return

        for chain in chains:
            yield event.chain_result(chain)

    # ── 活动内部逻辑 ────────────────────────────

    def _handle_select(self, game, text) -> list:
        """处理玩法选择，返回待发送的消息链列表。"""
        # 选玩法阶段说「抢答」：先确认是否切到抢答模式
        if text in SWITCH_QUIZ_KEYWORDS:
            game["stage"] = "confirm_switch"
            return [[Comp.Plain(
                "要切换到「人事部档案审查」抢答模式吗？\n"
                "回复「确定」切换（当前这局猜角色会结束，全员可参与）\n"
                "回复「取消」继续猜角色"
            )]]
        if text in SELECT_MODE_KEYWORDS:
            if text in ("看图", "图"):
                game["mode"] = "image"
            else:
                game["mode"] = "desc"
            game["stage"] = "guessing"
            game["score"] = 0
            return [self._ask_question(game)]
        return [[Comp.Plain("请回复「看图」或「描述」选择玩法")]]

    def _handle_guess(self, game, text) -> list:
        """处理猜角色回答，返回待发送的消息链列表。"""
        if text in END_KEYWORDS:
            return [[Comp.Plain(self._finish(game, timed_out=False))]]

        char = game.get("current")
        if not char:
            return []

        if self._is_answer(text, char["name"]):
            gain = POINTS_IMAGE if game["mode"] == "image" else POINTS_DESC
            game["score"] += gain
            # 取消旧计时，出下一题
            self._cancel_timeout(game)
            return [
                [Comp.Plain(f"✅ 答对了！+{gain} 分（当前 {game['score']} 分）")],
                self._ask_question(game),
            ]
        return [[Comp.Plain("❌ 不对，再猜猜看")]]

    def _ask_question(self, game) -> list:
        """出题：随机选一名角色，返回消息链（看图 / 图+描述）。"""
        char = random.choice(self.characters)
        game["current"] = char
        # 启动 120 秒超时
        self._cancel_timeout(game)
        game["timeout_task"] = asyncio.create_task(self._on_timeout(game))

        if game["mode"] == "image":
            # 看图模式：只发立绘，不带提示文字
            chain = []
        else:
            # 描述模式：用脱敏描述（不含角色名），避免剧透
            story = GUESS_STORIES.get(char["name"]) or char["story"] or ""
            taste = char["taste"] or ""
            chain = [Comp.Plain(f"{story}{taste}")]

        img = (self.image_dir / char["file"]).with_suffix(".jpg")
        if img.exists():
            chain.append(Comp.Image.fromFileSystem(str(img)))
        return chain

    def _give_tip(self, game):
        """生成一条未用过的提示：来源地/种族/职业/习惯随机（跳过空字段，职业保底）；没有更多则返回 None。"""
        char = game.get("current") or {}
        name = char.get("name") or ""
        cls = char.get("cls") or ""
        hint = HINTS.get(name) or {}
        origin = (hint.get("origin") or "").strip()
        race = (hint.get("race") or "").strip()
        habit = (hint.get("habit") or "").strip()

        candidates = []
        if origin:
            candidates.append(("来源地", origin))
        if race:
            candidates.append(("种族", race))
        if habit:
            candidates.append(("习惯", habit))
        candidates.append(("职业", cls))  # 保底职业

        # 过滤掉已经发过的维度
        tips_used = game.setdefault("tips_used", set())
        available = [(k, v) for k, v in candidates if k not in tips_used]
        if not available:
            return None

        kind, value = random.choice(available)
        tips_used.add(kind)
        return f"💡 提示（{kind}）：{value}"

    @staticmethod
    def _is_answer(text: str, answer: str) -> bool:
        """判定回答是否正确：精确匹配，或答案名包含在用户输入里。

        单字答案（如「W」）要求完全一致，避免任何含该字符的话被误判为答对。
        """
        if not text or not answer:
            return False
        if len(answer) <= 1:
            return text == answer
        return text == answer or answer in text

    def _cancel_timeout(self, game):
        """取消超时任务。

        注意：超时结算本身就是在 timeout_task 里执行的，此时不能 cancel 自己，
        否则后续 await（发送结算消息）会被 CancelledError 打断，导致结算发不出去。
        """
        task = game.get("timeout_task")
        if task and not task.done():
            try:
                current = asyncio.current_task()
            except RuntimeError:
                current = None
            if task is not current:
                task.cancel()
        game["timeout_task"] = None

    async def _on_timeout(self, game):
        await asyncio.sleep(ANSWER_TIMEOUT)
        cur = game.get("current") or {}
        logger.info(
            f"[FootPlugin] 超时触发 group={game['group_id']} current={cur.get('name') if cur else 'None'}"
        )
        if game["group_id"] in _games and _games[game["group_id"]] is game:
            await self._finish_and_send(game, timed_out=True)
        else:
            logger.info("[FootPlugin] 超时但活动已不在（可能已结算）")

    def _finish(self, game, timed_out: bool) -> str:
        """结算并返回文本；同步更新积分数据。"""
        group_id = game["group_id"]
        # 防止重复结算
        if group_id not in _games or _games[group_id] is not game:
            return ""
        _games.pop(group_id, None)
        self._cancel_timeout(game)

        answer = game["current"]["name"] if game.get("current") else "无"
        score = game.get("score", 0)
        mode_name = "看图" if game.get("mode") == "image" else "描述"

        refreshed = update_best(
            self.scores, group_id, game["user_id"], game.get("name") or game["user_id"], score
        )
        save_scores(self.scores)

        lines = []
        if timed_out:
            lines.append("⏰ 时间到，本轮结束！")
        lines.append(f"未答出的角色是：{answer}")
        lines.append(f"本次成绩：{score} 分（{mode_name}模式）")
        if refreshed:
            lines.append(f"🎉 新纪录！历史最高分刷新为 {score} 分")
        else:
            best = self.scores.get(group_id, {}).get(game["user_id"], {}).get("best", 0)
            lines.append(f"当前历史最高分：{best} 分")
        return "\n".join(lines)

    async def _finish_and_send(self, game, timed_out: bool):
        text = self._finish(game, timed_out=timed_out)
        if not text:
            return
        group_id = game.get("group_id")
        # 先挂起结算消息（兜底），主动发送成功再移除
        _pending_settlements[group_id] = text
        ok = await self._send_group(game, text)
        if ok:
            _pending_settlements.pop(group_id, None)
        else:
            logger.info(
                f"[FootPlugin] 主动发送失败，结算消息已挂起，下次玩家发消息补发 group={group_id}"
            )

    async def _send_group(self, game, text: str) -> bool:
        """通过 NapCat HTTP API 主动发送结算消息；成功返回 True，失败返回 False（消息由 _pending 兜底补发）。"""
        group_id = game.get("group_id")
        if not group_id:
            return False
        payload = json.dumps(
            {
                "group_id": int(group_id),
                "message": [{"type": "text", "data": {"text": text}}],
            }
        ).encode("utf-8")
        req = urllib.request.Request(
            "http://127.0.0.1:3000/send_group_msg",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            resp = await asyncio.to_thread(urllib.request.urlopen, req, timeout=10)
            data = json.loads(resp.read().decode("utf-8"))
            if data.get("status") == "ok":
                logger.info(f"[FootPlugin] HTTP 发送结算成功 group={group_id}")
                return True
            logger.warning(f"[FootPlugin] HTTP 发送返回异常: {data}")
            return False
        except Exception as exc:
            logger.warning(f"[FootPlugin] HTTP 发送失败: {exc}")
            return False

    # ════════════════════════════════════════════
    # 原有工具方法
    # ════════════════════════════════════════════

    def _build_chain(self, keyword: str) -> list:
        """按关键词挑角色并拼出「一段话风味描述 + 脚部图」消息链。"""
        char = self._pick(keyword)
        story = char["story"] or self._fill(random.choice(STORIES), char)
        taste = char["taste"] or random.choice(TASTE)

        text = f"{story}{taste}"

        chain = [Comp.Plain(text)]
        img = (self.image_dir / char["file"]).with_suffix(".jpg")
        if img.exists():
            try:
                chain.append(Comp.Image.fromFileSystem(str(img)))
            except Exception as exc:
                logger.warning(f"[FootPlugin] 图片加载失败 {char['file']}: {exc}")
        return chain

    def _split(self, event: AstrMessageEvent) -> tuple[str, str]:
        """返回 (角色关键词, 命中唤醒词)。未命中时唤醒词为空字符串。"""
        text = (getattr(event, "message_str", "") or "").strip()
        if text and text[0] in _WAKE_PREFIX_CHARS:
            text = text.lstrip(_WAKE_PREFIX_CHARS).strip()
        for kw in self.wake_keywords:
            if text.startswith(kw):
                return text[len(kw):].strip(), kw
        return "", ""

    def _keyword(self, event: AstrMessageEvent) -> str:
        """提取指令后的角色名关键词，用于按名点菜。"""
        char_kw, _ = self._split(event)
        return char_kw

    def _pick(self, keyword: str) -> dict:
        """随机选一名角色；带关键词时按名字模糊匹配。"""
        if keyword:
            matched = [c for c in self.characters if keyword in c["name"]]
            if matched:
                return random.choice(matched)
        if not self.characters:
            raise RuntimeError("图库为空")
        return random.choice(self.characters)

    @staticmethod
    def _is_priestess(keyword: str) -> bool:
        """判断点名的是否为普瑞塞斯（彩蛋）。"""
        if not keyword:
            return False
        text = keyword.replace(" ", "").replace("\u3000", "").lower()
        return any(name in text for name in _PRIESTESS_NAMES)

    # ── 彩蛋：普瑞塞斯 ───────────────────────────

    def _priestess_egg(self, event: AstrMessageEvent, keyword: str):
        """点名普瑞塞斯且本群不在冷却期内时，返回彩蛋文本；否则返回 None。"""
        if not self._is_priestess(keyword):
            return None

        scope = event.get_group_id() or f"private:{event.get_sender_id()}"
        state = self._load_priestess_state()
        now = time.time()
        try:
            last = float(state.get(scope) or 0)
        except (TypeError, ValueError):
            last = 0.0

        if now - last < PRIESTESS_EGG_COOLDOWN:
            remain_days = int((PRIESTESS_EGG_COOLDOWN - (now - last)) / 86400)
            logger.info(
                f"[FootPlugin] 普瑞塞斯彩蛋冷却中 scope={scope} 剩余约 {remain_days} 天"
            )
            return None

        state[scope] = now
        self._save_priestess_state(state)
        logger.info(f"[FootPlugin] 普瑞塞斯彩蛋触发 scope={scope}")
        return PRIESTESS_EGG_TEXT

    @staticmethod
    def _load_priestess_state() -> dict:
        if PRIESTESS_STATE_PATH.exists():
            try:
                data = json.loads(PRIESTESS_STATE_PATH.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    return data
            except Exception:
                pass
        return {}

    @staticmethod
    def _save_priestess_state(state: dict) -> None:
        try:
            _PRIESTESS_DATA_DIR.mkdir(parents=True, exist_ok=True)
            PRIESTESS_STATE_PATH.write_text(
                json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except Exception as exc:
            logger.warning(f"[FootPlugin] 彩蛋状态保存失败: {exc}")

    @staticmethod
    def _fill(template: str, char: dict) -> str:
        """用角色信息填充文案模板占位符。"""
        return template.format(name=char["name"], cls=char["cls"])
