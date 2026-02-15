# AdvancedTransfer v3.0 — 功能差距分析

对比参考代码 `torrenttransfer` 插件，列出已实现、已优化和未实现的功能。

## ✅ 已实现 / 已优化

| 功能 | 参考做法 | 本插件做法 | 状态 |
|------|---------|-----------|------|
| 三场景 (A/B/C) | 转移 + 辅种 | 转移 + Tracker合并 + 跳过 | ✅ |
| Cron 调度 | VTextField + CronTrigger | VCronField + CronTrigger (内置选择器) | ✅ 优化 |
| 立即运行一次 | onlyonce 标志 | 相同 | ✅ |
| 种子内容获取 | 从文件系统读 .torrent + .fastresume | API 导出 torrents_export | ✅ 优化 |
| Auto-Start | is_paused=True + check_recheck 3min轮询 | is_paused=False 自动校验做种 | ✅ 简化 |
| Tracker 注入 | 仅 QB update_tracker | QB + TR 双回退 (tracker_list / tracker_add) | ✅ 优化 |
| 通知 | SiteMessage | NotificationType.Plugin | ✅ |
| 调试模式 | 无 | debug 开关控制详细日志 | ✅ 新增 |
| 互斥锁 | threading.Lock | 相同 | ✅ |
| 历史记录 | save_data 防重复 | 相同 + 记录场景/时间 | ✅ |
| Partial Download Sync | 无 | 同步源端文件选择状态到目标 | ✅ 新增 |

## ❌ 未实现 — 后续可扩展

| 功能 | 参考实现方式 | 优先级 | 说明 |
|------|------------|--------|------|
| 路径映射 (frompath/topath) | fromtorrentpath / frompath / topath 多组映射 | 🔴 高 | 源/目标挂载路径不一致时必需 |
| 标签/分类过滤 | nolabels / includelabels / includecategory | 🟡 中 | 仅转移特定标签/分类的种子 |
| 排除路径 | nopaths | 🟡 中 | 排除特定保存路径不做转移 |
| 删除源种子 | deletesource | 🟡 中 | 当前仅暂停，不删除 (更安全) |
| 删除重复 | deleteduplicate | 🟢 低 | 发现目标有重复时删除源端该种子 |
| 自定义标签 | add_torrent_tags (QB) | 🟢 低 | 在目标端添加自定义标签标记来源 |
| 空标签处理 | transferemptylabel | 🟢 低 | 是否转移无标签种子 |
| fastresume 解析 | 从 QB BT_backup 目录读取 tracker | 🟢 低 | 本插件使用 API 获取，不需要 |
| check_recheck 轮询 | is_paused=True → 轮询 → start | 🟢 低 | 本插件用 is_paused=False 代替 |

## v3.0 新增/改进详情

### Partial Download Sync
- 读取源端文件选择状态:
  - QB: `get_files()` → `priority == 0` 为未选中
  - TR: `get_files()` → `selected == False` 为未选中
- 添加到目标后同步:
  - QB: `set_files(torrent_hash=, file_ids=, priority=0)`
  - TR: `set_unwanted_files(tid, file_ids)`
- 仅对 .torrent 文件内容有效 (Magnet 无法预设文件优先级)
- infohash 相同 → 文件索引一致 (由 .torrent info 字典决定)

### TR Tracker 双回退
- 方案 1: `server.update_tracker(tracker_list=[[url1], [url2], ...])`
  - 每个 tracker 作为独立 tier，`transmission-rpc` 转为 `"url1\n\nurl2"` 格式
  - 适用于 Transmission 4.0+ (RPC version 17+)
- 方案 2: `server.trc.change_torrent(ids=hash, tracker_add=[url, ...])`
  - 直接追加 tracker URL
  - 适用于 Transmission 3.x 回退

### Auto-Start (is_paused=False)
- 参考代码: `is_paused=True` → `check_recheck` 每3分钟轮询 → `start_torrents`
- 本插件: `is_paused=False` → QB/TR 自动 Verifying → Seeding
- 更简洁，无需后台轮询任务
