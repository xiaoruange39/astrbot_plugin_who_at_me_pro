from __future__ import annotations

import asyncio
import base64
import html
import json
import re
import tempfile
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
import astrbot.api.message_components as Comp
from astrbot.api.star import Context, Star


QUERY_PATTERN = re.compile(r"^(谁(艾特|@|at)(我|他|她|它)|哪个逼(艾特|@|at)我)(?:\s*(?:\[CQ:at,[^\]]+\]|@.+))?$", re.I)
CLEAR_PATTERN = re.compile(r"^(clear_at|清除(艾特|at)数据)$", re.I)
CLEAR_ALL_PATTERN = re.compile(r"^(clear_all|清除全部(艾特|at)数据)$", re.I)
CONTEXT_ON_PATTERN = re.compile(r"^(开启|打开)(艾特|at)上下文$", re.I)
CONTEXT_OFF_PATTERN = re.compile(r"^关闭(艾特|at)上下文$", re.I)
REMINDER_GROUP_ON_PATTERN = re.compile(r"^(开启|启用)(本群|群)(艾特|at)提醒$", re.I)
REMINDER_GROUP_OFF_PATTERN = re.compile(r"^关闭(本群|群)(艾特|at)提醒$", re.I)
REMINDER_PERSONAL_ON_PATTERN = re.compile(r"^(开启我的(艾特|at)提醒|开启(艾特|at)提醒)$", re.I)
REMINDER_PERSONAL_OFF_PATTERN = re.compile(r"^(关闭我的(艾特|at)提醒|关闭(艾特|at)提醒)$", re.I)
REMINDER_STATUS_PATTERN = re.compile(r"^(我的)?(艾特|at)提醒状态$", re.I)
REMINDER_CONTEXT_ON_PATTERN = re.compile(r"^开启提醒上下文$", re.I)
REMINDER_CONTEXT_OFF_PATTERN = re.compile(r"^关闭提醒上下文$", re.I)
REMINDER_CONTEXT_SET_PATTERN = re.compile(r"^设置提醒上下文\s*(\d+)\s*[,，]\s*(\d+)$", re.I)

ALL_TARGET = "__all__"
INDEX_KEY = "records:index"
CONTEXT_INDEX_KEY = "context:index"
MAX_RECORDS_PER_TARGET = 300
MAX_CONTEXT_MESSAGES = 5
MAX_MESSAGES_PER_IMAGE = 12
RENDER_IMAGE_QUALITY = 92
RENDER_TIMEOUT_MS = 20000
RENDER_TASK_TIMEOUT_SEC = 25
REMINDER_AWAY_SECONDS = 10 * 60
MAX_PENDING_REMINDERS = 50
MAX_REMINDER_CONTEXT = 5
HEADER_IMAGE_URL = "https://pic1.imgdb.cn/item/69e60edc1d6508f56becb8fa.png"
FOOTER_IMAGE_URL = "https://pic1.imgdb.cn/item/69e5f9e51d6508f56bec8ea5.png"
REFERENCE_SEGMENT_TYPES = {"reply", "quote", "source", "reference"}
PAGE_SETTINGS_DEFAULTS = {
    "time_x": 30,
    "time_y": 7,
    "group_x": 56,
    "group_y": 45,
    "font_path": "",
}


HTML_TEMPLATE = r"""
<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <style>
    {{ custom_font_css | safe }}
    html, body {
      margin: 0;
      padding: 0;
      background: #333;
      font-family: var(--who-at-me-font-family, -apple-system, BlinkMacSystemFont, "Segoe UI", "Microsoft YaHei", sans-serif);
    }
    .app {
      width: 600px;
      min-height: 200px;
      background: #f2f3f5;
      display: flex;
      flex-direction: column;
      color: #000;
    }
    .header-wrapper {
      min-height: 64px;
      background: #fdfdfd;
      position: relative;
      flex: 0 0 auto;
    }
    .header-wrapper img {
      display: block;
      width: 100%;
      min-height: 64px;
      object-fit: cover;
    }
    .status-time {
      position: absolute;
      top: {{ layout.time_y }}px;
      left: {{ layout.time_x }}px;
      font-size: 16px;
      font-weight: 700;
      color: #111;
    }
    .header-text {
      position: absolute;
      left: {{ layout.group_x }}px;
      right: 56px;
      top: {{ layout.group_y }}px;
      display: flex;
      align-items: center;
      min-width: 0;
      font-size: 22px;
      line-height: 1.2;
      font-weight: 800;
      color: #000;
      overflow: hidden;
      white-space: nowrap;
      text-overflow: ellipsis;
    }
    .chat-area {
      padding: 18px 16px 22px 16px;
      flex: 1;
    }
    .msg-item {
      display: flex;
      flex-direction: column;
      margin-bottom: 24px;
    }
    .msg-body {
      display: flex;
      gap: 12px;
      align-items: flex-start;
    }
    .avatar, .avatar-fallback {
      width: 50px;
      height: 50px;
      flex: 0 0 50px;
      border-radius: 50%;
      border: 1px solid #e5e7eb;
      object-fit: cover;
      background: linear-gradient(135deg, #1f8fff, #28c2d1);
    }
    .avatar-fallback {
      color: #fff;
      display: flex;
      align-items: center;
      justify-content: center;
      font-size: 22px;
      font-weight: 800;
    }
    .msg-content {
      flex: 1;
      min-width: 0;
    }
    .msg-info {
      display: flex;
      align-items: center;
      gap: 8px;
      min-width: 0;
      margin: 0 0 6px 2px;
      color: #888;
      font-size: 13px;
    }
    .tag-pill {
      flex: 0 0 auto;
      display: inline-flex;
      align-items: center;
      border-radius: 4px;
      padding: 3px 6px;
      color: #fff;
      font-size: 11px;
      line-height: 1;
      font-weight: 800;
      letter-spacing: 0;
    }
    .nickname {
      min-width: 0;
      color: #666;
      font-size: 14px;
      font-weight: 700;
      overflow: hidden;
      white-space: nowrap;
      text-overflow: ellipsis;
    }
    .msg-bubble {
      display: inline-block;
      max-width: 85%;
      padding: 12px 16px;
      border-radius: 4px 16px 16px 16px;
      background: #fff;
      border: 1px solid transparent;
      box-shadow: 0 1px 3px rgba(0,0,0,0.06);
    }
    .msg-bubble.is-at {
      background: #eef5ff;
      border-color: #cce0ff;
    }
    .msg-text {
      color: #000;
      font-size: 18px;
      line-height: 1.4;
      font-weight: 800;
      overflow-wrap: anywhere;
    }
    .at-text {
      color: #1e6fff;
      margin-right: 6px;
      font-style: normal;
    }
    .msg-img {
      display: block;
      max-width: 100%;
      max-height: 360px;
      margin-top: 8px;
      border-radius: 8px;
      border: 1px solid #eee;
      object-fit: contain;
    }
    .quote-card {
      display: flex;
      gap: 8px;
      margin-bottom: 9px;
      padding: 8px 10px;
      background: rgba(255,255,255,0.72);
      border-radius: 8px;
      border: 1px solid rgba(0,0,0,0.06);
      max-width: 100%;
    }
    .quote-card::before {
      content: "";
      flex: 0 0 3px;
      align-self: stretch;
      border-radius: 3px;
      background: #b8bcc3;
    }
    .quote-body {
      min-width: 0;
      flex: 1;
    }
    .quote-head {
      display: flex;
      align-items: center;
      gap: 8px;
      margin-bottom: 5px;
      color: #555;
      font-size: 13px;
      line-height: 1.25;
      font-weight: 700;
    }
    .quote-name {
      max-width: 160px;
      overflow: hidden;
      white-space: nowrap;
      text-overflow: ellipsis;
    }
    .member-title {
      flex: 0 0 auto;
      display: inline-flex;
      align-items: center;
      border-radius: 4px;
      padding: 2px 5px;
      background: #e1e1e1;
      color: #777;
      font-size: 11px;
      line-height: 1;
      font-weight: 700;
    }
    .quote-time {
      color: #999;
      font-weight: 600;
    }
    .quote-text {
      color: #333;
      font-size: 14px;
      line-height: 1.35;
      font-weight: 700;
      overflow-wrap: anywhere;
    }
    .quote-images {
      display: flex;
      flex-wrap: wrap;
      gap: 6px;
      margin-top: 6px;
    }
    .quote-img {
      display: block;
      max-width: 112px;
      max-height: 112px;
      border-radius: 6px;
      border: 1px solid #e5e7eb;
      object-fit: cover;
    }
    .msg-time-bottom {
      margin: 6px 0 0 4px;
      color: #b0b0b0;
      font-size: 12px;
      font-weight: 700;
    }
    .block-divider {
      display: flex;
      align-items: center;
      color: #0f9fff;
      font-size: 12px;
      margin: 14px 0 25px 0;
      font-weight: 500;
    }
    .block-divider::before, .block-divider::after {
      content: "";
      flex: 1;
      border-bottom: 1px solid #0f9fff;
    }
    .block-divider::before { margin-right: 15px; }
    .block-divider::after { margin-left: 15px; }
    .footer-wrapper {
      margin-top: auto;
      padding: 0;
      color: #999;
      text-align: center;
      font-size: 14px;
      min-height: 44px;
    }
    .footer-wrapper img {
      display: block;
      width: 100%;
      min-height: 44px;
      object-fit: cover;
    }
    .page-indicator {
      padding-bottom: 10px;
      color: #999;
    }
  </style>
</head>
<body>
  <div class="app">
    <div class="header-wrapper">
      <img src="{{ header_image }}" />
      <div class="status-time">{{ now }}</div>
      <div class="header-text">{{ group_name }}{% if member_count %}({{ member_count }}){% endif %} 👂</div>
    </div>
    <div class="chat-area">
      {% for block in blocks %}
        <div class="msg-block">
          {% for msg in block.msgs %}
            <div class="msg-item">
              <div class="msg-body">
                {% if msg.avatar %}
                  <img src="{{ msg.avatar }}" class="avatar" />
                {% else %}
                  <div class="avatar-fallback">{{ msg.initial }}</div>
                {% endif %}
                <div class="msg-content">
                  <div class="msg-info">
                    {% if msg.tag_text %}<span class="tag-pill" style="background: {{ msg.tag_color }}">{{ msg.tag_text }}</span>{% endif %}
                    <span class="nickname">{{ msg.nickname }}</span>
                  </div>
                  <div class="msg-bubble {% if msg.is_at %}is-at{% endif %}">
                    {% if msg.quote %}
                      <div class="quote-card">
                        <div class="quote-body">
                          <div class="quote-head">
                            <span class="quote-name">{{ msg.quote.nickname }}</span>
                            {% if msg.quote.time_text %}<span class="quote-time">{{ msg.quote.time_text }}</span>{% endif %}
                          </div>
                          {% if msg.quote.message_html %}
                            <div class="quote-text">{{ msg.quote.message_html | safe }}</div>
                          {% endif %}
                          {% if msg.quote.images %}
                            <div class="quote-images">
                              {% for image in msg.quote.images %}
                                <img src="{{ image }}" class="quote-img" onerror="this.remove()" />
                              {% endfor %}
                            </div>
                          {% endif %}
                        </div>
                      </div>
                    {% endif %}
                    {% if msg.is_at or msg.has_message %}
                      <div class="msg-text">
                        {% if msg.is_at %}<span class="at-text">@{{ target_name }}</span>{% endif %}
                        {% if msg.has_message %}{{ msg.message_html | safe }}{% endif %}
                      </div>
                    {% endif %}
                    {% for image in msg.images %}
                      <img src="{{ image }}" class="msg-img" onerror="this.remove()" />
                    {% endfor %}
                  </div>
                  <div class="msg-time-bottom">{{ msg.time_text }}</div>
                </div>
              </div>
            </div>
          {% endfor %}
        </div>
        {% if not loop.last %}<div class="block-divider">新消息</div>{% endif %}
      {% endfor %}
    </div>
    <div class="footer-wrapper">
      {% if page_label %}<div class="page-indicator">- {{ page_label }} -</div>{% endif %}
      <img src="{{ footer_image }}" />
    </div>
  </div>
</body>
</html>
"""


class WhoAtMePlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        self.config = config or {}
        self._jsonify = None
        self._register_page_apis(context)
        self.before_cache: dict[str, list[dict[str, Any]]] = {}
        self.after_tasks: dict[str, list[dict[str, Any]]] = {}
        self.reminder_after_tasks: dict[str, list[dict[str, Any]]] = {}
        self.bot_name_cache: dict[str, str] = {}
        self.page_settings = self._load_page_settings()
        self._font_css_cache_key: tuple[str, int, int] | None = None
        self._font_css_cache_value = ""
        self.started_at = int(time.time())

    def _register_page_apis(self, context: Context) -> None:
        try:
            from quart import jsonify

            self._jsonify = jsonify
            for prefix in ("astrbot_plugin_who_at_me_pro", "astrbot_plugin_who_at_me"):
                context.register_web_api(
                    f"/{prefix}/layout",
                    self.page_layout,
                    ["GET", "POST"],
                    "谁艾特我Pro渲染布局设置",
                )
                context.register_web_api(
                    f"/{prefix}/fonts",
                    self.page_fonts,
                    ["GET"],
                    "谁艾特我Pro字体列表",
                )
                context.register_web_api(
                    f"/{prefix}/fonts/upload",
                    self.page_font_upload,
                    ["POST"],
                    "谁艾特我Pro上传字体",
                )
                context.register_web_api(
                    f"/{prefix}/fonts/select",
                    self.page_font_select,
                    ["POST"],
                    "谁艾特我Pro选择字体",
                )
                context.register_web_api(
                    f"/{prefix}/fonts/delete",
                    self.page_font_delete,
                    ["POST"],
                    "谁艾特我Pro删除字体",
                )
        except Exception as exc:
            logger.warning(f"[谁艾特我] 注册 Page API 失败: {exc}")

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE, priority=10000)
    async def mark_group_activity_early(self, event: AstrMessageEvent):
        await self._mark_group_activity(event)

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE, priority=1000)
    async def on_group_message(self, event: AstrMessageEvent):
        """记录群聊 @，并兼容原插件的自然语言命令。"""
        group_id = self._group_id(event)
        if not group_id:
            return

        text = self._normalize_command_text(self._message_text(event))
        is_plugin_command = self._is_plugin_command(text)
        if not self._global_group_allowed(event):
            if is_plugin_command:
                self._stop_event(event)
                self._disable_llm(event)
            return

        sender_id = self._sender_id(event)
        self_id = self._self_id(event)
        if sender_id and self_id and sender_id == self_id:
            await self.delete_kv_data(self._reminder_pending_key(group_id, self_id))
            return

        mentions = self._mentions(event)
        if is_plugin_command:
            self._stop_event(event)
            self._disable_llm(event)
            if sender_id:
                await self.delete_kv_data(self._reminder_pending_key(group_id, sender_id))
                await self._update_last_active(group_id, sender_id, self._timestamp(event))
            command_result = await self._handle_command(event, group_id, text, mentions)
            for result in command_result or []:
                yield result
            return

        if sender_id:
            await self._deliver_pending_reminders(event, group_id, sender_id)
        await self._record_mentions(event, group_id, mentions)
        if sender_id:
            await self._update_last_active(group_id, sender_id, self._timestamp(event))

    async def _mark_group_activity(self, event: AstrMessageEvent) -> None:
        group_id = self._group_id(event)
        if not group_id or not self._global_group_allowed(event):
            return

        sender_id = self._sender_id(event)
        if not sender_id:
            return

        self_id = self._self_id(event)
        if self_id and sender_id == self_id:
            await self.delete_kv_data(self._reminder_pending_key(group_id, self_id))
            return

        text = self._normalize_command_text(self._message_text(event))
        if self._is_plugin_command(text):
            await self.delete_kv_data(self._reminder_pending_key(group_id, sender_id))
            await self._update_last_active(group_id, sender_id, self._timestamp(event))
            return

        await self._deliver_pending_reminders(event, group_id, sender_id)
        await self._update_last_active(group_id, sender_id, self._timestamp(event))

    async def page_layout(self):
        try:
            from quart import request

            if request.method == "POST":
                if not self._is_same_origin_request(request):
                    return self._json_response({"status": "error", "message": "请求来源无效"})
                payload = await request.get_json(silent=True)
                if not isinstance(payload, dict):
                    payload = {}
                layout = payload.get("layout") if isinstance(payload.get("layout"), dict) else payload
                self.page_settings.update(self._sanitize_layout_settings(layout))
                self._save_page_settings()
                return self._json_response({"status": "ok", "message": "布局已保存", "data": self._page_data()})

            return self._json_response({"status": "ok", "data": self._page_data()})
        except Exception as exc:
            logger.error(f"[谁艾特我] Page布局接口失败: {exc}", exc_info=True)
            return self._json_response({"status": "error", "message": "布局接口失败"})

    async def page_fonts(self):
        try:
            return self._json_response({"status": "ok", "data": self._font_data()})
        except Exception as exc:
            logger.error(f"[谁艾特我] Page字体列表失败: {exc}", exc_info=True)
            return self._json_response({"status": "error", "message": "获取字体列表失败"})

    async def page_font_upload(self):
        try:
            from quart import request

            if not self._is_same_origin_request(request):
                return self._json_response({"status": "error", "message": "请求来源无效"})

            filename = ""
            data = None
            files = await request.files
            file = files.get("font") if files else None
            if file and getattr(file, "filename", ""):
                filename = str(file.filename)
                data = file.read()
                if hasattr(data, "__await__"):
                    data = await data
            else:
                payload = await request.get_json(silent=True)
                if isinstance(payload, dict):
                    filename = str(payload.get("filename") or "")
                    content = str(payload.get("content") or "")
                    if "," in content:
                        content = content.split(",", 1)[1]
                    try:
                        data = base64.b64decode(content, validate=True)
                    except Exception:
                        return self._json_response({"status": "error", "message": "字体文件内容无效"})

            if not filename:
                return self._json_response({"status": "error", "message": "请选择字体文件"})
            if not self._is_allowed_font_file(filename):
                return self._json_response({"status": "error", "message": "仅支持 .ttf/.otf/.woff/.woff2/.ttc 字体文件"})
            if not data:
                return self._json_response({"status": "error", "message": "字体文件为空"})
            if len(data) > 50 * 1024 * 1024:
                return self._json_response({"status": "error", "message": "字体文件不能超过50MB"})

            fonts_dir = self._fonts_dir()
            fonts_dir.mkdir(parents=True, exist_ok=True)
            safe_name = self._sanitize_font_filename(filename)
            target = fonts_dir / safe_name
            if target.exists():
                target = fonts_dir / f"{target.stem}_{uuid.uuid4().hex[:8]}{target.suffix}"
            target.write_bytes(data)

            self._save_font_path(self._font_config_path(target.name))
            logger.info(f"[谁艾特我] Page上传并启用自定义字体: {target.name}")
            return self._json_response({"status": "ok", "message": "字体已上传并启用", "data": self._font_data()})
        except Exception as exc:
            logger.error(f"[谁艾特我] Page上传字体失败: {exc}", exc_info=True)
            return self._json_response({"status": "error", "message": "上传字体失败"})

    async def page_font_select(self):
        try:
            from quart import request

            if not self._is_same_origin_request(request):
                return self._json_response({"status": "error", "message": "请求来源无效"})

            payload = await request.get_json(silent=True)
            font_path = str((payload.get("font_path") or "") if isinstance(payload, dict) else "").strip()
            if font_path:
                font_name = self._sanitize_font_filename(self._selected_font_name(font_path))
                if not font_name or not self._is_allowed_font_file(font_name):
                    return self._json_response({"status": "error", "message": "字体文件类型不支持"})
                target = self._fonts_dir() / font_name
                if not target.exists() or not target.is_file():
                    return self._json_response({"status": "error", "message": "字体文件不存在"})
                font_path = self._font_config_path(font_name)

            self._save_font_path(font_path)
            logger.info(f"[谁艾特我] Page切换自定义字体: {font_path or '默认字体'}")
            return self._json_response({"status": "ok", "message": "字体设置已保存", "data": self._font_data()})
        except Exception as exc:
            logger.error(f"[谁艾特我] Page选择字体失败: {exc}", exc_info=True)
            return self._json_response({"status": "error", "message": "保存字体设置失败"})

    async def page_font_delete(self):
        try:
            from quart import request

            if not self._is_same_origin_request(request):
                return self._json_response({"status": "error", "message": "请求来源无效"})

            payload = await request.get_json(silent=True)
            font_path = str((payload.get("font_path") or "") if isinstance(payload, dict) else "").strip()
            font_name = self._sanitize_font_filename(self._selected_font_name(font_path))
            if not font_name or not self._is_allowed_font_file(font_name):
                return self._json_response({"status": "error", "message": "字体文件参数无效"})
            target = self._fonts_dir() / font_name
            if not target.exists() or not target.is_file():
                return self._json_response({"status": "error", "message": "字体文件不存在"})

            target.unlink()
            if self._selected_font_name(self.page_settings.get("font_path", "")) == font_name:
                self._save_font_path("")
            logger.info(f"[谁艾特我] Page删除自定义字体: {font_name}")
            return self._json_response({"status": "ok", "message": "字体已删除", "data": self._font_data()})
        except Exception as exc:
            logger.error(f"[谁艾特我] Page删除字体失败: {exc}", exc_info=True)
            return self._json_response({"status": "error", "message": "删除字体失败"})

    async def _handle_command(
        self,
        event: AstrMessageEvent,
        group_id: str,
        text: str,
        mentions: list[str],
    ) -> list[Any] | None:
        stripped = text.strip()
        if QUERY_PATTERN.match(stripped):
            return await self._query(event, group_id, stripped, mentions)

        if CLEAR_PATTERN.match(stripped):
            return [await self._clear_self(event, group_id)]

        if CLEAR_ALL_PATTERN.match(stripped):
            if not self._is_admin(event):
                return [event.plain_result("只有管理员可以清除全部艾特数据")]
            return [await self._clear_all(event)]

        if CONTEXT_ON_PATTERN.match(stripped):
            if not self._is_admin(event):
                return [event.plain_result("只有群管或主人可以操作哦")]
            await self._set_context(group_id, True)
            return [event.plain_result("已开启本群艾特上下文记录，将记录艾特前后各5条消息。")]

        if CONTEXT_OFF_PATTERN.match(stripped):
            if not self._is_admin(event):
                return [event.plain_result("只有群管或主人可以操作哦")]
            await self._set_context(group_id, False)
            self.before_cache.pop(group_id, None)
            self.after_tasks.pop(group_id, None)
            return [event.plain_result("已关闭本群艾特上下文记录。")]

        if REMINDER_GROUP_ON_PATTERN.match(stripped):
            if not self._is_admin(event):
                return [event.plain_result("只有群管或主人可以操作哦")]
            await self._set_reminder_group_enabled(group_id, True)
            return [event.plain_result("已开启本群艾特被动提醒。")]

        if REMINDER_GROUP_OFF_PATTERN.match(stripped):
            if not self._is_admin(event):
                return [event.plain_result("只有群管或主人可以操作哦")]
            await self._set_reminder_group_enabled(group_id, False)
            return [event.plain_result("已关闭本群艾特被动提醒。")]

        if REMINDER_PERSONAL_ON_PATTERN.match(stripped):
            await self._set_reminder_user_enabled(group_id, self._sender_id(event), True)
            return [event.plain_result("已开启你的艾特提醒。")]

        if REMINDER_PERSONAL_OFF_PATTERN.match(stripped):
            await self._set_reminder_user_enabled(group_id, self._sender_id(event), False)
            return [event.plain_result("已关闭你的艾特提醒。")]

        if REMINDER_CONTEXT_ON_PATTERN.match(stripped):
            if not self._is_admin(event):
                return [event.plain_result("只有群管或主人可以操作哦")]
            await self._set_reminder_context(group_id, True)
            return [event.plain_result("已开启本群提醒上下文，提醒截图会带上艾特前后消息。")]

        if REMINDER_CONTEXT_OFF_PATTERN.match(stripped):
            if not self._is_admin(event):
                return [event.plain_result("只有群管或主人可以操作哦")]
            await self._set_reminder_context(group_id, False)
            return [event.plain_result("已关闭本群提醒上下文。")]

        context_match = REMINDER_CONTEXT_SET_PATTERN.match(stripped)
        if context_match:
            if not self._is_admin(event):
                return [event.plain_result("只有群管或主人可以操作哦")]
            before = min(int(context_match.group(1)), self._max_reminder_context())
            after = min(int(context_match.group(2)), self._max_reminder_context())
            await self._set_reminder_context(group_id, True, before, after)
            return [event.plain_result(f"已设置提醒上下文：前 {before} 条，后 {after} 条。")]

        if REMINDER_STATUS_PATTERN.match(stripped):
            return [event.plain_result(await self._reminder_status_text(event, group_id))]

        return None

    def _is_plugin_command(self, text: str) -> bool:
        stripped = text.strip()
        return any(
            pattern.match(stripped)
            for pattern in (
                QUERY_PATTERN,
                CLEAR_PATTERN,
                CLEAR_ALL_PATTERN,
                CONTEXT_ON_PATTERN,
                CONTEXT_OFF_PATTERN,
                REMINDER_GROUP_ON_PATTERN,
                REMINDER_GROUP_OFF_PATTERN,
                REMINDER_PERSONAL_ON_PATTERN,
                REMINDER_PERSONAL_OFF_PATTERN,
                REMINDER_STATUS_PATTERN,
                REMINDER_CONTEXT_ON_PATTERN,
                REMINDER_CONTEXT_OFF_PATTERN,
                REMINDER_CONTEXT_SET_PATTERN,
            )
        )

    async def _record_mentions(
        self,
        event: AstrMessageEvent,
        group_id: str,
        mentions: list[str],
    ) -> None:
        context_on = await self._context_enabled(group_id)
        reminder_context = await self._reminder_context_config(group_id)
        reminder_context_on = bool(reminder_context.get("enabled"))
        sender_info = (
            await self._member_info(event, group_id, self._sender_id(event))
            if mentions or context_on or reminder_context_on
            else {}
        )
        quote = await self._quote(event) if mentions or context_on or reminder_context_on else None
        current = self._context_message(event, sender_info, quote) if context_on or reminder_context_on else None

        if context_on and current:
            await self._append_after_context(group_id, current)
        if reminder_context_on and current:
            await self._append_reminder_after_context(group_id, current)

        self_id = self._self_id(event)
        targets = [target for target in mentions if target not in {self._sender_id(event), self_id}]
        if targets:
            before = list(self.before_cache.get(group_id, [])) if context_on else []
            reminder_before_count = int(reminder_context.get("before", 1))
            reminder_before = (
                list(self.before_cache.get(group_id, []))[-reminder_before_count:]
                if reminder_context_on and reminder_before_count > 0
                else []
            )
            record = self._mention_record(event, targets, sender_info, quote)
            if context_on:
                record["is_context"] = True
                record["before"] = before
                record["after"] = []

            for target in targets:
                await self._append_record(group_id, target, record)
                queued = await self._queue_reminder_if_needed(
                    event,
                    group_id,
                    target,
                    record,
                    reminder_context,
                    reminder_before,
                )
                if queued and reminder_context_on and int(reminder_context.get("after", 0)) > 0:
                    tasks = self.reminder_after_tasks.setdefault(group_id, [])
                    tasks.append(
                        {
                            "target": target,
                            "time": record["time"],
                            "count": 0,
                            "limit": int(reminder_context.get("after", 0)),
                        }
                    )
                if context_on:
                    tasks = self.after_tasks.setdefault(group_id, [])
                    tasks.append({"target": target, "time": record["time"], "count": 0})

        if (context_on or reminder_context_on) and current:
            cache = self.before_cache.setdefault(group_id, [])
            cache.append(current)
            del cache[:-max(self._query_context_max_messages(), self._max_reminder_context())]

    async def _append_after_context(self, group_id: str, current: dict[str, Any]) -> None:
        tasks = self.after_tasks.get(group_id, [])
        for idx in range(len(tasks) - 1, -1, -1):
            task = tasks[idx]
            records = await self._get_records(group_id, task["target"])
            changed = False
            for record in records:
                if record.get("time") == task["time"]:
                    record.setdefault("after", []).append(current)
                    changed = True
                    break
            if changed:
                await self.put_kv_data(self._record_key(group_id, task["target"]), records)

            task["count"] += 1
            if task["count"] >= self._query_context_max_messages():
                tasks.pop(idx)

    async def _append_reminder_after_context(self, group_id: str, current: dict[str, Any]) -> None:
        tasks = self.reminder_after_tasks.get(group_id, [])
        for idx in range(len(tasks) - 1, -1, -1):
            task = tasks[idx]
            pending = await self._get_pending_reminders(group_id, task["target"])
            changed = False
            for record in pending:
                if record.get("time") == task["time"]:
                    record.setdefault("after", []).append(current)
                    changed = True
                    break
            if changed:
                await self.put_kv_data(self._reminder_pending_key(group_id, task["target"]), pending)

            task["count"] += 1
            if task["count"] >= int(task.get("limit", 0)):
                tasks.pop(idx)

    async def _queue_reminder_if_needed(
        self,
        event: AstrMessageEvent,
        group_id: str,
        target: str,
        record: dict[str, Any],
        context_config: dict[str, Any],
        before: list[dict[str, Any]],
    ) -> bool:
        if target == ALL_TARGET:
            return False
        if target == self._self_id(event):
            return False
        if not await self._reminder_group_enabled(event, group_id):
            return False
        if not await self._reminder_user_enabled(group_id, target):
            return False

        record_time = int(record.get("time", time.time()))
        last_active = await self.get_kv_data(self._reminder_last_active_key(group_id, target), None)
        try:
            last_active_time = int(last_active)
        except (TypeError, ValueError):
            last_active_time = self.started_at

        if record_time - min(last_active_time, record_time) < self._reminder_away_seconds():
            return False

        pending_record = dict(record)
        pending_record["target"] = target
        if context_config.get("enabled"):
            pending_record["is_context"] = True
            pending_record["before"] = list(before)
            pending_record["after"] = []

        key = self._reminder_pending_key(group_id, target)
        pending = await self._get_pending_reminders(group_id, target)
        if any(self._records_are_duplicate(item, pending_record) for item in pending):
            return False

        pending.append(pending_record)
        pending = pending[-self._max_pending_reminders():]
        await self.put_kv_data(key, pending)
        return True

    async def _deliver_pending_reminders(self, event: AstrMessageEvent, group_id: str, user_id: str) -> None:
        if not await self._reminder_group_enabled(event, group_id):
            return

        pending = await self._get_pending_reminders(group_id, user_id)
        if not pending:
            return

        now_time = self._timestamp(event)
        away_seconds = self._reminder_away_seconds()
        pending = self._dedupe_records(pending)
        ready = []
        for record in pending:
            if away_seconds <= 0 or now_time - self._record_time(record) >= away_seconds:
                ready.append(record)

        await self.delete_kv_data(self._reminder_pending_key(group_id, user_id))
        pending = ready
        if not pending:
            return

        pending.sort(key=lambda item: item.get("time", 0))
        target_name = await self._target_name(event, group_id, user_id)
        reminder_text = self._format_template(
            self._config_str(
                "message",
                "reminder_text_template",
                default="{target_name}，你不在的时候有 {count} 条艾特记录~",
            ),
            target_name=target_name,
            count=len(pending),
        )
        blocks = self._build_blocks(pending, target_name, reverse=False)
        chunks = self._chunk_blocks(blocks)
        chunks = self._limit_chunks(chunks, self._max_reminder_pages())
        image_paths: list[str] = []
        try:
            for idx, chunk in enumerate(chunks, start=1):
                image_path = await self._render_query_image(
                    {
                        "blocks": chunk,
                        "group_name": await self._group_name(event, group_id),
                        "member_count": await self._member_count(event, group_id),
                        "target_name": target_name,
                        "total_records": len(pending),
                        "context_enabled": any(item.get("is_context") for item in pending),
                        "now": datetime.now().strftime("%H:%M"),
                        "page_label": "",
                        "header_image": self._header_image_url(),
                        "footer_image": self._footer_image_url(),
                    }
                )
                image_paths.append(image_path)

            if not await self._try_send_text_images(event, reminder_text, image_paths):
                await self._try_send(event, event.plain_result(reminder_text))
                for image_path in image_paths:
                    if not await self._try_send(event, event.image_result(image_path)):
                        raise RuntimeError(f"发送提醒图片失败: {image_path}")
        except Exception as exc:
            logger.error(f"[谁艾特我] 渲染或发送提醒失败: {exc}")
            if not image_paths:
                await self._try_send(event, event.plain_result(reminder_text))
            await self._try_send(event, event.plain_result(self._plain_summary(pending, target_name)))

    async def _query(
        self,
        event: AstrMessageEvent,
        group_id: str,
        text: str,
        mentions: list[str],
    ) -> list[Any]:
        target = self._query_target(event, text, mentions)
        if not target:
            return [event.plain_result("请在命令里 @ 要查询的人")]

        records = await self._get_records(group_id, target)
        all_records = await self._get_records(group_id, ALL_TARGET)
        records = self._dedupe_records(records + all_records)

        if not records:
            return [event.plain_result("目前还没有人艾特")]

        query_reverse = self._query_reverse_order()
        records.sort(key=lambda item: item.get("time", 0), reverse=query_reverse)
        target_name = await self._target_name(event, group_id, target)
        blocks = self._build_blocks(records, target_name, reverse=query_reverse)
        chunks = self._chunk_blocks(blocks)
        chunks = self._limit_chunks(chunks, self._max_query_pages())
        if not chunks:
            return [event.plain_result(self._plain_summary(records, target_name))]

        is_self_query = target == self._sender_id(event)
        target_pronoun = "你" if is_self_query else "ta"
        waiting_template = self._config_str("message", "waiting_text_template", default="让{bot_name}看看谁艾特过你哦，稍等一下~")
        if not is_self_query and waiting_template == "让{bot_name}看看谁艾特过你哦，稍等一下~":
            waiting_template = "让{bot_name}看看谁艾特过ta哦，稍等一下~"
        waiting_text = self._format_template(
            waiting_template,
            bot_name=await self._bot_name(event, group_id),
            target_name=target_name,
            target_pronoun=target_pronoun,
        )
        if not await self._try_send(event, event.plain_result(waiting_text)):
            return [event.plain_result(waiting_text)]

        image_paths: list[str] = []
        try:
            for idx, chunk in enumerate(chunks, start=1):
                image_path = await self._render_query_image(
                    {
                        "blocks": chunk,
                        "group_name": await self._group_name(event, group_id),
                        "member_count": await self._member_count(event, group_id),
                        "target_name": target_name,
                        "total_records": len(records),
                        "context_enabled": any(item.get("is_context") for item in records),
                        "now": datetime.now().strftime("%H:%M"),
                        "page_label": f"第 {idx} / {len(chunks)} 页" if len(chunks) > 1 else "",
                        "header_image": self._header_image_url(),
                        "footer_image": self._footer_image_url(),
                    }
                )
                image_paths.append(image_path)

            if not await self._try_send_images(event, image_paths):
                for image_path in image_paths:
                    if not await self._try_send(event, event.image_result(image_path)):
                        raise RuntimeError(f"发送图片失败: {image_path}")
        except Exception as exc:
            logger.error(f"[谁艾特我] 渲染或发送图片失败: {exc}")
            await self._try_send(event, event.plain_result(self._plain_summary(records, target_name)))

        return []

    async def _clear_self(self, event: AstrMessageEvent, group_id: str) -> Any:
        key = self._record_key(group_id, self._sender_id(event))
        records = await self.get_kv_data(key, [])
        if not records:
            return event.plain_result("目前数据库没有你的at数据,无法清除")

        await self.delete_kv_data(key)
        await self._forget_index_key(key)
        return event.plain_result("已成功清除")

    async def _clear_all(self, event: AstrMessageEvent) -> Any:
        keys = await self.get_kv_data(INDEX_KEY, [])
        for key in keys:
            await self.delete_kv_data(key)
        await self.delete_kv_data(INDEX_KEY)

        context_keys = await self.get_kv_data(CONTEXT_INDEX_KEY, [])
        for key in context_keys:
            await self.delete_kv_data(key)
        await self.delete_kv_data(CONTEXT_INDEX_KEY)

        self.before_cache.clear()
        self.after_tasks.clear()
        return event.plain_result("已成功清除全部艾特数据")

    async def _render_query_image(self, data: dict[str, Any]) -> str:
        data = dict(data)
        data.setdefault("layout", self._render_layout())
        data.setdefault("custom_font_css", self._custom_font_css())
        timeout = self._render_task_timeout_sec()
        if self._config_bool("render", "prefer_browser", default=True):
            try:
                return await asyncio.wait_for(
                    self._render_html_with_browser(HTML_TEMPLATE, data),
                    timeout=timeout,
                )
            except Exception as exc:
                logger.warning(f"[谁艾特我] 浏览器直渲失败，回退到 AstrBot html_render: {exc}")
        return await asyncio.wait_for(
            self.html_render(
                HTML_TEMPLATE,
                data,
                options={
                    "type": "jpeg",
                    "quality": self._render_quality(),
                    "full_page": True,
                    "timeout": self._render_page_timeout_ms(),
                },
            ),
            timeout=timeout,
        )

    async def _try_send(self, event: AstrMessageEvent, result: Any) -> bool:
        try:
            await event.send(result)
            return True
        except Exception as exc:
            logger.error(f"[谁艾特我] 主动发送失败: {exc}")
            return False

    async def _try_send_images(self, event: AstrMessageEvent, image_paths: list[str]) -> bool:
        if not image_paths:
            return False
        if len(image_paths) == 1:
            return await self._try_send(event, event.image_result(image_paths[0]))
        if await self._try_send_forward_images(event, "", image_paths):
            return True
        try:
            await event.send(event.chain_result([self._image_component(path) for path in image_paths]))
            return True
        except Exception as exc:
            logger.warning(f"[谁艾特我] 普通合并发送图片失败，回退到分开发送: {exc}")
            return False

    async def _try_send_text_images(self, event: AstrMessageEvent, text: str, image_paths: list[str]) -> bool:
        if not image_paths:
            return False
        if len(image_paths) > 1 and await self._try_send_forward_images(event, text, image_paths):
            return True
        try:
            components = [Comp.Plain(text)]
            components.extend(self._image_component(path) for path in image_paths)
            await event.send(event.chain_result(components))
            return True
        except Exception as exc:
            logger.warning(f"[谁艾特我] 合并发送提醒失败，回退到分开发送: {exc}")
            return False

    async def _try_send_forward_images(self, event: AstrMessageEvent, text: str, image_paths: list[str]) -> bool:
        group_id = self._group_id(event)
        if not group_id or len(image_paths) <= 1:
            return False

        self_id = self._self_id(event) or "10000"
        bot_name = await self._bot_name(event, group_id)
        uin = self._numeric_id(self_id)
        nodes = []
        if text.strip():
            nodes.append(
                {
                    "type": "node",
                    "data": {
                        "name": bot_name,
                        "uin": uin,
                        "content": [{"type": "text", "data": {"text": text}}],
                    },
                }
            )

        for image_path in image_paths:
            nodes.append(
                {
                    "type": "node",
                    "data": {
                        "name": bot_name,
                        "uin": uin,
                        "content": [{"type": "image", "data": {"file": self._onebot_image_file(image_path)}}],
                    },
                }
            )

        sent = await self._try_onebot_action(
            event,
            "send_group_forward_msg",
            group_id=self._numeric_id(group_id),
            messages=nodes,
        )
        if sent:
            return True

        logger.warning("[谁艾特我] 合并转发发送失败，回退到普通图片发送")
        return False

    async def _try_onebot_action(self, event: AstrMessageEvent, action: str, **kwargs: Any) -> bool:
        bot = getattr(event, "bot", None)
        caller = getattr(bot, "call_action", None)
        if not callable(caller):
            return False

        self_id = self._self_id(event)
        if self_id and "self_id" not in kwargs:
            kwargs["self_id"] = self_id

        try:
            await caller(action, **kwargs)
            return True
        except TypeError:
            kwargs.pop("self_id", None)
            try:
                await caller(action, **kwargs)
                return True
            except Exception as exc:
                logger.debug(f"[谁艾特我] 调用协议端 API {action} 失败: {exc}")
        except Exception as exc:
            logger.debug(f"[谁艾特我] 调用协议端 API {action} 失败: {exc}")
        return False

    def _image_component(self, image_path: str) -> Any:
        image_path = str(image_path)
        if re.match(r"^https?://", image_path, re.I):
            return Comp.Image.fromURL(image_path)
        return Comp.Image.fromFileSystem(image_path)

    def _onebot_image_file(self, image_path: str) -> str:
        image_path = str(image_path)
        if re.match(r"^https?://", image_path, re.I):
            return image_path
        try:
            return "base64://" + base64.b64encode(Path(image_path).read_bytes()).decode("ascii")
        except Exception:
            try:
                return Path(image_path).resolve().as_uri()
            except Exception:
                return image_path

    async def _render_html_with_browser(self, template: str, data: dict[str, Any]) -> str:
        from jinja2 import Environment
        from playwright.async_api import async_playwright

        self._cleanup_old_renders()
        html_text = Environment(autoescape=True).from_string(template).render(**data)
        output_path = self._new_render_path()
        browser = None
        async with async_playwright() as playwright:
            try:
                browser = await playwright.chromium.launch()
                page = await browser.new_page(
                    viewport={"width": 600, "height": 800},
                    device_scale_factor=2,
                )
                await page.set_content(html_text, wait_until="load", timeout=self._render_page_timeout_ms())
                await page.wait_for_timeout(500)
                element = await page.query_selector(".app")
                if element:
                    await element.screenshot(
                        path=str(output_path),
                        type="jpeg",
                        quality=self._render_quality(),
                    )
                else:
                    await page.screenshot(
                        path=str(output_path),
                        type="jpeg",
                        quality=self._render_quality(),
                        full_page=True,
                    )
            finally:
                if browser:
                    await browser.close()
        return str(output_path)

    def _new_render_path(self) -> Path:
        render_dir = self._render_dir()
        render_dir.mkdir(parents=True, exist_ok=True)
        return render_dir / f"who_at_me_{int(time.time())}_{uuid.uuid4().hex}.jpg"

    def _render_dir(self) -> Path:
        try:
            from astrbot.core.utils.astrbot_path import get_astrbot_data_path

            return Path(get_astrbot_data_path()) / "plugin_data" / "astrbot_plugin_who_at_me" / "renders"
        except Exception:
            return Path(tempfile.gettempdir()) / "astrbot_plugin_who_at_me" / "renders"

    def _cleanup_old_renders(self) -> None:
        render_dir = self._render_dir()
        if not render_dir.exists():
            return
        expire_before = time.time() - self._config_int("render", "cleanup_render_hours", default=24) * 60 * 60
        for path in render_dir.glob("who_at_me_*.jpg"):
            try:
                if path.stat().st_mtime < expire_before:
                    path.unlink()
            except OSError:
                pass

    async def _append_record(self, group_id: str, target: str, record: dict[str, Any]) -> None:
        key = self._record_key(group_id, target)
        records = await self.get_kv_data(key, [])
        if not isinstance(records, list):
            records = []
        if any(self._records_are_duplicate(item, record) for item in records[-10:]):
            return
        records.append(dict(record))
        records = records[-self._max_records_per_target():]
        await self.put_kv_data(key, records)
        await self._remember_index_key(key)

    async def _get_records(self, group_id: str, target: str) -> list[dict[str, Any]]:
        records = await self.get_kv_data(self._record_key(group_id, target), [])
        return records if isinstance(records, list) else []

    async def _get_pending_reminders(self, group_id: str, target: str) -> list[dict[str, Any]]:
        pending = await self.get_kv_data(self._reminder_pending_key(group_id, target), [])
        return pending if isinstance(pending, list) else []

    async def _member_info(self, event: AstrMessageEvent, group_id: str, user_id: str) -> dict[str, Any]:
        info = self._member_info_from_event(event) if user_id == self._sender_id(event) else {}
        if not user_id:
            return info
        if info.get("level") and info.get("role") and (info.get("title") or info.get("member_title")):
            return info

        api_info = await self._call_onebot_action(
            event,
            "get_group_member_info",
            group_id=self._numeric_id(group_id),
            user_id=self._numeric_id(user_id),
            no_cache=True,
        )
        api_info = self._mapping_data(api_info)
        if api_info:
            info.update(self._member_info_from_mapping(api_info))
        return info

    def _member_info_from_event(self, event: AstrMessageEvent) -> dict[str, Any]:
        sender = getattr(event.message_obj, "sender", None)
        raw_sender = {}
        raw = getattr(event.message_obj, "raw_message", None)
        if isinstance(raw, dict) and isinstance(raw.get("sender"), dict):
            raw_sender = raw["sender"]

        def pick(names: list[str]) -> Any:
            for name in names:
                value = getattr(sender, name, None) if sender else None
                if value:
                    return value
                value = raw_sender.get(name)
                if value:
                    return value
            return None

        return self._member_info_from_mapping(
            {
                "role": pick(["role"]),
                "title": pick(["title", "special_title", "specialTitle"]),
                "member_title": pick(
                    [
                        "member_title",
                        "memberTitle",
                        "group_title",
                        "groupTitle",
                        "level_title",
                        "levelTitle",
                        "rank_title",
                        "rankTitle",
                        "title_name",
                        "titleName",
                        "badge",
                        "nameplate",
                        "honor_title",
                        "honorTitle",
                    ]
                ),
                "level": pick(["level", "member_level", "qq_level", "qqLevel"]),
                "card": pick(["card"]),
                "nickname": pick(["nickname", "name"]),
            }
        )

    def _member_info_from_mapping(self, data: dict[str, Any]) -> dict[str, Any]:
        info: dict[str, Any] = {}
        if data.get("role"):
            info["role"] = str(data["role"])
        title = data.get("title") or data.get("special_title") or data.get("specialTitle")
        if title:
            info["title"] = str(title)
        member_title = (
            data.get("member_title")
            or data.get("memberTitle")
            or data.get("group_title")
            or data.get("groupTitle")
            or data.get("level_title")
            or data.get("levelTitle")
            or data.get("rank_title")
            or data.get("rankTitle")
            or data.get("title_name")
            or data.get("titleName")
            or data.get("badge")
            or data.get("nameplate")
            or data.get("honor_title")
            or data.get("honorTitle")
        )
        if member_title:
            info["member_title"] = str(member_title)
        level = data.get("level") or data.get("member_level") or data.get("qq_level") or data.get("qqLevel")
        if level:
            info["level"] = self._level_text(level)
        if data.get("card"):
            info["card"] = str(data["card"])
        if data.get("nickname") or data.get("name"):
            info["nickname"] = str(data.get("nickname") or data.get("name"))
        return info

    async def _reminder_group_enabled(self, event: AstrMessageEvent, group_id: str) -> bool:
        enabled_umos = self._reminder_enabled_group_umos()
        if enabled_umos:
            return self._event_umo(event) in enabled_umos

        value = await self.get_kv_data(self._reminder_group_key(group_id), None)
        return self._config_bool("reminder", "default_group_enabled", default=True) if value is None else bool(value)

    async def _set_reminder_group_enabled(self, group_id: str, enabled: bool) -> None:
        await self.put_kv_data(self._reminder_group_key(group_id), bool(enabled))

    async def _reminder_user_enabled(self, group_id: str, user_id: str) -> bool:
        value = await self.get_kv_data(self._reminder_user_key(group_id, user_id), None)
        return self._config_bool("reminder", "default_user_enabled", default=True) if value is None else bool(value)

    async def _set_reminder_user_enabled(self, group_id: str, user_id: str, enabled: bool) -> None:
        await self.put_kv_data(self._reminder_user_key(group_id, user_id), bool(enabled))

    async def _update_last_active(self, group_id: str, user_id: str, timestamp: int) -> None:
        await self.put_kv_data(self._reminder_last_active_key(group_id, user_id), int(timestamp))

    async def _reminder_context_config(self, group_id: str) -> dict[str, Any]:
        config = await self.get_kv_data(self._reminder_context_key(group_id), {})
        if not isinstance(config, dict):
            config = {}
        return {
            "enabled": bool(config.get("enabled", self._config_bool("reminder", "default_context_enabled", default=False))),
            "before": min(int(config.get("before", self._config_int("reminder", "default_context_before", default=1))), self._max_reminder_context()),
            "after": min(int(config.get("after", self._config_int("reminder", "default_context_after", default=1))), self._max_reminder_context()),
        }

    async def _set_reminder_context(
        self,
        group_id: str,
        enabled: bool,
        before: int | None = None,
        after: int | None = None,
    ) -> None:
        current = await self._reminder_context_config(group_id)
        current["enabled"] = bool(enabled)
        if before is not None:
            current["before"] = min(max(int(before), 0), self._max_reminder_context())
        if after is not None:
            current["after"] = min(max(int(after), 0), self._max_reminder_context())
        await self.put_kv_data(self._reminder_context_key(group_id), current)

    async def _reminder_status_text(self, event: AstrMessageEvent, group_id: str) -> str:
        sender_id = self._sender_id(event)
        context_config = await self._reminder_context_config(group_id)
        group_status = "开启" if await self._reminder_group_enabled(event, group_id) else "关闭"
        user_status = "开启" if await self._reminder_user_enabled(group_id, sender_id) else "关闭"
        context_status = "开启" if context_config.get("enabled") else "关闭"
        pending_count = len(await self._get_pending_reminders(group_id, sender_id)) if sender_id else 0
        current_umo = self._event_umo(event)
        global_umos = self._global_enabled_group_umos()
        enabled_umos = self._reminder_enabled_group_umos()
        global_status = "未配置名单" if not global_umos else ("已命中" if current_umo in global_umos else "未命中")
        umo_status = "未配置名单" if not enabled_umos else ("已命中" if current_umo in enabled_umos else "未命中")
        return (
            "艾特提醒状态：\n"
            f"本群提醒：{group_status}\n"
            f"当前 UMO：{current_umo or '未知'}\n"
            f"全局白名单：{global_status}\n"
            f"UMO 名单：{umo_status}\n"
            f"你的提醒：{user_status}\n"
            f"提醒上下文：{context_status}（前 {context_config.get('before', 0)} / 后 {context_config.get('after', 0)}）\n"
            f"离开判定：{self._reminder_away_seconds() // 60} 分钟未发言\n"
            f"待提醒记录：{pending_count} 条"
        )

    async def _context_enabled(self, group_id: str) -> bool:
        return bool(await self.get_kv_data(self._context_key(group_id), False))

    async def _set_context(self, group_id: str, enabled: bool) -> None:
        key = self._context_key(group_id)
        if enabled:
            await self.put_kv_data(key, True)
            context_keys = await self.get_kv_data(CONTEXT_INDEX_KEY, [])
            if key not in context_keys:
                context_keys.append(key)
                await self.put_kv_data(CONTEXT_INDEX_KEY, context_keys)
        else:
            await self.delete_kv_data(key)

    async def _remember_index_key(self, key: str) -> None:
        keys = await self.get_kv_data(INDEX_KEY, [])
        if key not in keys:
            keys.append(key)
            await self.put_kv_data(INDEX_KEY, keys)

    async def _forget_index_key(self, key: str) -> None:
        keys = await self.get_kv_data(INDEX_KEY, [])
        if key in keys:
            keys.remove(key)
            await self.put_kv_data(INDEX_KEY, keys)

    def _build_blocks(self, records: list[dict[str, Any]], target_name: str, reverse: bool = True) -> list[dict[str, Any]]:
        blocks = []
        for record in records:
            messages = []
            if record.get("is_context"):
                for idx, ctx in enumerate(record.get("before") or []):
                    msg = self._view_message(ctx, False, target_name)
                    msg["sort_time"] = float(ctx.get("time", 0)) - 0.01 + idx * 0.001
                    messages.append(msg)

            main = self._view_message(record, True, target_name)
            main["sort_time"] = float(record.get("time", 0))
            messages.append(main)

            if record.get("is_context"):
                for idx, ctx in enumerate(record.get("after") or []):
                    msg = self._view_message(ctx, False, target_name)
                    msg["sort_time"] = float(ctx.get("time", 0)) + 0.001 + idx * 0.001
                    messages.append(msg)

            blocks.append(
                {
                    "at_time": record.get("time", 0),
                    "msgs": self._dedupe_messages(messages),
                }
            )

        blocks.sort(key=lambda item: item["at_time"], reverse=reverse)
        return blocks

    def _view_message(self, data: dict[str, Any], is_at: bool, target_name: str) -> dict[str, Any]:
        nickname = str(data.get("name") or data.get("user_id") or data.get("User") or "用户")
        message = str(data.get("message") or "")
        if is_at:
            message = self._strip_at_display(message, [target_name, data.get("target"), data.get("at"), data.get("AtQQ")])
        role = str(data.get("role") or "member").lower()
        role_text = {"owner": "群主", "admin": "管理员", "administrator": "管理员"}.get(role, "群员")
        title = str(data.get("title") or "")
        member_title = str(data.get("member_title") or title or "")
        user_id = str(data.get("user_id") or data.get("User") or "")
        level = self._level_text(data.get("level"))
        identity_text = member_title or role_text
        tag_parts = [f"LV{level}"] if level else []
        if identity_text:
            tag_parts.append(identity_text)
        tag_text = " ".join(tag_parts)
        tag_color = "#b4b4b6"
        if role == "owner":
            tag_color = "#f6c751"
        elif role in {"admin", "administrator"}:
            tag_color = "#57d6c5"
        avatar = f"http://q1.qlogo.cn/g?b=qq&nk={user_id}&s=100" if user_id.isdigit() else ""
        return {
            "user_id": user_id,
            "nickname": nickname,
            "member_title": member_title,
            "initial": self._initial(nickname),
            "avatar": avatar,
            "message": message,
            "has_message": bool(message.strip()),
            "message_html": html.escape(message).replace("\n", "<br>"),
            "images": self._renderable_images(data.get("images") or data.get("image") or []),
            "quote": self._view_quote(data.get("quote")),
            "time": data.get("time", 0),
            "time_text": self._time_text(data.get("time", 0)),
            "is_at": is_at,
            "target_name": target_name,
            "role_class": role if role in {"owner", "admin", "administrator"} else "",
            "role_text": role_text,
            "level": level,
            "tag_text": tag_text,
            "tag_color": tag_color,
        }

    def _view_quote(self, quote: Any) -> dict[str, Any] | None:
        if not isinstance(quote, dict):
            return None
        message = str(quote.get("message") or "").strip()
        images = self._renderable_images(quote.get("images") or quote.get("image") or [])
        if not message and not images:
            return None
        nickname = str(quote.get("name") or quote.get("nickname") or quote.get("user_id") or "引用消息")
        return {
            "nickname": nickname,
            "message": message,
            "message_html": html.escape(message).replace("\n", "<br>"),
            "images": images[:3],
            "time_text": self._time_text(quote.get("time", 0)),
        }

    def _dedupe_messages(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        seen: dict[tuple[Any, ...], dict[str, Any]] = {}
        for msg in messages:
            key = (
                msg.get("user_id"),
                msg.get("time"),
                msg.get("message"),
                tuple(msg.get("images") or []),
                self._record_quote_key(msg),
            )
            if key not in seen or msg.get("is_at"):
                seen[key] = msg
        result = list(seen.values())
        result.sort(key=lambda item: item.get("sort_time", 0))
        return result

    def _chunk_blocks(self, blocks: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
        chunks: list[list[dict[str, Any]]] = []
        current: list[dict[str, Any]] = []
        count = 0
        for block in blocks:
            size = len(block["msgs"])
            if current and count + size > self._max_messages_per_image():
                chunks.append(current)
                current = []
                count = 0
            current.append(block)
            count += size
        if current:
            chunks.append(current)
        return chunks

    def _limit_chunks(self, chunks: list[list[dict[str, Any]]], max_pages: int) -> list[list[dict[str, Any]]]:
        if max_pages <= 0:
            return chunks
        return chunks[:max_pages]

    def _dedupe_records(self, records: list[dict[str, Any]]) -> list[dict[str, Any]]:
        deduped: list[dict[str, Any]] = []
        for record in records:
            for idx, existing in enumerate(deduped):
                if self._records_are_duplicate(existing, record):
                    if self._record_time(record) >= self._record_time(existing):
                        deduped[idx] = record
                    break
            else:
                deduped.append(record)
        return deduped

    def _mention_record(
        self,
        event: AstrMessageEvent,
        mentions: list[str] | None = None,
        member_info: dict[str, Any] | None = None,
        quote: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        sender_id = self._sender_id(event)
        sender = getattr(event.message_obj, "sender", None)
        member_info = member_info or {}
        role = str(member_info.get("role") or getattr(sender, "role", "") or self._raw_sender_value(event, "role") or "member")
        record = {
            "user_id": sender_id,
            "message": self._message_text_for_record(event, mentions or []),
            "images": self._images(event),
            "name": member_info.get("card") or member_info.get("nickname") or self._sender_name(event),
            "role": role,
            "title": member_info.get("title") or "",
            "member_title": member_info.get("member_title") or "",
            "level": member_info.get("level") or "",
            "time": self._timestamp(event),
            "message_id": str(getattr(event.message_obj, "message_id", "") or ""),
        }
        if quote:
            record["quote"] = quote
        return record

    def _context_message(
        self,
        event: AstrMessageEvent,
        member_info: dict[str, Any] | None = None,
        quote: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        record = self._mention_record(event, member_info=member_info, quote=quote)
        context = {
            "user_id": record["user_id"],
            "message": record["message"],
            "images": record["images"],
            "name": record["name"],
            "role": record["role"],
            "title": record.get("title") or "",
            "member_title": record.get("member_title") or "",
            "level": record.get("level") or "",
            "time": record["time"],
        }
        if record.get("quote"):
            context["quote"] = record["quote"]
        return context

    async def _quote(self, event: AstrMessageEvent) -> dict[str, Any] | None:
        quote = self._quote_from_event(event)
        quote_id = str((quote or {}).get("message_id") or "")
        if quote_id and not self._quote_has_content(quote):
            fetched = await self._fetch_quote_message(event, quote_id)
            if fetched:
                merged = dict(quote or {})
                for key, value in fetched.items():
                    if value or key not in merged:
                        merged[key] = value
                return merged

        if self._quote_has_content(quote):
            return quote
        if quote_id:
            return {"message_id": quote_id, "message": "引用消息"}
        return None

    def _quote_from_event(self, event: AstrMessageEvent) -> dict[str, Any] | None:
        for segment in self._raw_message_segments(event):
            if not self._is_reference_segment(segment):
                continue
            quote = self._quote_from_segment(segment)
            if quote:
                return quote

        for item in self._message_chain(event):
            if not self._is_reference_segment(item):
                continue
            quote = self._quote_from_segment(item)
            if quote:
                return quote
        return None

    def _quote_from_segment(self, segment: Any) -> dict[str, Any] | None:
        data = self._segment_data(segment)
        quote = (self._quote_from_mapping(data) if data else {}) or {}
        message_id = (
            self._segment_value(segment, ["id", "message_id", "messageId", "msg_id", "msgId"])
            or (data.get("id") if isinstance(data, dict) else None)
            or (data.get("message_id") if isinstance(data, dict) else None)
        )
        if message_id:
            quote["message_id"] = str(message_id)
        return quote or None

    async def _fetch_quote_message(self, event: AstrMessageEvent, message_id: str) -> dict[str, Any] | None:
        for action in ("get_msg", "get_message"):
            payload = await self._call_onebot_action(event, action, message_id=self._numeric_id(message_id))
            quote = self._quote_from_mapping(payload)
            if quote:
                if not quote.get("message_id"):
                    quote["message_id"] = str(message_id)
                if self._quote_has_content(quote):
                    return quote
        return None

    def _quote_from_mapping(self, value: Any) -> dict[str, Any] | None:
        data = self._mapping_data(value)
        if not data:
            return None

        message_value = self._first_mapping_value(
            data,
            ["message", "message_chain", "messageChain", "content", "raw_message", "rawMessage"],
        )
        segments = self._segments_from_value(message_value)
        message = self._segments_text(segments, include_at=True) if segments else ""
        images = self._segments_images(segments) if segments else []

        raw_message = message_value if isinstance(message_value, str) else self._first_mapping_value(data, ["raw_message", "rawMessage"])
        if isinstance(raw_message, str):
            if not message:
                message = self._strip_cq_display(raw_message)
            images.extend(self._images_from_cq(raw_message))

        if not message:
            for key in ("text", "plain", "summary"):
                text = data.get(key)
                if isinstance(text, str) and text.strip():
                    message = self._strip_cq_display(text)
                    break

        sender = self._mapping_data(data.get("sender"))
        user_id = str(
            self._first_mapping_value(sender, ["user_id", "userId", "id", "qq"])
            or self._first_mapping_value(data, ["user_id", "userId", "sender_id", "senderId"])
            or ""
        )
        name = str(
            self._first_mapping_value(data, ["sender_name", "senderName", "nickname", "name"])
            or self._first_mapping_value(sender, ["card", "nickname", "name"])
            or user_id
            or "引用消息"
        )
        quote = {
            "message_id": str(self._first_mapping_value(data, ["message_id", "messageId", "id"]) or ""),
            "user_id": user_id,
            "name": name,
            "message": message,
            "images": self._unique_strings(images),
            "time": self._first_mapping_value(
                data,
                ["time", "timestamp", "send_time", "sendTime", "message_time", "messageTime", "msg_time", "msgTime"],
            )
            or 0,
        }
        return quote if self._quote_has_identity(quote) else None

    def _quote_has_identity(self, quote: dict[str, Any] | None) -> bool:
        if not quote:
            return False
        return bool(
            quote.get("message_id")
            or quote.get("user_id")
            or quote.get("message")
            or quote.get("images")
        )

    def _quote_has_content(self, quote: dict[str, Any] | None) -> bool:
        if not quote:
            return False
        return bool(str(quote.get("message") or "").strip() or quote.get("images"))

    def _query_target(self, event: AstrMessageEvent, text: str, mentions: list[str]) -> str:
        if "我" in text:
            return self._sender_id(event)
        if mentions:
            return mentions[0]
        match = re.search(r"@\S*?\((\d{5,})\)|@(\d{5,})", text)
        return next((group for group in match.groups() if group), "") if match else ""

    def _mentions(self, event: AstrMessageEvent) -> list[str]:
        result = []
        raw_segments = self._raw_message_segments(event)
        if raw_segments:
            for segment in raw_segments:
                if str(segment.get("type", "")).lower() != "at":
                    continue
                data = segment.get("data") or {}
                value = data.get("qq") or data.get("user_id") or data.get("target") or data.get("id")
                self._append_mention(result, value)
            return list(dict.fromkeys(result))

        for item in self._message_chain(event):
            if self._is_reference_segment(item):
                continue
            if item.__class__.__name__.lower() != "at" and not hasattr(item, "qq"):
                continue
            value = self._first_attr(item, ["qq", "user_id", "target", "id"])
            self._append_mention(result, value)
        return list(dict.fromkeys(result))

    def _images(self, event: AstrMessageEvent) -> list[str]:
        raw_segments = self._raw_message_segments(event)
        if raw_segments:
            return self._segments_images(raw_segments)
        return self._segments_images(self._message_chain(event))

    def _append_mention(self, result: list[str], value: Any) -> None:
        if value is None:
            return
        value_str = str(value)
        if value_str.lower() in {"all", "全体成员", "here", "@all"}:
            result.append(ALL_TARGET)
        else:
            result.append(value_str)

    def _raw_message_segments(self, event: AstrMessageEvent) -> list[dict[str, Any]]:
        raw = getattr(event.message_obj, "raw_message", None)
        if not isinstance(raw, dict):
            return []
        segments = raw.get("message") or raw.get("message_chain") or []
        return [segment for segment in segments if isinstance(segment, dict)]

    def _message_chain(self, event: AstrMessageEvent) -> list[Any]:
        if hasattr(event, "get_messages"):
            try:
                return list(event.get_messages())
            except Exception:
                pass
        return list(getattr(event.message_obj, "message", []) or [])

    def _message_text(self, event: AstrMessageEvent) -> str:
        return str(getattr(event, "message_str", "") or getattr(event.message_obj, "message_str", "") or "").strip()

    def _message_text_for_record(self, event: AstrMessageEvent, mentions: list[str]) -> str:
        raw_segments = self._raw_message_segments(event)
        if raw_segments:
            text = self._segments_text(raw_segments)
        else:
            text = self._segments_text(self._message_chain(event)) or self._message_text(event)

        return self._strip_at_display(self._strip_cq_display(text), mentions)

    def _strip_at_display(self, text: str, mentions: list[str]) -> str:
        cleaned = re.sub(r"\[CQ:at,[^\]]+\]", " ", text)
        cleaned = re.sub(r"(?<!\S)@\S+\([0-9]+\)", " ", cleaned)
        cleaned = re.sub(r"(?<!\S)@\S+", " ", cleaned)
        for mention in mentions:
            if not mention:
                continue
            if mention == ALL_TARGET:
                cleaned = cleaned.replace("@全体成员", " ").replace("@all", " ")
            else:
                cleaned = re.sub(rf"(?<!\S)@?{re.escape(str(mention))}(?:\([0-9]+\))?(?!\S)", " ", cleaned)
        return re.sub(r"\s+", " ", cleaned).strip()

    def _is_reference_segment(self, item: Any) -> bool:
        seg_type = self._segment_type(item)
        if seg_type in REFERENCE_SEGMENT_TYPES:
            return True
        cls_name = item.__class__.__name__.lower()
        return any(token in cls_name for token in REFERENCE_SEGMENT_TYPES)

    def _segments_from_value(self, value: Any) -> list[Any]:
        if isinstance(value, list):
            return value
        if isinstance(value, dict):
            nested = self._first_mapping_value(value, ["message", "message_chain", "messageChain", "content"])
            if nested is not None and nested is not value:
                return self._segments_from_value(nested)
            if value.get("type") or value.get("seg_type"):
                return [value]
            return []
        for attr in ("message", "messages", "message_chain", "messageChain", "content"):
            nested = getattr(value, attr, None)
            if nested is not None and nested is not value:
                return self._segments_from_value(nested)
        return []

    def _segments_text(self, segments: list[Any], include_at: bool = False) -> str:
        texts = []
        for segment in segments:
            if self._is_reference_segment(segment):
                continue
            seg_type = self._segment_type(segment)
            if seg_type in {"text", "plain"}:
                value = self._segment_value(segment, ["text", "content", "message"])
                if value:
                    texts.append(str(value))
            elif include_at and seg_type == "at":
                value = self._segment_value(segment, ["name", "display", "qq", "user_id", "target", "id"])
                if value:
                    texts.append(f"@{value}")
        return "".join(texts).strip()

    def _segments_images(self, segments: list[Any]) -> list[str]:
        urls = []
        for segment in segments:
            if self._is_reference_segment(segment):
                continue
            if self._segment_type(segment) != "image":
                continue
            value = self._segment_value(segment, ["url", "file", "path"])
            if value:
                urls.append(str(value))
        return self._unique_strings(urls)

    def _segment_type(self, segment: Any) -> str:
        if isinstance(segment, dict):
            return str(segment.get("type") or segment.get("seg_type") or "").lower()
        seg_type = str(getattr(segment, "type", "") or getattr(segment, "seg_type", "") or "").lower()
        return seg_type or segment.__class__.__name__.lower()

    def _segment_data(self, segment: Any) -> dict[str, Any]:
        if isinstance(segment, dict):
            data = segment.get("data")
            return data if isinstance(data, dict) else segment
        data = getattr(segment, "data", None)
        return data if isinstance(data, dict) else {}

    def _segment_value(self, segment: Any, names: list[str]) -> Any:
        data = self._segment_data(segment)
        if data:
            value = self._first_mapping_value(data, names)
            if value is not None:
                return value
        if isinstance(segment, dict):
            return self._first_mapping_value(segment, names)
        return self._first_attr(segment, names)

    def _strip_cq_display(self, text: str) -> str:
        cleaned = re.sub(r"\[CQ:reply,[^\]]+\]", " ", text)
        cleaned = re.sub(r"\[CQ:image,[^\]]+\]", " ", cleaned)
        cleaned = re.sub(r"\[CQ:at,qq=([^,\]]+)[^\]]*\]", r"@\1", cleaned)
        cleaned = re.sub(r"\[CQ:[^\]]+\]", " ", cleaned)
        return re.sub(r"\s+", " ", cleaned).strip()

    def _images_from_cq(self, text: str) -> list[str]:
        images = []
        for attrs in re.findall(r"\[CQ:image,([^\]]+)\]", text):
            data = self._parse_cq_attrs(attrs)
            value = data.get("url") or data.get("file") or data.get("path")
            if value:
                images.append(value)
        return self._unique_strings(images)

    def _renderable_images(self, images: Any) -> list[str]:
        if isinstance(images, str):
            candidates = [images]
        elif isinstance(images, list):
            candidates = images
        else:
            candidates = []

        result = []
        for image in candidates:
            value = self._renderable_image(image)
            if value:
                result.append(value)
        return self._unique_strings(result)

    def _renderable_image(self, image: Any) -> str:
        value = str(image or "").strip()
        if not value:
            return ""
        if re.match(r"^https?://", value, re.I):
            return value
        if re.match(r"^data:image/", value, re.I):
            return value
        if value.startswith("base64://"):
            return "data:image/png;base64," + value[len("base64://") :]
        if re.match(r"^file://", value, re.I):
            return value
        try:
            path = Path(value)
            if path.exists():
                return path.resolve().as_uri()
        except (OSError, ValueError):
            pass
        return ""

    def _parse_cq_attrs(self, attrs: str) -> dict[str, str]:
        result = {}
        for part in attrs.split(","):
            if "=" not in part:
                continue
            key, value = part.split("=", 1)
            result[key.strip()] = value.strip()
        return result

    def _first_mapping_value(self, data: dict[str, Any], names: list[str]) -> Any:
        for name in names:
            value = data.get(name)
            if value is not None and value != "":
                return value
        return None

    def _unique_strings(self, values: list[Any]) -> list[str]:
        result = []
        for value in values:
            if not value:
                continue
            text = str(value)
            if text not in result:
                result.append(text)
        return result

    def _normalize_command_text(self, text: str) -> str:
        normalized = text.strip()
        for prefix in ("/", "／", "#", "＃", "!", "！"):
            if normalized.startswith(prefix):
                return normalized[len(prefix) :].strip()
        return normalized

    def _group_id(self, event: AstrMessageEvent) -> str:
        return str(getattr(event.message_obj, "group_id", "") or "")

    def _sender_id(self, event: AstrMessageEvent) -> str:
        if hasattr(event, "get_sender_id"):
            try:
                return str(event.get_sender_id())
            except Exception:
                pass
        sender = getattr(event.message_obj, "sender", None)
        return str(
            getattr(sender, "user_id", "")
            or getattr(sender, "id", "")
            or self._raw_sender_value(event, "user_id")
            or ""
        )

    def _sender_name(self, event: AstrMessageEvent) -> str:
        if hasattr(event, "get_sender_name"):
            try:
                name = event.get_sender_name()
                if name:
                    return str(name)
            except Exception:
                pass
        sender = getattr(event.message_obj, "sender", None)
        return str(
            getattr(sender, "card", "")
            or getattr(sender, "nickname", "")
            or self._raw_sender_value(event, "card")
            or self._raw_sender_value(event, "nickname")
            or self._sender_id(event)
        )

    async def _target_name(self, event: AstrMessageEvent, group_id: str, target: str) -> str:
        if target == self._sender_id(event):
            return self._sender_name(event)
        if target == ALL_TARGET:
            return "全体成员"
        mention_name = self._mention_display_name(event, target)
        if mention_name:
            return mention_name
        member_info = await self._member_info(event, group_id, target)
        for key in ("card", "nickname", "name"):
            name = member_info.get(key)
            if name and not self._looks_like_numeric_id(name):
                return str(name)
        return str(target)

    def _mention_display_name(self, event: AstrMessageEvent, target: str) -> str:
        for segment in [*self._raw_message_segments(event), *self._message_chain(event)]:
            if self._segment_type(segment) != "at":
                continue
            value = self._segment_value(segment, ["qq", "user_id", "target", "id"])
            if str(value or "") != str(target):
                continue
            name = self._segment_value(segment, ["name", "display", "text", "nickname"])
            if not name:
                continue
            text = re.sub(r"^@", "", str(name)).strip()
            if text and not self._looks_like_numeric_id(text):
                return text
        return ""

    async def _group_name(self, event: AstrMessageEvent, group_id: str) -> str:
        group = getattr(event.message_obj, "group", None)
        if group and getattr(group, "group_name", None):
            name = str(group.group_name)
            if self._is_valid_group_name(name, group_id):
                return name
        raw = getattr(event.message_obj, "raw_message", None)
        if isinstance(raw, dict):
            for key in ("group_name", "groupName", "name"):
                name = raw.get(key)
                if self._is_valid_group_name(name, group_id):
                    return str(name)

        group_info = await self._call_onebot_action(
            event,
            "get_group_info",
            group_id=self._numeric_id(group_id),
            no_cache=True,
        )
        group_info = self._mapping_data(group_info)
        for key in ("group_name", "groupName", "name"):
            name = group_info.get(key)
            if self._is_valid_group_name(name, group_id):
                return str(name)
        return str(group_id)

    def _is_valid_group_name(self, value: Any, group_id: str) -> bool:
        text = str(value or "").strip()
        if not text:
            return False
        return text.lower() not in {"n/a", "na", "none", "null", "undefined", "unknown", str(group_id).lower()}

    async def _member_count(self, event: AstrMessageEvent, group_id: str) -> int:
        group = getattr(event.message_obj, "group", None)
        members = getattr(group, "members", None) if group else None
        if members:
            return len(members)

        raw = getattr(event.message_obj, "raw_message", None)
        if isinstance(raw, dict):
            for key in ("member_count", "member_num", "group_member_count"):
                value = raw.get(key)
                try:
                    if value:
                        return int(value)
                except (TypeError, ValueError):
                    pass
        group_info = await self._call_onebot_action(
            event,
            "get_group_info",
            group_id=self._numeric_id(group_id),
            no_cache=True,
        )
        group_info = self._mapping_data(group_info)
        if isinstance(group_info, dict):
            for key in ("member_count", "member_num", "group_member_count"):
                value = group_info.get(key)
                try:
                    if value:
                        return int(value)
                except (TypeError, ValueError):
                    pass
        return 0

    async def _bot_name(self, event: AstrMessageEvent, group_id: str) -> str:
        cache_key = f"{self._platform_id(event)}:{group_id}:{self._self_id(event)}"
        cached = self.bot_name_cache.get(cache_key)
        if cached:
            return cached

        candidates = []
        raw = getattr(event.message_obj, "raw_message", None)
        if isinstance(raw, dict):
            for key in ("self_name", "self_nickname", "bot_name", "bot_nickname", "nickname"):
                candidates.append(raw.get(key))
            for key in ("self", "bot", "login_info"):
                value = raw.get(key)
                if isinstance(value, dict):
                    for name_key in ("card", "nickname", "name", "display_name"):
                        candidates.append(value.get(name_key))

        for attr in ("self_name", "self_nickname", "bot_name", "bot_nickname"):
            candidates.append(getattr(event.message_obj, attr, None))

        api_name = await self._bot_name_from_api(event, group_id)
        if api_name:
            candidates.insert(0, api_name)

        for candidate in candidates:
            if candidate and not self._looks_like_numeric_id(candidate):
                name = str(candidate)
                self.bot_name_cache[cache_key] = name
                return name
        return "我"

    async def _bot_name_from_api(self, event: AstrMessageEvent, group_id: str) -> str:
        self_id = self._self_id(event)
        if self_id and group_id:
            info = await self._call_onebot_action(
                event,
                "get_group_member_info",
                group_id=self._numeric_id(group_id),
                user_id=self._numeric_id(self_id),
                no_cache=True,
            )
            name = self._name_from_mapping(info, ["card", "nickname"])
            if name:
                return name

            group = await self._event_group(event, group_id)
            if group and getattr(group, "members", None):
                for member in group.members or []:
                    if str(getattr(member, "user_id", "")) == str(self_id):
                        nickname = getattr(member, "nickname", None)
                        if nickname:
                            return str(nickname)

        info = await self._call_onebot_action(event, "get_login_info")
        return self._name_from_mapping(info, ["nickname", "name"])

    async def _event_group(self, event: AstrMessageEvent, group_id: str) -> Any:
        getter = getattr(event, "get_group", None)
        if not callable(getter):
            return None
        try:
            return await getter(group_id)
        except Exception as exc:
            logger.debug(f"[谁艾特我] 获取群信息失败: {exc}")
            return None

    async def _call_onebot_action(self, event: AstrMessageEvent, action: str, **kwargs) -> Any:
        bot = getattr(event, "bot", None)
        caller = getattr(bot, "call_action", None)
        if not callable(caller):
            return None

        self_id = self._self_id(event)
        if self_id and "self_id" not in kwargs:
            kwargs["self_id"] = self_id

        try:
            return await caller(action, **kwargs)
        except TypeError:
            kwargs.pop("self_id", None)
            try:
                return await caller(action, **kwargs)
            except Exception as exc:
                logger.debug(f"[谁艾特我] 调用协议端 API {action} 失败: {exc}")
        except Exception as exc:
            logger.debug(f"[谁艾特我] 调用协议端 API {action} 失败: {exc}")
        return None

    def _name_from_mapping(self, value: Any, keys: list[str]) -> str:
        data = self._mapping_data(value)
        if not data:
            return ""
        candidates = [data]
        if isinstance(value, dict) and value is not data:
            candidates.append(value)
        for mapping in candidates:
            for key in keys:
                name = mapping.get(key)
                if name and not self._looks_like_numeric_id(name):
                    return str(name)
        return ""

    def _mapping_data(self, value: Any) -> dict[str, Any]:
        if not isinstance(value, dict):
            return {}
        data = value.get("data")
        return data if isinstance(data, dict) else value

    def _self_id(self, event: AstrMessageEvent) -> str:
        if hasattr(event, "get_self_id"):
            try:
                self_id = event.get_self_id()
                if self_id:
                    return str(self_id)
            except Exception:
                pass
        return str(getattr(event.message_obj, "self_id", "") or "")

    def _platform_id(self, event: AstrMessageEvent) -> str:
        if hasattr(event, "get_platform_id"):
            try:
                return str(event.get_platform_id())
            except Exception:
                pass
        meta = getattr(event, "platform_meta", None)
        return str(getattr(meta, "id", "") or getattr(meta, "name", "") or "")

    def _numeric_id(self, value: Any) -> Any:
        text = str(value)
        return int(text) if text.isdigit() else value

    def _timestamp(self, event: AstrMessageEvent) -> int:
        value = getattr(event.message_obj, "timestamp", None)
        try:
            return int(value or time.time())
        except (TypeError, ValueError):
            return int(time.time())

    def _is_admin(self, event: AstrMessageEvent) -> bool:
        for name in ("is_admin", "isAdmin", "is_master", "isMaster"):
            checker = getattr(event, name, None)
            if callable(checker):
                try:
                    if checker():
                        return True
                except Exception:
                    pass
            elif checker:
                return True

        sender = getattr(event.message_obj, "sender", None)
        role = str(getattr(sender, "role", "") or self._raw_sender_value(event, "role") or "").lower()
        return role in {"admin", "owner", "administrator", "master"}

    def _raw_sender_value(self, event: AstrMessageEvent, key: str) -> Any:
        raw = getattr(event.message_obj, "raw_message", None)
        if isinstance(raw, dict):
            sender = raw.get("sender")
            if isinstance(sender, dict):
                return sender.get(key)
        return None

    def _first_attr(self, obj: Any, names: list[str]) -> Any:
        for name in names:
            if hasattr(obj, name):
                value = getattr(obj, name)
                if value is not None:
                    return value
        return None

    def _time_text(self, value: Any) -> str:
        try:
            timestamp = int(float(value))
            if timestamp <= 0:
                return ""
            if timestamp > 10_000_000_000:
                timestamp //= 1000
            return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            return ""

    def _initial(self, name: str) -> str:
        clean = name.strip()
        return clean[:1].upper() if clean else "?"

    def _looks_like_numeric_id(self, value: Any) -> bool:
        text = str(value).strip()
        return text.isdigit() and len(text) >= 5

    def _level_text(self, value: Any) -> str:
        if value is None or value == "":
            return ""
        text = str(value).strip()
        if text in {"0", "-1"}:
            return ""
        return re.sub(r"^lv", "", text, flags=re.I)

    def _plain_summary(self, records: list[dict[str, Any]], target_name: str) -> str:
        lines = [f"谁艾特了 {target_name}："]
        for record in records[:20]:
            name = record.get("name") or record.get("user_id") or "用户"
            msg = record.get("message") or "[无文字]"
            image_count = len(record.get("images") or record.get("image") or [])
            suffix = f"（{image_count} 张图）" if image_count else ""
            lines.append(f"{self._time_text(record.get('time'))} {name}: {msg}{suffix}")
        if len(records) > 20:
            lines.append(f"... 还有 {len(records) - 20} 条")
        return "\n".join(lines)

    def _records_are_duplicate(self, left: dict[str, Any], right: dict[str, Any], window_seconds: int = 3) -> bool:
        left_message_id = str(left.get("message_id") or "")
        right_message_id = str(right.get("message_id") or "")
        if left_message_id and right_message_id and left_message_id == right_message_id:
            return True

        if str(left.get("user_id") or left.get("User") or "") != str(right.get("user_id") or right.get("User") or ""):
            return False

        left_target = str(left.get("target") or "")
        right_target = str(right.get("target") or "")
        if left_target and right_target and left_target != right_target:
            return False

        if self._record_message_key(left) != self._record_message_key(right):
            return False
        if self._record_images_key(left) != self._record_images_key(right):
            return False
        if self._record_quote_key(left) != self._record_quote_key(right):
            return False

        return abs(self._record_time(left) - self._record_time(right)) <= window_seconds

    def _record_message_key(self, record: dict[str, Any]) -> str:
        return self._normalize_record_text(record.get("message"))

    def _normalize_record_text(self, value: Any) -> str:
        return re.sub(r"\s+", " ", str(value or "")).strip()

    def _record_images_key(self, record: dict[str, Any]) -> tuple[str, ...]:
        return tuple(str(image) for image in (record.get("images") or record.get("image") or []))

    def _record_quote_key(self, record: dict[str, Any]) -> tuple[str, str, tuple[str, ...]]:
        quote = record.get("quote")
        if not isinstance(quote, dict):
            return ("", "", ())
        images = tuple(str(image) for image in (quote.get("images") or quote.get("image") or []))
        return (
            str(quote.get("message_id") or ""),
            self._normalize_record_text(quote.get("message")),
            images,
        )

    def _record_time(self, record: dict[str, Any]) -> int:
        try:
            return int(float(record.get("time") or 0))
        except (TypeError, ValueError):
            return 0

    def _config_value(self, *keys: str, default: Any = None) -> Any:
        value: Any = self.config
        for key in keys:
            if isinstance(value, dict):
                value = value.get(key)
            else:
                getter = getattr(value, "get", None)
                value = getter(key) if callable(getter) else None
            if value is None:
                return default
        return value

    def _config_bool(self, *keys: str, default: bool = False) -> bool:
        value = self._config_value(*keys, default=default)
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on", "开启"}
        return bool(value)

    def _config_int(self, *keys: str, default: int = 0) -> int:
        value = self._config_value(*keys, default=default)
        try:
            return int(value)
        except (TypeError, ValueError):
            return int(default)

    def _config_str(self, *keys: str, default: str = "") -> str:
        value = self._config_value(*keys, default=default)
        return str(value if value is not None else default)

    def _config_list(self, *keys: str) -> list[str]:
        value = self._config_value(*keys, default=[])
        if isinstance(value, list):
            return [str(item).strip() for item in value if str(item).strip()]
        if isinstance(value, str):
            parts = re.split(r"[\n,，]+", value)
            return [part.strip() for part in parts if part.strip()]
        return []

    def _json_response(self, payload: dict[str, Any]) -> Any:
        return self._jsonify(payload) if callable(self._jsonify) else payload

    def _page_data(self) -> dict[str, Any]:
        data = self._font_data()
        data["layout"] = self._render_layout()
        return data

    def _font_data(self) -> dict[str, Any]:
        current_path = str(self.page_settings.get("font_path") or "")
        current_name = self._selected_font_name(current_path)
        return {
            "fonts": self._list_uploaded_fonts(),
            "current_path": current_path,
            "current_name": current_name,
        }

    def _plugin_data_dir(self) -> Path:
        try:
            from astrbot.core.utils.astrbot_path import get_astrbot_data_path

            return Path(get_astrbot_data_path()) / "plugin_data" / "astrbot_plugin_who_at_me"
        except Exception:
            return Path(tempfile.gettempdir()) / "astrbot_plugin_who_at_me"

    def _page_settings_file(self) -> Path:
        return self._plugin_data_dir() / "page_settings.json"

    def _fonts_dir(self) -> Path:
        return self._plugin_data_dir() / "resources" / "fonts"

    def _load_page_settings(self) -> dict[str, Any]:
        settings = dict(PAGE_SETTINGS_DEFAULTS)
        path = self._page_settings_file()
        try:
            if path.exists():
                raw = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    settings.update(raw)
        except Exception as exc:
            logger.warning(f"[谁艾特我] 读取 Page 设置失败，使用默认值: {exc}")
        settings.update(self._sanitize_layout_settings(settings))
        settings["font_path"] = str(settings.get("font_path") or "").strip()
        return settings

    def _save_page_settings(self) -> None:
        path = self._page_settings_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(self.page_settings, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _sanitize_layout_settings(self, value: Any) -> dict[str, int]:
        data = value if isinstance(value, dict) else {}
        return {
            "time_x": self._clamp_int(data.get("time_x"), PAGE_SETTINGS_DEFAULTS["time_x"], 0, 600),
            "time_y": self._clamp_int(data.get("time_y"), PAGE_SETTINGS_DEFAULTS["time_y"], 0, 120),
            "group_x": self._clamp_int(data.get("group_x"), PAGE_SETTINGS_DEFAULTS["group_x"], 0, 600),
            "group_y": self._clamp_int(data.get("group_y"), PAGE_SETTINGS_DEFAULTS["group_y"], 0, 120),
        }

    def _render_layout(self) -> dict[str, int]:
        return self._sanitize_layout_settings(self.page_settings)

    def _clamp_int(self, value: Any, default: int, minimum: int, maximum: int) -> int:
        try:
            number = int(value)
        except (TypeError, ValueError):
            number = int(default)
        return min(maximum, max(minimum, number))

    def _is_same_origin_request(self, request: Any) -> bool:
        host = request.headers.get("Host", "") if request else ""
        origin = request.headers.get("Origin", "") if request else ""
        referer = request.headers.get("Referer", "") if request else ""
        sec_fetch_site = request.headers.get("Sec-Fetch-Site", "") if request else ""
        if sec_fetch_site and sec_fetch_site not in {"same-origin", "same-site", "none"}:
            return False
        if origin:
            return bool(host and origin.split("://", 1)[-1].split("/", 1)[0] == host)
        if referer:
            return bool(host and referer.split("://", 1)[-1].split("/", 1)[0] == host)
        return not sec_fetch_site or sec_fetch_site == "none"

    def _sanitize_font_filename(self, filename: str) -> str:
        name = Path(str(filename or "")).name.strip()
        stem = Path(name).stem
        suffix = Path(name).suffix.lower()
        safe_stem = re.sub(r"[^0-9A-Za-z._\-一-鿿]+", "_", stem).strip("._-")
        if not safe_stem:
            safe_stem = "font"
        return f"{safe_stem[:80]}{suffix}"

    def _is_allowed_font_file(self, filename: str) -> bool:
        return Path(str(filename or "")).suffix.lower() in {".ttf", ".otf", ".woff", ".woff2", ".ttc"}

    def _format_file_size(self, size: int) -> str:
        if size < 1024 * 1024:
            return f"{size / 1024:.1f}KB"
        return f"{size / 1024 / 1024:.1f}MB"

    def _font_config_path(self, filename: str) -> str:
        return f"resources/fonts/{filename}"

    def _selected_font_name(self, font_path: str) -> str:
        text = str(font_path or "").replace("\\", "/").strip()
        return Path(text).name if text else ""

    def _list_uploaded_fonts(self) -> list[dict[str, Any]]:
        current_font = self._selected_font_name(str(self.page_settings.get("font_path") or ""))
        fonts: list[dict[str, Any]] = []
        fonts_dir = self._fonts_dir()
        if not fonts_dir.exists():
            return fonts
        for font_file in sorted(fonts_dir.iterdir(), key=lambda item: item.name.lower()):
            if not font_file.is_file() or not self._is_allowed_font_file(font_file.name):
                continue
            stat = font_file.stat()
            fonts.append(
                {
                    "name": font_file.name,
                    "path": self._font_config_path(font_file.name),
                    "size": stat.st_size,
                    "size_text": self._format_file_size(stat.st_size),
                    "is_current": font_file.name == current_font,
                }
            )
        return fonts

    def _save_font_path(self, font_path: str) -> None:
        font_path = str(font_path or "").strip()
        if font_path:
            font_name = self._sanitize_font_filename(self._selected_font_name(font_path))
            font_path = self._font_config_path(font_name) if font_name else ""
        self.page_settings["font_path"] = font_path
        self._font_css_cache_key = None
        self._font_css_cache_value = ""
        self._save_page_settings()

    def _resolve_custom_font_path(self) -> Path | None:
        font_path = str(self.page_settings.get("font_path") or "").strip()
        if not font_path:
            return None
        raw_path = Path(font_path).expanduser()
        candidates = [raw_path] if raw_path.is_absolute() else [
            self._plugin_data_dir() / raw_path,
            self._fonts_dir() / raw_path.name,
            Path(__file__).resolve().parent / raw_path,
            Path(__file__).resolve().parent / "fonts" / raw_path.name,
        ]
        for candidate in candidates:
            try:
                if candidate.exists() and candidate.is_file():
                    return candidate.resolve()
            except OSError:
                continue
        logger.warning(f"[谁艾特我] 自定义字体文件不存在，使用默认字体: {font_path}")
        return None

    def _font_format(self, font_path: Path) -> str:
        suffix = font_path.suffix.lower()
        if suffix == ".otf":
            return "opentype"
        if suffix == ".woff":
            return "woff"
        if suffix == ".woff2":
            return "woff2"
        return "truetype"

    def _font_mime_type(self, font_path: Path) -> str:
        suffix = font_path.suffix.lower()
        if suffix == ".otf":
            return "font/otf"
        if suffix == ".woff":
            return "font/woff"
        if suffix == ".woff2":
            return "font/woff2"
        return "font/ttf"

    def _custom_font_css(self) -> str:
        font_path = self._resolve_custom_font_path()
        if not font_path:
            self._font_css_cache_key = None
            self._font_css_cache_value = ""
            return ""

        try:
            stat = font_path.stat()
            cache_key = (str(font_path), stat.st_mtime_ns, stat.st_size)
            if cache_key == self._font_css_cache_key:
                return self._font_css_cache_value
            font_data = base64.b64encode(font_path.read_bytes()).decode("ascii")
        except OSError as exc:
            logger.warning(f"[谁艾特我] 读取自定义字体失败，使用默认字体: {exc}")
            self._font_css_cache_key = None
            self._font_css_cache_value = ""
            return ""

        css = (
            "@font-face { "
            "font-family: 'WhoAtMeCustomFont'; "
            f"src: url(\"data:{self._font_mime_type(font_path)};base64,{font_data}\") format('{self._font_format(font_path)}'); "
            "font-weight: 100 900; font-style: normal; font-display: block; "
            "}\n"
            ":root { --who-at-me-font-family: 'WhoAtMeCustomFont', 'Microsoft YaHei', 'Segoe UI', sans-serif; }\n"
            "body, body * { font-family: var(--who-at-me-font-family) !important; }"
        )
        self._font_css_cache_key = cache_key
        self._font_css_cache_value = css
        return css

    def _format_template(self, template: str, **kwargs: Any) -> str:
        try:
            return template.format(**kwargs)
        except Exception:
            return template

    def _max_records_per_target(self) -> int:
        return max(1, self._config_int("record", "max_records_per_target", default=MAX_RECORDS_PER_TARGET))

    def _query_context_max_messages(self) -> int:
        return max(1, self._config_int("record", "query_context_max_messages", default=MAX_CONTEXT_MESSAGES))

    def _max_messages_per_image(self) -> int:
        return max(1, self._config_int("record", "max_messages_per_image", default=MAX_MESSAGES_PER_IMAGE))

    def _max_query_pages(self) -> int:
        return max(0, self._config_int("record", "max_query_pages", default=0))

    def _max_reminder_pages(self) -> int:
        return max(0, self._config_int("reminder", "max_reminder_pages", default=0))

    def _query_reverse_order(self) -> bool:
        value = self._config_str("record", "query_sort_order", default="asc").strip().lower()
        return value in {"desc", "倒序", "reverse", "newest_first", "latest_first", "最新在上"}

    def _render_quality(self) -> int:
        return min(100, max(1, self._config_int("render", "image_quality", default=RENDER_IMAGE_QUALITY)))

    def _render_page_timeout_ms(self) -> int:
        return max(1000, self._config_int("render", "page_timeout_ms", default=RENDER_TIMEOUT_MS))

    def _render_task_timeout_sec(self) -> int:
        return max(3, self._config_int("render", "browser_timeout_seconds", default=RENDER_TASK_TIMEOUT_SEC))

    def _header_image_url(self) -> str:
        return self._config_str("render", "header_image_url", default=HEADER_IMAGE_URL)

    def _footer_image_url(self) -> str:
        return self._config_str("render", "footer_image_url", default=FOOTER_IMAGE_URL)

    def _reminder_away_seconds(self) -> int:
        return max(0, self._config_int("reminder", "away_minutes", default=10)) * 60

    def _max_pending_reminders(self) -> int:
        return max(1, self._config_int("reminder", "max_pending_reminders", default=MAX_PENDING_REMINDERS))

    def _max_reminder_context(self) -> int:
        return max(0, self._config_int("reminder", "max_context_messages", default=MAX_REMINDER_CONTEXT))

    def _global_group_allowed(self, event: AstrMessageEvent) -> bool:
        enabled_umos = self._global_enabled_group_umos()
        return not enabled_umos or self._event_umo(event) in enabled_umos

    def _global_enabled_group_umos(self) -> set[str]:
        return set(self._config_list("global", "enabled_group_umos"))

    def _reminder_enabled_group_umos(self) -> set[str]:
        return set(self._config_list("reminder", "enabled_group_umos"))

    def _event_umo(self, event: AstrMessageEvent) -> str:
        return str(getattr(event, "unified_msg_origin", "") or "")

    def _record_key(self, group_id: str, target: str) -> str:
        return f"records:{group_id}:{target}"

    def _context_key(self, group_id: str) -> str:
        return f"context:{group_id}"

    def _reminder_group_key(self, group_id: str) -> str:
        return f"reminder:group_enabled:{group_id}"

    def _reminder_user_key(self, group_id: str, user_id: str) -> str:
        return f"reminder:user_enabled:{group_id}:{user_id}"

    def _reminder_last_active_key(self, group_id: str, user_id: str) -> str:
        return f"reminder:last_active:{group_id}:{user_id}"

    def _reminder_pending_key(self, group_id: str, user_id: str) -> str:
        return f"reminder:pending:{group_id}:{user_id}"

    def _reminder_context_key(self, group_id: str) -> str:
        return f"reminder:context:{group_id}"

    def _stop_event(self, event: AstrMessageEvent) -> None:
        stopper = getattr(event, "stop_event", None)
        if callable(stopper):
            stopper()

    def _disable_llm(self, event: AstrMessageEvent) -> None:
        disabler = getattr(event, "should_call_llm", None)
        if callable(disabler):
            try:
                disabler(False)
            except Exception:
                pass
        try:
            event.call_llm = False
            event.is_wake = False
            event.is_at_or_wake_command = False
        except Exception:
            pass

    async def terminate(self):
        self.before_cache.clear()
        self.after_tasks.clear()
        self.reminder_after_tasks.clear()
