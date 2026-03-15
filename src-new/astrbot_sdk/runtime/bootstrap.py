"""启动引导入口。

对外提供三个顶层启动函数：

- ``run_supervisor``: 启动 Supervisor 进程
- ``run_plugin_worker``: 启动单插件或组 Worker 进程
- ``run_websocket_server``: 以 WebSocket 方式启动 Worker

运行时核心类分布在同目录的子模块：

- ``runtime.supervisor``: ``SupervisorRuntime`` / ``WorkerSession``
- ``runtime.worker``: ``PluginWorkerRuntime`` / ``GroupWorkerRuntime``
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import IO

from ..protocol.wire_codecs import make_protocol_codec
from .loader import PluginEnvironmentManager
from .supervisor import (
    SupervisorRuntime,
    WorkerSession,
    _install_signal_handlers,
    _prepare_stdio_transport,
    _sdk_source_dir,
    _wait_for_shutdown,
)
from .transport import StdioTransport, WebSocketServerTransport
from .worker import GroupWorkerRuntime, PluginWorkerRuntime

__all__ = [
    "GroupWorkerRuntime",
    "PluginWorkerRuntime",
    "SupervisorRuntime",
    "WorkerSession",
    "_install_signal_handlers",
    "_prepare_stdio_transport",
    "_sdk_source_dir",
    "_wait_for_shutdown",
    "run_supervisor",
    "run_plugin_worker",
    "run_websocket_server",
]


async def run_supervisor(
        *,
        plugins_dir: Path = Path("plugins"),
        stdin: IO[str] | IO[bytes] | None = None,
        stdout: IO[str] | IO[bytes] | None = None,
        env_manager: PluginEnvironmentManager | None = None,
        worker_wire_codec: str = "json",
) -> None:
    """
    运行插件监管器（Supervisor），管理工作进程的生命周期和通信。

    监管器是AstrBot SDK的核心组件，负责：
    1. 创建和管理工作进程
    2. 处理与工作进程的通信
    3. 监控工作进程的健康状态
    4. 优雅地处理关闭信号

    Args:
        plugins_dir (Path): 插件目录路径，默认为当前目录下的"plugins"文件夹
        stdin (IO[str] | IO[bytes] | None): 自定义标准输入流，None表示使用系统标准输入
        stdout (IO[str] | IO[bytes] | None): 自定义标准输出流，None表示使用系统标准输出
        env_manager (PluginEnvironmentManager | None): 插件环境管理器，用于管理插件的运行环境
        worker_wire_codec (str): 工作进程通信的编解码器类型，默认为"json"
                               可选值：["json", "msgpack"]等

    Returns:
        None

    工作流程:
        1. 准备标准输入输出传输层
        2. 创建监管器运行时
        3. 启动监管器
        4. 安装信号处理器（处理Ctrl+C等）
        5. 等待关闭信号或工作进程退出
        6. 清理资源
    """
    # 准备标准输入输出传输层
    # 这一步会重定向标准输入输出，以便与工作进程通信
    transport_stdin, transport_stdout, original_stdout = _prepare_stdio_transport(
        stdin,
        stdout,
    )

    # 创建标准输入输出传输层
    # 这个传输层负责读写工作进程的标准输入输出
    transport = StdioTransport(stdin=transport_stdin, stdout=transport_stdout)

    # 创建监管器运行时实例
    # 运行时包含所有核心逻辑：工作进程管理、消息路由、插件加载等
    runtime = SupervisorRuntime(
        transport=transport,  # 通信传输层
        plugins_dir=plugins_dir,  # 插件目录
        env_manager=env_manager,  # 环境管理器（可选）
        worker_wire_codec_name=worker_wire_codec,  # 编解码器名称
    )

    try:
        # 启动监管器运行时
        # 这会创建工作进程并建立通信
        await runtime.start()

        # 创建停止事件，用于接收关闭信号
        stop_event = asyncio.Event()

        # 安装信号处理器（SIGINT, SIGTERM等）
        # 当收到这些信号时，会设置stop_event
        _install_signal_handlers(stop_event)

        # 等待关闭信号或工作进程意外退出
        # 这是一个辅助函数，同时监听两个事件：
        # 1. stop_event: 主动关闭信号
        # 2. runtime.peer.wait_closed(): 工作进程意外退出
        await _wait_for_shutdown(runtime.peer, stop_event)

    finally:
        # 无论成功还是异常，都要确保清理资源
        await runtime.stop()

        # 如果之前重定向了标准输出，现在恢复
        if original_stdout is not None:
            sys.stdout = original_stdout

async def run_plugin_worker(
    *,
    plugin_dir: Path | None = None,
    group_metadata: Path | None = None,
    stdin: IO[str] | IO[bytes] | None = None,
    stdout: IO[str] | IO[bytes] | None = None,
    wire_codec: str = "json",
) -> None:
    if plugin_dir is None and group_metadata is None:
        raise ValueError("plugin_dir or group_metadata is required")
    if plugin_dir is not None and group_metadata is not None:
        raise ValueError("plugin_dir and group_metadata are mutually exclusive")

    codec = make_protocol_codec(wire_codec)
    transport_stdin, transport_stdout, original_stdout = _prepare_stdio_transport(
        stdin,
        stdout,
        binary=codec.stdio_framing == "length_prefixed",
    )
    transport = StdioTransport(
        stdin=transport_stdin,
        stdout=transport_stdout,
        framing=codec.stdio_framing,
    )
    if group_metadata is not None:
        runtime = GroupWorkerRuntime(
            group_metadata_path=group_metadata,
            transport=transport,
            codec=codec,
        )
    else:
        assert plugin_dir is not None
        runtime = PluginWorkerRuntime(
            plugin_dir=plugin_dir,
            transport=transport,
            codec=codec,
        )
    try:
        await runtime.start()
        stop_event = asyncio.Event()
        _install_signal_handlers(stop_event)
        await _wait_for_shutdown(runtime.peer, stop_event)
    finally:
        await runtime.stop()
        if original_stdout is not None:
            sys.stdout = original_stdout


async def run_websocket_server(
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    path: str = "/",
    plugin_dir: Path | None = None,
    wire_codec: str = "json",
) -> None:
    codec = make_protocol_codec(wire_codec)
    runtime = PluginWorkerRuntime(
        plugin_dir=plugin_dir or Path.cwd(),
        transport=WebSocketServerTransport(
            host=host,
            port=port,
            path=path,
            frame_type=codec.websocket_frame_type,
        ),
        codec=codec,
    )
    try:
        await runtime.start()
        stop_event = asyncio.Event()
        _install_signal_handlers(stop_event)
        await _wait_for_shutdown(runtime.peer, stop_event)
    finally:
        await runtime.stop()
