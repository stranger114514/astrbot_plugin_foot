# -*- coding: utf-8 -*-
"""astrbot_plugin_foot 面板后端 API：图库与配套文案管理。

面板（pages/dashboard）通过这套 HTTP 接口读写图库：
- GET  /astrbot_plugin_foot/api/characters   列出全部角色（含图片名、文案、是否被屏蔽）
- GET  /astrbot_plugin_foot/api/image?name=  返回立绘裁剪图
- POST /astrbot_plugin_foot/api/update       更新某角色故事/口感
- POST /astrbot_plugin_foot/api/add          新增角色（可带 base64 图片）
- POST /astrbot_plugin_foot/api/remove       屏蔽/删除角色
- POST /astrbot_plugin_foot/api/restore      恢复被屏蔽的内置角色
"""

import base64
import binascii
import uuid
from pathlib import Path

from quart import jsonify, request

from astrbot.api import logger

from .data import (
    CHAR_ROWS,
    CUSTOM,
    load_gallery,
    save_gallery,
)
from .score import all_groups as score_all_groups

PLUGIN_NAME = "astrbot_plugin_foot"


class FootAPI:
    """面板后端 API 提供者。"""

    def __init__(self, plugin) -> None:
        self.plugin = plugin

    @property
    def _image_dir(self) -> Path:
        return self.plugin.image_dir

    # ── 注册 ──────────────────────────────────────────

    def register(self, context) -> None:
        routes = [
            ("/characters", "handle_characters", ["GET"]),
            ("/image-data", "handle_image_data", ["GET"]),
            ("/ranking", "handle_ranking", ["GET"]),
            ("/update", "handle_update", ["POST"]),
            ("/add", "handle_add", ["POST"]),
            ("/remove", "handle_remove", ["POST"]),
            ("/restore", "handle_restore", ["POST"]),
        ]
        for route, handler_name, methods in routes:
            context.register_web_api(
                f"/{PLUGIN_NAME}{route}",
                getattr(self, handler_name),
                methods,
                f"脚图库面板: {handler_name}",
            )

    # ── 工具 ──────────────────────────────────────────

    def _safe_name(self, name: str) -> bool:
        """图片文件名只允许纯文件名，拒绝路径穿越。"""
        return bool(name) and "/" not in name and "\\" not in name and ".." not in name

    def _char_list(self, gallery: dict) -> list[dict]:
        """合并内置 + 新增角色，返回面板展示列表。"""
        overrides = gallery.get("overrides") or {}
        added = gallery.get("added") or []
        removed = set(gallery.get("removed") or [])
        result = []
        for name, file, cls in CHAR_ROWS:
            custom = CUSTOM.get(name) or {}
            ov = overrides.get(name) or {}
            result.append({
                "name": name,
                "file": file,
                "cls": cls,
                "story": ov.get("story") if ov.get("story") is not None else (custom.get("story") or ""),
                "taste": ov.get("taste") if ov.get("taste") is not None else (custom.get("taste") or ""),
                "builtin": True,
                "removed": name in removed,
            })
        for item in added:
            result.append({
                "name": item.get("name") or "",
                "file": item.get("file") or "",
                "cls": item.get("cls") or "",
                "story": item.get("story") or "",
                "taste": item.get("taste") or "",
                "builtin": False,
                "removed": False,
            })
        return result

    # ── 接口 ──────────────────────────────────────────

    async def handle_characters(self):
        gallery = load_gallery()
        return jsonify({
            "success": True,
            "keywords": list(self.plugin.wake_keywords or []),
            "characters": self._char_list(gallery),
        })

    async def handle_image_data(self):
        name = request.args.get("name", "")
        if not self._safe_name(name):
            return jsonify({"success": False, "error": "非法文件名"}), 400
        # 内置文件名是 .png 但裁剪图实际存成 .jpg；统一按 .jpg 读取
        img = (self._image_dir / name).with_suffix(".jpg")
        if not img.is_file():
            return jsonify({"success": False, "error": "文件不存在"}), 404
        raw = img.read_bytes()
        return jsonify({
            "success": True,
            "url": "data:image/jpeg;base64," + base64.b64encode(raw).decode("ascii"),
        })

    async def handle_ranking(self):
        """返回全部群聊积分排行，供面板展示。"""
        scores = self.plugin.scores
        return jsonify({
            "success": True,
            "groups": score_all_groups(scores),
        })

    async def handle_update(self):
        payload = await request.get_json() or {}
        name = str(payload.get("name", "") or "").strip()
        story = str(payload.get("story", "") or "").strip()
        taste = str(payload.get("taste", "") or "").strip()
        if not name:
            return jsonify({"success": False, "error": "缺少角色名"}), 400

        gallery = load_gallery()
        overrides = gallery.setdefault("overrides", {})
        overrides[name] = {
            "story": story or None,
            "taste": taste or None,
        }
        save_gallery(gallery)
        self.plugin.reload()
        return jsonify({"success": True})

    async def handle_add(self):
        payload = await request.get_json() or {}
        name = str(payload.get("name", "") or "").strip()
        cls = str(payload.get("cls", "") or "").strip()
        story = str(payload.get("story", "") or "").strip()
        taste = str(payload.get("taste", "") or "").strip()
        img_data = str(payload.get("image", "") or "").strip()
        if not name:
            return jsonify({"success": False, "error": "缺少角色名"}), 400

        filename = ""
        if img_data:
            raw = self._decode_image(img_data)
            if raw is None:
                return jsonify({"success": False, "error": "图片数据无效"}), 400
            filename = f"{uuid.uuid4().hex[:12]}.jpg"
            try:
                (self._image_dir / filename).write_bytes(raw)
            except Exception as exc:
                logger.warning(f"[FootAPI] 图片写入失败: {exc}")
                return jsonify({"success": False, "error": "图片保存失败"}), 500

        gallery = load_gallery()
        added = gallery.setdefault("added", [])
        updated = False
        for item in added:
            if item.get("name") == name:
                item["cls"] = cls
                item["story"] = story or None
                item["taste"] = taste or None
                if filename:
                    item["file"] = filename
                updated = True
                break
        if not updated:
            added.append({
                "name": name,
                "file": filename,
                "cls": cls,
                "story": story or None,
                "taste": taste or None,
            })
        save_gallery(gallery)
        self.plugin.reload()
        return jsonify({"success": True, "file": filename})

    async def handle_remove(self):
        payload = await request.get_json() or {}
        name = str(payload.get("name", "") or "").strip()
        if not name:
            return jsonify({"success": False, "error": "缺少角色名"}), 400

        gallery = load_gallery()
        # 新增角色：直接从 added 删除
        added = [a for a in gallery.get("added", []) if a.get("name") != name]
        removed_new = set(gallery.get("removed") or [])
        if len(added) != len(gallery.get("added", [])):
            # 确实是 added 里的，删除即可
            gallery["added"] = added
        else:
            # 内置角色：加入 removed 屏蔽列表
            removed_new.add(name)
        gallery["removed"] = sorted(removed_new)
        save_gallery(gallery)
        self.plugin.reload()
        return jsonify({"success": True})

    async def handle_restore(self):
        payload = await request.get_json() or {}
        name = str(payload.get("name", "") or "").strip()
        if not name:
            return jsonify({"success": False, "error": "缺少角色名"}), 400
        gallery = load_gallery()
        removed = set(gallery.get("removed") or [])
        removed.discard(name)
        gallery["removed"] = sorted(removed)
        save_gallery(gallery)
        self.plugin.reload()
        return jsonify({"success": True})

    # ── 图片解码 ──────────────────────────────────────

    @staticmethod
    def _decode_image(data: str) -> bytes | None:
        b64 = data.split(",", 1)[1] if "," in data else data
        try:
            return base64.b64decode(b64.strip(), validate=True)
        except (binascii.Error, ValueError):
            return None
