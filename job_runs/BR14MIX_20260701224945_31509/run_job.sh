#!/usr/bin/env bash
set +e
RC=0
echo "JCL emulator runtime started"
echo "STEP STEP1 EXEC PGM=IEFBR14"
if [[ -x /Users/chrishunter/vscode-workspace/open-ispf/data/SYS1/LOADLIB/IEFBR14 ]]; then
  /Users/chrishunter/vscode-workspace/open-ispf/data/SYS1/LOADLIB/IEFBR14 --dd 'IN1|GP5CRH.DATA|SHR||/Users/chrishunter/vscode-workspace/open-ispf/data/GP5CRH/DATA.dat' --dd 'NEW1|GP5CRH.BR14MIX.OUT|NEW|CATLG|/Users/chrishunter/vscode-workspace/open-ispf/data/GP5CRH/BR14MIX_OUT.dat'
  step_rc=$?
  echo "__STEP_RC__|STEP1|$step_rc"
  if [[ $step_rc -ne 0 ]]; then RC=$step_rc; fi
else
  echo "PGM NOT FOUND OR NOT EXECUTABLE: IEFBR14"
  echo "__STEP_RC__|STEP1|12"
  RC=12
fi
echo "STEP STEP2 EXEC PGM=IEFBR14"
if [[ -x /Users/chrishunter/vscode-workspace/open-ispf/data/SYS1/LOADLIB/IEFBR14 ]]; then
  /Users/chrishunter/vscode-workspace/open-ispf/data/SYS1/LOADLIB/IEFBR14 --dd 'DEL1|GP5CRH.BR14MIX.OUT|OLD|DELETE|/Users/chrishunter/vscode-workspace/open-ispf/data/GP5CRH/BR14MIX_OUT.dat'
  step_rc=$?
  echo "__STEP_RC__|STEP2|$step_rc"
  if [[ $step_rc -ne 0 ]]; then RC=$step_rc; fi
else
  echo "PGM NOT FOUND OR NOT EXECUTABLE: IEFBR14"
  echo "__STEP_RC__|STEP2|12"
  RC=12
fi
echo "JCL emulator runtime complete RC=$RC"
exit $RC
