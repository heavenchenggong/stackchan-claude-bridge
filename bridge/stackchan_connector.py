"""Stack-chan MCP connector for WorkBuddy 自定义连接器.

把桌面机器人派蒙（Stack-chan）的本地控制接口包装成 MCP 连接器，让 WorkBuddy
通过「连接器 → 自定义连接器」挂载后，直接在聊天里指挥派蒙：
  - notify_attention: 摇头 3 次 + 屏幕钉"等你确认：<project>"，直到 dismiss
  - notify_dismiss:   点头 + 清除屏幕提醒
  - robot_status:     机器人在线/健康检查

计费：官方文档明确"连接器自身的读写不消耗积分"，只有 WorkBuddy 理解外部
数据时才计费——完全绕开 codebuddy CLI 的 agent 算力豆体系。

在 WorkBuddy「连接器 → 自定义连接器」里用如下配置安装：
{
  "mcpServers": {
    "stackchan": {
      "type": "stdio",
      "command": "/Users/I501579/Claude Code/claude-bridge-mcp/.venv/bin/python",
      "args": ["/Users/I501579/Claude Code/stackchan-projects/bridge/bridge/stackchan_connector.py"],
      "env": {"STACKCHAN_URL": "http://192.168.31.22:8788"},
      "description": "桌面机器人派蒙的身体：摇头提醒/清除提醒/状态"
    }
  }
}

前置条件：Mac 与机器人在同一网段（机器人在 Heaven_Xiaomi 192.168.31.x）。

环境变量:
  STACKCHAN_URL             — 机器人 notify 服务地址（默认 http://192.168.31.22:8788）
  STACKCHAN_CONNECTOR_HTTP  — 置 1 时改以 streamable-http 模式跑在 127.0.0.1:8790
                              （给只支持 HTTP 接入的宿主用；默认 stdio）
"""
from __future__ import annotations

import json
import logging
import os
import sys
import urllib.request

from mcp.server.fastmcp import FastMCP

STACKCHAN_URL = os.environ.get("STACKCHAN_URL", "http://192.168.31.22:8788")
TIMEOUT_SECS = 10

logging.basicConfig(level=logging.INFO, stream=sys.stderr)
logger = logging.getLogger("stackchan-connector")

mcp = FastMCP("stackchan")


def _post(path: str, payload: dict | None = None) -> tuple[bool, str]:
    url = f"{STACKCHAN_URL}{path}"
    data = json.dumps(payload).encode() if payload is not None else b"{}"
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_SECS) as resp:
            return True, resp.read().decode(errors="replace")
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


@mcp.tool()
def notify_attention(project: str) -> str:
    """让桌面机器人派蒙吸引用户注意：摇头 3 次，屏幕钉住"等你确认：<project>"，直到 notify_dismiss。

    使用场景：需要用户回到电脑前做确认/审批/授权时（比如任务等用户拍板、权限等待批准），
    用户不盯屏幕也能看到是哪件事在等。闲聊、通知类消息不要用。

    Args:
        project: 等待确认的事项名，会显示在机器人屏幕上，例 "Q3 报告审批"
    """
    ok, body = _post("/notify", {"project": project})
    if ok:
        return f"派蒙正在摇头提醒你确认「{project}」，屏幕上已钉住，处理完叫我清除。"
    return f"❌ 派蒙叫不应（{body}）。检查：机器人开着我没、Mac 和它是不是同一个 Wi-Fi（它 在 Heaven_Xiaomi）。"


@mcp.tool()
def notify_dismiss() -> str:
    """让派蒙点头并清除屏幕上的"等你确认"提醒。在用户已回应/事项已处理完后调用。"""
    ok, body = _post("/dismiss")
    if ok:
        return "派蒙点头，屏幕提醒已清掉。"
    return f"❌ 派蒙叫不应（{body}）。"


@mcp.tool()
def robot_status() -> str:
    """检查桌面机器人派蒙在不在线（连通性/健康检查）。"""
    ok, body = _post("/healthz")
    if ok:
        return f"派蒙在线 ✅ {body}"
    return f"派蒙不在线 ❌（{body}）。它在 Heaven_Xiaomi 网段的 192.168.31.22，确认 Mac 连的同一个 Wi-Fi。"


if __name__ == "__main__":
    if os.environ.get("STACKCHAN_CONNECTOR_HTTP") == "1":
        logger.info("stackchan connector starting in streamable-http mode on 127.0.0.1:8790")
        mcp.run(transport="streamable-http")
    else:
        logger.info("stackchan connector starting in stdio mode")
        mcp.run()
