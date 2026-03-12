from __future__ import annotations

import asyncio
import locale
import json
import os
import subprocess
from concurrent.futures import ThreadPoolExecutor
from typing import IO, Any

from loguru import logger

from ..jsonrpc import (
    JSONRPCErrorResponse,
    JSONRPCMessage,
    JSONRPCRequest,
    JSONRPCSuccessResponse,
)
from .base import JSONRPCClient


class StdioClient(JSONRPCClient):
    """JSON-RPC client using standard input/output for communication."""

    _fallback_executor: ThreadPoolExecutor | None = None

    def __init__(
        self,
        command: list[str],
        cwd: str | None = None,
        env: dict[str, str] | None = None,
    ) -> None:
        """Initialize the STDIO client.

        Args:
            command: Command to start subprocess (e.g., ['python', 'plugin.py'])
            cwd: Working directory for subprocess
        """
        super().__init__()
        self._command = command
        self._cwd = cwd
        self._env = env or os.environ.copy()
        self._process: asyncio.subprocess.Process | subprocess.Popen[str] | None = None
        self._stdin: IO[str] | None = None
        self._stdout: IO[str] | None = None
        self._stdin_writer: asyncio.StreamWriter | None = None
        self._stdout_reader: asyncio.StreamReader | None = None
        self._stderr_reader: asyncio.StreamReader | None = None
        self._read_task: asyncio.Task | None = None
        self._stderr_task: asyncio.Task | None = None
        self._write_lock = asyncio.Lock()
        self._fallback_encoding = locale.getpreferredencoding(False)

    @classmethod
    def _get_fallback_executor(cls) -> ThreadPoolExecutor:
        if cls._fallback_executor is None:
            env_override = os.environ.get("ASTRBOT_STDIO_EXECUTOR_MAX_WORKERS")
            if env_override:
                try:
                    max_workers = max(4, int(env_override))
                except ValueError:
                    logger.warning(
                        "Invalid ASTRBOT_STDIO_EXECUTOR_MAX_WORKERS value. "
                        f"Expected int, got: {env_override!r}"
                    )
                    max_workers = 0
            else:
                max_workers = 0

            if max_workers <= 0:
                cpu_count = os.cpu_count() or 1
                # Each client uses long-lived blocking reads (stdout + stderr) when
                # subprocess APIs are unavailable (e.g. selector loop on Windows).
                # Use a larger pool than asyncio's default to avoid deadlocks.
                max_workers = max(32, cpu_count * 8)

            cls._fallback_executor = ThreadPoolExecutor(
                max_workers=max_workers,
                thread_name_prefix="astrbot-stdio",
            )
        return cls._fallback_executor

    async def start(self) -> None:
        """Start the client and launch subprocess."""
        if self._running:
            logger.warning("StdioClient is already running")
            return

        self._running = True

        # Start subprocess
        await self._start_subprocess()

        self._read_task = asyncio.create_task(self._read_loop())
        logger.info("StdioClient started")

    async def _start_subprocess(self) -> None:
        """Start the subprocess and connect to its stdio."""
        logger.info(f"Starting subprocess: {' '.join(self._command)}")

        try:
            self._process = await asyncio.create_subprocess_exec(
                *self._command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=self._cwd,
                env=self._env,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.debug(
                "asyncio subprocess transport failed "
                f"({exc.__class__.__name__}: {exc}); "
                "falling back to thread-based stdio transport."
            )
        else:
            assert self._process.stdout is not None
            assert self._process.stderr is not None
            assert self._process.stdin is not None

            self._stdout_reader = self._process.stdout
            self._stderr_reader = self._process.stderr
            self._stdin_writer = self._process.stdin
            logger.info(f"Subprocess started with PID {self._process.pid}")
            self._stderr_task = asyncio.create_task(self._monitor_stderr_asyncio())
            return

        self._process = subprocess.Popen(
            self._command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=self._cwd,
            env=self._env,
            text=True,
            bufsize=1,  # Line buffered
        )

        # Use subprocess's stdio
        self._stdin = self._process.stdout  # Read from subprocess stdout
        self._stdout = self._process.stdin  # Write to subprocess stdin

        logger.info(f"Subprocess started with PID {self._process.pid}")

        # Start monitoring stderr
        self._stderr_task = asyncio.create_task(self._monitor_stderr_threaded())

    def _decode_stdout_line(self, data: bytes) -> str:
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError:
            try:
                return data.decode(self._fallback_encoding)
            except UnicodeDecodeError:
                return data.decode("utf-8", errors="replace")

    async def _monitor_stderr_asyncio(self) -> None:
        if self._stderr_reader is None:
            return
        try:
            while self._running:
                line = await self._stderr_reader.readline()
                if not line:
                    break
                text = self._decode_stdout_line(line).strip()
                if text:
                    logger.debug(f"[Subprocess stderr] {text}")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error(f"Error monitoring stderr: {exc}")

    async def _monitor_stderr_threaded(self) -> None:
        if not isinstance(self._process, subprocess.Popen) or self._process.stderr is None:
            return

        loop = asyncio.get_event_loop()
        executor = self._get_fallback_executor()

        try:
            while self._running and self._process.poll() is None:
                line = await loop.run_in_executor(executor, self._process.stderr.readline)
                if not line:
                    break
                line = line.strip()
                if line:
                    logger.debug(f"[Subprocess stderr] {line}")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error(f"Error monitoring stderr: {exc}")

    async def stop(self) -> None:
        """Stop the client and terminate subprocess if running."""
        if not self._running:
            return

        self._running = False

        # Cancel read task
        if self._read_task:
            self._read_task.cancel()
            try:
                await self._read_task
            except asyncio.CancelledError:
                pass
            self._read_task = None

        if self._stderr_task:
            self._stderr_task.cancel()
            try:
                await self._stderr_task
            except asyncio.CancelledError:
                pass
            self._stderr_task = None

        # Terminate subprocess if running
        if isinstance(self._process, asyncio.subprocess.Process):
            if self._stdin_writer is not None:
                try:
                    self._stdin_writer.close()
                    await self._stdin_writer.wait_closed()
                except Exception:
                    logger.debug("Failed to close subprocess stdin cleanly")
            logger.info("Terminating subprocess...")
            self._process.terminate()
            try:
                await asyncio.wait_for(self._process.wait(), timeout=5.0)
                logger.info("Subprocess terminated gracefully")
            except TimeoutError:
                logger.warning("Subprocess did not terminate, killing...")
                self._process.kill()
                await self._process.wait()
                logger.info("Subprocess killed")
            finally:
                self._process = None
                self._stdin_writer = None
                self._stdout_reader = None
                self._stderr_reader = None
                self._stdin = None
                self._stdout = None

        if isinstance(self._process, subprocess.Popen):
            if self._stdout:
                try:
                    self._stdout.close()
                except Exception:
                    logger.debug("Failed to close subprocess stdin cleanly")
            logger.info("Terminating subprocess...")
            self._process.terminate()
            loop = asyncio.get_event_loop()
            executor = self._get_fallback_executor()
            try:
                await loop.run_in_executor(executor, self._process.wait, 5.0)
                logger.info("Subprocess terminated gracefully")
            except subprocess.TimeoutExpired:
                logger.warning("Subprocess did not terminate, killing...")
                self._process.kill()
                await loop.run_in_executor(executor, self._process.wait)
                logger.info("Subprocess killed")
            finally:
                self._process = None
                self._stdin = None
                self._stdout = None

        logger.info("StdioClient stopped")

    async def send_message(self, message: JSONRPCMessage) -> None:
        """Send a JSON-RPC message to stdout.

        Args:
            message: The JSON-RPC message to send
        """
        async with self._write_lock:
            try:
                json_str = message.model_dump_json(
                    exclude_none=True,
                    ensure_ascii=True,
                )
                if self._stdin_writer is not None:
                    self._stdin_writer.write((json_str + "\n").encode("utf-8"))
                    await self._stdin_writer.drain()
                else:
                    self._write_line(json_str)
            except Exception as e:
                logger.error(f"Failed to send message: {e}")
                raise

    def _write_line(self, line: str) -> None:
        """Write a line to stdout (synchronous helper)."""
        if self._stdout:
            self._stdout.write(line + "\n")
            self._stdout.flush()

    async def _read_loop(self) -> None:
        """Main loop to read messages from subprocess stdout."""
        if self._stdout_reader is not None:
            await self._read_loop_asyncio()
            return
        await self._read_loop_threaded()

    async def _read_loop_asyncio(self) -> None:
        if self._stdout_reader is None:
            logger.error("No stdout reader available for reading")
            return

        logger.debug("Started reading from stdin")

        try:
            while self._running:
                line = await self._stdout_reader.readline()
                if not line:
                    logger.info("EOF reached on stdin")
                    break

                text = self._decode_stdout_line(line).strip()
                if not text:
                    continue

                try:
                    message = self._parse_message(text)
                    await self._handle_message(message)
                except Exception as exc:
                    logger.error(
                        f"Failed to parse message: {exc}, raw line: {text}"
                    )

        except asyncio.CancelledError:
            logger.debug("Read loop cancelled")
            raise
        except Exception as exc:
            logger.error(f"Error in read loop: {exc}")
        finally:
            logger.debug("Stopped reading from stdin")

    async def _read_loop_threaded(self) -> None:
        if self._stdin is None:
            logger.error("No stdin available for reading")
            return

        logger.debug("Started reading from stdin")
        loop = asyncio.get_event_loop()
        executor = self._get_fallback_executor()

        try:
            while self._running:
                line = await loop.run_in_executor(executor, self._stdin.readline)

                if not line:
                    logger.info("EOF reached on stdin")
                    break

                line = line.strip()
                if not line:
                    continue

                try:
                    message = self._parse_message(line)
                    await self._handle_message(message)
                except Exception as exc:
                    logger.error(
                        f"Failed to parse message: {exc}, raw line: {line}"
                    )

        except asyncio.CancelledError:
            logger.debug("Read loop cancelled")
            raise
        except Exception as exc:
            logger.error(f"Error in read loop: {exc}")
        finally:
            logger.debug("Stopped reading from stdin")

    def _parse_message(self, line: str) -> JSONRPCMessage:
        """Parse a JSON-RPC message from a string.

        Args:
            line: JSON string to parse

        Returns:
            Parsed JSONRPCMessage (Request, SuccessResponse, or ErrorResponse)
        """
        data = json.loads(line)

        # Determine message type based on presence of fields
        if "method" in data:
            return JSONRPCRequest.model_validate(data)
        elif "error" in data:
            return JSONRPCErrorResponse.model_validate(data)
        elif "result" in data:
            return JSONRPCSuccessResponse.model_validate(data)
        else:
            raise ValueError(f"Invalid JSON-RPC message: {data}")
