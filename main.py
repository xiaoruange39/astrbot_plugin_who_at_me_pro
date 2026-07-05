from __future__ import annotations

import asyncio
import html
import re
import tempfile
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star


QUERY_PATTERN = re.compile(r"^(谁(艾特|@|at)(我|他|她|它)|哪个逼(艾特|@|at)我)$", re.I)
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


HTML_TEMPLATE = r"""
<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <style>
    html, body {
      margin: 0;
      padding: 0;
      background: #333;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "Microsoft YaHei", sans-serif;
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
      top: 7px;
      left: 30px;
      font-size: 16px;
      font-weight: 700;
      color: #111;
    }
    .header-text {
      position: absolute;
      left: 56px;
      right: 56px;
      top: 45px;
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
                    <span class="tag-pill" style="background: {{ msg.tag_color }}">LV{{ msg.level }} {{ msg.role_text }}</span>
                    <span class="nickname">{{ msg.nickname }}</span>
                  </div>
                  <div class="msg-bubble {% if msg.is_at %}is-at{% endif %}">
                    <div class="msg-text">
                      {% if msg.is_at %}<span class="at-text">@{{ target_name }}</span>{% endif %}
                      {{ msg.message_html | safe }}
                    </div>
                    {% for image in msg.images %}
                      <img src="{{ image }}" class="msg-img" />
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
        self.before_cache: dict[str, list[dict[str, Any]]] = {}
        self.after_tasks: dict[str, list[dict[str, Any]]] = {}
        self.reminder_after_tasks: dict[str, list[dict[str, Any]]] = {}
        self.bot_name_cache: dict[str, str] = {}

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE, priority=1000)
    async def on_group_message(self, event: AstrMessageEvent):
        """记录群聊 @，并兼容原插件的自然语言命令。"""
        group_id = self._group_id(event)
        if not group_id:
            return

        text = self._normalize_command_text(self._message_text(event))
        mentions = self._mentions(event)
        if self._is_plugin_command(text):
            self._stop_event(event)
            self._disable_llm(event)
            command_result = await self._handle_command(event, group_id, text, mentions)
            sender_id = self._sender_id(event)
            if sender_id:
                await self._update_last_active(group_id, sender_id, self._timestamp(event))
            for result in command_result or []:
                yield result
            return

        sender_id = self._sender_id(event)
        if sender_id:
            await self._deliver_pending_reminders(event, group_id, sender_id)
        await self._record_mentions(event, group_id, mentions)
        if sender_id:
            await self._update_last_active(group_id, sender_id, self._timestamp(event))

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
        current = self._context_message(event) if context_on or reminder_context_on else None

        if context_on and current:
            await self._append_after_context(group_id, current)
        if reminder_context_on and current:
            await self._append_reminder_after_context(group_id, current)

        targets = [target for target in mentions if target != self._sender_id(event)]
        if targets:
            before = list(self.before_cache.get(group_id, [])) if context_on else []
            reminder_before_count = int(reminder_context.get("before", 1))
            reminder_before = (
                list(self.before_cache.get(group_id, []))[-reminder_before_count:]
                if reminder_context_on and reminder_before_count > 0
                else []
            )
            record = self._mention_record(event, targets)
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
        if not await self._reminder_group_enabled(group_id):
            return False
        if not await self._reminder_user_enabled(group_id, target):
            return False

        last_active = await self.get_kv_data(self._reminder_last_active_key(group_id, target), None)
        try:
            last_active_time = int(last_active)
        except (TypeError, ValueError):
            return False

        if int(record.get("time", time.time())) - last_active_time < self._reminder_away_seconds():
            return False

        pending_record = dict(record)
        pending_record["target"] = target
        if context_config.get("enabled"):
            pending_record["is_context"] = True
            pending_record["before"] = list(before)
            pending_record["after"] = []

        key = self._reminder_pending_key(group_id, target)
        pending = await self._get_pending_reminders(group_id, target)
        dedupe_key = self._record_identity(pending_record)
        if any(self._record_identity(item) == dedupe_key for item in pending):
            return False

        pending.append(pending_record)
        pending = pending[-self._max_pending_reminders():]
        await self.put_kv_data(key, pending)
        return True

    async def _deliver_pending_reminders(self, event: AstrMessageEvent, group_id: str, user_id: str) -> None:
        pending = await self._get_pending_reminders(group_id, user_id)
        if not pending:
            return

        await self.delete_kv_data(self._reminder_pending_key(group_id, user_id))
        pending.sort(key=lambda item: item.get("time", 0))
        target_name = self._target_name(event, user_id)
        reminder_text = self._format_template(
            self._config_str(
                "message",
                "reminder_text_template",
                default="{target_name}，你不在的时候有 {count} 条艾特记录~",
            ),
            target_name=target_name,
            count=len(pending),
        )
        await self._try_send(event, event.plain_result(reminder_text))

        blocks = self._build_blocks(pending, target_name, reverse=False)
        chunks = self._chunk_blocks(blocks)
        try:
            for idx, chunk in enumerate(chunks, start=1):
                image_path = await self._render_query_image(
                    {
                        "blocks": chunk,
                        "group_name": self._group_name(event, group_id),
                        "member_count": self._member_count(event),
                        "target_name": target_name,
                        "total_records": len(pending),
                        "context_enabled": any(item.get("is_context") for item in pending),
                        "now": datetime.now().strftime("%H:%M"),
                        "page_label": f"提醒 第 {idx} / {len(chunks)} 页" if len(chunks) > 1 else "艾特提醒",
                        "header_image": self._header_image_url(),
                        "footer_image": self._footer_image_url(),
                    }
                )
                if not await self._try_send(event, event.image_result(image_path)):
                    raise RuntimeError(f"发送提醒图片失败: {image_path}")
        except Exception as exc:
            logger.error(f"[谁艾特我] 渲染或发送提醒失败: {exc}")
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

        records.sort(key=lambda item: item.get("time", 0), reverse=True)
        target_name = self._target_name(event, target)
        blocks = self._build_blocks(records, target_name)
        chunks = self._chunk_blocks(blocks)
        if not chunks:
            return [event.plain_result(self._plain_summary(records, target_name))]

        waiting_text = self._format_template(
            self._config_str("message", "waiting_text_template", default="让{bot_name}看看谁艾特过你哦，稍等一下~"),
            bot_name=await self._bot_name(event, group_id),
        )
        if not await self._try_send(event, event.plain_result(waiting_text)):
            return [event.plain_result(waiting_text)]

        for idx, chunk in enumerate(chunks, start=1):
            try:
                image_path = await self._render_query_image(
                    {
                        "blocks": chunk,
                        "group_name": self._group_name(event, group_id),
                        "member_count": self._member_count(event),
                        "target_name": target_name,
                        "total_records": len(records),
                        "context_enabled": any(item.get("is_context") for item in records),
                        "now": datetime.now().strftime("%H:%M"),
                        "page_label": f"第 {idx} / {len(chunks)} 页" if len(chunks) > 1 else "",
                        "header_image": self._header_image_url(),
                        "footer_image": self._footer_image_url(),
                    }
                )
                if not await self._try_send(event, event.image_result(image_path)):
                    raise RuntimeError(f"发送图片失败: {image_path}")
            except Exception as exc:
                logger.error(f"[谁艾特我] 渲染或发送图片失败: {exc}")
                await self._try_send(event, event.plain_result(self._plain_summary(records, target_name)))
                break

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

    async def _reminder_group_enabled(self, group_id: str) -> bool:
        value = await self.get_kv_data(self._reminder_group_key(group_id), None)
        return self._config_bool("reminder", "default_group_enabled", default=False) if value is None else bool(value)

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
        group_status = "开启" if await self._reminder_group_enabled(group_id) else "关闭"
        user_status = "开启" if await self._reminder_user_enabled(group_id, sender_id) else "关闭"
        context_status = "开启" if context_config.get("enabled") else "关闭"
        return (
            "艾特提醒状态：\n"
            f"本群提醒：{group_status}\n"
            f"你的提醒：{user_status}\n"
            f"提醒上下文：{context_status}（前 {context_config.get('before', 0)} / 后 {context_config.get('after', 0)}）\n"
            f"离开判定：{self._reminder_away_seconds() // 60} 分钟未发言"
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
        role = str(data.get("role") or "member")
        role_text = {"owner": "群主", "admin": "管理员"}.get(role, "群员")
        user_id = str(data.get("user_id") or data.get("User") or "")
        level = data.get("level") or 1
        tag_color = "#b4b4b6"
        if role == "owner":
            tag_color = "#f6c751"
        elif role == "admin":
            tag_color = "#57d6c5"
        avatar = f"http://q1.qlogo.cn/g?b=qq&nk={user_id}&s=100" if user_id.isdigit() else ""
        return {
            "user_id": user_id,
            "nickname": nickname,
            "initial": self._initial(nickname),
            "avatar": avatar,
            "message": message,
            "message_html": html.escape(message).replace("\n", "<br>") or "&nbsp;",
            "images": data.get("images") or data.get("image") or [],
            "time": data.get("time", 0),
            "time_text": self._time_text(data.get("time", 0)),
            "is_at": is_at,
            "target_name": target_name,
            "role_class": role if role in {"owner", "admin"} else "",
            "role_text": role_text,
            "level": level,
            "tag_color": tag_color,
        }

    def _dedupe_messages(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        seen: dict[tuple[Any, ...], dict[str, Any]] = {}
        for msg in messages:
            key = (
                msg.get("user_id"),
                msg.get("time"),
                msg.get("message"),
                tuple(msg.get("images") or []),
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

    def _dedupe_records(self, records: list[dict[str, Any]]) -> list[dict[str, Any]]:
        deduped = {}
        for record in records:
            key = (
                record.get("user_id") or record.get("User"),
                record.get("time"),
                record.get("message"),
                tuple(record.get("images") or record.get("image") or []),
            )
            deduped[key] = record
        return list(deduped.values())

    def _mention_record(self, event: AstrMessageEvent, mentions: list[str] | None = None) -> dict[str, Any]:
        sender_id = self._sender_id(event)
        sender = getattr(event.message_obj, "sender", None)
        role = str(getattr(sender, "role", "") or self._raw_sender_value(event, "role") or "member")
        return {
            "user_id": sender_id,
            "message": self._message_text_for_record(event, mentions or []),
            "images": self._images(event),
            "name": self._sender_name(event),
            "role": role,
            "time": self._timestamp(event),
            "message_id": str(getattr(event.message_obj, "message_id", "") or ""),
        }

    def _context_message(self, event: AstrMessageEvent) -> dict[str, Any]:
        record = self._mention_record(event)
        return {
            "user_id": record["user_id"],
            "message": record["message"],
            "images": record["images"],
            "name": record["name"],
            "role": record["role"],
            "time": record["time"],
        }

    def _query_target(self, event: AstrMessageEvent, text: str, mentions: list[str]) -> str:
        if "我" in text:
            return self._sender_id(event)
        return mentions[0] if mentions else ""

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
        urls = []
        raw_segments = self._raw_message_segments(event)
        if raw_segments:
            for segment in raw_segments:
                if str(segment.get("type", "")).lower() != "image":
                    continue
                data = segment.get("data") or {}
                value = data.get("url") or data.get("file") or data.get("path")
                if value:
                    urls.append(str(value))
            return urls

        for item in self._message_chain(event):
            if self._is_reference_segment(item):
                continue
            if item.__class__.__name__.lower() != "image":
                continue
            value = self._first_attr(item, ["url", "file", "path"])
            if value:
                urls.append(str(value))
        return urls

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
            texts = []
            for segment in raw_segments:
                seg_type = str(segment.get("type", "")).lower()
                if seg_type in {"text", "plain"}:
                    data = segment.get("data") or {}
                    texts.append(str(data.get("text") or data.get("content") or ""))
            text = "".join(texts).strip()
        else:
            text = self._message_text(event)

        return self._strip_at_display(text, mentions)

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
        cls_name = item.__class__.__name__.lower()
        if any(token in cls_name for token in ("reply", "quote", "source", "reference")):
            return True
        seg_type = str(getattr(item, "type", "") or getattr(item, "seg_type", "") or "").lower()
        return seg_type in {"reply", "quote", "source", "reference"}

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

    def _target_name(self, event: AstrMessageEvent, target: str) -> str:
        if target == self._sender_id(event):
            return self._sender_name(event)
        return "全体成员" if target == ALL_TARGET else str(target)

    def _group_name(self, event: AstrMessageEvent, group_id: str) -> str:
        group = getattr(event.message_obj, "group", None)
        if group and getattr(group, "group_name", None):
            return str(group.group_name)
        raw = getattr(event.message_obj, "raw_message", None)
        if isinstance(raw, dict):
            for key in ("group_name", "groupName"):
                if raw.get(key):
                    return str(raw[key])
        return str(group_id)

    def _member_count(self, event: AstrMessageEvent) -> int:
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
        if not isinstance(value, dict):
            return ""
        candidates = [value]
        data = value.get("data")
        if isinstance(data, dict):
            candidates.insert(0, data)
        for mapping in candidates:
            for key in keys:
                name = mapping.get(key)
                if name and not self._looks_like_numeric_id(name):
                    return str(name)
        return ""

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
            return datetime.fromtimestamp(int(value)).strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            return ""

    def _initial(self, name: str) -> str:
        clean = name.strip()
        return clean[:1].upper() if clean else "?"

    def _looks_like_numeric_id(self, value: Any) -> bool:
        text = str(value).strip()
        return text.isdigit() and len(text) >= 5

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

    def _record_identity(self, record: dict[str, Any]) -> tuple[Any, ...]:
        return (
            record.get("message_id"),
            record.get("user_id") or record.get("User"),
            record.get("time"),
            record.get("message"),
            tuple(record.get("images") or record.get("image") or []),
        )

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
