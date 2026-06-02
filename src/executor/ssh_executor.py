"""
SSH/Telnet executor using Paramiko (pure Python socket — satisfies security policy).
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

# 最大翻页次数，防止无限循环
MAX_MORE_PAGES = 200

# 纯分页提示行（只有横线和 More，无实际内容）
MORE_LINE_RE = re.compile(r'^\s*[-–—]*(?:More|more|MORE)[-–—\s]*$', re.IGNORECASE)
# 行内分页提示（从文本中移除）
MORE_INLINE_RE = re.compile(
    r'[-–—]{2,}\s*(?:More|more|MORE)\s*[-–—]*', re.IGNORECASE
)
# ANSI 控制码
ANSI_RE = re.compile(r'\x1b\[[0-9;]*[a-zA-Z]')


def _strip_pagination_markers(text: str) -> str:
    """清理 SSH 输出中的分页提示和其他控制字符。

    移除：
    - 纯分页提示行（如 "---- More ----"）
    - 行内分页提示
    - ANSI escape 序列
    - \r 回车
    """
    # 清理 ANSI
    text = ANSI_RE.sub('', text)
    # 规范化 \r\n 和孤立 \r
    text = text.replace('\r\n', '\n').replace('\r', '\n')
    # 清理分页提示（按行处理）
    lines = []
    for line in text.split('\n'):
        stripped = line.strip()
        # 跳过纯分页提示行（如 "---- More ----"）
        if MORE_LINE_RE.match(stripped):
            continue
        # 从行内移除分页提示
        clean = MORE_INLINE_RE.sub('', stripped)
        # 保留非空行
        if clean:
            lines.append(clean)
        elif lines and lines[-1].strip():
            lines.append('')  # 保留分隔
    # 合并连续空行（最多2个）
    result = '\n'.join(lines)
    result = re.sub(r'\n{3,}', '\n\n', result)
    return result.strip() + '\n'


def _resolve_var(template: str, variables: dict) -> str:
    """Replace {{var.X}} placeholders with extracted variable values."""
    def _replace(m):
        key = m.group(1)
        return variables.get(key, m.group(0))
    return re.sub(r'\{\{var\.(\w+)\}\}', _replace, template)


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

        # Build output directory using unified resolve_template
        output_dir = self._build_output_dir(output_root, device, task)
        os.makedirs(output_dir, exist_ok=True)

        # Parse command spec (new JSON format or legacy static format)
        cmd_spec = self._parse_command_spec(task)
        commands = cmd_spec["commands"]  # list of (name, resolved_cmd)
        cmd_outputs: dict[str, str] = {}  # cmd_name → output
        variables: dict[str, str] = {}   # runtime variables

        client = None
        all_output: list[str] = []
        has_failure = False
        has_timeout = False
        failure_reasons: list[str] = []

        try:
            client = paramiko.SSHClient()
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

            logger.info(f"[{device.device_name}] 正在连接到 {host}:{port} as {username}")
            client.connect(
                hostname=host,
                port=port,
                username=username,
                password=password,
                timeout=self.connect_timeout,
                look_for_keys=False,
                allow_agent=False,
            )

            # 禁用分页（华为 VRP / Cisco / Linux）
            self._disable_paging(client)

            step_index = 0

            # Per-task hard deadline: prevent any single SSH task from blocking forever
            task_deadline = time.time() + max(self.command_timeout * len(commands), self.command_timeout) * 2

            for cmd_name, cmd in commands:
                step_name = cmd_name or f"cmd_{step_index}"
                logger.info(f"[{device.device_name}] 正在执行:  {cmd[:60]}...")

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
                    more_count = 0  # 累计翻页次数

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

                        # Handle pagination: "---- More ----" / "----More----"
                        if out_chunks:
                            tail = b"".join(out_chunks[-2:]).decode("utf-8", errors="replace")
                            if "----More----" in tail or "---- More ----" in tail or "---more---" in tail or "--More--" in tail:
                                if more_count >= MAX_MORE_PAGES:
                                    logger.warning(f"[{device.device_name}] 翻页次数超限 ({more_count})，停止翻页")
                                    break
                                try:
                                    _stdin.write(" ")
                                    _stdin.flush()
                                    more_count += 1
                                    last_data_at = time.time()
                                    got_data = True
                                except Exception:
                                    pass

                        if channel.exit_status_ready():
                            break  # Command process exited (Linux)

                        # Idle detection: no data for idle_timeout → command output complete
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

                    # Check if we hit the hard deadline (timeout)
                    cmd_timed_out = time.time() >= cmd_deadline and not channel.exit_status_ready()
                    if cmd_timed_out:
                        has_timeout = True

                    out = b"".join(out_chunks).decode("utf-8", errors="replace")
                    err = b"".join(err_chunks).decode("utf-8", errors="replace")

                    combined = out
                    if err:
                        combined += f"\n{err}"

                    cmd_outputs[cmd_name] = combined
                    all_output.append(combined)

                    # Run extractors after this command if any are defined
                    if cmd_spec.get("extractors"):
                        for ex in cmd_spec["extractors"]:
                            if ex.get("from") == f"cmd:{cmd_name}" or not ex.get("from"):
                                self._run_extractor(ex, combined, variables)

                    result.step_results.append(StepResult(
                        step_index=step_index,
                        step_name=step_name,
                        status="SUCCESS",
                        details=f"output {len(combined)} chars",
                    ))

                except TimeoutError as e:
                    has_timeout = True
                    has_failure = True
                    failure_reasons.append(f"命令超时: {cmd[:50]}... ({self.command_timeout}s)")
                    # Do NOT inject timeout markers into final evidence — keep only real output.
                    result.step_results.append(StepResult(
                        step_index=step_index, step_name=step_name,
                        status="TIMEOUT", details=str(e),
                    ))
                except Exception as e:
                    has_failure = True
                    failure_reasons.append(f"命令失败: {cmd[:50]}... ({e})")
                    # Do NOT inject error markers into final evidence — keep only real output.
                    result.step_results.append(StepResult(
                        step_index=step_index,
                        step_name=step_name,
                        status="FAILED",
                        details=str(e),
                    ))

                step_index += 1

            # Determine final execution status based on failures
            if has_timeout:
                result.execution_status = "EXEC_PARTIAL"
                result.execution_failure_reason = f"命令超时 ({len([s for s in result.step_results if s.status == 'TIMEOUT'])} 个命令超时)"
            elif has_failure:
                result.execution_status = "EXEC_PARTIAL"
                result.execution_failure_reason = "; ".join(failure_reasons[:3])  # Limit to 3 reasons
            else:
                result.execution_status = "EXEC_SUCCESS"

            # File naming from template using unified resolve_template
            file_base = resolve_template(task.image_name_template, device, task)

            # 清理分页提示后再写入
            cleaned_output = _strip_pagination_markers("\n\n".join(all_output))
            txt_path = write_text_file(output_dir, f"{file_base}.txt", cleaned_output)
            result.txt_file = txt_path

            # Generate terminal-style screenshot
            ss_path = render_text_to_image(cleaned_output, output_dir, f"{file_base}.png")
            result.screenshots = (ss_path,)
            result.artifact_status = "ARTIFACT_SAVED"
            result.step_results.append(StepResult(
                step_index=step_index,
                step_name="ssh_terminal_screenshot",
                status="SUCCESS",
                screenshot=ss_path,
                details=f"Terminal output {len(cleaned_output)} chars",
            ))

            # Evaluate evidence checkpoints (non-blocking, after artifacts saved)
            if cmd_spec.get("checkpoints"):
                import asyncio
                asyncio.get_event_loop().run_until_complete(
                    self._evaluate_ssh_checkpoints(cmd_spec["checkpoints"], cmd_outputs, variables,
                                                    result, txt_path, ss_path)
                )

        except socket.timeout as e:
            result.execution_status = "EXEC_FAILED"
            result.execution_failure_reason = f"SSH连接超时 ({self.connect_timeout}s): {e}"
            logger.error(f"[{device.device_name}] SSH连接超时: {e}")
        except socket.error as e:
            result.execution_status = self._classify_socket_error(e)
            result.execution_failure_reason = str(e)
            logger.error(f"[{device.device_name}] Socket错误: {e}")
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
            logger.error(f"[{device.device_name}] 未知错误: {e}")
        finally:
            if client is not None:
                try:
                    client.close()
                except Exception:
                    pass

        # Generate terminal screenshot for error paths too (partial output)
        if not result.screenshots and output_dir:
            file_base = resolve_template(task.image_name_template, device, task)
            # Error info goes to log only — do NOT generate polluted evidence PNG/TXT.
            logger.warning(
                "[%s] SSH failed — no evidence generated. Status=%s Reason=%s",
                device.device_name, result.execution_status, result.execution_failure_reason,
            )
            result.artifact_status = "ARTIFACT_FAILED"
            result.artifact_failure_reason = result.execution_failure_reason or "SSH execution failed"

        result.output_dir = output_dir
        result.ended_at = time.time()
        result.duration_seconds = result.ended_at - result.started_at

        # Write task log
        file_base = resolve_template(task.image_name_template, device, task)
        log_path = write_log_file(output_dir, f"{file_base}.log", self._build_log(result))
        result.log_file = log_path

        return result

    # ------------------------------------------------------------------
    def _parse_command_spec(self, task) -> dict:
        """Parse command_or_url as either:
        - New JSON format: {"commands": [{"cmd":..., "name":...}], "extractors": [...], "checkpoints": [...]}
        - Legacy static format: "cmd1; cmd2" or "cmd1\\ncmd2"
        Returns {"commands": [(name, resolved_cmd)], "extractors": [...], "checkpoints": [...]}
        """
        raw = task.command_or_url.strip()
        if not raw:
            return {"commands": [], "extractors": [], "checkpoints": []}

        # Try JSON object format first
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

        # Legacy static format
        cmd_list = self._parse_commands(raw)
        return {
            "commands": [(f"cmd_{i}", c) for i, c in enumerate(cmd_list)],
            "extractors": [],
            "checkpoints": [],
        }

    def _parse_commands(self, raw: str) -> list[str]:
        if not raw.strip():
            return []
        # Support semicolon or newline delimiters
        if "\n" in raw:
            return [c.strip() for c in raw.split("\n") if c.strip()]
        return [c.strip() for c in raw.split(";") if c.strip()]

    def _run_extractor(self, ex: dict, output: str, variables: dict) -> None:
        """Run a single extractor against command output."""
        import re
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
        self,
        checkpoints: list,
        cmd_outputs: dict,
        variables: dict,
        result: ExecutionResult,
        txt_path: str,
        ss_path: str,
    ) -> None:
        """Evaluate evidence checkpoints against SSH command outputs."""
        from ..rules.checkpoint_engine import CheckpointEngine
        from ..rules.engine import RuleContext

        specs = [CheckpointSpec.from_dict(c) for c in checkpoints]

        # Build a synthetic text output from all command outputs
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
        # Check for unreplaced variables
        tmpl = task.output_dir_template
        resolved = resolve_template(tmpl, device, task)
        unreplaced = check_unreplaced_vars(resolved)
        if unreplaced:
            logger.warning(f"SSH output_dir_template 残留未替换变量: {unreplaced} in '{tmpl}'")
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

    def _disable_paging(self, client) -> bool:
        """尝试禁用设备分页。

        华为 VRP: screen-length 0 temporary
        Cisco: terminal length 0
        Linux: stty -echo

        Returns:
            True if paging disabled, False if failed (non-fatal)
        """
        disable_commands = [
            "screen-length 0 temporary",
            "screen-length 0",
            "terminal length 0",
            "stty -echo",
        ]
        for disable_cmd in disable_commands:
            try:
                logger.info(f"尝试禁用分页: {disable_cmd}")
                stdin, stdout, stderr = client.exec_command(disable_cmd, timeout=2)
                # 读取响应（不阻塞）
                start = time.time()
                response = b""
                while time.time() - start < 2:
                    if stdout.channel.recv_ready():
                        response += stdout.channel.recv(4096)
                    else:
                        time.sleep(0.1)
                resp_text = response.decode('utf-8', errors='replace').lower()
                if 'error' not in resp_text and 'invalid' not in resp_text and 'unknown' not in resp_text:
                    logger.info(f"分页已禁用: {disable_cmd}")
                    return True
                else:
                    logger.debug(f"禁用分页命令响应含错误信息: {resp_text[:100]}")
            except Exception as e:
                logger.debug(f"禁用分页命令失败（可忽略）: {disable_cmd} ({e})")
                continue
        logger.warning("所有禁用分页命令均失败，继续执行（可能在不支持的设备上）")
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
