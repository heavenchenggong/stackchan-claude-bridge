"""WorkBuddy Bridge MCP server.

把"调用本地 WorkBuddy"包装成 MCP 工具，暴露给 xiaozhi.me 的 LLM（DeepSeek/Qwen）。
用户语音里识别到「查我的知识库」「写公众号」「查邮件」之类需要 WorkBuddy 全套能力
（persona + memory + skills + connectors）的意图时，LLM 通过 wss MCP endpoint 调本
工具，本工具 spawn `codebuddy -p` 跑本地 WorkBuddy 引擎，把结果回流给 LLM 让 TTS 朗读。

与 Claude Code 版（server.py）的差异：
  1. 引擎换成 WorkBuddy 自带的 CodeBuddy CLI（-p 非交互模式）
  2. 每次调用动态注入 ~/.workbuddy/ 的人格文件（IDENTITY/USER/MEMORY）作为
     system prompt 追加——文件随时在改，不缓存
  3. 默认挂载 WorkBuddy 的 connector-proxy MCP（邮件/飞书/SAP wiki/Outlook 等
     聚合代理），机器人借此能查邮件、查 wiki；App 没开时代理不通，CLI 会
     警告但继续跑（置 WORKBUDDY_MCP_URL= 空串可关闭）

工具清单（FastMCP 自动注册）:
  workbuddy(task)                    — 默认异步，立刻回执；后台结果写 outbox/{ts}.txt
  workbuddy_quick(task)              — 同步，仅限 ≤20 秒查询
  workbuddy_background(task, summary_for_user) — 长任务异步，自定义口语回执
  workbuddy_check_results()          — 取最新 outbox，移到 done/

环境变量:
  WORKBUDDY_BIN           — codebuddy CLI 路径（默认 App bundle 内）
  WORKBUDDY_BRIDGE_WORKDIR — 引擎工作目录（默认 ~/Claude Code）
  WORKBUDDY_HOME          — 人格文件目录（默认 ~/.workbuddy）
  WORKBUDDY_MCP_URL       — connector-proxy MCP 地址（默认 127.0.0.1:61400，空串关闭）

参考：https://github.com/heavenchenggong/stackchan-claude-bridge
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import sys
from pathlib import Path

from mcp.server.fastmcp import FastMCP

# ── 配置 ─────────────────────────────────────────────────────────────
WORKBUDDY_BIN = os.environ.get(
    "WORKBUDDY_BIN",
    "/Applications/WorkBuddy.app/Contents/Resources/app.asar.unpacked/cli/bin/codebuddy",
)
WORKDIR = os.environ.get(
    "WORKBUDDY_BRIDGE_WORKDIR",
    "/Users/I501579/Claude Code",
)
WORKBUDDY_HOME = Path(os.environ.get("WORKBUDDY_HOME", str(Path.home() / ".workbuddy")))
# 注入的人格文件：IDENTITY（我是谁）/ USER（用户是谁）/ MEMORY（共享记忆索引）
PERSONA_FILES = ["IDENTITY.md", "USER.md", "MEMORY.md"]
WORKBUDDY_MCP_URL = os.environ.get("WORKBUDDY_MCP_URL", "http://127.0.0.1:61400/mcp")
# 模型路由是生死线：默认 auto 会落到移动云 cmcc 体验池（算力豆=0 必失败），
# hy4-preview 是 WorkBuddy App 自己用的路由（实测可用）。App 升级换了模型名就改这里。
WORKBUDDY_MODEL = os.environ.get("WORKBUDDY_MODEL", "hy4-preview")
# -p 模式下无人批权限：写文章/跑 skill 需要 Bash+Write 全开，默认全放行（个人机器）
WORKBUDDY_PERMISSION = os.environ.get("WORKBUDDY_PERMISSION", "bypassPermissions")
# 显式授权引擎访问的目录（wiki 在 WORKDIR 之外，不挂的话 Read 不到）
WORKBUDDY_EXTRA_DIRS = os.environ.get("WORKBUDDY_EXTRA_DIRS", "/Users/I501579/wiki")

# 简单查询超时 120s（引擎启动 + memory 检索能跑这么久），复杂任务（写文章）允许 5 分钟
DEFAULT_TIMEOUT_SECS = 120
LONG_TIMEOUT_SECS = 300

# 用于异步任务的输出目录（长任务回写到这里，对话结束时回报）
OUTBOX = Path("/tmp/workbuddy-bridge-outbox")
OUTBOX.mkdir(exist_ok=True)

# ── 日志（写到 stderr 不要污染 stdio MCP）─────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("workbuddy-bridge")

# ── MCP server ────────────────────────────────────────────────────────
mcp = FastMCP("workbuddy-bridge")


def _node_bin_dir() -> str | None:
    """launchd 环境 PATH 里没有 node（CLI 是 #!/usr/bin/env node），需显式定位。
    优先 WorkBuddy 自带 node（App 升级换版本目录也能跟上），其次 homebrew。"""
    versions_dir = Path.home() / ".workbuddy/binaries/node/versions"
    bundled = sorted(versions_dir.glob("*/bin")) if versions_dir.exists() else []
    for d in [*bundled, Path("/opt/homebrew/bin"), Path("/usr/local/bin")]:
        if (d / "node").is_file():
            return str(d)
    return None


def _persona_prompt() -> str:
    """拼装 ~/.workbuddy 的人格文件作为 system prompt 追加。文件缺失就跳过。"""
    parts = []
    for name in PERSONA_FILES:
        path = WORKBUDDY_HOME / name
        try:
            text = path.read_text(encoding="utf-8", errors="replace").strip()
            if text:
                parts.append(f"=== {name} ===\n{text}")
        except OSError as e:
            logger.warning("persona file missing: %s (%s)", path, e)
    if not parts:
        return ""
    header = (
        "你是用户桌面机器人派蒙背后的 WorkBuddy 引擎。以下是你的身份设定和与"
        "用户的共享记忆，回答时保持这个人格并尽量利用记忆里的信息：\n\n"
    )
    return header + "\n\n".join(parts)


def _build_cmd(task: str, persona: str) -> list[str]:
    cmd = [
        WORKBUDDY_BIN,
        "-p",
        "--permission-mode",
        WORKBUDDY_PERMISSION,
    ]
    if WORKBUDDY_MODEL:
        cmd += ["--model", WORKBUDDY_MODEL]
    extra_dirs = [d for d in WORKBUDDY_EXTRA_DIRS.split(":") if d.strip()]
    if extra_dirs:
        cmd += ["--add-dir", *extra_dirs]
    if persona:
        cmd += ["--append-system-prompt", persona]
    if WORKBUDDY_MCP_URL:
        mcp_cfg = json.dumps(
            {
                "mcpServers": {
                    "connector-proxy": {
                        "type": "http",
                        "url": WORKBUDDY_MCP_URL,
                    }
                }
            }
        )
        cmd += ["--mcp-config", mcp_cfg]
    cmd.append(task)
    return cmd


async def _run_workbuddy(task: str, timeout: int) -> tuple[bool, str]:
    """跑 codebuddy -p，返回 (成功, 输出)。"""
    persona = _persona_prompt()
    cmd = _build_cmd(task, persona)
    env = {**os.environ}
    node_dir = _node_bin_dir()
    if node_dir:
        env["PATH"] = node_dir + os.pathsep + env.get("PATH", "")
    else:
        logger.warning("node not found in fallback paths; codebuddy CLI will likely fail (exit 127)")
    logger.info("workbuddy run: %s (persona %d chars)", task[:50], len(persona))
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.DEVNULL,  # 绝不继承 MCP server 的 stdin（会被引擎偷读/搞挂 stdio）
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=WORKDIR,
            env=env,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            try:
                proc.send_signal(signal.SIGTERM)
                await asyncio.wait_for(proc.wait(), timeout=5)
            except Exception:
                proc.kill()
            return False, f"任务超时（>{timeout}秒），请改用 background 模式"

        if proc.returncode != 0:
            err = (stderr or b"").decode(errors="replace")[-500:]
            return False, f"workbuddy 退出码 {proc.returncode}: {err}"
        text = (stdout or b"").decode(errors="replace").strip()
        # print 模式有时会带 hook 报错前缀，过滤掉
        if "SessionEnd hook" in text:
            text = text.split("SessionEnd hook")[0].strip()
        if not text:
            # codebuddy 后端报错（如算力豆用完）时 exit 0 + stdout 空 + 真相在 stderr，
            # 必须当失败处理，否则机器人会念"✅"加空白
            err = (stderr or b"").decode(errors="replace").strip()
            return False, f"workbuddy 引擎没返回内容（退出码 {proc.returncode}）" + (
                f"，引擎消息: {err[-300:]}" if err else ""
            )
        return True, text
    except Exception as e:
        logger.exception("workbuddy run failed")
        return False, f"调用失败: {type(e).__name__}: {e}"


def _write_outbox(task: str, ok: bool, output: str) -> Path:
    out_file = OUTBOX / f"{int(asyncio.get_event_loop().time() * 1000)}.txt"
    status = "✅" if ok else "❌"
    out_file.write_text(f"{status} {task}\n\n{output}", encoding="utf-8")
    logger.info("bg done: %s -> %s", task[:50], out_file)
    return out_file


@mcp.tool()
async def workbuddy(task: str) -> str:
    """调用本地 WorkBuddy，访问个人记忆、知识库、邮件日历、跑技能、用 MCP 连接器。

    本工具**异步执行**：立刻返回口语回执（"派蒙这就去查"），实际查询/任务在
    后台跑。结果写到 outbox，下次用户问"派蒙查到了吗"/"结果出来了没"时调
    workbuddy_check_results 取。

    适用场景：
    1. 查询用户的个人记忆和知识库（MEMORY、~/wiki/、evernote、SAP wiki 等）
    2. 查 Outlook 邮件、日历、飞书文档（走 WorkBuddy 的连接器）
    3. 调用 skill：写公众号文章、做封面图、查 AI 行业新闻、整理 3D 打印耗材等
    4. 复杂分析、跨文件检索、需要思考的任务

    简单闲聊、查时间、查天气、控制设备（点头/灯）不要用本工具。

    重要：本工具调用后请告诉用户"派蒙正在查/正在做，稍等一下问我"，
    不要让用户以为已经有结果了。

    Args:
        task: 给 WorkBuddy 的中文指令，越具体越好。例:
            "查我的记忆里 HK 薪酬 schema 的要点"
            "帮我看看今天 Outlook 有没有未读重要邮件"
            "用 wechat-write 帮我写一篇 200 字的小贴士"
    """
    # 异步发起，立刻返回让 wss 不超时
    asyncio.create_task(_bg_workbuddy(task, None))
    return f"派蒙正在查「{task[:30]}」，稍等一下再问我结果"


async def _bg_workbuddy(task: str, _summary: str | None) -> None:
    ok, output = await _run_workbuddy(task, LONG_TIMEOUT_SECS)
    _write_outbox(task, ok, output)


@mcp.tool()
async def workbuddy_quick(task: str) -> str:
    """同 workbuddy 但**同步等结果**——只用于很快能完成的查询（≤ 20 秒）。

    例如查记忆里某个具体已知文件、列出 wiki 目录、问简单事实。
    复杂任务（写文章、深度搜索、查邮件）请用 workbuddy。

    超时返回提示让用户重新用 workbuddy 异步模式。

    Args:
        task: 简短具体的查询。
    """
    ok, output = await _run_workbuddy(task, 20)
    if not ok:
        return f"❌ 这个查询有点慢，请用 workbuddy 工具异步跑"
    if len(output) > 500:
        return output[:400] + "\n（结果较长已截短）"
    return output or "（没返回内容）"


@mcp.tool()
async def workbuddy_background(task: str, summary_for_user: str) -> str:
    """跑长任务（写公众号、深度研究），后台异步执行。

    用 workbuddy 也能异步，但本工具允许 LLM 自定义口语回执 summary_for_user，
    更适合长任务场景（写文章、做研究）。

    Args:
        task: 给 WorkBuddy 的指令。
        summary_for_user: 对用户口语化的回执，例 "派蒙这就去写文章，写完叫你"。
    """
    asyncio.create_task(_bg_workbuddy(task, summary_for_user))
    return summary_for_user


@mcp.tool()
async def workbuddy_check_results() -> str:
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
    logger.info("workbuddy-bridge MCP server starting (cwd=%s)", WORKDIR)
    mcp.run()
