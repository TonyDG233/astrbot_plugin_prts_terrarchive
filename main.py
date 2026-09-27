"""PRTS 泰拉档案 · AstrBot 插件入口。

- 注册 prts_search / prts_read / prts_timeline / prts_i18n 四个 LLM 工具
- /prts 命令组：状态 / 检查更新 / 更新 / 版本 / 激活 / 删除 / 帮助
- on_llm_request：按需注入 <prts:retrieval-context> 实体提示（extra_user_content_parts）
- 语料存储在 data/plugin_data/<插件名>/releases，插件代码目录随时可被覆盖安装

模块导入采用防同名冲突策略：先把插件目录插入 sys.path，再检查 sys.modules 中
既有的 prts_corpus 是否属于本目录，否则清除后从本目录重新导入。
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools
from astrbot.core.agent.message import TextPart
from astrbot.core.star.star import star_map

PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
if PLUGIN_DIR not in sys.path:
    sys.path.insert(0, PLUGIN_DIR)

_existing_package = sys.modules.get("prts_corpus")
if _existing_package is not None:
    _existing_file = getattr(_existing_package, "__file__", "") or ""
    if _existing_file and os.path.dirname(os.path.abspath(_existing_file)) != PLUGIN_DIR:
        for _name in [
            name for name in list(sys.modules)
            if name == "prts_corpus" or name.startswith("prts_corpus.")
        ]:
            del sys.modules[_name]

try:
    import aiohttp
except ImportError:  # requirements.txt 未安装时仅禁用更新能力
    aiohttp = None

from prts_corpus import constants
from prts_corpus import installer as prts_installer
from prts_corpus import recognizer as prts_recognizer
from prts_corpus import tools as prts_tools
from prts_corpus import versions as prts_versions
from prts_corpus.errors import ContractError, InstallerFault
from prts_corpus.evidence import EvidenceRegistry
from prts_corpus.store import CorpusStore

DEFAULT_PLUGIN_NAME = "astrbot_plugin_prts_terrarchive"
RELEASE_WATCH_INTERVAL_SECONDS = 6 * 3600
ACTIVE_STATE_FILENAME = "prts-active-state.json"

DEFAULT_SETTINGS = {
    "enabled_games": ["arknights", "endfield"],
    "releases_dir": "",
    "site_base_url": constants.DEFAULT_SITE_BASE_URL,
    "download_order": ["modelscope", "site"],
    "pinned_release": "",
    "auto_check_update": False,
    "auto_install_on_start": False,
    "enable_search_tool": True,
    "enable_read_tool": True,
    "enable_timeline_tool": True,
    "enable_i18n_tool": True,
    "inject_entity_context": True,
    "command_enabled": True,
    "content_cache_mb": 64,
    "index_cache_mb": 32,
    "cloud_enabled": False,
}

HELP_TEXT = "\n".join([
    "PRTS 泰拉档案 · 命令",
    "/prts 状态 — 本地语料安装状态",
    "/prts 检查更新 — 查询远端最新版本（不下载）",
    "/prts 更新 [release_id] — 下载并激活语料（管理员）",
    "/prts 版本 — 列出本地已下载版本",
    "/prts 激活 <release_id> — 切换到指定版本（管理员）",
    "/prts 删除 <release_id> — 删除指定版本（管理员，不能删除当前版本）",
    "提示：语料下载完成后即可离线检索；如需固定版本，请在插件配置中设置 pinned_release。",
])


def _normalize_settings(config) -> dict:
    raw: dict = {}
    try:
        if isinstance(config, dict):
            raw = dict(config)
        elif config is not None:
            raw = {key: config.get(key) for key in DEFAULT_SETTINGS}
    except Exception:
        raw = {}
    settings = dict(DEFAULT_SETTINGS)
    for key in DEFAULT_SETTINGS:
        value = raw.get(key)
        if value is None:
            continue
        settings[key] = value
    settings["enabled_games"] = [
        game for game in (settings["enabled_games"] or []) if game in ("arknights", "endfield")
    ] or ["arknights", "endfield"]
    order = [source for source in (settings["download_order"] or []) if source in ("modelscope", "site")]
    settings["download_order"] = order or ["modelscope", "site"]
    for key in ("content_cache_mb", "index_cache_mb"):
        try:
            settings[key] = max(8, min(1024, int(settings[key])))
        except (TypeError, ValueError):
            settings[key] = DEFAULT_SETTINGS[key]
    settings["site_base_url"] = str(settings["site_base_url"] or "").strip() or constants.DEFAULT_SITE_BASE_URL
    settings["pinned_release"] = str(settings["pinned_release"] or "").strip()
    settings["releases_dir"] = str(settings["releases_dir"] or "").strip()
    for key in (
        "auto_check_update", "auto_install_on_start", "enable_search_tool", "enable_read_tool",
        "enable_timeline_tool", "enable_i18n_tool", "inject_entity_context", "command_enabled",
    ):
        settings[key] = bool(settings[key])
    return settings


def _fault_text(error: Exception) -> str:
    if isinstance(error, (InstallerFault, ContractError)):
        return f"{error.code}: {error.message}"
    return str(error)


class PrtsArchive(Star):
    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context, config)
        metadata = star_map.get(type(self).__module__)
        self.plugin_name = (
            (metadata.name if metadata and metadata.name else None)
            or getattr(self, "name", None)
            or DEFAULT_PLUGIN_NAME
        )
        self.settings = _normalize_settings(config)
        self.module_path = type(self).__module__
        data_dir = StarTools.get_data_dir(self.plugin_name)
        self.plugin_data_dir = Path(data_dir)
        releases_dir = (
            Path(self.settings["releases_dir"]) if self.settings["releases_dir"]
            else Path(data_dir) / "releases"
        )
        self.releases_dir = str(releases_dir)
        self.store = CorpusStore(
            self.releases_dir,
            content_cache_bytes=self.settings["content_cache_mb"] * 1024 * 1024,
            index_cache_bytes=self.settings["index_cache_mb"] * 1024 * 1024,
        )
        self.evidence = EvidenceRegistry()
        self._tasks: list[asyncio.Task] = []
        self._install_lock = asyncio.Lock()
        self._session = None
        self._started = False

    # ---- 生命周期 ----

    async def initialize(self) -> None:
        if self._started:  # 配置保存触发的 reload 也会调用 initialize
            return
        self._started = True
        try:
            self.context.add_llm_tools(*prts_tools.make_tools(self, self.module_path))
        except Exception as error:
            logger.error(f"[prts] 注册 LLM 工具失败：{error}", exc_info=True)
        try:
            await self.store.ready()
            self.logger.info(
                f"[prts] 本地语料已加载：release={self.store.release_id} "
                f"data_version={str(self.store.data_version or '')[:12]} "
                f"documents={len(self.store.documents)}"
            )
        except Exception as error:
            self.logger.warning(f"[prts] 本地语料未就绪（{_fault_text(error)}），请执行 /prts 更新")
        if self.settings["auto_check_update"]:
            self._tasks.append(asyncio.create_task(self._release_watch_loop()))
        if self.settings["auto_install_on_start"] and self.store.data_version is None:
            self._tasks.append(asyncio.create_task(self._auto_install()))

    async def terminate(self) -> None:
        self._started = False
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        self._tasks.clear()
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None
        self.store.reset()

    # ---- LLM 请求注入 ----

    @filter.on_llm_request()
    async def prts_inject_context(self, event: AstrMessageEvent, req) -> None:
        if not self.settings["inject_entity_context"]:
            return
        if getattr(self.store, "data_version", None) is None:
            return
        text = str(event.message_str or "").strip()
        if len(text) < 2:
            return
        try:
            recognizer = await prts_recognizer.prepare_entity_recognition(self.store)
            if recognizer is None:
                return
            context_text = await prts_recognizer.build_retrieval_context(
                self.store, recognizer, text, self.settings["enabled_games"],
                getattr(self.store, "data_version", None),
            )
        except Exception as error:
            self.logger.debug(f"[prts] 实体识别跳过：{error}")
            return
        if not context_text:
            return
        try:
            req.extra_user_content_parts.append(TextPart(text=context_text))
        except Exception as error:
            self.logger.debug(f"[prts] 注入检索上下文失败：{error}")

    # ---- 后台任务 ----

    async def _http_session(self):
        if aiohttp is None:
            raise InstallerFault("DEPENDENCY_MISSING", "缺少 aiohttp 依赖，请先安装 requirements.txt")
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=None))
        return self._session

    async def _release_watch_loop(self) -> None:
        while True:
            await asyncio.sleep(RELEASE_WATCH_INTERVAL_SECONDS)
            try:
                session = await self._http_session()
                current = await prts_installer.resolve_trusted_current_release(
                    session=session, site_base_url=self.settings["site_base_url"]
                )
                remote = str(current.get("data_version") or current.get("dataVersion") or "")
                if remote and remote != getattr(self.store, "data_version", None):
                    self.logger.info(
                        "[prts] 检测到语料更新（远端 %s / 本地 %s）；执行 /prts 更新 应用。",
                        remote[:12], str(getattr(self.store, "data_version", "") or "(未安装)")[:12],
                    )
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self.logger.debug(f"[prts] 更新检查失败：{_fault_text(error)}")

    async def _auto_install(self) -> None:
        try:
            await self._run_update(release_id="")
        except Exception as error:
            self.logger.warning(f"[prts] 自动安装失败：{_fault_text(error)}")

    async def _run_update(self, release_id: str, progress=None) -> dict:
        session = await self._http_session()
        async with self._install_lock:
            result = await prts_installer.ensure_corpus_release(
                releases_dir=self.releases_dir,
                release_id=release_id or self.settings["pinned_release"] or None,
                enabled_games=self.settings["enabled_games"],
                download_order=self.settings["download_order"],
                site_base_url=self.settings["site_base_url"],
                session=session,
                on_progress=progress,
            )
        self.store.reset()
        await self.store.ready()
        return result

    # ---- 命令组 ----

    @filter.command_group("prts")
    def prts(self):
        """PRTS 泰拉档案"""

    @prts.command("状态")
    async def prts_status(self, event: AstrMessageEvent):
        if not self.settings["command_enabled"]:
            return
        status = await prts_versions.local_release_status(self.releases_dir)
        lines = ["PRTS 泰拉档案 · 状态"]
        if not status.get("installed"):
            lines.append("本地语料：未安装（执行 /prts 更新 下载）")
        else:
            data_version = str(status.get("data_version") or "")
            lines.append(f"当前版本：{status.get('active_release_id')}")
            lines.append(f"资料版本：{data_version[:12]}…" if data_version else "资料版本：未知")
            lines.append(
                f"文档 {status.get('documents', 0)} 篇 / 行 {status.get('lines', 0)} 条 / "
                f"压缩体积 {int(status.get('compressed_bytes') or 0) / 1024 / 1024:.1f} MiB"
            )
            packs = status.get("packs") or []
            if packs:
                lines.append("已装载资料包：" + "、".join(str(pack) for pack in packs))
        lines.append(f"本地版本数：{status.get('releases_count', 0)}")
        lines.append("启用资料库：" + " + ".join(self.settings["enabled_games"]))
        lines.append(f"存储目录：{self.releases_dir}")
        if self.settings["pinned_release"]:
            lines.append(f"已固定版本：{self.settings['pinned_release']}")
        yield event.plain_result("\n".join(lines))

    @prts.command("检查更新")
    async def prts_check_update(self, event: AstrMessageEvent):
        if not self.settings["command_enabled"]:
            return
        try:
            session = await self._http_session()
            current = await prts_installer.resolve_trusted_current_release(
                session=session, site_base_url=self.settings["site_base_url"]
            )
        except Exception as error:
            yield event.plain_result(f"检查更新失败：{_fault_text(error)}")
            return
        remote = str(current.get("data_version") or current.get("dataVersion") or "")
        remote_release = str(current.get("release_id") or current.get("releaseId") or "")
        local = str(getattr(self.store, "data_version", "") or "")
        if remote and remote == local:
            yield event.plain_result(f"已是最新版本（{remote_release}）。")
        else:
            yield event.plain_result(
                f"发现新版本：{remote_release}\n远端资料版本：{remote[:12]}…\n"
                f"本地资料版本：{local[:12] + '…' if local else '未安装'}\n执行 /prts 更新 下载并激活。"
            )

    @prts.command("更新")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def prts_update(self, event: AstrMessageEvent, release_id: str = ""):
        if not self.settings["command_enabled"]:
            return
        target = str(release_id or "").strip() or self.settings["pinned_release"]
        yield event.plain_result("开始检查并下载 PRTS 语料，请稍候……")
        last_report = {"files_done": -1}

        def on_progress(info: dict) -> None:
            files_done = int(info.get("files_done") or 0)
            files_total = int(info.get("files_total") or 0)
            if files_total and files_done != last_report["files_done"] and files_done % 32 == 0:
                last_report["files_done"] = files_done
                self.logger.info(f"[prts] 下载进度 {files_done}/{files_total}")

        try:
            result = await self._run_update(target, progress=on_progress)
        except Exception as error:
            yield event.plain_result(f"更新失败：{_fault_text(error)}")
            return
        if result.get("reused"):
            yield event.plain_result(f"本地已是最新版本（{result.get('release_id')}），未重复下载。")
            return
        yield event.plain_result(
            f"更新完成：{result.get('release_id')}\n"
            f"资料版本：{str(result.get('data_version') or '')[:12]}…\n"
            f"下载 {int(result.get('downloaded_bytes') or 0) / 1024 / 1024:.1f} MiB，"
            f"复用 {int(result.get('skipped_bytes') or 0) / 1024 / 1024:.1f} MiB。\n"
            f"现在可以离线检索了。"
        )

    @prts.command("版本")
    async def prts_releases(self, event: AstrMessageEvent):
        if not self.settings["command_enabled"]:
            return
        releases = await prts_versions.list_local_releases(self.releases_dir)
        if not releases:
            yield event.plain_result("本地还没有任何语料版本，执行 /prts 更新 下载。")
            return
        lines = ["PRTS 泰拉档案 · 本地版本"]
        for item in releases[:20]:
            marker = "●" if item.get("active") else "○"
            validity = "" if item.get("valid") else f"（无效：{item.get('reason') or '校验失败'}）"
            size = int(item.get("compressed_size") or 0) / 1024 / 1024
            lines.append(
                f"{marker} {item.get('release_id')}｜{int(item.get('document_count') or 0)} 篇 / "
                f"{int(item.get('line_count') or 0)} 行 / {size:.1f} MiB{validity}"
            )
        if len(releases) > 20:
            lines.append(f"……共 {len(releases)} 个版本（仅显示前 20 个）")
        lines.append("激活：/prts 激活 <release_id>；删除：/prts 删除 <release_id>")
        yield event.plain_result("\n".join(lines))

    @prts.command("激活")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def prts_activate(self, event: AstrMessageEvent, release_id: str):
        if not self.settings["command_enabled"]:
            return
        try:
            pointer = await prts_versions.activate_release(self.releases_dir, str(release_id).strip())
            self.store.reset()
            await self.store.ready()
        except Exception as error:
            yield event.plain_result(f"激活失败：{_fault_text(error)}")
            return
        yield event.plain_result(
            f"已激活 {pointer.get('release_id')}（资料版本 {str(pointer.get('data_version') or '')[:12]}…）。"
        )

    @prts.command("删除")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def prts_delete(self, event: AstrMessageEvent, release_id: str):
        if not self.settings["command_enabled"]:
            return
        try:
            await prts_versions.delete_release(self.releases_dir, str(release_id).strip())
        except Exception as error:
            yield event.plain_result(f"删除失败：{_fault_text(error)}")
            return
        yield event.plain_result(f"已删除 {release_id}。")

    @prts.command("帮助")
    async def prts_help(self, event: AstrMessageEvent):
        if not self.settings["command_enabled"]:
            return
        yield event.plain_result(HELP_TEXT)