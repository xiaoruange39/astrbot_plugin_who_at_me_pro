"""测试群关闭彻底静默（零数据库读写）、禁止 Base64 入库与成员缓存写库降频。"""
import base64
import hashlib
import ipaddress
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from urllib.parse import unquote, urlparse

import test_reminder_count
from test_reminder_count import plugin_harness

test_reminder_count.base64 = base64
test_reminder_count.hashlib = hashlib
test_reminder_count.ipaddress = ipaddress
test_reminder_count.urlparse = urlparse
test_reminder_count.unquote = unquote


PNG_1X1_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO7+X3cAAAAASUVORK5CYII="
)
PNG_1X1_DATA_URI = "data:image/png;base64," + base64.b64encode(PNG_1X1_BYTES).decode("ascii")
PNG_1X1_BASE64_URI = "base64://" + base64.b64encode(PNG_1X1_BYTES).decode("ascii")


def group_event(
    text: str = "hello",
    *,
    platform: str = "A",
    group_id: str = "g1",
    sender_id: str = "u1",
    self_id: str = "bot",
    mentions: list[str] | None = None,
    umo: str = "A:GroupMessage:g1",
    is_admin: bool = False,
):
    raw_msg = {
        "group_id": group_id,
        "user_id": sender_id,
        "sender": {"user_id": sender_id, "nickname": "Alice", "card": "AliceCard", "role": "admin" if is_admin else "member"},
    }
    msg_obj = SimpleNamespace(
        timestamp=2000,
        raw_message=raw_msg,
        group_id=group_id,
        message_id="m1",
        message=[],
    )
    ev = SimpleNamespace(
        unified_msg_origin=umo,
        message_str=text,
        message_obj=msg_obj,
        get_platform_id=lambda: platform,
        get_platform_name=lambda: "aiocqhttp",
        get_sender_id=lambda: sender_id,
        get_self_id=lambda: self_id,
        get_message_str=lambda: text,
        is_admin=lambda: is_admin,
        plain_result=lambda t: t,
        stop_event=lambda: None,
    )
    ev._test_mentions = list(mentions or [])
    return ev


