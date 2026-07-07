import json
import os
import platform
import re
import shlex
import fnmatch
import shutil
import signal
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from app_config import BASE_DIR, JOB_RUNS_DIR
from utilities.base import UtilityActions, UtilityLayout, UtilityResult

try:
    import psutil  # type: ignore
except Exception:
    psutil = None


DEFAULT_RUNTIME_MODE = "native-argv"
ENV_RUNTIME_MODES = {"gnucobol-env", "dual"}
VALID_RUNTIME_MODES = {DEFAULT_RUNTIME_MODE, *ENV_RUNTIME_MODES}
COBOL_MODULE_SUFFIXES = {".so", ".dylib", ".dll"}

SECTION_FILES = {
    "JESMSG": "JESMSGLG.log",
    "JCL": "JCL.log",
    "SYSOUT": "SYSOUT.log",
    "SYSERR": "SYSERR.log",
    "JOBMETA": "JOBMETA.json",
}

_ACTIVE_JOBS: dict[str, dict] = {}
_JOB_LOCK = threading.Lock()
_JOB_COUNTER = 0
_DEFAULT_SUBMIT_OWNER = "UNKNOWN"


def set_default_submit_owner(owner: str) -> None:
    global _DEFAULT_SUBMIT_OWNER
    _DEFAULT_SUBMIT_OWNER = (owner or "UNKNOWN").strip().upper() or "UNKNOWN"


def _iso_now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _extract_job_counter_value(job_id: str) -> int:
    m = re.match(r"^JOB(\d+)$", str(job_id or "").strip().upper())
    if not m:
        return 0
    try:
        return int(m.group(1))
    except Exception:
        return 0


def _next_job_id() -> str:
    global _JOB_COUNTER

    with _JOB_LOCK:
        if _JOB_COUNTER == 0:
            max_seen = 0
            if JOB_RUNS_DIR.exists():
                for meta in JOB_RUNS_DIR.glob("*/JOBMETA.json"):
                    try:
                        loaded = json.loads(meta.read_text(encoding="utf-8"))
                    except Exception:
                        continue
                    if isinstance(loaded, dict):
                        max_seen = max(max_seen, _extract_job_counter_value(loaded.get("job_id", "")))
            _JOB_COUNTER = max_seen

        _JOB_COUNTER += 1
        return f"JOB{_JOB_COUNTER:05d}"


def _job_meta_path(run_dir: Path) -> Path:
    return run_dir / "JOBMETA.json"


