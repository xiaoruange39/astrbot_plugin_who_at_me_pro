from __future__ import annotations

import base64
import json
import re
import shutil
from pathlib import Path
from typing import Any

from astrbot.api import logger

try:
    from .constants import (
        DEFAULT_FOOTER_IMAGE_FILE,
        DEFAULT_HEADER_IMAGE_FILE,
        IMAGE_KINDS,
        IMAGE_MIME_TYPES,
        LEGACY_FOOTER_IMAGE_URL,
        LEGACY_HEADER_IMAGE_URL,
        LEGACY_PLUGIN_NAME,
        PAGE_SETTINGS_DEFAULTS,
        PLUGIN_NAME,
    )
except ImportError:
    from modules.constants import (
        DEFAULT_FOOTER_IMAGE_FILE,
        DEFAULT_HEADER_IMAGE_FILE,
        IMAGE_KINDS,
        IMAGE_MIME_TYPES,
        LEGACY_FOOTER_IMAGE_URL,
        LEGACY_HEADER_IMAGE_URL,
        LEGACY_PLUGIN_NAME,
        PAGE_SETTINGS_DEFAULTS,
        PLUGIN_NAME,
    )


class PageSettingsMixin:
    def _json_response(self, payload: dict[str, Any]) -> Any:
        return self._jsonify(payload) if callable(self._jsonify) else payload

    def _page_data(self) -> dict[str, Any]:
        data = self._font_data()
        data["layout"] = self._render_layout()
        data["images"] = self._image_data()
        return data

    def _font_data(self) -> dict[str, Any]:
        current_path = str(self.page_settings.get("font_path") or "")
        current_name = self._selected_font_name(current_path)
        return {
            "fonts": self._list_uploaded_fonts(),
            "current_path": current_path,
            "current_name": current_name,
            "current_font_css": self._page_font_css(),
        }

    def _plugin_data_dir(self) -> Path:
        cached = getattr(self, "_plugin_data_dir_cache", None)
        if cached:
            return Path(cached)
        try:
            from astrbot.api.star import StarTools

            target = Path(StarTools.get_data_dir(PLUGIN_NAME))
            legacy = Path(StarTools.get_data_dir(LEGACY_PLUGIN_NAME))
        except Exception:
            try:
                from astrbot.core.utils.astrbot_path import get_astrbot_data_path

                root = Path(get_astrbot_data_path()) / "plugin_data"
            except Exception:
                root = Path.cwd() / "data"
            target = root / PLUGIN_NAME
            legacy = root / LEGACY_PLUGIN_NAME

        self._migrate_legacy_plugin_data(legacy, target)
        target.mkdir(parents=True, exist_ok=True)
        self._plugin_data_dir_cache = target
        return target

    def _migrate_legacy_plugin_data(self, legacy: Path, target: Path) -> None:
        marker = target / ".migrated_from_who_at_me"
        if marker.exists() or not legacy.exists() or legacy.resolve() == target.resolve():
            return
        try:
            target.mkdir(parents=True, exist_ok=True)
            for source in legacy.iterdir():
                destination = target / source.name
                if destination.exists():
                    continue
                if source.is_dir():
                    shutil.copytree(source, destination)
                elif source.is_file():
                    shutil.copy2(source, destination)
            marker.write_text(LEGACY_PLUGIN_NAME, encoding="utf-8")
            logger.info(f"[who_at_me] migrated plugin data to {target}")
        except Exception as exc:
            logger.warning(f"[who_at_me] legacy data migration failed: {exc}")

    def _page_settings_file(self) -> Path:
        return self._plugin_data_dir() / "page_settings.json"

    def _fonts_dir(self) -> Path:
        return self._plugin_data_dir() / "resources" / "fonts"

    def _images_dir(self) -> Path:
        return self._plugin_data_dir() / "resources" / "images"

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
        for kind in IMAGE_KINDS:
            key = self._image_setting_key(kind)
            settings[key] = str(settings.get(key) or "").strip()
        return settings

    def _save_page_settings(self) -> None:
        path = self._page_settings_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(self.page_settings, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _sanitize_layout_settings(self, value: Any) -> dict[str, Any]:
        data = value if isinstance(value, dict) else {}
        bold_strength = self._clamp_int(data.get("font_bold_strength"), PAGE_SETTINGS_DEFAULTS["font_bold_strength"], 0, 120)
        if "font_bold_strength" not in data and self._bool_setting(data.get("font_bold"), PAGE_SETTINGS_DEFAULTS["font_bold"]):
            bold_strength = 60
        return {
            "time_x": self._clamp_int(data.get("time_x"), PAGE_SETTINGS_DEFAULTS["time_x"], 0, 600),
            "time_y": self._clamp_int(data.get("time_y"), PAGE_SETTINGS_DEFAULTS["time_y"], 0, 120),
            "time_font_size": self._clamp_int(
                data.get("time_font_size"), PAGE_SETTINGS_DEFAULTS["time_font_size"], 8, 72
            ),
            "group_x": self._clamp_int(data.get("group_x"), PAGE_SETTINGS_DEFAULTS["group_x"], 0, 600),
            "group_y": self._clamp_int(data.get("group_y"), PAGE_SETTINGS_DEFAULTS["group_y"], 0, 120),
            "group_font_size": self._clamp_int(
                data.get("group_font_size"), PAGE_SETTINGS_DEFAULTS["group_font_size"], 8, 96
            ),
            "font_bold": bold_strength > 0,
            "font_bold_strength": bold_strength,
            "font_bold_stroke": self._bold_css_value(bold_strength, 0.006),
            "font_bold_shadow_x": self._bold_css_value(bold_strength, 0.012),
            "font_bold_shadow_x_neg": self._bold_css_value(bold_strength, 0.006),
            "font_bold_shadow_y": self._bold_css_value(bold_strength, 0.008),
            "font_bold_shadow_y_neg": self._bold_css_value(bold_strength, 0.004),
        }

    def _render_layout(self) -> dict[str, Any]:
        return self._sanitize_layout_settings(self.page_settings)

    def _clamp_int(self, value: Any, default: int, minimum: int, maximum: int) -> int:
        try:
            number = int(value)
        except (TypeError, ValueError):
            number = int(default)
        return min(maximum, max(minimum, number))

    def _bold_css_value(self, strength: int, scale: float) -> str:
        return f"{float(strength) * scale:.3f}".rstrip("0").rstrip(".")

    def _bool_setting(self, value: Any, default: bool = False) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        if isinstance(value, str):
            text = value.strip().lower()
            if text in {"1", "true", "yes", "on", "y"}:
                return True
            if text in {"0", "false", "no", "off", "n", ""}:
                return False
        return bool(default)

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
        return False

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

    def _is_allowed_image_file(self, filename: str) -> bool:
        return Path(str(filename or "")).suffix.lower() in IMAGE_MIME_TYPES

    def _format_file_size(self, size: int) -> str:
        if size < 1024 * 1024:
            return f"{size / 1024:.1f}KB"
        return f"{size / 1024 / 1024:.1f}MB"

    def _font_config_path(self, filename: str) -> str:
        return f"resources/fonts/{filename}"

    def _image_config_path(self, filename: str) -> str:
        return f"resources/images/{filename}"

    def _selected_font_name(self, font_path: str) -> str:
        text = str(font_path or "").replace("\\", "/").strip()
        return Path(text).name if text else ""

    def _selected_image_name(self, image_path: str) -> str:
        text = str(image_path or "").replace("\\", "/").strip()
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

    def _save_image_path(self, kind: str, image_path: str, *, save: bool = True) -> None:
        if kind not in IMAGE_KINDS:
            return
        image_path = str(image_path or "").strip()
        if image_path:
            image_name = self._selected_image_name(image_path)
            image_path = self._image_config_path(image_name) if image_name else ""
        self.page_settings[self._image_setting_key(kind)] = image_path
        if save:
            self._save_page_settings()

    def _resolve_custom_font_path(self) -> Path | None:
        font_path = str(self.page_settings.get("font_path") or "").strip()
        if not font_path:
            return None
        raw_path = Path(font_path).expanduser()
        candidates = [raw_path] if raw_path.is_absolute() else [
            self._plugin_data_dir() / raw_path,
            self._fonts_dir() / raw_path.name,
            Path(__file__).resolve().parents[1] / raw_path,
            Path(__file__).resolve().parents[1] / "fonts" / raw_path.name,
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

    def _page_font_css(self) -> str:
        font_path = self._resolve_custom_font_path()
        if not font_path:
            return ""
        try:
            font_data = base64.b64encode(font_path.read_bytes()).decode("ascii")
        except OSError as exc:
            logger.warning(f"[谁艾特我] 读取 Page 预览字体失败，使用默认字体: {exc}")
            return ""
        return (
            "@font-face { "
            "font-family: 'WhoAtMePreviewFont'; "
            f"src: url(\"data:{self._font_mime_type(font_path)};base64,{font_data}\") format('{self._font_format(font_path)}'); "
            "font-weight: 100 900; font-style: normal; font-display: block; "
            "}\n"
            ".phone, .phone * { font-family: 'WhoAtMePreviewFont', 'Microsoft YaHei', 'Segoe UI', sans-serif !important; }"
        )

    def _image_setting_key(self, kind: str) -> str:
        return f"{kind}_image_path"

    def _image_data(self) -> dict[str, Any]:
        return {
            "header": self._header_image_url(),
            "footer": self._footer_image_url(),
            "header_source": self._image_source_label("header"),
            "footer_source": self._image_source_label("footer"),
        }

    def _image_source_label(self, kind: str) -> str:
        if self._resolve_custom_image_path(kind):
            return "Page自定义图片"
        configured = self._configured_image_value(kind)
        if configured:
            return "Web配置图片"
        return "插件内置本地图片"

    def _configured_image_value(self, kind: str) -> str:
        key = "header_image_url" if kind == "header" else "footer_image_url"
        value = self._config_str("render", key, default="").strip()
        if value in {LEGACY_HEADER_IMAGE_URL, LEGACY_FOOTER_IMAGE_URL}:
            return ""
        return value

    def _resolve_custom_image_path(self, kind: str) -> Path | None:
        image_path = str(self.page_settings.get(self._image_setting_key(kind)) or "").strip()
        if not image_path:
            return None
        return self._resolve_local_image_path(image_path)

    def _resolve_configured_image_path(self, value: str) -> Path | None:
        text = str(value or "").strip()
        if not text or re.match(r"^(https?://|data:image/)", text, re.I):
            return None
        return self._resolve_local_image_path(text)

    def _resolve_local_image_path(self, value: str) -> Path | None:
        raw_path = Path(str(value or "").strip()).expanduser()
        candidates = [raw_path] if raw_path.is_absolute() else [
            self._plugin_data_dir() / raw_path,
            self._images_dir() / raw_path.name,
            Path(__file__).resolve().parents[1] / raw_path,
            Path(__file__).resolve().parents[1] / "assets" / raw_path.name,
        ]
        for candidate in candidates:
            try:
                if candidate.exists() and candidate.is_file():
                    return candidate.resolve()
            except OSError:
                continue
        return None

    def _default_image_path(self, kind: str) -> Path:
        filename = DEFAULT_HEADER_IMAGE_FILE if kind == "header" else DEFAULT_FOOTER_IMAGE_FILE
        return Path(__file__).resolve().parents[1] / filename

    def _image_src(self, kind: str) -> str:
        custom_path = self._resolve_custom_image_path(kind)
        if custom_path:
            return self._image_file_data_url(custom_path)

        configured = self._configured_image_value(kind)
        if configured:
            configured_path = self._resolve_configured_image_path(configured)
            if configured_path:
                return self._image_file_data_url(configured_path)
            return configured

        return self._image_file_data_url(self._default_image_path(kind))

    def _image_file_data_url(self, path: Path) -> str:
        try:
            suffix = path.suffix.lower()
            mime = IMAGE_MIME_TYPES.get(suffix, "image/png")
            data = base64.b64encode(path.read_bytes()).decode("ascii")
            return f"data:{mime};base64,{data}"
        except OSError as exc:
            logger.warning(f"[谁艾特我] 读取本地图片失败: {path} {exc}")
            return ""
