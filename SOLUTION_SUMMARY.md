# Solution Summary: sherpa-onnx Version Issue

## Situation Analysis

1. **Version 1.12.11 exists** on HuggingFace ([commit de2e8bc](https://huggingface.co/csukuangfj/sherpa-onnx-libs/commit/de2e8bc33c721656f6840e5949e948a09790df2b)) but **NOT on PyPI**
2. **Docker build uses Aliyun mirror** which doesn't have 1.12.11
3. **Current requirements.txt has 1.12.17** (already upgraded!)
4. **Logging issue** after upgrade to 1.12.15+ (logs disappear)

---

## Best Solution: Upgrade + Fix Logging

Since you're already on `1.12.17`, the build works. The remaining issue is **logging**.

### Option A: Try Debug Mode First (Simplest)

The `sherpa_onnx` initialization might output more logs with `debug=True`. Test this:

**In `core/providers/asr/sherpa_onnx_local.py` lines 85 and 95:**

```python
# Change debug=False to debug=True
debug=True,  # Try enabling this
```

This might restore the initialization logs that disappeared.

---

### Option B: Capture Both stdout and stderr (More Comprehensive)

If `debug=True` doesn't help, update `CaptureOutput` to capture both streams:

**In `core/providers/asr/sherpa_onnx_local.py`:**

```python
# 捕获标准输出和标准错误
class CaptureOutput:
    def __enter__(self):
        self._output = io.StringIO()
        self._error_output = io.StringIO()
        self._original_stdout = sys.stdout
        self._original_stderr = sys.stderr
        sys.stdout = self._output
        sys.stderr = self._error_output

    def __exit__(self, exc_type, exc_value, traceback):
        sys.stdout = self._original_stdout
        sys.stderr = self._original_stderr
        self.output = self._output.getvalue()
        self.error_output = self._error_output.getvalue()
        self._output.close()
        self._error_output.close()

        # 将捕获到的内容通过 logger 输出
        if self.output:
            logger.bind(tag=TAG).info(f"[stdout] {self.output.strip()}")
        if self.error_output:
            logger.bind(tag=TAG).info(f"[stderr] {self.error_output.strip()}")
```

---

### Option C: Use PyPI Directly (If Network Allows)

If you must use 1.12.11 (though not recommended), modify Dockerfile to try PyPI as fallback:

**In `Dockerfile-server`**, modify the pip install step:

```dockerfile
# Try Aliyun mirror first, fallback to PyPI
RUN pip config set global.index-url https://mirrors.aliyun.com/pypi/simple/ && \
    pip config set global.trusted-host mirrors.aliyun.com && \
    pip install --no-cache-dir --upgrade pip setuptools wheel && \
    (pip install --no-cache-dir -r requirements.txt --default-timeout=120 --retries 5 || \
     pip install --no-cache-dir -r requirements.txt --index-url https://pypi.org/simple/ --default-timeout=120 --retries 5)
```

**However, this won't help for 1.12.11** since it's not on PyPI either - it's only on HuggingFace as an Android .aar file.

---

## Recommended Action Plan

1. ✅ **Keep `sherpa_onnx==1.12.17`** (or `>=1.12.15,<1.13` for flexibility)
2. **Test `debug=True`** first - quickest fix if it works
3. **If debug doesn't help, implement Option B** (capture both stdout/stderr)
4. **Test thoroughly** to ensure logs appear correctly

---

## Why 1.12.11 Isn't Available

- **HuggingFace**: Has Android `.aar` file (for mobile apps) - [see commit](https://huggingface.co/csukuangfj/sherpa-onnx-libs/commit/de2e8bc33c721656f6840e5949e948a09790df2b)
- **PyPI**: Doesn't have Python wheels for 1.12.11
- **Aliyun Mirror**: Mirrors PyPI, so also doesn't have it

The version exists for Android but not for Python/PyPI. Your Docker build needs Python packages from PyPI, so 1.12.11 isn't accessible.

---

## Testing Commands

### Test current version (1.12.17) with debug mode:
```python
# In Python, test initialization
import sherpa_onnx
model = sherpa_onnx.OfflineRecognizer.from_sense_voice(
    ...,
    debug=True  # Test with this
)
```

### Verify logs appear:
```bash
# Run your application and check logs
# Look for initialization messages from sherpa_onnx
```
