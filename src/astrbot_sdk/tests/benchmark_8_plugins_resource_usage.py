# 导入未来版本的类型注解特性，允许在类定义中使用自身作为类型提示
from __future__ import annotations

# 命令行参数解析库
import argparse
# 异步IO库，用于编写并发代码
import asyncio
# JSON处理库
import json
# 子进程管理库，用于启动和控制外部进程
import subprocess
# 系统相关功能库，如获取Python解释器路径
import sys
# 临时文件目录管理库
import tempfile
# 时间相关功能库，用于性能测量
import time
# 路径操作库，提供面向对象的文件系统路径接口
from pathlib import Path
# 类型提示支持，Any表示任意类型
from typing import Any

# YAML格式处理库，用于解析和生成配置文件
import yaml

try:
    # psutil是跨平台进程和系统监控库，尝试导入
    import psutil
except ImportError:  # pragma: no cover - optional dependency
    # 如果导入失败（可选依赖），设置为None，后续会降级使用ps命令
    psutil = None

# 从AstrBot SDK导入星体上下文类，用于插件运行时环境
from astrbot_sdk.api.star.context import Context
# 从AstrBot SDK导入星系类，管理星体（插件实例）的连接和生命周期
from astrbot_sdk.runtime.galaxy import Galaxy

# 要生成的插件数量，这里是16个
PLUGIN_COUNT = 16
# 目标Python版本，用于插件清单声明
TARGET_PYTHON = "3.12"
# 握手超时时间，单位为秒，设置为3600秒（1小时）
HANDSHAKE_TIMEOUT_SECONDS = 3600.0


class BenchmarkContext(Context):
    """
    基准测试上下文类，继承自Context，目前为空实现，
    用于为基准测试提供特定的上下文环境
    """
    pass


def parse_args() -> argparse.Namespace:
    """
    解析命令行参数

    返回:
        包含解析后参数的命令行命名空间
    """
    parser = argparse.ArgumentParser(
        description=(
            "生成16个Python 3.12插件并测量独立工作进程运行时的资源使用情况。"
        )
    )
    # 指定用于启动监控进程的Python可执行文件路径，默认为当前解释器
    parser.add_argument(
        "--python-executable",
        default=sys.executable,
        help="用于启动监控进程的Python可执行文件。",
    )
    # 可选：指定生成插件的输出目录
    parser.add_argument(
        "--plugins-dir",
        type=Path,
        default=None,
        help="可选：生成插件的目标目录。",
    )
    # 可选：是否保留生成的插件目录（默认会在结束后删除）
    parser.add_argument(
        "--keep-plugins-dir",
        action="store_true",
        help="保留生成的插件目录而不是删除它。",
    )
    # 可选：输出基准测试报告的JSON文件路径
    parser.add_argument(
        "--output-json",
        type=Path,
        default=None,
        help="可选：写入基准测试报告的JSON文件路径。",
    )
    return parser.parse_args()


