from __future__ import annotations

import asyncio
import base64
import hashlib
import re
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse
from urllib.request import Request, urlopen

from astrbot.api import logger

try:
    from .constants import *
except ImportError:
    from modules.constants import *


class DataMixin:
    async def _append_record(self, group_id: str, target: str, record: dict[str, Any]) -> None:
        key = self._record_key(group_id, target)
        records = await self.get_kv_data(key, [])
        if not isinstance(records, list):
            records = []
        cached_record = await self._cache_record_images(dict(record))
        start = max(0, len(records) - 10)
        for index in range(start, len(records)):
            item = records[index]
            if not isinstance(item, dict) or not self._records_are_duplicate(item, cached_record):
                continue
            records[index] = self._merge_duplicate_record(item, cached_record)
            self._prune_record_image_caches(records)
            await self.put_kv_data(key, records)
            await self._remember_index_key(key)
            return
        records.append(cached_record)
        max_records = self._max_records_per_target()
        dropped_records = records[:-max_records]
        records = records[-max_records:]
        for dropped in dropped_records:
            if isinstance(dropped, dict):
                self._drop_record_image_cache(dropped, delete_files=True)
        self._prune_record_image_caches(records)
        await self.put_kv_data(key, records)
        await self._remember_index_key(key)

    async def _cache_record_images(self, record: dict[str, Any]) -> dict[str, Any]:
        if self._recent_image_cache_records() <= 0:
            self._drop_record_image_cache(record, delete_files=False)
            return record

        await self._cache_record_direct_images(record)

        await self._cache_media_covers(record)

        quote = record.get("quote")
        if isinstance(quote, dict):
            quote = dict(quote)
            await self._cache_record_direct_images(quote)
            record["quote"] = quote

        for key in ("before", "after"):
            items = record.get(key)
            if not isinstance(items, list):
                continue
            cached_items = []
            for item in items:
                if isinstance(item, dict):
                    cached_items.append(await self._cache_record_images(dict(item)))
                else:
                    cached_items.append(item)
            record[key] = cached_items
        return record

    async def _cache_record_direct_images(self, record: dict[str, Any]) -> None:
        cache = record.get("image_cache")
        existing_cache = self._dedupe_image_cache_entries(list(cache) if isinstance(cache, list) else [])
        cached_sources = {
            str(item.get("source") or "").strip()
            for item in existing_cache
            if isinstance(item, dict) and str(item.get("source") or "").strip()
        }
        cached_hashes = {
            digest
            for digest in (self._image_cache_hash(item) for item in existing_cache)
            if digest
        }

        images = record.get("images") or record.get("image") or []
        if isinstance(images, str):
            images = [images]
        if not isinstance(images, list):
            return

        candidates = []
        for image in images:
            source = str(image or "").strip()
            if source and source not in cached_sources:
                candidates.append(image)

        new_cache = await self._cache_images(candidates, existing_hashes=cached_hashes)
        if existing_cache or new_cache:
            record["image_cache"] = self._dedupe_image_cache_entries([*existing_cache, *new_cache])

    async def _cache_media_covers(self, record: dict[str, Any]) -> None:
        media = record.get("media")
        if not isinstance(media, list):
            return
        for item in media:
            if not isinstance(item, dict):
                continue
            cover = str(item.get("cover") or "").strip()
            if not cover:
                continue
            cached = await self._cache_image(cover)
            if cached:
                item["cover_cache"] = cached

    async def _cache_images(self, images: Any, existing_hashes: set[str] | None = None) -> list[dict[str, str]]:
        if isinstance(images, str):
            candidates = [images]
        elif isinstance(images, list):
            candidates = images
        else:
            return []

        result = []
        seen_sources: set[str] = set()
        seen_hashes: set[str] = set(existing_hashes or set())
        for image in candidates:
            source = str(image or "").strip()
            if not source or source in seen_sources:
                continue
            seen_sources.add(source)
            cached = await self._cache_image(source)
            if not cached:
                continue
            digest = str(cached.get("hash") or "").strip()
            if digest and digest in seen_hashes:
                self._delete_cached_image(cached.get("local"))
                continue
            if digest:
                seen_hashes.add(digest)
            result.append(cached)
        return result

    async def _cache_image(self, source: str) -> dict[str, str] | None:
        try:
            data, image_type = await asyncio.to_thread(self._read_image_source, source)
            if not data:
                return None
            digest = hashlib.sha256(data).hexdigest()
            suffix = self._cached_image_suffix(data, source, image_type)
            if not suffix:
                return None
            output = self._new_message_image_cache_path(suffix)
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(data)
            return {"source": source, "local": str(output), "hash": digest}
        except Exception as exc:
            logger.debug(f"[谁艾特我] 缓存消息图片失败: {type(exc).__name__}: {exc}")
            return None

    def _dedupe_image_cache_entries(self, entries: list[Any]) -> list[dict[str, str]]:
        result: list[dict[str, str]] = []
        seen_sources: set[str] = set()
        seen_locals: set[str] = set()
        seen_hashes: set[str] = set()
        kept_locals: set[str] = set()
        for item in entries:
            if not isinstance(item, dict):
                continue
            source = str(item.get("source") or "").strip()
            local = str(item.get("local") or "").strip()
            digest = self._image_cache_hash(item)
            if digest and digest in seen_hashes:
                if local and local not in kept_locals:
                    self._delete_cached_image(local)
                continue
            if source and source in seen_sources:
                if local and local not in kept_locals:
                    self._delete_cached_image(local)
                continue
            if not source and local and local in seen_locals:
                continue
            if source:
                seen_sources.add(source)
            if local:
                seen_locals.add(local)
                kept_locals.add(local)
            if digest:
                seen_hashes.add(digest)
            result.append({k: str(v) for k, v in item.items() if v is not None})
        return result

    def _image_cache_hash(self, item: Any) -> str:
        if not isinstance(item, dict):
            return ""
        digest = str(item.get("hash") or "").strip()
        if digest:
            return digest
        local = str(item.get("local") or "").strip()
        if not local:
            return ""
        try:
            path = Path(local)
            if not path.exists() or not path.is_file():
                return ""
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            item["hash"] = digest
            return digest
        except OSError:
            return ""

    def _read_image_source(self, source: str) -> tuple[bytes, str]:
        value = str(source or "").strip()
        if not value:
            return b"", ""
        if re.match(r"^https?://", value, re.I):
            request = Request(value, headers={"User-Agent": "Mozilla/5.0"})
            with urlopen(request, timeout=10) as response:
                image_type = str(response.headers.get("Content-Type") or "")
                return self._read_limited_bytes(response), image_type
        if value.startswith("base64://"):
            return base64.b64decode(value[len("base64://") :]), "png"
        if value.lower().startswith("data:image/"):
            header, payload = value.split(",", 1)
            image_type = header.split(";", 1)[0].rsplit("/", 1)[-1]
            return base64.b64decode(payload), image_type
        if re.match(r"^file://", value, re.I):
            parsed = urlparse(value)
            path_text = unquote(parsed.path or "")
            if re.match(r"^/[A-Za-z]:/", path_text):
                path_text = path_text[1:]
            path = Path(path_text)
            return path.read_bytes(), path.suffix

        path = Path(value)
        if path.exists():
            return path.read_bytes(), path.suffix
        return b"", ""

    def _read_limited_bytes(self, response: Any, max_bytes: int = 20 * 1024 * 1024) -> bytes:
        chunks = []
        total = 0
        while True:
            chunk = response.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise ValueError("image is too large")
            chunks.append(chunk)
        return b"".join(chunks)

    def _cached_image_suffix(self, data: bytes, source: str = "", image_type: str = "") -> str:
        if data.startswith(b"\x89PNG\r\n\x1a\n"):
            return ".png"
        if data.startswith(b"\xff\xd8"):
            return ".jpg"
        if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
            return ".webp"
        if data.startswith(b"GIF87a") or data.startswith(b"GIF89a"):
            return ".gif"
        text = str(image_type or "").lower()
        if "/" in text:
            text = text.rsplit("/", 1)[-1]
        text = text.split(";", 1)[0].strip().lstrip(".")
        if text in {"png", "jpg", "jpeg", "webp", "gif"}:
            return ".jpg" if text == "jpeg" else f".{text}"
        suffix = Path(str(source or "")).suffix.lower()
        return suffix if suffix in IMAGE_MIME_TYPES else ""

    def _new_message_image_cache_path(self, suffix: str) -> Path:
        cache_dir = self._message_image_cache_dir() / datetime.now().strftime("%Y%m%d")
        suffix = suffix if suffix.startswith(".") else f".{suffix}"
        return cache_dir / f"msg_{int(time.time())}_{uuid.uuid4().hex}{suffix}"

    def _message_image_cache_dir(self) -> Path:
        try:
            from astrbot.core.utils.astrbot_path import get_astrbot_data_path

            return Path(get_astrbot_data_path()) / "plugin_data" / "astrbot_plugin_who_at_me" / "message_images"
        except Exception:
            import tempfile

            return Path(tempfile.gettempdir()) / "astrbot_plugin_who_at_me" / "message_images"

    def _prune_record_image_caches(self, records: list[dict[str, Any]]) -> None:
        keep_count = self._recent_image_cache_records()
        if keep_count <= 0:
            for record in records:
                self._drop_record_image_cache(record, delete_files=True)
            return

        image_records_seen = 0
        for record in reversed(records):
            if not self._record_has_image_content(record):
                continue
            image_records_seen += 1
            if image_records_seen > keep_count:
                self._drop_record_image_cache(record, delete_files=True)

    def _record_has_image_content(self, record: dict[str, Any]) -> bool:
        if record.get("images") or record.get("image") or record.get("image_cache"):
            return True

        quote = record.get("quote")
        if isinstance(quote, dict) and (quote.get("images") or quote.get("image") or quote.get("image_cache")):
            return True

        media = record.get("media")
        if isinstance(media, list):
            for item in media:
                if isinstance(item, dict) and (item.get("cover") or item.get("cover_cache")):
                    return True
        for key in ("before", "after"):
            items = record.get(key)
            if not isinstance(items, list):
                continue
            for item in items:
                if isinstance(item, dict) and self._record_has_image_content(item):
                    return True
        return False

    def _drop_record_image_cache(self, record: dict[str, Any], delete_files: bool) -> None:
        cache = record.pop("image_cache", None)
        if delete_files:
            self._delete_image_cache_entries(cache)

        quote = record.get("quote")
        if isinstance(quote, dict):
            cache = quote.pop("image_cache", None)
            if delete_files:
                self._delete_image_cache_entries(cache)

        media = record.get("media")
        if isinstance(media, list):
            for item in media:
                if not isinstance(item, dict):
                    continue
                cache = item.pop("cover_cache", None)
                if delete_files:
                    self._delete_image_cache_entries([cache] if isinstance(cache, dict) else cache)

        for key in ("before", "after"):
            items = record.get(key)
            if not isinstance(items, list):
                continue
            for item in items:
                if isinstance(item, dict):
                    self._drop_record_image_cache(item, delete_files=delete_files)

    def _delete_image_cache_entries(self, entries: Any) -> None:
        if not isinstance(entries, list):
            return
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            self._delete_cached_image(entry.get("local"))

    def _delete_cached_image(self, path_value: Any) -> None:
        try:
            path = Path(str(path_value or "")).resolve()
            cache_dir = self._message_image_cache_dir().resolve()
            if cache_dir == path or cache_dir not in path.parents:
                return
            path.unlink(missing_ok=True)
            self._remove_empty_cache_parents(path.parent, cache_dir)
        except OSError:
            pass

    def _remove_empty_cache_parents(self, path: Path, stop_at: Path) -> None:
        while path != stop_at and stop_at in path.parents:
            try:
                path.rmdir()
            except OSError:
                break
            path = path.parent

    async def _get_records(self, group_id: str, target: str) -> list[dict[str, Any]]:
        records = await self.get_kv_data(self._record_key(group_id, target), [])
        if not isinstance(records, list):
            return []
        return records[-self._max_records_per_target():]

    async def _get_pending_reminders(self, group_id: str, target: str) -> list[dict[str, Any]]:
        pending = await self.get_kv_data(self._reminder_pending_key(group_id, target), [])
        return pending if isinstance(pending, list) else []

    async def _remove_recalled_message(self, group_id: str, message_id: str) -> int:
        message_id = str(message_id or "").strip()
        if not group_id or not message_id:
            return 0

        removed = 0
        touched_targets: set[str] = set()
        keys = await self.get_kv_data(INDEX_KEY, [])
        if not isinstance(keys, list):
            keys = []
        prefix = f"records:{group_id}:"

        for key in list(keys):
            if not isinstance(key, str) or not key.startswith(prefix):
                continue
            records = await self.get_kv_data(key, [])
            if not isinstance(records, list):
                continue
            next_records, count = self._remove_recalled_from_records(records, message_id)
            if not count:
                continue
            target = key[len(prefix) :]
            touched_targets.add(target)
            removed += count
            await self.put_kv_data(key, next_records)

        for target in touched_targets:
            pending_key = self._reminder_pending_key(group_id, target)
            pending = await self.get_kv_data(pending_key, [])
            if not isinstance(pending, list):
                continue
            next_pending, count = self._remove_recalled_from_records(pending, message_id)
            if not count:
                continue
            removed += count
            if next_pending:
                await self.put_kv_data(pending_key, next_pending)
            else:
                await self.delete_kv_data(pending_key)

        cache = self.before_cache.get(group_id, []) if hasattr(self, "before_cache") else []
        if isinstance(cache, list):
            next_cache, count = self._remove_recalled_from_records(cache, message_id)
            if count:
                removed += count
                self.before_cache[group_id] = next_cache
        return removed

    def _remove_recalled_from_records(
        self,
        records: list[dict[str, Any]],
        message_id: str,
    ) -> tuple[list[dict[str, Any]], int]:
        kept: list[dict[str, Any]] = []
        removed = 0
        for record in records:
            if not isinstance(record, dict):
                kept.append(record)
                continue
            if self._record_has_message_id(record, message_id):
                self._drop_record_image_cache(record, delete_files=True)
                removed += 1
                continue

            item = dict(record)
            for key in ("before", "after"):
                value = item.get(key)
                if not isinstance(value, list):
                    continue
                nested, count = self._remove_recalled_from_records(value, message_id)
                if count:
                    item[key] = nested
                    removed += count

            quote = item.get("quote")
            if isinstance(quote, dict) and self._record_has_message_id(quote, message_id):
                self._drop_record_image_cache({"quote": quote}, delete_files=True)
                item.pop("quote", None)
                removed += 1
            kept.append(item)
        return kept, removed

    def _record_has_message_id(self, record: dict[str, Any], message_id: str) -> bool:
        target = str(message_id or "").strip()
        if not target:
            return False
        for key in ("message_id", "messageId", "msg_id", "msgId", "id", "real_id", "realId"):
            if str(record.get(key) or "").strip() == target:
                return True
        order = self._record_order(record)
        return order is not None and str(order) == target

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

    async def _target_name(self, event: AstrMessageEvent, group_id: str, target: str) -> str:
        if target == self._sender_id(event):
            sender_name = self._sender_name(event)
            if sender_name and not self._looks_like_numeric_id(sender_name):
                return sender_name
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
