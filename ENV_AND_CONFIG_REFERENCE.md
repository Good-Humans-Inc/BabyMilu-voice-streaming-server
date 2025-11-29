# Environment Variables & Configuration Reference

## ⚠️ Files NOT in GitHub (Must Create Locally)

This document lists all configuration files and environment variables that contain secrets or are environment-specific and should NOT be committed to GitHub.

---

## 1. Python Server Configuration

### File: `main/xiaozhi-server/data/.config.yaml`

**Status**: ⚠️ **NOT in GitHub** - Contains API keys and secrets

**Location**: `main/xiaozhi-server/data/.config.yaml`

**How to Create**:
```bash
cd main/xiaozhi-server
mkdir -p data
cp config.yaml data/.config.yaml
# Edit with your API keys
```

**Required Configuration** (Minimum):
```yaml
selected_module:
  LLM: ChatGLMLLM
  ASR: FunASR
  TTS: EdgeTTS

LLM:
  ChatGLMLLM:
    api_key: "your-chatglm-api-key-here"  # ⚠️ REQUIRED
```

**All Possible Secrets in This File**:
- `LLM.*.api_key`: LLM provider API keys
- `TTS.*.api_key`, `access_token`, `appid`: TTS provider credentials
- `ASR.*.api_key`, `access_token`, `appid`: ASR provider credentials
- `plugins.get_weather.api_key`: Weather API key
- `plugins.home_assistant.api_key`: Home Assistant token
- `manager-api.secret`: Manager API secret (if using manager-api)
- `manager-api.url`: Manager API URL (if using manager-api)
- `mcp_endpoint`: MCP endpoint URL with token
- `voiceprint.url`: Voiceprint service URL (if external)

**Environment Variables** (Optional - can use config file instead):
```bash
export GOOGLE_APPLICATION_CREDENTIALS="/path/to/gcp/sa.json"
export GOOGLE_CLOUD_PROJECT="your-project-id"
export MQTT_URL="mqtt://localhost:1883"
export TZ="Asia/Shanghai"
export PYTHONUNBUFFERED=1
```

---

## 2. Java Backend Configuration

### File: `main/manager-api/src/main/resources/application-dev.yml`

**Status**: ⚠️ **NOT in GitHub** - Contains database credentials

**Location**: `main/manager-api/src/main/resources/application-dev.yml`

**Required Configuration**:
```yaml
spring:
  datasource:
    druid:
      url: jdbc:mysql://127.0.0.1:3306/xiaozhi_esp32_server?useUnicode=true&characterEncoding=UTF-8&serverTimezone=Asia/Shanghai
      username: root                    # ⚠️ CHANGE THIS
      password: your-db-password        # ⚠️ CHANGE THIS
  data:
    redis:
      host: 127.0.0.1
      port: 6379
      password: your-redis-password      # ⚠️ SET IF REDIS HAS PASSWORD
      database: 0
```

**Environment Variables** (Docker/Production):
```bash
SPRING_DATASOURCE_DRUID_URL=jdbc:mysql://host:3306/xiaozhi_esp32_server?...
SPRING_DATASOURCE_DRUID_USERNAME=root
SPRING_DATASOURCE_DRUID_PASSWORD=your-password
SPRING_DATA_REDIS_HOST=localhost
SPRING_DATA_REDIS_PASSWORD=
SPRING_DATA_REDIS_PORT=6379
TZ=Asia/Shanghai
```

**Additional Secrets** (if configured):
- Aliyun SMS credentials (in `application-dev.yml`)
- Shiro encryption keys
- JWT signing keys

---

## 3. Web Frontend Configuration

### File: `main/manager-web/.env.development`

**Status**: ⚠️ **May not be in GitHub**

**Location**: `main/manager-web/.env.development`

**Required Configuration**:
```env
VUE_APP_API_BASE_URL=http://localhost:8002
```

**Production File**: `.env.production`
```env
VUE_APP_API_BASE_URL=https://your-production-api.com
```

---

## 4. Mobile App Configuration

### File: `main/manager-mobile/env/.env.development`

**Status**: ⚠️ **NOT in GitHub**

**Location**: `main/manager-mobile/env/.env.development`

**Required Configuration**:
```env
VITE_APP_TITLE=小智
VITE_FALLBACK_LOCALE=zh-Hans
VITE_UNI_APPID=your-uni-app-id          # ⚠️ REQUIRED for App
VITE_WX_APPID=your-wechat-appid         # ⚠️ REQUIRED for WeChat Mini Program
VITE_SERVER_BASEURL=http://localhost:8002
VITE_DELETE_CONSOLE=false
VITE_SHOW_SOURCEMAP=false
VITE_LOGIN_URL=/pages/login/login
```

**Production File**: `env/.env.production`
```env
VITE_SERVER_BASEURL=https://your-production-api.com
```

