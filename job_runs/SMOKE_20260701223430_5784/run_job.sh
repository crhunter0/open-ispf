#!/usr/bin/env bash
set +e
RC=0
echo "JCL emulator runtime started"
echo "STEP STEP1 EXEC PGM=IEFBR14"
if [[ -x /Users/chrishunter/vscode-workspace/open-ispf/data/SYS1/LOADLIB/IEFBR14 ]]; then
  /Users/chrishunter/vscode-workspace/open-ispf/data/SYS1/LOADLIB/IEFBR14 --dd 'DD1|GP5CRH.SMOKETST|NEW||/Users/chrishunter/vscode-workspace/open-ispf/data/GP5CRH/SMOKETST.dat'
  step_rc=$?
  if [[ $step_rc -ne 0 ]]; then RC=$step_rc; fi
else
  echo "PGM NOT FOUND OR NOT EXECUTABLE: IEFBR14"
  RC=12
fi
echo "JCL emulator runtime complete RC=$RC"
exit $RC