def write_plugin(plugins_dir: Path, index: int) -> None:
    """
    在指定目录生成一个基准测试插件

    参数:
        plugins_dir: 插件根目录
        index: 插件索引（0-15），用于生成唯一的插件名和命令名
    """
    # 生成插件名和命令名，格式如 plugin_000, bench_000
    plugin_name = f"plugin_{index:03d}"
    command_name = f"bench_{index:03d}"
    plugin_dir = plugins_dir / plugin_name  # 插件主目录
    commands_dir = plugin_dir / "commands"  # 命令模块目录
    commands_dir.mkdir(parents=True, exist_ok=True)  # 创建目录，如果存在则忽略
    # 创建空的 __init__.py 文件，使 commands 成为Python包
    (commands_dir / "__init__.py").write_text("", encoding="utf-8")

    # 插件清单文件内容（plugin.yaml）
    manifest = {
        "_schema_version": 2,  # 清单文件架构版本
        "name": plugin_name,  # 插件名称
        "display_name": plugin_name,  # 插件显示名称
        "desc": f"资源基准测试插件 {index}",  # 插件描述
        "author": "codex",  # 插件作者
        "version": "0.1.0",  # 插件版本
        "runtime": {"python": TARGET_PYTHON},  # 运行时要求，Python版本
        "components": [  # 插件组件列表
            {
                "class": f"commands.plugin_{index:03d}:BenchmarkCommand{index:03d}",  # 组件类路径
                "type": "command",  # 组件类型：命令
                "name": command_name,  # 命令名称
                "description": command_name,  # 命令描述
            }
        ],
    }
    # 将清单写入 plugin.yaml 文件
    (plugin_dir / "plugin.yaml").write_text(
        yaml.safe_dump(manifest, sort_keys=False),
        encoding="utf-8",
    )
    # 创建空的 requirements.txt（无依赖）
    (plugin_dir / "requirements.txt").write_text("", encoding="utf-8")

    # 插件命令模块的源代码
    module_source = f"""
from astrbot_sdk.api.components.command import CommandComponent
from astrbot_sdk.api.event import AstrMessageEvent, filter
from astrbot_sdk.api.star.context import Context


class BenchmarkCommand{index:03d}(CommandComponent):
    def __init__(self, context: Context):
        self.context = context

    @filter.command("{command_name}")
    async def handle(self, event: AstrMessageEvent):
        yield event.plain_result("{{plugin_name}}:{{command_name}}")
""".strip()  # 注意：字符串格式化时需要使用双重花括号转义
    # 将源代码写入命令模块文件
    (commands_dir / f"plugin_{index:03d}.py").write_text(
        module_source + "\n",
        encoding="utf-8",
    )


def _collect_with_psutil(root_pid: int) -> dict[str, Any]:
    """
    使用psutil库收集进程树资源使用情况

    参数:
        root_pid: 根进程ID

    返回:
        包含进程树资源信息的字典
    """
    assert psutil is not None  # 确保psutil已导入
    root_process = psutil.Process(root_pid)  # 获取根进程对象
    processes = [root_process] + root_process.children(recursive=True)  # 获取所有子进程（递归）
    entries: list[dict[str, Any]] = []  # 存储每个进程的信息
    total_rss = 0  # 总RSS内存使用量（字节）

    for process in processes:
        try:
            rss = process.memory_info().rss  # 获取进程RSS内存
            total_rss += rss
            entries.append(
                {
                    "pid": process.pid,  # 进程ID
                    "name": process.name(),  # 进程名
                    "rss_mb": round(rss / 1024 / 1024, 2),  # RSS内存，单位MB
                    "cmdline": process.cmdline(),  # 命令行参数列表
                }
            )
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue  # 进程已不存在或无权限访问，跳过

    entries.sort(key=lambda item: item["pid"])  # 按进程ID排序
    return {
        "collector": "psutil",  # 使用的收集器
        "process_count": len(entries),  # 进程数量
        "total_rss_mb": round(total_rss / 1024 / 1024, 2),  # 总RSS内存，单位MB
        "processes": entries,  # 各进程详细信息
    }