class StorageAndGroupGatingTests(unittest.IsolatedAsyncioTestCase):
    async def test_disabled_group_performs_zero_db_writes_and_caches_read(self):
        plugin = plugin_harness()
        # Use real _reminder_group_enabled with default_group_enabled=False
        real_method = type(plugin)._reminder_group_enabled.__get__(plugin, type(plugin))
        plugin._reminder_group_enabled = real_method
        plugin.config = {"reminder": {"default_group_enabled": False}}

        get_calls: list[str] = []
        put_calls: list[str] = []

        orig_get = plugin.get_kv_data
        orig_put = plugin.put_kv_data

        async def tracked_get(key, default=None):
            get_calls.append(key)
            return await orig_get(key, default)

        async def tracked_put(key, value):
            put_calls.append(key)
            return await orig_put(key, value)

        plugin.get_kv_data = tracked_get
        plugin.put_kv_data = tracked_put
        plugin._mentions = lambda ev: getattr(ev, "_test_mentions", [])

        ev1 = group_event("你好 @u2", mentions=["u2"])
        await plugin.mark_group_activity_early(ev1)
        replies1 = [r async for r in plugin.on_group_message(ev1)]
        self.assertEqual(replies1, [])

        # Second message in the same disabled group should hit memory cache -> 0 extra DB reads, 0 writes
        ev2 = group_event("再来一条 @u2", mentions=["u2"])
        await plugin.mark_group_activity_early(ev2)
        replies2 = [r async for r in plugin.on_group_message(ev2)]
        self.assertEqual(replies2, [])

        # Group notice (recall) in disabled group should also do 0 DB reads/writes
        await plugin.on_group_notice(ev2)

        self.assertEqual(put_calls, [])
        self.assertEqual(get_calls, ["reminder:group_enabled:g1"])
        self.assertEqual(plugin._group_message_counts, {})
        self.assertEqual(plugin._latest_group_activity, {})
        self.assertEqual(plugin.before_cache, {})
        self.assertEqual(plugin.reminder_before_cache, {})

        # Query command in disabled group returns notice without writing DB
        ev_query = group_event("谁艾特我")
        replies_q = [r async for r in plugin.on_group_message(ev_query)]
        self.assertEqual(len(replies_q), 1)
        self.assertIn("已关闭", replies_q[0])
        self.assertEqual(put_calls, [])

        # Admin re-enables group -> writes group switch and updates cache
        ev_on = group_event("#开启本群艾特提醒", is_admin=True)
        replies_on = [r async for r in plugin.on_group_message(ev_on)]
        self.assertEqual(replies_on, ["已开启本群艾特被动提醒。"])
        self.assertEqual(put_calls, ["reminder:group_enabled:g1"])
        self.assertTrue(await plugin._group_active(ev1, "g1"))

    async def test_validated_message_image_source_keeps_local_path_without_base64(self):
        plugin = plugin_harness()
        with tempfile.TemporaryDirectory() as tmp:
            img_path = Path(tmp) / "sample.png"
            img_path.write_bytes(PNG_1X1_BYTES)
            plugin._is_allowed_local_image_path = lambda p: True

            res_path = await plugin._validated_message_image_source(str(img_path))
            self.assertEqual(res_path, str(img_path.resolve()))
            self.assertFalse(res_path.startswith("data:image/"))

            res_uri = await plugin._validated_message_image_source(img_path.resolve().as_uri())
            self.assertEqual(res_uri, str(img_path.resolve()))
            self.assertFalse(res_uri.startswith("data:image/"))

    async def test_cache_record_images_strips_inline_base64_and_still_renders(self):
        plugin = plugin_harness()
        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp) / "message_images"
            plugin._message_image_cache_dir = lambda: cache_dir
            plugin._is_allowed_local_image_path = lambda p: True
            plugin.config = {"record": {"recent_image_cache_records": 10}}

            record = {
                "message": "带图消息",
                "images": [PNG_1X1_DATA_URI, PNG_1X1_BASE64_URI, "https://example.com/a.png"],
                "before": [
                    {"message": "前文", "images": [PNG_1X1_DATA_URI]},
                ],
                "media": [
                    {"type": "file", "cover": PNG_1X1_DATA_URI},
                ],
            }
            cached = await plugin._cache_record_images(record, force=True)

            # Inline base64 must be stripped from images and never stored in image_cache.source
            self.assertEqual(cached["images"], ["https://example.com/a.png"])
            self.assertEqual(len(cached["image_cache"]), 1)
            self.assertTrue(cached["image_cache"][0]["source"].startswith("sha256:"))
            self.assertTrue(Path(cached["image_cache"][0]["local"]).is_file())
            self.assertEqual(cached["before"][0]["images"], [])
            self.assertTrue(cached["before"][0]["image_cache"][0]["source"].startswith("sha256:"))
            self.assertNotIn("cover", cached["media"][0])
            self.assertTrue(cached["media"][0]["cover_cache"]["source"].startswith("sha256:"))

            # Rendering must still produce data URI on demand from local file
            rendered = plugin._record_renderable_images(cached)
            self.assertEqual(rendered, [PNG_1X1_DATA_URI])

    async def test_sanitize_kv_value_strips_nested_base64_guardrail(self):
        plugin = plugin_harness()
        dirty = [
            {
                "images": [PNG_1X1_DATA_URI, PNG_1X1_BASE64_URI, "https://example.com/ok.png"],
                "image_cache": [
                    {"source": PNG_1X1_DATA_URI, "local": "/tmp/a.png", "hash": "abc123"},
                ],
                "before": [{"images": [PNG_1X1_BASE64_URI], "message": "hi"}],
                "after": [{"images": [PNG_1X1_DATA_URI], "message": "yo"}],
                "quote": {"images": [PNG_1X1_DATA_URI], "message": "q"},
            }
        ]
        clean = plugin._sanitize_kv_value(dirty)
        self.assertEqual(clean[0]["images"], ["https://example.com/ok.png"])
        self.assertEqual(clean[0]["image_cache"][0]["source"], "sha256:abc123")
        self.assertEqual(clean[0]["before"][0]["images"], [])
        self.assertEqual(clean[0]["after"][0]["images"], [])
        self.assertEqual(clean[0]["quote"]["images"], [])

    async def test_member_info_write_throttling_skips_unchanged_and_writes_on_change(self):
        plugin = plugin_harness()
        put_calls: list[str] = []
        orig_put = plugin.put_kv_data

        async def tracked_put(key, value):
            put_calls.append(key)
            return await orig_put(key, value)

        plugin.put_kv_data = tracked_put

        info = {"card": "AliceCard", "nickname": "Alice", "role": "member"}
        await plugin._remember_member_info("A", "g1", "u1", info, complete=True)
        self.assertEqual(len(put_calls), 1)

        # Unchanged within 24h -> skipped
        await plugin._remember_member_info("A", "g1", "u1", info, complete=True)
        self.assertEqual(len(put_calls), 1)

        # Changed card -> written immediately
        await plugin._remember_member_info("A", "g1", "u1", {**info, "card": "NewCard"}, complete=True)
        self.assertEqual(len(put_calls), 2)


if __name__ == "__main__":
    unittest.main()
