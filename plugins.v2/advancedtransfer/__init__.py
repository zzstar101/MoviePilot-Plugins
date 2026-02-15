"""
AdvancedTransfer - 高级种子转移插件 v1.0

架构特性:
  1. Cron-Only 调度 — 通过 cron 表达式触发，每次执行一轮完整扫描后退出。
  2. Fire-and-Forget 策略 — 以 is_paused=False 添加到目标，自动校验做种。
     参考代码 (torrenttransfer) 使用 is_paused=True + check_recheck 轮询，
     本插件采用 is_paused=False 让客户端自行完成 Verifying → Seeding 全流程。
  3. Partial Download Sync — 读取源端文件选择状态 (QB priority=0 / TR selected=False)，
     添加到目标后同步 unwanted 文件索引，保留用户的文件取消选择。
     注意: 仅对 .torrent 内容有效，Magnet 链接无法在元数据获取前设置文件优先级。
  4. Transmission Tracker 双回退 — 先尝试 tracker_list (TR 4.0+)，
     失败后回退 tracker_add (TR 3.x)，解决 "Invalid tracker list" 错误。
  5. 智能三场景 (Cross-Seeding):
     A: 目标无该 Hash → 完整添加 + 同步文件选择状态
     B: 目标有 Hash 但缺少 Tracker → 注入缺失 Tracker
     C: 完全重复 → 跳过
  6. 安全保障: 仅使用 stop_torrents (暂停)，绝不调用 delete/remove。
"""

import time
import threading
import urllib.parse
from datetime import datetime, timedelta
from typing import Any, List, Dict, Tuple, Optional, Union

import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.core.config import settings
from app.helper.downloader import DownloaderHelper
from app.log import logger
from app.plugins import _PluginBase
from app.schemas import NotificationType


