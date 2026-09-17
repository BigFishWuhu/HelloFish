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

MXU 负责设备连接、任务配置、运行状态和日志展示。“语音厅贡献榜扫描”默认加入任务列表。首次运行任务启动 Agent 后，可在任务说明中点击“查看贡献记录”，或直接访问 [http://127.0.0.1:8765/](http://127.0.0.1:8765/)；停止任务不会关闭页面，后台服务会在退出 MXU 时停止。只有点击链接时才会打开浏览器，无需再运行额外任务。页面仅限本机访问，支持按日期、近 N 天、财富等级、性别、挚友数量、厅 ID、用户 ID 和用户名筛选，点击用户名或用户 ID 区域均会复制用户 ID，可持久化隐藏指定厅 ID 或用户 ID，并可在贡献值与金额之间切换（10 贡献值 = 1 元）。CSV 导出会沿用当前筛选条件，可按需勾选导出列。

更新时先关闭 MXU，再双击发布包根目录的 `update.bat`。更新器会从 GitHub 下载最新的 Windows x64 Release，验证并解压后覆盖程序文件；`data/` 中的数据库、扫描状态和页面设置不会被删除或覆盖。若当前已经是最新版本，更新器不会重复下载；需要强制覆盖时可运行 `python updater.py --force`。

扫描任务的“云端上传”选项可在每条记录保存到本地 SQLite 后同步到独立云端服务。填写云端服务地址、账号和密码即可启用；云端暂时不可用时不会影响本地保存。云端服务位于 [`cloud/`](./cloud/) 目录，可用 `python cloud/server.py --password "强密码"` 启动，默认监听 8787 端口，并为每个账号隔离数据。

云端服务支持 GitHub Actions 自动发布 GHCR Docker 镜像，VPS 部署可直接参考 [`cloud/docker-compose.example.yml`](./cloud/docker-compose.example.yml)。

## 语音厅贡献榜扫描

启动前将模拟器停留在厅列表页，然后在通用 UI 中运行“语音厅贡献榜扫描”。
任务会依次进入未扫描的厅，打开右上角皇冠入口，切换到“房间贡献榜”，
先遍历一次贡献榜，从无障碍结构读取排名、公开 ID 和“距前一名”，再回到榜首逐项读取用户名、性别、IP 属地和挚友数量。推测贡献值以本次扫描最后一名为 1，按相对差值从后向前累计；缺少名次或差值时，使用该位置之后已知差值的平均值补算，第 1、2、3 名统一采用第 3 名的推测值。任务设置中可指定每个厅扫描贡献榜前多少名，默认 100 名；可设置跳过的厅 ID，并用复选框选择需要记录的男、女、未知性别，默认全部勾选。识别到未勾选性别的用户会立即返回贡献榜。默认允许每次任务重复扫描同一个厅；可按需开启“跳过今天已扫描的厅”，该选项默认关闭，且绝不会跨天跳过。性别按详情页 ID 复制按钮同一行的 ♂/♀ 图标识别。同一厅、同一用户在同一天重复扫描时覆盖更新，跨天记录继续保留。

若某个厅无法打开贡献榜，可先手动进入该厅，再运行“单厅贡献榜调试”并填写厅 ID。该任务会直接从当前房间尝试打开并扫描贡献榜，可反复运行，且不会读取或写入已扫描厅状态；打开失败的厅也不会被常规扫描任务记为已扫描。

源码调试运行时，财富或魅力等级未识别的资料页截图会保存到输出目录下的 `voice_hall_level_samples/`，并在 SQLite 的 `level_samples` 表中建立索引，供后续改进识别；Release 客户端不会保存这些调试截图或样本索引。

数据文件：

- `data/voice_hall.sqlite3`：新的唯一业务数据源。
  - `contributions`：用户贡献榜记录，包含距前一名和推测贡献值；同厅、同用户、同一天由唯一键覆盖更新。
  - `wealth_level_thresholds`：0–300 级财富等级与最低贡献值的独立映射表。
  - `level_samples`：财富或魅力等级识别失败的截图索引。
  - `contribution_records_enriched`：将用户记录与财富门槛关联后的查询视图，额外提供当前等级最低贡献值及下一等级门槛；这些值不会重复写入用户记录。
- `data/contribution_viewer_settings.json`：贡献记录页面持久化的厅/用户隐藏名单。
- `data/voice_hall_scan_state.json`：已扫描厅列表；删除该文件可重新扫描。
- `data/voice_hall_agent.log`：Agent 运行日志。

旧的 JSONL/CSV 文件不会迁移到 SQLite，也不会继续读取或写入；如目录中已有旧文件，会原样保留。首次运行会创建新数据库，贡献记录为空，仅预置财富等级映射表。

可在 [my_task.json](./assets/resource/pipeline/my_task.json) 的
`VoiceHallScanStart.custom_action_param` 中调整扫描上限、跳过厅 ID、记录性别和 SQLite 输出路径。CSV 导出的金额列按金额大小显示为 `元` 或 `万元`，例如 `9999元`、`1万元`。

## 鸣谢

本项目由 **[MaaFramework](https://github.com/MaaXYZ/MaaFramework)** 强力驱动！

感谢所有为本项目提供反馈和贡献的开发者：

[![Contributors](https://contrib.rocks/image?repo=BigFishWuhu/HelloFish&max=1000)](https://github.com/BigFishWuhu/HelloFish/graphs/contributors)
