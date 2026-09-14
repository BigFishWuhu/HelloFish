<!-- markdownlint-disable MD033 MD041 -->
<p align="center">
  <img alt="LOGO" src="https://cdn.jsdelivr.net/gh/MaaAssistantArknights/design@main/v1/icons/maa-logo_512x512.png" width="256" height="256" />
</p>

<div align="center">

# HelloFish

</div>

HelloFish 是基于 [MaaFramework](https://github.com/MaaXYZ/MaaFramework) 的语音厅贡献榜采集工具，使用 [MXU](https://github.com/MistEO/MXU) 作为任务配置与运行前端。

## 使用 MXU

发布包已经包含 MXU 和 MaaFramework。Windows 运行 `mxu.exe`，Linux 或 macOS 运行 `./mxu`，连接安卓模拟器后选择“语音厅贡献榜扫描”。Agent 使用系统 Python，首次使用需在发布包目录运行 `python -m pip install -r agent/requirements.txt`。

MXU 负责设备连接、任务配置、运行状态和日志展示；SQLite 业务数据可使用 SQLite 查看工具打开，或由后续数据页面直接查询下述关联视图。

## 语音厅贡献榜扫描

启动前将模拟器停留在厅列表页，然后在通用 UI 中运行“语音厅贡献榜扫描”。
任务会依次进入未扫描的厅，打开右上角皇冠入口，切换到“房间贡献榜”，
逐项读取用户名、公开 ID、性别、IP 属地、挚友数量和排名。性别按详情页 ID 复制按钮同一行的 ♂/♀ 图标识别。同一厅、同一用户在同一天重复扫描时覆盖更新，跨天记录继续保留。

财富或魅力等级未识别时，资料页截图会保存到输出目录下的 `voice_hall_level_samples/`，并在 SQLite 的 `level_samples` 表中记录缺失字段与用户信息，供后续归纳不同徽章底色和装饰的识别规律。

数据文件：

- `data/voice_hall.sqlite3`：新的唯一业务数据源。
  - `contributions`：用户贡献榜记录；同厅、同用户、同一天由唯一键覆盖更新。
  - `wealth_level_thresholds`：0–300 级财富等级与最低贡献值的独立映射表。
  - `level_samples`：财富或魅力等级识别失败的截图索引。
  - `contribution_records_enriched`：将用户记录与财富门槛关联后的查询视图，额外提供当前等级最低贡献值及下一等级门槛；这些值不会重复写入用户记录。
- `data/voice_hall_scan_state.json`：已扫描厅列表；删除该文件可重新扫描。
- `data/voice_hall_agent.log`：Agent 运行日志。

旧的 JSONL/CSV 文件不会迁移到 SQLite，也不会继续读取或写入；如目录中已有旧文件，会原样保留。首次运行会创建新数据库，贡献记录为空，仅预置财富等级映射表。

可在 [my_task.json](./assets/resource/pipeline/my_task.json) 的
`VoiceHallScanStart.custom_action_param` 中调整扫描上限和 SQLite 输出路径。

## 鸣谢

本项目由 **[MaaFramework](https://github.com/MaaXYZ/MaaFramework)** 强力驱动！

感谢所有为本项目提供反馈和贡献的开发者：

[![Contributors](https://contrib.rocks/image?repo=BigFishWuhu/HelloFish&max=1000)](https://github.com/BigFishWuhu/HelloFish/graphs/contributors)
