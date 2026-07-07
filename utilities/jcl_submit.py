import json
import os
import platform
import re
import shlex
import subprocess
from datetime import datetime
from pathlib import Path

from app_config import BASE_DIR, JOB_RUNS_DIR
from utilities.base import UtilityActions, UtilityLayout, UtilityResult


DEFAULT_RUNTIME_MODE = "native-argv"
ENV_RUNTIME_MODES = {"gnucobol-env", "dual"}
VALID_RUNTIME_MODES = {DEFAULT_RUNTIME_MODE, *ENV_RUNTIME_MODES}
COBOL_MODULE_SUFFIXES = {".so", ".dylib", ".dll"}


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


def _run_job(job_name: str, script_text: str, is_windows: bool) -> tuple[int, str, str, Path]:
    runs_root = JOB_RUNS_DIR
    runs_root.mkdir(parents=True, exist_ok=True)

    now = datetime.now().strftime("%Y%m%d%H%M%S")
    run_name = f"{job_name}_{now}_{os.getpid()}"
    run_dir = runs_root / run_name
    run_dir.mkdir(parents=True, exist_ok=True)

    script_name = "run_job.bat" if is_windows else "run_job.sh"
    script_path = run_dir / script_name
    script_path.write_text(script_text, encoding="utf-8")
    if not is_windows:
        script_path.chmod(0o755)

    cmd = ["cmd.exe", "/c", str(script_path)] if is_windows else ["/bin/bash", str(script_path)]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    return proc.returncode, proc.stdout or "", proc.stderr or "", run_dir


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


def _write_spool_files(run_dir: Path, job_name: str, rc: int, stdout: str, stderr: str, source_dsn: str, source_member: str, steps: list[dict]):
    (run_dir / "SYSOUT.log").write_text(stdout, encoding="utf-8")
    (run_dir / "SYSERR.log").write_text(stderr, encoding="utf-8")

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
    (run_dir / "JESMSGLG.log").write_text("\n".join(jes_lines) + "\n", encoding="utf-8")

    summary = {
        "job_name": job_name,
        "return_code": rc,
        "source_dsn": source_dsn,
        "source_member": source_member,
        "step_count": len(steps),
        "step_results": step_results,
        "run_dir": str(run_dir),
        "completed_at": datetime.now().isoformat(timespec="seconds"),
    }
    (run_dir / "JOBMETA.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")


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


def submit_jcl(actions: UtilityActions, jcl_dsn: str, jcl_member: str = "") -> str:
    norm_dsn = actions.normalize_dsn(jcl_dsn)
    member = str(jcl_member or "").strip().upper()

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

    try:
        rc, stdout, stderr, run_dir = _run_job(job_name, script_text, is_windows)
        _write_spool_files(run_dir, job_name, rc, stdout, stderr, norm_dsn, member, steps)
        if rc == 0:
            _sync_catalog_for_dispositions(actions, catalog, steps)
    except Exception as e:
        return f"JOB EXECUTION FAILED: {e}"

    return f"JOB {job_name} SUBMITTED - COMPLETE RC={rc} RUN={run_dir.name}"


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
