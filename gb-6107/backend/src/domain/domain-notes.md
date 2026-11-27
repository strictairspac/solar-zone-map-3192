# 出货前查验闪测瓦数

炙川光伏的 10 人闪测质保与出货验收班每天按仪器日志追查试板序号。全表筛选耗时，缺测和零实际读数容易被混淆，出货抽检无法说明跳过了哪些页。上游是 D3 闪测仪的批次读数，下游是出货复核员与复测班。

09:00 从 D3 仪器取出 SL-D3-1124，09:10 确认板件连续序号，09:15 选择 419.00–421.00 W 质保窗口，09:18 以每页 4 条运行，09:20 抽验 419.00 W 和 421.00 W 两个边界，09:25 下载 4 个命中编号及裁剪回执，09:35 另跑 A2 的空读数清单。

读数列使用整数厘瓦，每页记录最小值、最大值和有效位置位图。范围与页元数据无交集时跳过，筛选空值时直接读位图。结果按物理行次序与逐行基线比较。读取读数统计检查的非空数值，不含生成元数据或基线验证的开销。PostgreSQL 持久化批单与结果，Python 构建教学用内存列页，未改动 PostgreSQL 的执行器。

## 主备读数切换与页级续跑（reading_failover）

接单时 `build_snapshot` 钉住板号、厘瓦、谓词、逐页 Zone Map/位图和摘要，写入 `runs.reading_snapshot`；之后资料被修改也不影响已接抽检单。

- 主库连接失败时，`advance_one` 自动改用备库，并逐页记录 `page_sources`；回执顶层和逐页明细都写清「主库 / 备库 / 主库、备库」。
- 可跳过页不要求原始瓦数到达：跳页只核对接单快照中的 Zone Map/位图。备库仅缺这些页时仍签发回执，并在 `standby_missing_skippable_pages` 列明。
- 可能命中的必读页未到达时抛 `RequiredPagesMissing`，服务把单据保留为 `queued`、写 `wait_reason/missing_pages` 和检查点；缺页不生成空值行，因此不是缺测。
- 每推进一页就把 `progress/cursor` 提交到 `runs`。中断后用同一 `run_id` 重试，从 `cursor` 续跑；`receipts.run_id UNIQUE` 与完成态复查保证一单一回执。
- 验收/演练入口：`POST /runs/{id}/failover|source-control|pages/arrive|retry|resume|recover|interrupt|advance`，以及 `/receipts`、`/reading-sources/failover`、`/worker/tick`。纯领域入口为 `src/domain/reading_failover.py:execute`。
