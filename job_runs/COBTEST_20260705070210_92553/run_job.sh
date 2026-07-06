#!/usr/bin/env bash
set +e
RC=0
echo "JCL emulator runtime started"
echo "STEP STEP1 EXEC PGM=COBMOD"
if [[ -x '' ]]; then
  '' --dd 'SYSIN|GP5CRH.DATA|SHR||/Users/chrishunter/vscode-workspace/open-ispf/data/GP5CRH/DATA.dat'
  step_rc=$?
  echo "__STEP_RC__|STEP1|$step_rc"
  if [[ $step_rc -ne 0 ]]; then RC=$step_rc; fi
else
  echo "PGM NOT FOUND OR NOT EXECUTABLE: COBMOD"
  echo "__STEP_RC__|STEP1|12"
  RC=12
fi
echo "JCL emulator runtime complete RC=$RC"
exit $RC