def _write_jobmeta(run_dir: Path, payload: dict) -> None:
    _job_meta_path(run_dir).write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _read_jobmeta(run_dir: Path) -> dict:
    meta_path = _job_meta_path(run_dir)
    if not meta_path.exists() or not meta_path.is_file():
        return {}
    try:
        loaded = json.loads(meta_path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _sample_runtime_metrics(pid: int) -> dict:
    if not pid or psutil is None:
        return {}

    try:
        proc = psutil.Process(pid)
        mem = proc.memory_info().rss
        cpu = proc.cpu_percent(interval=0.0)
        io_data = proc.io_counters() if hasattr(proc, "io_counters") else None
        read_bytes = int(getattr(io_data, "read_bytes", 0) or 0) if io_data is not None else None
        write_bytes = int(getattr(io_data, "write_bytes", 0) or 0) if io_data is not None else None
        return {
            "cpu_percent": round(float(cpu), 1),
            "mem_bytes": int(mem),
            "io_read_bytes": read_bytes,
            "io_write_bytes": write_bytes,
        }
    except Exception:
        return {}


def _prime_process_metrics(proc_obj) -> None:
    if psutil is None or proc_obj is None:
        return
    try:
        proc_obj.cpu_percent(interval=None)
        for child in proc_obj.children(recursive=True):
            try:
                child.cpu_percent(interval=None)
            except Exception:
                continue
    except Exception:
        return


def _merge_runtime_metrics(previous: dict, current: dict) -> dict:
    prev = dict(previous or {})
    cur = dict(current or {})
    if not cur:
        return prev
    if not prev:
        return cur

    prev_cpu = float(prev.get("cpu_percent", 0.0) or 0.0)
    cur_cpu = float(cur.get("cpu_percent", 0.0) or 0.0)
    if cur_cpu <= 0.0 and prev_cpu > 0.0:
        cur["cpu_percent"] = round(prev_cpu, 1)

    for key in ("mem_bytes", "io_read_bytes", "io_write_bytes"):
        if cur.get(key) is None and prev.get(key) is not None:
            cur[key] = prev.get(key)

    return cur


def _sample_runtime_metrics_for_state(state: dict) -> dict:
    if psutil is not None:
        proc_obj = state.get("ps_proc")
        if proc_obj is not None:
            try:
                if not proc_obj.is_running():
                    return {}
                procs = [proc_obj]
                try:
                    procs.extend(proc_obj.children(recursive=True))
                except Exception:
                    pass

                cpu_total = 0.0
                mem_total = 0
                read_total = 0
                write_total = 0
                saw_io = False
                now = time.time()
                samples = dict(state.get("cpu_samples", {}) or {})
                new_samples = {}

                for proc in procs:
                    pid_key = str(getattr(proc, "pid", ""))
                    try:
                        cpu_times = proc.cpu_times()
                        cpu_used = float(getattr(cpu_times, "user", 0.0) or 0.0) + float(getattr(cpu_times, "system", 0.0) or 0.0)
                        prev = samples.get(pid_key)
                        if isinstance(prev, dict):
                            delta_cpu = max(0.0, cpu_used - float(prev.get("cpu_used", 0.0) or 0.0))
                            delta_wall = max(0.0001, now - float(prev.get("wall", now) or now))
                            cpu_total += (delta_cpu / delta_wall) * 100.0
                        new_samples[pid_key] = {"cpu_used": cpu_used, "wall": now}
                    except Exception:
                        pass
                    try:
                        mem_total += int(proc.memory_info().rss)
                    except Exception:
                        pass
                    try:
                        io_data = proc.io_counters() if hasattr(proc, "io_counters") else None
                        if io_data is not None:
                            read_total += int(getattr(io_data, "read_bytes", 0) or 0)
                            write_total += int(getattr(io_data, "write_bytes", 0) or 0)
                            saw_io = True
                    except Exception:
                        pass

                state["cpu_samples"] = new_samples

                return {
                    "cpu_percent": round(float(cpu_total), 1),
                    "mem_bytes": int(mem_total),
                    "io_read_bytes": int(read_total) if saw_io else None,
                    "io_write_bytes": int(write_total) if saw_io else None,
                }
            except Exception:
                pass

    sampled = _sample_runtime_metrics(int(state.get("pid", 0) or 0))
    if sampled:
        return sampled
    return dict(state.get("last_runtime_metrics", {}) or {})


def _extract_jump_option(*values: str) -> str:
    for value in values:
        text = str(value or "").strip().upper()
        if text.startswith("=") and len(text) > 1:
            return text[1:]
    return ""


def _find_entry(catalog: list, norm_dsn: str, normalize_dsn) -> dict:
    for entry in catalog:
        if normalize_dsn(entry.get("dsn", "")) == norm_dsn:
            return entry
    return None


def _entry_abs_path(entry: dict) -> Path:
    raw = str(entry.get("path", "")).strip()
    candidate = Path(raw)
    if candidate.is_absolute():
        return candidate
    return BASE_DIR / candidate


def _default_dataset_relpath(dsn: str) -> str:
    parts = [p for p in dsn.split(".") if p]
    if not parts:
        return "data/UNKNOWN/DATA.dat"
    hlq = parts[0]
    rest = parts[1:] or ["DATA"]
    return f"data/{hlq}/{'_'.join(rest)}.dat"


def _load_jcl_text(actions: UtilityActions, entry: dict, member: str) -> tuple[str, str]:
    is_pds = actions.is_pds_like(entry)
    if is_pds:
        if not member:
            return "", "ENTER MEMBER FOR PARTITIONED DATA SET"
        pds_path = _entry_abs_path(entry)
        member_path = pds_path / member
        if not member_path.exists() or not member_path.is_file():
            return "", f"MEMBER NOT FOUND: {member}"
        try:
            raw = member_path.read_bytes()
        except Exception as e:
            return "", f"UNABLE TO READ MEMBER: {e}"

        text_ccsid = str(entry.get("text_ccsid", "cp037")).strip() or "cp037"
        try:
            return raw.decode(text_ccsid), ""
        except Exception as e:
            return "", f"CCSID DECODE ERROR ({text_ccsid}): {e}"

    lines, load_error = actions.load_dataset_lines(entry)
    if load_error:
        return "", load_error
    return "\n".join(lines), ""


def _parse_job_name(jcl_text: str, fallback: str) -> str:
    for line in jcl_text.splitlines():
        line = line.strip().upper()
        m = re.match(r"^//([A-Z0-9@$#_-]{1,8})\s+JOB\b", line)
        if m:
            return m.group(1)
    return fallback[:8] or "JOB00000"


def _parse_disp(disp_value: str) -> tuple[str, str]:
    text = (disp_value or "").strip().upper()
    if not text:
        return "", ""
    if text.startswith("(") and text.endswith(")"):
        parts = [p.strip() for p in text[1:-1].split(",") if p.strip()]
    else:
        parts = [text]
    primary = parts[0] if parts else ""
    normal = parts[1] if len(parts) > 1 else ""
    return primary, normal


def _iter_jcl_statements(jcl_text: str) -> list[tuple[str, str]]:
    """Return logical JCL statements as (name, text), folding continuation lines."""
    statements = []
    current_name = ""
    current_text = ""

    for raw_line in jcl_text.splitlines():
        if not raw_line.startswith("//"):
            continue

        body = raw_line[2:]
        if not body.strip():
            continue

        is_cont = body[0].isspace()
        if is_cont and current_text:
            current_text += " " + body.strip()
            continue

        if current_text:
            statements.append((current_name, current_text.strip()))

        parts = body.strip().split(None, 1)
        current_name = parts[0].upper() if parts else ""
        current_text = parts[1] if len(parts) > 1 else ""

    if current_text:
        statements.append((current_name, current_text.strip()))

    return statements


def _extract_keyword_value(text: str, keyword: str) -> str:
    pattern = re.compile(rf"\b{re.escape(keyword)}\s*=", re.IGNORECASE)
    m = pattern.search(text)
    if not m:
        return ""

    idx = m.end()
    while idx < len(text) and text[idx].isspace():
        idx += 1
    if idx >= len(text):
        return ""

    if text[idx] == "(":
        depth = 0
        start = idx
        while idx < len(text):
            ch = text[idx]
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0:
                    idx += 1
                    break
            idx += 1
        return text[start:idx].strip()

    start = idx
    while idx < len(text) and text[idx] not in ", ":
        idx += 1
    return text[start:idx].strip()


def _unquote_value(value: str) -> str:
    text = str(value or "").strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {"'", '"'}:
        return text[1:-1]
    return text


def _parse_jcl_steps(jcl_text: str) -> list[dict]:
    steps = []
    current_step = None

    for name, statement in _iter_jcl_statements(jcl_text):
        exec_match = re.match(r"^EXEC\b.*\bPGM\s*=\s*([A-Z0-9@$#_-]{1,64})", statement, re.IGNORECASE)
        if exec_match:
            parm_raw = _extract_keyword_value(statement, "PARM")
            current_step = {
                "step_name": name[:8].upper(),
                "pgm": exec_match.group(1).upper(),
                "parm": _unquote_value(parm_raw),
                "dds": [],
            }
            steps.append(current_step)
            continue

        dd_match = re.match(r"^DD\b\s*(.*)$", statement, re.IGNORECASE)
        if dd_match and current_step is not None:
            params = dd_match.group(1).strip()
            dsn = _extract_keyword_value(params, "DSN").upper()
            disp_raw = _extract_keyword_value(params, "DISP").upper()
            disp_primary, disp_normal = _parse_disp(disp_raw)

            current_step["dds"].append(
                {
                    "ddname": name[:8].upper(),
                    "dsn": dsn,
                    "disp_primary": disp_primary,
                    "disp_normal": disp_normal,
                }
            )

    return steps


def _resolve_default_loadlib(actions: UtilityActions) -> Path:
    catalog = list(actions.load_catalog())
    entry = _find_entry(catalog, "SYS1.LOADLIB", actions.normalize_dsn)
    if entry is not None:
        return _entry_abs_path(entry)
    return BASE_DIR / "data" / "SYS1" / "LOADLIB"


def _find_case_insensitive(path: Path, name: str) -> Path:
    if not path.exists() or not path.is_dir():
        return None
    target = name.upper()
    for child in path.iterdir():
        if child.name.upper() == target:
            return child
    return None


def _resolve_program_path(loadlib_path: Path, pgm: str, is_windows: bool) -> Path:
    candidates = [pgm]
    if is_windows:
        candidates.extend([f"{pgm}.EXE", f"{pgm}.BAT", f"{pgm}.CMD"])
    else:
        candidates.extend([f"{pgm}.so", f"{pgm}.dylib"])

    for candidate in candidates:
        exact = loadlib_path / candidate
        if exact.exists() and exact.is_file():
            return exact
        ci = _find_case_insensitive(loadlib_path, candidate)
        if ci is not None and ci.is_file():
            return ci

    return None


def _normalize_runtime_mode(raw_value: str) -> str:
    mode = str(raw_value or "").strip().lower()
    if mode in VALID_RUNTIME_MODES:
        return mode
    return DEFAULT_RUNTIME_MODE


def _load_program_metadata(pgm_path: Path) -> dict:
    if pgm_path is None:
        return {}

    metadata_path = pgm_path.with_suffix(".meta.json")
    if not metadata_path.exists() or not metadata_path.is_file():
        return {}

    try:
        loaded = json.loads(metadata_path.read_text(encoding="utf-8"))
    except Exception:
        return {}

    return loaded if isinstance(loaded, dict) else {}


def _resolve_runtime_mode(pgm_path: Path) -> str:
    metadata = _load_program_metadata(pgm_path)
    return _normalize_runtime_mode(metadata.get("runtime_mode", DEFAULT_RUNTIME_MODE))


def _sanitize_env_name(ddname: str) -> str:
    cleaned = re.sub(r"[^A-Z0-9_]", "_", str(ddname or "").strip().upper())
    if not cleaned:
        return "DD_UNKNOWN"
    if cleaned[0].isdigit():
        return f"DD_{cleaned}"
    return cleaned


def _should_use_cobcrun(step: dict) -> bool:
    runtime_mode = _normalize_runtime_mode(step.get("runtime_mode", DEFAULT_RUNTIME_MODE))
    return runtime_mode in ENV_RUNTIME_MODES


def _cobcrun_module_name(pgm_path: str) -> str:
    path = Path(str(pgm_path or "").strip())
    suffix = path.suffix.lower()
    if suffix in COBOL_MODULE_SUFFIXES:
        return path.stem
    return path.name


def _quote_for_bash(value: str) -> str:
    return shlex.quote(str(value))


def _quote_for_cmd(value: str) -> str:
    text = str(value).replace('"', '""')
    return f'"{text}"'


def _build_step_command_args(step: dict) -> list[str]:
    args = []
    parm = str(step.get("parm", "")).strip()
    if parm:
        args.extend(["--parm", parm])

    for dd in step.get("dds", []):
        payload = "|".join(
            [
                dd.get("ddname", ""),
                dd.get("dsn", ""),
                dd.get("disp_primary", ""),
                dd.get("disp_normal", ""),
                dd.get("path", ""),
            ]
        )
        args.extend(["--dd", payload])
    return args


def _build_step_env(step: dict) -> dict[str, str]:
    runtime_mode = _normalize_runtime_mode(step.get("runtime_mode", DEFAULT_RUNTIME_MODE))
    if runtime_mode not in ENV_RUNTIME_MODES:
        return {}

    env = {}
    for dd in step.get("dds", []):
        path = str(dd.get("path", "")).strip()
        if not path:
            continue
        env_name = _sanitize_env_name(dd.get("ddname", ""))
        env[env_name] = path
    return env


def _build_bash_script(steps: list[dict]) -> str:
    lines = [
        "#!/usr/bin/env bash",
        "set +e",
        "RC=0",
        "echo \"JCL emulator runtime started\"",
    ]

    for step in steps:
        step_name = step.get("step_name", "STEP")
        pgm = step.get("pgm", "")
        pgm_path = step.get("pgm_path", "")
        use_cobcrun = _should_use_cobcrun(step)
        args = _build_step_command_args(step)
        args_str = " ".join(_quote_for_bash(a) for a in args)
        env_map = _build_step_env(step)
        if use_cobcrun and pgm_path:
            env_map["COB_LIBRARY_PATH"] = str(Path(pgm_path).parent)
        env_str = " ".join(f"{name}={_quote_for_bash(value)}" for name, value in sorted(env_map.items()))
        runner_cmd = "cobcrun" if use_cobcrun else _quote_for_bash(pgm_path)
        runner_target = _quote_for_bash(_cobcrun_module_name(pgm_path)) if use_cobcrun else ""
        invoke_parts = [part for part in [env_str, runner_cmd, runner_target, args_str] if part]
        invoke_cmd = " ".join(invoke_parts)
        if use_cobcrun:
            check_expr = f'command -v cobcrun >/dev/null 2>&1 && [[ -f {_quote_for_bash(pgm_path)} ]]'
            missing_msg = f'COBCRUN NOT FOUND OR MODULE MISSING: {pgm}'
        else:
            check_expr = f'[[ -x {_quote_for_bash(pgm_path)} ]]'
            missing_msg = f'PGM NOT FOUND OR NOT EXECUTABLE: {pgm}'

        lines.extend([
            f'echo "STEP {step_name} EXEC PGM={pgm}"',
            f'if {check_expr}; then',
            f"  {invoke_cmd}".rstrip(),
            "  step_rc=$?",
            f'  echo "__STEP_RC__|{step_name}|$step_rc"',
            "  if [[ $step_rc -ne 0 ]]; then RC=$step_rc; fi",
            "else",
            f'  echo "{missing_msg}"',
            f'  echo "__STEP_RC__|{step_name}|12"',
            "  RC=12",
            "fi",
        ])

    lines.extend([
        "echo \"JCL emulator runtime complete RC=$RC\"",
        "exit $RC",
    ])
    return "\n".join(lines) + "\n"


def _build_windows_script(steps: list[dict]) -> str:
    lines = [
        "@echo off",
        "setlocal enabledelayedexpansion",
        "set RC=0",
        "echo JCL emulator runtime started",
    ]

    for step in steps:
        step_name = step.get("step_name", "STEP")
        pgm = step.get("pgm", "")
        pgm_path = step.get("pgm_path", "")
        use_cobcrun = _should_use_cobcrun(step)
        args = _build_step_command_args(step)
        args_str = " ".join(_quote_for_cmd(a) for a in args)
        env_map = _build_step_env(step)
        if use_cobcrun and pgm_path:
            env_map["COB_LIBRARY_PATH"] = str(Path(pgm_path).parent)
        env_setup = []
        if env_map:
            for name, value in sorted(env_map.items()):
                safe_value = str(value).replace('"', '""')
                env_setup.append(f'set "{name}={safe_value}"')

        runner_cmd = "cobcrun" if use_cobcrun else _quote_for_cmd(pgm_path)
        runner_target = _quote_for_cmd(_cobcrun_module_name(pgm_path)) if use_cobcrun else ""
        run_line = " ".join(part for part in [runner_cmd, runner_target, args_str] if part).rstrip()

        if env_setup:
            runner_parts = env_setup + [run_line]
            step_runner = "cmd.exe /v:on /c \"" + " & ".join(runner_parts) + "\""
        else:
            step_runner = run_line

        missing_msg = f"COBCRUN NOT FOUND OR MODULE MISSING: {pgm}" if use_cobcrun else f"PGM NOT FOUND OR NOT EXECUTABLE: {pgm}"

        lines.extend([
            f"echo STEP {step_name} EXEC PGM={pgm}",
            f"if exist {_quote_for_cmd(pgm_path)} (",
            f"  {step_runner}".rstrip(),
            "  set STEP_RC=!errorlevel!",
            f"  echo __STEP_RC__|{step_name}|!STEP_RC!",
            "  if !STEP_RC! neq 0 set RC=!STEP_RC!",
            ") else (",
            f"  echo {missing_msg}",
            f"  echo __STEP_RC__|{step_name}|12",
            "  set RC=12",
            ")",
        ])

    lines.extend([
        "echo JCL emulator runtime complete RC=%RC%",
        "exit /b %RC%",
    ])
    return "\n".join(lines) + "\n"


def _prepare_run_dir(job_name: str) -> tuple[Path, Path]:
    runs_root = JOB_RUNS_DIR
    runs_root.mkdir(parents=True, exist_ok=True)

    now = datetime.now().strftime("%Y%m%d%H%M%S")
    run_name = f"{job_name}_{now}_{os.getpid()}"
    run_dir = runs_root / run_name
    run_dir.mkdir(parents=True, exist_ok=True)

    return run_dir, run_dir / ("run_job.bat" if platform.system().upper().startswith("WIN") else "run_job.sh")


def _start_job_async(
    job_id: str,
    job_name: str,
    owner: str,
    script_text: str,
    is_windows: bool,
    source_dsn: str,
    source_member: str,
    steps: list[dict],
    jcl_text: str,
    actions: UtilityActions,
    catalog_snapshot: list,
) -> tuple[Path, int, Optional[int]]:
    run_dir, script_path = _prepare_run_dir(job_name)

    script_path.write_text(script_text, encoding="utf-8")
    if not is_windows:
        script_path.chmod(0o755)

    (run_dir / SECTION_FILES["JCL"]).write_text(jcl_text, encoding="utf-8")

    sysout_path = run_dir / SECTION_FILES["SYSOUT"]
    syserr_path = run_dir / SECTION_FILES["SYSERR"]
    stdout_file = sysout_path.open("w", encoding="utf-8")
    stderr_file = syserr_path.open("w", encoding="utf-8")

    cmd = ["cmd.exe", "/c", str(script_path)] if is_windows else ["/bin/bash", str(script_path)]
    popen_kwargs = {
        "stdout": stdout_file,
        "stderr": stderr_file,
        "text": True,
    }
    if is_windows:
        popen_kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    else:
        popen_kwargs["start_new_session"] = True

    proc = subprocess.Popen(cmd, **popen_kwargs)
    ps_proc = None
    if psutil is not None:
        try:
            ps_proc = psutil.Process(proc.pid)
            _prime_process_metrics(ps_proc)
        except Exception:
            ps_proc = None

    pgid = None
    if not is_windows:
        try:
            pgid = os.getpgid(proc.pid)
        except Exception:
            pgid = None

    submitted_at = _iso_now()
    base_meta = {
        "job_id": job_id,
        "job_name": job_name,
        "owner": owner,
        "status": "ACTIVE",
        "pid": proc.pid,
        "pgid": pgid,
        "source_dsn": source_dsn,
        "source_member": source_member,
        "step_count": len(steps),
        "submitted_at": submitted_at,
        "started_at": submitted_at,
        "completed_at": None,
        "return_code": None,
        "runtime_metrics": {},
        "run_dir": str(run_dir),
        "step_results": [],
    }
    _write_jobmeta(run_dir, base_meta)

    with _JOB_LOCK:
        _ACTIVE_JOBS[job_id] = {
            "job_id": job_id,
            "job_name": job_name,
            "owner": owner,
            "proc": proc,
            "pid": proc.pid,
            "pgid": pgid,
            "run_dir": run_dir,
            "source_dsn": source_dsn,
            "source_member": source_member,
            "steps": steps,
            "actions": actions,
            "catalog": list(catalog_snapshot),
            "submitted_at": submitted_at,
            "stdout_file": stdout_file,
            "stderr_file": stderr_file,
            "ps_proc": ps_proc,
            "cpu_samples": {},
            "last_runtime_metrics": {},
            "status": "ACTIVE",
        }

    return run_dir, proc.pid, pgid


def _extract_step_results(stdout: str) -> list[dict]:
    results = []
    for line in stdout.splitlines():
        if not line.startswith("__STEP_RC__|"):
            continue
        parts = line.split("|", 2)
        if len(parts) != 3:
            continue
        step_name = parts[1].strip().upper()
        try:
            step_rc = int(parts[2].strip())
        except Exception:
            step_rc = 16
        results.append({"step_name": step_name, "rc": step_rc})
    return results


def _write_spool_files(
    run_dir: Path,
    job_id: str,
    job_name: str,
    owner: str,
    status: str,
    rc: int,
    source_dsn: str,
    source_member: str,
    steps: list[dict],
    pid: int,
    pgid: Optional[int],
    submitted_at: str,
    runtime_metrics: Optional[dict] = None,
):
    stdout = (run_dir / SECTION_FILES["SYSOUT"]).read_text(encoding="utf-8") if (run_dir / SECTION_FILES["SYSOUT"]).exists() else ""
    stderr = (run_dir / SECTION_FILES["SYSERR"]).read_text(encoding="utf-8") if (run_dir / SECTION_FILES["SYSERR"]).exists() else ""

    step_results = _extract_step_results(stdout)
    jes_lines = [
        f"JOB {job_name} COMPLETION SUMMARY",
        f"SOURCE={source_dsn}{'(' + source_member + ')' if source_member else ''}",
        f"MAXRC={rc}",
        "STEP EXECUTION RESULTS:",
    ]
    if step_results:
        for item in step_results:
            jes_lines.append(f"  {item['step_name']:<8} RC={item['rc']}")
    else:
        for step in steps:
            jes_lines.append(f"  {step.get('step_name', 'STEP'):<8} RC=UNKNOWN")
    (run_dir / SECTION_FILES["JESMSG"]).write_text("\n".join(jes_lines) + "\n", encoding="utf-8")

    summary = {
        "job_id": job_id,
        "job_name": job_name,
        "owner": owner,
        "status": status,
        "pid": pid,
        "pgid": pgid,
        "return_code": rc,
        "source_dsn": source_dsn,
        "source_member": source_member,
        "step_count": len(steps),
        "step_results": step_results,
        "run_dir": str(run_dir),
        "submitted_at": submitted_at,
        "completed_at": _iso_now(),
        "runtime_metrics": runtime_metrics or {},
    }
    _write_jobmeta(run_dir, summary)


def _finalize_completed_jobs() -> None:
    finished: list[tuple[str, dict, int]] = []
    with _JOB_LOCK:
        for job_id, state in list(_ACTIVE_JOBS.items()):
            proc = state.get("proc")
            rc = proc.poll() if proc is not None else 16
            if rc is None:
                continue
            finished.append((job_id, state, int(rc)))
            del _ACTIVE_JOBS[job_id]

    for job_id, state, rc in finished:
        stdout_file = state.get("stdout_file")
        stderr_file = state.get("stderr_file")
        try:
            if stdout_file is not None:
                stdout_file.flush()
                stdout_file.close()
        except Exception:
            pass
        try:
            if stderr_file is not None:
                stderr_file.flush()
                stderr_file.close()
        except Exception:
            pass

        status = "COMPLETE" if rc == 0 else "FAILED"
        if state.get("status") == "CANCELED":
            status = "CANCELED"

        final_metrics = _merge_runtime_metrics(
            dict(state.get("last_runtime_metrics", {}) or {}),
            _sample_runtime_metrics_for_state(state),
        )
        if not final_metrics:
            persisted = _read_jobmeta(state.get("run_dir")) if state.get("run_dir") is not None else {}
            final_metrics = dict(persisted.get("runtime_metrics", {}) or {})

        _write_spool_files(
            run_dir=state.get("run_dir"),
            job_id=job_id,
            job_name=state.get("job_name", "JOB"),
            owner=state.get("owner", "UNKNOWN"),
            status=status,
            rc=rc,
            source_dsn=state.get("source_dsn", ""),
            source_member=state.get("source_member", ""),
            steps=state.get("steps", []),
            pid=int(state.get("pid", 0) or 0),
            pgid=state.get("pgid"),
            submitted_at=state.get("submitted_at", _iso_now()),
            runtime_metrics=final_metrics,
        )

        if rc == 0:
            actions = state.get("actions")
            catalog = state.get("catalog", [])
            steps = state.get("steps", [])
            if actions is not None:
                try:
                    _sync_catalog_for_dispositions(actions, catalog, steps)
                except Exception:
                    pass


def refresh_job_registry() -> None:
    with _JOB_LOCK:
        active_snapshot = list(_ACTIVE_JOBS.values())

    for state in active_snapshot:
        run_dir = state.get("run_dir")
        if run_dir is None:
            continue
        live_metrics = _merge_runtime_metrics(
            dict(state.get("last_runtime_metrics", {}) or {}),
            _sample_runtime_metrics_for_state(state),
        )
        if live_metrics:
            state["last_runtime_metrics"] = dict(live_metrics)
        existing = _read_jobmeta(run_dir)
        if not existing:
            continue
        existing["runtime_metrics"] = dict(state.get("last_runtime_metrics", {}) or {})
        existing["status"] = state.get("status", existing.get("status", "ACTIVE"))
        _write_jobmeta(run_dir, existing)

    _finalize_completed_jobs()


def _read_all_jobmeta() -> dict[str, dict]:
    jobs: dict[str, dict] = {}
    if not JOB_RUNS_DIR.exists():
        return jobs

    for run_dir in JOB_RUNS_DIR.iterdir():
        if not run_dir.is_dir():
            continue
        meta = _read_jobmeta(run_dir)
        if not meta:
            continue
        job_id = str(meta.get("job_id", "")).strip().upper()
        if not job_id:
            continue
        jobs[job_id] = meta
    return jobs


def list_jobs(pre_filter: str = "", owner_filter: str = "", active_only: bool = False) -> list[dict]:
    refresh_job_registry()
    jobs = _read_all_jobmeta()

    with _JOB_LOCK:
        active_ids = set(_ACTIVE_JOBS.keys())

    for job_id, meta in jobs.items():
        if str(meta.get("status", "")).upper() != "ACTIVE":
            continue
        if job_id in active_ids:
            continue

        pid = int(meta.get("pid", 0) or 0)
        running = False
        if pid > 0:
            try:
                if psutil is not None:
                    running = psutil.pid_exists(pid)
                else:
                    os.kill(pid, 0)
                    running = True
            except Exception:
                running = False

        if not running:
            meta["status"] = "ENDED"

    with _JOB_LOCK:
        for job_id, state in _ACTIVE_JOBS.items():
            metrics = _sample_runtime_metrics_for_state(state)
            jobs[job_id] = {
                "job_id": job_id,
                "job_name": state.get("job_name", "JOB"),
                "owner": state.get("owner", "UNKNOWN"),
                "status": state.get("status", "ACTIVE"),
                "pid": state.get("pid"),
                "pgid": state.get("pgid"),
                "return_code": None,
                "source_dsn": state.get("source_dsn", ""),
                "source_member": state.get("source_member", ""),
                "step_count": len(state.get("steps", [])),
                "step_results": [],
                "submitted_at": state.get("submitted_at"),
                "completed_at": None,
                "runtime_metrics": metrics,
                "run_dir": str(state.get("run_dir")),
            }

    pre = (pre_filter or "*").strip().upper() or "*"
    owner = (owner_filter or "*").strip().upper() or "*"

    rows = []
    for meta in jobs.values():
        status = str(meta.get("status", "")).upper()
        if active_only and status not in {"ACTIVE", "STARTING", "CANCELING"}:
            continue

        job_name = str(meta.get("job_name", "")).upper()
        job_id = str(meta.get("job_id", "")).upper()
        owner_value = str(meta.get("owner", "")).upper()

        if not (fnmatch.fnmatchcase(job_name, pre) or fnmatch.fnmatchcase(job_id, pre)):
            continue
        if not fnmatch.fnmatchcase(owner_value, owner):
            continue

        rows.append(meta)

    def _sort_key(item: dict):
        submitted = str(item.get("submitted_at", ""))
        return (submitted, str(item.get("job_id", "")))

    rows.sort(key=_sort_key, reverse=True)
    return rows


def get_job(job_id: str) -> tuple[dict, str]:
    refresh_job_registry()
    target = str(job_id or "").strip().upper()
    if not target:
        return {}, "ENTER JOB ID"

    with _JOB_LOCK:
        state = _ACTIVE_JOBS.get(target)
        if state is not None:
            metrics = _sample_runtime_metrics_for_state(state)
            return {
                "job_id": target,
                "job_name": state.get("job_name", "JOB"),
                "owner": state.get("owner", "UNKNOWN"),
                "status": state.get("status", "ACTIVE"),
                "pid": state.get("pid"),
                "pgid": state.get("pgid"),
                "return_code": None,
                "source_dsn": state.get("source_dsn", ""),
                "source_member": state.get("source_member", ""),
                "step_count": len(state.get("steps", [])),
                "step_results": [],
                "submitted_at": state.get("submitted_at"),
                "completed_at": None,
                "runtime_metrics": metrics,
                "run_dir": str(state.get("run_dir")),
            }, ""

    for meta in _read_all_jobmeta().values():
        if str(meta.get("job_id", "")).strip().upper() == target:
            return meta, ""

    return {}, f"JOB NOT FOUND: {target}"


def get_job_sections(job_id: str) -> tuple[dict, str]:
    meta, err = get_job(job_id)
    if err:
        return {}, err

    run_dir = Path(str(meta.get("run_dir", "")).strip())
    if not run_dir.exists() or not run_dir.is_dir():
        return {}, "RUN DIRECTORY NOT FOUND"

    out = {}
    for name, file_name in SECTION_FILES.items():
        path = run_dir / file_name
        if not path.exists() or not path.is_file():
            out[name] = ""
            continue
        try:
            out[name] = path.read_text(encoding="utf-8")
        except Exception as e:
            out[name] = f"UNABLE TO READ SECTION {name}: {e}"
    return out, ""


def cancel_job(job_id: str) -> str:
    refresh_job_registry()
    target = str(job_id or "").strip().upper()
    if not target:
        return "ENTER JOB ID"

    with _JOB_LOCK:
        state = _ACTIVE_JOBS.get(target)

    if state is None:
        return f"JOB {target} IS NOT ACTIVE"

    proc = state.get("proc")
    if proc is None:
        return f"JOB {target} PROCESS NOT FOUND"

    try:
        if platform.system().upper().startswith("WIN"):
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True, text=True)
        else:
            pgid = state.get("pgid")
            if pgid:
                os.killpg(int(pgid), signal.SIGTERM)
            else:
                proc.terminate()
    except Exception as e:
        return f"CANCEL FAILED FOR {target}: {e}"

    state["status"] = "CANCELED"
    return f"CANCEL REQUESTED FOR {target}"


