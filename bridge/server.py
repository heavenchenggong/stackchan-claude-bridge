"""Claude Code Bridge MCP server.

把"调用本地 Claude Code"包装成 MCP 工具，暴露给 xiaozhi-esp32-server 的 LLM。
xiaozhi-server 的 LLM (DeepSeek/Qwen) 在用户语音里识别到知识库 / 写文章 / 复杂分析
意图时，调用本工具，本工具 spawn `claude --print` 跑你的本地 Claude Code（带
memory + skills + MCP），把结果返回给 LLM 让 TTS 朗读。

用法（xiaozhi-server data/.mcp_server_settings.json）:
  "claude_code": {
      "command": "/Users/I501579/Claude Code/claude-bridge-mcp/.venv/bin/python",
      "args": ["/Users/I501579/Claude Code/claude-bridge-mcp/server.py"]
  }

依赖：pip install mcp
"""
from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
from pathlib import Path

from mcp.server.fastmcp import FastMCP

# ── 配置 ─────────────────────────────────────────────────────────────
CLAUDE_BIN = os.environ.get("CLAUDE_BIN", "/Users/I501579/.local/bin/claude")
WORKDIR = os.environ.get(
    "CLAUDE_BRIDGE_WORKDIR",
    "/Users/I501579/Claude Code",
)
# 简单查询超时 120s（Claude Code 启动 + memory 检索能跑这么久），复杂任务（写文章）允许 5 分钟
DEFAULT_TIMEOUT_SECS = 120
LONG_TIMEOUT_SECS = 300

# 用于异步任务的输出目录（长任务回写到这里，对话结束时回报）
OUTBOX = Path("/tmp/claude-bridge-outbox")
OUTBOX.mkdir(exist_ok=True)

# ── 日志（写到 stderr 不要污染 stdio MCP）─────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("claude-bridge")

# ── MCP server ────────────────────────────────────────────────────────
mcp = FastMCP("claude-bridge")


async def _run_claude(prompt: str, timeout: int) -> tuple[bool, str]:
    """跑 claude --print，返回 (成功, 输出)。"""
    cmd = [
        CLAUDE_BIN,
        "--print",
        # 默认放开 read 与 skill 类工具，禁掉危险的写盘 / shell；用户授权过的
        # 命令仍可走（claude 自己有 settings.json 配的 allowedTools）。
        "--permission-mode",
        "acceptEdits",
        prompt,
    ]
    logger.info("claude run: %s", " ".join(cmd[:4] + ["..."]))
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=WORKDIR,
            env={**os.environ},
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=timeout
            )
        except asyncio.TimeoutError:
            try:
                proc.send_signal(signal.SIGTERM)
                await asyncio.wait_for(proc.wait(), timeout=5)
            except Exception:
                proc.kill()
            return False, f"任务超时（>{timeout}秒），请改用 background 模式"

        if proc.returncode != 0:
            err = (stderr or b"").decode(errors="replace")[-500:]
            return False, f"claude 退出码 {proc.returncode}: {err}"
        text = (stdout or b"").decode(errors="replace").strip()
        # claude print 模式有时会带 SessionEnd hook 错误前缀，过滤掉
        if "SessionEnd hook" in text:
            text = text.split("SessionEnd hook")[0].strip()
        return True, text
    except Exception as e:
        logger.exception("claude run failed")
        return False, f"调用失败: {type(e).__name__}: {e}"


