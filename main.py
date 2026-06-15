import asyncio
import base64
import io
import os
import shutil
import zipfile
from pathlib import Path

from quart import jsonify, request, send_file

import astrbot.api.message_components as Comp
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageEventResult, filter
from astrbot.api.star import Context, Star, register
from astrbot.core.utils.astrbot_path import get_astrbot_data_path
from astrbot.core.utils.session_waiter import SessionController, session_waiter

PLUGIN_NAME = "astrbot_plugin_jmcomic"


@register(
    PLUGIN_NAME,
    "YumemiAI",
    "通过数字 ID 下载 JMComic 漫画，支持导出 PDF/ZIP",
    "1.0.0",
)
class JmComicPlugin(Star):
    """JMComic 漫画下载器插件"""

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.cache = []
        self._ensure_download_dir()

        # 注册 Pages 后端 API
        context.register_web_api(
            f"/{PLUGIN_NAME}/cache/list",
            self._api_cache_list,
            ["GET"],
            "获取缓存漫画列表",
        )
        context.register_web_api(
            f"/{PLUGIN_NAME}/cache/preview",
            self._api_cache_preview,
            ["GET"],
            "获取漫画首页预览图",
        )
        context.register_web_api(
            f"/{PLUGIN_NAME}/cache/delete",
            self._api_cache_delete,
            ["POST"],
            "删除指定缓存漫画",
        )
        context.register_web_api(
            f"/{PLUGIN_NAME}/cache/download",
            self._api_cache_download,
            ["GET"],
            "下载缓存文件",
        )

    async def initialize(self):
        """异步初始化：加载缓存索引。"""
        await self._load_cache()

    # ──────────────────────────── 初始化 ────────────────────────────

    def _ensure_download_dir(self):
        """确保下载目录存在。"""
        cfg_path = self.config.get("download_path", "")
        if cfg_path and cfg_path.strip():
            self.download_dir = Path(cfg_path.strip())
        else:
            self.download_dir = (
                Path(get_astrbot_data_path()) / "plugin_data" / "astrbot_plugin_jmcomic"
            )
        self.download_dir.mkdir(parents=True, exist_ok=True)
        logger.info(f"JMComic 下载目录: {self.download_dir}")

    async def _load_cache(self):
        """从 KV 存储加载缓存索引。"""
        try:
            cached = await self.get_kv_data("jm_cache")
            self.cache = cached if isinstance(cached, list) else []
        except Exception:
            self.cache = []

    async def _save_cache(self):
        """持久化缓存索引。"""
        try:
            await self.put_kv_data("jm_cache", self.cache)
        except Exception as e:
            logger.error(f"保存缓存索引失败: {e}")

    # ──────────────────────────── 指令: /jm ────────────────────────────

    @filter.command("jm", alias={"JM", "jmcomic"})
    async def jm_download(self, event: AstrMessageEvent, comic_id: str = ""):
        """下载 JMComic 漫画并发送。用法: /jm <漫画ID>"""
        # ── 1. 校验输入 ──
        if not comic_id or not comic_id.strip().isdigit():
            yield event.plain_result("❌ 请提供有效的漫画数字 ID，如：/jm 350234")
            return

        comic_id = comic_id.strip()
        max_pages = self.config.get("max_pages", 50)

        # ── 2. 获取漫画元数据（API 客户端） ──
        try:
            option = self._build_jm_option()
            client = option.build_jm_client()
            album = client.get_album_detail(comic_id)

            if album is None:
                yield event.plain_result(
                    f"❌ 未找到 ID 为 {comic_id} 的漫画，请检查 ID 是否正确。"
                )
                return

            album_title = album.name or f"漫画 {comic_id}"

            # API 客户端 page_count 永远为 0，通过获取第一个章节的图片数来估算
            total_pages = 0
            try:
                first_photo_id = album.episode_list[0][0]  # (photo_id, index, title)
                first_photo = client.get_photo_detail(first_photo_id)
                if first_photo and hasattr(first_photo, 'page_arr') and first_photo.page_arr:
                    images_per_chapter = len(first_photo.page_arr)
                    total_pages = images_per_chapter * len(album.episode_list)
            except Exception:
                pass  # 获取失败则跳过页数检查

            logger.info(f"获取到漫画 [{comic_id}] {album_title}，约 {total_pages} 页")
        except Exception as e:
            logger.error(f"获取漫画信息失败: {e}", exc_info=True)
            yield event.plain_result(f"❌ 获取漫画信息失败（ID: {comic_id}）：{e}")
            return

        # ── 3. 页数限制检查（total_pages 为 0 表示估算失败，跳过检查） ──
        if total_pages > 0 and total_pages > max_pages:
            yield event.plain_result(
                f"⚠️ 漫画「{album_title}」约 {total_pages} 页，"
                f"超过了限制的 {max_pages} 页，无法下载。"
            )
            return

        # ── 4. 缓存命中检查 ──
        cached = self._find_in_cache(comic_id)
        if cached and Path(cached["file_path"]).is_file():
            yield event.plain_result(f"📖 从缓存中找到「{album_title}」，直接发送…")
            safe_name = f"jm_{comic_id}{Path(cached['file_path']).suffix}"
            yield event.chain_result(
                [Comp.File(file=str(cached["file_path"]), name=safe_name)]
            )
            return

        # ── 5. 确定导出格式 ──
        default_format = self.config.get("default_format", "ask")
        export_format = None

        pages_info = f"约 {total_pages} 页" if total_pages > 0 else ""

        if default_format in ("pdf", "zip"):
            export_format = default_format
            yield event.plain_result(
                f"📖 漫画「{album_title}」{pages_info}，"
                f"将导出为 {export_format.upper()} 格式。"
            )
        else:
            # 交互询问用户（session_waiter 不支持 return，用可变容器传递结果）
            try:
                yield event.plain_result(
                    f"📖 漫画「{album_title}」{pages_info}。\n"
                    f"请选择导出格式（回复数字）：\n"
                    f"1️⃣  PDF（合并为一个文件）\n"
                    f"2️⃣  ZIP（打包所有图片）\n\n"
                    f"⏳ 请在 60 秒内回复，超时将自动取消。"
                )

                result_box = []

                @session_waiter(timeout=60, record_history_chains=False)
                async def format_waiter(
                    controller: SessionController, wait_event: AstrMessageEvent
                ):
                    choice = wait_event.message_str.strip()
                    if choice in ("1", "pdf", "PDF"):
                        result_box.append("pdf")
                        controller.stop()
                    elif choice in ("2", "zip", "ZIP"):
                        result_box.append("zip")
                        controller.stop()
                    else:
                        await wait_event.send(
                            wait_event.plain_result("❓ 无效选择，请回复 1（PDF）或 2（ZIP）。")
                        )
                        controller.keep(timeout=60, reset_timeout=True)

                await format_waiter(event)
                export_format = result_box[0] if result_box else None

            except TimeoutError:
                yield event.plain_result("⏰ 选择超时，已取消下载。")
                return
            except Exception as e:
                logger.error(f"会话等待异常: {e}")
                yield event.plain_result(f"❌ 发生错误：{e}")
                return

        if not export_format:
            yield event.plain_result("❌ 未选择导出格式，已取消。")
            return

        # ── 6. 执行下载（阻塞操作放线程池） ──
        yield event.plain_result(
            f"⬇️ 开始下载「{album_title}」（{export_format.upper()}）…\n"
            f"⏳ 图片较多时可能需要几分钟，请耐心等待。"
        )

        try:
            loop = asyncio.get_event_loop()
            album_result, _ = await loop.run_in_executor(
                None, self._do_download, comic_id
            )

            if album_result is None:
                yield event.plain_result("❌ 下载失败，请检查 ID 是否正确或网络连接。")
                return

            # ── 7. 导出 PDF / ZIP ──
            if export_format == "pdf":
                file_path = await loop.run_in_executor(
                    None, self._create_pdf, album_result, album_title, comic_id
                )
            else:
                file_path = await loop.run_in_executor(
                    None, self._create_zip, album_result, album_title, comic_id
                )

            if not file_path or not Path(file_path).is_file():
                yield event.plain_result("❌ 导出文件失败。")
                return

            # ── 8. 更新缓存 ──
            await self._update_cache(comic_id, album_title, file_path)

            # ── 9. 发送文件（用安全文件名，避免特殊字符导致QQ传输失败） ──
            safe_name = f"jm_{comic_id}{Path(file_path).suffix}"
            yield event.chain_result(
                [Comp.File(file=str(file_path), name=safe_name)]
            )

        except Exception as e:
            logger.error(f"下载/导出失败: {e}", exc_info=True)
            yield event.plain_result(f"❌ 下载或导出失败：{e}")

    # ──────────────────────────── 指令: /jmhelp ────────────────────────────

    @filter.command("jmhelp", alias={"jmh"})
    async def jm_help(self, event: AstrMessageEvent):
        """显示 JMComic 插件使用帮助。"""
        text = (
            "📖 JMComic 漫画下载器 使用说明\n"
            "─────────────────────\n\n"
            "📌 基本指令：\n"
            "  /jm <漫画ID>  - 下载漫画并发送\n"
            "  /jmhelp       - 显示本帮助\n"
            "  /jmcache      - 查看已缓存的漫画\n"
            "  /jmclear      - 清空所有缓存\n\n"
            "📌 使用示例：\n"
            "  /jm 350234    - 下载 ID 为 350234 的漫画\n"
            "  /jm 12345     - 下载 ID 为 12345 的漫画\n\n"
            "📌 流程说明：\n"
            "  1. 发送 /jm <ID>\n"
            "  2. 机器人会显示漫画信息和总页数\n"
            "  3. 选择导出格式（PDF 或 ZIP）\n"
            "  4. 等待下载完成并接收文件\n\n"
            "📌 注意事项：\n"
            f"  • 页数限制：最多 {self.config.get('max_pages', 50)} 页\n"
            f"  • 缓存上限：最多保留 {self.config.get('max_cache', 5)} 部漫画\n"
            "  • 已缓存的漫画会直接发送，无需重新下载\n"
            "  • 下载需要时间，请耐心等待"
        )
        yield event.plain_result(text)

    # ──────────────────────────── 指令: /jmcache ────────────────────────────

    @filter.command("jmcache", alias={"jmc"})
    async def jm_cache_list(self, event: AstrMessageEvent):
        """查看已缓存的漫画列表。"""
        if not self.cache:
            yield event.plain_result("📭 当前没有缓存的漫画。")
            return

        lines = [f"📚 已缓存 {len(self.cache)} 部漫画：\n"]
        for i, item in enumerate(self.cache, 1):
            exists = "✅" if Path(item["file_path"]).is_file() else "❌"
            lines.append(
                f"  {i}. {exists} 「{item['title']}」(ID: {item['comic_id']}) "
                f"[{item['format'].upper()}]"
            )
        lines.append(f"\n✅ = 文件存在  ❌ = 文件已丢失")
        yield event.plain_result("\n".join(lines))

    # ──────────────────────────── 指令: /jmclear ────────────────────────────

    @filter.command("jmclear", alias={"jmcl"})
    async def jm_clear_cache(self, event: AstrMessageEvent):
        """清空所有缓存的漫画文件。"""
        if not self.cache:
            yield event.plain_result("📭 当前没有缓存，无需清理。")
            return

        count = 0
        for item in list(self.cache):
            file_path = Path(item["file_path"])
            if file_path.is_file():
                try:
                    file_path.unlink()
                    count += 1
                except Exception as e:
                    logger.warning(f"删除缓存文件失败: {file_path} - {e}")

        self.cache = []
        await self._save_cache()
        yield event.plain_result(f"🗑️ 已清理 {count} 个缓存文件。")

    # ──────────────────────────── 核心方法 ────────────────────────────

    def _build_jm_option(self, impl: str = "api"):
        """构建 jmcomic JmOption 配置对象。impl: 'api'(移动API) 或 'html'(网页)"""
        from jmcomic import JmOption

        proxy = self.config.get("proxy", "").strip()
        image_suffix = self.config.get("image_suffix", "").strip() or None
        image_decode = self.config.get("image_decode", True)

        option_dict = {
            "dir_rule": {
                "rule": "Bd_Aid",
                "base_dir": str(self.download_dir),
            },
            "download": {
                "cache": True,
                "image": {
                    "decode": image_decode,
                    "suffix": image_suffix,
                },
                "threading": {"image": 30},
            },
            "client": {
                "impl": impl,
                "retry_times": 5,
                "postman": {
                    "type": "curl_cffi",
                    "meta_data": {
                        "impersonate": "chrome",
                        "proxies": (
                            {"https": proxy, "http": proxy} if proxy else None
                        ),
                    },
                },
            },
        }

        return JmOption.construct(option_dict)

    def _do_download(self, comic_id: str):
        """同步下载漫画（在线程池中调用）。返回 (album, downloader) 或 (None, None)。"""
        try:
            from jmcomic import download_album

            option = self._build_jm_option()
            album, downloader = download_album(comic_id, option)
            return album, downloader
        except Exception as e:
            logger.error(f"下载漫画 {comic_id} 失败: {e}", exc_info=True)
            return None, None

    def _find_image_files(self, album_dir: Path) -> list:
        """递归扫描目录，按顺序返回所有图片文件路径。"""
        suffixes = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
        images = []
        for root, _, files in os.walk(album_dir):
            for f in files:
                if Path(f).suffix.lower() in suffixes:
                    images.append(Path(root) / f)
        images.sort(key=lambda p: str(p))
        return images

    def _create_pdf(self, album, album_title: str, comic_id: str) -> str | None:
        """将下载的漫画图片合并为 PDF。返回文件路径。"""
        try:
            from PIL import Image

            album_dir = self.download_dir / comic_id
            if not album_dir.is_dir():
                album_dir = self.download_dir / str(comic_id)
                if not album_dir.is_dir():
                    logger.error(f"找不到漫画目录: {album_dir}")
                    return None

            images = self._find_image_files(album_dir)
            if not images:
                logger.error(f"目录中没有找到图片: {album_dir}")
                return None

            safe_title = self._safe_filename(album_title)
            pdf_path = self.download_dir / f"{safe_title}_{comic_id}.pdf"

            pil_images = []
            for img_path in images:
                img = Image.open(img_path)
                if img.mode == "RGBA":
                    bg = Image.new("RGB", img.size, (255, 255, 255))
                    bg.paste(img, mask=img.split()[3])
                    img = bg
                elif img.mode != "RGB":
                    img = img.convert("RGB")
                pil_images.append(img)

            if pil_images:
                pil_images[0].save(
                    str(pdf_path),
                    "PDF",
                    save_all=True,
                    append_images=pil_images[1:],
                    quality=95,
                )
                logger.info(f"PDF 生成成功: {pdf_path} ({len(pil_images)} 页)")
                return str(pdf_path)

            return None
        except Exception as e:
            logger.error(f"创建 PDF 失败: {e}", exc_info=True)
            return None

    def _create_zip(self, album, album_title: str, comic_id: str) -> str | None:
        """将下载的漫画图片打包为 ZIP。返回文件路径。"""
        try:
            album_dir = self.download_dir / comic_id
            if not album_dir.is_dir():
                album_dir = self.download_dir / str(comic_id)
                if not album_dir.is_dir():
                    logger.error(f"找不到漫画目录: {album_dir}")
                    return None

            images = self._find_image_files(album_dir)
            if not images:
                logger.error(f"目录中没有找到图片: {album_dir}")
                return None

            safe_title = self._safe_filename(album_title)
            zip_path = self.download_dir / f"{safe_title}_{comic_id}.zip"

            with zipfile.ZipFile(str(zip_path), "w", zipfile.ZIP_DEFLATED) as zf:
                for idx, img_path in enumerate(images, 1):
                    ext = img_path.suffix
                    arc_name = f"{idx:04d}{ext}"
                    zf.write(str(img_path), arc_name)

            logger.info(f"ZIP 生成成功: {zip_path} ({len(images)} 张图片)")
            return str(zip_path)
        except Exception as e:
            logger.error(f"创建 ZIP 失败: {e}", exc_info=True)
            return None

    # ──────────────────────────── 缓存管理 ────────────────────────────

    def _find_in_cache(self, comic_id: str) -> dict | None:
        """在缓存中查找指定漫画。"""
        for item in self.cache:
            if item.get("comic_id") == comic_id:
                return item
        return None

    async def _update_cache(self, comic_id: str, title: str, file_path: str):
        """更新缓存记录，必要时清理超出上限的旧缓存。"""
        # 移除同 ID 旧记录
        self.cache = [c for c in self.cache if c.get("comic_id") != comic_id]

        self.cache.append(
            {
                "comic_id": comic_id,
                "title": title,
                "file_path": file_path,
                "format": "pdf" if file_path.endswith(".pdf") else "zip",
            }
        )
        await self._save_cache()

        # 清理超出上限的旧缓存
        max_cache = self.config.get("max_cache", 5)
        while len(self.cache) > max_cache:
            oldest = self.cache.pop(0)
            old_path = Path(oldest["file_path"])
            if old_path.is_file():
                try:
                    old_path.unlink()
                    logger.info(f"清理旧缓存: {old_path}")
                except Exception as e:
                    logger.warning(f"清理旧缓存失败: {old_path} - {e}")
            # 同时清理对应的下载目录
            old_dir = self.download_dir / oldest.get("comic_id", "")
            if old_dir.is_dir():
                try:
                    shutil.rmtree(old_dir)
                except Exception:
                    pass
        await self._save_cache()

    # ──────────────────────────── Pages 后端 API ────────────────────────────

    async def _api_cache_list(self):
        """GET /cache/list — 返回缓存列表及文件状态。"""
        result = []
        for item in self.cache:
            file_path = Path(item["file_path"])
            album_dir = self.download_dir / item.get("comic_id", "")
            image_count = len(self._find_image_files(album_dir)) if album_dir.is_dir() else 0
            result.append(
                {
                    "comic_id": item.get("comic_id", ""),
                    "title": item.get("title", ""),
                    "format": item.get("format", ""),
                    "file_exists": file_path.is_file(),
                    "file_size": file_path.stat().st_size if file_path.is_file() else 0,
                    "file_name": file_path.name,
                    "image_count": image_count,
                }
            )
        return jsonify({"cache": result, "download_dir": str(self.download_dir)})

    async def _api_cache_preview(self):
        """GET /cache/preview?comic_id=xxx — 返回漫画第一张图片的 base64。"""
        comic_id = request.args.get("comic_id", "")
        if not comic_id:
            return jsonify({"error": "missing comic_id"}), 400

        album_dir = self.download_dir / comic_id
        if not album_dir.is_dir():
            return jsonify({"error": "album directory not found"}), 404

        images = self._find_image_files(album_dir)
        if not images:
            return jsonify({"error": "no images found"}), 404

        first_image = images[0]
        try:
            with open(first_image, "rb") as f:
                data = f.read()
            b64 = base64.b64encode(data).decode("utf-8")
            suffix = first_image.suffix.lower().lstrip(".")
            mime_map = {"jpg": "jpeg", "jpeg": "jpeg", "png": "png", "webp": "webp", "gif": "gif"}
            mime = mime_map.get(suffix, "jpeg")
            return jsonify(
                {
                    "comic_id": comic_id,
                    "image_count": len(images),
                    "data_url": f"data:image/{mime};base64,{b64}",
                }
            )
        except Exception as e:
            return jsonify({"error": f"read image failed: {e}"}), 500

    async def _api_cache_delete(self):
        """POST /cache/delete — body: {"comic_id": "xxx"} 删除指定缓存。"""
        body = await request.get_json(force=True, silent=True) or {}
        comic_id = body.get("comic_id", "")
        if not comic_id:
            return jsonify({"error": "missing comic_id"}), 400

        target = self._find_in_cache(comic_id)
        if not target:
            return jsonify({"error": "comic not found in cache"}), 404

        file_path = Path(target["file_path"])
        if file_path.is_file():
            try:
                file_path.unlink()
            except Exception as e:
                logger.warning(f"删除缓存文件失败: {file_path} - {e}")

        album_dir = self.download_dir / comic_id
        if album_dir.is_dir():
            try:
                shutil.rmtree(album_dir)
            except Exception as e:
                logger.warning(f"删除缓存目录失败: {album_dir} - {e}")

        self.cache = [c for c in self.cache if c.get("comic_id") != comic_id]
        await self._save_cache()

        return jsonify({"deleted": comic_id})

    async def _api_cache_download(self):
        """GET /cache/download?comic_id=xxx — 下载缓存文件。"""
        comic_id = request.args.get("comic_id", "")
        if not comic_id:
            return jsonify({"error": "missing comic_id"}), 400

        target = self._find_in_cache(comic_id)
        if not target:
            return jsonify({"error": "comic not found in cache"}), 404

        file_path = Path(target["file_path"])
        if not file_path.is_file():
            return jsonify({"error": "file not found on disk"}), 404

        safe_name = f"jm_{comic_id}{file_path.suffix}"
        return await send_file(
            str(file_path),
            mimetype="application/pdf" if file_path.suffix == ".pdf" else "application/zip",
            as_attachment=True,
            attachment_filename=safe_name,
        )

    # ──────────────────────────── 工具方法 ────────────────────────────

    @staticmethod
    def _safe_filename(name: str) -> str:
        """移除文件名中的非法字符。"""
        illegal = '<>:"/\\|?*'
        for ch in illegal:
            name = name.replace(ch, "_")
        return name.strip()[:100] or "comic"