def _collect_with_ps(root_pid: int) -> dict[str, Any]:
    """
    使用系统ps命令收集进程树资源使用情况（psutil不可用时降级使用）

    参数:
        root_pid: 根进程ID

    返回:
        包含进程树资源信息的字典
    """
    # 执行ps命令，获取所有进程的PID、父PID、RSS内存（KB）和命令名
    process = subprocess.run(
        ["ps", "-axo", "pid,ppid,rss,comm"],
        capture_output=True,
        text=True,
        check=True,
    )
    # 构建子进程映射：父PID -> [(子PID, RSS, 命令名), ...]
    children_by_parent: dict[int, list[tuple[int, int, str]]] = {}
    # RSS内存映射：PID -> RSS (KB)
    rss_by_pid: dict[int, int] = {}

    for line in process.stdout.splitlines()[1:]:  # 跳过标题行
        parts = line.strip().split(None, 3)  # 分割最多4部分
        if len(parts) != 4:
            continue
        pid, ppid, rss_kb, command = parts
        pid_int = int(pid)
        ppid_int = int(ppid)
        rss_int = int(rss_kb)
        rss_by_pid[pid_int] = rss_int
        children_by_parent.setdefault(ppid_int, []).append((pid_int, rss_int, command))

    # BFS遍历进程树，从根进程开始
    queue = [root_pid]
    seen: set[int] = set()  # 已访问的进程ID集合
    entries: list[dict[str, Any]] = []  # 存储每个进程的信息
    total_rss = 0  # 总RSS内存使用量（字节）

    while queue:
        pid = queue.pop(0)
        if pid in seen:
            continue
        seen.add(pid)
        rss_kb = rss_by_pid.get(pid)  # 获取进程RSS（KB）
        command = None
        # 在子进程映射中查找该进程的详细信息
        for siblings in children_by_parent.values():
            for child_pid, child_rss, child_command in siblings:
                if child_pid == pid:
                    rss_kb = child_rss
                    command = child_command
                    break
            if command is not None:
                break
        if rss_kb is not None:  # 如果找到了RSS信息
            total_rss += rss_kb * 1024  # 转换为字节并累加
            entries.append(
                {
                    "pid": pid,
                    "name": command or "unknown",
                    "rss_mb": round((rss_kb * 1024) / 1024 / 1024, 2),  # 转换为MB
                    "cmdline": [command] if command else [],  # 简化命令行
                }
            )
        # 将该进程的所有子进程加入队列继续遍历
        for child_pid, _child_rss, _child_command in children_by_parent.get(pid, []):
            queue.append(child_pid)

    entries.sort(key=lambda item: item["pid"])  # 按进程ID排序
    return {
        "collector": "ps",  # 使用的收集器
        "process_count": len(entries),  # 进程数量
        "total_rss_mb": round(total_rss / 1024 / 1024, 2),  # 总RSS内存，单位MB
        "processes": entries,  # 各进程详细信息
    }


def collect_process_tree_metrics(root_pid: int) -> dict[str, Any]:
    """
    收集进程树的资源使用指标，优先使用psutil，失败则降级使用ps命令

    参数:
        root_pid: 根进程ID

    返回:
        包含进程树资源信息的字典
    """
    if psutil is not None:
        try:
            return _collect_with_psutil(root_pid)
        except (PermissionError, psutil.Error):
            pass  # 发生错误时降级使用ps命令
    return _collect_with_ps(root_pid)


async def terminate_process(process: Any) -> None:
    """
    终止进程，先尝试优雅终止（terminate），超时后强制终止（kill）

    参数:
        process: 可以是asyncio子进程对象或普通的subprocess.Popen对象
    """
    if process is None:
        return

    # 处理asyncio子进程
    if isinstance(process, asyncio.subprocess.Process):
        if process.returncode is not None:  # 进程已结束
            return
        process.terminate()  # 发送SIGTERM
        try:
            await asyncio.wait_for(process.wait(), timeout=10.0)  # 等待最多10秒
        except Exception:
            process.kill()  # 超时则强制终止（SIGKILL）
            await asyncio.wait_for(process.wait(), timeout=10.0)
        return

    # 处理普通子进程（Popen对象）
    if hasattr(process, "poll") and process.poll() is not None:  # 进程已结束
        return

    process.terminate()  # 发送SIGTERM
    try:
        await asyncio.to_thread(process.wait, 10.0)  # 在线程中等待最多10秒
    except Exception:
        process.kill()  # 超时则强制终止
        await asyncio.to_thread(process.wait, 10.0)


