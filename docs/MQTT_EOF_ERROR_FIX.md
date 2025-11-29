# MQTT EOF Error After ws_start - Troubleshooting Guide

## Problem
After initiating `ws_start` via MQTT (via curl tests), the MQTT connection closes with an EOF error. The connection should remain persistent.

## Root Causes

### 1. Missing WebSocket Listener for MQTT
**Issue**: If clients are connecting via MQTT over WebSocket protocol, you need a separate WebSocket listener.

**Solution**: The updated `mosquitto.conf` now includes:
```
listener 9001 0.0.0.0
protocol websockets
```

**Testing**:
- Standard MQTT (TCP): Connect to port `1883`
- MQTT over WebSocket: Connect to port `9001`

### 2. Keepalive Configuration Issues
**Issue**: Default keepalive might be too short, causing connections to timeout.

**Solution**: Configure appropriate keepalive settings:
- In `mosquitto.conf`: `keepalive_interval 60`
- Client keepalive should match or be slightly less (e.g., 30-60 seconds)
- The server's `publish_ws_start` function uses `keepalive=30`, which is appropriate

### 3. Connection Timeout Settings
**Issue**: Mosquitto may be closing idle connections too aggressively.

**Solution**: The updated config includes:
- `connect_timeout 60` - Time to wait for CONNECT packet
- `max_connections -1` - Unlimited connections
- `max_queued_messages 1000` - Allow message queuing

### 4. Persistence Configuration
**Issue**: Without proper persistence, connections may not survive broker restarts.

**Solution**: Ensure persistence is enabled:
```
persistence true
persistence_location /mosquitto/data/
persistence_file mosquitto.db
autosave_interval 30
```

### 5. Client-Side Connection Management
**Issue**: The device's MQTT client may not be configured for persistent connections.

**Check**: In firmware (`mqtt_protocol.cc`), verify:
- Keepalive interval matches broker settings (default: 120 seconds)
- Client ID is persistent (not changing on each connection)
- Clean session flag is set appropriately

## Updated Configuration

The `mosquitto.conf` has been updated with comprehensive settings:

```conf
# MQTT TCP Listener (standard MQTT protocol)
listener 1883 0.0.0.0
protocol mqtt

# MQTT WebSocket Listener (for MQTT over WebSocket connections)
listener 9001 0.0.0.0
protocol websockets

# Authentication
allow_anonymous true

# Persistence Configuration
persistence true
persistence_location /mosquitto/data/
persistence_file mosquitto.db

# Connection Settings
keepalive_interval 60
max_connections -1
max_inflight_messages 20
max_queued_messages 1000
retained_persistence true
autosave_interval 30

# Logging
log_dest file /mosquitto/log/mosquitto.log
log_type all
log_timestamp true

# Connection timeout settings
connect_timeout 60
allow_zero_length_clientid true
max_packet_size 0
```

## Testing Persistent Connections

### Test 1: Standard MQTT TCP Connection
```bash
# Connect and subscribe
mosquitto_sub -h localhost -p 1883 -t "xiaozhi/AA:BB:CC:DD:EE:FF/down" -v

# In another terminal, publish ws_start
mosquitto_pub -h localhost -p 1883 -t "xiaozhi/AA:BB:CC:DD:EE:FF/down" \
  -m '{"type":"ws_start","wss":"ws://localhost:8000/xiaozhi/v1/","version":3}'

# Connection should remain open after publishing
```

### Test 2: MQTT over WebSocket
```bash
# Using wscat or similar WebSocket MQTT client
# Connect to ws://localhost:9001
# Send MQTT CONNECT packet
# Subscribe to topic
# Publish ws_start
# Verify connection persists
```

### Test 3: Using curl (if testing HTTP-like interface)
```bash
# Note: curl doesn't support MQTT protocol directly
# You may need to use a proper MQTT client library
# Or test via the server's HTTP endpoint that publishes ws_start
curl -X POST http://localhost:8003/alarm/ws_start \
  -H "Content-Type: application/json" \
  -d '{"device_id":"AA:BB:CC:DD:EE:FF"}'
```

## Common Issues and Solutions

### Issue: Connection closes immediately after ws_start
**Cause**: Server's `publish_ws_start` function disconnects its publishing client (this is normal - it's a temporary client).

**Solution**: The device should maintain its own persistent MQTT connection. The server's disconnect doesn't affect the device's connection.

### Issue: EOF error on curl test
**Cause**: If using curl to test MQTT, curl doesn't support MQTT protocol. You need a proper MQTT client.

**Solution**: Use `mosquitto_sub`/`mosquitto_pub` or a proper MQTT client library.

### Issue: Connection timeout after inactivity
**Cause**: Keepalive not working or too short.

**Solution**: 
1. Verify keepalive settings match between client and broker
2. Check network doesn't have intermediate NAT/firewall timeouts
3. Increase keepalive interval if needed

### Issue: Connection closes after broker restart
**Cause**: Persistence not properly configured or data directory not writable.

**Solution**:
1. Ensure `/mosquitto/data/` directory exists and is writable
2. Check `mosquitto.log` for persistence errors
3. Verify `persistence true` is set

## Verification Steps

1. **Check Mosquitto is running**:
   ```bash
   mosquitto -c mosquitto.conf -v
   ```

2. **Monitor connections**:
   ```bash
   # Watch mosquitto log
   tail -f /mosquitto/log/mosquitto.log
   ```

3. **Test persistent connection**:
   ```bash
   # Start a persistent subscriber
   mosquitto_sub -h localhost -p 1883 \
     -t "xiaozhi/+/down" \
     -i "test-client" \
     -c  # Clean session = false (persistent)
   ```

4. **Publish ws_start and verify**:
   - Connection should remain open
   - No EOF errors in logs
   - Device receives message and opens WebSocket
   - MQTT connection remains active

## Architecture Notes

The system uses **dual-protocol architecture**:
- **MQTT**: Persistent connection for control messages (ws_start, wake word, etc.)
- **WebSocket**: On-demand connection for audio conversations

After `ws_start`:
- Device opens WebSocket connection for audio
- **MQTT connection should remain open** for future control messages
- Both connections can be active simultaneously

## Additional Debugging

If EOF errors persist:

1. **Check mosquitto logs**:
   ```bash
   tail -f /mosquitto/log/mosquitto.log | grep -i "error\|disconnect\|eof"
   ```

2. **Verify network connectivity**:
   ```bash
   # Test if port is accessible
   telnet localhost 1883
   telnet localhost 9001
   ```

3. **Check for firewall/NAT issues**:
   - Intermediate NAT may timeout idle connections
   - Consider reducing keepalive or using TCP keepalive

4. **Monitor connection state**:
   ```bash
   # Use mosquitto's status monitoring
   mosquitto_sub -h localhost -p 1883 -t '$SYS/#' -v
   ```

## Summary

The EOF error is likely caused by:
1. ✅ **Fixed**: Missing WebSocket listener (added port 9001)
2. ✅ **Fixed**: Incomplete persistence configuration (now comprehensive)
3. ✅ **Fixed**: Missing keepalive/timeout settings (now configured)
4. ⚠️ **Verify**: Client-side keepalive matches broker settings
5. ⚠️ **Verify**: Network doesn't have intermediate timeouts

The updated `mosquitto.conf` should resolve most issues. If problems persist, check the client-side MQTT configuration in the firmware.

