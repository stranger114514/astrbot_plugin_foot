# -*- coding: utf-8 -*-
"""astrbot_plugin_foot 积分系统：按「群聊 -> 用户」记录历史最高分。

数据结构（data/scores.json）：
{
    "_group_names": {"<group_id>": "<群名>"},
    "<group_id>": {
        "<user_id>": {"name": "<昵称>", "best": <历史最高分>}
    }
}
"""

import json
from pathlib import Path

from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

# 持久化数据目录（AstrBot 官方位置：data/plugin_data/astrbot_plugin_foot）
# 卸载插件时，若用户选择「保留数据」，此目录不会被删除，积分可保留到下次重装。
SCORE_DIR = Path(get_astrbot_plugin_data_path()) / "astrbot_plugin_foot"
SCORE_PATH = SCORE_DIR / "scores.json"

# 旧位置（插件目录内 data/scores.json），用于迁移历史数据
_LEGACY_SCORE_PATH = Path(__file__).parent / "data" / "scores.json"

_GROUP_NAMES_KEY = "_group_names"


def _migrate_legacy_scores() -> None:
    """把旧位置（插件目录内）的积分数据迁移到持久化目录（仅当新位置还不存在时）。"""
    if _LEGACY_SCORE_PATH.exists() and not SCORE_PATH.exists():
        try:
            SCORE_DIR.mkdir(parents=True, exist_ok=True)
            SCORE_PATH.write_text(
                _LEGACY_SCORE_PATH.read_text(encoding="utf-8"), encoding="utf-8"
            )
        except Exception:
            pass


def load_scores() -> dict:
    """读取积分数据；文件不存在或损坏时返回空结构。"""
    _migrate_legacy_scores()
    if SCORE_PATH.exists():
        try:
            data = json.loads(SCORE_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except Exception:
            pass
    return {}


def save_scores(scores: dict) -> None:
    """把积分数据写回 data/scores.json。"""
    SCORE_DIR.mkdir(parents=True, exist_ok=True)
    SCORE_PATH.write_text(
        json.dumps(scores, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def update_best(scores: dict, group_id: str, user_id: str, name: str, new_score: int) -> bool:
    """记录本次活动得分；若超过历史最高分则替换。

    返回是否刷新了历史最高分。
    """
    group = scores.setdefault(group_id, {})
    user = group.setdefault(user_id, {"name": name or str(user_id), "best": 0})
    user["name"] = name or user.get("name") or str(user_id)
    best = int(user.get("best") or 0)
    if new_score > best:
        user["best"] = new_score
        return True
    return False


def reset_user(scores: dict, group_id: str, user_id: str) -> bool:
    """清除某用户在本群的记录；返回是否确实存在并清除了记录。"""
    group = scores.get(group_id)
    if not group:
        return False
    existed = user_id in group
    group.pop(user_id, None)
    if not group:
        scores.pop(group_id, None)
    return existed


def set_group_name(scores: dict, group_id: str, name: str) -> None:
    """记录群名（用于面板展示）。"""
    if not group_id:
        return
    if name:
        names = scores.setdefault(_GROUP_NAMES_KEY, {})
        names[group_id] = name


def get_group_name(scores: dict, group_id: str) -> str:
    """返回群名；无记录时退回 group_id。"""
    names = scores.get(_GROUP_NAMES_KEY, {})
    return names.get(group_id) or str(group_id)


def ranking(scores: dict, group_id: str) -> list:
    """返回本群积分排行 [(昵称, 最高分)]，按最高分降序，排除 0 分用户。"""
    group = scores.get(group_id)
    if not isinstance(group, dict):
        return []
    rows = [
        (u.get("name") or "", int(u.get("best") or 0))
        for u in group.values()
        if isinstance(u, dict) and int(u.get("best") or 0) > 0
    ]
    rows.sort(key=lambda x: (-x[1], x[0]))
    return rows


def all_groups(scores: dict) -> list:
    """返回全部群聊排行，供面板展示。

    每项：
    {
        "group_id": str,
        "group_name": str,
        "ranking": [(昵称, 最高分), ...]  # 排除 0 分，按最高分降序
    }
    按群内最高分降序排列。
    """
    result = []
    for gid, users in scores.items():
        if gid == _GROUP_NAMES_KEY:
            continue
        if not isinstance(users, dict):
            continue
        rows = ranking(scores, gid)
        result.append({
            "group_id": str(gid),
            "group_name": get_group_name(scores, gid),
            "ranking": rows,
        })
    result.sort(key=lambda g: -(max((r[1] for r in g["ranking"]), default=0)))
    return result
