# Device Connection Log Issue - Root Cause & Fix

## Root Cause

**Problem:** Logs disappear when connecting with real devices, but work fine with testing frontend.

**Root Cause:** `_initialize_components()` runs in a **ThreadPoolExecutor** (connection.py:510). When ASR initialization happens in that thread pool thread (line 697), the `CaptureOutput()` context manager attempts to capture `sys.stdout`/`sys.stderr`, but **Python threads don't share `sys.stdout`/`sys.stderr` references the same way**.

### Technical Details:

1. Connection setup calls `_initialize_components()` via ThreadPoolExecutor (line 510)
2. ASR initialization (`sherpa_onnx_local.py`) happens inside that thread (line 697: `self.asr = self._initialize_asr()`)
3. `CaptureOutput()` redirects `sys.stdout`/`sys.stderr` in that thread
4. **BUT**: In thread pool threads, stdout/stderr redirection may not work correctly because:
   - Each thread may have its own view of `sys.stdout`/`sys.stderr`
   - Logging frameworks may bypass stdout/stderr in worker threads
   - Buffering behavior differs in thread contexts

### Why Frontend Works But Device Doesn't:

- **Frontend connections**: May initialize components differently or synchronously
- **Device connections**: Use `_initialize_private_config()` → `initialize_modules()` → ThreadPoolExecutor → `_initialize_components()` → ASR init with stdout capture

## Solution

### Option 1: Initialize ASR Before Thread Pool (Recommended)

Move ASR initialization outside the thread pool, before `_initialize_components()` is submitted.

**Modify `connection.py` around line 488-510:**

```python
# Get private config
self._initialize_private_config()

# Initialize ASR BEFORE submitting to thread pool (if not already initialized)
if self.asr is None and self._asr.interface_type == InterfaceType.LOCAL:
    # Initialize ASR synchronously in main thread where stdout/stderr capture works
    self.asr = self._initialize_asr()
    logger.bind(tag=TAG).info("ASR initialized in main thread")

# Then submit rest of component initialization to thread pool
self.executor.submit(self._initialize_components)
```

And modify `_initialize_components()` to skip ASR init if already done:

```python
def _initialize_components(self):
    try:
        self.selected_module_str = build_module_string(
            self.config.get("selected_module", {})
        )
        self.logger = create_connection_logger(self.selected_module_str)

        # ... prompt code ...

        if self.vad is None:
            self.vad = self._vad
        # Skip ASR if already initialized
        if self.asr is None:
            self.asr = self._initialize_asr()
        # ... rest of initialization ...
```

### Option 2: Fix CaptureOutput for Thread Context

Ensure `CaptureOutput` works correctly in thread pool contexts by using thread-local storage or ensuring proper redirection.

**However**, this is more complex and may not fully solve the issue if sherpa-onnx writes to different streams in thread contexts.

### Option 3: Use Logger Directly Instead of Capturing stdout/stderr

Instead of capturing stdout/stderr, check if sherpa-onnx has logging hooks or try enabling debug mode to see if logs appear through the logger framework.

**In `sherpa_onnx_local.py`, try:**

```python
with CaptureOutput():
    # ... existing code ...
    debug=True,  # Try enabling debug mode
```

But this may not help if the issue is thread-based stdout capture.

## Recommended Fix

**Use Option 1** - Initialize ASR in the main thread before submitting other components to the thread pool. This ensures stdout/stderr capture works correctly.

## Testing

After implementing the fix:

1. Connect with a real device
2. Check logs for sherpa-onnx initialization messages
3. Verify logs appear with `[stdout]` or `[stderr]` prefixes from `CaptureOutput`

## Related Code Locations

- **connection.py**: Line 488 (`_initialize_private_config()`), Line 510 (`executor.submit`)
- **connection.py**: Line 679 (`_initialize_components()`), Line 697 (`self.asr = self._initialize_asr()`)
- **sherpa_onnx_local.py**: Line 20-42 (`CaptureOutput` class), Line 84 (`with CaptureOutput()`)
