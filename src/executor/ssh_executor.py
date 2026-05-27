"""
SSH/Telnet executor using Paramiko (pure Python socket — satisfies security policy).
"""


from __future__ import annotations
import logging
import os
import re
import socket
import time
from pathlib import Path

import paramiko

from .base import AbstractExecutor
from ..models.task_plan import TaskPlan
from ..models.execution_result import ExecutionResult, StepResult
from ..out.file_writer import write_text_file, write_log_file

logger = logging.getLogger("bmc_auto_capture.ssh")


class SSHError(Exception):
    pass


class SSHExecutor(AbstractExecutor):
    """Execute SSH_CMD or TELNET_CMD tasks via Paramiko."""

    # Matches common host-key / fingerprint prompts
    FINGERPRINT_PROMPTS = re.compile(
        r"(yes/no|\(yes/no|continue connecting|Are you sure)",
        re.IGNORECASE,
    )

    def __init__(self, connect_timeout: float = 15.0, command_timeout: float = 60.0, idle_timeout: float = 5.0):
        self.connect_timeout = connect_timeout
        self.command_timeout = command_timeout
        self.idle_timeout = idle_timeout

    def execute(self, plan: TaskPlan, output_root: str) -> ExecutionResult:
        device = plan.device
        task = plan.task

        result = ExecutionResult(
            plan_id=plan.plan_id,
            device_name=device.device_name,
            device_group=device.device_group,
            bmc_ip=device.bmc_ip,
            inband_ip=device.inband_ip,
            task_name=task.task_name,
            task_type=task.task_type,
            execution_mode=task.execution_mode,
            started_at=time.time(),
        )

        # Determine host/port/credentials
        host = device.inband_ip
        port = 22 if task.task_type.upper() == "SSH" else 23
        username = device.inband_username
        password = device.inband_password

        if not host:
            result.execution_status = "EXEC_FAILED"
            result.execution_failure_reason = "带内管理IP为空"
            result.ended_at = time.time()
            result.duration_seconds = result.ended_at - result.started_at
            return result

        # Build output directory
        output_dir = self._build_output_dir(output_root, device, task)
        os.makedirs(output_dir, exist_ok=True)

        # Build command list (split by line or semicolon)
        commands = self._parse_commands(task.command_or_url)

        client = None
        try:
            client = paramiko.SSHClient()
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

            logger.info(f"[{device.device_name}] Connecting to {host}:{port} as {username}")
            client.connect(
                hostname=host,
                port=port,
                username=username,
                password=password,
                timeout=self.connect_timeout,
                look_for_keys=False,
                allow_agent=False,
            )

            all_output: list[str] = []
            step_index = 0

            # Per-task hard deadline: prevent any single SSH task from blocking forever
            task_deadline = time.time() + max(self.command_timeout * len(commands), self.command_timeout) * 2

            for cmd in commands:
                step_name = f"cmd_{step_index}"
                logger.info(f"[{device.device_name}] Executing: {cmd[:60]}...")

                try:
                    # Check task-level timeout before each command
                    if time.time() > task_deadline:
                        raise TimeoutError(f"Task deadline exceeded after {self.command_timeout * 2:.0f}s")

                    _stdin, stdout, stderr = client.exec_command(
                        cmd, timeout=self.command_timeout, get_pty=True,
                    )

                    channel = stdout.channel
                    channel.settimeout(self.command_timeout)

                    out_chunks: list[bytes] = []
                    err_chunks: list[bytes] = []
                    cmd_deadline = time.time() + self.command_timeout
                    last_data_at = time.time()

                    while time.time() < cmd_deadline:
                        got_data = False

                        if channel.recv_ready():
                            chunk = channel.recv(65536)
                            if chunk:
                                out_chunks.append(chunk)
                                last_data_at = time.time()
                                got_data = True
                            else:
                                break  # EOF

                        if channel.recv_stderr_ready():
                            chunk = channel.recv_stderr(65536)
                            if chunk:
                                err_chunks.append(chunk)
                                got_data = True

                        if channel.exit_status_ready():
                            break  # Command process exited (Linux)

                        # Idle detection: no data for idle_timeout → command output complete
                        # This handles network device CLIs that never send exit-status
                        if time.time() - last_data_at > self.idle_timeout:
                            break

                        if not got_data:
                            time.sleep(0.1)

                    # Drain any remaining data after break
                    try:
                        channel.settimeout(0.5)
                        remaining = stdout.read()
                        if remaining:
                            out_chunks.append(remaining)
                    except Exception:
                        pass

                    # Check if we hit the hard deadline (idle timeout is normal, not a warning)
                    if time.time() >= cmd_deadline and not channel.exit_status_ready():
                        out_chunks.append(b"\n[WARNING] Hard timeout - partial output saved")

                    out = b"".join(out_chunks).decode("utf-8", errors="replace")
                    err = b"".join(err_chunks).decode("utf-8", errors="replace")

                    combined = out
                    if err:
                        combined += f"\n[STDERR]\n{err}"

                    all_output.append(f"$ {cmd}\n{combined}")
                    result.step_results.append(StepResult(
                        step_index=step_index,
                        step_name=step_name,
                        status="SUCCESS",
                        details=f"output {len(combined)} chars",
                    ))
                except TimeoutError as e:
                    all_output.append(f"$ {cmd}\n[TIMEOUT] {e}")
                    result.step_results.append(StepResult(
                        step_index=step_index, step_name=step_name,
                        status="TIMEOUT", details=str(e),
                    ))
                except Exception as e:
                    all_output.append(f"$ {cmd}\n[ERROR] {e}")
                    result.step_results.append(StepResult(
                        step_index=step_index,
                        step_name=step_name,
                        status="FAILED",
                        details=str(e),
                    ))

                step_index += 1

            # Write output
            full_output = "\n\n".join(all_output)
            txt_path = write_text_file(output_dir, "output.txt", full_output)
            result.txt_file = txt_path

            result.execution_status = "EXEC_SUCCESS"

        except socket.error as e:
            result.execution_status = self._classify_socket_error(e)
            result.execution_failure_reason = str(e)
            logger.error(f"[{device.device_name}] Socket error: {e}")
        except paramiko.AuthenticationException as e:
            result.execution_status = "EXEC_FAILED"
            result.execution_failure_reason = f"SSH认证失败: {e}"
            logger.error(f"[{device.device_name}] Auth failed: {e}")
        except paramiko.SSHException as e:
            result.execution_status = "EXEC_FAILED"
            result.execution_failure_reason = f"SSH错误: {e}"
            logger.error(f"[{device.device_name}] SSH error: {e}")
        except Exception as e:
            result.execution_status = "EXEC_ERROR"
            result.execution_failure_reason = str(e)
            logger.error(f"[{device.device_name}] Unexpected error: {e}")
        finally:
            if client is not None:
                try:
                    client.close()
                except Exception:
                    pass

        result.output_dir = output_dir
        result.ended_at = time.time()
        result.duration_seconds = result.ended_at - result.started_at

        # Write task log
        log_path = write_log_file(output_dir, "task.log", self._build_log(result))
        result.log_file = log_path

        return result

    # ------------------------------------------------------------------
    def _parse_commands(self, raw: str) -> list[str]:
        if not raw.strip():
            return []
        # Support semicolon or newline delimiters
        if "\n" in raw:
            return [c.strip() for c in raw.split("\n") if c.strip()]
        return [c.strip() for c in raw.split(";") if c.strip()]

    def _build_output_dir(self, root: str, device, task) -> str:
        # Render template variables
        tmpl = task.output_dir_template
        tmpl = tmpl.replace("{device_name}", device.device_name)
        tmpl = tmpl.replace("{device_group}", device.device_group)
        tmpl = tmpl.replace("{task_name}", task.task_name)
        tmpl = tmpl.replace("{task_type}", task.task_type)
        return os.path.join(root, tmpl)

    def _classify_socket_error(self, e: socket.error) -> str:
        errno = e.errno if hasattr(e, "errno") else 0
        msg = str(e).lower()
        if errno == 13 or "permission" in msg or "eacces" in msg:
            return "EXEC_SKIPPED_PORT_BLOCKED"
        if errno == 111 or "connection refused" in msg:
            return "EXEC_SKIPPED_PRECHECK_FAILED"
        if errno == 110 or "timeout" in msg:
            return "EXEC_SKIPPED_PRECHECK_FAILED"
        if errno in (113, 101) or "unreachable" in msg:
            return "EXEC_SKIPPED_PRECHECK_FAILED"
        return "EXEC_ERROR"

    def _build_log(self, result: ExecutionResult) -> str:
        lines = [
            f"Plan ID: {result.plan_id}",
            f"Device: {result.device_name} ({result.device_group})",
            f"BMC IP: {result.bmc_ip}  Inband IP: {result.inband_ip}",
            f"Task: {result.task_name}  Type: {result.task_type}  Mode: {result.execution_mode}",
            f"Status: {result.execution_status}",
            f"Duration: {result.duration_seconds:.1f}s",
        ]
        if result.execution_failure_reason:
            lines.append(f"Failure: {result.execution_failure_reason}")
        for s in result.step_results:
            lines.append(f"  Step {s.step_index} [{s.status}] {s.step_name}: {s.details}")
        return "\n".join(lines)