---

## 5. Google Cloud Platform (Optional)

### File: `main/xiaozhi-server/data/.gcp/sa.json`

**Status**: ⚠️ **NOT in GitHub** - Contains GCP service account credentials

**Location**: `main/xiaozhi-server/data/.gcp/sa.json`

**When Needed**: If using Firestore for device profiles

**How to Create**:
1. Create service account in GCP Console
2. Download JSON key file
3. Place at `data/.gcp/sa.json`

**Alternative**: Use environment variable:
```bash
export GOOGLE_APPLICATION_CREDENTIALS="/path/to/sa.json"
export GOOGLE_CLOUD_PROJECT="your-project-id"
```

---

## 6. Model Files

### Directory: `main/xiaozhi-server/models/`

**Status**: ⚠️ **NOT in GitHub** - Large binary files

**Required Models**:
- `SenseVoiceSmall/model.pt`: FunASR speech recognition model
- `snakers4_silero-vad/`: VAD model files

**Download Instructions**:
See deployment documentation or:
```bash
# FunASR model download (example)
# Check docs/Deployment.md for official download links
```

---

## 7. OTA Manifest

### File: `OTA/ota_manifest.json`

**Status**: ⚠️ **May contain device-specific info**

**Location**: `OTA/ota_manifest.json`

**Template**: `OTA/ota_manifest.example.json` (in GitHub)

**Example**:
```json
{
  "version": "1.0.0",
  "firmware_url": "https://your-server.com/firmware.bin",
  "release_notes": "Update description"
}
```

---

## 8. Docker Environment Variables

### File: `docker-compose.override.yml` (Optional)

**Status**: ⚠️ **NOT in GitHub** - Local overrides

**Purpose**: Override docker-compose settings locally

**Example**:
```yaml
services:
  xiaozhi-esp32-server:
    environment:
      - GOOGLE_APPLICATION_CREDENTIALS=/run/secrets/gcp_sa.json
      - MQTT_URL=mqtt://mqtt:1883
      - TZ=America/Los_Angeles
    volumes:
      - ./data/.gcp/sa.json:/run/secrets/gcp_sa.json:ro
```

---

## Configuration Priority

### Python Server
1. Environment variables (highest priority)
2. `data/.config.yaml` (user config)
3. `config.yaml` (default template)
4. API configuration (if `read_config_from_api: true`)

### Java Backend
1. Environment variables
2. `application-{profile}.yml` (dev, prod, etc.)
3. `application.yml` (base config)

### Frontend
1. `.env.production` (production build)
2. `.env.development` (development)
3. `.env` (base)

---

## Security Checklist

Before committing code, ensure:

- [ ] `data/.config.yaml` is in `.gitignore`
- [ ] `application-dev.yml` with credentials is in `.gitignore`
- [ ] `.env*` files are in `.gitignore`
- [ ] `*.json` files with API keys are in `.gitignore`
- [ ] GCP service account files are in `.gitignore`
- [ ] Model files are in `.gitignore` (if large)
- [ ] No hardcoded API keys in source code
- [ ] No database passwords in committed files

---

## Quick Setup Commands

### Python Server
```bash
cd main/xiaozhi-server
mkdir -p data
cp config.yaml data/.config.yaml
# Edit data/.config.yaml with your API keys
```

### Java Backend
```bash
cd main/manager-api/src/main/resources
# Create or edit application-dev.yml
# Add database credentials
```

### Frontend
```bash
cd main/manager-web
# Create .env.development
echo "VUE_APP_API_BASE_URL=http://localhost:8002" > .env.development
```

### Mobile App
```bash
cd main/manager-mobile
mkdir -p env
# Create env/.env.development
# Add required variables
```

---

## Environment-Specific Notes

### Development
- Use `application-dev.yml` for Java backend
- Use `.env.development` for frontends
- Local database: `localhost:3306`
- Local Redis: `localhost:6379`

### Production
- Use environment variables or `application-prod.yml`
- Use `.env.production` for frontends
- Secure database credentials
- Enable authentication
- Use HTTPS/WSS for WebSocket

### Docker
- Use environment variables in `docker-compose.yml`
- Mount secrets as volumes (read-only)
- Use Docker secrets for sensitive data

---

## Troubleshooting

### "Config file not found"
- Check `data/.config.yaml` exists
- Verify path is correct
- Check file permissions

### "API key invalid"
- Verify key is correct (no extra spaces)
- Check key hasn't expired
- Verify provider account is active

### "Database connection failed"
- Check `application-dev.yml` credentials
- Verify MySQL is running
- Check network connectivity
- Verify database exists

### "Redis connection failed"
- Check Redis is running
- Verify host/port in config
- Check password if set

---

**Last Updated**: Based on codebase analysis
**Maintained By**: Development Team
