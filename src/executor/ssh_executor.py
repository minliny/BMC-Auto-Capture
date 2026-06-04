"""
SSH/Telnet executor using Paramiko (pure Python socket — satisfies security policy).

Two strategies:
  - exec_command:    Linux OpenSSH / A3 devices. One channel per command, get_pty=False.
  - interactive_shell: Huawei VRP / L1 / L2 devices. Single invoke_shell() channel,
                       screen-length + all task commands share one transport session.
"""

from __future__ import annotations
import json
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
from ..models.checkpoint import CheckpointSpec
from ..out.file_writer import write_text_file, write_log_file
from ..out.screenshot import render_text_to_image
from ..utils.template import resolve_template, check_unreplaced_vars

MAX_MORE_PAGES = 200

MORE_LINE_RE = re.compile(r'^\s*[-–—]*(?:More|more|MORE)[-–—\s]*$', re.IGNORECASE)
MORE_INLINE_RE = re.compile(
    r'[-–—]{2,}\s*(?:More|more|MORE)\s*[-–—]*', re.IGNORECASE
)
ANSI_RE = re.compile(r'\x1b\[[0-9;]*[a-zA-Z]')


def _strip_pagination_markers(text: str) -> str:
    text = ANSI_RE.sub('', text)
    text = text.replace('\r\n', '\n').replace('\r', '\n')
    lines = []
    for line in text.split('\n'):
        stripped = line.strip()
        if MORE_LINE_RE.match(stripped):
            continue
        clean = MORE_INLINE_RE.sub('', stripped)
        if clean:
            lines.append(clean)
        elif lines and lines[-1].strip():
            lines.append('')
    result = '\n'.join(lines)
    result = re.sub(r'\n{3,}', '\n\n', result)
    return result.strip() + '\n'


def _resolve_var(template: str, variables: dict) -> str:
    def _replace(m):
        key = m.group(1)
        return variables.get(key, m.group(0))
    return re.sub(r'\{\{var\.(\w+)\}\}', _replace, template)


logger = logging.getLogger("bmc_auto_capture.ssh")


class SSHError(Exception):
    pass