def purge_job(job_id: str) -> str:
    refresh_job_registry()
    target = str(job_id or "").strip().upper()
    if not target:
        return "ENTER JOB ID"

    with _JOB_LOCK:
        if target in _ACTIVE_JOBS:
            return f"JOB {target} IS ACTIVE - CANCEL FIRST"

    meta, err = get_job(target)
    if err:
        return err

    run_dir = Path(str(meta.get("run_dir", "")).strip())
    if not run_dir.exists() or not run_dir.is_dir():
        return f"RUN DIRECTORY NOT FOUND FOR {target}"

    try:
        shutil.rmtree(run_dir)
    except Exception as e:
        return f"PURGE FAILED FOR {target}: {e}"
    return f"PURGED {target}"


def _resolve_dd_paths(actions: UtilityActions, catalog: list, step: dict, known_new_dsns: set[str]) -> tuple[dict, str]:
    def _split_dsn_member(value: str) -> tuple[str, str]:
        text = str(value or "").strip().upper()
        if text.endswith(")") and "(" in text:
            base, member = text[:-1].split("(", 1)
            return base.strip(), member.strip()
        return text, ""

    for dd in step.get("dds", []):
        dsn = actions.normalize_dsn(dd.get("dsn", ""))
        dd["dsn"] = dsn
        dd["path"] = ""

        if not dsn:
            continue

        base_dsn, member = _split_dsn_member(dsn)
        if member:
            base_entry = _find_entry(catalog, base_dsn, actions.normalize_dsn)
            if base_entry is None:
                return step, f"DD DSN NOT FOUND IN CATALOG: {base_dsn}"
            if not actions.is_pds_like(base_entry):
                return step, f"DD MEMBER REQUIRES PARTITIONED DATA SET: {dsn}"
            member_path = _entry_abs_path(base_entry) / member
            if not member_path.exists() or not member_path.is_file():
                return step, f"DD MEMBER NOT FOUND: {dsn}"
            dd["path"] = str(member_path)
            continue

        entry = _find_entry(catalog, dsn, actions.normalize_dsn)
        if entry is not None:
            dd["path"] = str(_entry_abs_path(entry))
            continue

        if dd.get("disp_primary", "") == "NEW":
            rel = _default_dataset_relpath(dsn)
            dd["path"] = str(BASE_DIR / rel)
            known_new_dsns.add(dsn)
            continue

        # Allow later steps to reference data sets created as DISP=NEW in earlier steps.
        if dsn in known_new_dsns:
            rel = _default_dataset_relpath(dsn)
            dd["path"] = str(BASE_DIR / rel)
            continue

        return step, f"DD DSN NOT FOUND IN CATALOG: {dsn}"

    return step, ""


