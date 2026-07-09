from __future__ import annotations

import base64
import json
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

IMAGE_SEGMENT_TYPES = {
    "image",
    "picture",
    "mface",
    "market_face",
    "marketface",
    "pic",
    "photo",
    "sticker",
    "bface",
    "sface",
}
IMAGE_SOURCE_KEYS = [
    "url",
    "image_url",
    "imageUrl",
    "file_url",
    "fileUrl",
    "origin_url",
    "originUrl",
    "download_url",
    "downloadUrl",
    "resource_url",
    "resourceUrl",
    "preview_url",
    "previewUrl",
    "thumb_url",
    "thumbUrl",
    "thumbnail",
    "thumb",
    "big_url",
    "bigUrl",
    "static_url",
    "staticUrl",
    "path",
    "file_path",
    "filePath",
    "local_path",
    "localPath",
    "src",
    "source",
    "image",
    "image_file",
    "imageFile",
    "file",
    "file_id",
    "fileId",
    "image_id",
    "imageId",
    "file_unique",
    "fileUnique",
    "file_uuid",
    "fileUuid",
]
VIDEO_COVER_KEYS = ["cover", "cover_url", "coverUrl", "thumbnail", "thumb", "preview", "poster", "image"]
CHAT_RECORD_TEXT = "[聊天记录]"
CHAT_RECORD_SEGMENT_TYPES = {"forward", "forward_msg", "merged_forward", "multimsg"}
RECALL_NOTICE_TYPES = {"group_recall", "friend_recall", "recall", "message_recall", "revoke", "message_revoke"}


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
        images = await self._images(event)
        media = self._media(event)
        message = self._message_text_for_record(event, mentions or [])
        poke = await self._poke_message(event, group_id, sender_name)
        record = {
            "user_id": sender_id,
            "message": message,
            "images": images,
            "media": media,
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
        if mentions:
            record["at_targets"] = [str(item) for item in mentions]
            record["at_after_image"] = self._mention_after_image(event, mentions)
            if not record["at_after_image"]:
                message_after_images = self._message_after_images(event, mentions)
                if message_after_images:
                    record["message_after_images"] = message_after_images
        if poke:
            record["poke"] = poke
        if quote:
            record["quote"] = quote
        self._log_record_image_diagnostics(
            event,
            group_id,
            record,
            mentions or [],
            kind="mention" if mentions else "context",
        )
        return record

    async def _context_message(
        self,
        event: AstrMessageEvent,
        group_id: str,
        mentions: list[str] | None = None,
        member_info: dict[str, Any] | None = None,
        quote: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        record = await self._mention_record(event, group_id, mentions=mentions, member_info=member_info, quote=quote)
        context = {
            "user_id": record["user_id"],
            "message": record["message"],
            "images": record["images"],
            "media": record.get("media") or [],
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
        if record.get("at_targets"):
            context["at_targets"] = record["at_targets"]
        if record.get("at_after_image"):
            context["at_after_image"] = record["at_after_image"]
        if record.get("message_after_images"):
            context["message_after_images"] = record["message_after_images"]
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

    async def _images(self, event: AstrMessageEvent) -> list[str]:
        images = []
        raw_segments = self._raw_message_segments(event)
        chain_segments = self._message_chain(event)
        if raw_segments:
            images.extend(self._segments_images(raw_segments))
        images.extend(self._segments_images(chain_segments))
        for text in self._raw_message_texts(event):
            images.extend(self._images_from_cq(text))
        candidates = self._unique_strings(images)
        resolved = await self._resolve_image_sources(event, candidates)
        raw_texts = self._raw_message_texts(event)
        if not resolved and self._has_image_debug_hint([*raw_segments, *chain_segments, *raw_texts]):
            logger.warning(
                "[who_at_me] image extraction empty; "
                f"raw={self._segments_debug_summary(raw_segments)} "
                f"chain={self._segments_debug_summary(chain_segments)} "
                f"raw_texts={self._debug_text_values(raw_texts)}"
            )
        elif candidates and not resolved:
            logger.warning(f"[who_at_me] image candidates unresolved; candidates={self._debug_text_values(candidates)}")
        return resolved

    async def _resolve_image_sources(self, event: AstrMessageEvent, images: list[str]) -> list[str]:
        result = []
        for image in images:
            text = str(image or "").strip()
            if not text:
                continue
            if self._renderable_image(text):
                result.append(text)
                continue
            resolved = await self._resolve_onebot_image(event, text)
            result.append(resolved or text)
        return self._unique_strings(result)

    async def _resolve_onebot_image(self, event: AstrMessageEvent, file_id: str) -> str:
        file_id = str(file_id or "").strip()
        if not file_id:
            return ""
        for kwargs in ({"file": file_id}, {"file_id": file_id}, {"image_id": file_id}):
            payload = await self._call_onebot_action(event, "get_image", **kwargs)
            data = self._mapping_data(payload)
            value = self._first_mapping_value(
                data,
                [*IMAGE_SOURCE_KEYS, "base64"],
            )
            if not value or str(value) == file_id:
                continue
            text = str(value).strip()
            if data.get("base64") == value and not text.startswith(("base64://", "data:image/")):
                return f"base64://{text}"
            return text
        return ""

    def _media(self, event: AstrMessageEvent) -> list[dict[str, str]]:
        media: list[dict[str, str]] = []
        raw_segments = self._raw_message_segments(event)
        if raw_segments:
            media.extend(self._segments_media(raw_segments))
        media.extend(self._segments_media(self._message_chain(event)))
        for text in self._raw_message_texts(event):
            media.extend(self._media_from_cq(text))
        return self._unique_media(media)

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

    def _recall_message_id(self, event: AstrMessageEvent) -> str:
        data = self._recall_event_data(event)
        if not data:
            return ""
        value = self._first_mapping_value(data, ["message_id", "messageId", "msg_id", "msgId", "id", "real_id", "realId"])
        return str(value or "").strip()

    def _recall_event_data(self, event: AstrMessageEvent) -> dict[str, Any]:
        for data in self._event_mapping_candidates(event):
            if self._mapping_is_recall_notice(data):
                return data
        return {}

    def _event_mapping_candidates(self, event: AstrMessageEvent) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        raw = getattr(event.message_obj, "raw_message", None)
        for value in (
            raw,
            getattr(event, "raw_event", None),
            getattr(event, "raw", None),
            getattr(event, "data", None),
            getattr(event.message_obj, "raw_event", None),
            getattr(event.message_obj, "data", None),
        ):
            if isinstance(value, dict):
                result.append(value)
        for segment in [*self._raw_message_segments(event), *self._message_chain(event)]:
            data = self._segment_data(segment)
            if data:
                result.append(data)
        return result

    def _mapping_is_recall_notice(self, data: dict[str, Any]) -> bool:
        values = [
            data.get("post_type"),
            data.get("postType"),
            data.get("notice_type"),
            data.get("noticeType"),
            data.get("sub_type"),
            data.get("subType"),
            data.get("type"),
            data.get("event"),
            data.get("action"),
        ]
        for value in values:
            text = str(value or "").lower()
            if text in RECALL_NOTICE_TYPES or "recall" in text or "revoke" in text:
                return True
        return False

    def _mention_after_image(self, event: AstrMessageEvent, mentions: list[str]) -> bool:
        segments = self._layout_segments(event)
        saw_image = False
        for segment in segments:
            if self._is_reference_segment(segment):
                continue
            if self._is_image_segment(segment):
                saw_image = True
                continue
            if self._is_target_at_segment(segment, mentions):
                return saw_image

        for text in self._raw_message_texts(event):
            saw_image = False
            for match in re.finditer(r"\[CQ:([^,\]]+),([^\]]+)\]", text or "", re.I):
                seg_type = match.group(1).lower()
                if self._is_image_segment_type(seg_type):
                    saw_image = True
                    continue
                if seg_type != "at":
                    continue
                data = self._parse_cq_attrs(match.group(2))
                value = data.get("qq") or data.get("user_id") or data.get("target") or data.get("id")
                if self._mention_matches(value, mentions):
                    return saw_image
        return False

    def _message_after_images(self, event: AstrMessageEvent, mentions: list[str]) -> str:
        segments = self._layout_segments(event)
        if segments:
            text = self._segments_text_after_images(segments, mentions)
            if text:
                return text

        for raw_text in self._raw_message_texts(event):
            text = self._cq_text_after_images(raw_text, mentions)
            if text:
                return text
        return ""

    def _segments_text_after_images(self, segments: list[Any], mentions: list[str]) -> str:
        saw_target_at = False
        saw_image_after_at = False
        texts: list[str] = []
        for segment in segments:
            if self._is_reference_segment(segment):
                continue
            seg_type = self._segment_type(segment)
            if self._is_target_at_segment(segment, mentions):
                saw_target_at = True
                continue
            if self._is_image_segment(segment):
                if saw_target_at:
                    saw_image_after_at = True
                continue
            if not saw_image_after_at:
                continue
            if seg_type in {"text", "plain"}:
                value = self._segment_value(segment, ["text", "content", "message"])
                if value:
                    texts.append(str(value))
            else:
                summary = self._segment_media_summary(segment)
                if summary:
                    texts.append(summary)
        return self._strip_cq_display("".join(texts))

    def _cq_text_after_images(self, text: str, mentions: list[str]) -> str:
        saw_target_at = False
        saw_image_after_at = False
        parts: list[str] = []
        cursor = 0
        for match in re.finditer(r"\[CQ:([^,\]]+),([^\]]+)\]", text or "", re.I):
            if saw_image_after_at and match.start() > cursor:
                parts.append(text[cursor : match.start()])
            seg_type = match.group(1).lower()
            data = self._parse_cq_attrs(match.group(2))
            if seg_type == "at":
                value = data.get("qq") or data.get("user_id") or data.get("target") or data.get("id")
                if self._mention_matches(value, mentions):
                    saw_target_at = True
            elif saw_target_at and self._is_image_segment_type(seg_type):
                saw_image_after_at = True
            cursor = match.end()
        if saw_image_after_at and cursor < len(text or ""):
            parts.append((text or "")[cursor:])
        return self._strip_cq_display("".join(parts))

    def _layout_segments(self, event: AstrMessageEvent) -> list[Any]:
        chain = self._message_chain(event)
        if any(self._is_image_segment(segment) for segment in chain):
            return chain
        return self._raw_message_segments(event) or chain

    def _is_target_at_segment(self, segment: Any, mentions: list[str]) -> bool:
        if self._segment_type(segment) != "at" and segment.__class__.__name__.lower() != "at" and not hasattr(segment, "qq"):
            return False
        value = self._segment_value(segment, ["qq", "user_id", "target", "id"])
        return self._mention_matches(value, mentions)

    def _mention_matches(self, value: Any, mentions: list[str]) -> bool:
        if value is None:
            return not mentions
        text = str(value)
        if text.lower() in {"all", "here", "@all"}:
            text = ALL_TARGET
        return not mentions or text in {str(item) for item in mentions}

    def _raw_message_segments(self, event: AstrMessageEvent) -> list[dict[str, Any]]:
        raw = getattr(event.message_obj, "raw_message", None)
        if isinstance(raw, list):
            return [segment for segment in raw if isinstance(segment, dict)]
        if not isinstance(raw, dict):
            return []
        segments = raw.get("message") or raw.get("message_chain") or []
        if isinstance(segments, dict):
            segments = self._segments_from_value(segments)
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
        at = r"[@\uff20]"
        cleaned = re.sub(r"\[CQ:at,[^\]]+\]", " ", text)
        for mention in mentions:
            if not mention:
                continue
            if mention == ALL_TARGET:
                cleaned = cleaned.replace("@全体成员", " ").replace("@all", " ")
            else:
                mention_text = re.escape(str(mention).strip())
                cleaned = re.sub(rf"{at}\s*{mention_text}(?:\([0-9]+\))?", " ", cleaned)
        cleaned = re.sub(rf"(?<!\S){at}\S+\([0-9]+\)", " ", cleaned)
        cleaned = re.sub(rf"(?<!\S){at}\S+", " ", cleaned)
        return re.sub(r"\s+", " ", cleaned).strip()

    def _is_reference_segment(self, item: Any) -> bool:
        seg_type = self._segment_type(item)
        if seg_type in REFERENCE_SEGMENT_TYPES:
            return True
        cls_name = item.__class__.__name__.lower()
        return any(token in cls_name for token in REFERENCE_SEGMENT_TYPES)

    def _is_image_segment_type(self, seg_type: str) -> bool:
        return self._normalize_segment_type(seg_type) in IMAGE_SEGMENT_TYPES

    def _is_image_segment(self, segment: Any) -> bool:
        if self._is_image_segment_type(self._segment_type(segment)):
            return True
        if self._is_image_component_name(segment):
            return True
        return bool(self._segment_image_values(segment))

    def _is_image_component_name(self, segment: Any) -> bool:
        cls_name = self._normalize_segment_type(segment.__class__.__name__)
        return cls_name in IMAGE_SEGMENT_TYPES or any(token in cls_name for token in ("image", "picture", "photo"))

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
            if not self._is_image_segment(segment):
                continue
            names = IMAGE_SOURCE_KEYS
            if seg_type in {"video", "shortvideo"}:
                names = VIDEO_COVER_KEYS
            if self._is_image_segment_type(seg_type) or self._is_image_component_name(segment):
                values = self._segment_values(segment, names)
            else:
                values = self._segment_image_values(segment, names)
            if values:
                urls.extend(str(value) for value in values)
                continue
            data = self._segment_data(segment)
            base64_value = self._first_mapping_value(data, ["base64"]) if data else None
            if base64_value:
                text = str(base64_value).strip()
                if text:
                    urls.append(text if text.startswith(("base64://", "data:image/")) else f"base64://{text}")
            else:
                logger.warning(f"[who_at_me] image-like segment has no source; segment={self._segment_debug_summary(segment)}")
                data = self._segment_data(segment)
                logger.debug(f"[谁艾特我] 图片段未找到可渲染来源: type={seg_type}, keys={list(data.keys()) if data else []}")
        return self._unique_strings(urls)

    def _segment_image_values(self, segment: Any, names: list[str] | None = None) -> list[Any]:
        values = self._segment_values(segment, names or IMAGE_SOURCE_KEYS)
        return [value for value in values if self._looks_like_image_source(value)]

    def _looks_like_image_source(self, value: Any) -> bool:
        text = str(value or "").strip()
        if not text:
            return False
        if text.startswith(("base64://", "data:image/")):
            return True
        if re.match(r"^https?://", text, re.I):
            return True
        suffix = Path(text.split("?", 1)[0]).suffix.lower()
        if suffix in IMAGE_MIME_TYPES:
            return True
        return bool(re.search(r"(^|[/\\])media_image_[^/\\]+$", text, re.I))

    def _segments_media(self, segments: list[Any]) -> list[dict[str, str]]:
        media = []
        for segment in segments:
            if self._is_reference_segment(segment):
                continue
            item = self._media_from_segment(segment)
            if item:
                media.append(item)
        return self._unique_media(media)

    def _media_from_segment(self, segment: Any) -> dict[str, str] | None:
        seg_type = self._segment_type(segment)
        if seg_type in {"video", "shortvideo"}:
            cover = self._first_string(
                self._segment_values(
                    segment,
                    VIDEO_COVER_KEYS,
                )
            )
            source = self._first_string(
                self._segment_values(
                    segment,
                    ["url", "path", "file_path", "filePath", "local_path", "localPath", "src", "file"],
                )
            )
            title = self._display_name(
                self._segment_value(segment, ["title", "name", "file_name", "fileName", "filename"]),
                default="视频",
            )
            return {"type": "video", "title": title, "source": source, "cover": cover}
        if seg_type == "file":
            source = self._first_string(
                self._segment_values(segment, ["url", "path", "file_path", "filePath", "local_path", "localPath", "file_", "file"])
            )
            title = self._display_name(
                self._segment_value(segment, ["name", "file_name", "fileName", "filename", "title"]),
                self._basename(source),
                default="文件",
            )
            size = str(self._segment_value(segment, ["size", "file_size", "fileSize"]) or "")
            return {"type": "file", "title": title, "source": source, "size": size}
        if seg_type in {"record", "voice", "audio"}:
            source = self._first_string(
                self._segment_values(segment, ["url", "path", "file_path", "filePath", "local_path", "localPath", "file"])
            )
            text = str(self._segment_value(segment, ["text", "title", "name"]) or "")
            return {"type": "audio", "title": text or "语音", "source": source}
        if seg_type in {"mface", "market_face", "marketface", "face", "emoji"}:
            return {"type": "emoji", "title": "表情"}
        return None

    def _first_string(self, values: list[Any]) -> str:
        for value in values:
            text = str(value or "").strip()
            if text:
                return text
        return ""

    def _basename(self, value: Any) -> str:
        text = str(value or "").replace("\\", "/").strip()
        if not text:
            return ""
        return text.rsplit("/", 1)[-1]

    def _unique_media(self, items: list[dict[str, str]]) -> list[dict[str, str]]:
        result = []
        for item in items:
            if not isinstance(item, dict):
                continue
            current = {k: str(v) for k, v in item.items() if v is not None}
            merged = False
            for existing in result:
                if self._media_items_match(existing, current):
                    for key, value in current.items():
                        if value and (not existing.get(key) or existing.get(key) in {"视频", "语音", "文件", "表情"}):
                            existing[key] = value
                    merged = True
                    break
            if not merged:
                result.append(current)
        return result

    def _media_items_match(self, left: dict[str, str], right: dict[str, str]) -> bool:
        left_type = str(left.get("type") or "")
        right_type = str(right.get("type") or "")
        if left_type != right_type:
            return False
        for key in ("source", "cover"):
            left_value = str(left.get(key) or "").strip()
            right_value = str(right.get(key) or "").strip()
            if left_value and right_value and left_value == right_value:
                return True
        if left_type == "video":
            return True
        return False

    def _is_chat_record_segment(self, seg_type: str, data: dict[str, Any] | None = None) -> bool:
        seg_type = str(seg_type or "").lower()
        return seg_type in CHAT_RECORD_SEGMENT_TYPES or self._looks_like_chat_record(data or {})

    def _looks_like_chat_record(self, data: dict[str, Any]) -> bool:
        if not isinstance(data, dict):
            return False
        text = self._mapping_text_blob(data).lower()
        return any(
            token in text
            for token in (
                "聊天记录",
                "合并转发",
                "forward",
                "multimsg",
                "multi_msg",
                "com.tencent.multimsg",
            )
        )

    def _mapping_text_blob(self, value: Any) -> str:
        if isinstance(value, dict):
            parts = []
            for key, item in value.items():
                parts.append(str(key))
                parts.append(self._mapping_text_blob(item))
            return " ".join(parts)
        if isinstance(value, list):
            return " ".join(self._mapping_text_blob(item) for item in value)
        return str(value or "")

    def _segment_values(self, segment: Any, names: list[str]) -> list[Any]:
        values = []
        data = self._segment_data(segment)
        if data:
            for name in names:
                value = data.get(name)
                if value is not None and value != "":
                    values.append(value)
        source = segment if isinstance(segment, dict) else None
        if source:
            for name in names:
                value = source.get(name)
                if value is not None and value != "":
                    values.append(value)
        if not isinstance(segment, dict):
            for name in names:
                if hasattr(segment, name):
                    value = getattr(segment, name)
                    if value is not None and value != "":
                        values.append(value)
        return self._unique_strings(values)

    def _segment_media_summary(self, segment: Any) -> str:
        seg_type = self._segment_type(segment)
        data = self._segment_data(segment)
        if self._is_image_segment(segment):
            return ""
        if self._is_chat_record_segment(seg_type, data):
            return CHAT_RECORD_TEXT
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
            value = segment.get("type") or segment.get("seg_type") or ""
        else:
            value = getattr(segment, "type", "") or getattr(segment, "seg_type", "")
            if not value:
                data = self._object_mapping(segment)
                value = data.get("type") or data.get("seg_type") or ""
        return self._normalize_segment_type(value) or self._normalize_segment_type(segment.__class__.__name__) or segment.__class__.__name__.lower()

    def _normalize_segment_type(self, value: Any) -> str:
        if hasattr(value, "value"):
            value = value.value
        text = str(value or "").strip()
        if not text:
            return ""
        text = text.lower()
        quoted = re.search(r"['\"]([a-z_][a-z0-9_]*)['\"]", text)
        if quoted:
            text = quoted.group(1)
        elif "." in text:
            text = text.rsplit(".", 1)[-1]
        if ":" in text:
            text = text.split(":", 1)[0]
        text = text.strip("<> ")
        return re.sub(r"[^a-z0-9_]+", "", text)

    def _segment_data(self, segment: Any) -> dict[str, Any]:
        if isinstance(segment, dict):
            data = segment.get("data")
            return data if isinstance(data, dict) else segment
        data = getattr(segment, "data", None)
        if isinstance(data, dict):
            return data
        return self._object_mapping(segment)

    def _object_mapping(self, value: Any) -> dict[str, Any]:
        if isinstance(value, dict):
            return value
        for name in ("model_dump", "dict", "to_dict", "as_dict"):
            method = getattr(value, name, None)
            if not callable(method):
                continue
            try:
                data = method()
            except Exception:
                continue
            if isinstance(data, dict):
                return data

        data = getattr(value, "__dict__", None)
        if isinstance(data, dict) and data:
            return {key: item for key, item in data.items() if not str(key).startswith("_")}
        text = str(value or "").strip()
        if text.startswith("{") or text.startswith("["):
            try:
                parsed = json.loads(text)
            except Exception:
                return {}
            if isinstance(parsed, dict):
                return parsed
            if isinstance(parsed, list):
                for item in parsed:
                    if isinstance(item, dict):
                        return item
        return {}

    def _has_image_debug_hint(self, values: list[Any]) -> bool:
        for value in values:
            text = self._debug_text(value).lower()
            if any(token in text for token in ("image", "picture", "photo", "media_image", ".jpg", ".jpeg", ".png", ".webp", ".gif")):
                return True
        return False

    def _segments_debug_summary(self, segments: list[Any]) -> list[dict[str, Any]]:
        return [self._segment_debug_summary(segment) for segment in segments[:8]]

    def _segment_debug_summary(self, segment: Any) -> dict[str, Any]:
        data = self._segment_data(segment)
        values = self._segment_values(segment, IMAGE_SOURCE_KEYS) if data or not isinstance(segment, str) else []
        return {
            "class": segment.__class__.__name__,
            "type": self._segment_type(segment),
            "keys": list(data.keys())[:24] if isinstance(data, dict) else [],
            "values": self._debug_text_values(values),
            "text": self._debug_text(segment),
        }

    def _debug_text_values(self, values: list[Any]) -> list[str]:
        return [self._debug_text(value) for value in values[:8]]

    def _debug_text(self, value: Any, limit: int = 240) -> str:
        text = str(value or "").replace("\n", "\\n").replace("\r", "\\r")
        return text if len(text) <= limit else text[:limit] + "..."

    def _log_record_image_diagnostics(
        self,
        event: AstrMessageEvent,
        group_id: str,
        record: dict[str, Any],
        mentions: list[str],
        *,
        kind: str,
    ) -> None:
        raw_segments = self._raw_message_segments(event)
        chain_segments = self._message_chain(event)
        raw_texts = self._raw_message_texts(event)
        has_image_hint = self._has_image_debug_hint([*raw_segments, *chain_segments, *raw_texts])
        images = record.get("images") or record.get("image") or []
        media = record.get("media") or []
        image_count = len(images) if isinstance(images, list) else int(bool(images))
        media_count = len(media) if isinstance(media, list) else int(bool(media))
        message_preview = self._debug_text(record.get("message"), limit=120)
        summary = (
            "[who_at_me] record image diagnostic "
            f"kind={kind} group={group_id} sender={record.get('user_id') or ''} "
            f"msg_id={record.get('message_id') or ''} mentions={self._debug_text_values(mentions)} "
            f"images={image_count} media={media_count} image_hint={has_image_hint} "
            f"message={message_preview}"
        )
        if has_image_hint and image_count <= 0 and media_count <= 0:
            logger.warning(
                summary
                + " raw="
                + str(self._segments_debug_summary(raw_segments))
                + " chain="
                + str(self._segments_debug_summary(chain_segments))
                + " raw_texts="
                + str(self._debug_text_values(raw_texts))
            )
        elif mentions or has_image_hint or image_count or media_count:
            logger.info(summary)

    def _log_query_image_diagnostics(
        self,
        group_id: str,
        target: str,
        records: list[dict[str, Any]],
        *,
        page_count: int,
    ) -> None:
        image_records = 0
        cached_records = 0
        media_records = 0
        for record in records:
            images = record.get("images") or record.get("image") or []
            if images:
                image_records += 1
            if record.get("image_cache"):
                cached_records += 1
            if record.get("media"):
                media_records += 1
        renderable_records = sum(1 for record in records if self._record_renderable_images(record))
        logger.info(
            "[who_at_me] query image diagnostic "
            f"group={group_id} target={target} records={len(records)} pages={page_count} "
            f"with_images={image_records} with_cache={cached_records} with_media={media_records} "
            f"renderable_images={renderable_records}"
        )

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
        for match in re.finditer(r"\[CQ:([^,\]]+)(?:,([^\]]*))?\]", text or "", re.I):
            seg_type = match.group(1).lower()
            attrs = match.group(2) or ""
            if self._is_image_segment_type(seg_type):
                continue
            data = self._parse_cq_attrs(attrs) if attrs else {}
            if self._is_chat_record_segment(seg_type, data) or self._looks_like_chat_record({"raw": attrs}):
                summaries.append(CHAT_RECORD_TEXT)
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
        for match in re.finditer(r"\[CQ:([^,\]]+),([^\]]+)\]", text or "", re.I):
            seg_type = match.group(1).lower()
            if not self._is_image_segment_type(seg_type) and seg_type not in {"video", "shortvideo"}:
                continue
            attrs = match.group(2)
            data = self._parse_cq_attrs(attrs)
            names = (
                VIDEO_COVER_KEYS
                if seg_type in {"video", "shortvideo"}
                else IMAGE_SOURCE_KEYS
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

    def _media_from_cq(self, text: str) -> list[dict[str, str]]:
        media = []
        for match in re.finditer(r"\[CQ:(video|shortvideo|file|record|voice|audio),([^\]]+)\]", text or ""):
            seg_type = match.group(1).lower()
            data = self._parse_cq_attrs(match.group(2))
            if seg_type in {"video", "shortvideo"}:
                cover = str(
                    self._first_mapping_value(
                        data,
                        ["cover", "cover_url", "coverUrl", "thumbnail", "thumb", "preview", "poster", "image"],
                    )
                    or ""
                )
                source = str(
                    self._first_mapping_value(data, ["url", "path", "file_path", "local_path", "src", "file"]) or ""
                )
                title = str(self._first_mapping_value(data, ["title", "name", "filename", "file_name"]) or "视频")
                media.append({"type": "video", "title": title, "source": source, "cover": cover})
            elif seg_type == "file":
                source = str(self._first_mapping_value(data, ["url", "path", "file_path", "local_path", "file"]) or "")
                title = str(
                    self._first_mapping_value(data, ["name", "filename", "file_name", "title"])
                    or self._basename(source)
                    or "文件"
                )
                size = str(self._first_mapping_value(data, ["size", "file_size"]) or "")
                media.append({"type": "file", "title": title, "source": source, "size": size})
            else:
                source = str(self._first_mapping_value(data, ["url", "path", "file_path", "local_path", "file"]) or "")
                media.append({"type": "audio", "title": "语音", "source": source})
        return self._unique_media(media)

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
        cache = data.get("image_cache")
        if isinstance(cache, list):
            cached_images = self._renderable_images(cache)
            if cached_images:
                return cached_images

        images = data.get("images") or data.get("image") or []
        if isinstance(images, str):
            images = [images]
        return self._renderable_images(images)

    def _record_renderable_media(self, data: dict[str, Any]) -> list[dict[str, str]]:
        raw_media = data.get("media") or []
        if not isinstance(raw_media, list):
            return []
        result = []
        for item in raw_media:
            if not isinstance(item, dict):
                continue
            kind = str(item.get("type") or "file").lower()
            card = {
                "type": kind,
                "title": str(item.get("title") or {"video": "视频", "audio": "语音", "file": "文件"}.get(kind, "媒体")),
                "size": str(item.get("size") or ""),
                "cover": self._renderable_image(self._media_cached_value(item, "cover") or item.get("cover")),
                "source": self._renderable_media_source(self._media_cached_value(item, "source") or item.get("source")),
            }
            result.append(card)
        return self._unique_media(result)

    def _media_cached_value(self, item: dict[str, Any], key: str) -> str:
        cache_key = f"{key}_cache"
        cached = item.get(cache_key)
        if isinstance(cached, dict):
            return str(cached.get("local") or cached.get("url") or cached.get("source") or "")
        return ""

    def _renderable_media_source(self, source: Any) -> str:
        value = str(source or "").strip()
        if not value:
            return ""
        if re.match(r"^(https?|file)://", value, re.I):
            return value
        if value.startswith(("base64://", "data:")):
            return value
        try:
            path = Path(value)
            if path.exists():
                return path.resolve().as_uri()
        except (OSError, ValueError):
            pass
        return value

    def _renderable_image(self, image: Any) -> str:
        if isinstance(image, dict):
            source = str(image.get("source") or "").strip()
            for key in (
                "local",
                "path",
                "file_path",
                "filePath",
                "local_path",
                "localPath",
                "url",
                "src",
                "image",
                "image_url",
                "imageUrl",
                "file_url",
                "fileUrl",
                "thumb_url",
                "thumbUrl",
                "preview_url",
                "previewUrl",
                "source",
                "file",
            ):
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
            return self._file_uri_image_data(value)
        try:
            path = Path(value)
            if path.exists():
                return self._local_image_data_uri(path)
        except (OSError, ValueError):
            pass
        return ""

    def _file_uri_image_data(self, value: str) -> str:
        try:
            from urllib.parse import unquote, urlparse

            parsed = urlparse(value)
            path_text = unquote(parsed.path or "")
            if re.match(r"^/[A-Za-z]:/", path_text):
                path_text = path_text[1:]
            return self._local_image_data_uri(Path(path_text))
        except Exception:
            return ""

    def _local_image_data_uri(self, path: Path) -> str:
        try:
            resolved = path.resolve()
            if not resolved.exists() or not resolved.is_file():
                return ""
            suffix = resolved.suffix.lower()
            mime = IMAGE_MIME_TYPES.get(suffix, "image/png")
            data = base64.b64encode(resolved.read_bytes()).decode("ascii")
            return f"data:{mime};base64,{data}"
        except Exception:
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
        if not self._record_visual_keys_match(self._record_images_key(left), self._record_images_key(right)):
            return False
        if not self._record_visual_keys_match(self._record_media_key(left), self._record_media_key(right)):
            return False
        if self._record_quote_key(left) != self._record_quote_key(right):
            return False

        return abs(self._record_time(left) - self._record_time(right)) <= window_seconds

    def _record_visual_keys_match(self, left: tuple[Any, ...], right: tuple[Any, ...]) -> bool:
        if left == right:
            return True
        if not left or not right:
            return True
        left_set = set(left)
        right_set = set(right)
        return left_set.issubset(right_set) or right_set.issubset(left_set)

    def _merge_duplicate_record(self, left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
        use_right = self._record_richness_key(right) >= self._record_richness_key(left)
        base = dict(right if use_right else left)
        other = left if use_right else right

        for key in (
            "message",
            "name",
            "role",
            "title",
            "member_title",
            "level",
            "message_id",
            "target",
            "quote",
            "poke",
            "message_after_images",
        ):
            if not base.get(key) and other.get(key):
                base[key] = other[key]

        base["images"] = self._unique_strings([*(base.get("images") or []), *(other.get("images") or [])])
        if base.get("image") or other.get("image"):
            base["image"] = base["images"]
        cache = [*(base.get("image_cache") or []), *(other.get("image_cache") or [])]
        if cache:
            base["image_cache"] = self._unique_image_cache(cache)
        base["media"] = self._unique_media([*(base.get("media") or []), *(other.get("media") or [])])
        if base.get("at_targets") or other.get("at_targets"):
            base["at_targets"] = self._unique_strings([*(base.get("at_targets") or []), *(other.get("at_targets") or [])])
        for key in ("is_context", "at_after_image"):
            base[key] = bool(base.get(key) or other.get(key))
        for key in ("before", "after"):
            merged = [*(base.get(key) or []), *(other.get(key) or [])]
            if merged:
                base[key] = merged
        return base

    def _record_richness_key(self, record: dict[str, Any]) -> tuple[int, int, int, int, int]:
        image_count = len(record.get("images") or record.get("image") or [])
        cache_count = len(record.get("image_cache") or [])
        media_count = len(record.get("media") or [])
        quote = record.get("quote")
        quote_score = 1 if isinstance(quote, dict) and (quote.get("message") or quote.get("images")) else 0
        return (
            image_count + cache_count,
            media_count,
            quote_score,
            len(str(record.get("message") or "")),
            self._record_time(record),
        )

    def _unique_image_cache(self, items: list[Any]) -> list[dict[str, str]]:
        result: list[dict[str, str]] = []
        seen_sources: set[str] = set()
        seen_locals: set[str] = set()
        for item in items:
            if not isinstance(item, dict):
                continue
            source = str(item.get("source") or "").strip()
            local = str(item.get("local") or "").strip()
            if source and source in seen_sources:
                continue
            if not source and local and local in seen_locals:
                continue
            if source:
                seen_sources.add(source)
            if local:
                seen_locals.add(local)
            result.append({k: str(v) for k, v in item.items() if v is not None})
        return result

    def _record_message_key(self, record: dict[str, Any]) -> str:
        poke = record.get("poke")
        if isinstance(poke, dict):
            return "poke:" + self._normalize_record_text(
                f"{poke.get('actor') or ''}|{poke.get('action') or ''}|{poke.get('target') or ''}|{poke.get('suffix') or ''}"
            )
        message = str(record.get("message") or "")
        targets: list[Any] = [record.get("target"), record.get("at"), record.get("AtQQ")]
        if isinstance(record.get("at_targets"), list):
            targets.extend(record["at_targets"])
        if any(str(item or "").strip() for item in targets):
            message = self._strip_at_display(message, targets)
        return self._normalize_record_text(message)

    def _normalize_record_text(self, value: Any) -> str:
        return re.sub(r"\s+", " ", str(value or "")).strip()

    def _record_images_key(self, record: dict[str, Any]) -> tuple[str, ...]:
        return tuple(str(image) for image in (record.get("images") or record.get("image") or []))

    def _record_media_key(self, record: dict[str, Any]) -> tuple[tuple[str, str, str], ...]:
        media = record.get("media") or []
        if not isinstance(media, list):
            return ()
        return tuple(
            (
                str(item.get("type") or ""),
                str(item.get("source") or ""),
                str(item.get("title") or ""),
            )
            for item in media
            if isinstance(item, dict)
        )

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
