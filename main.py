from __future__ import annotations

import asyncio
import base64
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
import astrbot.api.message_components as Comp
from astrbot.api.star import Context, Star

try:
    from .constants import *
    from .web.page_api import PageApiMixin
    from .web.page_settings import PageSettingsMixin
except ImportError:
    from constants import *
    from web.page_api import PageApiMixin
    from web.page_settings import PageSettingsMixin


def _load_result_template() -> str:
    return (Path(__file__).resolve().parent / "templates" / "result.html").read_text(encoding="utf-8")


HTML_TEMPLATE = _load_result_template()


class WhoAtMePlugin(PageApiMixin, PageSettingsMixin, Star):
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
        if sender_id:
            await self._remember_sender_member(event, group_id, sender_id)

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
        current = (
            await self._context_message(event, group_id, sender_info, quote)
            if context_on or reminder_context_on
            else None
        )

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
            record = await self._mention_record(event, group_id, targets, sender_info, quote)
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
        if not await self._reminder_user_enabled(group_id, user_id):
            await self.delete_kv_data(self._reminder_pending_key(group_id, user_id))
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

            if reminder_text:
                if not await self._try_send_text_images(event, reminder_text, image_paths):
                    await self._try_send(event, event.plain_result(reminder_text))
                    for image_path in image_paths:
                        if not await self._try_send(event, event.image_result(image_path)):
                            raise RuntimeError(f"发送提醒图片失败: {image_path}")
            elif not await self._try_send_images(event, image_paths):
                for image_path in image_paths:
                    if not await self._try_send(event, event.image_result(image_path)):
                        raise RuntimeError(f"发送提醒图片失败: {image_path}")
        except Exception as exc:
            logger.error(f"[谁艾特我] 渲染或发送提醒失败: {exc}")
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
        records = self._select_query_records(records, target_name, reverse=query_reverse)
        records = await self._resolve_record_pokes(event, group_id, records)
        blocks = self._build_blocks(records, target_name, reverse=query_reverse)
        chunks = self._chunk_blocks(blocks)
        chunks = self._limit_chunks(chunks, self._max_query_pages())
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
            except asyncio.TimeoutError:
                logger.warning(f"[谁艾特我] 浏览器直渲超过 {timeout} 秒，回退到 AstrBot html_render")
            except Exception as exc:
                logger.warning(
                    f"[谁艾特我] 浏览器直渲失败，回退到 AstrBot html_render: {type(exc).__name__}: {exc}",
                    exc_info=True,
                )
        return await asyncio.wait_for(self._render_html_with_t2i(HTML_TEMPLATE, data), timeout=timeout)

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
                browser = await playwright.chromium.launch(
                    args=["--disable-dev-shm-usage", "--disable-gpu", "--no-sandbox"]
                )
                page = await browser.new_page(
                    viewport={"width": 600, "height": 800},
                    device_scale_factor=2,
                )
                await page.set_content(
                    html_text,
                    wait_until="domcontentloaded",
                    timeout=self._render_page_timeout_ms(),
                )
                await self._wait_for_browser_assets(page)
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

    async def _render_html_with_t2i(self, template: str, data: dict[str, Any]) -> str:
        from jinja2 import Environment

        self._cleanup_old_renders()
        html_text = Environment(autoescape=True).from_string(template).render(**data)

        last_error: Exception | None = None
        for options in self._t2i_render_options():
            try:
                image_data = await self.html_render(html_text, {}, False, options)
            except TypeError:
                try:
                    rendered = await self.html_render(html_text, {}, options=options)
                    return str(rendered)
                except Exception as exc:
                    last_error = exc
                    logger.warning(f"[谁艾特我] t2i 渲染失败: {type(exc).__name__}: {exc}")
                    continue
            except Exception as exc:
                last_error = exc
                logger.warning(f"[谁艾特我] t2i 渲染失败: {type(exc).__name__}: {exc}")
                continue

            image_path = self._store_t2i_render_result(image_data, str(options.get("type") or ""))
            if image_path:
                return image_path

            logger.warning(f"[谁艾特我] t2i 返回了无效图片数据，尝试下一策略: {options}")

        if last_error:
            raise last_error
        raise RuntimeError("t2i 渲染没有返回有效图片")

    def _t2i_render_options(self) -> list[dict[str, Any]]:
        return [
            {
                "full_page": True,
                "type": "png",
                "device_scale_factor_level": "ultra",
                "timeout": self._render_page_timeout_ms(),
            },
            {
                "full_page": True,
                "type": "jpeg",
                "quality": self._render_quality(),
                "device_scale_factor_level": "high",
                "timeout": self._render_page_timeout_ms(),
            },
        ]

    def _store_t2i_render_result(self, image_data: Any, image_type: str = "") -> str | None:
        if isinstance(image_data, bytes | bytearray):
            return self._store_t2i_image_bytes(bytes(image_data), image_type)

        text = str(image_data or "").strip()
        if not text:
            return None
        if text.startswith("base64://"):
            try:
                return self._store_t2i_image_bytes(base64.b64decode(text[len("base64://") :]), image_type)
            except Exception as exc:
                logger.warning(f"[谁艾特我] 解析 t2i base64 图片失败: {type(exc).__name__}: {exc}")
                return None
        if text.lower().startswith("data:image/"):
            try:
                header, payload = text.split(",", 1)
                source_type = header.split(";", 1)[0].rsplit("/", 1)[-1]
                return self._store_t2i_image_bytes(base64.b64decode(payload), source_type)
            except Exception as exc:
                logger.warning(f"[谁艾特我] 解析 t2i data-uri 图片失败: {type(exc).__name__}: {exc}")
                return None
        if text.startswith("<") or "<html" in text[:200].lower():
            return None
        return text

    def _store_t2i_image_bytes(self, data: bytes, image_type: str = "") -> str | None:
        suffix = self._t2i_image_suffix(data, image_type)
        if not suffix:
            return None

        output = self._new_render_path(suffix)
        try:
            output.write_bytes(data)
            return str(output)
        except Exception as exc:
            logger.warning(f"[谁艾特我] 保存 t2i 图片失败: {type(exc).__name__}: {exc}")
            return None

    def _t2i_image_suffix(self, data: bytes, image_type: str = "") -> str:
        if data.startswith(b"\x89PNG\r\n\x1a\n"):
            return ".png"
        if data.startswith(b"\xff\xd8"):
            return ".jpg"
        if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
            return ".webp"
        image_type = image_type.lower().strip().lstrip(".")
        if image_type in {"png", "jpg", "jpeg", "webp"}:
            return ".jpg" if image_type == "jpeg" else f".{image_type}"
        return ""

    async def _wait_for_browser_assets(self, page: Any) -> None:
        asset_timeout = min(10000, max(1000, self._render_page_timeout_ms() // 2))
        try:
            await page.evaluate(
                """
                async (assetTimeout) => {
                  const delay = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
                  if (document.fonts && document.fonts.ready) {
                    await Promise.race([document.fonts.ready.catch(() => {}), delay(Math.min(assetTimeout, 1500))]);
                  }

                  const images = Array.from(document.images || []);
                  await Promise.race([
                    Promise.all(images.map((img) => new Promise((resolve) => {
                      if (img.complete) {
                        resolve();
                        return;
                      }
                      const done = () => resolve();
                      img.addEventListener("load", done, { once: true });
                      img.addEventListener("error", done, { once: true });
                    }))),
                    delay(assetTimeout),
                  ]);

                  for (const img of images) {
                    const optional = img.classList.contains("msg-img") || img.classList.contains("quote-img");
                    if (optional && (!img.complete || img.naturalWidth === 0)) {
                      img.remove();
                    }
                  }
                }
                """,
                asset_timeout,
            )
            await page.wait_for_timeout(300)
        except Exception as exc:
            logger.debug(f"[谁艾特我] 等待浏览器资源加载失败，继续截图: {type(exc).__name__}: {exc}")

    def _new_render_path(self, suffix: str = ".jpg") -> Path:
        render_dir = self._render_dir()
        render_dir.mkdir(parents=True, exist_ok=True)
        suffix = suffix if suffix.startswith(".") else f".{suffix}"
        return render_dir / f"who_at_me_{int(time.time())}_{uuid.uuid4().hex}{suffix}"

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
        for path in render_dir.glob("who_at_me_*.*"):
            try:
                if path.suffix.lower() not in {".jpg", ".jpeg", ".png", ".webp"}:
                    continue
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
        if not isinstance(records, list):
            return []
        return records[-self._max_records_per_target():]

    async def _get_pending_reminders(self, group_id: str, target: str) -> list[dict[str, Any]]:
        pending = await self.get_kv_data(self._reminder_pending_key(group_id, target), [])
        return pending if isinstance(pending, list) else []

    async def _remember_sender_member(self, event: AstrMessageEvent, group_id: str, user_id: str) -> None:
        if not group_id or not user_id:
            return
        info = self._member_info_from_event(event)
        sender_name = self._sender_name(event)
        if sender_name and not self._looks_like_numeric_id(sender_name):
            info.setdefault("nickname", sender_name)
        if not self._member_info_has_name(info):
            return
        await self._remember_member_info(group_id, user_id, info)

    async def _remember_member_info(self, group_id: str, user_id: str, info: dict[str, Any]) -> None:
        if not group_id or not user_id or not isinstance(info, dict):
            return
        data = {
            "card": str(info.get("card") or ""),
            "nickname": str(info.get("nickname") or info.get("name") or ""),
            "name": str(info.get("name") or ""),
            "time": int(time.time()),
        }
        if not self._member_info_has_name(data):
            return
        await self.put_kv_data(self._member_cache_key(group_id, user_id), data)

    async def _cached_member_info(self, group_id: str, user_id: str) -> dict[str, Any]:
        data = await self.get_kv_data(self._member_cache_key(group_id, user_id), {})
        return data if isinstance(data, dict) else {}

    def _member_info_has_name(self, info: dict[str, Any]) -> bool:
        for key in ("card", "nickname", "name"):
            value = info.get(key)
            if value and not self._looks_like_numeric_id(value):
                return True
        return False

    async def _member_info(self, event: AstrMessageEvent, group_id: str, user_id: str) -> dict[str, Any]:
        info = self._member_info_from_event(event) if user_id == self._sender_id(event) else {}
        if not user_id:
            return info
        cached = await self._cached_member_info(group_id, user_id)
        for key, value in cached.items():
            if value and not info.get(key):
                info[key] = value
        if self._member_info_has_name(info) and info.get("level") and info.get("role") and (info.get("title") or info.get("member_title")):
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
        if not self._member_info_has_name(info):
            list_info = await self._member_info_from_group_member_list(event, group_id, user_id)
            if list_info:
                info.update(list_info)
        if self._member_info_has_name(info):
            await self._remember_member_info(group_id, user_id, info)
        return info

    async def _member_info_from_group_member_list(
        self,
        event: AstrMessageEvent,
        group_id: str,
        user_id: str,
    ) -> dict[str, Any]:
        value = await self._call_onebot_action(
            event,
            "get_group_member_list",
            group_id=self._numeric_id(group_id),
        )
        data = self._mapping_data(value)
        if data:
            members = data.get("members") or data.get("list") or data.get("data")
        else:
            members = value
        if not isinstance(members, list):
            return {}
        for member in members:
            mapping = self._mapping_data(member)
            member_id = mapping.get("user_id") or mapping.get("userId") or mapping.get("qq") or mapping.get("id")
            if str(member_id or "") == str(user_id):
                return self._member_info_from_mapping(mapping)
        return {}

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
        card = data.get("card") or data.get("card_name") or data.get("cardName") or data.get("group_card") or data.get("groupCard")
        if card:
            info["card"] = str(card)
        nickname = (
            data.get("nickname")
            or data.get("nick")
            or data.get("nickName")
            or data.get("display_name")
            or data.get("displayName")
            or data.get("remark")
            or data.get("name")
        )
        if nickname:
            info["nickname"] = str(nickname)
        if data.get("name"):
            info["name"] = str(data["name"])
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
        if not self._reminder_user_allowed(user_id):
            return False
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
        user_rule_status = self._reminder_user_rule_status(sender_id)
        return (
            "艾特提醒状态：\n"
            f"本群提醒：{group_status}\n"
            f"当前 UMO：{current_umo or '未知'}\n"
            f"全局白名单：{global_status}\n"
            f"UMO 名单：{umo_status}\n"
            f"用户名单：{user_rule_status}\n"
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
                    msg["sort_phase"] = 0
                    msg["sort_index"] = idx
                    msg["sort_time"] = float(ctx.get("time", 0)) - 0.01 + idx * 0.001
                    messages.append(msg)

            main = self._view_message(record, True, target_name)
            main["sort_phase"] = 1
            main["sort_index"] = 0
            main["sort_time"] = float(record.get("time", 0))
            messages.append(main)

            if record.get("is_context"):
                for idx, ctx in enumerate(record.get("after") or []):
                    msg = self._view_message(ctx, False, target_name)
                    msg["sort_phase"] = 2
                    msg["sort_index"] = idx
                    msg["sort_time"] = float(ctx.get("time", 0)) + 0.001 + idx * 0.001
                    messages.append(msg)

            blocks.append(
                {
                    "at_time": record.get("time", 0),
                    "at_order": self._record_order(record),
                    "at_received_order": self._record_received_order(record),
                    "msgs": self._dedupe_messages(messages),
                }
            )

        blocks.sort(
            key=lambda item: (
                self._numeric_order(item.get("at_time")) or 0,
                item.get("at_order") if item.get("at_order") is not None else -1,
                item.get("at_received_order") if item.get("at_received_order") is not None else -1,
            ),
            reverse=reverse,
        )
        return self._dedupe_block_context(blocks)

    def _view_message(self, data: dict[str, Any], is_at: bool, target_name: str) -> dict[str, Any]:
        user_id = str(data.get("user_id") or data.get("User") or "")
        nickname = self._display_name(data.get("name"), data.get("nickname"), user_id, default="用户")
        poke = data.get("poke") if isinstance(data.get("poke"), dict) else None
        message = str(data.get("message") or "")
        if is_at:
            message = self._strip_at_display(message, [target_name, data.get("target"), data.get("at"), data.get("AtQQ")])
        images = self._renderable_images(data.get("images") or data.get("image") or [])
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
            "message_id": str(data.get("message_id") or ""),
            "order": self._record_order(data),
            "message": message,
            "has_message": bool(message.strip()),
            "message_html": html.escape(message).replace("\n", "<br>"),
            "images": images,
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
        images = self._renderable_images(quote.get("images") or quote.get("image") or [])
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

    def _dedupe_messages(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        seen: dict[tuple[Any, ...], dict[str, Any]] = {}
        for msg in messages:
            key = self._message_key(msg)
            if key not in seen or msg.get("is_at"):
                seen[key] = msg
        result = list(seen.values())
        result.sort(key=self._message_sort_key)
        return result

    def _dedupe_block_context(self, blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
        at_keys = {
            self._message_key(msg)
            for block in blocks
            for msg in block.get("msgs", [])
            if msg.get("is_at")
        }
        seen_context: set[tuple[Any, ...]] = set()
        for block in blocks:
            messages = []
            for msg in block.get("msgs", []):
                if msg.get("is_at"):
                    messages.append(msg)
                    continue

                key = self._message_key(msg)
                if key in at_keys or key in seen_context:
                    continue
                seen_context.add(key)
                messages.append(msg)
            block["msgs"] = messages
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

    def _select_query_records(
        self,
        records: list[dict[str, Any]],
        target_name: str,
        reverse: bool,
    ) -> list[dict[str, Any]]:
        max_pages = self._max_query_pages()
        if max_pages <= 0:
            return sorted(records, key=self._record_sort_key, reverse=reverse)

        selected: list[dict[str, Any]] = []
        latest_first = sorted(records, key=self._record_sort_key, reverse=True)
        for record in latest_first:
            trial = selected + [record]
            blocks = self._build_blocks(trial, target_name, reverse=True)
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
                    if self._record_time(record) >= self._record_time(existing):
                        deduped[idx] = record
                    break
            else:
                deduped.append(record)
        return deduped

    async def _mention_record(
        self,
        event: AstrMessageEvent,
        group_id: str,
        mentions: list[str] | None = None,
        member_info: dict[str, Any] | None = None,
        quote: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        sender_id = self._sender_id(event)
        sender = getattr(event.message_obj, "sender", None)
        member_info = member_info or {}
        role = str(member_info.get("role") or getattr(sender, "role", "") or self._raw_sender_value(event, "role") or "member")
        sender_name = self._display_name(member_info.get("card"), member_info.get("nickname"), self._sender_name(event), sender_id)
        images = self._images(event)
        message = self._message_text_for_record(event, mentions or [])
        poke = await self._poke_message(event, group_id, sender_name)
        record = {
            "user_id": sender_id,
            "message": message,
            "images": images,
            "name": sender_name,
            "role": role,
            "title": member_info.get("title") or "",
            "member_title": member_info.get("member_title") or "",
            "level": member_info.get("level") or "",
            "time": self._timestamp(event),
            "message_id": self._event_message_id(event),
            "order": self._event_order(event),
            "received_order": self._event_received_order(event),
        }
        if poke:
            record["poke"] = poke
        if quote:
            record["quote"] = quote
        return record

    async def _context_message(
        self,
        event: AstrMessageEvent,
        group_id: str,
        member_info: dict[str, Any] | None = None,
        quote: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        record = await self._mention_record(event, group_id, member_info=member_info, quote=quote)
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
            "message_id": record.get("message_id") or "",
            "order": record.get("order"),
            "received_order": record.get("received_order"),
        }
        if record.get("poke"):
            context["poke"] = record["poke"]
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
        include_at = not mentions
        raw_segments = self._raw_message_segments(event)
        if raw_segments:
            text = self._segments_text(raw_segments, include_at=include_at)
        else:
            text = self._segments_text(self._message_chain(event), include_at=include_at) or self._message_text(event)

        text = self._strip_cq_display(text)
        return self._strip_at_display(text, mentions) if mentions else text

    async def _poke_message(self, event: AstrMessageEvent, group_id: str, sender_name: str) -> dict[str, str] | None:
        raw = getattr(event.message_obj, "raw_message", None)
        if isinstance(raw, dict):
            poke = self._poke_from_mapping(raw, sender_name)
            if poke:
                return await self._resolve_poke_message(event, group_id, poke)

        for segment in [*self._raw_message_segments(event), *self._message_chain(event)]:
            poke = self._poke_from_segment(segment, sender_name)
            if poke:
                return await self._resolve_poke_message(event, group_id, poke)
        return None

    def _poke_from_segment(self, segment: Any, sender_name: str) -> dict[str, str] | None:
        data = self._segment_data(segment)
        if not data and not isinstance(segment, dict):
            names = [
                "type",
                "seg_type",
                "sub_type",
                "subType",
                "notice_type",
                "noticeType",
                "operator_name",
                "operatorName",
                "sender_name",
                "senderName",
                "from_name",
                "fromName",
                "operator_card",
                "operatorCard",
                "operator_nickname",
                "operatorNickname",
                "target_name",
                "targetName",
                "target_display",
                "targetDisplay",
                "receiver_name",
                "receiverName",
                "to_name",
                "toName",
                "target_id",
                "targetId",
                "target_uin",
                "targetUin",
                "target",
                "receiver_id",
                "receiverId",
                "user_id",
                "userId",
                "qq",
                "action",
                "action_text",
                "actionText",
                "suffix",
                "text",
                "display",
                "summary",
                "prompt",
                "msg",
                "raw_info",
                "rawInfo",
            ]
            data = {name: getattr(segment, name, None) for name in names}
        if not isinstance(data, dict):
            return None
        data = dict(data)
        data.setdefault("type", self._segment_type(segment))
        return self._poke_from_mapping(data, sender_name)

    def _poke_from_mapping(self, data: dict[str, Any], sender_name: str) -> dict[str, str] | None:
        seg_type = str(data.get("type") or data.get("seg_type") or data.get("notice_type") or "").lower()
        sub_type = str(data.get("sub_type") or data.get("subType") or data.get("notice_type") or data.get("noticeType") or "").lower()
        if seg_type not in POKE_SEGMENT_TYPES and sub_type not in POKE_SEGMENT_TYPES:
            return None

        actor = self._display_name(
            data.get("operator_card"),
            data.get("operatorCard"),
            data.get("operator_nickname"),
            data.get("operatorNickname"),
            data.get("operator_name"),
            data.get("operatorName"),
            data.get("sender_name"),
            data.get("senderName"),
            data.get("from_name"),
            data.get("fromName"),
            data.get("source_name"),
            data.get("sourceName"),
            data.get("card"),
            data.get("nickname"),
            data.get("name"),
            sender_name,
        )
        target_id = self._first_poke_target_id(data)
        target = self._display_name(
            data.get("target_name"),
            data.get("targetName"),
            data.get("target_display"),
            data.get("targetDisplay"),
            data.get("target_card"),
            data.get("targetCard"),
            data.get("target_nickname"),
            data.get("targetNickname"),
            data.get("receiver_name"),
            data.get("receiverName"),
            data.get("to_name"),
            data.get("toName"),
            data.get("poked_name"),
            data.get("pokedName"),
            default="",
        )
        if not target and data.get("target") and not self._looks_like_numeric_id(data.get("target")):
            target = str(data.get("target"))
        raw_text = self._poke_raw_text(data)
        action = self._display_name(
            data.get("action"),
            data.get("action_text"),
            data.get("actionText"),
            data.get("poke_action"),
            data.get("pokeAction"),
            default="",
        )
        suffix = self._display_name(
            data.get("suffix"),
            data.get("append"),
            data.get("tail"),
            data.get("postfix"),
            default="",
        )
        return {
            "actor": actor,
            "target": target or str(target_id or "") or "对方",
            "target_id": str(target_id or ""),
            "action": action or "👋 拍了拍",
            "suffix": suffix,
            "raw_text": raw_text,
        }

    async def _resolve_poke_message(
        self,
        event: AstrMessageEvent,
        group_id: str,
        poke: dict[str, str],
    ) -> dict[str, str]:
        target_id = str(poke.get("target_id") or "").strip()
        target = str(poke.get("target") or "").strip()
        if target_id and (not target or self._looks_like_numeric_id(target)):
            target = await self._target_name(event, group_id, target_id)
            poke["target"] = target

        parsed = self._parse_poke_raw_text(str(poke.get("raw_text") or ""), target)
        if parsed.get("action") and (not poke.get("action") or poke.get("action") == "👋 拍了拍"):
            poke["action"] = parsed["action"]
        if parsed.get("target") and (not poke.get("target") or self._looks_like_numeric_id(poke.get("target"))):
            poke["target"] = parsed["target"]
        if parsed.get("suffix") and not poke.get("suffix"):
            poke["suffix"] = parsed["suffix"]

        if not poke.get("target"):
            poke["target"] = target_id or "对方"
        poke["action"] = self._normalize_poke_action(str(poke.get("action") or ""), str(poke.get("raw_text") or ""))
        return poke

    async def _resolve_record_pokes(
        self,
        event: AstrMessageEvent,
        group_id: str,
        records: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        resolved = []
        for record in records:
            item = dict(record)
            await self._resolve_record_poke_item(event, group_id, item)
            for key in ("before", "after"):
                messages = []
                for ctx in item.get(key) or []:
                    ctx_item = dict(ctx)
                    await self._resolve_record_poke_item(event, group_id, ctx_item)
                    messages.append(ctx_item)
                if messages:
                    item[key] = messages
            resolved.append(item)
        return resolved

    async def _resolve_record_poke_item(
        self,
        event: AstrMessageEvent,
        group_id: str,
        record: dict[str, Any],
    ) -> None:
        poke = record.get("poke")
        if not isinstance(poke, dict):
            return
        poke = dict(poke)
        target = str(poke.get("target") or "").strip()
        if not poke.get("target_id") and self._looks_like_numeric_id(target):
            poke["target_id"] = target
        record["poke"] = await self._resolve_poke_message(event, group_id, poke)

    def _first_poke_target_id(self, data: dict[str, Any]) -> str:
        for key in (
            "target_id",
            "targetId",
            "target_uin",
            "targetUin",
            "receiver_id",
            "receiverId",
            "to_id",
            "toId",
            "to_uin",
            "toUin",
            "qq",
            "target",
        ):
            value = data.get(key)
            if value and self._looks_like_numeric_id(value):
                return str(value)
        return ""

    def _poke_raw_text(self, data: dict[str, Any]) -> str:
        value = self._first_mapping_value(
            data,
            [
                "raw_message",
                "rawMessage",
                "raw_info",
                "rawInfo",
                "summary",
                "prompt",
                "msg",
                "message",
                "text",
                "display",
                "content",
            ],
        )
        if isinstance(value, list):
            return self._segments_text(value, include_at=True)
        return str(value or "").strip()

    def _parse_poke_raw_text(self, text: str, target_name: str = "") -> dict[str, str]:
        text = re.sub(r"\s+", " ", str(text or "")).strip()
        if not text:
            return {}
        for token in POKE_ACTION_TOKENS:
            index = text.find(token)
            if index < 0:
                continue
            tail = text[index + len(token) :].strip()
            prefix = text[max(0, index - 2) : index].strip()
            action = f"{prefix} {token}".strip() if prefix and re.fullmatch(r"[^\w\u4e00-\u9fff]+", prefix) else token
            result = {"action": action}
            target_name = str(target_name or "").strip()
            if target_name and tail.startswith(target_name):
                result["target"] = target_name
                result["suffix"] = tail[len(target_name) :]
            elif tail:
                result["target"] = tail
            return result
        return {}

    def _normalize_poke_action(self, action: str, raw_text: str = "") -> str:
        action = re.sub(r"\s+", " ", str(action or "")).strip()
        if action and not any(token in action for token in POKE_ACTION_TOKENS):
            parsed = self._parse_poke_raw_text(raw_text)
            if parsed.get("action"):
                return parsed["action"]
        return action or "👋 拍了拍"

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
                value = self._segment_value(
                    segment,
                    ["name", "display", "text", "nickname", "card", "qq", "user_id", "target", "id"],
                )
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
        cleaned = re.sub(r"\[CQ:at,([^\]]+)\]", self._cq_at_display, cleaned)
        cleaned = re.sub(r"\[CQ:[^\]]+\]", " ", cleaned)
        return re.sub(r"\s+", " ", cleaned).strip()

    def _cq_at_display(self, match: re.Match[str]) -> str:
        data = self._parse_cq_attrs(match.group(1))
        value = (
            data.get("name")
            or data.get("display")
            or data.get("text")
            or data.get("nickname")
            or data.get("card")
            or data.get("qq")
        )
        return f"@{value}" if value else " "

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

    def _numeric_order(self, value: Any) -> int | None:
        if value is None:
            return None
        if isinstance(value, bool):
            return None
        if isinstance(value, (int, float)):
            return int(value)
        text = str(value).strip()
        if not text or text.lower() in {"none", "null", "undefined"}:
            return None
        if text.lstrip("-").isdigit():
            return int(text)
        return None

    def _event_message_id(self, event: AstrMessageEvent) -> str:
        raw = getattr(event.message_obj, "raw_message", None)
        for key in ("message_id", "messageId", "msg_id", "msgId", "id"):
            value = getattr(event.message_obj, key, None)
            if value is not None and str(value).strip():
                return str(value)
        if isinstance(raw, dict):
            for key in ("message_id", "messageId", "msg_id", "msgId", "id"):
                value = raw.get(key)
                if value is not None and str(value).strip():
                    return str(value)
        return ""

    def _event_order(self, event: AstrMessageEvent) -> int | None:
        raw = getattr(event.message_obj, "raw_message", None)
        keys = (
            "message_seq",
            "messageSeq",
            "msg_seq",
            "msgSeq",
            "seq",
            "real_id",
            "realId",
            "message_id",
            "messageId",
            "msg_id",
            "msgId",
            "id",
        )
        for key in keys:
            order = self._numeric_order(getattr(event.message_obj, key, None))
            if order is not None:
                return order
        if isinstance(raw, dict):
            for key in keys:
                order = self._numeric_order(raw.get(key))
                if order is not None:
                    return order
        return self._numeric_order(self._event_message_id(event))

    def _event_received_order(self, event: AstrMessageEvent) -> int:
        holder = getattr(event, "message_obj", event)
        cached = self._numeric_order(getattr(holder, "_who_at_me_received_order", None))
        if cached is not None:
            return cached
        self._receive_order += 1
        order = self._receive_order
        try:
            setattr(holder, "_who_at_me_received_order", order)
        except Exception:
            pass
        return order

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
            poke = record.get("poke")
            if isinstance(poke, dict):
                msg = (
                    f"{poke.get('actor') or name} "
                    f"{poke.get('action') or '拍了拍'} "
                    f"{poke.get('target') or '对方'}{poke.get('suffix') or ''}"
                )
            else:
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
        poke = record.get("poke")
        if isinstance(poke, dict):
            return "poke:" + self._normalize_record_text(
                f"{poke.get('actor') or ''}|{poke.get('action') or ''}|{poke.get('target') or ''}|{poke.get('suffix') or ''}"
            )
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

    def _record_order(self, record: dict[str, Any]) -> int | None:
        for key in (
            "order",
            "message_seq",
            "messageSeq",
            "msg_seq",
            "msgSeq",
            "seq",
            "real_id",
            "realId",
            "message_id",
            "messageId",
            "msg_id",
            "msgId",
            "id",
        ):
            order = self._numeric_order(record.get(key))
            if order is not None:
                return order
        return None

    def _record_received_order(self, record: dict[str, Any]) -> int | None:
        return self._numeric_order(record.get("received_order"))

    def _record_sort_key(self, record: dict[str, Any]) -> tuple[int, int, int]:
        order = self._record_order(record)
        received_order = self._record_received_order(record)
        return (
            self._record_time(record),
            order if order is not None else -1,
            received_order if received_order is not None else -1,
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

    def _config_list(self, *keys: str) -> list[str]:
        value = self._config_value(*keys, default=[])
        if isinstance(value, list):
            return [str(item).strip() for item in value if str(item).strip()]
        if isinstance(value, str):
            parts = re.split(r"[\n,，]+", value)
            return [part.strip() for part in parts if part.strip()]
        return []

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
        return self._image_src("header")

    def _footer_image_url(self) -> str:
        return self._image_src("footer")

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

    def _reminder_user_whitelist(self) -> set[str]:
        return set(self._config_list("reminder", "user_whitelist"))

    def _reminder_user_blacklist(self) -> set[str]:
        return set(self._config_list("reminder", "user_blacklist"))

    def _reminder_user_allowed(self, user_id: str) -> bool:
        user_id = str(user_id or "").strip()
        if not user_id:
            return False
        if user_id in self._reminder_user_blacklist():
            return False
        whitelist = self._reminder_user_whitelist()
        return not whitelist or user_id in whitelist

    def _reminder_user_rule_status(self, user_id: str) -> str:
        user_id = str(user_id or "").strip()
        whitelist = self._reminder_user_whitelist()
        blacklist = self._reminder_user_blacklist()
        if user_id and user_id in blacklist:
            return "黑名单命中"
        if whitelist:
            return "白名单命中" if user_id in whitelist else "白名单未命中"
        if blacklist:
            return "黑名单未命中"
        return "未配置名单"

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

    def _member_cache_key(self, group_id: str, user_id: str) -> str:
        return f"member:name:{group_id}:{user_id}"

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
