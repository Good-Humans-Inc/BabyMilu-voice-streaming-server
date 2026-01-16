#!/bin/bash
# Test script to diagnose sherpa-onnx installation issues

set -e

echo "=== Testing sherpa-onnx version availability ==="
echo ""

echo "1. Testing with default PyPI index..."
pip index versions sherpa-onnx 2>&1 | grep -A 20 "Available versions" || echo "Command failed or different output format"

echo ""
echo "2. Testing with Aliyun mirror (used in Docker)..."
pip index versions sherpa-onnx --index-url https://mirrors.aliyun.com/pypi/simple/ --trusted-host mirrors.aliyun.com 2>&1 | grep -A 20 "Available versions" || echo "Command failed or different output format"

echo ""
echo "3. Testing if 1.12.11 can be downloaded from PyPI..."
pip download --no-deps sherpa-onnx==1.12.11 --dry-run 2>&1 | head -10 || echo "Failed as expected - version doesn't exist"

echo ""
echo "4. Testing if 1.12.15 can be downloaded from PyPI..."
pip download --no-deps sherpa-onnx==1.12.15 --dry-run 2>&1 | head -10 || echo "Failed"

echo ""
echo "5. Testing if 1.12.11 can be downloaded from Aliyun mirror..."
pip download --no-deps sherpa-onnx==1.12.11 --index-url https://mirrors.aliyun.com/pypi/simple/ --trusted-host mirrors.aliyun.com --dry-run 2>&1 | head -10 || echo "Failed as expected - version doesn't exist"

echo ""
echo "6. Testing if 1.12.15 can be downloaded from Aliyun mirror..."
pip download --no-deps sherpa-onnx==1.12.15 --index-url https://mirrors.aliyun.com/pypi/simple/ --trusted-host mirrors.aliyun.com --dry-run 2>&1 | head -10 || echo "Failed"

echo ""
echo "=== Summary ==="
echo "If 1.12.11 fails but 1.12.15 succeeds, the version simply doesn't exist."
echo "The Docker build will fail because pip cannot find sherpa-onnx==1.12.11"