async def run_benchmark(plugins_dir: Path, python_executable: str) -> dict[str, Any]:
    """
    运行基准测试主流程：
    1. 生成所有插件
    2. 启动星系并连接星体
    3. 等待握手完成
    4. 收集资源指标
    5. 停止星体并返回报告

    参数:
        plugins_dir: 插件目录路径
        python_executable: Python可执行文件路径

    返回:
        包含所有基准测试数据的字典
    """
    # 生成所有插件（16个）
    for index in range(PLUGIN_COUNT):
        write_plugin(plugins_dir, index)

    # 创建星系和上下文
    galaxy = Galaxy()
    context = BenchmarkContext()
    started_at = time.perf_counter()  # 记录开始时间（高精度）
    # 连接到标准输入输出星体（启动子进程）
    star = await galaxy.connect_to_stdio_star(
        context=context,
        star_name="resource-benchmark",
        config={
            "plugins_dir": str(plugins_dir),
            "python_executable": python_executable,
        },
    )
    connected_at = time.perf_counter()  # 记录连接完成时间

    # 获取子进程对象（内部可能存储在私有属性_process中）
    client_process = getattr(star._client, "_process", None)
    metadata: dict[str, Any] = {}
    handshake_error: str | None = None
    try:
        # 等待握手完成（获取插件元数据），带超时控制
        metadata = await asyncio.wait_for(
            star.handshake(),
            timeout=HANDSHAKE_TIMEOUT_SECONDS,
        )
    except Exception as exc:
        handshake_error = f"{exc.__class__.__name__}: {exc}"

    measured_at = time.perf_counter()  # 记录测量时间点
    # 收集进程树资源指标（如果有子进程）
    metrics = (
        collect_process_tree_metrics(client_process.pid) if client_process else {}
    )
    # 提取已加载的插件名称列表，并按字母顺序排序
    loaded_plugins = sorted(
        metadata_item.name
        for metadata_item in metadata.values()
        if getattr(metadata_item, "name", None)
    )

    stop_error: str | None = None
    try:
        # 优雅停止星体
        await star.stop()
    except Exception as exc:
        stop_error = f"{exc.__class__.__name__}: {exc}"
        # 如果停止失败，强制终止子进程
        await terminate_process(client_process)

    # 返回完整的基准测试报告
    return {
        "plugin_count": PLUGIN_COUNT,  # 插件总数
        "target_python": TARGET_PYTHON,  # 目标Python版本
        "python_executable": python_executable,  # 实际使用的Python解释器
        "loaded_plugin_count": len(loaded_plugins),  # 实际加载的插件数量
        "loaded_plugins": loaded_plugins,  # 已加载插件名称列表
        "connect_duration_ms": round((connected_at - started_at) * 1000, 2),  # 连接耗时（毫秒）
        "handshake_duration_ms": round((measured_at - connected_at) * 1000, 2),  # 握手耗时（毫秒）
        "startup_total_duration_ms": round((measured_at - started_at) * 1000, 2),  # 总启动耗时（毫秒）
        "handshake_error": handshake_error,  # 握手错误（如果有）
        "metrics": metrics,  # 资源指标
        "stop_error": stop_error,  # 停止错误（如果有）
    }


def main() -> None:
    """
    主函数：解析参数，管理临时目录，运行基准测试，输出报告
    """
    args = parse_args()  # 解析命令行参数

    temp_dir: tempfile.TemporaryDirectory[str] | None = None
    plugins_dir = args.plugins_dir
    if plugins_dir is None:
        # 如果没有指定插件目录，在当前工作目录的tmp子目录下创建临时目录
        tmp_root = Path.cwd() / "tmp"
        tmp_root.mkdir(parents=True, exist_ok=True)
        temp_dir = tempfile.TemporaryDirectory(
            prefix="astrbot-8-plugin-bench-",  # 临时目录前缀
            dir=str(tmp_root),  # 在tmp_root下创建
        )
        plugins_dir = Path(temp_dir.name)
    else:
        # 如果指定了插件目录，确保它存在
        plugins_dir.mkdir(parents=True, exist_ok=True)

    try:
        # 运行基准测试（异步转同步）
        report = asyncio.run(
            run_benchmark(
                plugins_dir=plugins_dir,
                python_executable=args.python_executable,
            )
        )
    finally:
        # 如果使用了临时目录且未指定保留，则清理
        if temp_dir is not None and not args.keep_plugins_dir:
            temp_dir.cleanup()

    report["plugins_dir"] = str(plugins_dir)  # 在报告中添加插件目录路径

    # 如果指定了输出JSON文件，写入文件
    if args.output_json is not None:
        args.output_json.write_text(
            json.dumps(report, ensure_ascii=True, indent=2),
            encoding="utf-8",
        )

    # 无论是否写入文件，都打印报告到标准输出
    print(json.dumps(report, ensure_ascii=True, indent=2))


if __name__ == "__main__":
    main()  # 程序入口点