class SSHExecutor(AbstractExecutor):
    """Execute SSH_CMD or TELNET_CMD tasks via Paramiko."""

    FINGERPRINT_PROMPTS = re.compile(
        r"(yes/no|\(yes/no|continue connecting|Are you sure)",
        re.IGNORECASE,
    )

    # Device groups that require interactive shell (Huawei VRP / proprietary)
    INTERACTIVE_SHELL_GROUPS = frozenset({"L1", "L2"})

    def __init__(self, connect_timeout: float = 15.0, command_timeout: float = 60.0, idle_timeout: float = 5.0):
        self.connect_timeout = connect_timeout
        self.command_timeout = command_timeout
        self.idle_timeout = idle_timeout

    # ------------------------------------------------------------------
    # SSH strategy detection
    # ------------------------------------------------------------------
    def _get_ssh_strategy(self, device) -> str:
        """Determine SSH strategy based on device group.

        L1 / L2 → interactive_shell (Huawei VRP / 灵衢)
        Everything else → exec_command (Linux OpenSSH, Cisco, etc.)
        """
        group = (device.device_group or "").upper().strip()
        if group in self.INTERACTIVE_SHELL_GROUPS:
            logger.info("SSH strategy: interactive_shell (group=%s)", group)
            return "interactive_shell"
        logger.info("SSH strategy: exec_command (group=%s)", group)
        return "exec_command"

    # ------------------------------------------------------------------
    # Main execute entry point
    # ------------------------------------------------------------------
    def execute(self, plan: TaskPlan, output_root: str) -> ExecutionResult:
        device = plan.device
        task = plan.task

        result = ExecutionResult(
            plan_id=plan.plan_id,
            task_id=plan.task_id,
            client_task_id=plan.client_task_id,
            device_name=device.device_name,
            device_group=device.device_group,
            bmc_ip=device.bmc_ip,
            inband_ip=device.inband_ip,
            task_name=task.task_name,
            task_type=task.task_type,
            execution_mode=task.execution_mode,
            started_at=time.time(),
        )

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

        output_dir = self._build_output_dir(output_root, device, task)
        os.makedirs(output_dir, exist_ok=True)

        cmd_spec = self._parse_command_spec(task)
        commands = cmd_spec["commands"]
        cmd_outputs: dict[str, str] = {}
        variables: dict[str, str] = {}

        client = None
        all_output: list[str] = []
        has_failure = False
        has_timeout = False
        failure_reasons: list[str] = []

        try:
            client = paramiko.SSHClient()
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

            logger.info("[%s] 正在连接到 %s:%s as %s", device.device_name, host, port, username)
            client.connect(
                hostname=host,
                port=port,
                username=username,
                password=password,
                timeout=self.connect_timeout,
                look_for_keys=False,
                allow_agent=False,
            )

            strategy = self._get_ssh_strategy(device)

            all_output, has_failure, has_timeout, failure_reasons, cmd_outputs, step_results = (
                self._execute_commands(
                    client, device, commands, cmd_spec, strategy,
                )
            )

            result.step_results = step_results

            # Determine final status
            if has_timeout:
                result.execution_status = "EXEC_PARTIAL"
                result.execution_failure_reason = f"命令超时 ({len([s for s in step_results if s.status == 'TIMEOUT'])} 个命令超时)"
            elif has_failure:
                result.execution_status = "EXEC_PARTIAL"
                result.execution_failure_reason = "; ".join(failure_reasons[:3])
            else:
                result.execution_status = "EXEC_SUCCESS"

            # Write evidence
            file_base = resolve_template(task.image_name_template, device, task)
            cleaned_output = _strip_pagination_markers("\n\n".join(all_output))
            txt_path = write_text_file(output_dir, f"{file_base}.txt", cleaned_output)
            result.txt_file = txt_path

            ss_path = render_text_to_image(cleaned_output, output_dir, f"{file_base}.png")
            result.screenshots = (ss_path,)
            result.artifact_status = "ARTIFACT_SAVED"
            result.step_results.append(StepResult(
                step_index=len(result.step_results),
                step_name="ssh_terminal_screenshot",
                status="SUCCESS",
                screenshot=ss_path,
                details=f"Terminal output {len(cleaned_output)} chars",
            ))

            # Evaluate checkpoints
            if cmd_spec.get("checkpoints"):
                import asyncio
                asyncio.get_event_loop().run_until_complete(
                    self._evaluate_ssh_checkpoints(cmd_spec["checkpoints"], cmd_outputs, variables,
                                                    result, txt_path, ss_path)
                )

        except socket.timeout as e:
            result.execution_status = "EXEC_FAILED"
            result.execution_failure_reason = f"SSH连接超时 ({self.connect_timeout}s): {e}"
            logger.error("[%s] SSH连接超时: %s", device.device_name, e)
        except socket.error as e:
            result.execution_status = self._classify_socket_error(e)
            result.execution_failure_reason = str(e)
            logger.error("[%s] Socket错误: %s", device.device_name, e)
        except paramiko.AuthenticationException as e:
            result.execution_status = "EXEC_FAILED"
            result.execution_failure_reason = f"SSH认证失败: {e}"
            logger.error("[%s] Auth failed: %s", device.device_name, e)
        except paramiko.SSHException as e:
            result.execution_status = "EXEC_FAILED"
            result.execution_failure_reason = f"SSH错误: {e}"
            logger.error("[%s] SSH error: %s", device.device_name, e)
        except Exception as e:
            result.execution_status = "EXEC_ERROR"
            result.execution_failure_reason = str(e)
            logger.error("[%s] 未知错误: %s", device.device_name, e)
        finally:
            if client is not None:
                try:
                    client.close()
                except Exception:
                    pass

        if not result.screenshots and output_dir:
            logger.warning(
                "[%s] SSH failed — no evidence generated. Status=%s Reason=%s",
                device.device_name, result.execution_status, result.execution_failure_reason,
            )
            result.artifact_status = "ARTIFACT_FAILED"
            result.artifact_failure_reason = result.execution_failure_reason or "SSH execution failed"

        result.output_dir = output_dir
        result.ended_at = time.time()
        result.duration_seconds = result.ended_at - result.started_at

        file_base = resolve_template(task.image_name_template, device, task)
        log_path = write_log_file(output_dir, f"{file_base}.log", self._build_log(result))
        result.log_file = log_path

        return result

    # ------------------------------------------------------------------
    # Command execution — router
    # ------------------------------------------------------------------
    def _execute_commands(self, client, device, commands, cmd_spec, strategy):
        if strategy == "interactive_shell":
            return self._execute_interactive_shell(client, device, commands, cmd_spec)
        else:
            return self._execute_exec_command(client, device, commands, cmd_spec)

    # ------------------------------------------------------------------
    # Strategy A: exec_command (Linux OpenSSH / A3 / Cisco)
    # ------------------------------------------------------------------
    def _execute_exec_command(self, client, device, commands, cmd_spec):
        step_results: list[StepResult] = []
        cmd_outputs: dict[str, str] = {}
        variables: dict[str, str] = {}
        all_output: list[str] = []
        has_failure = False
        has_timeout = False
        failure_reasons: list[str] = []

        # Disable paging via exec_command
        self._disable_paging(client, device, strategy="exec_command")

        task_deadline = time.time() + max(self.command_timeout * len(commands), self.command_timeout) * 2
        step_index = 0

        for cmd_name, cmd in commands:
            step_name = cmd_name or f"cmd_{step_index}"
            logger.info("[%s] exec_command: %s", device.device_name, cmd[:60])

            try:
                if time.time() > task_deadline:
                    raise TimeoutError(f"Task deadline exceeded")

                _stdin, stdout, stderr = client.exec_command(
                    cmd, timeout=self.command_timeout, get_pty=False,
                )

                channel = stdout.channel
                channel.settimeout(self.command_timeout)

                out_chunks, err_chunks, cmd_timed_out, more_count = self._read_channel(
                    channel, stdout, _stdin, device, cmd_deadline=None,
                )

                out = b"".join(out_chunks).decode("utf-8", errors="replace")
                err = b"".join(err_chunks).decode("utf-8", errors="replace")

                combined = out
                if err:
                    combined += f"\n{err}"

                cmd_outputs[cmd_name] = combined
                all_output.append(combined)

                if cmd_spec.get("extractors"):
                    for ex in cmd_spec["extractors"]:
                        if ex.get("from") == f"cmd:{cmd_name}" or not ex.get("from"):
                            self._run_extractor(ex, combined, variables)

                if cmd_timed_out:
                    has_timeout = True
                    has_failure = True
                    failure_reasons.append(f"命令超时: {cmd[:50]}... ({self.command_timeout}s)")
                    step_results.append(StepResult(
                        step_index=step_index, step_name=step_name,
                        status="TIMEOUT", details=f"Timeout after {self.command_timeout}s",
                    ))
                else:
                    step_results.append(StepResult(
                        step_index=step_index, step_name=step_name,
                        status="SUCCESS", details=f"output {len(combined)} chars",
                    ))

            except TimeoutError as e:
                has_timeout = True
                has_failure = True
                failure_reasons.append(f"命令超时: {cmd[:50]}... ({self.command_timeout}s)")
                step_results.append(StepResult(
                    step_index=step_index, step_name=step_name,
                    status="TIMEOUT", details=str(e),
                ))
            except Exception as e:
                has_failure = True
                failure_reasons.append(f"命令失败: {cmd[:50]}... ({e})")
                step_results.append(StepResult(
                    step_index=step_index, step_name=step_name,
                    status="FAILED", details=str(e),
                ))

            step_index += 1

        return all_output, has_failure, has_timeout, failure_reasons, cmd_outputs, step_results

    # ------------------------------------------------------------------
    # Strategy B: interactive_shell (Huawei VRP / L1 / L2 / 灵衢)
    # ------------------------------------------------------------------
    def _execute_interactive_shell(self, client, device, commands, cmd_spec):
        step_results: list[StepResult] = []
        cmd_outputs: dict[str, str] = {}
        variables: dict[str, str] = {}
        all_output: list[str] = []
        has_failure = False
        has_timeout = False
        failure_reasons: list[str] = []

        transport = client.get_transport()
        if transport is None or not transport.is_active():
            raise SSHError("SSH transport is not active — cannot open interactive shell")

        logger.info("[%s] 打开 interactive shell (width=220, height=80)", device.device_name)
        channel = transport.open_session()
        channel.get_pty(width=220, height=80)
        channel.invoke_shell()
        channel.settimeout(self.command_timeout)

        # Read banner / initial prompt
        banner = self._read_until_prompt(channel, device, timeout=5.0)
        all_output.append(banner)

        # Run screen-length 0 temporary in the same channel
        self._send_and_read(channel, "screen-length 0 temporary", device, timeout=3.0)

        # Run each task command in the same channel
        task_deadline = time.time() + max(self.command_timeout * len(commands), self.command_timeout) * 2
        step_index = 0

        for cmd_name, cmd in commands:
            step_name = cmd_name or f"cmd_{step_index}"
            logger.info("[%s] interactive_shell: %s", device.device_name, cmd[:60])

            try:
                if time.time() > task_deadline:
                    raise TimeoutError("Task deadline exceeded")

                if not transport.is_active():
                    raise SSHError("SSH transport died during interactive session")

                output = self._send_and_read(channel, cmd, device, timeout=self.command_timeout)
                cmd_outputs[cmd_name] = output
                all_output.append(output)

                if cmd_spec.get("extractors"):
                    for ex in cmd_spec["extractors"]:
                        if ex.get("from") == f"cmd:{cmd_name}" or not ex.get("from"):
                            self._run_extractor(ex, output, variables)

                step_results.append(StepResult(
                    step_index=step_index, step_name=step_name,
                    status="SUCCESS", details=f"output {len(output)} chars",
                ))

            except TimeoutError as e:
                has_timeout = True
                has_failure = True
                failure_reasons.append(f"命令超时: {cmd[:50]}... ({self.command_timeout}s)")
                step_results.append(StepResult(
                    step_index=step_index, step_name=step_name,
                    status="TIMEOUT", details=str(e),
                ))
            except Exception as e:
                has_failure = True
                failure_reasons.append(f"命令失败: {cmd[:50]}... ({e})")
                step_results.append(StepResult(
                    step_index=step_index, step_name=step_name,
                    status="FAILED", details=str(e),
                ))

            step_index += 1

        try:
            channel.close()
        except Exception:
            pass

        return all_output, has_failure, has_timeout, failure_reasons, cmd_outputs, step_results

    # ------------------------------------------------------------------
    # Interactive shell helpers
    # ------------------------------------------------------------------
    def _read_until_prompt(self, channel, device, timeout: float) -> str:
        """Read initial banner/prompt from interactive shell."""
        deadline = time.time() + timeout
        chunks: list[bytes] = []
        last_data = time.time()
        while time.time() < deadline:
            if channel.recv_ready():
                chunk = channel.recv(65536)
                if chunk:
                    chunks.append(chunk)
                    last_data = time.time()
            elif time.time() - last_data > 2.0:
                break  # idle — prompt received
            else:
                time.sleep(0.1)
        # Drain any final bytes
        try:
            channel.settimeout(0.3)
            while True:
                chunk = channel.recv(4096)
                if not chunk:
                    break
                chunks.append(chunk)
        except Exception:
            pass
        channel.settimeout(self.command_timeout)
        return b"".join(chunks).decode("utf-8", errors="replace")

    def _send_and_read(self, channel, cmd: str, device, timeout: float) -> str:
        """Send a command through the interactive shell and read back the output."""
        channel.send(cmd + "\n")
        deadline = time.time() + timeout
        chunks: list[bytes] = []
        last_data = time.time()
        more_count = 0

        while time.time() < deadline:
            got_data = False
            if channel.recv_ready():
                chunk = channel.recv(65536)
                if chunk:
                    chunks.append(chunk)
                    last_data = time.time()
                    got_data = True

            # Handle pagination in interactive mode
            tail = b"".join(chunks[-2:]).decode("utf-8", errors="replace") if len(chunks) >= 1 else ""
            if tail and ("----More----" in tail or "---- More ----" in tail or "---more---" in tail or "--More--" in tail):
                if more_count >= MAX_MORE_PAGES:
                    logger.warning("[%s] 翻页次数超限 (%s)，停止翻页", device.device_name, more_count)
                    break
                try:
                    channel.send(" ")
                    more_count += 1
                    last_data = time.time()
                    got_data = True
                except Exception:
                    pass

            # Idle detection
            if time.time() - last_data > self.idle_timeout:
                break

            if not got_data:
                time.sleep(0.1)

        # Drain remaining
        try:
            channel.settimeout(0.5)
            while True:
                chunk = channel.recv(4096)
                if not chunk:
                    break
                chunks.append(chunk)
        except Exception:
            pass
        channel.settimeout(self.command_timeout)

        return b"".join(chunks).decode("utf-8", errors="replace")

    # ------------------------------------------------------------------
    # Channel read helper (shared)
    # ------------------------------------------------------------------
    def _read_channel(self, channel, stdout, stdin, device, cmd_deadline=None):
        """Read stdout/stderr from an exec_command channel with pagination handling."""
        if cmd_deadline is None:
            cmd_deadline = time.time() + self.command_timeout

        out_chunks: list[bytes] = []
        err_chunks: list[bytes] = []
        last_data_at = time.time()
        more_count = 0

        while time.time() < cmd_deadline:
            got_data = False

            if channel.recv_ready():
                chunk = channel.recv(65536)
                if chunk:
                    out_chunks.append(chunk)
                    last_data_at = time.time()
                    got_data = True
                else:
                    break

            if channel.recv_stderr_ready():
                chunk = channel.recv_stderr(65536)
                if chunk:
                    err_chunks.append(chunk)
                    got_data = True

            # Pagination
            if out_chunks:
                tail = b"".join(out_chunks[-2:]).decode("utf-8", errors="replace")
                if "----More----" in tail or "---- More ----" in tail or "---more---" in tail or "--More--" in tail:
                    if more_count >= MAX_MORE_PAGES:
                        logger.warning("[%s] 翻页次数超限 (%s)，停止翻页", device.device_name, more_count)
                        break
                    try:
                        stdin.write(" ")
                        stdin.flush()
                        more_count += 1
                        last_data_at = time.time()
                        got_data = True
                    except Exception:
                        pass

            if channel.exit_status_ready():
                break

            if time.time() - last_data_at > self.idle_timeout:
                break

            if not got_data:
                time.sleep(0.1)

        # Drain remaining
        try:
            channel.settimeout(0.5)
            remaining = stdout.read()
            if remaining:
                out_chunks.append(remaining)
        except Exception:
            pass

        cmd_timed_out = time.time() >= cmd_deadline and not channel.exit_status_ready()
        return out_chunks, err_chunks, cmd_timed_out, more_count

    # ------------------------------------------------------------------
    # Command spec parsing
    # ------------------------------------------------------------------
    def _parse_command_spec(self, task) -> dict:
        raw = task.command_or_url.strip()
        if not raw:
            return {"commands": [], "extractors": [], "checkpoints": []}

        if raw.startswith("{"):
            try:
                spec = json.loads(raw)
                commands = []
                for item in spec.get("commands", []):
                    name = item.get("name", "")
                    cmd = _resolve_var(item.get("cmd", ""), {})
                    commands.append((name, cmd))
                return {
                    "commands": commands,
                    "extractors": spec.get("extractors", []),
                    "checkpoints": spec.get("checkpoints", []),
                }
            except json.JSONDecodeError:
                pass

        cmd_list = self._parse_commands(raw)
        return {
            "commands": [(f"cmd_{i}", c) for i, c in enumerate(cmd_list)],
            "extractors": [],
            "checkpoints": [],
        }

    def _parse_commands(self, raw: str) -> list[str]:
        if not raw.strip():
            return []
        if "\n" in raw:
            return [c.strip() for c in raw.split("\n") if c.strip()]
        return [c.strip() for c in raw.split(";") if c.strip()]

    def _run_extractor(self, ex: dict, output: str, variables: dict) -> None:
        ex_type = ex.get("type", "")
        pattern = ex.get("pattern", ex.get("selector", ""))
        var_name = ex.get("var", "")
        if not var_name:
            return

        if ex_type == "regex":
            match = re.search(pattern, output)
            if match:
                variables[var_name] = match.group(1) if match.groups() else match.group(0)
                logger.debug("SSH extractor regex '%s' → %s=%s", pattern, var_name, variables[var_name])
        elif ex_type == "text_contains":
            idx = output.find(pattern)
            if idx >= 0:
                start = max(0, idx - 50)
                end = min(len(output), idx + len(pattern) + 50)
                variables[var_name] = output[start:end].strip()
                logger.debug("SSH extractor text '%s' → %s", var_name, variables[var_name])

    async def _evaluate_ssh_checkpoints(
        self, checkpoints, cmd_outputs, variables, result, txt_path, ss_path,
    ):
        from ..rules.checkpoint_engine import CheckpointEngine
        from ..rules.engine import RuleContext

        specs = [CheckpointSpec.from_dict(c) for c in checkpoints]
        combined_output = "\n\n".join(
            f"[{name}]\n{out}" for name, out in cmd_outputs.items()
        )
        ctx = RuleContext()
        ctx.text_output = combined_output
        ctx.variables = dict(variables)
        ctx.artifacts["txt"] = txt_path
        ctx.artifacts["screenshot"] = ss_path

        engine = CheckpointEngine()
        eval_result = await engine.evaluate(specs, ctx, evidence_ref=txt_path)

        result.checkpoint_results = eval_result.results
        result.checkpoint_status = eval_result.rollup_status()

        for cp in eval_result.results:
            result.step_results.append(StepResult(
                step_index=len(result.step_results),
                step_name=f"checkpoint_{cp.checkpoint_name}",
                status="SUCCESS" if cp.status == "CHECK_PASS" else
                       "FAILED" if cp.status == "CHECK_FAIL" else
                       "WARN" if cp.status == "CHECK_WARN" else "SKIP",
                details=cp.details,
                step_type="checkpoint",
            ))

        if ctx.variables:
            result.runtime_context = json.dumps(ctx.variables, ensure_ascii=False)

    def _build_output_dir(self, root: str, device, task) -> str:
        tmpl = task.output_dir_template
        resolved = resolve_template(tmpl, device, task)
        unreplaced = check_unreplaced_vars(resolved)
        if unreplaced:
            logger.warning("SSH output_dir_template 残留未替换变量: %s in '%s'", unreplaced, tmpl)
        return os.path.join(root, resolved)

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

    def _disable_paging(self, client, device, strategy="exec_command") -> bool:
        disable_commands = [
            "screen-length 0 temporary",
            "screen-length 0",
            "terminal length 0",
            "stty -echo",
        ]
        for disable_cmd in disable_commands:
            try:
                logger.info("[%s] 尝试禁用分页 (strategy=%s): %s",
                           device.device_name, strategy, disable_cmd)

                if strategy == "interactive_shell":
                    # Already handled in _execute_interactive_shell
                    return True

                stdin, stdout, stderr = client.exec_command(disable_cmd, timeout=2, get_pty=False)
                start = time.time()
                response = b""
                while time.time() - start < 2:
                    if stdout.channel.recv_ready():
                        response += stdout.channel.recv(4096)
                    else:
                        time.sleep(0.1)
                resp_text = response.decode('utf-8', errors='replace').lower()
                try:
                    _ = stdout.read()
                except Exception:
                    pass
                try:
                    _ = stderr.read()
                except Exception:
                    pass
                if 'error' not in resp_text and 'invalid' not in resp_text and 'unknown' not in resp_text:
                    logger.info("[%s] 分页已禁用: %s", device.device_name, disable_cmd)
                    return True
                else:
                    logger.debug("[%s] 禁用分页命令响应含错误: %s", device.device_name, resp_text[:100])
            except Exception as e:
                logger.debug("[%s] 禁用分页命令失败（可忽略）: %s (%s)", device.device_name, disable_cmd, e)
                continue
        logger.warning("[%s] 所有禁用分页命令均失败，继续执行", device.device_name)
        return False

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
