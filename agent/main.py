import os
import sys
from pathlib import Path


# MXU loads MaaFramework from ./maafw. Make the Python Agent load its
# MaaAgentServer from the same release directory, otherwise a system-wide
# MaaFw installation can use a different Agent protocol version.
project_root = Path(__file__).resolve().parent.parent
packaged_maafw = project_root / "maafw"
if packaged_maafw.is_dir():
    os.environ["MAAFW_BINARY_PATH"] = str(packaged_maafw)

from maa.agent.agent_server import AgentServer
from maa.toolkit import Toolkit

import contribution_viewer
import my_action
import my_reco
import voice_hall


def main():
    Toolkit.init_option("./")

    if len(sys.argv) < 2:
        print("Usage: python main.py <socket_id>")
        print("socket_id is provided by AgentIdentifier.")
        sys.exit(1)

    socket_id = sys.argv[-1]

    try:
        started = contribution_viewer.start_background_server()
        status = "后台服务已启动" if started else "后台服务已在运行"
        url = contribution_viewer.get_server_url()
        location = url or "随机端口（请查看贡献记录服务日志）"
        print(f"[ContributionViewer] {status}：{location}")
    except Exception as exc:  # noqa: BLE001
        print(f"[ContributionViewer] 启动失败：{exc!r}")

    AgentServer.start_up(socket_id)
    AgentServer.join()
    AgentServer.shut_down()


if __name__ == "__main__":
    main()
