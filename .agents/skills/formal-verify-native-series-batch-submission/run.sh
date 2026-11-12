#!/bin/sh
set -eu
skill_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
: "${TLA_JAVA:?Set TLA_JAVA to a Java 21 executable}"
: "${TLA_JAR:?Set TLA_JAR to tla2tools.jar v1.8.0}"
state_dir=$(mktemp -d)
trap 'rm -rf "$state_dir"' EXIT
"$TLA_JAVA" -XX:+UseParallelGC -cp "$TLA_JAR" tlc2.TLC -workers 1 -metadir "$state_dir" "$skill_dir/Submission.tla"