def _sync_catalog_for_dispositions(actions: UtilityActions, catalog: list, steps: list[dict]) -> None:
    changed = False

    for step in steps:
        for dd in step.get("dds", []):
            dsn = actions.normalize_dsn(dd.get("dsn", ""))
            if not dsn:
                continue

            disp_primary = dd.get("disp_primary", "")
            disp_normal = dd.get("disp_normal", "")
            dd_path = Path(dd.get("path", "")) if dd.get("path") else None
            entry = _find_entry(catalog, dsn, actions.normalize_dsn)

            if disp_normal == "DELETE":
                if entry is not None:
                    catalog.remove(entry)
                    changed = True
                continue

            if disp_primary == "NEW" and entry is None and dd_path is not None and dd_path.exists():
                rel = str(dd_path.relative_to(BASE_DIR)) if str(dd_path).startswith(str(BASE_DIR)) else str(dd_path)
                catalog.append(
                    {
                        "dsn": dsn,
                        "path": rel,
                        "org": "PS",
                        "recfm": "FB",
                        "lrecl": 80,
                        "content_mode": "text",
                        "text_ccsid": "cp037",
                    }
                )
                changed = True

    if changed:
        actions.save_catalog(catalog)


def submit_jcl(actions: UtilityActions, jcl_dsn: str, jcl_member: str = "", owner: str = "") -> str:
    refresh_job_registry()

    norm_dsn = actions.normalize_dsn(jcl_dsn)
    member = str(jcl_member or "").strip().upper()
    owner_id = (owner or _DEFAULT_SUBMIT_OWNER or "UNKNOWN").strip().upper() or "UNKNOWN"

    if not norm_dsn:
        return "ENTER JCL DATA SET NAME"

    catalog = list(actions.load_catalog())
    entry = _find_entry(catalog, norm_dsn, actions.normalize_dsn)
    if entry is None:
        return f"DATA SET NOT FOUND: {norm_dsn}"

    jcl_text, load_error = _load_jcl_text(actions, entry, member)
    if load_error:
        return load_error

    steps = _parse_jcl_steps(jcl_text)
    if not steps:
        return "NO EXEC PGM STATEMENTS FOUND"

    dd_resolution_error = ""
    known_new_dsns = set()
    for idx, step in enumerate(steps):
        steps[idx], dd_error = _resolve_dd_paths(actions, catalog, step, known_new_dsns)
        if dd_error:
            dd_resolution_error = dd_error
            break

    if dd_resolution_error:
        return dd_resolution_error

    fallback_name = (member or norm_dsn.split(".")[-1] or "JOB")[:8]
    job_name = _parse_job_name(jcl_text, fallback_name)
    loadlib_path = _resolve_default_loadlib(actions)

    is_windows = platform.system().upper().startswith("WIN")
    for step in steps:
        pgm = step.get("pgm", "")
        pgm_path = _resolve_program_path(loadlib_path, pgm, is_windows)
        step["pgm_path"] = str(pgm_path) if pgm_path is not None else ""
        step["runtime_mode"] = _resolve_runtime_mode(pgm_path)

    script_text = _build_windows_script(steps) if is_windows else _build_bash_script(steps)
    job_id = _next_job_id()

    try:
        run_dir, pid, pgid = _start_job_async(
            job_id=job_id,
            job_name=job_name,
            owner=owner_id,
            script_text=script_text,
            is_windows=is_windows,
            source_dsn=norm_dsn,
            source_member=member,
            steps=steps,
            jcl_text=jcl_text,
            actions=actions,
            catalog_snapshot=catalog,
        )
    except Exception as e:
        return f"JOB EXECUTION FAILED: {e}"

    pgid_text = f" PGID={pgid}" if pgid is not None else ""
    return f"JOB {job_name} SUBMITTED ID={job_id} STATUS=ACTIVE PID={pid}{pgid_text} RUN={run_dir.name}"


