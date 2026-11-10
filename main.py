# -*- coding: utf-8 -*-
"""看看脚 / 看看足 / 看看jio 触发，随机发送明日方舟角色的脚臭吧风格故事 + 立绘裁剪图。

两种触发方式：
1. ``@filter.command`` —— 标准指令：``/看看脚`` 或群聊 @ 机器人后发 ``看看脚``；
2. ``@filter.regex`` —— 裸关键词：群里直接发 ``看看脚``（不带斜杠、不 @ 机器人）也能触发。
"""

import random
from pathlib import Path

from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register
from astrbot.api import logger
import astrbot.api.message_components as Comp

from .data import CHAR_ROWS, STORIES, TASTE, build_characters

# 兜底剥离可能的唤醒/指令前缀（正常情况下 AstrBot 已剥离，这里兼容原始消息场景）
_WAKE_PREFIX_CHARS = "/#!！.。·"

# 裸关键词：以 看看脚/看看足/看看jio 开头（不带斜杠前缀，避免与命令形式重复触发）
_BARE_RE = r"^看看(?:脚|足|jio)"


@register(
    "astrbot_plugin_foot",
    "stranger114514",
    "识别到“看看脚/看看足/看看jio”关键词，随机推荐饭盒（bushi）",
    "2.0.0",
)
class FootPlugin(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.characters = build_characters()
        self.image_dir = Path(__file__).parent / "images"
        logger.info(f"[FootPlugin] 已加载 {len(self.characters)} 名角色")

    @filter.command("看看脚", alias={"看看足", "看看jio"})
    async def foot(self, event: AstrMessageEvent):
        yield event.chain_result(self._build_chain(self._keyword(event)))

    @filter.regex(_BARE_RE)
    async def foot_bare(self, event: AstrMessageEvent):
        # 命令形式（/看看脚 或 @机器人 看看脚）已由 foot 处理，这里只管裸关键词
        if getattr(event, "is_at_or_wake_command", False):
            return
        yield event.chain_result(self._build_chain(self._keyword(event)))

    def _build_chain(self, keyword: str) -> list:
        """按关键词挑角色并拼出「一段话风味描述 + 脚部图」消息链。"""
        char = self._pick(keyword)
        story = char["story"] or self._fill(random.choice(STORIES), char)
        taste = char["taste"] or random.choice(TASTE)

        # 故事后面接一句口感/气味描述
        text = f"{story}{taste}"

        chain = [Comp.Plain(text)]
        img = (self.image_dir / char["file"]).with_suffix(".jpg")
        if img.exists():
            try:
                chain.append(Comp.Image.fromFileSystem(str(img)))
            except Exception as exc:
                logger.warning(f"[FootPlugin] 图片加载失败 {char['file']}: {exc}")
        return chain

    def _keyword(self, event: AstrMessageEvent) -> str:
        """提取指令后的角色名关键词，用于按名点菜。"""
        text = getattr(event, "message_str", "") or ""
        text = text.strip()
        if text and text[0] in _WAKE_PREFIX_CHARS:
            text = text.lstrip(_WAKE_PREFIX_CHARS).strip()
        for cmd in ("看看jio", "看看足", "看看脚"):
            if text.startswith(cmd):
                return text[len(cmd):].strip()
        return ""

    def _pick(self, keyword: str) -> dict:
        """随机选一名角色；带关键词时按名字模糊匹配。"""
        if keyword:
            matched = [c for c in self.characters if keyword in c["name"]]
            if matched:
                return random.choice(matched)
        return random.choice(self.characters)

    @staticmethod
    def _fill(template: str, char: dict) -> str:
        """用角色信息填充文案模板占位符。"""
        return template.format(name=char["name"], cls=char["cls"])
