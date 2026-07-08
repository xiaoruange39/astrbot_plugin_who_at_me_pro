from __future__ import annotations

import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from astrbot.api import logger

try:
    from .constants import *
except ImportError:
    from modules.constants import *


class MessageMixin:
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
        images = []
        raw_segments = self._raw_message_segments(event)
        if raw_segments:
            images.extend(self._segments_images(raw_segments))
        images.extend(self._segments_images(self._message_chain(event)))
        for text in self._raw_message_texts(event):
            images.extend(self._images_from_cq(text))
        return self._unique_strings(images)

    def _raw_message_texts(self, event: AstrMessageEvent) -> list[str]:
        result = []
        raw = getattr(event.message_obj, "raw_message", None)
        values = [
            raw,
            getattr(event.message_obj, "message_str", None),
            getattr(event, "message_str", None),
        ]
        if isinstance(raw, dict):
            values.extend(
                self._first_mapping_value(raw, [key])
                for key in ("raw_message", "rawMessage", "message", "message_str", "messageStr", "content")
            )
        for value in values:
            if isinstance(value, str) and value.strip():
                result.append(value)
        return self._unique_strings(result)

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
        if not text:
            text = self._cq_media_summary(" ".join(self._raw_message_texts(event)))
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
            else:
                summary = self._segment_media_summary(segment)
                if summary:
                    texts.append(summary)
        return "".join(texts).strip()

    def _segments_images(self, segments: list[Any]) -> list[str]:
        urls = []
        for segment in segments:
            if self._is_reference_segment(segment):
                continue
            seg_type = self._segment_type(segment)
            if seg_type not in {"image", "mface", "market_face", "marketface", "video", "shortvideo"}:
                continue
            names = [
                "url",
                "file",
                "path",
                "file_path",
                "filePath",
                "local_path",
                "localPath",
                "src",
                "image",
            ]
            if seg_type in {"video", "shortvideo"}:
                names = [
                    "cover",
                    "cover_url",
                    "coverUrl",
                    "thumbnail",
                    "thumb",
                    "preview",
                    "poster",
                    "image",
                ]
            value = self._segment_value(
                segment,
                names,
            )
            if value:
                urls.append(str(value))
                continue
            data = self._segment_data(segment)
            base64_value = self._first_mapping_value(data, ["base64"]) if data else None
            if base64_value:
                text = str(base64_value).strip()
                if text:
                    urls.append(text if text.startswith(("base64://", "data:image/")) else f"base64://{text}")
        return self._unique_strings(urls)

    def _segment_media_summary(self, segment: Any) -> str:
        seg_type = self._segment_type(segment)
        if seg_type in {"image"}:
            return ""
        if seg_type in {"mface", "market_face", "marketface", "face", "emoji"}:
            return "[表情]"
        if seg_type in {"video", "shortvideo"}:
            return "[视频]"
        if seg_type in {"record", "voice", "audio"}:
            return "[语音]"
        if seg_type == "file":
            name = self._segment_value(segment, ["name", "file_name", "fileName", "filename", "file"])
            return f"[文件] {name}" if name else "[文件]"
        if seg_type in {"json", "xml", "card", "share"}:
            value = self._segment_value(
                segment,
                ["title", "desc", "description", "summary", "text", "content", "message"],
            )
            if value:
                return self._strip_cq_display(str(value))
            return "[卡片消息]"
        return ""

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

    def _cq_media_summary(self, text: str) -> str:
        summaries = []
        for seg_type in re.findall(r"\[CQ:([^,\]]+)", text or ""):
            seg_type = seg_type.lower()
            if seg_type in {"image"}:
                continue
            if seg_type in {"mface", "market_face", "face", "emoji"}:
                summaries.append("[表情]")
            elif seg_type in {"video", "shortvideo"}:
                summaries.append("[视频]")
            elif seg_type in {"record", "voice", "audio"}:
                summaries.append("[语音]")
            elif seg_type == "file":
                summaries.append("[文件]")
            elif seg_type in {"json", "xml", "card", "share"}:
                summaries.append("[卡片消息]")
        return " ".join(self._unique_strings(summaries))

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
        for match in re.finditer(r"\[CQ:(image|mface|market_face|video|shortvideo),([^\]]+)\]", text):
            seg_type = match.group(1).lower()
            attrs = match.group(2)
            data = self._parse_cq_attrs(attrs)
            names = (
                ["cover", "cover_url", "coverUrl", "thumbnail", "thumb", "preview", "poster", "image"]
                if seg_type in {"video", "shortvideo"}
                else ["url", "file", "path", "file_path", "local_path", "src", "image"]
            )
            value = self._first_mapping_value(data, names)
            if value:
                images.append(value)
                continue
            base64_value = data.get("base64")
            if base64_value:
                images.append(
                    base64_value
                    if base64_value.startswith(("base64://", "data:image/"))
                    else f"base64://{base64_value}"
                )
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

    def _record_renderable_images(self, data: dict[str, Any]) -> list[str]:
        candidates: list[Any] = []
        cached_sources = set()
        cache = data.get("image_cache")
        if isinstance(cache, list):
            for item in cache:
                candidates.append(item)
                if isinstance(item, dict):
                    source = str(item.get("source") or "").strip()
                    if source:
                        cached_sources.add(source)

        images = data.get("images") or data.get("image") or []
        if isinstance(images, str):
            images = [images]
        if isinstance(images, list):
            for image in images:
                source = str(image or "").strip()
                if source and source not in cached_sources:
                    candidates.append(image)
        return self._renderable_images(candidates)

    def _renderable_image(self, image: Any) -> str:
        if isinstance(image, dict):
            source = str(image.get("source") or "").strip()
            for key in ("local", "path", "file", "url", "source"):
                value = self._renderable_image(image.get(key))
                if value:
                    return value
            return self._renderable_image(source)

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
