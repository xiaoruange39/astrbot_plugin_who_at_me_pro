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

    async def _render_query_images(self, items: list[dict[str, Any]]) -> list[str]:
        if not items:
            return []
        prepared = []
        for item in items:
            data = dict(item)
            data.setdefault("layout", self._render_layout())
            data.setdefault("custom_font_css", self._custom_font_css())
            prepared.append(data)

        timeout = self._render_task_timeout_sec()
        if self._config_bool("render", "prefer_browser", default=True):
            try:
                return await asyncio.wait_for(
                    self._render_html_pages_with_browser(HTML_TEMPLATE, prepared),
                    timeout=timeout * len(prepared),
                )
            except asyncio.TimeoutError:
                logger.warning("[who_at_me] batched browser render timed out; falling back to html_render")
            except Exception as exc:
                logger.warning(
                    f"[who_at_me] batched browser render failed; falling back to html_render: {type(exc).__name__}: {exc}",
                    exc_info=True,
                )

        result = []
        for data in prepared:
            result.append(await asyncio.wait_for(self._render_html_with_t2i(HTML_TEMPLATE, data), timeout=timeout))
        return result

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

    async def _render_html_pages_with_browser(
        self,
        template: str,
        items: list[dict[str, Any]],
    ) -> list[str]:
        from jinja2 import Environment
        from playwright.async_api import async_playwright

        self._cleanup_old_renders()
        renderer = Environment(autoescape=True).from_string(template)
        output_paths = []
        browser = None
        async with async_playwright() as playwright:
            try:
                browser = await playwright.chromium.launch(
                    args=["--disable-dev-shm-usage", "--disable-gpu", "--no-sandbox"]
                )
                for data in items:
                    page = await browser.new_page(
                        viewport={"width": 600, "height": 800},
                        device_scale_factor=2,
                    )
                    output_path = self._new_render_path()
                    try:
                        await page.set_content(
                            renderer.render(**data),
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
                        output_paths.append(str(output_path))
                    finally:
                        await page.close()
            finally:
                if browser:
                    await browser.close()
        return output_paths

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
                    image_path = self._store_t2i_render_result(rendered, str(options.get("type") or ""))
                    if image_path:
                        return image_path
                    logger.warning(f"[璋佽壘鐗规垜] t2i 杩斿洖浜嗘棤鏁堝浘鐗囨暟鎹紝灏濊瘯涓嬩竴绛栫暐: {options}")
                    continue
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
        first_timeout = max(self._render_page_timeout_ms(), 50000)
        second_timeout = max(first_timeout, 100000)
        return [
            {
                "full_page": True,
                "type": "png",
                "device_scale_factor_level": "ultra",
                "timeout": first_timeout,
            },
            {
                "full_page": True,
                "type": "jpeg",
                "quality": min(self._render_quality(), 80),
                "device_scale_factor_level": "high",
                "timeout": second_timeout,
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
        if re.match(r"^https?://", text, re.I):
            return text
        return self._store_t2i_file_result(text)

    def _store_t2i_image_bytes(self, data: bytes, image_type: str = "") -> str | None:
        suffix = self._t2i_image_suffix(data, image_type)
        if not suffix:
            return None

        output = self._new_render_path(suffix)
        try:
            output.write_bytes(data)
            self._trim_t2i_blank_margin(output)
            return str(output)
        except Exception as exc:
            logger.warning(f"[谁艾特我] 保存 t2i 图片失败: {type(exc).__name__}: {exc}")
            return None

    def _store_t2i_file_result(self, value: str) -> str | None:
        try:
            path = Path(value)
            if not path.exists() or not path.is_file():
                return None
            head = path.read_bytes()[:16]
            if not self._t2i_image_suffix(head, path.suffix):
                logger.warning(f"[who_at_me] t2i returned non-image file: {path}")
                return None
            self._trim_t2i_blank_margin(path)
            return str(path)
        except Exception as exc:
            logger.warning(f"[who_at_me] t2i file result rejected: {type(exc).__name__}: {exc}")
            return None

    def _trim_t2i_blank_margin(self, path: Path) -> None:
        try:
            from PIL import Image

            with Image.open(path) as img:
                width, height = img.size
                if width <= 720 or height <= 0:
                    return
                work = img.convert("RGB")
                bg = work.getpixel((width - 1, min(10, height - 1)))
                y_step = max(1, height // 600)
                x_step = 1 if width <= 1600 else 2
                right = width - 1
                for x in range(width - 1, -1, -x_step):
                    has_content = False
                    for y in range(0, height, y_step):
                        pixel = work.getpixel((x, y))
                        if sum(abs(pixel[i] - bg[i]) for i in range(3)) > 24:
                            has_content = True
                            break
                    if has_content:
                        right = min(width - 1, x + 28)
                        break
                if width - right < 80 or right < 320:
                    return
                cropped = img.crop((0, 0, right + 1, height))
                suffix = path.suffix.lower()
                if suffix in {".jpg", ".jpeg"}:
                    cropped.convert("RGB").save(path, format="JPEG", quality=max(90, self._render_quality()), optimize=True)
                elif suffix == ".webp":
                    cropped.save(path, format="WEBP", quality=max(90, self._render_quality()))
                else:
                    cropped.save(path)
        except Exception as exc:
            logger.debug(f"[who_at_me] t2i blank-margin trim skipped: {type(exc).__name__}: {exc}")

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
        plugin_dir = getattr(self, "_plugin_data_dir", None)
        if callable(plugin_dir):
            return Path(plugin_dir()) / "renders"
        try:
            from astrbot.api.star import StarTools

            return Path(StarTools.get_data_dir(PLUGIN_NAME)) / "renders"
        except Exception:
            try:
                from astrbot.core.utils.astrbot_path import get_astrbot_data_path

                return Path(get_astrbot_data_path()) / "plugin_data" / PLUGIN_NAME / "renders"
            except Exception:
                return Path.cwd() / "data" / PLUGIN_NAME / "renders"

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
