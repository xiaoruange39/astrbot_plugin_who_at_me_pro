from __future__ import annotations

import asyncio
import html
import re
import time
from datetime import datetime
from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star

try:
    from .modules.config import ConfigMixin
    from .modules.constants import *
    from .modules.data import DataMixin
    from .modules.message import MessageMixin
    from .modules.page_api import PageApiMixin
    from .modules.page_settings import PageSettingsMixin
    from .modules.rendering import RenderingMixin
except ImportError:
    from modules.config import ConfigMixin
    from modules.constants import *
    from modules.data import DataMixin
    from modules.message import MessageMixin
    from modules.page_api import PageApiMixin
    from modules.page_settings import PageSettingsMixin
    from modules.rendering import RenderingMixin


GROUP_NOTICE_EVENT_TYPE = getattr(filter.EventMessageType, "GROUP_NOTICE", filter.EventMessageType.GROUP_MESSAGE)


class WhoAtMePlugin(ConfigMixin, RenderingMixin, DataMixin, MessageMixin, PageApiMixin, PageSettingsMixin, Star):
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
        self._receive_order = 0
        self._kv_locks: dict[str, asyncio.Lock] = {}
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
                context.register_web_api(
                    f"/{prefix}/images/upload",
                    self.page_image_upload,
                    ["POST"],
                    "谁艾特我Pro上传渲染图片",
                )
                context.register_web_api(
                    f"/{prefix}/images/reset",
                    self.page_image_reset,
                    ["POST"],
                    "谁艾特我Pro恢复默认渲染图片",
                )
        except Exception as exc:
            logger.warning(f"[谁艾特我] 注册 Page API 失败: {exc}")

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE, priority=10000)
    async def mark_group_activity_early(self, event: AstrMessageEvent):
        try:
            setattr(event, "_who_at_me_activity_handled", True)
        except Exception:
            pass
        try:
            await self._mark_group_activity(event)
        except Exception as exc:
            try:
                setattr(event, "_who_at_me_activity_handled", False)
            except Exception:
                pass
            logger.error(f"[who_at_me] early activity update failed: {exc}")

    @filter.event_message_type(GROUP_NOTICE_EVENT_TYPE, priority=10001)
    async def on_group_notice(self, event: AstrMessageEvent):
        group_id = self._group_id(event)
        if group_id:
            await self._handle_recall_event(event, group_id)

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE, priority=1000)
    async def on_group_message(self, event: AstrMessageEvent):
        """记录群聊 @，并兼容原插件的自然语言命令。"""
        group_id = self._group_id(event)
        if not group_id:
            return
        if await self._handle_recall_event(event, group_id):
            return

        text = self._normalize_command_text(self._message_text(event))
        is_plugin_command = self._is_plugin_command(text)
        activity_handled = bool(getattr(event, "_who_at_me_activity_handled", False))
        if not self._global_group_allowed(event):
            if is_plugin_command:
                self._stop_event(event)
                self._disable_llm(event)
            return

        sender_id = self._sender_id(event)
        self_id = self._self_id(event)
        if sender_id and self_id and sender_id == self_id:
            if not activity_handled:
                await self._delete_pending_reminders(group_id, self_id)
            return
        if sender_id:
            await self._remember_sender_member(event, group_id, sender_id)

        mentions = self._mentions(event)
        if is_plugin_command:
            self._stop_event(event)
            self._disable_llm(event)
            if sender_id:
                if not activity_handled:
                    await self._delete_pending_reminders(group_id, sender_id)
                await self._record_context_message(event, group_id, mentions, append_to_cache=True)
                if not activity_handled:
                    await self._update_last_active(group_id, sender_id, self._timestamp(event))
            command_result = await self._handle_command(event, group_id, text, mentions)
            for result in command_result or []:
                yield result
            return

        if sender_id and not activity_handled:
            await self._deliver_pending_reminders(event, group_id, sender_id)
        await self._record_mentions(event, group_id, mentions)
        if sender_id and not activity_handled:
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
            await self._delete_pending_reminders(group_id, self_id)
            return

        text = self._normalize_command_text(self._message_text(event))
        if self._is_plugin_command(text):
            await self._delete_pending_reminders(group_id, sender_id)
            await self._update_last_active(group_id, sender_id, self._timestamp(event))
            return

        await self._deliver_pending_reminders(event, group_id, sender_id)
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
            if not self._is_bot_admin(event):
                return [event.plain_result("只有 AstrBot 管理员或主人可以清除全部艾特数据")]
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

    async def _delete_pending_reminders(self, group_id: str, user_id: str) -> None:
        await self._delete_pending_key(self._reminder_pending_key(group_id, user_id))

    async def _delete_record_key(self, key: str) -> bool:
        async with self._kv_lock(key):
            records = await self.get_kv_data(key, [])
            has_records = bool(records) if isinstance(records, list) else False
            self._drop_records_image_cache(records, delete_files=True)
            await self.delete_kv_data(key)
        await self._forget_index_key(key)
        return has_records

    async def _delete_pending_key(self, key: str) -> bool:
        async with self._kv_lock(key):
            pending = await self.get_kv_data(key, [])
            has_pending = bool(pending) if isinstance(pending, list) else False
            self._drop_records_image_cache(pending, delete_files=True)
            await self.delete_kv_data(key)
        await self._forget_pending_key(key)
        return has_pending

    async def _restore_pending_reminders(self, group_id: str, user_id: str, records: list[dict[str, Any]]) -> None:
        if not records:
            return
        key = self._reminder_pending_key(group_id, user_id)
        async with self._kv_lock(key):
            current = await self._get_pending_reminders(group_id, user_id)
            merged = self._dedupe_records([*current, *records])
            merged.sort(key=self._record_sort_key)
            await self.put_kv_data(key, self._trim_pending_reminders(merged))
        await self._remember_pending_key(key)

    def _trim_pending_reminders(self, records: list[dict[str, Any]]) -> list[dict[str, Any]]:
        limit = max(1, self._max_pending_reminders())
        if len(records) <= limit:
            return records
        dropped = records[:-limit]
        self._drop_records_image_cache(dropped, delete_files=True)
        return records[-limit:]

    async def _take_ready_pending_reminders(
        self,
        group_id: str,
        user_id: str,
        now_time: int,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        key = self._reminder_pending_key(group_id, user_id)
        async with self._kv_lock(key):
            pending = await self._get_pending_reminders(group_id, user_id)
            if not pending:
                return [], []
            pending = self._dedupe_records(pending)
            away_seconds = self._reminder_away_seconds()
            ready = [
                record
                for record in pending
                if away_seconds <= 0 or now_time - self._record_time(record) >= away_seconds
            ]
            if not ready:
                self._drop_records_image_cache(pending, delete_files=True)
            await self.delete_kv_data(key)
        await self._forget_pending_key(key)
        return ready, pending if ready else []

    async def _handle_recall_event(self, event: AstrMessageEvent, group_id: str) -> bool:
        message_id = self._recall_message_id(event)
        if not message_id:
            return False
        await self._remove_recalled_message(group_id, message_id)
        return True

    async def _record_mentions(
        self,
        event: AstrMessageEvent,
        group_id: str,
        mentions: list[str],
    ) -> None:
        context_state = await self._record_context_message(event, group_id, mentions, append_to_cache=False)
        context_on = bool(context_state.get("context_on"))
        reminder_context = context_state["reminder_context"]
        reminder_context_on = bool(context_state.get("reminder_context_on"))
        sender_info = context_state["sender_info"]
        quote = context_state["quote"]

        current = context_state["current"]

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
            record = await self._mention_record(event, group_id, targets, sender_info, quote)
            if context_on:
                record["is_context"] = True
                record["before"] = before
                record["after"] = []

            for target in targets:
                target_record = dict(record)
                target_record["target"] = target
                await self._append_record(group_id, target, target_record)
                queued = await self._queue_reminder_if_needed(
                    event,
                    group_id,
                    target,
                    target_record,
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
                    tasks.append({"target": target, "time": target_record["time"], "count": 0})

        if current:
            await self._append_before_context_cache(group_id, current)

    async def _record_context_message(
        self,
        event: AstrMessageEvent,
        group_id: str,
        mentions: list[str],
        *,
        append_to_cache: bool,
    ) -> dict[str, Any]:
        context_on = await self._context_enabled(group_id)
        reminder_context = await self._reminder_context_config(group_id)
        reminder_context_on = bool(reminder_context.get("enabled"))
        needs_context = context_on or reminder_context_on
        sender_info = (
            await self._member_info(event, group_id, self._sender_id(event))
            if mentions or needs_context
            else {}
        )
        quote = await self._quote(event) if mentions or needs_context else None
        current = await self._context_message(event, group_id, mentions, sender_info, quote) if needs_context else None
        if current:
            current = await self._cache_record_images(current)

        if context_on and current:
            await self._append_after_context(group_id, current)
        if reminder_context_on and current:
            await self._append_reminder_after_context(group_id, current)
        if append_to_cache and current:
            await self._append_before_context_cache(group_id, current)

        return {
            "context_on": context_on,
            "reminder_context": reminder_context,
            "reminder_context_on": reminder_context_on,
            "sender_info": sender_info,
            "quote": quote,
            "current": current,
        }

    async def _append_before_context_cache(self, group_id: str, current: dict[str, Any]) -> None:
        cache = self.before_cache.setdefault(group_id, [])
        cache.append(current)
        del cache[:-max(self._query_context_max_messages(), self._max_reminder_context())]

    async def _append_after_context(self, group_id: str, current: dict[str, Any]) -> None:
        async with self._kv_lock(f"runtime:after-context:{group_id}"):
            await self._append_after_context_locked(group_id, current)

    async def _append_after_context_locked(self, group_id: str, current: dict[str, Any]) -> None:
        tasks = self.after_tasks.get(group_id, [])
        for idx in range(len(tasks) - 1, -1, -1):
            task = tasks[idx]
            key = self._record_key(group_id, task["target"])
            async with self._kv_lock(key):
                records = await self.get_kv_data(key, [])
                if not isinstance(records, list):
                    records = []
                changed = False
                for record in records:
                    if record.get("time") == task["time"]:
                        record.setdefault("after", []).append(current)
                        changed = True
                        break
                if changed:
                    await self.put_kv_data(key, records)

            task["count"] += 1
            if task["count"] >= self._query_context_max_messages():
                tasks.pop(idx)

    async def _append_reminder_after_context(self, group_id: str, current: dict[str, Any]) -> None:
        async with self._kv_lock(f"runtime:after-context:{group_id}"):
            await self._append_reminder_after_context_locked(group_id, current)

    async def _append_reminder_after_context_locked(self, group_id: str, current: dict[str, Any]) -> None:
        tasks = self.reminder_after_tasks.get(group_id, [])
        for idx in range(len(tasks) - 1, -1, -1):
            task = tasks[idx]
            key = self._reminder_pending_key(group_id, task["target"])
            async with self._kv_lock(key):
                pending = await self._get_pending_reminders(group_id, task["target"])
                changed = False
                for record in pending:
                    if record.get("time") == task["time"]:
                        record.setdefault("after", []).append(current)
                        changed = True
                        break
                if changed:
                    await self.put_kv_data(key, pending)

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

        pending_record = dict(record)
        pending_record["target"] = target
        if context_config.get("enabled"):
            pending_record["is_context"] = True
            pending_record["before"] = list(before)
            pending_record["after"] = []
        pending_record = await self._cache_record_images(pending_record)

        key = self._reminder_pending_key(group_id, target)
        duplicate = False
        async with self._kv_lock(key):
            pending = await self._get_pending_reminders(group_id, target)
            if any(self._records_are_duplicate(item, pending_record) for item in pending):
                duplicate = True
            else:
                pending.append(pending_record)
                pending = self._trim_pending_reminders(pending)
                await self.put_kv_data(key, pending)

        if duplicate:
            self._drop_record_image_cache(pending_record, delete_files=True)
        await self._remember_pending_key(key)
        return not duplicate

    async def _deliver_pending_reminders(self, event: AstrMessageEvent, group_id: str, user_id: str) -> None:
        if not await self._reminder_group_enabled(event, group_id):
            return
        if not await self._reminder_user_enabled(group_id, user_id):
            await self._delete_pending_reminders(group_id, user_id)
            return

        pending, original_pending = await self._take_ready_pending_reminders(
            group_id,
            user_id,
            self._timestamp(event),
        )
        if not pending:
            return

        pending.sort(key=self._record_sort_key)
        target_name = await self._target_name(event, group_id, user_id)
        pending = await self._resolve_record_pokes(event, group_id, pending)
        reminder_text = self._format_template(
            self._config_str(
                "message",
                "reminder_text_template",
                default="{target_name}，你不在的时候有 {count} 条艾特记录~",
            ),
            target_name=target_name,
            count=len(pending),
        ).strip()
        blocks = self._build_blocks(pending, target_name, user_id, reverse=False)
        chunks = self._chunk_blocks(blocks)
        chunks = self._limit_chunks(chunks, self._max_reminder_pages())
        image_paths: list[str] = []
        sent = False
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

            if reminder_text:
                if not await self._try_send_text_images(event, reminder_text, image_paths):
                    if not await self._try_send(event, event.plain_result(reminder_text)):
                        raise RuntimeError("failed to send reminder text")
                    for image_path in image_paths:
                        if not await self._try_send(event, event.image_result(image_path)):
                            raise RuntimeError(f"发送提醒图片失败: {image_path}")
            elif not await self._try_send_images(event, image_paths):
                for image_path in image_paths:
                    if not await self._try_send(event, event.image_result(image_path)):
                        raise RuntimeError(f"发送提醒图片失败: {image_path}")
            sent = True
            self._drop_records_image_cache(original_pending, delete_files=True)
        except Exception as exc:
            logger.error(f"[谁艾特我] 渲染或发送提醒失败: {exc}")
            if not sent:
                await self._restore_pending_reminders(group_id, user_id, original_pending)
            if reminder_text and not image_paths:
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

        target_name = await self._target_name(event, group_id, target)
        total_records = len(records)
        query_reverse = self._query_reverse_order()
        records = self._select_query_records(records, target_name, target, reverse=query_reverse)
        records = await self._resolve_record_pokes(event, group_id, records)
        blocks = self._build_blocks(records, target_name, target, reverse=query_reverse)
        chunks = self._chunk_blocks(blocks)
        chunks = self._limit_chunks(chunks, self._max_query_pages())
        self._log_query_image_diagnostics(group_id, target, records, page_count=len(chunks))
        if not chunks:
            return [event.plain_result(self._plain_summary(records, target_name))]

        is_self_query = target == self._sender_id(event)
        target_pronoun = "你" if is_self_query else "ta"
        waiting_template = self._config_str("message", "waiting_text_template", default="让{bot_name}看看谁艾特过你哦，稍等一下~")
        if waiting_template.strip():
            if not is_self_query and waiting_template == "让{bot_name}看看谁艾特过你哦，稍等一下~":
                waiting_template = "让{bot_name}看看谁艾特过ta哦，稍等一下~"
            waiting_text = self._format_template(
                waiting_template,
                bot_name=await self._bot_name(event, group_id),
                target_name=target_name,
                target_pronoun=target_pronoun,
            ).strip()
            if waiting_text and not await self._try_send(event, event.plain_result(waiting_text)):
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
                        "total_records": total_records,
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
        sender_id = self._sender_id(event)
        key = self._record_key(group_id, sender_id)
        pending_key = self._reminder_pending_key(group_id, sender_id)
        removed_records = await self._delete_record_key(key)
        removed_pending = await self._delete_pending_key(pending_key)
        if not removed_records and not removed_pending:
            return event.plain_result("目前数据库没有你的at数据,无法清除")

        return event.plain_result("已成功清除")

    async def _clear_all(self, event: AstrMessageEvent) -> Any:
        keys = await self.get_kv_data(INDEX_KEY, [])
        if not isinstance(keys, list):
            keys = []
        pending_keys = set(await self._pending_index_keys())
        for key in keys:
            if not isinstance(key, str):
                continue
            await self._delete_record_key(key)
            if key.startswith("records:"):
                body = key[len("records:") :]
                if ":" in body:
                    group_id, target = body.split(":", 1)
                    pending_keys.add(self._reminder_pending_key(group_id, target))
        await self.delete_kv_data(INDEX_KEY)

        context_keys = await self.get_kv_data(CONTEXT_INDEX_KEY, [])
        if not isinstance(context_keys, list):
            context_keys = []
        for key in context_keys:
            if isinstance(key, str):
                await self.delete_kv_data(key)
        await self.delete_kv_data(CONTEXT_INDEX_KEY)

        for key in pending_keys:
            await self._delete_pending_key(key)
        await self.delete_kv_data(REMINDER_PENDING_INDEX_KEY)

        self.before_cache.clear()
        self.after_tasks.clear()
        self.reminder_after_tasks.clear()
        return event.plain_result("已成功清除全部艾特数据")

    def _build_blocks(
        self,
        records: list[dict[str, Any]],
        target_name: str,
        target_id: str = "",
        reverse: bool = True,
    ) -> list[dict[str, Any]]:
        messages = []
        for record in records:
            if record.get("is_context"):
                for idx, ctx in enumerate(record.get("before") or []):
                    msg = self._view_message(ctx, False, target_name, target_id)
                    msg["sort_phase"] = 0
                    msg["sort_index"] = idx
                    msg["sort_time"] = float(ctx.get("time", 0)) - 0.01 + idx * 0.001
                    messages.append(msg)

            main = self._view_message(record, True, target_name, target_id)
            main["sort_phase"] = 1
            main["sort_index"] = 0
            main["sort_time"] = float(record.get("time", 0))
            messages.append(main)

            if record.get("is_context"):
                for idx, ctx in enumerate(record.get("after") or []):
                    msg = self._view_message(ctx, False, target_name, target_id)
                    msg["sort_phase"] = 2
                    msg["sort_index"] = idx
                    msg["sort_time"] = float(ctx.get("time", 0)) + 0.001 + idx * 0.001
                    messages.append(msg)

        messages = self._dedupe_timeline_messages(messages, reverse=reverse)
        messages = [message for message in messages if self._timeline_message_visible(message)]
        return self._split_timeline_blocks(messages)

    def _view_message(self, data: dict[str, Any], is_at: bool, target_name: str, target_id: str = "") -> dict[str, Any]:
        user_id = str(data.get("user_id") or data.get("User") or "")
        nickname = self._display_name(data.get("name"), data.get("nickname"), user_id, default="用户")
        poke = data.get("poke") if isinstance(data.get("poke"), dict) else None
        message = str(data.get("message") or "")
        images = self._record_renderable_images(data)
        media = self._record_renderable_media(data)
        media_covers = {str(item.get("cover") or "") for item in media if isinstance(item, dict) and item.get("cover")}
        if media_covers:
            images = [image for image in images if image not in media_covers]
        if media and self._is_media_summary_message(message):
            message = ""
        at_targets = [data.get("target"), data.get("at"), data.get("AtQQ")]
        if isinstance(data.get("at_targets"), list):
            at_targets.extend(data["at_targets"])
        at_candidates = [target_name, target_id, *at_targets]
        inferred_at = bool(target_id and str(target_id) in {str(item) for item in at_targets if item is not None})
        if not inferred_at and (images or media) and self._starts_with_at_display(message):
            inferred_at = any(
                re.search(rf"^[@\uff20]\s*{re.escape(str(item).strip())}(?:\([0-9]+\))?(?=\s|$)", message.lstrip())
                for item in (target_name, target_id)
                if str(item or "").strip()
            )
        render_is_at = bool(is_at or inferred_at)
        if render_is_at:
            message = self._strip_at_display(message, at_candidates)
        at_after_image = bool(data.get("at_after_image") and images)
        message_after_images = str(data.get("message_after_images") or "").strip()
        if message_after_images and images and not at_after_image:
            if message.strip() == message_after_images:
                message = ""
            elif message.strip().endswith(message_after_images):
                message = message.strip()[: -len(message_after_images)].strip()
        role = str(data.get("role") or "member").lower()
        role_text = {"owner": "群主", "admin": "管理员", "administrator": "管理员"}.get(role, "群员")
        title = str(data.get("title") or "")
        member_title = str(data.get("member_title") or title or "")
        level = self._level_text(data.get("level"))
        identity_text = member_title or role_text
        tag_parts = [f"LV{level}"] if level else []
        if identity_text:
            tag_parts.append(identity_text)
        tag_text = " ".join(tag_parts)
        tag_color = "#b4b4b6"
        tag_text_color = "#fff"
        if member_title and role != "owner":
            tag_color = "#c77df3"
        elif role == "owner":
            tag_color = "#f6c751"
        elif role in {"admin", "administrator"}:
            tag_color = "#45d3c9"
        avatar = f"http://q1.qlogo.cn/g?b=qq&nk={user_id}&s=100" if user_id.isdigit() else ""
        return {
            "user_id": user_id,
            "nickname": nickname,
            "member_title": member_title,
            "initial": self._initial(nickname),
            "avatar": avatar,
            "message_id": str(data.get("message_id") or ""),
            "order": self._record_order(data),
            "received_order": data.get("received_order"),
            "message": message,
            "has_message": bool(message.strip()),
            "message_html": html.escape(message).replace("\n", "<br>"),
            "has_message_after_images": bool(message_after_images),
            "message_after_images_html": html.escape(message_after_images).replace("\n", "<br>"),
            "images": images,
            "media": media,
            "quote": self._view_quote(data.get("quote")),
            "time": data.get("time", 0),
            "time_text": self._time_text(data.get("time", 0)),
            "is_at": render_is_at,
            "at_after_image": at_after_image,
            "target_name": target_name,
            "role_class": role if role in {"owner", "admin", "administrator"} else "",
            "role_text": role_text,
            "level": level,
            "tag_text": tag_text,
            "tag_color": tag_color,
            "tag_text_color": tag_text_color,
            "is_poke": bool(poke),
            "poke_actor": self._display_name((poke or {}).get("actor"), nickname),
            "poke_target": self._display_name((poke or {}).get("target"), default="对方"),
            "poke_action": str((poke or {}).get("action") or "👋 拍了拍"),
            "poke_suffix": str((poke or {}).get("suffix") or ""),
        }

    def _view_quote(self, quote: Any) -> dict[str, Any] | None:
        if not isinstance(quote, dict):
            return None
        message = str(quote.get("message") or "").strip()
        images = self._record_renderable_images(quote)
        if not message and not images:
            return None
        nickname = self._display_name(quote.get("name"), quote.get("nickname"), quote.get("user_id"), default="引用消息")
        return {
            "nickname": nickname,
            "message": message,
            "message_html": html.escape(message).replace("\n", "<br>"),
            "images": images[:3],
            "time_text": self._time_text(quote.get("time", 0)),
        }

    def _display_name(self, *values: Any, default: str = "用户") -> str:
        for value in values:
            text = str(value or "").strip()
            if text and text.lower() not in {"none", "null", "undefined"}:
                return text
        return default

    def _dedupe_timeline_messages(self, messages: list[dict[str, Any]], reverse: bool) -> list[dict[str, Any]]:
        seen: dict[tuple[Any, ...], dict[str, Any]] = {}
        order: list[tuple[Any, ...]] = []
        for msg in messages:
            key = self._timeline_message_key(msg)
            existing = seen.get(key)
            if existing is None:
                seen[key] = msg
                order.append(key)
                continue
            seen[key] = self._merge_timeline_message(existing, msg)

        result = self._merge_complementary_timeline_messages([seen[key] for key in order])
        result.sort(key=self._message_sort_key, reverse=reverse)
        return result

    def _merge_complementary_timeline_messages(
        self,
        messages: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        candidates: dict[tuple[Any, ...], list[int]] = {}
        for msg in messages:
            loose_key = self._timeline_loose_message_key(msg)
            merged = False
            if loose_key:
                for index in reversed(candidates.get(loose_key, [])):
                    if not self._timeline_messages_are_complementary(result[index], msg):
                        continue
                    result[index] = self._merge_timeline_message(result[index], msg)
                    merged = True
                    break
            if merged:
                continue
            if loose_key:
                candidates.setdefault(loose_key, []).append(len(result))
            result.append(msg)
        return result

    def _timeline_messages_are_complementary(
        self,
        left: dict[str, Any],
        right: dict[str, Any],
    ) -> bool:
        left_payload = self._timeline_payload_tokens(left)
        right_payload = self._timeline_payload_tokens(right)
        if left_payload == right_payload:
            return False
        return left_payload < right_payload or right_payload < left_payload

    def _timeline_payload_tokens(self, msg: dict[str, Any]) -> frozenset[tuple[Any, ...]]:
        tokens: set[tuple[Any, ...]] = {
            ("image", str(image))
            for image in (msg.get("images") or [])
            if str(image or "").strip()
        }
        for item in msg.get("media") or []:
            if not isinstance(item, dict):
                continue
            tokens.add(
                (
                    "media",
                    str(item.get("type") or ""),
                    str(item.get("source") or ""),
                    str(item.get("cover") or ""),
                    str(item.get("title") or ""),
                )
            )
        after_image_text = str(msg.get("message_after_images_html") or "").strip()
        if after_image_text:
            tokens.add(("message_after_images", after_image_text))
        return frozenset(tokens)

    def _timeline_message_visible(self, msg: dict[str, Any]) -> bool:
        return bool(
            msg.get("has_message")
            or msg.get("images")
            or msg.get("media")
            or msg.get("quote")
            or msg.get("is_poke")
            or msg.get("is_at")
            or msg.get("has_message_after_images")
        )

    def _timeline_message_key(self, msg: dict[str, Any]) -> tuple[Any, ...]:
        message_id = str(msg.get("message_id") or "")
        if message_id:
            return ("message_id", message_id)
        order = self._record_order(msg)
        received_order = self._record_received_order(msg)
        if order is not None or received_order is not None:
            return (
                "order",
                str(msg.get("user_id") or ""),
                self._record_time(msg),
                order if order is not None else -1,
                received_order if received_order is not None else -1,
            )
        return self._message_key(msg)

    def _timeline_loose_message_key(self, msg: dict[str, Any]) -> tuple[Any, ...]:
        if not (msg.get("is_at") or self._starts_with_at_display(msg.get("message"))):
            return ()
        message = self._strip_at_display(
            str(msg.get("message") or ""),
            [msg.get("target_name"), msg.get("target"), msg.get("at"), msg.get("AtQQ")],
        )
        return (
            "at_message",
            str(msg.get("user_id") or ""),
            self._record_time(msg),
            self._normalize_record_text(message),
            str(msg.get("target_name") or ""),
            self._record_quote_key(msg),
        )

    def _starts_with_at_display(self, value: Any) -> bool:
        return str(value or "").lstrip().startswith(("@", "＠"))

    def _merge_timeline_message(self, left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
        use_right = bool(right.get("is_at") and not left.get("is_at"))
        base = dict(right if use_right else left)
        other = left if use_right else right

        base["is_at"] = bool(left.get("is_at") or right.get("is_at"))
        if not str(base.get("message") or "").strip() and str(other.get("message") or "").strip():
            message = str(other.get("message") or "")
            if base.get("is_at"):
                message = self._strip_at_display(
                    message,
                    [
                        base.get("target_name"),
                        other.get("target_name"),
                        base.get("target"),
                        other.get("target"),
                        base.get("at"),
                        other.get("at"),
                        base.get("AtQQ"),
                        other.get("AtQQ"),
                    ],
                )
            base["message"] = message
            base["message_html"] = html.escape(message).replace("\n", "<br>")
            base["has_message"] = bool(message.strip())
        base["images"] = self._unique_strings([*(base.get("images") or []), *(other.get("images") or [])])
        base["media"] = self._unique_media([*(base.get("media") or []), *(other.get("media") or [])])
        base["at_after_image"] = bool(base.get("at_after_image") or other.get("at_after_image"))
        if not base.get("has_message_after_images") and other.get("has_message_after_images"):
            base["has_message_after_images"] = True
            base["message_after_images_html"] = other.get("message_after_images_html") or ""

        if not base.get("quote") and other.get("quote"):
            base["quote"] = other.get("quote")
        elif isinstance(base.get("quote"), dict) and isinstance(other.get("quote"), dict):
            quote = dict(base["quote"])
            quote["images"] = self._unique_strings([*(quote.get("images") or []), *(other["quote"].get("images") or [])])
            if not quote.get("message_html") and other["quote"].get("message_html"):
                quote["message"] = other["quote"].get("message")
                quote["message_html"] = other["quote"].get("message_html")
            base["quote"] = quote
        return base

    def _is_media_summary_message(self, message: str) -> bool:
        text = re.sub(r"\s+", " ", str(message or "")).strip()
        return text in {"[视频]", "[语音]", "[文件]", "[表情]", "[卡片消息]"} or text.startswith("[文件] ")

    def _split_timeline_blocks(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        blocks: list[dict[str, Any]] = []
        current: list[dict[str, Any]] = []
        current_has_at = False

        for msg in messages:
            if msg.get("is_at") and current and current_has_at:
                blocks.append({"msgs": current})
                current = []
                current_has_at = False
            current.append(msg)
            current_has_at = current_has_at or bool(msg.get("is_at"))

        if current:
            blocks.append({"msgs": current})
        return blocks

    def _message_key(self, msg: dict[str, Any]) -> tuple[Any, ...]:
        message_id = str(msg.get("message_id") or "")
        if message_id:
            return ("message_id", message_id)
        return (
            str(msg.get("user_id") or ""),
            self._record_time(msg),
            self._record_message_key(msg),
            self._record_images_key(msg),
            self._record_media_key(msg),
            self._record_quote_key(msg),
        )

    def _message_sort_key(self, msg: dict[str, Any]) -> tuple[float, int, int, int, int]:
        order = self._record_order(msg)
        received_order = self._record_received_order(msg)
        phase = self._numeric_order(msg.get("sort_phase"))
        index = self._numeric_order(msg.get("sort_index"))
        return (
            float(msg.get("sort_time") or self._record_time(msg)),
            order if order is not None else -1,
            received_order if received_order is not None else -1,
            phase or 0,
            index or 0,
        )

    def _chunk_blocks(self, blocks: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
        chunks: list[list[dict[str, Any]]] = []
        current: list[dict[str, Any]] = []
        count = 0
        max_messages = max(1, self._max_messages_per_image())
        split_blocks: list[dict[str, Any]] = []
        for block in blocks:
            messages = list(block.get("msgs") or [])
            for start in range(0, len(messages), max_messages):
                split_block = dict(block)
                split_block["msgs"] = messages[start : start + max_messages]
                split_blocks.append(split_block)

        for block in split_blocks:
            size = len(block["msgs"])
            if current and count + size > max_messages:
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

    def _select_query_records(
        self,
        records: list[dict[str, Any]],
        target_name: str,
        target_id: str = "",
        reverse: bool = True,
    ) -> list[dict[str, Any]]:
        max_pages = self._max_query_pages()
        if max_pages <= 0:
            return sorted(records, key=self._record_sort_key, reverse=reverse)

        selected: list[dict[str, Any]] = []
        latest_first = sorted(records, key=self._record_sort_key, reverse=True)
        for record in latest_first:
            trial = selected + [record]
            blocks = self._build_blocks(trial, target_name, target_id, reverse=True)
            if len(self._chunk_blocks(blocks)) > max_pages:
                if not selected:
                    selected = trial
                break
            selected = trial

        return sorted(selected, key=self._record_sort_key, reverse=reverse)

    def _dedupe_records(self, records: list[dict[str, Any]]) -> list[dict[str, Any]]:
        deduped: list[dict[str, Any]] = []
        for record in records:
            for idx, existing in enumerate(deduped):
                if self._records_are_duplicate(existing, record):
                    deduped[idx] = self._merge_duplicate_record(existing, record)
                    break
            else:
                deduped.append(record)
        return deduped

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
