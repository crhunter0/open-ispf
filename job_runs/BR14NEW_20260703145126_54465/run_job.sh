#!/usr/bin/env bash
set +e
RC=0
echo "JCL emulator runtime started"
echo "STEP STEP1 EXEC PGM=IEFBR14"
if [[ -x /Users/chrishunter/vscode-workspace/open-ispf/data/SYS1/LOADLIB/IEFBR14 ]]; then
  /Users/chrishunter/vscode-workspace/open-ispf/data/SYS1/LOADLIB/IEFBR14 --dd 'DD1|GP5CRH.BR14NEW.TEST|NEW|CATLG|/Users/chrishunter/vscode-workspace/open-ispf/data/GP5CRH/BR14NEW_TEST.dat'
  step_rc=$?
  echo "__STEP_RC__|STEP1|$step_rc"
  if [[ $step_rc -ne 0 ]]; then RC=$step_rc; fi
else
  echo "PGM NOT FOUND OR NOT EXECUTABLE: IEFBR14"
  echo "__STEP_RC__|STEP1|12"
  RC=12
fi
echo "JCL emulator runtime complete RC=$RC"
exit $RC
