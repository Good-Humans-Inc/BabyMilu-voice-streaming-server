# Quick Test Commands for sherpa-onnx Build Issue

## Root Cause Analysis ✅

**Confirmed:** `sherpa-onnx==1.12.11` **does NOT exist** on PyPI or Aliyun mirror.

**Available versions in 1.12.x series:**
- 1.12.15, 1.12.17, 1.12.18, 1.12.19, 1.12.20, 1.12.21, 1.12.22, 1.12.23

**This is why your Docker build fails** - pip cannot find version 1.12.11.

---

## Commands to Test Locally (Outside Docker)

### 1. Check available versions from your machine:
```bash
pip index versions sherpa-onnx --index-url https://mirrors.aliyun.com/pypi/simple/ --trusted-host mirrors.aliyun.com
```

### 2. Test if 1.12.11 can be installed (will fail):
```bash
pip install --dry-run sherpa-onnx==1.12.11
# Expected: ERROR: Could not find a version that satisfies the requirement
```

### 3. Test if 1.12.15 can be installed (should work):
```bash
pip install --dry-run sherpa-onnx==1.12.15
# Expected: Would install sherpa-onnx-1.12.15 sherpa-onnx-core-1.12.15
```

---

## Commands to Test in Docker Environment

### 1. Quick test in Python 3.10 container (matches Dockerfile):
```bash
docker run --rm python:3.10-slim bash -c '
  pip config set global.index-url https://mirrors.aliyun.com/pypi/simple/ && \
  pip config set global.trusted-host mirrors.aliyun.com && \
  pip install --upgrade pip setuptools wheel && \
  pip index versions sherpa-onnx'
```

### 2. Test installing 1.12.11 in container (will fail):
```bash
docker run --rm python:3.10-slim bash -c '
  pip config set global.index-url https://mirrors.aliyun.com/pypi/simple/ && \
  pip config set global.trusted-host mirrors.aliyun.com && \
  pip install --upgrade pip setuptools wheel && \
  pip install --verbose sherpa-onnx==1.12.11 2>&1 | tail -20'
```

### 3. Test installing 1.12.15 in container (should work):
```bash
docker run --rm python:3.10-slim bash -c '
  pip config set global.index-url https://mirrors.aliyun.com/pypi/simple/ && \
  pip config set global.trusted-host mirrors.aliyun.com && \
  pip install --upgrade pip setuptools wheel && \
  pip install --verbose sherpa-onnx==1.12.15 2>&1 | tail -20'
```

### 4. Full Docker build test (with detailed logs):
```bash
cd /Users/yan/Desktop/BabyMilu/BabyMilu-voice-streaming-server
docker build -f Dockerfile-server --progress=plain --no-cache -t test-sherpa-build . 2>&1 | tee docker-build.log
```

---

## Solution: You Must Upgrade to 1.12.15+

Since 1.12.11 doesn't exist, you have two options:

### Option A: Use 1.12.15 (earliest available 1.12.x)
```bash
# Update requirements.txt line 24:
sherpa_onnx==1.12.15
```

### Option B: Use latest 1.12.x version
```bash
# Update requirements.txt line 24:
sherpa_onnx>=1.12.15,<1.13
# This will install the latest 1.12.x (currently 1.12.23)
```

---

## Logging Issue After Upgrade

**Problem:** After upgrading to 1.12.15+, initialization logs disappear.

**Likely cause:** sherpa-onnx 1.12.15+ may write to `stderr` instead of `stdout`, or uses internal logging.

**Current CaptureOutput only captures `stdout`** - see `core/providers/asr/sherpa_onnx_local.py` line 20-34.

**To fix logging issue:** Update `CaptureOutput` class to also capture `stderr`, or investigate if sherpa-onnx has a `debug=True` flag or verbose mode.

---

## Recommendation

1. **Immediate fix:** Update `requirements.txt` to use `sherpa_onnx==1.12.15` or `sherpa_onnx>=1.12.15,<1.13`
2. **For logging:** After upgrade, test and see if logs appear. If not, we can update `CaptureOutput` to capture both stdout and stderr.
