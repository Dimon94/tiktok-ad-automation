#!/bin/sh
set -eu
skill_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
: "${TLA_JAVA:?Set TLA_JAVA to Java 21}"
: "${TLA_JAR:?Set TLA_JAR to pinned tla2tools.jar}"
python3 "$skill_dir/models.py"
if [ "${1:-}" = "--all" ]; then
  : "${TEST_RUNNER:?Set TEST_RUNNER to the isolated M2 run-test.py}"
  : "${FORMAL_OUT:?Set FORMAL_OUT to an external evidence directory}"
  python3 "$skill_dir/source_checks.py" --runner "$TEST_RUNNER" --out "$FORMAL_OUT/source"
fi