class AdvancedTransfer(_PluginBase):
    """
    高级种子转移插件
    自动将做种任务从源下载器转移到目标下载器，支持 PT 辅种智能合并。
    Cron 调度 + Fire-and-Forget + Partial Download Sync。
    """

    # ==================== 插件元信息 ====================
    plugin_name = "高级种子转移"
    plugin_desc = "自动将做种任务从源下载器转移到目标下载器，支持PT辅种智能合并（Cron调度）"
    plugin_icon = "advancedtransfer.png"
    plugin_version = "1.0"
    plugin_author = "zzstar101"
    plugin_order = 18

    # ==================== 配置属性 ====================
    _enabled: bool = False
    _onlyonce: bool = False
    _notify: bool = False
    _debug: bool = False
    _source_id: str = ""
    _target_id: str = ""
    _cron_expression: str = ""
    _clean_source: bool = False

    # ==================== 运行时 ====================
    _scheduler: Optional[BackgroundScheduler] = None
    _lock: threading.Lock = threading.Lock()

    # ================================================================
    #                       生命周期方法
    # ================================================================

    def init_plugin(self, config: dict = None):
        """加载配置"""
        self.stop_service()

        if config:
            self._enabled = config.get("enabled", False)
            self._onlyonce = config.get("onlyonce", False)
            self._notify = config.get("notify", False)
            self._debug = config.get("debug", False)
            self._source_id = config.get("source_id", "")
            self._target_id = config.get("target_id", "")
            self._cron_expression = config.get("cron_expression", "")
            self._clean_source = config.get("clean_source", False)

        # 立即运行一次
        if self._onlyonce:
            self._onlyonce = False
            self.__save_config()
            self._scheduler = BackgroundScheduler(timezone=settings.TZ)
            self._scheduler.add_job(
                func=self.transfer_torrents,
                trigger="date",
                run_date=datetime.now(
                    pytz.timezone(settings.TZ)
                ) + timedelta(seconds=3),
                name=f"{self.plugin_name}",
            )
            if self._scheduler.get_jobs():
                self._scheduler.print_jobs()
                self._scheduler.start()

    def __save_config(self):
        """保存配置"""
        self.update_config({
            "enabled": self._enabled,
            "onlyonce": self._onlyonce,
            "notify": self._notify,
            "debug": self._debug,
            "source_id": self._source_id,
            "target_id": self._target_id,
            "cron_expression": self._cron_expression,
            "clean_source": self._clean_source,
        })

    def get_state(self) -> bool:
        return bool(
            self._enabled
            and self._cron_expression
            and self._source_id
            and self._target_id
        )

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        pass

    def get_api(self) -> List[Dict[str, Any]]:
        return []

    def get_service(self) -> List[Dict[str, Any]]:
        """注册 Cron 定时服务"""
        if not self.get_state():
            return []
        try:
            cron_trigger = CronTrigger.from_crontab(self._cron_expression)
        except Exception as e:
            logger.error(
                f"【{self.plugin_name}】Cron 表达式无效 "
                f"'{self._cron_expression}': {e}"
            )
            return []
        return [
            {
                "id": "AdvancedTransfer",
                "name": self.plugin_name,
                "trigger": cron_trigger,
                "func": self.transfer_torrents,
                "kwargs": {},
            }
        ]

    def get_page(self) -> Optional[List[dict]]:
        return None

    def stop_service(self):
        """停止插件服务"""
        try:
            if self._scheduler:
                self._scheduler.remove_all_jobs()
                if self._scheduler.running:
                    self._scheduler.shutdown()
                self._scheduler = None
        except Exception as e:
            logger.error(f"停止 {self.plugin_name} 出错: {e}")

    # ================================================================
    #                       配置表单
    # ================================================================

    def get_form(self) -> Tuple[Optional[List[dict]], Dict[str, Any]]:
        """拼装插件配置页面"""
        downloader_options = self.__get_downloader_options()

        return [
            # ---- 第一行: 开关组 ----
            {
                "component": "VRow",
                "content": [
                    {
                        "component": "VCol",
                        "props": {"cols": 12, "md": 3},
                        "content": [
                            {
                                "component": "VSwitch",
                                "props": {
                                    "model": "enabled",
                                    "label": "启用插件",
                                },
                            }
                        ],
                    },
                    {
                        "component": "VCol",
                        "props": {"cols": 12, "md": 3},
                        "content": [
                            {
                                "component": "VSwitch",
                                "props": {
                                    "model": "notify",
                                    "label": "发送通知",
                                },
                            }
                        ],
                    },
                    {
                        "component": "VCol",
                        "props": {"cols": 12, "md": 3},
                        "content": [
                            {
                                "component": "VSwitch",
                                "props": {
                                    "model": "debug",
                                    "label": "调试日志",
                                },
                            }
                        ],
                    },
                    {
                        "component": "VCol",
                        "props": {"cols": 12, "md": 3},
                        "content": [
                            {
                                "component": "VSwitch",
                                "props": {
                                    "model": "onlyonce",
                                    "label": "立即运行一次",
                                },
                            }
                        ],
                    },
                ],
            },
            # ---- 第二行: 下载器选择 + Cron ----
            {
                "component": "VRow",
                "content": [
                    {
                        "component": "VCol",
                        "props": {"cols": 12, "md": 4},
                        "content": [
                            {
                                "component": "VSelect",
                                "props": {
                                    "model": "source_id",
                                    "label": "源下载器",
                                    "items": downloader_options,
                                },
                            }
                        ],
                    },
                    {
                        "component": "VCol",
                        "props": {"cols": 12, "md": 4},
                        "content": [
                            {
                                "component": "VSelect",
                                "props": {
                                    "model": "target_id",
                                    "label": "目标下载器",
                                    "items": downloader_options,
                                },
                            }
                        ],
                    },
                    {
                        "component": "VCol",
                        "props": {"cols": 12, "md": 4},
                        "content": [
                            {
                                "component": "VCronField",
                                "props": {
                                    "model": "cron_expression",
                                    "label": "执行周期",
                                    "placeholder": "如：0 4 * * *",
                                },
                            }
                        ],
                    },
                ],
            },
            # ---- 第三行: 高级选项 ----
            {
                "component": "VRow",
                "content": [
                    {
                        "component": "VCol",
                        "props": {"cols": 12, "md": 6},
                        "content": [
                            {
                                "component": "VSwitch",
                                "props": {
                                    "model": "clean_source",
                                    "label": "转移后暂停源任务",
                                    "hint": "成功转移/合并后暂停源下载器中的种子（仅暂停，不删除）",
                                    "persistent-hint": True,
                                },
                            }
                        ],
                    },
                ],
            },
            # ---- 说明 ----
            {
                "component": "VRow",
                "content": [
                    {
                        "component": "VCol",
                        "props": {"cols": 12},
                        "content": [
                            {
                                "component": "VAlert",
                                "props": {
                                    "type": "info",
                                    "variant": "tonal",
                                    "text": (
                                        "Cron 调度 + Fire-and-Forget 策略\n\n"
                                        "三种场景处理：\n"
                                        "A. 完整转移 - 目标无该种子，添加并自动校验做种，"
                                        "同步源端文件选择状态（Partial Download Sync）\n"
                                        "B. 辅种合并 - 目标已有相同文件但来自不同站点，"
                                        "智能注入Tracker（兼容TR 3.x/4.0+）\n"
                                        "C. 跳过重复 - 目标已有完全相同的种子和Tracker\n\n"
                                        "注意：源和目标下载器需能访问相同的文件路径"
                                        "（如共享挂载目录）"
                                    ),
                                },
                            }
                        ],
                    }
                ],
            },
        ], {
            "enabled": False,
            "onlyonce": False,
            "notify": False,
            "debug": False,
            "source_id": "",
            "target_id": "",
            "cron_expression": "0 4 * * *",
            "clean_source": False,
        }

    @staticmethod
    def __get_downloader_options() -> List[dict]:
        """获取可用的下载器选项列表"""
        options = []
        try:
            dl_helper = DownloaderHelper()
            for name, service in dl_helper.get_services().items():
                dl_type = service.type or "unknown"
                options.append({
                    "title": f"{name} ({dl_type})",
                    "value": name,
                })
        except Exception as e:
            logger.debug(f"获取下载器列表出错: {e}")
        return options

    # ================================================================
    #                       核心转移逻辑
    # ================================================================

    def transfer_torrents(self):
        """Cron 触发入口 — 互斥锁保护，单次扫描。"""
        if not self._lock.acquire(blocking=False):
            logger.warning(
                f"【{self.plugin_name}】上一次任务仍在运行，跳过"
            )
            return
        try:
            self._do_transfer()
        except Exception as e:
            logger.error(
                f"【{self.plugin_name}】执行异常: {e}", exc_info=True
            )
        finally:
            self._lock.release()

    def _do_transfer(self):
        """执行一轮完整转移"""
        logger.info(f"【{self.plugin_name}】开始执行转移任务...")

        if self._source_id == self._target_id:
            logger.error(
                f"【{self.plugin_name}】源和目标下载器不能相同"
            )
            return

        dl_helper = DownloaderHelper()

        source_service = dl_helper.get_service(name=self._source_id)
        if not source_service or not source_service.instance:
            logger.error(
                f"【{self.plugin_name}】源下载器 '{self._source_id}' 不可用"
            )
            return

        target_service = dl_helper.get_service(name=self._target_id)
        if not target_service or not target_service.instance:
            logger.error(
                f"【{self.plugin_name}】目标下载器 '{self._target_id}' 不可用"
            )
            return

        source_server = source_service.instance
        source_type: str = source_service.type
        target_server = target_service.instance
        target_type: str = target_service.type

        history: dict = self.get_data("history") or {}

        source_torrents = source_server.get_completed_torrents()
        if source_torrents is None:
            logger.error(
                f"【{self.plugin_name}】获取源下载器种子列表失败"
            )
            return
        if not source_torrents:
            logger.info(
                f"【{self.plugin_name}】源下载器暂无已完成种子"
            )
            return

        logger.info(
            f"【{self.plugin_name}】获取到 "
            f"{len(source_torrents)} 个已完成种子"
        )

        stats = {
            "transferred": 0,
            "merged": 0,
            "skipped": 0,
            "failed": 0,
            "details": [],
        }

        for torrent in source_torrents:
            try:
                self._process_single_torrent(
                    source_type, source_server,
                    target_type, target_server,
                    torrent, history, stats,
                )
            except Exception as e:
                logger.error(
                    f"【{self.plugin_name}】处理种子异常: {e}",
                    exc_info=True,
                )
                stats["failed"] += 1

        self.save_data("history", history)

        logger.info(
            f"【{self.plugin_name}】执行完成 — "
            f"转移: {stats['transferred']}, 合并: {stats['merged']}, "
            f"跳过: {stats['skipped']}, 失败: {stats['failed']}"
        )

        total_actions = (
            stats["transferred"] + stats["merged"] + stats["failed"]
        )
        if self._notify and total_actions > 0:
            self._send_notification(stats)

    def _process_single_torrent(
        self,
        source_type: str, source_server,
        target_type: str, target_server,
        torrent, history: dict, stats: dict,
    ):
        """处理单个种子"""
        meta = self._extract_meta(source_type, source_server, torrent)
        if not meta or not meta.get("hash"):
            return

        torrent_hash = meta["hash"].lower()

        if torrent_hash in history:
            return

        self._log_debug(
            f"处理: {meta['name']} [{torrent_hash[:8]}...]"
        )

        # 获取种子内容 (优先 .torrent bytes，Magnet 无法同步文件选择)
        content = self._get_torrent_content(
            source_type, source_server, meta
        )
        if not content:
            logger.warning(
                f"【{self.plugin_name}】无法获取种子内容: {meta['name']}"
            )
            stats["failed"] += 1
            return

        # 执行三场景逻辑
        scenario, success = self._execute_transfer(
            target_type, target_server, torrent_hash, meta, content
        )

        if not success:
            stats["failed"] += 1
            stats["details"].append(f"[失败] {meta['name']}")
            return

        # 写入历史
        history[torrent_hash] = {
            "name": meta.get("name", ""),
            "scenario": scenario,
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "source": self._source_id,
            "target": self._target_id,
        }

        if scenario == "A":
            stats["transferred"] += 1
            stats["details"].append(f"[转移] {meta['name']}")
        elif scenario == "B":
            stats["merged"] += 1
            stats["details"].append(f"[合并] {meta['name']}")
        else:
            stats["skipped"] += 1
            return  # C 场景不暂停源

        # 成功后暂停源
        if self._clean_source:
            self._stop_source_torrent(
                source_server, torrent_hash, meta["name"]
            )

    # ================================================================
    #                       元数据提取
    # ================================================================

    def _extract_meta(
        self, service_type: str, server, torrent
    ) -> Optional[dict]:
        """提取种子元数据，包含 unwanted_file_ids"""
        try:
            if service_type == "qbittorrent":
                return self._extract_qb_meta(server, torrent)
            elif service_type == "transmission":
                return self._extract_tr_meta(server, torrent)
            else:
                logger.warning(
                    f"【{self.plugin_name}】不支持的下载器类型: "
                    f"{service_type}"
                )
                return None
        except Exception as e:
            logger.error(
                f"【{self.plugin_name}】提取元数据出错: {e}"
            )
            return None

    def _extract_qb_meta(self, server, torrent) -> dict:
        """提取 qBittorrent 种子元数据"""
        torrent_hash = torrent.get("hash", "")
        name = torrent.get("name", "")
        save_path = torrent.get("save_path", "")
        category = torrent.get("category", "")
        tags = torrent.get("tags", "")

        tracker_urls = self._get_qb_tracker_urls(server, torrent_hash)
        unwanted = self._get_qb_unwanted_files(server, torrent_hash)

        self._log_debug(
            f"QB 元数据 [{torrent_hash[:8]}]: name={name}, "
            f"save_path={save_path}, trackers={len(tracker_urls)}, "
            f"unwanted_files={len(unwanted)}"
        )

        return {
            "hash": torrent_hash,
            "name": name,
            "save_path": save_path,
            "category": category,
            "tags": tags,
            "labels": [],
            "trackers": tracker_urls,
            "unwanted_file_ids": unwanted,
        }

    def _extract_tr_meta(self, server, torrent) -> dict:
        """提取 Transmission 种子元数据"""
        torrent_hash = torrent.hashString
        name = torrent.name
        save_path = getattr(torrent, "download_dir", "") or ""
        labels = (
            list(torrent.labels)
            if hasattr(torrent, "labels") and torrent.labels
            else []
        )
        tags = ",".join(labels)

        tracker_urls = self._get_tr_tracker_urls(torrent)
        unwanted = self._get_tr_unwanted_files(server, torrent_hash)

        self._log_debug(
            f"TR 元数据 [{torrent_hash[:8]}]: name={name}, "
            f"save_path={save_path}, trackers={len(tracker_urls)}, "
            f"unwanted_files={len(unwanted)}"
        )

        return {
            "hash": torrent_hash,
            "name": name,
            "save_path": save_path,
            "category": "",
            "tags": tags,
            "labels": labels,
            "trackers": tracker_urls,
            "unwanted_file_ids": unwanted,
        }

    # ================================================================
    #                   Unwanted File 提取
    # ================================================================

    def _get_qb_unwanted_files(
        self, server, torrent_hash: str
    ) -> List[int]:
        """
        获取 QB 源中未选择的文件索引。
        qbittorrentapi 的 `torrents_files()` 返回的每个文件有:
          - index: 文件索引 (0-based)
          - priority: 0=不下载, 1=正常, 6=高, 7=最高
        """
        try:
            files = server.get_files(torrent_hash)
            if not files:
                return []
            unwanted = []
            for f in files:
                priority = (
                    f.get("priority", 1)
                    if isinstance(f, dict)
                    else getattr(f, "priority", 1)
                )
                index = (
                    f.get("index", 0)
                    if isinstance(f, dict)
                    else getattr(f, "index", 0)
                )
                if priority == 0:
                    unwanted.append(index)
            return unwanted
        except Exception as e:
            self._log_debug(f"获取 QB 文件优先级出错: {e}")
            return []

    def _get_tr_unwanted_files(
        self, server, torrent_hash: str
    ) -> List[int]:
        """
        获取 TR 源中未选择的文件索引。
        transmission-rpc 的 `torrent.files()` 返回 File 对象:
          - id: 文件索引 (0-based)
          - selected: bool (True=下载, False=跳过)
        """
        try:
            files = server.get_files(torrent_hash)
            if not files:
                return []
            unwanted = []
            for f in files:
                selected = getattr(f, "selected", True)
                file_id = getattr(f, "id", 0)
                if not selected:
                    unwanted.append(file_id)
            return unwanted
        except Exception as e:
            self._log_debug(f"获取 TR 文件选择状态出错: {e}")
            return []

    # ================================================================
    #                       Tracker URL 提取
    # ================================================================

    @staticmethod
    def _get_qb_tracker_urls(
        server, torrent_hash: str
    ) -> List[str]:
        """获取 QB 种子的 Tracker announce URL 列表"""
        urls = []
        try:
            trackers = server.qbc.torrents_trackers(
                torrent_hash=torrent_hash
            )
            for t in trackers:
                url = (
                    t.get("url", "")
                    if isinstance(t, dict)
                    else getattr(t, "url", "")
                )
                if url and url.startswith(("http", "udp")):
                    urls.append(url.strip())
        except Exception as e:
            logger.debug(f"获取 QB Tracker 出错: {e}")
        return urls

    @staticmethod
    def _get_tr_tracker_urls(torrent) -> List[str]:
        """获取 TR 种子的 Tracker announce URL 列表"""
        urls = []
        try:
            if hasattr(torrent, "trackers") and torrent.trackers:
                for t in torrent.trackers:
                    announce = (
                        t.get("announce", "")
                        if isinstance(t, dict)
                        else getattr(t, "announce", "")
                    )
                    if announce and announce.startswith(("http", "udp")):
                        urls.append(announce.strip())
        except Exception as e:
            logger.debug(f"获取 TR Tracker 出错: {e}")
        return urls

    # ================================================================
    #                     Tracker URL 清洗
    # ================================================================

    def _sanitize_tracker_list(self, tracker_list: List) -> List[str]:
        """
        清洗 Tracker URL 列表为严格 List[str]。
        展平嵌套 → str() → strip() → 过滤无效 → 去重保序。
        """
        clean = []
        for item in tracker_list:
            if isinstance(item, (list, tuple)):
                for sub in item:
                    url = str(sub).strip()
                    if url and url.startswith(("http", "udp")):
                        clean.append(url)
            elif isinstance(item, str):
                url = item.strip()
                if url and url.startswith(("http", "udp")):
                    clean.append(url)
            elif item is not None:
                url = str(item).strip()
                if url and url.startswith(("http", "udp")):
                    clean.append(url)

        # 去重保序
        seen = set()
        result = []
        for u in clean:
            if u not in seen:
                seen.add(u)
                result.append(u)

        self._log_debug(
            f"Tracker 清洗: 输入 {len(tracker_list)} → "
            f"输出 {len(result)}"
        )
        return result

    # ================================================================
    #                       种子内容获取
    # ================================================================

    def _get_torrent_content(
        self, service_type: str, server, meta: dict
    ) -> Optional[Union[bytes, str]]:
        """
        获取种子内容: 优先 .torrent 文件 (bytes)，失败则 Magnet。
        优先 .torrent 的原因：
          1. Magnet 在元数据获取前无法设置文件优先级
          2. .torrent 添加后立即可操作文件列表
        """
        torrent_hash = meta["hash"]

        if service_type == "qbittorrent":
            try:
                torrent_bytes = server.qbc.torrents_export(
                    torrent_hash=torrent_hash
                )
                if torrent_bytes:
                    self._log_debug(
                        f"成功导出种子文件: {meta['name']}"
                    )
                    return torrent_bytes
            except Exception as e:
                self._log_debug(
                    f"导出种子文件失败，使用磁力链接: {e}"
                )

        magnet = self._build_magnet(
            info_hash=torrent_hash,
            trackers=meta.get("trackers", []),
            name=meta.get("name", ""),
        )
        self._log_debug(f"构建磁力链接: {meta['name']}")
        return magnet

    @staticmethod
    def _build_magnet(
        info_hash: str, trackers: List[str], name: str = ""
    ) -> str:
        """构建 Magnet URI"""
        magnet = f"magnet:?xt=urn:btih:{info_hash}"
        if name:
            magnet += f"&dn={urllib.parse.quote(name)}"
        for tracker in trackers:
            magnet += f"&tr={urllib.parse.quote(tracker, safe='')}"
        return magnet

    # ================================================================
    #                 三场景转移/合并 (Fire-and-Forget)
    # ================================================================

    def _execute_transfer(
        self,
        target_type: str,
        target_server,
        torrent_hash: str,
        meta: dict,
        content: Union[bytes, str],
    ) -> Tuple[Optional[str], bool]:
        """
        A - 目标无该 Hash → 添加 + 同步文件选择
        B - 目标有 Hash 但缺少 Tracker → 注入
        C - 完全重复 → 跳过
        """
        target_torrents, error = target_server.get_torrents(
            ids=torrent_hash
        )
        if error:
            logger.error(
                f"【{self.plugin_name}】查询目标下载器出错: "
                f"{meta['name']}"
            )
            return None, False

        if not target_torrents:
            return self._scenario_a(
                target_type, target_server,
                torrent_hash, meta, content,
            )

        # Hash 已存在 → 比对 Tracker
        if target_type == "qbittorrent":
            target_tracker_list = self._get_qb_tracker_urls(
                target_server, torrent_hash
            )
        else:
            target_tracker_list = self._get_tr_tracker_urls(
                target_torrents[0]
            )

        source_trackers = set(meta.get("trackers", []))
        target_trackers = set(target_tracker_list)
        missing_trackers = source_trackers - target_trackers

        if missing_trackers:
            return self._scenario_b(
                target_type, target_server, torrent_hash, meta,
                list(missing_trackers), target_tracker_list,
            )

        logger.info(
            f"【{self.plugin_name}】[C] 跳过重复: {meta['name']}"
        )
        return "C", True

    # --------------- Scenario A: 完整转移 ---------------

    def _scenario_a(
        self,
        target_type: str,
        target_server,
        torrent_hash: str,
        meta: dict,
        content: Union[bytes, str],
    ) -> Tuple[str, bool]:
        """
        目标无该 Hash → is_paused=False 添加 → 同步文件选择。

        Auto-Start 实现分析:
          参考代码 (torrenttransfer):
            - add_torrent(is_paused=True)
            - check_recheck 每3分钟轮询检查
            - 校验完暂停态 → start_torrents 手动启动

          本插件:
            - add_torrent(is_paused=False) → 自动 Verifying → Seeding
            - 无需轮询等待，API 返回即完成

        Partial Download Sync:
          仅当 content 为 bytes (.torrent 文件) 时执行。
          Magnet 链接在元数据获取前无法设置文件优先级。
        """
        logger.info(
            f"【{self.plugin_name}】[A] 完整转移: {meta['name']} "
            f"→ {self._target_id}"
        )

        add_ok = self._add_to_target(
            target_type, target_server, meta, content
        )
        if not add_ok:
            logger.error(
                f"【{self.plugin_name}】[A] 添加失败: {meta['name']}"
            )
            return "A", False

        # Partial Download Sync: 同步未选择文件
        unwanted = meta.get("unwanted_file_ids", [])
        if unwanted and isinstance(content, bytes):
            self._sync_unwanted_files(
                target_type, target_server, torrent_hash,
                unwanted, meta["name"],
            )

        logger.info(
            f"【{self.plugin_name}】[A] 转移成功 "
            f"(Fire-and-Forget): {meta['name']}"
        )
        return "A", True

    # --------------- Scenario B: 辅种合并 ---------------

    def _scenario_b(
        self,
        target_type: str,
        target_server,
        torrent_hash: str,
        meta: dict,
        missing_trackers: List[str],
        existing_tracker_list: List[str],
    ) -> Tuple[str, bool]:
        """
        目标有 Hash 但缺少 Tracker → 注入。
        QB: update_tracker (追加模式)
        TR: 双回退 (tracker_list → tracker_add)
        """
        logger.info(
            f"【{self.plugin_name}】[B] 辅种合并 - 注入 "
            f"{len(missing_trackers)} 个Tracker: {meta['name']}"
        )

        try:
            if target_type == "qbittorrent":
                clean_list = self._sanitize_tracker_list(
                    missing_trackers
                )
                self._log_debug(
                    f"[B] QB 追加 Tracker ({len(clean_list)} 个)"
                )
                result = target_server.update_tracker(
                    hash_string=torrent_hash,
                    tracker_list=clean_list,
                )
            else:
                result = self._inject_tr_trackers(
                    target_server, torrent_hash,
                    missing_trackers, existing_tracker_list,
                )

            if result:
                logger.info(
                    f"【{self.plugin_name}】[B] Tracker合并成功: "
                    f"{meta['name']}"
                )
                return "B", True
            else:
                logger.error(
                    f"【{self.plugin_name}】[B] Tracker合并失败: "
                    f"{meta['name']}"
                )
                return "B", False

        except Exception as e:
            logger.error(
                f"【{self.plugin_name}】[B] 合并异常: {e}",
                exc_info=True,
            )
            return "B", False

    def _inject_tr_trackers(
        self,
        server,
        torrent_hash: str,
        missing_trackers: List[str],
        existing_tracker_list: List[str],
    ) -> bool:
        """
        向 TR 目标注入缺失 Tracker，双回退策略:

        方案 1 — tracker_list (TR 4.0+, transmission-rpc ≥ 4.1):
          合并 existing + missing → 每个 URL 作为独立 tier
          → List[List[str]] 如 [['url1'], ['url2'], ...]
          transmission-rpc 会将其转为:
            "url1\\n\\nurl2" (tiers 间双换行)

        方案 2 — tracker_add (TR 3.x 回退):
          直接追加缺失的 tracker URL 列表
        """
        # 方案 1: tracker_list (合并 + 每个tracker独立tier)
        combined = existing_tracker_list + missing_trackers
        clean_list = self._sanitize_tracker_list(combined)
        tr_tiers = [[url] for url in clean_list]

        self._log_debug(
            f"[B] TR tracker_list: "
            f"{len(clean_list)} URLs, {len(tr_tiers)} tiers"
        )

        result = server.update_tracker(
            hash_string=torrent_hash,
            tracker_list=tr_tiers,
        )
        if result:
            return True

        # 方案 2: 回退 tracker_add
        self._log_debug(
            "[B] tracker_list 失败，回退 tracker_add 方式"
        )
        try:
            clean_missing = self._sanitize_tracker_list(missing_trackers)
            if hasattr(server, "trc") and server.trc:
                server.trc.change_torrent(
                    ids=torrent_hash,
                    tracker_add=clean_missing,
                )
                logger.info(
                    f"【{self.plugin_name}】[B] tracker_add 成功 "
                    f"({len(clean_missing)} 个)"
                )
                return True
            else:
                logger.error(
                    f"【{self.plugin_name}】[B] 无法访问 trc 客户端"
                )
                return False
        except Exception as e:
            logger.error(
                f"【{self.plugin_name}】[B] tracker_add 也失败: {e}"
            )
            return False

    # ================================================================
    #                   Partial Download Sync
    # ================================================================

    def _sync_unwanted_files(
        self,
        target_type: str,
        target_server,
        torrent_hash: str,
        unwanted_ids: List[int],
        name: str,
    ):
        """
        将源端 unwanted 文件状态同步到目标端。

        实现:
          - QB: server.set_files(torrent_hash=, file_ids=, priority=0)
            底层调用 torrents_file_priority
          - TR: server.set_unwanted_files(tid, file_ids)
            底层调用 change_torrent(files_unwanted=)

        注意:
          - 延迟 1 秒确保种子已注册到目标客户端
          - 仅对 .torrent 内容有效 (Magnet 无法在元数据前设置)
          - infohash 相同 → 文件索引一致 (由 .torrent info 字典决定)
        """
        if not unwanted_ids:
            return

        # 等待种子注册完成
        time.sleep(1)

        try:
            if target_type == "qbittorrent":
                ok = target_server.set_files(
                    torrent_hash=torrent_hash,
                    file_ids=unwanted_ids,
                    priority=0,
                )
            else:
                ok = target_server.set_unwanted_files(
                    torrent_hash, unwanted_ids
                )

            if ok:
                self._log_debug(
                    f"已同步 {len(unwanted_ids)} 个未选择文件: "
                    f"{name}"
                )
            else:
                logger.warning(
                    f"【{self.plugin_name}】文件状态同步失败: {name}"
                )
        except Exception as e:
            logger.warning(
                f"【{self.plugin_name}】文件状态同步异常: "
                f"{name} - {e}"
            )

    # ================================================================
    #                   添加种子到目标
    # ================================================================

    def _add_to_target(
        self,
        target_type: str,
        target_server,
        meta: dict,
        content: Union[bytes, str],
    ) -> bool:
        """添加种子到目标 (is_paused=False → Auto-Start)"""
        try:
            if target_type == "qbittorrent":
                return self._add_to_qb(
                    target_server, content,
                    meta.get("save_path", ""),
                    meta.get("category", ""),
                    meta.get("tags", ""),
                )
            else:
                labels = meta.get("labels", [])
                if not labels and meta.get("tags"):
                    labels = [
                        t.strip()
                        for t in meta["tags"].split(",")
                        if t.strip()
                    ]
                return self._add_to_tr(
                    target_server, content,
                    meta.get("save_path", ""),
                    labels,
                )
        except Exception as e:
            logger.error(
                f"【{self.plugin_name}】添加种子出错: {e}"
            )
            return False

    def _add_to_qb(
        self,
        server,
        content: Union[bytes, str],
        save_path: str,
        category: str,
        tags: str,
    ) -> bool:
        """
        添加到 QB。
        直接调用 qbc API (不走 wrapper) 以精确控制:
          - is_paused=False → Auto-Start (区别于参考代码的 True)
          - use_auto_torrent_management=False → 不让分类覆盖 save_path
        """
        try:
            kwargs = {
                "save_path": save_path if save_path else None,
                "is_paused": False,
                "use_auto_torrent_management": False,
            }

            if tags:
                kwargs["tags"] = tags
            if category:
                kwargs["category"] = category

            if isinstance(content, bytes):
                kwargs["torrent_files"] = content
            else:
                kwargs["urls"] = content

            self._log_debug(
                f"QB 添加: save_path={save_path}, "
                f"category={category}, "
                f"type={'bytes' if isinstance(content, bytes) else 'magnet'}"
            )

            ret = server.qbc.torrents_add(**kwargs)
            ok = bool(ret and str(ret).find("Ok") != -1)
            self._log_debug(
                f"QB 结果: {ret} → {'成功' if ok else '失败'}"
            )
            return ok

        except Exception as e:
            logger.error(
                f"【{self.plugin_name}】QB 添加出错: {e}"
            )
            return False

    def _add_to_tr(
        self,
        server,
        content: Union[bytes, str],
        save_path: str,
        labels: List[str],
    ) -> bool:
        """
        添加到 TR (is_paused=False → Auto-Start)。
        通过 wrapper 的 add_torrent 添加。
        TR 在 is_paused=False 时自动进入 Verifying → Seeding。
        """
        try:
            self._log_debug(
                f"TR 添加: save_path={save_path}, labels={labels}, "
                f"type={'bytes' if isinstance(content, bytes) else 'magnet'}"
            )

            torrent = server.add_torrent(
                content=content,
                is_paused=False,
                download_dir=save_path if save_path else None,
                labels=labels if labels else None,
            )
            ok = torrent is not None
            if ok:
                self._log_debug(
                    f"TR 添加成功: hash={torrent.hashString}"
                )
            else:
                self._log_debug("TR 添加失败: 返回 None")
            return ok
        except Exception as e:
            logger.error(
                f"【{self.plugin_name}】TR 添加出错: {e}"
            )
            return False

    # ================================================================
    #                   源任务暂停 (安全操作)
    # ================================================================

    def _stop_source_torrent(
        self,
        source_server,
        torrent_hash: str,
        name: str,
    ):
        """暂停源任务 (仅 stop，绝不 delete)"""
        try:
            result = source_server.stop_torrents(ids=torrent_hash)
            if result:
                logger.info(
                    f"【{self.plugin_name}】已暂停源任务: {name}"
                )
            else:
                logger.warning(
                    f"【{self.plugin_name}】暂停源任务失败: {name}"
                )
        except Exception as e:
            logger.error(
                f"【{self.plugin_name}】暂停源任务异常: {e}"
            )

    # ================================================================
    #                        调试 & 通知
    # ================================================================

    def _log_debug(self, msg: str):
        """调试日志（受 debug 开关控制）"""
        if self._debug:
            logger.info(f"【{self.plugin_name}·DEBUG】{msg}")

    def _send_notification(self, stats: dict):
        """发送执行结果通知"""
        title = f"【{self.plugin_name}】执行完成"
        text = (
            f"源: {self._source_id} → 目标: {self._target_id}\n"
            f"转移: {stats['transferred']} | "
            f"合并: {stats['merged']} | "
            f"跳过: {stats['skipped']} | "
            f"失败: {stats['failed']}"
        )
        details = stats.get("details", [])
        if details:
            text += "\n\n详情:\n" + "\n".join(details[:20])
            if len(details) > 20:
                text += f"\n...共 {len(details)} 条"

        self.post_message(
            mtype=NotificationType.Plugin,
            title=title,
            text=text,
        )
