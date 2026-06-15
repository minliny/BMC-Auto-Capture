"""
SSH/Telnet executor using Paramiko (pure Python socket — satisfies security policy).

Primary evidence strategies:
  - terminal_session: Linux shell session with PTY, used for terminal-style evidence.
  - interactive_shell: Huawei VRP / L1 / L2 shell with prompt and pagination handling.
  - exec_command: Structured SSH command execution, available only by explicit config.
"""

from __future__ import annotations
import json
import logging
import os
import re
import socket
import time
from dataclasses import dataclass
from pathlib import Path

import paramiko

from .base import AbstractExecutor
from ..models.task_plan import TaskPlan
from ..models.task import resolve_task_timeout_seconds
from ..models.execution_result import ExecutionResult, StepResult
from ..models.checkpoint import CheckpointSpec
from ..out.file_writer import write_text_file, write_log_file
from ..out.screenshot import render_text_to_image
from ..utils.template import resolve_template, check_unreplaced_vars
from ..utils.path_safety import safe_filename, validate_template_for_path

MAX_MORE_PAGES = 200
MAX_OUTPUT_BYTES = 10 * 1024 * 1024  # 10 MB output limit per command
HCCN_MARKER_RE = re.compile(r'={10,}>\s*(\d+)')
TERMINAL_SENTINEL_EXIT_RE_TEMPLATE = r"(?m)^%s:(\d+)[ \t]*$"
VRP_PROMPT_RE = re.compile(
    r"(?m)(?:^|\r?\n)[<\[][^<>\[\]\r\n]{1,128}[>\]][ \t]*(?:\r?\n)?\Z"
)
INTERACTIVE_MORE_RE = re.compile(
    r"(?:[-–—]{2,}\s*More\s*[-–—]*|(?m:^[ \t]*More[ \t\r]*$))",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Unified command resolution
# ---------------------------------------------------------------------------

def resolve_task_no_split(task, device_group: str = "") -> bool:
    """Check whether the resolved command for this device group should NOT be split.

    Priority:
      1. per_group_no_split[device_group] — group-specific override
      2. task._no_split — global no_split flag

    Returns True if the command should be kept as a single raw command.
    """
    pg_ns = getattr(task, '_per_group_no_split', None) or {}
    dg = (device_group or "").strip().upper()
    if pg_ns and dg and dg in pg_ns:
        return bool(pg_ns[dg])
    return bool(getattr(task, '_no_split', False))


def resolve_task_command(task, device_group: str = "") -> str:
    """Resolve the effective command for a task on a specific device group.

    Priority:
      1. per_group_commands[device_group]  — group-specific override
      2. task.command_or_url                — default for all groups

    Returns the resolved command string (may be empty).
    Logs which source was used.
    """
    pgc = getattr(task, '_per_group_commands', None) or {}
    dg = (device_group or "").strip().upper()

    if pgc and dg and dg in pgc:
        cmd = pgc[dg]
        logger.info(
            "resolve_task_command: task=%s group=%s → per_group_commands[%s] (%d chars)",
            getattr(task, 'task_name', '?'), dg, dg, len(str(cmd)),
        )
        return str(cmd)

    cmd = getattr(task, 'command_or_url', '') or ''
    if pgc:
        # per_group_commands exists but no match for this group — log warning
        logger.warning(
            "resolve_task_command: task=%s group=%s → per_group_commands has keys=%s, "
            "no match for '%s', falling back to command_or_url (%d chars)",
            getattr(task, 'task_name', '?'), dg, sorted(pgc.keys()), dg, len(str(cmd)),
        )
    else:
        logger.info(
            "resolve_task_command: task=%s group=%s → command_or_url (%d chars)",
            getattr(task, 'task_name', '?'), dg, len(str(cmd)),
        )
    return str(cmd)

MORE_LINE_RE = re.compile(r'^\s*[-–—]*(?:More|more|MORE)[-–—\s]*$', re.IGNORECASE)
MORE_INLINE_RE = re.compile(
    r'[-–—]{2,}\s*(?:More|more|MORE)\s*[-–—]*', re.IGNORECASE
)
ANSI_RE = re.compile(r'\x1b\[[0-9;]*[a-zA-Z]')
TERMINAL_CONTROL_RE = re.compile(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]')


def _clean_interactive_detection_text(text: str) -> str:
    return TERMINAL_CONTROL_RE.sub('', ANSI_RE.sub('', text))


def _strip_pagination_markers(text: str) -> str:
    text = _clean_interactive_detection_text(text)
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


def _sanitize_raw_stream(text: str) -> str:
    """Minimal sanitization for raw interactive shell streams.

    Only removes ANSI escape codes and pagination markers.
    Preserves all whitespace, line structure, and relative positions
    between prompt and command echo.
    Does NOT strip lines or add/remove newlines.
    """
    text = _clean_interactive_detection_text(text)
    text = text.replace('\r\n', '\n').replace('\r', '\n')
    lines = text.split('\n')
    result = []
    for line in lines:
        stripped = line.strip()
        if MORE_LINE_RE.match(stripped):
            continue
        # Remove inline More markers while preserving original whitespace
        result.append(MORE_INLINE_RE.sub('', line))
    return '\n'.join(result)


def _resolve_var(template: str, variables: dict) -> str:
    def _replace(m):
        key = m.group(1)
        return variables.get(key, m.group(0))
    return re.sub(r'\{\{var\.(\w+)\}\}', _replace, template)


logger = logging.getLogger("bmc_auto_capture.ssh")


class SSHError(Exception):
    pass


def _normalized_option(value) -> str:
    return str(value or "").strip().lower().replace("-", "_")


@dataclass(frozen=True)
class SSHExecutionOptions:
    command_timeout: float
    idle_timeout: float
    retry_count: int


@dataclass(frozen=True)
class StreamEvent:
    stream: str
    ts: float
    data: bytes


def infer_command_role(command: str, explicit_role: str = "") -> str:
    """Classify SSH command role for evidence quality decisions."""
    explicit = _normalized_option(explicit_role)
    if explicit in ("setup", "context", "evidence"):
        return explicit

    cmd = (command or "").strip().lower()
    if not cmd:
        return "evidence"
    if cmd.startswith("screen-length"):
        return "setup"
    if cmd == "system-view" or cmd.startswith("system-view "):
        return "context"
    if cmd.startswith("interface "):
        return "context"
    if cmd in ("quit", "return") or cmd.startswith("quit ") or cmd.startswith("return "):
        return "context"
    if cmd.startswith("display ") or "hccn_tool" in cmd:
        return "evidence"
    return "evidence"


class SSHExecutor(AbstractExecutor):
    """Execute SSH_CMD or TELNET_CMD tasks via Paramiko."""

    FINGERPRINT_PROMPTS = re.compile(
        r"(yes/no|\(yes/no|continue connecting|Are you sure)",
        re.IGNORECASE,
    )

    # Device groups that require VRP interactive shell semantics.
    VRP_GROUPS = frozenset({"L1", "L2"})
    INTERACTIVE_SHELL_GROUPS = VRP_GROUPS  # backward-compatible alias

    def __init__(self, connect_timeout: float = 15.0, command_timeout: float = 60.0, idle_timeout: float = 5.0):
        self.connect_timeout = connect_timeout
        self.command_timeout = command_timeout
        self.idle_timeout = idle_timeout

    def _resolve_execution_options(self, task, device_group: str = "") -> SSHExecutionOptions:
        task_timeout = resolve_task_timeout_seconds(
            task, device_group, fallback=self.command_timeout,
        )
        return SSHExecutionOptions(
            command_timeout=task_timeout,
            idle_timeout=self.idle_timeout,
            retry_count=max(0, int(getattr(task, "retry_count", 0) or 0)),
        )

    def _transcript_join_mode(self, strategy: str) -> str:
        # exec_command runs independent programs; PTY modes must preserve raw prompt/echo flow.
        return "\n\n" if strategy == "exec_command" else ""

    def _format_ssh_transcript(self, outputs: list[str], strategy: str) -> str:
        raw_transcript = self._transcript_join_mode(strategy).join(outputs)
        if strategy in ("interactive_shell", "terminal_session"):
            return _sanitize_raw_stream(raw_transcript)
        return _strip_pagination_markers(raw_transcript)

    # ------------------------------------------------------------------
    # SSH strategy detection
    # ------------------------------------------------------------------
    def _get_ssh_strategy(self, device, task=None) -> str:
        """Determine the SSH transport used for evidence capture.

        User-facing model:
          - ssh_profile=vrp   -> VRP interactive terminal
          - ssh_profile=linux -> Linux terminal-style evidence

        Internal transports:
          - interactive_shell -> VRP prompt/pagination handling
          - terminal_session  -> Linux PTY terminal transcript
          - exec_command      -> structured command mode, explicit opt-in only
        """
        group = (device.device_group or "").upper().strip()
        profile = self._resolve_ssh_profile(device, task)
        evidence_mode = self._resolve_ssh_evidence_mode(task, group)
        explicit_transport = self._resolve_ssh_transport(task, group)

        if explicit_transport:
            logger.info(
                "SSH strategy: %s (explicit transport, group=%s, profile=%s, evidence_mode=%s)",
                explicit_transport, group, profile, evidence_mode,
            )
            return explicit_transport

        if profile == "vrp":
            logger.info(
                "SSH strategy: interactive_shell (profile=vrp, group=%s, evidence_mode=%s)",
                group, evidence_mode,
            )
            return "interactive_shell"

        if evidence_mode == "structured":
            logger.info("SSH strategy: exec_command (profile=%s, group=%s)", profile, group)
            return "exec_command"

        logger.info(
            "SSH strategy: terminal_session (profile=linux, group=%s, evidence_mode=%s)",
            group, evidence_mode,
        )
        return "terminal_session"

    def _resolve_ssh_profile(self, device, task=None) -> str:
        group = (getattr(device, "device_group", "") or "").upper().strip()
        profile = self._task_group_option(task, group, "ssh_profile", "ssh_type")
        profile = _normalized_option(profile)
        if profile in ("ssh_vrp", "vrp"):
            return "vrp"
        if profile in ("ssh_linux", "linux", "ssh", "generic"):
            return "linux"
        if group in self.VRP_GROUPS:
            return "vrp"
        return "linux"

    def _resolve_ssh_evidence_mode(self, task=None, group: str = "") -> str:
        mode = _normalized_option(self._task_group_option(task, group, "evidence_mode", "ssh_evidence_mode"))
        if mode in ("structured", "exec", "exec_command", "command"):
            return "structured"
        return "terminal"

    def _resolve_ssh_transport(self, task=None, group: str = "") -> str:
        value = _normalized_option(
            self._task_group_option(
                task, group, "ssh_transport", "ssh_strategy", "transport",
            )
        )
        if not value:
            return ""
        if value in ("exec", "exec_command", "structured", "command"):
            return "exec_command"
        if value in ("vrp", "interactive", "interactive_shell"):
            return "interactive_shell"
        if value in ("terminal", "terminal_session", "pty", "linux_terminal"):
            return "terminal_session"
        logger.warning("Unknown SSH transport option %r; falling back to profile", value)
        return ""

    def _task_group_option(self, task, group: str, *names: str):
        if task is None:
            return ""
        tdef = getattr(task, "_task_def", None) or {}
        group_key = (group or "").upper().strip()
        for name in names:
            per_group_key = f"per_group_{name}"
            per_group = tdef.get(per_group_key)
            if isinstance(per_group, dict) and group_key:
                if group_key in per_group:
                    return per_group[group_key]
                lower_map = {str(k).upper().strip(): v for k, v in per_group.items()}
                if group_key in lower_map:
                    return lower_map[group_key]
            if name in tdef:
                return tdef[name]
            if hasattr(task, name):
                return getattr(task, name)
        return ""

    # ------------------------------------------------------------------
    # Main execute entry point
    # ------------------------------------------------------------------
    def execute(self, plan: TaskPlan, output_root: str) -> ExecutionResult:
        if plan._resource_lease_held:
            # Scheduler already holds the global ResourceRegistry lease
            return self._execute_impl(plan, output_root)

        # Standalone executor call (no scheduler).  Self-acquire the
        # global ResourceRegistry to prevent concurrent access to the
        # same INBAND endpoint from another thread/execution.
        from ..scheduler.resource_registry import ResourceRegistry

        _reg = ResourceRegistry()
        _meta = {
            "execution_id": plan._execution_id,
            "plan_id": plan.plan_id,
            "device_name": plan.device.device_name,
            "task_name": plan.task.task_name,
        }
        _acquire_start = time.time()
        with _reg.acquire(plan.endpoint_key, _meta):
            _wait_sec = time.time() - _acquire_start
            if _wait_sec > 0.05:
                logger.info(
                    "[%s] Executor fallback acquired %s (wait=%.2fs)",
                    plan.device.device_name, plan.endpoint_key, _wait_sec,
                )
            return self._execute_impl(plan, output_root)

    def _execute_impl(self, plan: TaskPlan, output_root: str) -> ExecutionResult:
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

        # --- Resolve per-group command BEFORE parsing ---
        # This is the SINGLE source of truth for what command executes.
        # _parse_command_spec must receive the resolved command, NOT raw command_or_url.
        dg = getattr(device, 'device_group', '') or ''
        resolved_cmd = resolve_task_command(task, dg)
        no_split = resolve_task_no_split(task, dg)

        cmd_spec = self._parse_command_spec(task, override_command=resolved_cmd, no_split=no_split)
        commands = cmd_spec["commands"]
        cmd_outputs: dict[str, str] = {}
        variables: dict[str, str] = {}

        # --- P0-2: empty commands → EXEC_FAILED ---
        if not commands:
            result.execution_status = "EXEC_FAILED"
            result.execution_failure_reason = (
                f"COMMAND_MISSING: task '{task.task_name}' has no resolved commands "
                f"(command_or_url={getattr(task, 'command_or_url', '')!r}, "
                f"resolved_cmd={resolved_cmd!r}, device_group={dg}, "
                f"per_group_commands={sorted(getattr(task, '_per_group_commands', {}).keys()) if getattr(task, '_per_group_commands', None) else 'N/A'})"
            )
            result.ended_at = time.time()
            result.duration_seconds = result.ended_at - result.started_at
            logger.error("[%s] %s", device.device_name, result.execution_failure_reason)
            return result

        client = None
        transcript_meta: dict | None = None
        all_output: list[str] = []
        has_failure = False
        has_timeout = False
        failure_reasons: list[str] = []

        options = self._resolve_execution_options(task, dg)

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

            strategy = self._get_ssh_strategy(device, task)
            ssh_profile = self._resolve_ssh_profile(device, task)
            ssh_evidence_mode = self._resolve_ssh_evidence_mode(task, dg)

            transcript_meta = {
                "ssh_profile": ssh_profile,
                "ssh_evidence_mode": ssh_evidence_mode,
                "ssh_strategy": strategy,
                "input_echo_available": strategy in ("interactive_shell", "terminal_session"),
                "prompt_preserved": strategy in ("interactive_shell", "terminal_session"),
                "transcript_sanitization": "ansi_only/more_removed/control_chars_removed",
            }

            all_output, has_failure, has_timeout, failure_reasons, cmd_outputs, step_results = (
                self._execute_commands(
                    client, device, commands, cmd_spec, strategy, options,
                )
            )

            result.step_results = step_results

            # --- P0-4: detect ONLY_LOGIN_BANNER / COMMAND_OUTPUT_MISSING ---
            # Check if output contains actual command execution evidence,
            # not just login banner + device prompt.
            if not has_failure and not has_timeout:
                joined_output = "".join(all_output)
                joined_len = len(joined_output.strip())
                cmd_count = len(commands)

                if cmd_count > 0 and joined_len < 100:
                    # Output is suspiciously short — likely only login banner
                    result.execution_status = "EXEC_FAILED"
                    result.execution_failure_reason = (
                        f"COMMAND_OUTPUT_MISSING: {cmd_count} command(s) resolved but "
                        f"total output only {joined_len} chars — likely only login banner, "
                        f"no command echo or output captured"
                    )
                    logger.error("[%s] %s (output preview: %s)",
                                 device.device_name,
                                 result.execution_failure_reason,
                                 joined_output[:200])
                elif cmd_count > 0:
                    # Check if commands actually appear in output (for exec_command, output is raw)
                    cmds_in_output = 0
                    for _name, cmd in commands:
                        if cmd and len(cmd) > 3 and cmd[:30] in joined_output:
                            cmds_in_output += 1
                    if cmds_in_output == 0 and strategy == "exec_command":
                        # exec_command doesn't echo — check if output is non-trivial
                        # Just having non-banner output is fine
                        pass
                    elif cmds_in_output == 0 and strategy in ("interactive_shell", "terminal_session") and joined_len < 500:
                        # Interactive shell should echo commands — if none found + short, suspect
                        result.execution_status = "EXEC_FAILED"
                        result.execution_failure_reason = (
                            f"ONLY_LOGIN_BANNER: {cmd_count} command(s) but no command echo "
                            f"found in {joined_len} chars of output"
                        )
                        logger.error("[%s] %s", device.device_name, result.execution_failure_reason)

            # Determine final status
            if has_timeout:
                result.execution_status = "EXEC_TIMEOUT"
                result.execution_failure_reason = f"命令超时 ({len([s for s in step_results if s.status == 'TIMEOUT'])} 个命令超时)"
            elif has_failure:
                result.execution_status = (
                    "EXEC_FAILED"
                    if strategy in ("exec_command", "terminal_session")
                    else "EXEC_PARTIAL"
                )
                result.execution_failure_reason = "; ".join(failure_reasons[:3])
            elif result.execution_status == "EXEC_SUCCESS":
                evidence_failure = self._evidence_output_failure(
                    commands, cmd_outputs, cmd_spec, strategy,
                )
                if evidence_failure:
                    result.execution_status = (
                        "EXEC_FAILED"
                        if strategy in ("exec_command", "terminal_session")
                        else "EXEC_PARTIAL"
                    )
                    result.execution_failure_reason = evidence_failure

            # P0-6: evaluate SSH rules (required_patterns, forbidden_patterns, min_output_lines, etc.)
            rule_failure = self._evaluate_ssh_rules(
                task, combined_output="".join(all_output), cmd_outputs=cmd_outputs,
                strategy=strategy,
            )
            if rule_failure:
                has_failure = True
                if result.execution_status == "EXEC_SUCCESS":
                    result.execution_status = "EXEC_PARTIAL"
                result.execution_failure_reason = (
                    (result.execution_failure_reason + "; " if result.execution_failure_reason else "")
                    + f"规则检查失败: {rule_failure}"
                )
                logger.warning("[%s] SSH规则检查失败: %s", device.device_name, rule_failure)

            # Write evidence
            # NEW-001: validate template before resolution, sanitize filename
            validate_template_for_path(task.image_name_template, context="file_basename")
            file_base = safe_filename(resolve_template(task.image_name_template, device, task))

            # exec_command: separate commands with double newline (each is an independent program)
            # interactive_shell: raw stream concat — no separator, prompt+echo must stay together
            join_mode = self._transcript_join_mode(strategy)
            transcript_meta["transcript_join_mode"] = "double_newline" if join_mode else "raw_stream_concat"
            transcript_meta["chunk_separator_inserted"] = bool(join_mode)
            transcript_meta["strip_applied"] = (strategy != "interactive_shell")
            full_transcript = self._format_ssh_transcript(all_output, strategy)
            transcript_lines = full_transcript.split("\n")
            total_lines = len(transcript_lines)

            # FullScreenshot: control PNG line limit
            full_ss = getattr(task, "full_screenshot", False)
            ssh_line_limit = 0 if full_ss else 100
            transcript_meta["full_screenshot"] = full_ss
            transcript_meta["ssh_output_line_limit"] = "unlimited" if full_ss else "100"
            transcript_meta["total_line_count"] = total_lines

            if ssh_line_limit > 0 and total_lines > ssh_line_limit:
                png_input = "\n".join(transcript_lines[:ssh_line_limit])
                png_input += f"\n[TRUNCATED: showing first {ssh_line_limit} lines only; full transcript saved in TXT]"
                transcript_meta["rendered_line_count"] = ssh_line_limit + 1
                transcript_meta["truncated"] = True
            else:
                png_input = full_transcript
                transcript_meta["rendered_line_count"] = total_lines
                transcript_meta["truncated"] = False

            txt_path = write_text_file(output_dir, f"{file_base}.txt", full_transcript)
            result.txt_file = txt_path
            transcript_meta["full_transcript_path"] = txt_path

            ss_path = render_text_to_image(png_input, output_dir, f"{file_base}.png")
            result.screenshots = (ss_path,)
            result.artifact_status = "ARTIFACT_SAVED"
            result.step_results.append(StepResult(
                step_index=len(result.step_results),
                step_name="ssh_terminal_screenshot",
                status="SUCCESS",
                screenshot=ss_path,
                details=f"Terminal output {len(full_transcript)} chars",
            ))

            # Evaluate checkpoints
            if cmd_spec.get("checkpoints"):
                import asyncio
                asyncio.get_event_loop().run_until_complete(
                    self._evaluate_ssh_checkpoints(cmd_spec["checkpoints"], cmd_outputs, variables,
                                                    result, txt_path, ss_path)
                )

        except socket.timeout as e:
            result.execution_status = "EXEC_TIMEOUT"
            result.execution_failure_reason = f"SSH连接超时 ({self.connect_timeout}s): {e}"
            logger.error("[%s] SSH连接超时: %s", device.device_name, e)
        except socket.error as e:
            result.execution_status = self._classify_socket_error(e)
            if self._is_transient_socket_error(e):
                result.execution_failure_reason = f"TRANSIENT_NETWORK_ERROR: {e}"
            else:
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

        # NEW-001: validate template before resolution, sanitize filename
        validate_template_for_path(task.image_name_template, context="file_basename")
        file_base = safe_filename(resolve_template(task.image_name_template, device, task))
        result.log_file = ""  # .log files discontinued; metadata in runtime_context
        # Store transcript metadata in runtime_context for diagnostics
        if transcript_meta:
            result.runtime_context = json.dumps(transcript_meta, ensure_ascii=False)

        return result

    # ------------------------------------------------------------------
    # Command execution — router
    # ------------------------------------------------------------------
    def _execute_commands(self, client, device, commands, cmd_spec, strategy, options):
        if strategy == "interactive_shell":
            return self._execute_interactive_shell(client, device, commands, cmd_spec, options)
        if strategy == "terminal_session":
            return self._execute_terminal_session(client, device, commands, cmd_spec, options)
        else:
            return self._execute_exec_command(client, device, commands, cmd_spec, options)

    def _command_role(self, cmd_spec: dict, cmd_name: str, cmd: str) -> str:
        return (cmd_spec.get("command_roles") or {}).get(cmd_name) or infer_command_role(cmd)

    def _evidence_output_failure(
        self,
        commands: list[tuple[str, str]],
        cmd_outputs: dict[str, str],
        cmd_spec: dict,
        strategy: str,
    ) -> str:
        evidence_commands = [
            (name, cmd)
            for name, cmd in commands
            if self._command_role(cmd_spec, name, cmd) == "evidence"
        ]
        if not evidence_commands:
            return "EVIDENCE_COMMAND_MISSING: no evidence command resolved"

        first_missing = ""
        for name, cmd in evidence_commands:
            output = cmd_outputs.get(name, "")
            if self._command_output_has_effective_content(output, cmd, strategy):
                return ""
            if not first_missing:
                first_missing = cmd
        target = first_missing or evidence_commands[0][1]
        return f"EVIDENCE_COMMAND_OUTPUT_MISSING: {target} produced no effective output"

    @staticmethod
    def _command_output_has_effective_content(output: str, cmd: str, strategy: str) -> bool:
        clean = _clean_interactive_detection_text(output or "").replace("\r\n", "\n").replace("\r", "\n")
        if strategy == "exec_command":
            return bool(clean.strip())

        cmd_stripped = (cmd or "").strip()
        if cmd_stripped and cmd_stripped in clean:
            echo_pos = clean.rfind(cmd_stripped)
            clean = clean[echo_pos + len(cmd_stripped):]

        effective_lines = []
        for line in clean.split("\n"):
            stripped = line.strip()
            if not stripped:
                continue
            if cmd_stripped and stripped == cmd_stripped:
                continue
            if VRP_PROMPT_RE.search(stripped):
                continue
            effective_lines.append(stripped)
        return bool(effective_lines)

    # ------------------------------------------------------------------
    # Strategy A: exec_command (Linux OpenSSH / A3 / Cisco)
    # ------------------------------------------------------------------
    def _execute_exec_command(self, client, device, commands, cmd_spec, options):
        step_results: list[StepResult] = []
        cmd_outputs: dict[str, str] = {}
        variables: dict[str, str] = {}
        all_output: list[str] = []
        has_failure = False
        has_timeout = False
        failure_reasons: list[str] = []

        # Disable paging via exec_command
        self._disable_paging(client, device, strategy="exec_command")

        command_timeout = options.command_timeout
        task_deadline = time.time() + max(command_timeout * len(commands), command_timeout)
        step_index = 0

        for cmd_name, cmd in commands:
            step_name = cmd_name or f"cmd_{step_index}"
            logger.info("[%s] exec_command: %s", device.device_name, cmd[:60])

            try:
                if time.time() > task_deadline:
                    raise TimeoutError(f"Task deadline exceeded")

                _stdin, stdout, stderr = client.exec_command(
                    cmd, timeout=command_timeout, get_pty=False,
                )

                channel = stdout.channel
                channel.settimeout(command_timeout)

                out_chunks, err_chunks, stream_events, cmd_timed_out, more_count = self._read_channel(
                    channel, stdout, _stdin, device,
                    cmd_deadline=time.time() + command_timeout,
                    idle_timeout=options.idle_timeout,
                )

                out = b"".join(out_chunks).decode("utf-8", errors="replace")
                err = b"".join(err_chunks).decode("utf-8", errors="replace")

                # P0-6: read exit status — non-zero must be recorded
                exit_code = -1
                exit_code_available = False
                try:
                    # Wait briefly for exit status
                    if channel.exit_status_ready():
                        exit_code = channel.recv_exit_status()
                        exit_code_available = True
                    else:
                        # Channel may still be open — try with short timeout
                        channel.settimeout(2.0)
                        try:
                            exit_status_deadline = time.time() + 2.0
                            while (
                                not channel.exit_status_ready()
                                and time.time() < exit_status_deadline
                            ):
                                if channel.recv_ready():
                                    chunk = channel.recv(65536)
                                    if chunk:
                                        out_chunks.append(chunk)
                                        stream_events.append(
                                            StreamEvent("stdout", time.time(), chunk)
                                        )
                                        out = b"".join(out_chunks).decode("utf-8", errors="replace")
                                else:
                                    time.sleep(0.05)
                            exit_code = channel.recv_exit_status()
                            exit_code_available = True
                        except Exception:
                            pass
                        finally:
                            channel.settimeout(command_timeout)
                except Exception:
                    pass

                combined = self._stream_events_to_text(stream_events)

                cmd_outputs[cmd_name] = combined
                all_output.append(combined)

                if cmd_spec.get("extractors"):
                    for ex in cmd_spec["extractors"]:
                        if ex.get("from") == f"cmd:{cmd_name}" or not ex.get("from"):
                            self._run_extractor(ex, combined, variables)

                # P0-6: determine step status — exit code / timeout / stderr all matter
                step_has_failure = False
                stderr_failure = self._stderr_failure_reason(
                    err, cmd_spec, exit_code if exit_code_available else None,
                )
                nonzero_failure = self._exit_code_is_failure(
                    exit_code if exit_code_available else None,
                    cmd_spec,
                )
                if cmd_timed_out:
                    has_timeout = True
                    has_failure = True
                    step_has_failure = True
                    failure_reasons.append(f"命令超时: {cmd[:50]}... ({command_timeout}s)")
                    step_results.append(StepResult(
                        step_index=step_index, step_name=step_name,
                        status="TIMEOUT",
                        details=json.dumps({
                            "timeout_seconds": command_timeout,
                            "elapsed_seconds": command_timeout,
                            "timeout_type": "command_hard_timeout",
                            "command": cmd,
                            "partial_output": combined,
                        }, ensure_ascii=False),
                    ))
                elif nonzero_failure:
                    step_has_failure = True
                    has_failure = True
                    reason = f"命令非零退出码: exit_code={exit_code} cmd={cmd[:50]}"
                    if err:
                        reason += f" stderr={err[:120]}"
                    failure_reasons.append(reason)
                    step_results.append(StepResult(
                        step_index=step_index, step_name=step_name,
                        status="FAILED", details=reason,
                    ))
                elif stderr_failure:
                    has_failure = True
                    failure_reasons.append(stderr_failure)
                    step_results.append(StepResult(
                        step_index=step_index, step_name=step_name,
                        status="FAILED", details=stderr_failure,
                    ))
                else:
                    step_results.append(StepResult(
                        step_index=step_index, step_name=step_name,
                        status="SUCCESS",
                        details=f"output {len(combined)} chars, exit_code={exit_code}",
                    ))

            except TimeoutError as e:
                has_timeout = True
                has_failure = True
                failure_reasons.append(f"命令超时: {cmd[:50]}... ({command_timeout}s)")
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

    def _execute_terminal_session(self, client, device, commands, cmd_spec, options):
        """Linux PTY session used for terminal-faithful A3 evidence."""
        step_results: list[StepResult] = []
        cmd_outputs: dict[str, str] = {}
        all_output: list[str] = []
        has_failure = False
        has_timeout = False
        failure_reasons: list[str] = []

        channel = client.invoke_shell(width=220, height=80)
        channel.settimeout(options.command_timeout)
        banner, _ = self._read_terminal_until_idle(
            channel, timeout=min(options.command_timeout, 10),
            idle_timeout=min(options.idle_timeout, 2),
        )
        all_output.append(banner)

        for step_index, (cmd_name, cmd) in enumerate(commands):
            sentinel = self._make_terminal_sentinel(step_index)
            stop_pattern = self._terminal_sentinel_pattern(sentinel)
            channel.send(self._append_terminal_sentinel(cmd, sentinel))
            output, meta = self._read_terminal_until_idle(
                channel,
                timeout=options.command_timeout,
                idle_timeout=options.idle_timeout,
                stop_pattern=stop_pattern,
            )
            exit_code = self._extract_terminal_sentinel_exit_code(output, sentinel)
            output = self._strip_terminal_sentinel(output, sentinel)
            meta["sentinel_detected"] = exit_code is not None
            meta["exit_code_available"] = exit_code is not None
            if exit_code is not None:
                meta["exit_code"] = exit_code
            # Some test doubles/devices do not echo input despite PTY. Preserve
            # the submitted command as explicit terminal evidence in that case.
            if cmd not in output:
                output = f"{cmd}\n{output}"
            cmd_outputs[cmd_name] = output
            all_output.append(output)

            if meta["hard_timeout_hit"]:
                has_timeout = True
                has_failure = True
                last_marker = meta.get("last_marker")
                marker_info = f", last_marker={last_marker}" if last_marker is not None else ""
                trunc_info = " [TRUNCATED]" if meta.get("output_truncated") else ""
                reason = f"命令超时: {cmd[:50]}... ({options.command_timeout}s){marker_info}{trunc_info}"
                failure_reasons.append(reason)
                status = "TIMEOUT"
            elif self._matches_any_pattern(
                output, list(cmd_spec.get("stderr_fail_patterns", [])),
            ):
                has_failure = True
                reason = f"terminal output matched fail pattern: {output[:160]}"
                failure_reasons.append(reason)
                status = "FAILED"
            elif exit_code not in (None, 0):
                has_failure = True
                reason = f"命令退出码非0: {exit_code} ({cmd[:50]}...)"
                failure_reasons.append(reason)
                status = "FAILED"
            else:
                status = "SUCCESS"
            step_results.append(StepResult(
                step_index=step_index,
                step_name=cmd_name or f"cmd_{step_index}",
                status=status,
                details=json.dumps(meta, ensure_ascii=False),
            ))

        try:
            channel.close()
        except Exception:
            pass
        return all_output, has_failure, has_timeout, failure_reasons, cmd_outputs, step_results

    def _make_terminal_sentinel(self, step_index: int) -> str:
        return f"__BMC_AUTO_CAPTURE_DONE_{os.getpid()}_{int(time.time() * 1000000)}_{step_index}__"

    def _append_terminal_sentinel(self, cmd: str, sentinel: str) -> str:
        return f"{cmd}\nprintf '\\n{sentinel}:%s\\n' \"$?\"\n"

    def _terminal_sentinel_pattern(self, sentinel: str):
        return re.compile(TERMINAL_SENTINEL_EXIT_RE_TEMPLATE % re.escape(sentinel))

    def _extract_terminal_sentinel_exit_code(self, output: str, sentinel: str) -> int | None:
        normalised = output.replace("\r\n", "\n").replace("\r", "\n")
        match = self._terminal_sentinel_pattern(sentinel).search(normalised)
        if not match:
            return None
        try:
            return int(match.group(1))
        except ValueError:
            return None

    def _strip_terminal_sentinel(self, output: str, sentinel: str) -> str:
        lines = output.replace("\r\n", "\n").replace("\r", "\n").split("\n")
        kept = [line for line in lines if sentinel not in line]
        return "\n".join(kept)

    def _read_terminal_until_idle(
        self, channel, timeout: float, idle_timeout: float,
        max_output_bytes: int = MAX_OUTPUT_BYTES,
        stop_pattern=None,
    ) -> tuple[str, dict]:
        started_at = time.time()
        deadline = started_at + timeout
        last_data_at = started_at
        chunks: list[bytes] = []
        bytes_read = 0
        output_truncated = False
        last_marker = None
        timeout_reason = ""

        while time.time() < deadline:
            if channel.recv_ready():
                chunk = channel.recv(65536)
                if not chunk:
                    break
                chunks.append(chunk)
                bytes_read += len(chunk)
                last_data_at = time.time()

                # Check output limit
                if bytes_read >= max_output_bytes:
                    output_truncated = True
                    timeout_reason = "max_output_bytes_reached"
                    break

                # Check for hccn_tool markers
                try:
                    text_so_far = b"".join(chunks).decode("utf-8", errors="replace")
                    markers = HCCN_MARKER_RE.findall(text_so_far)
                    if markers:
                        last_marker = int(markers[-1])
                    if stop_pattern is not None:
                        normalised_text = text_so_far.replace("\r\n", "\n").replace("\r", "\n")
                        if stop_pattern.search(normalised_text):
                            timeout_reason = "sentinel_detected"
                            break
                except Exception:
                    pass

                continue

            if chunks and time.time() - last_data_at >= idle_timeout:
                timeout_reason = "idle_timeout"
                break
            if not chunks and time.time() - started_at >= min(timeout, idle_timeout):
                timeout_reason = "no_output_timeout"
                break
            time.sleep(0.05)

        finished_at = time.time()
        output = b"".join(chunks).decode("utf-8", errors="replace")

        # Extract last non-empty line
        lines = output.strip().split("\n")
        last_non_empty_line = ""
        for line in reversed(lines):
            if line.strip():
                last_non_empty_line = line.strip()[:200]
                break

        if finished_at >= deadline and not timeout_reason:
            timeout_reason = "command_timeout"

        return output, {
            "terminal_session": True,
            "bytes_read": bytes_read,
            "duration_seconds": round(finished_at - started_at, 3),
            "hard_timeout_hit": finished_at >= deadline,
            "idle_timeout_hit": bool(chunks) and finished_at < deadline and timeout_reason == "idle_timeout",
            "sentinel_detected": timeout_reason == "sentinel_detected",
            "output_truncated": output_truncated,
            "max_output_bytes": max_output_bytes,
            "last_non_empty_line": last_non_empty_line,
            "last_marker": last_marker,
            "timeout_reason": timeout_reason,
            "elapsed_seconds": round(finished_at - started_at, 3),
        }

    # ------------------------------------------------------------------
    # Strategy B: interactive_shell (Huawei VRP / L1 / L2 / 灵衢)
    # ------------------------------------------------------------------
    def _execute_interactive_shell(self, client, device, commands, cmd_spec, options):
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
        command_timeout = options.command_timeout
        channel.settimeout(command_timeout)

        # Read banner / initial prompt
        banner = self._read_until_prompt(
            channel, device, timeout=min(command_timeout, 10.0),
            command_timeout=command_timeout,
        )
        all_output.append(banner)

        # Run screen-length 0 temporary in the same channel
        paging_out, paging_meta = self._send_and_read(
            channel,
            "screen-length 0 temporary",
            device,
            timeout=min(command_timeout, 10.0),
            command_timeout=command_timeout,
            idle_timeout=options.idle_timeout,
        )
        all_output.append(paging_out)
        if not paging_meta["prompt_detected"]:
            logger.warning(
                "[%s] screen-length 0 temporary did not return prompt; "
                "continuing with pagination handling enabled",
                device.device_name,
            )

        # Run each task command in the same channel
        task_deadline = time.time() + max(command_timeout * len(commands), command_timeout)
        step_index = 0

        for cmd_name, cmd in commands:
            step_name = cmd_name or f"cmd_{step_index}"
            command_role = self._command_role(cmd_spec, cmd_name, cmd)
            logger.info("[%s] interactive_shell: %s", device.device_name, cmd[:60])

            try:
                if time.time() > task_deadline:
                    raise TimeoutError("Task deadline exceeded")

                if not transport.is_active():
                    raise SSHError("SSH transport died during interactive session")

                output, read_meta = self._send_and_read(
                    channel, cmd, device, timeout=command_timeout,
                    command_timeout=command_timeout,
                    idle_timeout=options.idle_timeout,
                )
                cmd_outputs[cmd_name] = output
                all_output.append(output)

                # Check output classification for quality issues
                output_cls = read_meta.get("output_classification", "OK")
                timeout_reason = read_meta.get("reason") or read_meta.get("timeout_reason") or output_cls
                if (
                    timeout_reason == "HARD_TIMEOUT_WITH_OUTPUT"
                    and read_meta.get("bytes_received", 0) > 0
                ):
                    has_failure = True
                    failure_reasons.append(
                        f"命令输出未结束: {cmd[:50]}... "
                        f"(HARD_TIMEOUT_WITH_OUTPUT, timeout={read_meta.get('timeout_seconds')}s, "
                        f"bytes={read_meta.get('bytes_received')})"
                    )
                    step_results.append(StepResult(
                        step_index=step_index,
                        step_name=step_name,
                        status="PARTIAL",
                        details=json.dumps(read_meta, ensure_ascii=False),
                    ))
                    step_index += 1
                    continue

                if read_meta["hard_timeout_hit"]:
                    has_timeout = True
                    has_failure = True
                    failure_reasons.append(
                        f"命令超时: {cmd[:50]}... "
                        f"({timeout_reason}, timeout={read_meta.get('timeout_seconds')}s)"
                    )
                    step_results.append(StepResult(
                        step_index=step_index,
                        step_name=step_name,
                        status="TIMEOUT",
                        details=json.dumps(read_meta, ensure_ascii=False),
                    ))
                    step_index += 1
                    continue

                if output_cls in ("ONLY_COMMAND_ECHO", "NO_COMMAND_OUTPUT",
                                  "ONLY_LOGIN_BANNER", "PROMPT_TIMEOUT",
                                  "FIRST_OUTPUT_TIMEOUT", "SESSION_LOST"):
                    if (
                        command_role in ("setup", "context")
                        and output_cls == "ONLY_COMMAND_ECHO"
                        and read_meta.get("prompt_detected")
                    ):
                        read_meta["command_role"] = command_role
                        step_results.append(StepResult(
                            step_index=step_index,
                            step_name=step_name,
                            status="SUCCESS",
                            details=json.dumps(read_meta, ensure_ascii=False),
                        ))
                        step_index += 1
                        continue
                    has_failure = True
                    failure_reasons.append(
                        f"命令输出异常: {cmd[:50]}... ({output_cls}, role={command_role})"
                    )
                    read_meta["command_role"] = command_role
                    step_results.append(StepResult(
                        step_index=step_index,
                        step_name=step_name,
                        status="FAILED",
                        details=json.dumps(read_meta, ensure_ascii=False),
                    ))
                    step_index += 1
                    continue

                if read_meta["idle_timeout_hit"]:
                    has_timeout = True
                    has_failure = True
                    failure_reasons.append(
                        f"命令超时: {cmd[:50]}... "
                        f"({timeout_reason}, idle_timeout={read_meta.get('idle_timeout_seconds')}s)"
                    )
                    step_results.append(StepResult(
                        step_index=step_index,
                        step_name=step_name,
                        status="TIMEOUT",
                        details=json.dumps(read_meta, ensure_ascii=False),
                    ))
                    step_index += 1
                    continue

                if cmd_spec.get("extractors"):
                    for ex in cmd_spec["extractors"]:
                        if ex.get("from") == f"cmd:{cmd_name}" or not ex.get("from"):
                            self._run_extractor(ex, output, variables)

                step_results.append(StepResult(
                    step_index=step_index, step_name=step_name,
                    status="SUCCESS",
                    details=json.dumps({**read_meta, "command_role": command_role}, ensure_ascii=False),
                ))

            except TimeoutError as e:
                has_timeout = True
                has_failure = True
                failure_reasons.append(f"命令超时: {cmd[:50]}... ({command_timeout}s)")
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
    def _read_until_prompt(self, channel, device, timeout: float, command_timeout: float | None = None) -> str:
        """Read initial banner/prompt from interactive shell."""
        deadline = time.time() + timeout
        chunks: list[bytes] = []
        last_data = time.time()
        prompt_detected = False
        while time.time() < deadline:
            if channel.recv_ready():
                chunk = channel.recv(65536)
                if chunk:
                    chunks.append(chunk)
                    last_data = time.time()
                    text = b"".join(chunks).decode("utf-8", errors="replace")
                    if VRP_PROMPT_RE.search(_clean_interactive_detection_text(text)):
                        prompt_detected = True
                        break
            elif time.time() - last_data > 2.0:
                break
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
        channel.settimeout(command_timeout or self.command_timeout)
        output = b"".join(chunks).decode("utf-8", errors="replace")
        logger.info(
            "[%s] interactive_shell banner: prompt_detected=%s bytes_received=%d",
            device.device_name,
            prompt_detected,
            len(output.encode("utf-8")),
        )
        return output

    def _send_and_read(
        self, channel, cmd: str, device, timeout: float,
        command_timeout: float | None = None, idle_timeout: float | None = None,
    ) -> tuple[str, dict]:
        """Send a command through the interactive shell and read back the output.

        After sending the command, we must wait for the command echo to appear
        first, then continue reading until we see the VRP prompt AFTER the
        command echo.  This prevents premature exit when the command echo line
        (e.g. ``<hostname>display interface transceiver``) itself matches the
        VRP prompt pattern.
        """
        command_start = time.time()
        channel.send(cmd + "\n")
        deadline = command_start + timeout
        chunks: list[bytes] = []
        first_output_at: float | None = None
        last_output_at: float | None = None
        prompt_detected_at: float | None = None
        more_count = 0
        handled_more_count = 0
        idle_timeout_hit = False
        hard_timeout_hit = False
        effective_idle_timeout = self.idle_timeout if idle_timeout is None else idle_timeout
        first_output_timeout = min(timeout, max(10.0, effective_idle_timeout))

        # Phase tracking: we must see the command echo before accepting a
        # prompt as the "real" end-of-output prompt.  Otherwise the command
        # echo line like <hostname>command matches VRP_PROMPT_RE and we exit
        # before receiving the actual command output.
        cmd_echo_seen = False
        # Normalise the command for echo detection (strip whitespace)
        cmd_stripped = cmd.strip()

        while time.time() < deadline:
            got_data = False
            if channel.recv_ready():
                chunk = channel.recv(65536)
                if chunk:
                    chunks.append(chunk)
                    now = time.time()
                    if first_output_at is None:
                        first_output_at = now
                    last_output_at = now
                    got_data = True

            output_so_far = b"".join(chunks).decode("utf-8", errors="replace")
            clean_output = _clean_interactive_detection_text(output_so_far)

            # Detect command echo: the sent command text appears in the output.
            # This typically shows as "<hostname>command" or "[hostname]command".
            if not cmd_echo_seen and cmd_stripped and cmd_stripped in clean_output:
                cmd_echo_seen = True
                # After seeing the echo, reset last_output_at so idle_timeout
                # counts from the echo, giving the device time to produce output.
                last_output_at = time.time()

            # Only accept prompt detection AFTER the command echo has been seen.
            # This prevents the echo line itself from being misidentified as a
            # final prompt.
            if cmd_echo_seen and VRP_PROMPT_RE.search(clean_output):
                # Verify this prompt is NOT on the same line as the command echo.
                # Find the last prompt match and check it's after the echo.
                prompt_detected_at = time.time()
                break

            # Send one space for each newly observed pagination marker.
            detected_more_count = len(INTERACTIVE_MORE_RE.findall(clean_output))
            if detected_more_count > handled_more_count:
                if more_count >= MAX_MORE_PAGES:
                    logger.warning("[%s] 翻页次数超限 (%s)，停止翻页", device.device_name, more_count)
                    break
                try:
                    channel.send(" ")
                    more_count += 1
                    handled_more_count = detected_more_count
                    logger.info(
                        "interactive_shell pager prompt detected, sending space "
                        "(page=%d)",
                        more_count,
                    )
                    got_data = True
                except Exception:
                    pass

            now = time.time()
            if first_output_at is None and now - command_start > first_output_timeout:
                idle_timeout_hit = True
                break
            if last_output_at is not None and now - last_output_at > effective_idle_timeout:
                idle_timeout_hit = True
                break

            if not got_data:
                time.sleep(0.1)

        if time.time() >= deadline and prompt_detected_at is None:
            hard_timeout_hit = True
        channel.settimeout(command_timeout or self.command_timeout)

        output = b"".join(chunks).decode("utf-8", errors="replace")
        finished_at = time.time()
        bytes_received = len(output.encode("utf-8"))
        time_since_last_output = (
            finished_at - last_output_at if last_output_at is not None else None
        )
        hard_timeout_with_output = (
            hard_timeout_hit
            and prompt_detected_at is None
            and bytes_received > 0
            and time_since_last_output is not None
            and time_since_last_output <= effective_idle_timeout
        )
        if prompt_detected_at is not None:
            timeout_reason = "PROMPT_DETECTED"
        elif hard_timeout_with_output:
            timeout_reason = "HARD_TIMEOUT_WITH_OUTPUT"
        elif hard_timeout_hit:
            timeout_reason = "HARD_TIMEOUT"
        elif idle_timeout_hit and first_output_at is None:
            timeout_reason = "FIRST_OUTPUT_TIMEOUT"
        elif idle_timeout_hit:
            timeout_reason = "PROMPT_TIMEOUT"
        else:
            timeout_reason = "NO_PROMPT"

        # Classify output quality for interactive_shell commands.
        # This helps detect cases where only the command echo was captured
        # without the actual command output body.
        output_classification = "OK"
        clean = _clean_interactive_detection_text(output).replace("\r\n", "\n").replace("\r", "\n")
        if cmd_echo_seen:
            # Check if there is content AFTER the command echo
            echo_pos = clean.rfind(cmd_stripped)
            after_echo = clean[echo_pos + len(cmd_stripped):] if echo_pos >= 0 else ""
            after_echo_lines = [l for l in after_echo.split("\n") if l.strip()]
            # Remove the final prompt line from the count
            if after_echo_lines and VRP_PROMPT_RE.search(after_echo_lines[-1]):
                after_echo_lines = after_echo_lines[:-1]
            if not after_echo_lines:
                output_classification = "ONLY_COMMAND_ECHO"
        elif not clean.strip():
            output_classification = "NO_COMMAND_OUTPUT"
        elif not cmd_echo_seen and first_output_at is not None:
            output_classification = "ONLY_LOGIN_BANNER"

        if prompt_detected_at is None and timeout_reason in (
            "PROMPT_TIMEOUT",
            "FIRST_OUTPUT_TIMEOUT",
            "HARD_TIMEOUT",
            "HARD_TIMEOUT_WITH_OUTPUT",
        ):
            output_classification = timeout_reason

        meta = {
            "command": cmd,
            "timeout_seconds": timeout,
            "idle_timeout_seconds": effective_idle_timeout,
            "command_start_time": command_start,
            "first_output_time": first_output_at,
            "last_output_time": last_output_at,
            "prompt_detected_time": prompt_detected_at,
            "idle_timeout_hit": idle_timeout_hit,
            "hard_timeout_hit": hard_timeout_hit,
            "hard_timeout_with_output": hard_timeout_with_output,
            "bytes_received": bytes_received,
            "prompt_detected": prompt_detected_at is not None,
            "pagination_detected": more_count > 0,
            "pagination_count": more_count,
            "duration_seconds": round(finished_at - command_start, 3),
            "time_since_last_output_seconds": (
                round(time_since_last_output, 3)
                if time_since_last_output is not None else None
            ),
            "reason": timeout_reason,
            "timeout_reason": timeout_reason,
            "cmd_echo_seen": cmd_echo_seen,
            "output_classification": output_classification,
        }
        logger.info(
            "[%s] interactive_shell metrics: command=%r "
            "command_start_time=%.6f first_output_time=%s last_output_time=%s "
            "prompt_detected_time=%s idle_timeout_hit=%s hard_timeout_hit=%s "
            "bytes_received=%d prompt_detected=%s pagination_detected=%s "
            "pagination_count=%d timeout_seconds=%s reason=%s duration=%.3fs",
            device.device_name,
            cmd,
            command_start,
            f"{first_output_at:.6f}" if first_output_at is not None else "None",
            f"{last_output_at:.6f}" if last_output_at is not None else "None",
            f"{prompt_detected_at:.6f}" if prompt_detected_at is not None else "None",
            idle_timeout_hit,
            hard_timeout_hit,
            meta["bytes_received"],
            meta["prompt_detected"],
            meta["pagination_detected"],
            more_count,
            timeout,
            timeout_reason,
            meta["duration_seconds"],
        )
        return output, meta

    # ------------------------------------------------------------------
    # Channel read helper (shared)
    # ------------------------------------------------------------------
    def _read_channel(self, channel, stdout, stdin, device, cmd_deadline=None, idle_timeout=None, max_output_bytes=MAX_OUTPUT_BYTES):
        """Read stdout/stderr from an exec_command channel with pagination handling."""
        if cmd_deadline is None:
            cmd_deadline = time.time() + self.command_timeout
        effective_idle_timeout = self.idle_timeout if idle_timeout is None else idle_timeout

        out_chunks: list[bytes] = []
        err_chunks: list[bytes] = []
        stream_events: list[StreamEvent] = []
        last_data_at = time.time()
        more_count = 0
        bytes_read = 0
        output_truncated = False

        while time.time() < cmd_deadline:
            got_data = False

            if channel.recv_ready():
                chunk = channel.recv(65536)
                if chunk:
                    out_chunks.append(chunk)
                    bytes_read += len(chunk)
                    event_time = time.time()
                    stream_events.append(StreamEvent("stdout", event_time, chunk))
                    last_data_at = event_time
                    got_data = True

                    if bytes_read >= max_output_bytes:
                        output_truncated = True
                        break
                else:
                    break

            if channel.recv_stderr_ready():
                chunk = channel.recv_stderr(65536)
                if chunk:
                    err_chunks.append(chunk)
                    bytes_read += len(chunk)
                    event_time = time.time()
                    stream_events.append(StreamEvent("stderr", event_time, chunk))
                    last_data_at = event_time
                    got_data = True

                    if bytes_read >= max_output_bytes:
                        output_truncated = True
                        break

            # Pagination
            if out_chunks:
                tail = b"".join(out_chunks[-2:]).decode("utf-8", errors="replace")
                clean_tail = _clean_interactive_detection_text(tail)
                if INTERACTIVE_MORE_RE.search(clean_tail):
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

            if time.time() - last_data_at > effective_idle_timeout:
                break

            if not got_data:
                time.sleep(0.1)

        # Drain remaining
        try:
            channel.settimeout(0.5)
            remaining = stdout.read()
            if remaining:
                out_chunks.append(remaining)
                stream_events.append(StreamEvent("stdout", time.time(), remaining))
        except Exception:
            pass

        cmd_timed_out = time.time() >= cmd_deadline and not channel.exit_status_ready()

        # Extract last non-empty line for diagnostics
        all_out = b"".join(out_chunks).decode("utf-8", errors="replace")
        lines = all_out.strip().split("\n")
        last_non_empty_line = ""
        for line in reversed(lines):
            if line.strip():
                last_non_empty_line = line.strip()[:200]
                break

        # Check for hccn_tool markers
        last_marker = None
        markers = HCCN_MARKER_RE.findall(all_out)
        if markers:
            last_marker = int(markers[-1])

        if output_truncated:
            logger.warning(
                "[%s] Output truncated at %d bytes (max=%d), last_marker=%s, last_line=%s",
                device.device_name, bytes_read, max_output_bytes,
                last_marker, last_non_empty_line[:80],
            )

        return out_chunks, err_chunks, stream_events, cmd_timed_out, more_count

    @staticmethod
    def _stream_events_to_text(events: list[StreamEvent]) -> str:
        return b"".join(event.data for event in events).decode(
            "utf-8", errors="replace",
        )

    @staticmethod
    def _matches_any_pattern(text: str, patterns: list[str]) -> bool:
        for pattern in patterns:
            try:
                if re.search(pattern, text, re.IGNORECASE):
                    return True
            except re.error:
                if pattern.lower() in text.lower():
                    return True
        return False

    def _stderr_failure_reason(
        self, stderr_text: str, cmd_spec: dict, exit_code: int | None,
    ) -> str:
        if not stderr_text.strip():
            return ""
        fail_patterns = list(cmd_spec.get("stderr_fail_patterns", []))
        allow_patterns = list(cmd_spec.get("stderr_allow_patterns", []))
        ignore_patterns = list(cmd_spec.get("stderr_ignore_patterns", []))

        if self._matches_any_pattern(stderr_text, fail_patterns):
            return f"stderr matched fail pattern: {stderr_text[:160]}"

        unmatched_lines = [
            line for line in stderr_text.splitlines()
            if line.strip()
            and not self._matches_any_pattern(
                line, allow_patterns + ignore_patterns,
            )
        ]
        if unmatched_lines:
            return f"stderr not allowlisted: {' | '.join(unmatched_lines)[:160]}"
        return ""

    @staticmethod
    def _exit_code_is_failure(exit_code: int | None, cmd_spec: dict) -> bool:
        if exit_code is None or exit_code == 0:
            return False
        allowed = {
            int(code) for code in cmd_spec.get("allow_exit_codes", [])
        }
        return exit_code not in allowed

    # ------------------------------------------------------------------
    # Command spec parsing
    # ------------------------------------------------------------------
    def _parse_command_spec(self, task, override_command: str | None = None,
                            no_split: bool = False) -> dict:
        """Parse task commands. If override_command is given, use it instead of task.command_or_url.

        This is the SINGLE point where commands enter the execution pipeline.
        All callers MUST pass the output of resolve_task_command() as override_command.

        If no_split is True, the entire command string is kept as a single raw command
        (no ; or \\n splitting).  This preserves shell compound commands like for/do/done.
        """
        raw = (override_command if override_command is not None else task.command_or_url).strip()
        if not raw:
            return {"commands": [], "extractors": [], "checkpoints": []}

        if raw.startswith("{"):
            try:
                spec = json.loads(raw)
                commands = []
                command_roles = {}
                for i, item in enumerate(spec.get("commands", [])):
                    name = item.get("name", "") or f"cmd_{i}"
                    cmd = _resolve_var(item.get("cmd", ""), {})
                    commands.append((name, cmd))
                    command_roles[name] = infer_command_role(cmd, item.get("role", ""))
                return {
                    "commands": commands,
                    "command_roles": command_roles,
                    "extractors": spec.get("extractors", []),
                    "checkpoints": spec.get("checkpoints", []),
                    "stderr_allow_patterns": spec.get("stderr_allow_patterns", []),
                    "stderr_ignore_patterns": spec.get("stderr_ignore_patterns", []),
                    "stderr_fail_patterns": spec.get("stderr_fail_patterns", []),
                    "allow_exit_codes": spec.get("allow_exit_codes", []),
                }
            except json.JSONDecodeError:
                pass

        cmd_list = self._parse_commands(raw, no_split=no_split)
        commands = [(f"cmd_{i}", c) for i, c in enumerate(cmd_list)]
        task_def = getattr(task, "_task_def", None) or {}
        return {
            "commands": commands,
            "command_roles": {
                name: infer_command_role(cmd)
                for name, cmd in commands
            },
            "extractors": [],
            "checkpoints": [],
            "stderr_allow_patterns": task_def.get("stderr_allow_patterns", []),
            "stderr_ignore_patterns": task_def.get("stderr_ignore_patterns", []),
            "stderr_fail_patterns": task_def.get("stderr_fail_patterns", []),
            "allow_exit_codes": task_def.get("allow_exit_codes", []),
        }

    def _parse_commands(self, raw: str, no_split: bool = False) -> list[str]:
        """Parse raw command string into a list of individual commands.

        If no_split is True, returns the raw string as a single command entry,
        preserving shell compound structures (for/do/done, if/fi, etc.).
        """
        if not raw.strip():
            return []
        if no_split:
            return [raw.strip()]
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

    def _evaluate_ssh_rules(
        self,
        task,
        combined_output: str,
        cmd_outputs: dict[str, str],
        strategy: str,
    ) -> str:
        """P0-6: Evaluate SSH rules from tasks.json against command output.

        Checks:
          - required_patterns: must be present in output
          - forbidden_patterns: must NOT be present
          - min_output_lines: output must have at least N lines
          - command_echo_required: each command must appear in output (interactive_shell)
          - prompt_required: VRP prompt must appear (interactive_shell)

        Returns empty string on pass, or failure reason string on fail.
        """
        tdef = getattr(task, '_task_def', None) or {}
        ssh_rules = tdef.get("ssh_rules") or tdef.get("rules") or []

        if not ssh_rules:
            return ""

        # Normalize rules: may be list of rule dicts or legacy format
        if isinstance(ssh_rules, list) and ssh_rules and isinstance(ssh_rules[0], dict):
            rules_list = ssh_rules
        else:
            return ""

        failures = []
        for rule in rules_list:
            rule_name = rule.get("rule_name", rule.get("name", "unnamed"))
            enabled = rule.get("enabled", True)
            if enabled is False:
                continue

            rule_type = rule.get("rule_type", rule.get("type", ""))
            checks = rule.get("checks", rule.get("actions", []))

            for check in checks:
                check_type = check.get("type", check.get("action_type", ""))
                target = check.get("target", check.get("value", ""))
                desc = check.get("desc", check.get("description", check_type))

                if check_type in ("text_exists", "required_pattern", "text_contains"):
                    if target and target not in combined_output:
                        failures.append(f"[{rule_name}] {desc}: '{target}' not found")
                elif check_type in ("text_not_exists", "forbidden_pattern", "not_contains_any"):
                    if target and target in combined_output:
                        failures.append(f"[{rule_name}] {desc}: forbidden '{target}' found")
                elif check_type == "min_output_lines":
                    min_lines = int(target) if target else 1
                    actual_lines = len(combined_output.split('\n'))
                    if actual_lines < min_lines:
                        failures.append(
                            f"[{rule_name}] {desc}: only {actual_lines} lines (min {min_lines})"
                        )
                elif check_type == "command_echo_required":
                    if strategy == "interactive_shell":
                        for cmd_name, cmd in (getattr(task, '_resolved_commands', None) or []):
                            if cmd and cmd[:30] not in combined_output:
                                failures.append(
                                    f"[{rule_name}] command echo missing: {cmd[:50]}"
                                )
                elif check_type == "prompt_required":
                    if strategy == "interactive_shell":
                        if not VRP_PROMPT_RE.search(combined_output):
                            failures.append(f"[{rule_name}] VRP prompt not detected in output")

        return "; ".join(failures[:5]) if failures else ""

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
        from ..utils.path_safety import resolve_under_output_root, validate_template_for_path

        tmpl = task.output_dir_template
        # P0-1: fail-fast if template contains sensitive vars (password/token/secret/key)
        if tmpl:
            validate_template_for_path(tmpl, context="output_dir")
        resolved = resolve_template(tmpl, device, task)
        unreplaced = check_unreplaced_vars(resolved)
        if unreplaced:
            logger.warning("SSH output_dir_template 残留未替换变量: %s in '%s'", unreplaced, tmpl)
        # P0-2: all output paths must be contained under root
        return resolve_under_output_root(root, resolved)

    def _classify_socket_error(self, e: socket.error) -> str:
        errno = e.errno if hasattr(e, "errno") else 0
        msg = str(e).lower()
        if self._is_transient_socket_error(e):
            return "EXEC_ERROR"
        if errno == 13 or "permission" in msg or "eacces" in msg:
            return "EXEC_SKIPPED_PORT_BLOCKED"
        if errno == 111 or "connection refused" in msg:
            return "EXEC_SKIPPED_PRECHECK_FAILED"
        if errno == 110 or "timeout" in msg:
            return "EXEC_SKIPPED_PRECHECK_FAILED"
        if errno in (113, 101) or "unreachable" in msg:
            return "EXEC_SKIPPED_PRECHECK_FAILED"
        return "EXEC_ERROR"

    @staticmethod
    def _is_transient_socket_error(e: BaseException) -> bool:
        winerror = getattr(e, "winerror", None)
        msg = str(e).lower()
        return (
            winerror == 10054
            or "10054" in msg
            or "connection reset" in msg
            or "forcibly closed" in msg
            or "远程主机强迫关闭" in msg
            or "remote host closed" in msg
            or "connection aborted" in msg
        )

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

    def _build_log(self, result: ExecutionResult, transcript_meta: dict | None = None) -> str:
        lines = [
            f"Plan ID: {result.plan_id}",
            f"Device: {result.device_name} ({result.device_group})",
            f"BMC IP: {result.bmc_ip}  Inband IP: {result.inband_ip}",
            f"Task: {result.task_name}  Type: {result.task_type}  Mode: {result.execution_mode}",
            f"Status: {result.execution_status}",
            f"Duration: {result.duration_seconds:.1f}s",
        ]
        if transcript_meta:
            lines.append(f"ssh_profile={transcript_meta.get('ssh_profile', 'N/A')}")
            lines.append(f"ssh_evidence_mode={transcript_meta.get('ssh_evidence_mode', 'N/A')}")
            lines.append(f"ssh_strategy={transcript_meta.get('ssh_strategy', 'N/A')}")
            lines.append(f"input_echo_available={transcript_meta.get('input_echo_available', False)}")
            lines.append(f"raw_transcript_preserved=true")
            lines.append(f"prompt_preserved={transcript_meta.get('prompt_preserved', False)}")
            lines.append(f"transcript_sanitization={transcript_meta.get('transcript_sanitization', 'N/A')}")
        if result.execution_failure_reason:
            lines.append(f"Failure: {result.execution_failure_reason}")
        for s in result.step_results:
            lines.append(f"  Step {s.step_index} [{s.status}] {s.step_name}: {s.details}")
        return "\n".join(lines)
