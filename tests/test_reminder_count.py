"""隔离加载实际源码；无需 AstrBot、网络或 QQ 账号。"""
import ast
import asyncio
import copy
from contextlib import asynccontextmanager
from datetime import datetime
import logging
import os
from pathlib import Path
import re
import shutil
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock
from urllib.parse import quote
import uuid

ROOT = Path(__file__).resolve().parents[1]


def plugin_harness():
    # 保留源码方法与内部调用链，只跳过服务器初始化及事件注册装饰器。
    sources = {"main.py": "WhoAtMePlugin", "modules/config.py": "ConfigMixin",
               "modules/data.py": "DataMixin", "modules/message.py": "MessageMixin"}
    methods = []
    for filename, class_name in sources.items():
        tree = ast.parse((ROOT / filename).read_text())
        cls = next(node for node in tree.body
                   if isinstance(node, ast.ClassDef) and node.name == class_name)
        for node in cls.body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) or node.name == "__init__":
                continue
            node.decorator_list = [decorator for decorator in node.decorator_list
                                   if isinstance(decorator, ast.Name)
                                   and decorator.id in {"staticmethod", "classmethod", "asynccontextmanager"}]
            methods.append(node)
    module = ast.parse("from __future__ import annotations\nclass Harness: pass")
    module.body[1].body = methods
    namespace = dict(globals(), logger=logging.getLogger(__name__))
    exec(compile((ROOT / "modules/constants.py").read_text(), "<constants>", "exec"), namespace)
    exec(compile(ast.fix_missing_locations(module), "<reminder-methods>", "exec"), namespace)
    plugin = namespace["Harness"]()
    plugin._message_count_epoch = "current"
    plugin._group_message_counts = {}
    plugin._latest_group_activity = {}
    plugin._group_pipeline_locks = {}
    plugin._kv_locks = {}
    plugin.before_cache = {}
    plugin.after_tasks = {}
    plugin.reminder_before_cache = {}
    plugin.reminder_after_tasks = {}
    plugin.bot_name_cache = {}
    plugin._receive_order = 0
    plugin.config = {}
    plugin.store = {}
    plugin.deleted_images = []

    async def get(key, default=None):
        return copy.deepcopy(plugin.store.get(key, default))

    async def put(key, value):
        plugin.store[key] = copy.deepcopy(value)

    async def delete(key):
        plugin.store.pop(key, None)

    plugin.get_kv_data = get
    plugin.put_kv_data = put
    plugin.delete_kv_data = delete
    plugin._delete_cached_image = plugin.deleted_images.append
    plugin._reminder_away_seconds = lambda: 600
    plugin._reminder_min_messages = lambda: 20
    plugin._reminder_group_enabled = AsyncMock(return_value=True)
    plugin._reminder_user_enabled = AsyncMock(return_value=True)
    return plugin


def event(platform="A", *, timestamp=1000, sender="sender", self_id="bot", message_obj=None):
    return SimpleNamespace(
        message_obj=message_obj if message_obj is not None else SimpleNamespace(
            timestamp=timestamp, raw_message={}, group_id="group", message_id="message"),
        get_platform_id=lambda: platform, get_platform_name=lambda: "aiocqhttp",
        get_sender_id=lambda: sender, get_self_id=lambda: self_id,
        plain_result=lambda text: text,
    )


class ReminderCountTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.plugin = plugin_harness()
        self.record = {"time": 1000, "message_count_epoch": "current",
                       "group_message_count": 10, "message_sequence": 100,
                       "message_id": "mention", "message": "mention", "platform_id": "A"}

    def ready(self, between, minimum=20, now=1600, record=None):
        return self.plugin._pending_reminder_ready(
            self.record if record is None else record, now, 600, minimum,
            10 + between + 1,
        )

    async def queue(self, platform="A", *, message="mention", context=None, before=None):
        p = self.plugin
        mention = event(platform)
        record = self.record | {"message_id": message, "message": message,
                                "group_message_count": p._event_group_message_count(mention, "group")}
        queued = await p._queue_reminder_if_needed(
            mention, "group", "user", record, context or {}, before or [])
        self.assertTrue(queued)
        return (await p._get_pending_reminders(platform, "group", "user"))[0]

    def test_received_count_boundaries_ignore_protocol_sequence(self):
        for sequence in (None, 100, 2021177632, -100):
            for between, expected in ((6, False), (19, False), (20, True), (21, True)):
                with self.subTest(sequence=sequence, between=between):
                    self.record["message_sequence"] = sequence
                    self.assertEqual(self.ready(between), expected)

    def test_time_threshold_still_applies(self):
        self.assertFalse(self.ready(21, now=1599))
        self.assertTrue(self.ready(20, now=1600))

    def test_incomparable_or_missing_counts_do_not_satisfy_positive_threshold(self):
        for change in ({"message_count_epoch": "old"}, {"message_count_epoch": None},
                       {"group_message_count": None}, {"group_message_count": "invalid"}):
            with self.subTest(change=change):
                record = self.record | change
                self.assertFalse(self.ready(21, record=record))
                self.assertTrue(self.ready(21, minimum=0, record=record))
                self.assertFalse(self.ready(21, minimum=0, now=1599, record=record))

    def test_event_cache_is_scoped_by_platform_group_and_plugin_epoch(self):
        p = self.plugin
        shared = SimpleNamespace(_who_at_me_group_message_count=999)
        a, b = event("A", message_obj=shared), event("B", message_obj=shared)
        self.assertEqual(p._event_group_message_count(a, "group"), 1)
        self.assertEqual(p._event_group_message_count(a, "group"), 1)
        p._event_group_message_count(event("A"), "group")
        self.assertEqual(p._event_group_message_count(b, "group"), 1)
        self.assertEqual(p._event_group_message_count(a, "other"), 1)
        a.get_platform_id = lambda: "B"
        self.assertEqual(p._event_group_message_count(a, "group"), 2)
        restarted = plugin_harness()
        restarted._message_count_epoch = "restarted"
        self.assertEqual(restarted._event_group_message_count(a, "group"), 1)

    async def test_two_bots_thresholds_ten_and_twenty_are_independent(self):
        for threshold in (10, 20):
            for platform, between in (("A", threshold - 1), ("B", threshold)):
                with self.subTest(threshold=threshold, returning=platform):
                    self.plugin = p = plugin_harness()
                    p._reminder_min_messages = lambda: threshold
                    await self.queue("A")
                    await self.queue("B")
                    for _ in range(threshold - 1):
                        p._event_group_message_count(event("A"), "group")
                    for _ in range(threshold):
                        p._event_group_message_count(event("B"), "group")
                    untouched = "B" if platform == "A" else "A"
                    other_before = await p._get_pending_reminders(untouched, "group", "user")
                    returning = event(platform, timestamp=1600, sender="user")
                    ready, _ = await p._take_ready_pending_reminders(
                        platform, "group", "user", 1600,
                        p._event_group_message_count(returning, "group"))
                    self.assertEqual(bool(ready), between >= threshold)
                    self.assertEqual(await p._get_pending_reminders(untouched, "group", "user"), other_before)
                    self.assertEqual(await p._get_pending_reminders(platform, "group", "user"), [])

    async def test_return_consumes_pending_and_selects_only_eligible_mentions(self):
        p = self.plugin
        old = self.record | {"group_message_count": 1, "message_id": "old", "message": "old"}
        recent = self.record | {"group_message_count": 15, "message_id": "new", "message": "new"}
        key = p._reminder_pending_key("A", "group", "user")
        await p.put_kv_data(key, [old, recent])
        await p._remember_pending_key(key)
        ready, original = await p._take_ready_pending_reminders("A", "group", "user", 1600, 22)
        self.assertEqual(ready, [old])
        self.assertEqual(original, [old, recent])
        self.assertEqual(await p._take_ready_pending_reminders("A", "group", "user", 1600, 23), ([], []))

    async def test_sender_activity_does_not_cancel_other_bot_queue(self):
        p = self.plugin
        p._remember_group_activity(event("A", timestamp=2000, sender="user"), "group", "user")
        self.assertTrue(p._returned_after_mention("A", "group", "user", self.record | {"group_message_count": 0}))
        self.assertFalse(p._returned_after_mention("B", "group", "user", self.record | {"group_message_count": 0}))
        await self.queue("B")

    async def test_actual_delivery_failure_restores_only_origin_platform(self):
        for text_sent in (False, True):
            with self.subTest(text_sent=text_sent):
                self.plugin = p = plugin_harness()
                await self.queue("A")
                b_before = await self.queue("B")
                for _ in range(20):
                    p._event_group_message_count(event("A"), "group")
                p._return_message_targets = AsyncMock(return_value=set())
                p._target_name = AsyncMock(return_value="用户")
                async def resolve(event, group, records):
                    return records
                p._resolve_record_pokes = resolve
                p._render_text_only = lambda: True
                p._try_send = AsyncMock(return_value=text_sent)
                p._try_send_records_forward_text = AsyncMock(return_value=False)
                await p._deliver_pending_reminders(event("A", timestamp=1600, sender="user"), "group", "user")
                self.assertEqual(len(await p._get_pending_reminders("A", "group", "user")), 1)
                self.assertEqual(await p._get_pending_reminders("B", "group", "user"), [b_before])
                self.assertEqual(p._try_send_records_forward_text.await_count, int(text_sent))

    async def test_restore_delete_and_recall_leave_other_platform_untouched(self):
        p = self.plugin
        a = await self.queue("A")
        b = await self.queue("B")
        await p._delete_pending_reminders("A", "group", "user")
        await p._restore_pending_reminders("A", "group", "user", [a, b])
        self.assertEqual(await p._get_pending_reminders("A", "group", "user"), [a])
        task = {"target": "user", "time": 1000, "message_id": "mention", "count": 0, "limit": 2}
        for platform in ("A", "B"):
            p.reminder_after_tasks[(platform, "group")] = [dict(task)]
            p._append_reminder_before_context_cache(platform, "group", a)
        p._recall_message_id = lambda event: "mention"
        self.assertTrue(await p._handle_recall_event(event("A"), "group"))
        self.assertEqual(await p._get_pending_reminders("A", "group", "user"), [])
        self.assertEqual(await p._get_pending_reminders("B", "group", "user"), [b])
        self.assertEqual(p.reminder_after_tasks[("A", "group")], [])
        self.assertEqual(p.reminder_after_tasks[("B", "group")], [task])
        self.assertEqual(p.reminder_before_cache[("A", "group")], [])
        self.assertEqual(len(p.reminder_before_cache[("B", "group")]), 1)

    async def test_legacy_unowned_records_are_quarantined_even_at_zero_threshold(self):
        p = self.plugin
        legacy = "reminder:pending:group:user"
        legacy_index = "reminder:pending:index"
        legacy_record = {"time": 1000, "message_id": "mention"}
        p.store[legacy] = [legacy_record]
        p.store[legacy_index] = [legacy]
        p._reminder_min_messages = lambda: 0
        for platform in ("A", "B"):
            self.assertEqual(await p._take_ready_pending_reminders(platform, "group", "user", 2000, 100), ([], []))
            await p._restore_pending_reminders(platform, "group", "user", [legacy_record])
            await p._remove_recalled_message(platform, "group", "mention")
            await p._clear_all(event(platform))
        self.assertEqual(p.store[legacy], [legacy_record])
        self.assertEqual(p.store[legacy_index], [legacy])
        scoped_key = p._reminder_pending_key("A", "group", "user")
        p.store[scoped_key] = [legacy_record, self.record | {"platform_id": "B"}]
        self.assertEqual(await p._get_pending_reminders("A", "group", "user"), [])

    async def test_clear_all_limits_pending_and_reminder_tasks_to_current_platform(self):
        p = self.plugin
        await self.queue("A")
        b = await self.queue("B")
        for platform in ("A", "B"):
            p.reminder_after_tasks[(platform, "group")] = [{"target": "user"}]
            p._append_reminder_before_context_cache(platform, "group", self.record)
        await p._clear_all(event("A"))
        self.assertEqual(await p._get_pending_reminders("A", "group", "user"), [])
        self.assertEqual(await p._get_pending_reminders("B", "group", "user"), [b])
        self.assertNotIn(("A", "group"), p.reminder_after_tasks)
        self.assertIn(("B", "group"), p.reminder_after_tasks)
        self.assertIn(("B", "group"), p.reminder_before_cache)
        self.assertEqual(await p._pending_index_keys(), [p._reminder_pending_key("B", "group", "user")])

    async def test_async_context_pipeline_cannot_mix_platforms(self):
        p = self.plugin
        p._context_enabled = AsyncMock(return_value=True)
        p._reminder_context_config = AsyncMock(return_value={"enabled": True, "before": 1, "after": 1})
        p._member_info = AsyncMock(return_value={})
        p._quote = AsyncMock(return_value=None)
        p._append_record = AsyncMock()
        entered, resume = asyncio.Event(), asyncio.Event()

        async def normalized(event, group, mentions, sender_info, quote):
            platform = event.get_platform_id()
            if platform == "A" and event.message_obj.message_id == "mention-A":
                entered.set()
                await resume.wait()
            return {"time": event.message_obj.timestamp, "message_id": event.message_obj.message_id,
                    "message": event.message_obj.message_id, "user_id": event.get_sender_id()}

        p._context_message = normalized
        p._mention_record = normalized
        for platform in ("A", "B"):
            before = event(platform)
            before.message_obj.message_id = f"before-{platform}"
            await p._record_context_message(before, "group", [], append_to_cache=True)
        a = event("A")
        a.message_obj.message_id = "mention-A"
        running = asyncio.create_task(p._record_mentions(a, "group", ["user"]))
        await entered.wait()
        try:
            b = event("B")
            b.message_obj.message_id = "mention-B"
            await asyncio.wait_for(p._record_mentions(b, "group", ["user"]), 1)
        finally:
            resume.set()
            await running
        for platform in ("A", "B"):
            pending = (await p._get_pending_reminders(platform, "group", "user"))[0]
            self.assertEqual([item["message_id"] for item in pending["before"]], [f"before-{platform}"])
        after_b = event("B", timestamp=1001)
        after_b.message_obj.message_id = "after-B"
        await p._record_context_message(after_b, "group", [], append_to_cache=True)
        a_pending = (await p._get_pending_reminders("A", "group", "user"))[0]
        b_pending = (await p._get_pending_reminders("B", "group", "user"))[0]
        self.assertEqual(a_pending["after"], [])
        self.assertEqual([item["message_id"] for item in b_pending["after"]], ["after-B"])
        self.assertEqual(p.reminder_after_tasks[("A", "group")][0]["count"], 0)
        self.assertEqual(p.reminder_after_tasks[("B", "group")], [])

    async def test_disabled_reminder_context_does_not_inherit_shared_query_context(self):
        p = self.plugin
        contaminated = self.record | {"is_context": True, "before": [{"message": "B-secret"}],
                                      "after": [{"message": "B-secret"}]}
        await p._queue_reminder_if_needed(event("A"), "group", "user", contaminated, {"enabled": False}, [])
        pending = (await p._get_pending_reminders("A", "group", "user"))[0]
        for field in ("is_context", "before", "after"):
            self.assertNotIn(field, pending)

    async def test_query_image_cleanup_cannot_delete_another_bot_reminder_cache(self):
        p = self.plugin
        original = self.record | {"images": ["https://example.invalid/image"],
                                  "image_cache": [{"source": "https://example.invalid/image", "local": "query-image"}]}
        p._recent_image_cache_records = lambda: 1
        image_number = 0
        async def cache_image(source):
            nonlocal image_number
            image_number += 1
            return {"source": source, "local": f"reminder-image-{image_number}", "hash": str(image_number)}
        p._cache_image = cache_image
        for platform in ("A", "B"):
            await p._queue_reminder_if_needed(event(platform), "group", "user", original, {"enabled": True}, [original])
        a = await p._get_pending_reminders("A", "group", "user")
        b = await p._get_pending_reminders("B", "group", "user")
        a_paths = p._record_image_cache_paths(a[0])
        b_paths = p._record_image_cache_paths(b[0])
        self.assertTrue(a_paths)
        self.assertTrue(b_paths)
        self.assertTrue(a_paths.isdisjoint(b_paths))
        p._drop_record_image_cache(original, delete_files=True)
        await p._delete_pending_reminders("A", "group", "user")
        self.assertTrue(a_paths.issubset(set(p.deleted_images)))
        self.assertTrue(b_paths.isdisjoint(p.deleted_images))
        self.assertEqual(await p._get_pending_reminders("B", "group", "user"), b)

    async def test_reminder_images_copy_local_cache_without_redownload(self):
        p = self.plugin
        with tempfile.TemporaryDirectory() as directory:
            p._plugin_data_dir = lambda: directory
            p._recent_image_cache_records = lambda: 1
            p._cache_image = AsyncMock(side_effect=AssertionError("提醒不应重新下载已缓存图片"))
            query_dir = Path(directory) / "message_images" / "query"
            query_dir.mkdir(parents=True)

            def cached(name):
                path = query_dir / f"{name}.png"
                path.write_bytes(name.encode())
                source = f"https://example.invalid/{name}"
                return {"images": [source], "image_cache": [{"source": source, "local": str(path), "hash": name}]}

            record = self.record | cached("mention") | {"is_context": True, "before": [cached("query-context")]}
            before = {"message_id": "before", "time": 990} | cached("before")
            after = {"message_id": "after", "time": 1010} | cached("after")
            paths = {}
            for platform in ("A", "B"):
                self.assertTrue(await p._queue_reminder_if_needed(
                    event(platform), "group", "user", record, {"enabled": True}, [before]))
                p.reminder_after_tasks[(platform, "group")] = [
                    {"target": "user", "time": 1000, "message_id": "mention", "count": 0, "limit": 1}]
                await p._append_reminder_after_context(platform, "group", after)
                pending = (await p._get_pending_reminders(platform, "group", "user"))[0]
                paths[platform] = p._record_image_cache_paths(pending)

            p._cache_image.assert_not_awaited()
            originals = set(map(str, query_dir.iterdir()))
            self.assertTrue(paths["A"].isdisjoint(paths["B"]))
            for platform_paths in paths.values():
                self.assertTrue(platform_paths.isdisjoint(originals))
                self.assertEqual({Path(path).read_bytes() for path in platform_paths},
                                 {b"mention", b"before", b"after"})
            # 查询侧删除原图后，提醒仍持有自己的文件；查询上下文图片没有被复制。
            for path in originals:
                Path(path).unlink()
            remaining = {str(path) for path in Path(directory).rglob("*.png")}
            self.assertEqual(remaining, paths["A"] | paths["B"])

    async def test_member_and_bot_identity_caches_are_scoped(self):
        p = self.plugin
        for platform in ("A", "B"):
            await p._remember_member_info(platform, "group", "user", {"nickname": "同名"}, complete=True)
        self.assertTrue(await p._cached_member_info("A", "group", "user"))
        self.assertTrue(await p._cached_member_info("B", "group", "user"))
        await p._remember_member_info("B", "group", "user", {"nickname": "乙昵称"}, complete=True)
        self.assertEqual((await p._cached_member_info("A", "group", "user"))["nickname"], "同名")
        self.assertEqual((await p._cached_member_info("B", "group", "user"))["nickname"], "乙昵称")
        async def bot_name(event, group):
            return event.get_platform_id() + event.get_self_id()
        p._bot_name_from_api = AsyncMock(side_effect=bot_name)
        self.assertEqual(await p._bot_name(event("A", self_id="one"), "group"), "Aone")
        self.assertEqual(await p._bot_name(event("B", self_id="one"), "group"), "Bone")
        self.assertEqual(await p._bot_name(event("A", self_id="two"), "group"), "Atwo")
        self.assertEqual(await p._bot_name(event("A", self_id="one"), "group"), "Aone")
        self.assertEqual(p._bot_name_from_api.await_count, 3)
        await self.queue("B")
        self.assertFalse(await p._queue_reminder_if_needed(event("A", self_id="user"), "group", "user", self.record, {}, []))
        self.assertEqual(len(await p._get_pending_reminders("B", "group", "user")), 1)
        await self.queue("A")
        await p._mark_group_activity(event("A", self_id="user", sender="user"))
        self.assertEqual(await p._get_pending_reminders("A", "group", "user"), [])
        self.assertEqual(len(await p._get_pending_reminders("B", "group", "user")), 1)

    async def test_delayed_queue_ignores_other_platform_return(self):
        p = self.plugin
        entered, resume = asyncio.Event(), asyncio.Event()
        cache_images = p._cache_record_images

        async def delayed(record):
            entered.set()
            await resume.wait()
            return await cache_images(record)

        p._cache_record_images = delayed
        running = asyncio.create_task(self.queue("A"))
        await entered.wait()
        try:
            for _ in range(20):
                p._event_group_message_count(event("B"), "group")
            p._remember_group_activity(event("B", timestamp=2000, sender="user"), "group", "user")
        finally:
            resume.set()
            await running
        self.assertEqual(len(await p._get_pending_reminders("A", "group", "user")), 1)
        self.assertEqual(await p._get_pending_reminders("B", "group", "user"), [])

    def test_pending_key_components_cannot_alias_and_missing_platform_fails_closed(self):
        p = self.plugin
        self.assertNotEqual(p._reminder_pending_key("A:B", "group", "user"),
                            p._reminder_pending_key("A", "B:group", "user"))
        self.assertNotEqual(p._reminder_pending_key("A", "group:user", "x"),
                            p._reminder_pending_key("A", "group", "user:x"))
        with self.assertRaises(ValueError):
            p._event_group_message_count(event(""), "group")
        with self.assertRaises(ValueError):
            p._event_group_message_count(event(None), "group")


if __name__ == "__main__":
    unittest.main()