@mcp.tool()
async def claude_code(task: str) -> str:
    """调用本地 Claude Code，访问个人知识库、跑技能、用 MCP 工具。

    本工具**异步执行**：立刻返回口语回执（"派蒙这就去查"），实际查询/任务在
    后台跑。结果写到 /tmp/claude-bridge-outbox/，下次用户问"派蒙查到了吗" /
    "结果出来了没" 时调 claude_code_check_results 取。

    适用场景：
    1. 查询用户的个人知识库（~/wiki/、memory、evernote、SAP wiki 等）
    2. 调用 skill：写公众号文章、做封面图、查 AI 行业新闻、整理 3D 打印耗材等
    3. 用 MCP 工具：发邮件 / 查日历 / 查 SAP 内部资料 / 操控 Playwright 浏览器
    4. 复杂分析、跨文件检索、需要思考的任务

    简单闲聊、查时间、查天气、控制设备（点头/灯）不要用本工具。

    重要：本工具调用后请告诉用户"派蒙正在查/正在做，稍等一下问我"，
    不要让用户以为已经有结果了。

    Args:
        task: 给 Claude Code 的中文指令，越具体越好。例:
            "查我的知识库里关于 HK 薪酬 schema 的内容"
            "用 wechat-write 帮我写一篇 200 字的小贴士"
            "帮我查今天 Outlook 有没有未读重要邮件"
    """
    # 异步发起，立刻返回让 wss 不超时
    async def _bg():
        ok, output = await _run_claude(task, LONG_TIMEOUT_SECS)
        out_file = OUTBOX / f"{int(asyncio.get_event_loop().time() * 1000)}.txt"
        status = "✅" if ok else "❌"
        out_file.write_text(
            f"{status} {task}\n\n{output}", encoding="utf-8"
        )
        logger.info("bg done: %s -> %s", task[:50], out_file)

    asyncio.create_task(_bg())
    return f"派蒙正在查「{task[:30]}」，稍等一下再问我结果"


@mcp.tool()
async def claude_code_quick(task: str) -> str:
    """同 claude_code 但**同步等结果**——只用于很快能完成的查询（≤ 20 秒）。

    例如查 memory 里某个具体已知文件、列出 wiki 目录、问简单事实。
    复杂任务（写文章、深度搜索）请用 claude_code。

    超时返回提示让用户重新用 claude_code 异步模式。

    Args:
        task: 简短具体的查询。
    """
    ok, output = await _run_claude(task, 20)
    if not ok:
        return f"❌ 这个查询有点慢，请用 claude_code 工具异步跑"
    if len(output) > 500:
        return output[:400] + "\n（结果较长已截短）"
    return output or "（没返回内容）"


@mcp.tool()
async def claude_code_background(task: str, summary_for_user: str) -> str:
    """跑长任务（写公众号、深度研究），后台异步执行。

    用 claude_code 也能异步，但本工具允许 LLM 自定义口语回执 summary_for_user，
    更适合长任务场景（写文章、做研究）。

    Args:
        task: 给 Claude Code 的指令。
        summary_for_user: 对用户口语化的回执，例 "派蒙这就去写文章，写完叫你"。
    """
    async def _bg():
        ok, output = await _run_claude(task, LONG_TIMEOUT_SECS)
        out_file = OUTBOX / f"{int(asyncio.get_event_loop().time() * 1000)}.txt"
        status = "✅" if ok else "❌"
        out_file.write_text(f"{status} {task}\n\n{output}", encoding="utf-8")
        logger.info("bg done: %s -> %s", task[:50], out_file)

    asyncio.create_task(_bg())
    return summary_for_user


@mcp.tool()
async def claude_code_check_results() -> str:
    """检查后台任务有没有出结果。"""
    files = sorted(OUTBOX.glob("*.txt"), key=lambda p: p.stat().st_mtime)
    if not files:
        return "暂时还没有完成的后台任务"
    # 取最近一个
    latest = files[-1]
    text = latest.read_text(encoding="utf-8", errors="replace")
    # 读完就移到 done/ 避免重复念
    done_dir = OUTBOX / "done"
    done_dir.mkdir(exist_ok=True)
    latest.rename(done_dir / latest.name)
    if len(text) > 800:
        text = text[:600] + "\n\n（结果较长，已截短）"
    return text


if __name__ == "__main__":
    logger.info("claude-bridge MCP server starting (cwd=%s)", WORKDIR)
    mcp.run()
