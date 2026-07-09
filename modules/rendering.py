from __future__ import annotations

import asyncio
import base64
import re
import time
import uuid
from pathlib import Path
from typing import Any

from astrbot.api import logger
import astrbot.api.message_components as Comp

try:
    from .constants import *
except ImportError:
    from modules.constants import *


def _load_result_template() -> str:
    return (Path(__file__).resolve().parents[1] / "templates" / "result.html").read_text(encoding="utf-8")


HTML_TEMPLATE = _load_result_template()


class RenderingMixin:
    async def _render_query_image(self, data: dict[str, Any]) -> str:
        data = dict(data)
        data.setdefault("layout", self._render_layout())
        data.setdefault("custom_font_css", self._custom_font_css())
        data.setdefault("extract_video_frame", self._extract_video_frame())
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

                  const videos = Array.from(document.querySelectorAll("video.media-video") || []);
                  await Promise.race([
                    Promise.all(videos.map((video) => new Promise((resolve) => {
                      if (video.readyState >= 1) {
                        resolve();
                        return;
                      }
                      const done = () => resolve();
                      video.addEventListener("loadedmetadata", done, { once: true });
                      video.addEventListener("loadeddata", done, { once: true });
                      video.addEventListener("error", done, { once: true });
                    }))),
                    delay(assetTimeout),
                  ]);
                  for (const video of videos) {
                    await Promise.race([
                      new Promise((resolve) => {
                        if (!video.duration || !Number.isFinite(video.duration)) {
                          resolve();
                          return;
                        }
                        const done = () => resolve();
                        video.addEventListener("seeked", done, { once: true });
                        video.addEventListener("error", done, { once: true });
                        try {
                          const target = Math.min(Math.max(0.5, video.duration * 0.1), Math.max(0.1, video.duration - 0.1));
                          video.currentTime = target;
                        } catch (_) {
                          resolve();
                        }
                      }),
                      delay(Math.min(assetTimeout, 2000)),
                    ]);
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
        plugin_dir = getattr(self, "_plugin_data_dir", None)
        if callable(plugin_dir):
            return Path(plugin_dir()) / "renders"
        try:
            from astrbot.api.star import StarTools

            return Path(StarTools.get_data_dir("astrbot_plugin_who_at_me")) / "renders"
        except Exception:
            try:
                from astrbot.core.utils.astrbot_path import get_astrbot_data_path

                return Path(get_astrbot_data_path()) / "plugin_data" / "astrbot_plugin_who_at_me" / "renders"
            except Exception:
                return Path.cwd() / "data" / "astrbot_plugin_who_at_me" / "renders"

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
