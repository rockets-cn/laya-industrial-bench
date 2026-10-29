#!/usr/bin/env bash
# 主机单测: SortLogic 协议/状态机/HuskyLens 解析 (无需硬件)
set -euo pipefail
cd "$(dirname "$0")/../.."
g++ -std=c++17 -Wall -Wextra -O1 \
    -Ilib/SortLogic/src \
    tests/host/test_logic.cpp -o /tmp/edge101_host_test
/tmp/edge101_host_test
