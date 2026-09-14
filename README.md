<!-- markdownlint-disable MD033 MD041 -->
<p align="center">
  <img alt="LOGO" src="https://cdn.jsdelivr.net/gh/MaaAssistantArknights/design@main/v1/icons/maa-logo_512x512.png" width="256" height="256" />
</p>

<div align="center">

# HelloFish

</div>

HelloFish 是基于 [MaaFramework](https://github.com/MaaXYZ/MaaFramework) 的语音厅贡献榜采集工具，使用 [MXU](https://github.com/MistEO/MXU) 作为任务配置与运行前端。

## 运行环境要求

当前 Release 仅提供 Windows x64 版本，采集流程基于 **MuMu 模拟器 12** 的竖屏环境开发和验证。首次使用前请完成以下设置：

1. 打开 MuMu 模拟器的设置中心，将安卓设备分辨率设置为 **720 × 1280（竖屏）**。这里指模拟器内部的设备分辨率，不是 Windows 上的模拟器窗口大小；不要设置成横屏的 1280 × 720。
2. 在 MuMu 模拟器设置中开启 **Root 权限**和 **ADB 调试**。修改后完整重启模拟器，使设置生效。不同 MuMu 版本的选项名称或位置可能略有差异，通常位于设置中心的“其他”或“开发者”相关页面。
3. 在目标应用内登录账号，并在房间设置中开启 **屏蔽礼物特效**。礼物动画可能遮挡右上角的贡献榜入口，导致日志出现“未打开贡献榜，跳过”。
4. 保持目标应用位于前台和竖屏状态，不要在任务运行过程中调整分辨率、旋转屏幕或操作模拟器。
5. 在 MXU 中选择当前 MuMu 实例对应的 ADB 设备。启动扫描后，日志中的设备分辨率应显示为 `(720, 1280)`；如果数值不同，请先停止任务并修正模拟器设置。

## 使用 MXU

发布包已经包含 MXU 和 MaaFramework。运行 `mxu.exe`，连接配置好的 MuMu 模拟器后选择“语音厅贡献榜扫描”。Agent 使用系统 Python，首次使用需在发布包目录运行 `python -m pip install -r agent/requirements.txt`。

MXU 负责设备连接、任务配置、运行状态和日志展示。“语音厅贡献榜扫描”默认加入任务列表；运行“查看贡献记录”任务会启动仅限本机访问的 Web 页面并使用默认浏览器打开。页面支持按日期、近 N 天、财富等级、性别、挚友数量、厅 ID、用户 ID 和用户名筛选，可点击复制用户名和用户 ID，可持久化隐藏指定厅 ID 或用户 ID，并可在贡献值与金额之间切换（10 贡献值 = 1 元）。CSV 导出会沿用当前筛选条件，可按需勾选导出列。

## 语音厅贡献榜扫描

启动前将模拟器停留在厅列表页，然后在通用 UI 中运行“语音厅贡献榜扫描”。
任务会依次进入未扫描的厅，打开右上角皇冠入口，切换到“房间贡献榜”，
逐项读取用户名、公开 ID、性别、IP 属地、挚友数量和排名。任务设置中可指定每个厅扫描贡献榜前多少名，默认 30 名。性别按详情页 ID 复制按钮同一行的 ♂/♀ 图标识别。同一厅、同一用户在同一天重复扫描时覆盖更新，跨天记录继续保留。

源码调试运行时，财富或魅力等级未识别的资料页截图会保存到输出目录下的 `voice_hall_level_samples/`，并在 SQLite 的 `level_samples` 表中建立索引，供后续改进识别；Release 客户端不会保存这些调试截图或样本索引。

数据文件：

- `data/voice_hall.sqlite3`：新的唯一业务数据源。
  - `contributions`：用户贡献榜记录；同厅、同用户、同一天由唯一键覆盖更新。
  - `wealth_level_thresholds`：0–300 级财富等级与最低贡献值的独立映射表。
  - `level_samples`：财富或魅力等级识别失败的截图索引。
  - `contribution_records_enriched`：将用户记录与财富门槛关联后的查询视图，额外提供当前等级最低贡献值及下一等级门槛；这些值不会重复写入用户记录。
- `data/contribution_viewer_settings.json`：贡献记录页面持久化的厅/用户隐藏名单。
- `data/voice_hall_scan_state.json`：已扫描厅列表；删除该文件可重新扫描。
- `data/voice_hall_agent.log`：Agent 运行日志。

旧的 JSONL/CSV 文件不会迁移到 SQLite，也不会继续读取或写入；如目录中已有旧文件，会原样保留。首次运行会创建新数据库，贡献记录为空，仅预置财富等级映射表。

可在 [my_task.json](./assets/resource/pipeline/my_task.json) 的
`VoiceHallScanStart.custom_action_param` 中调整扫描上限和 SQLite 输出路径。

## 鸣谢

本项目由 **[MaaFramework](https://github.com/MaaXYZ/MaaFramework)** 强力驱动！

感谢所有为本项目提供反馈和贡献的开发者：

[![Contributors](https://contrib.rocks/image?repo=BigFishWuhu/HelloFish&max=1000)](https://github.com/BigFishWuhu/HelloFish/graphs/contributors)