def handle_jcl_submit(client_socket, actions: UtilityActions, layout: UtilityLayout) -> UtilityResult:
    option = "SUBMIT"
    jcl_dsn = ""
    jcl_member = ""
    msg = None

    while True:
        actions.send_ispf_jcl_submit(
            client_socket,
            option=option,
            jcl_dsn=jcl_dsn,
            jcl_member=jcl_member,
            short_msg=msg,
        )

        result = actions.read_client_input(client_socket)
        if result is None:
            return UtilityResult(message=None, disconnect=True)

        aid, cursor_addr, fields = result
        aid_str = actions.aid_to_string(aid)

        entered_opt = fields.get(layout.jcl_option_addr, "").strip().upper()
        entered_dsn = fields.get(layout.jcl_dsn_addr, "").strip().upper()
        entered_member = fields.get(layout.jcl_member_addr, "").strip().upper()

        jump_option = _extract_jump_option(entered_opt, entered_dsn, entered_member)
        if jump_option:
            return UtilityResult(message=None, jump_option=jump_option)

        if entered_opt:
            option = entered_opt
        if entered_dsn:
            jcl_dsn = entered_dsn
        if entered_member:
            jcl_member = entered_member

        if option == "X" or aid_str in ("PF3", "PF15"):
            return UtilityResult(message=None)

        # Tolerate emulator AID variation; treat any non-exit key as submit.
        aid_str = "Enter"

        submit_cmd = option.split()[0] if option else ""
        if submit_cmd not in {"SUBMIT", "S"}:
            msg = "ENTER SUBMIT (OR S), OR X TO EXIT"
            continue

        msg = submit_jcl(actions, jcl_dsn, jcl_member)
