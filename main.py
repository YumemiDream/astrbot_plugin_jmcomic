import asyncio
import base64
import io
import json
import math
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
    "通过数字 ID 下载 JMComic 漫画，支持导出 PDF/ZIP、详情查看、搜索、排行榜、分类浏览",
    "1.1.0",
)
class JmComicPlugin(Star):
    """JMComic 漫画下载器插件"""

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.cache = []
        self._ensure_download_dir()
        self._load_cache_sync()

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
        context.register_web_api(
            f"/{PLUGIN_NAME}/detail",
            self._api_detail,
            ["GET"],
            "获取漫画详情",
        )

    async def initialize(self):
        """异步初始化：再次从文件加载缓存索引（兼容 AstrBot 在 __init__ 之后调用）。"""
        self._load_cache_sync()

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

    def _load_cache_sync(self):
        """从本地 JSON 文件同步加载缓存索引。"""
        try:
            cache_file = self.download_dir / "cache_index.json"
            if cache_file.is_file():
                with open(cache_file, "r", encoding="utf-8") as f:
                    cached = json.load(f)
                self.cache = cached if isinstance(cached, list) else []
                logger.info(f"已加载 {len(self.cache)} 条缓存记录")
        except Exception as e:
            logger.error(f"加载缓存索引失败: {e}")
            self.cache = []

    async def _save_cache(self):
        """持久化缓存索引到本地 JSON 文件。"""
        try:
            cache_file = self.download_dir / "cache_index.json"
            with open(cache_file, "w", encoding="utf-8") as f:
                json.dump(self.cache, f, ensure_ascii=False, indent=2)
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

            # API 客户端 page_count 可能为 0，使用统一方法估算
            total_pages = self._estimate_page_count(album, client)

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
            "  /jm <漫画ID>        - 下载漫画并发送\n"
            "  /jmview <漫画ID>    - 查看漫画详情\n"
            "  /jmcover <漫画ID>   - 发送漫画封面\n"
            "  /jmsearch <关键词>  - 站内搜索漫画\n"
            "  /jmrank <day|week|month> - 查看排行榜\n"
            "  /jmcategory <分类>  - 按分类浏览漫画\n"
            "  /jmcache            - 查看已缓存的漫画\n"
            "  /jmclear            - 清空所有缓存\n"
            "  /jmhelp             - 显示本帮助\n\n"
            "📌 使用示例：\n"
            "  /jm 350234          - 下载 ID 为 350234 的漫画\n"
            "  /jmview 350234      - 查看 ID 为 350234 的漫画详情\n"
            "  /jmcover 350234     - 发送该漫画封面\n"
            "  /jmsearch 無修正 2  - 搜索「無修正」，第 2 页\n"
            "  /jmrank week        - 查看周排行榜\n"
            "  /jmcategory doujin  - 浏览同人分类\n\n"
            "📌 流程说明：\n"
            "  1. 发送 /jm <ID>\n"
            "  2. 机器人会显示漫画信息和总页数\n"
            "  3. 选择导出格式（PDF 或 ZIP）\n"
            "  4. 等待下载完成并接收文件\n\n"
            "📌 注意事项：\n"
            f"  • 页数限制：最多 {self.config.get('max_pages', 50)} 页\n"
            f"  • 缓存上限：最多保留 {self.config.get('max_cache', 5)} 部漫画\n"
            f"  • 搜索/排行榜每页最多显示 {self.config.get('search_result_limit', 10)} 条\n"
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

        nodes = [self._build_header_node(event, f"📚 已缓存 {len(self.cache)} 部漫画")]
        for i, item in enumerate(self.cache, 1):
            file_path = Path(item["file_path"])
            exists = "✅ 文件正常" if file_path.is_file() else "❌ 文件已丢失"
            album_dir = self.download_dir / item.get("comic_id", "")
            image_count = len(self._find_image_files(album_dir)) if album_dir.is_dir() else 0
            file_size = file_path.stat().st_size if file_path.is_file() else 0
            extra = (
                f"格式：{item.get('format', '').upper()} | "
                f"大小：{self._format_size(file_size)} | "
                f"图片：{image_count} 张 | {exists}"
            )
            nodes.append(
                self._build_result_node(
                    event, i, item.get("title", "未知"), item.get("comic_id", ""), extra=extra
                )
            )
        yield event.chain_result(nodes)

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

    # ──────────────────────────── 指令: /jmview ────────────────────────────

    @filter.command("jmview", alias={"jmv"})
    async def jm_view(self, event: AstrMessageEvent, comic_id: str = ""):
        """查看 JMComic 漫画详情。用法: /jmview <漫画ID>"""
        comic_id = self._parse_jm_id(comic_id)
        if not comic_id:
            yield event.plain_result("❌ 请提供有效的漫画数字 ID，如：/jmview 350234")
            return

        try:
            option = self._build_jm_option()
            client = option.build_jm_client()
            album = await self._run_sync(client.get_album_detail, comic_id)
            page_count = await self._run_sync(self._estimate_page_count, album, client)
            album.page_count = page_count
            text = self._format_album_detail(album)
            yield event.plain_result(text)
        except Exception as e:
            logger.error(f"获取漫画详情失败: {e}", exc_info=True)
            yield event.plain_result(f"❌ 获取漫画详情失败（ID: {comic_id}）：{e}")

    # ──────────────────────────── 指令: /jmcover ────────────────────────────

    @filter.command("jmcover", alias={"jmc"})
    async def jm_cover(self, event: AstrMessageEvent, comic_id: str = ""):
        """发送 JMComic 漫画封面。用法: /jmcover <漫画ID>"""
        comic_id = self._parse_jm_id(comic_id)
        if not comic_id:
            yield event.plain_result("❌ 请提供有效的漫画数字 ID，如：/jmcover 350234")
            return

        try:
            option = self._build_jm_option()
            client = option.build_jm_client()
            cover_dir = self.download_dir / "covers"
            cover_dir.mkdir(parents=True, exist_ok=True)
            cover_path = cover_dir / f"{comic_id}.jpg"

            await self._run_sync(client.download_album_cover, comic_id, str(cover_path))

            if not cover_path.is_file():
                yield event.plain_result("❌ 封面下载失败。")
                return

            yield event.chain_result([Comp.Image(file=str(cover_path))])
        except Exception as e:
            logger.error(f"下载封面失败: {e}", exc_info=True)
            yield event.plain_result(f"❌ 下载封面失败（ID: {comic_id}）：{e}")

    # ──────────────────────────── 指令: /jmsearch ────────────────────────────

    @filter.command("jmsearch", alias={"jms"})
    async def jm_search(self, event: AstrMessageEvent, args: str = ""):
        """站内搜索漫画。用法: /jmsearch <关键词> [页码]"""
        query, page = self._parse_search_args(args)
        if not query:
            yield event.plain_result("❌ 请提供搜索关键词，如：/jmsearch 無修正")
            return

        try:
            option = self._build_jm_option()
            client = option.build_jm_client()
            result = await self._run_sync(client.search_site, query, page)
            limit = self.config.get("search_result_limit", 10)

            nodes = [
                self._build_header_node(
                    event, f"🔍 站内搜索「{query}」\n📄 第 {page}/{result.page_count} 页，本页 {len(result.content)} 条"
                )
            ]
            for i, (aid, ainfo) in enumerate(result.content[:limit], 1):
                nodes.append(
                    self._build_result_node(
                        event, i, ainfo.get("name", "未知"), aid, ainfo.get("tags", [])
                    )
                )
            if len(result.content) > limit:
                nodes.append(
                    self._build_header_node(
                        event, f"... 本页还有 {len(result.content) - limit} 条，翻页可查看更多"
                    )
                )
            yield event.chain_result(nodes)
        except Exception as e:
            logger.error(f"搜索失败: {e}", exc_info=True)
            yield event.plain_result(f"❌ 搜索失败：{e}")

    # ──────────────────────────── 指令: /jmrank ────────────────────────────

    @filter.command("jmrank", alias={"jmr"})
    async def jm_rank(self, event: AstrMessageEvent, args: str = ""):
        """查看 JMComic 排行榜。用法: /jmrank <day|week|month> [页码]"""
        from jmcomic import JmMagicConstants
        time_value, page = self._parse_rank_args(args)
        if time_value is None:
            yield event.plain_result("❌ 请指定时间范围：day/week/month，如：/jmrank week")
            return

        try:
            option = self._build_jm_option()
            client = option.build_jm_client()

            if time_value == JmMagicConstants.TIME_TODAY:
                result = await self._run_sync(client.day_ranking, page)
                label = "日排行"
            elif time_value == JmMagicConstants.TIME_WEEK:
                result = await self._run_sync(client.week_ranking, page)
                label = "周排行"
            else:
                result = await self._run_sync(client.month_ranking, page)
                label = "月排行"

            limit = self.config.get("search_result_limit", 10)
            nodes = [
                self._build_header_node(
                    event, f"🏆 {label}\n📄 第 {page}/{result.page_count} 页，本页 {len(result.content)} 条"
                )
            ]
            for i, (aid, ainfo) in enumerate(result.content[:limit], 1):
                nodes.append(
                    self._build_result_node(
                        event, i, ainfo.get("name", "未知"), aid, ainfo.get("tags", [])
                    )
                )
            if len(result.content) > limit:
                nodes.append(
                    self._build_header_node(
                        event, f"... 本页还有 {len(result.content) - limit} 条，翻页可查看更多"
                    )
                )
            yield event.chain_result(nodes)
        except Exception as e:
            logger.error(f"获取排行榜失败: {e}", exc_info=True)
            yield event.plain_result(f"❌ 获取排行榜失败：{e}")

    # ──────────────────────────── 指令: /jmcategory ────────────────────────────

    @filter.command("jmcategory", alias={"jmcg"})
    async def jm_category(self, event: AstrMessageEvent, args: str = ""):
        """按分类浏览漫画。用法: /jmcategory <分类> [子分类] [页码]"""
        from jmcomic import JmMagicConstants
        category, sub_category, page = self._parse_category_args(args)
        if not category:
            yield event.plain_result(
                "❌ 请提供分类，如：/jmcategory doujin\n"
                "可选分类：doujin、single、short、hanman、meiman、3D、another、english_site 等"
            )
            return

        try:
            option = self._build_jm_option()
            client = option.build_jm_client()
            result = await self._run_sync(
                client.categories_filter,
                page,
                JmMagicConstants.TIME_ALL,
                category,
                JmMagicConstants.ORDER_BY_LATEST,
                sub_category,
            )
            limit = self.config.get("search_result_limit", 10)
            header = f"📂 分类「{category}」" + (f" / {sub_category}" if sub_category else "")
            nodes = [
                self._build_header_node(
                    event, f"{header}\n📄 第 {page}/{result.page_count} 页，本页 {len(result.content)} 条"
                )
            ]
            for i, (aid, ainfo) in enumerate(result.content[:limit], 1):
                nodes.append(
                    self._build_result_node(
                        event, i, ainfo.get("name", "未知"), aid, ainfo.get("tags", [])
                    )
                )
            if len(result.content) > limit:
                nodes.append(
                    self._build_header_node(
                        event, f"... 本页还有 {len(result.content) - limit} 条，翻页可查看更多"
                    )
                )
            yield event.chain_result(nodes)
        except Exception as e:
            logger.error(f"分类浏览失败: {e}", exc_info=True)
            yield event.plain_result(f"❌ 分类浏览失败：{e}")

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

    # ──────────────────────────── 查询类辅助方法 ────────────────────────────

    async def _run_sync(self, func, *args, **kwargs):
        """在线程池中执行同步函数，避免阻塞事件循环。"""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, lambda: func(*args, **kwargs))

    def _parse_jm_id(self, text: str) -> str | None:
        """解析 JM 车号，支持纯数字或 JMxxx 形式。"""
        from jmcomic import JmcomicText
        try:
            return JmcomicText.parse_to_jm_id(text.strip())
        except Exception:
            return None

    def _strip_command_prefix(self, source, prefixes: tuple) -> str:
        """从 message_str 或纯文本中去除指令前缀，保留参数部分。"""
        if hasattr(source, "message_str"):
            text = source.message_str
        else:
            text = source
        text = (text or "").strip()
        for prefix in prefixes:
            if text.startswith(prefix):
                return text[len(prefix):].strip()
        return text

    def _parse_search_args(self, text: str) -> tuple[str | None, int]:
        """解析 /jmsearch 参数：关键词 [页码]。

        当关键词本身为纯数字时（如搜索车号），不会误解析为页码；
        只有存在两个及以上词组且最后一个为数字时，才视为指定页码。
        """
        text = self._strip_command_prefix(text, ("/jmsearch ", "/jms "))
        if not text:
            return None, 1
        parts = text.split()
        if len(parts) >= 2 and parts[-1].isdigit():
            page = int(parts[-1])
            query = " ".join(parts[:-1]).strip()
        else:
            page = 1
            query = text
        return query or None, page

    def _parse_rank_args(self, text: str) -> tuple[str | None, int]:
        """解析 /jmrank 参数：<day|week|month> [页码]。"""
        from jmcomic import JmMagicConstants
        text = self._strip_command_prefix(text, ("/jmrank ", "/jmr "))
        parts = text.split()
        if not parts:
            return None, 1
        time_map = {
            "day": JmMagicConstants.TIME_TODAY,
            "d": JmMagicConstants.TIME_TODAY,
            "today": JmMagicConstants.TIME_TODAY,
            "week": JmMagicConstants.TIME_WEEK,
            "w": JmMagicConstants.TIME_WEEK,
            "month": JmMagicConstants.TIME_MONTH,
            "m": JmMagicConstants.TIME_MONTH,
        }
        time_key = parts[0].lower()
        if time_key not in time_map:
            return None, 1
        page = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 1
        return time_map[time_key], page

    def _parse_category_args(self, text: str) -> tuple[str | None, str | None, int]:
        """解析 /jmcategory 参数：<分类> [子分类] [页码]。"""
        text = self._strip_command_prefix(text, ("/jmcategory ", "/jmcg "))
        parts = text.split()
        if not parts:
            return None, None, 1
        category = parts[0]
        sub_category = None
        page = 1
        if len(parts) >= 2:
            if parts[-1].isdigit():
                page = int(parts[-1])
                if len(parts) >= 3:
                    sub_category = parts[1]
            else:
                sub_category = parts[1]
                if len(parts) >= 3 and parts[2].isdigit():
                    page = int(parts[2])
        return category, sub_category, page

    def _format_album_detail(self, album) -> str:
        """将 JmAlbumDetail 格式化为可读文本。"""
        lines = [
            f"📖 标题：{album.name}",
            f"🆔 ID：{album.album_id}",
            f"✍️ 作者：{', '.join(album.authors) if album.authors else '未知'}",
            f"📚 作品：{', '.join(album.works) if album.works else '无'}",
            f"🎭 人物：{', '.join(album.actors) if album.actors else '无'}",
            f"🏷️ 标签：{', '.join(album.tags) if album.tags else '无'}",
            f"📄 总页数：{album.page_count}",
            f"👀 观看：{album.views or '-'}  ❤️ 点赞：{album.likes or '-'}",
            f"📅 发布：{self._format_date(album.pub_date)}  更新：{self._format_date(album.update_date)}",
            f"📝 章节 ({len(album.episode_list)}):",
        ]
        for i, ep in enumerate(album.episode_list[:20], 1):
            pid = ep[0]
            pname = ep[2] if len(ep) > 2 else f"第{i}話"
            lines.append(f"  {i}. {pname} (ID: {pid})")
        if len(album.episode_list) > 20:
            lines.append(f"  ... 还有 {len(album.episode_list) - 20} 个章节")
        return "\n".join(lines)

    def _format_page_content(self, page, title: str, page_no: int) -> str:
        """将 JmSearchPage / JmCategoryPage 格式化为可读文本。"""
        limit = self.config.get("search_result_limit", 10)
        lines = [title, f"📄 第 {page_no}/{page.page_count} 页，本页 {len(page.content)} 条"]
        for i, (aid, ainfo) in enumerate(page.content[:limit], 1):
            name = ainfo.get("name", "未知")
            tags = ainfo.get("tags", [])
            tag_str = f" | 🏷️ {', '.join(tags[:5])}" if tags else ""
            lines.append(f"{i}. 「{name}」(ID: {aid}){tag_str}")
        if len(page.content) > limit:
            lines.append(f"\n... 本页还有 {len(page.content) - limit} 条，翻页可查看更多")
        return "\n".join(lines)

    def _estimate_page_count(self, album, client) -> int:
        """API 客户端返回的 page_count 可能为 0，尝试估算真实页数。

        优先使用 album.page_count；若为 0，则获取第一个章节的图片数并按章节数估算。
        """
        if album.page_count and int(album.page_count) > 0:
            return int(album.page_count)

        try:
            if not album.episode_list:
                return 0
            first_photo_id = album.episode_list[0][0]
            first_photo = client.get_photo_detail(first_photo_id)
            if first_photo and first_photo.page_arr:
                return len(first_photo.page_arr) * len(album.episode_list)
        except Exception as e:
            logger.warning(f"估算页数失败: {e}")

        return 0

    @staticmethod
    def _format_date(value) -> str:
        """把禁漫 API 返回的 0/空日期格式化为可读文本。"""
        if value is None:
            return "-"
        s = str(value).strip()
        if s in ("", "0", "None"):
            return "-"
        return s

    @staticmethod
    def _format_size(bytes_val: int) -> str:
        """把字节数格式化为可读大小。"""
        if bytes_val == 0:
            return "0 B"
        k = 1024
        sizes = ["B", "KB", "MB", "GB"]
        i = int(math.floor(math.log(bytes_val) / math.log(k)))
        return f"{bytes_val / math.pow(k, i):.1f} {sizes[i]}"

    def _build_result_node(self, event: AstrMessageEvent, index: int, title: str, comic_id: str, tags: list = None, extra: str = "") -> Comp.Node:
        """构建合并转发消息中的单条结果 Node。"""
        lines = [f"{index}. {title}", f"ID: {comic_id}"]
        if tags:
            lines.append(f"标签：{', '.join(tags[:5])}")
        if extra:
            lines.append(extra)
        return Comp.Node(
            uin=event.get_self_id() or "0",
            name="JMComicBot",
            content=[Comp.Plain("\n".join(lines))],
        )

    def _build_header_node(self, event: AstrMessageEvent, text: str) -> Comp.Node:
        """构建合并转发消息中的标题 Node。"""
        return Comp.Node(
            uin=event.get_self_id() or "0",
            name="JMComicBot",
            content=[Comp.Plain(text)],
        )

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

    async def _api_detail(self):
        """GET /detail?comic_id=xxx — 返回漫画详情。"""
        comic_id = request.args.get("comic_id", "")
        if not comic_id:
            return jsonify({"error": "missing comic_id"}), 400

        target = self._find_in_cache(comic_id)
        if not target:
            return jsonify({"error": "comic not found in cache"}), 404

        try:
            option = self._build_jm_option()
            client = option.build_jm_client()
            album = await self._run_sync(client.get_album_detail, comic_id)
            page_count = await self._run_sync(self._estimate_page_count, album, client)
            album.page_count = page_count

            episodes = []
            for i, ep in enumerate(album.episode_list, 1):
                pid = ep[0]
                pname = ep[2] if len(ep) > 2 else f"第{i}話"
                episodes.append({"index": i, "photo_id": pid, "title": pname})

            return jsonify(
                {
                    "comic_id": album.album_id,
                    "title": album.name,
                    "description": album.description,
                    "authors": album.authors,
                    "works": album.works,
                    "actors": album.actors,
                    "tags": album.tags,
                    "page_count": album.page_count,
                    "views": album.views,
                    "likes": album.likes,
                    "pub_date": self._format_date(album.pub_date),
                    "update_date": self._format_date(album.update_date),
                    "episodes": episodes,
                }
            )
        except Exception as e:
            logger.error(f"获取漫画详情失败: {e}", exc_info=True)
            return jsonify({"error": f"fetch detail failed: {e}"}), 500

    # ──────────────────────────── 工具方法 ────────────────────────────

    @staticmethod
    def _safe_filename(name: str) -> str:
        """移除文件名中的非法字符。"""
        illegal = '<>:"/\\|?*'
        for ch in illegal:
            name = name.replace(ch, "_")
        return name.strip()[:100] or "comic"
