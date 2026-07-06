#!/usr/bin/env bash
set +e
RC=0
echo "JCL emulator runtime started"
echo "STEP STEP1 EXEC PGM=COBMOD"
if command -v cobcrun >/dev/null 2>&1 && [[ -f /Users/chrishunter/vscode-workspace/open-ispf/data/SYS1/LOADLIB/COBMOD.so ]]; then
  COB_LIBRARY_PATH=/Users/chrishunter/vscode-workspace/open-ispf/data/SYS1/LOADLIB SYSIN=/Users/chrishunter/vscode-workspace/open-ispf/data/GP5CRH/DATA.dat cobcrun COBMOD --dd 'SYSIN|GP5CRH.DATA|SHR||/Users/chrishunter/vscode-workspace/open-ispf/data/GP5CRH/DATA.dat'
  step_rc=$?
  echo "__STEP_RC__|STEP1|$step_rc"
  if [[ $step_rc -ne 0 ]]; then RC=$step_rc; fi
else
  echo "COBCRUN NOT FOUND OR MODULE MISSING: COBMOD"
  echo "__STEP_RC__|STEP1|12"
  RC=12
fi
echo "JCL emulator runtime complete RC=$RC"
exit $RC
