from __future__ import annotations

import asyncio
import base64
import copy
from contextlib import asynccontextmanager
import hashlib
import ipaddress
import re
import socket
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

from astrbot.api import logger

try:
    from .constants import *
except ImportError:
    from modules.constants import *


class _NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class DataMixin:
    def _data_gate(self) -> asyncio.Condition:
        condition = getattr(self, "_data_gate_condition", None)
        if condition is None:
            condition = asyncio.Condition()
            self._data_gate_condition = condition
            self._data_gate_readers = 0
            self._data_gate_writer = False
            self._data_gate_waiting_writers = 0
            self._data_operation_depths = {}
            self._data_maintenance_owner = None
            self._data_maintenance_depth = 0
        return condition

    @asynccontextmanager
    async def _data_operation(self):
        condition = self._data_gate()
        task = asyncio.current_task()
        if self._data_maintenance_owner is task:
            yield
            return

        depth = self._data_operation_depths.get(task, 0)
        if depth:
            self._data_operation_depths[task] = depth + 1
            try:
                yield
            finally:
                self._data_operation_depths[task] -= 1
            return

        acquired = False
        try:
            async with condition:
                while self._data_gate_writer or self._data_gate_waiting_writers:
                    await condition.wait()
                self._data_gate_readers += 1
                self._data_operation_depths[task] = 1
                acquired = True
            yield
        finally:
            if acquired:
                self._data_operation_depths.pop(task, None)
                async with condition:
                    self._data_gate_readers -= 1
                    if self._data_gate_readers == 0:
                        condition.notify_all()

    @asynccontextmanager
    async def _data_maintenance(self):
        condition = self._data_gate()
        task = asyncio.current_task()
        if self._data_maintenance_owner is task:
            self._data_maintenance_depth += 1
            try:
                yield
            finally:
                self._data_maintenance_depth -= 1
            return

        acquired = False
        try:
            async with condition:
                self._data_gate_waiting_writers += 1
                try:
                    while self._data_gate_writer or self._data_gate_readers:
                        await condition.wait()
                    self._data_gate_writer = True
                    self._data_maintenance_owner = task
                    self._data_maintenance_depth = 1
                    acquired = True
                finally:
                    self._data_gate_waiting_writers -= 1
            yield
        finally:
            if acquired:
                async with condition:
                    self._data_maintenance_depth = 0
                    self._data_maintenance_owner = None
                    self._data_gate_writer = False
                    condition.notify_all()
    def _kv_lock(self, key: str) -> asyncio.Lock:
        locks = getattr(self, "_kv_locks", None)
        if not isinstance(locks, dict):
            locks = {}
            setattr(self, "_kv_locks", locks)
        lock = locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            locks[key] = lock
        return lock

    def _preferences_write_lock(self) -> asyncio.Lock:
        lock = getattr(self, "_preferences_write_lock_instance", None)
        if lock is None:
            lock = asyncio.Lock()
            setattr(self, "_preferences_write_lock_instance", lock)
        return lock

    async def put_kv_data(self, key: str, value: Any) -> Any:
        return await self._run_kv_write(super().put_kv_data, key, value)

    async def delete_kv_data(self, key: str) -> Any:
        return await self._run_kv_write(super().delete_kv_data, key)

    async def _run_kv_write(self, operation: Any, key: str, *args: Any) -> Any:
        delays = (0.05, 0.1, 0.2, 0.4, 0.8)
        async with self._preferences_write_lock():
            for attempt in range(len(delays) + 1):
                try:
                    return await operation(key, *args)
                except Exception as exc:
                    if not self._is_database_locked_error(exc) or attempt >= len(delays):
                        raise
                    delay = delays[attempt]
                    logger.warning(
                        "[who_at_me] preferences database locked; "
                        f"retrying write for {key} in {delay:.2f}s ({attempt + 1}/{len(delays)})"
                    )
                    await asyncio.sleep(delay)
        raise RuntimeError("unreachable")

    def _is_database_locked_error(self, exc: Exception) -> bool:
        return "database is locked" in str(exc).lower()

    async def _append_record(self, group_id: str, target: str, record: dict[str, Any]) -> None:
        cached_record = await self._cache_record_images(dict(record))
        async with self._data_operation():
            await self._append_record_locked(group_id, target, cached_record)

    async def _append_record_locked(
        self,
        group_id: str,
        target: str,
        cached_record: dict[str, Any],
    ) -> None:
        key = self._record_key(group_id, target)
        async with self._kv_lock(key):
            records = await self.get_kv_data(key, [])
            if not isinstance(records, list):
                records = []
            start = max(0, len(records) - 10)
            for index in range(start, len(records)):
                item = records[index]
                if not isinstance(item, dict) or not self._records_are_duplicate(item, cached_record):
                    continue
                records[index] = self._merge_duplicate_record(item, cached_record)
                pruned_cache_records = self._prune_record_image_caches(records)
                await self.put_kv_data(key, records)
                await self._remember_index_key(key)
                self._drop_records_image_cache(pruned_cache_records, delete_files=True)
                return
            records.append(cached_record)
            max_records = self._max_records_per_target()
            dropped_records = records[:-max_records]
            records = records[-max_records:]
            pruned_cache_records = self._prune_record_image_caches(records)
            await self.put_kv_data(key, records)
            await self._remember_index_key(key)
            self._drop_records_image_cache(dropped_records, delete_files=True)
            self._drop_records_image_cache(pruned_cache_records, delete_files=True)

    async def _cache_record_images(self, record: dict[str, Any], *, force: bool = False) -> dict[str, Any]:
        if self._recent_image_cache_records() <= 0 and not force:
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
                    cached_items.append(await self._cache_record_images(dict(item), force=force))
                else:
                    cached_items.append(item)
            record[key] = cached_items
        return record

    async def _cache_record_direct_images(self, record: dict[str, Any]) -> None:
        cache = record.get("image_cache")
        existing_cache = self._dedupe_image_cache_entries(list(cache) if isinstance(cache, list) else [])
        existing_cache = [
            item
            for item in existing_cache
            if str(item.get("local") or "").strip() and Path(str(item["local"])).is_file()
        ]
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
        else:
            record.pop("image_cache", None)

    async def _prepare_records_for_render(
        self,
        records: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], set[str]]:
        prepared = []
        temporary_paths: set[str] = set()
        for record in records:
            copied = copy.deepcopy(record)
            original_paths = self._record_image_cache_paths(copied)
            copied = await self._cache_record_images(copied, force=True)
            temporary_paths.update(self._record_image_cache_paths(copied) - original_paths)
            prepared.append(copied)
        return prepared, temporary_paths

    def _record_image_cache_paths(self, record: dict[str, Any]) -> set[str]:
        result: set[str] = set()

        def collect(entries: Any) -> None:
            if not isinstance(entries, list):
                return
            for entry in entries:
                if isinstance(entry, dict):
                    local = str(entry.get("local") or "").strip()
                    if local:
                        result.add(local)

        collect(record.get("image_cache"))
        quote = record.get("quote")
        if isinstance(quote, dict):
            collect(quote.get("image_cache"))
        media = record.get("media")
        if isinstance(media, list):
            for item in media:
                if isinstance(item, dict):
                    cache = item.get("cover_cache")
                    collect([cache] if isinstance(cache, dict) else cache)
        for key in ("before", "after"):
            items = record.get(key)
            if isinstance(items, list):
                for item in items:
                    if isinstance(item, dict):
                        result.update(self._record_image_cache_paths(item))
        return result

    def _delete_cached_image_paths(self, paths: set[str]) -> None:
        for path in paths:
            self._delete_cached_image(path)

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
            logger.debug(f"[璋佽壘鐗规垜] 缂撳瓨娑堟伅鍥剧墖澶辫触: {type(exc).__name__}: {exc}")
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
            if not self._is_allowed_remote_image_url(value):
                return b"", ""
            request = Request(value, headers={"User-Agent": "Mozilla/5.0"})
            opener = build_opener(_NoRedirectHandler())
            with opener.open(request, timeout=10) as response:
                image_type = str(response.headers.get("Content-Type") or "")
                if image_type and not image_type.lower().startswith("image/"):
                    return b"", ""
                return self._read_limited_bytes(response, MAX_IMAGE_SOURCE_BYTES), image_type
        if value.startswith("base64://"):
            return self._decode_inline_image(value)
        if value.lower().startswith("data:image/"):
            return self._decode_inline_image(value)
        if re.match(r"^file://", value, re.I):
            parsed = urlparse(value)
            path_text = unquote(parsed.path or "")
            if re.match(r"^/[A-Za-z]:/", path_text):
                path_text = path_text[1:]
            return self._read_local_image_path(Path(path_text))

        path = Path(value)
        if path.exists():
            return self._read_local_image_path(path)
        return b"", ""

    def _inline_image_source_within_limit(self, value: str) -> bool:
        text = str(value or "").strip()
        if text.startswith("base64://"):
            payload = text[len("base64://") :]
        elif text.lower().startswith("data:image/") and "," in text:
            header, payload = text.split(",", 1)
            if ";base64" not in header.lower():
                return False
        else:
            return False
        encoded_limit = ((MAX_IMAGE_SOURCE_BYTES + 2) // 3) * 4
        return len(re.sub(r"\s+", "", payload)) <= encoded_limit

    def _decode_inline_image(self, value: str) -> tuple[bytes, str]:
        text = str(value or "").strip()
        image_type = "png"
        if text.startswith("base64://"):
            payload = text[len("base64://") :]
        elif text.lower().startswith("data:image/") and "," in text:
            header, payload = text.split(",", 1)
            if ";base64" not in header.lower():
                return b"", ""
            image_type = header.split(";", 1)[0].rsplit("/", 1)[-1]
        else:
            return b"", ""
        payload = re.sub(r"\s+", "", payload)
        if not self._inline_image_source_within_limit(text):
            raise ValueError("inline image is too large")
        data = base64.b64decode(payload, validate=True)
        if len(data) > MAX_IMAGE_SOURCE_BYTES:
            raise ValueError("inline image is too large")
        return data, image_type

    def _is_allowed_remote_image_url(self, value: str) -> bool:
        parsed = urlparse(value)
        if parsed.scheme.lower() not in {"http", "https"}:
            return False
        host = parsed.hostname
        if not host:
            return False
        host_text = host.lower().strip("[]")
        if host_text in {"localhost", "localhost.localdomain"} or host_text.endswith(".local"):
            return False
        try:
            infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
        except OSError:
            return False
        for info in infos:
            try:
                ip = ipaddress.ip_address(info[4][0])
            except ValueError:
                return False
            if not ip.is_global:
                return False
        return True

    def _read_local_image_path(self, path: Path) -> tuple[bytes, str]:
        try:
            resolved = path.expanduser().resolve()
            if not resolved.is_file() or not self._is_allowed_local_image_path(resolved):
                return b"", ""
            if resolved.stat().st_size > MAX_IMAGE_SOURCE_BYTES:
                return b"", ""
            data = resolved.read_bytes()
            return (data, resolved.suffix) if len(data) <= MAX_IMAGE_SOURCE_BYTES else (b"", "")
        except OSError:
            return b"", ""

    def _is_allowed_local_image_path(self, path: Path) -> bool:
        for root in self._allowed_local_image_roots():
            if path == root or root in path.parents:
                return True
        return False

    def _allowed_local_image_roots(self) -> list[Path]:
        roots: list[Path] = [self._message_image_cache_dir()]
        plugin_dir = getattr(self, "_plugin_data_dir", None)
        if callable(plugin_dir):
            roots.append(Path(plugin_dir()))
        try:
            from astrbot.core.utils.astrbot_path import get_astrbot_data_path

            roots.append(Path(get_astrbot_data_path()) / "temp")
        except Exception:
            pass

        allowed = []
        for root in roots:
            try:
                allowed.append(Path(root).expanduser().resolve())
            except OSError:
                continue
        return allowed

    def _read_limited_bytes(self, response: Any, max_bytes: int = MAX_IMAGE_SOURCE_BYTES) -> bytes:
        content_length = str(response.headers.get("Content-Length") or "").strip()
        if content_length.isdigit() and int(content_length) > max_bytes:
            raise ValueError("image is too large")
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
        plugin_dir = getattr(self, "_plugin_data_dir", None)
        if callable(plugin_dir):
            return Path(plugin_dir()) / "message_images"
        try:
            from astrbot.api.star import StarTools

            return Path(StarTools.get_data_dir(PLUGIN_NAME)) / "message_images"
        except Exception:
            try:
                from astrbot.core.utils.astrbot_path import get_astrbot_data_path

                return Path(get_astrbot_data_path()) / "plugin_data" / PLUGIN_NAME / "message_images"
            except Exception:
                return Path.cwd() / "data" / PLUGIN_NAME / "message_images"

    def _prune_record_image_caches(self, records: list[dict[str, Any]]) -> list[dict[str, Any]]:
        pruned_cache_records: list[dict[str, Any]] = []
        keep_count = self._recent_image_cache_records()
        if keep_count <= 0:
            for record in records:
                if not isinstance(record, dict) or not self._record_has_image_content(record):
                    continue
                pruned_cache_records.append(copy.deepcopy(record))
                self._drop_record_image_cache(record, delete_files=False)
            return pruned_cache_records

        image_records_seen = 0
        for record in reversed(records):
            if not isinstance(record, dict) or not self._record_has_image_content(record):
                continue
            image_records_seen += 1
            if image_records_seen > keep_count:
                pruned_cache_records.append(copy.deepcopy(record))
                self._drop_record_image_cache(record, delete_files=False)
        return pruned_cache_records

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

    def _drop_records_image_cache(self, records: Any, delete_files: bool) -> None:
        if not isinstance(records, list):
            return
        for record in records:
            if isinstance(record, dict):
                self._drop_record_image_cache(record, delete_files=delete_files)

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

    async def _cleanup_expired_image_caches(self, hours: int = 24) -> int:
        \"\"\"
        清理过期的图片缓存，hours 为保留小时数（目前按天级文件夹清理）
        \"\"\"
        try:
            cache_dir = self._message_image_cache_dir()
            if not cache_dir.exists() or not cache_dir.is_dir():
                return 0

            # 计算 24 小时前的时间字符串 (YYYYMMDD)
            cutoff_ts = time.time() - (hours * 3600)
            cutoff_date_str = datetime.fromtimestamp(cutoff_ts).strftime("%Y%m%d")
            
            removed_count = 0

            # 遍历日期目录 (格式: YYYYMMDD)
            for item in cache_dir.iterdir():
                if not item.is_dir() or not re.match(r"^\d{8}$", item.name):
                    continue

                # 如果文件夹名称（日期）早于截止日期字符串，说明是过期文件夹
                if item.name < cutoff_date_str:
                    try:
                        # 异步删除整个目录
                        await asyncio.to_thread(self._delete_directory_recursive, item)
                        removed_count += 1
                    except Exception:
                        continue

            if removed_count > 0:
                logger.info(f"[谁艾特我] 已清理 {removed_count} 个 24 小时前的过期图片缓存目录")
            return removed_count
        except Exception as exc:
            logger.debug(f"[谁艾特我] 清理过期缓存失败: {exc}")
            return 0

    def _delete_directory_recursive(self, path: Path) -> None:
        if not path.exists():
            return
        for item in path.iterdir():
            if item.is_dir():
                self._delete_directory_recursive(item)
            else:
                try:
                    item.unlink(missing_ok=True)
                except OSError:
                    pass
        try:
            path.rmdir()
        except OSError:
            pass


    async def _get_records(self, group_id: str, target: str) -> list[dict[str, Any]]:
        records = await self.get_kv_data(self._record_key(group_id, target), [])
        if not isinstance(records, list):
            return []
        return records[-self._max_records_per_target():]

    async def _get_pending_reminders(self, group_id: str, target: str) -> list[dict[str, Any]]:
        pending = await self.get_kv_data(self._reminder_pending_key(group_id, target), [])
        return pending if isinstance(pending, list) else []

    async def _remove_recalled_message(self, group_id: str, message_id: str) -> int:
        async with self._data_operation():
            return await self._remove_recalled_message_locked(group_id, message_id)

    async def _remove_recalled_message_locked(self, group_id: str, message_id: str) -> int:
        message_id = str(message_id or "").strip()
        if not group_id or not message_id:
            return 0

        removed = 0
        async with self._kv_lock(INDEX_KEY):
            keys = await self.get_kv_data(INDEX_KEY, [])
        if not isinstance(keys, list):
            keys = []
        prefix = f"records:{group_id}:"

        for key in list(keys):
            if not isinstance(key, str) or not key.startswith(prefix):
                continue
            async with self._kv_lock(key):
                records = await self.get_kv_data(key, [])
                if not isinstance(records, list):
                    continue
                removed_cache_records: list[dict[str, Any]] = []
                next_records, count = self._remove_recalled_from_records(
                    records,
                    message_id,
                    removed_cache_records,
                )
                if not count:
                    continue
                removed += count
                if next_records:
                    await self.put_kv_data(key, next_records)
                else:
                    await self.delete_kv_data(key)
                    await self._forget_index_key(key)
                self._drop_records_image_cache(removed_cache_records, delete_files=True)

        async with self._kv_lock(REMINDER_PENDING_INDEX_KEY):
            pending_keys = await self.get_kv_data(REMINDER_PENDING_INDEX_KEY, [])
        if not isinstance(pending_keys, list):
            pending_keys = []
        pending_prefix = f"reminder:pending:{group_id}:"

        for pending_key in list(pending_keys):
            if not isinstance(pending_key, str) or not pending_key.startswith(pending_prefix):
                continue
            async with self._kv_lock(pending_key):
                pending = await self.get_kv_data(pending_key, [])
                if not isinstance(pending, list):
                    continue
                removed_cache_records = []
                next_pending, count = self._remove_recalled_from_records(
                    pending,
                    message_id,
                    removed_cache_records,
                )
                if not count:
                    continue
                removed += count
                if next_pending:
                    await self.put_kv_data(pending_key, next_pending)
                else:
                    await self.delete_kv_data(pending_key)
                    await self._forget_pending_key(pending_key)
                self._drop_records_image_cache(removed_cache_records, delete_files=True)

        cache = self.before_cache.get(group_id, []) if hasattr(self, "before_cache") else []
        if isinstance(cache, list):
            removed_cache_records = []
            next_cache, count = self._remove_recalled_from_records(
                cache,
                message_id,
                removed_cache_records,
            )
            if count:
                removed += count
                self.before_cache[group_id] = next_cache
                self._drop_records_image_cache(removed_cache_records, delete_files=True)
        return removed

    def _remove_recalled_from_records(
        self,
        records: list[dict[str, Any]],
        message_id: str,
        removed_cache_records: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], int]:
        kept: list[dict[str, Any]] = []
        removed = 0
        for record in records:
            if not isinstance(record, dict):
                kept.append(record)
                continue
            if self._record_has_message_id(record, message_id):
                removed_cache_records.append(record)
                removed += 1
                continue

            item = dict(record)
            for key in ("before", "after"):
                value = item.get(key)
                if not isinstance(value, list):
                    continue
                nested, count = self._remove_recalled_from_records(
                    value,
                    message_id,
                    removed_cache_records,
                )
                if count:
                    item[key] = nested
                    removed += count

            quote = item.get("quote")
            if isinstance(quote, dict) and self._record_has_message_id(quote, message_id):
                removed_cache_records.append({"quote": quote})
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
        await self._remember_member_info(group_id, user_id, info, complete=bool(info.get("role")))

    async def _remember_member_info(
        self,
        group_id: str,
        user_id: str,
        info: dict[str, Any],
        *,
        complete: bool = False,
    ) -> None:
        if not group_id or not user_id or not isinstance(info, dict):
            return
        data = {
            "card": str(info.get("card") or ""),
            "nickname": str(info.get("nickname") or info.get("name") or ""),
            "name": str(info.get("name") or ""),
            "role": str(info.get("role") or ""),
            "level": str(info.get("level") or ""),
            "title": str(info.get("title") or ""),
            "member_title": str(info.get("member_title") or ""),
            "complete": bool(complete),
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
            if key not in {"time", "complete"} and value and not info.get(key):
                info[key] = value
        cache_time = int(cached.get("time") or 0)
        if (
            self._member_info_has_name(info)
            and bool(cached.get("complete"))
            and time.time() - cache_time <= MEMBER_CACHE_TTL_SECONDS
        ):
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
            await self._remember_member_info(group_id, user_id, info, complete=True)
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
        group_status = "寮€鍚? if await self._reminder_group_enabled(event, group_id) else "鍏抽棴"
        user_status = "寮€鍚? if await self._reminder_user_enabled(group_id, sender_id) else "鍏抽棴"
        context_status = "寮€鍚? if context_config.get("enabled") else "鍏抽棴"
        pending_count = len(await self._get_pending_reminders(group_id, sender_id)) if sender_id else 0
        current_umo = self._event_umo(event)
        global_umos = self._global_enabled_group_umos()
        enabled_umos = self._reminder_enabled_group_umos()
        global_status = "鏈厤缃悕鍗? if not global_umos else ("宸插懡涓? if current_umo in global_umos else "鏈懡涓?)
        umo_status = "鏈厤缃悕鍗? if not enabled_umos else ("宸插懡涓? if current_umo in enabled_umos else "鏈懡涓?)
        user_rule_status = self._reminder_user_rule_status(sender_id)
        return (
            "鑹剧壒鎻愰啋鐘舵€侊細\n"
            f"鏈兢鎻愰啋锛歿group_status}\n"
            f"褰撳墠 UMO锛歿current_umo or '鏈煡'}\n"
            f"鍏ㄥ眬鐧藉悕鍗曪細{global_status}\n"
            f"UMO 鍚嶅崟锛歿umo_status}\n"
            f"鐢ㄦ埛鍚嶅崟锛歿user_rule_status}\n"
            f"浣犵殑鎻愰啋锛歿user_status}\n"
            f"鎻愰啋涓婁笅鏂囷細{context_status}锛堝墠 {context_config.get('before', 0)} / 鍚?{context_config.get('after', 0)}锛塡n"
            f"鎻愰啋鏉′欢锛氳壘鐗瑰悗 {self._reminder_away_seconds() // 60} 鍒嗛挓锛屼笖鏈熼棿鑷冲皯 {self._reminder_min_messages()} 鏉＄兢娑堟伅\n"
            f"寰呮彁閱掕褰曪細{pending_count} 鏉?
        )

    async def _context_enabled(self, group_id: str) -> bool:
        return bool(await self.get_kv_data(self._context_key(group_id), False))

    async def _set_context(self, group_id: str, enabled: bool) -> None:
        async with self._data_operation():
            key = self._context_key(group_id)
            async with self._kv_lock(CONTEXT_INDEX_KEY):
                context_keys = await self.get_kv_data(CONTEXT_INDEX_KEY, [])
                if not isinstance(context_keys, list):
                    context_keys = []
                if enabled:
                    await self.put_kv_data(key, True)
                    if key not in context_keys:
                        context_keys.append(key)
                        await self.put_kv_data(CONTEXT_INDEX_KEY, context_keys)
                else:
                    await self.delete_kv_data(key)
                    if key in context_keys:
                        context_keys.remove(key)
                        if context_keys:
                            await self.put_kv_data(CONTEXT_INDEX_KEY, context_keys)
                        else:
                            await self.delete_kv_data(CONTEXT_INDEX_KEY)

    async def _remember_index_key(self, key: str) -> None:
        async with self._kv_lock(INDEX_KEY):
            keys = await self.get_kv_data(INDEX_KEY, [])
            if not isinstance(keys, list):
                keys = []
            if key not in keys:
                keys.append(key)
                await self.put_kv_data(INDEX_KEY, keys)

    async def _forget_index_key(self, key: str) -> None:
        async with self._kv_lock(INDEX_KEY):
            keys = await self.get_kv_data(INDEX_KEY, [])
            if not isinstance(keys, list):
                return
            if key in keys:
                keys.remove(key)
                await self.put_kv_data(INDEX_KEY, keys)

    async def _remember_pending_key(self, key: str) -> None:
        async with self._kv_lock(REMINDER_PENDING_INDEX_KEY):
            keys = await self.get_kv_data(REMINDER_PENDING_INDEX_KEY, [])
            if not isinstance(keys, list):
                keys = []
            if key not in keys:
                keys.append(key)
                await self.put_kv_data(REMINDER_PENDING_INDEX_KEY, keys)

    async def _forget_pending_key(self, key: str) -> None:
        async with self._kv_lock(REMINDER_PENDING_INDEX_KEY):
            keys = await self.get_kv_data(REMINDER_PENDING_INDEX_KEY, [])
            if not isinstance(keys, list):
                return
            if key in keys:
                keys.remove(key)
                await self.put_kv_data(REMINDER_PENDING_INDEX_KEY, keys)

    async def _pending_index_keys(self) -> list[str]:
        async with self._kv_lock(REMINDER_PENDING_INDEX_KEY):
            keys = await self.get_kv_data(REMINDER_PENDING_INDEX_KEY, [])
        if not isinstance(keys, list):
            return []
        return [key for key in keys if isinstance(key, str)]

    async def _target_name(self, event: AstrMessageEvent, group_id: str, target: str) -> str:
        if target == self._sender_id(event):
            sender_name = self._sender_name(event)
            if sender_name and not self._looks_like_numeric_id(sender_name):
                return sender_name
        if target == ALL_TARGET:
            return "鍏ㄤ綋鎴愬憳"
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
        return "鎴?

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
            logger.debug(f"[璋佽壘鐗规垜] 鑾峰彇缇や俊鎭け璐? {exc}")
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
                logger.debug(f"[璋佽壘鐗规垜] 璋冪敤鍗忚绔?API {action} 澶辫触: {exc}")
        except Exception as exc:
            logger.debug(f"[璋佽壘鐗规垜] 璋冪敤鍗忚绔?API {action} 澶辫触: {exc}")
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
