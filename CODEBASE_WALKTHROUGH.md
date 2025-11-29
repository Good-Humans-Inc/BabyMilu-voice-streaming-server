# BabyMilu Voice Streaming Server - Codebase Walkthrough

## Table of Contents
1. [System Overview](#system-overview)
2. [Architecture](#architecture)
3. [Project Structure](#project-structure)
4. [Core Components](#core-components)
5. [Configuration Files](#configuration-files)
6. [Environment Variables & Secrets](#environment-variables--secrets)
7. [Key Modules Deep Dive](#key-modules-deep-dive)
8. [Deployment Architecture](#deployment-architecture)
9. [Important Files Not in GitHub](#important-files-not-in-github)

---

## System Overview

The **BabyMilu Voice Streaming Server** (also known as `xiaozhi-esp32-server`) is a comprehensive backend service system for intelligent voice interaction devices. It provides:

- **Real-time voice processing**: ASR (Automatic Speech Recognition), TTS (Text-to-Speech), VAD (Voice Activity Detection)
- **AI-powered conversations**: Integration with multiple LLM providers (ChatGLM, Doubao, DeepSeek, etc.)
- **Multi-modal capabilities**: Vision language models (VLLM) for image understanding
- **Device management**: OTA updates, device binding, configuration management
- **Plugin system**: Extensible function calling and intent recognition
- **Memory system**: Conversation history and context management

### Technology Stack
- **Python 3.10+**: Main server (`xiaozhi-server`)
- **Java 21 + Spring Boot 3.4.3**: Management API (`manager-api`)
- **Vue.js**: Web management console (`manager-web`)
- **uni-app + Vue 3**: Mobile management app (`manager-mobile`)
- **WebSocket**: Real-time bidirectional communication
- **MQTT**: Device messaging (optional)
- **MySQL**: Database for full module deployment
- **Redis**: Caching and session management

---

## Architecture

### High-Level Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                    ESP32 Hardware Device                     │
│              (BabyMilu Voice Interaction Device)             │
└──────────────────────┬──────────────────────────────────────┘
                       │ WebSocket (ws://server:8000/xiaozhi/v1/)
                       │
┌──────────────────────▼──────────────────────────────────────┐
│              xiaozhi-server (Python)                          │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐       │
│  │ WebSocket    │  │ HTTP Server  │  │ Connection   │       │
│  │ Server       │  │ (OTA/Vision) │  │ Handler      │       │
│  └──────┬───────┘  └──────┬───────┘  └──────┬───────┘       │
│         │                 │                  │               │
│  ┌──────▼─────────────────▼──────────────────▼───────┐     │
│  │         Core Processing Pipeline                   │     │
│  │  VAD → ASR → Intent → LLM → Memory → TTS        │     │
│  └───────────────────────────────────────────────────┘     │
└──────────────────────┬──────────────────────────────────────┘
                       │
                       │ HTTP API (if using manager-api)
                       │
┌──────────────────────▼──────────────────────────────────────┐
│              manager-api (Java Spring Boot)                   │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐       │
│  │ User Mgmt    │  │ Device Mgmt  │  │ Config Mgmt  │       │
│  └──────────────┘  └──────────────┘  └──────────────┘       │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐       │
│  │ OTA Mgmt     │  │ Voiceprint   │  │ Agent Config │       │
│  └──────────────┘  └──────────────┘  └──────────────┘       │
└──────────────────────┬──────────────────────────────────────┘
                       │
                       │ HTTP API
                       │
┌──────────────────────▼──────────────────────────────────────┐
│              manager-web (Vue.js)                             │
│              manager-mobile (uni-app)                         │
│              Web & Mobile Management Consoles                 │
└──────────────────────────────────────────────────────────────┘
```

### Communication Flow

1. **Device Connection**: ESP32 device connects via WebSocket to `xiaozhi-server`
2. **Audio Streaming**: Device sends Opus-encoded audio frames
3. **Processing Pipeline**:
   - VAD detects voice activity
   - ASR converts speech to text
   - Intent recognition determines user intent
   - LLM generates response
   - TTS converts text to speech
   - Audio sent back to device
4. **Management**: Web/mobile consoles communicate with `manager-api` for configuration

---

## Project Structure

```
BabyMilu-voice-streaming-server/
├── main/
│   ├── xiaozhi-server/          # Python WebSocket server (core)
│   │   ├── app.py               # Entry point
│   │   ├── config.yaml          # Default configuration template
│   │   ├── config/              # Configuration management
│   │   │   ├── settings.py      # Settings loader
│   │   │   ├── config_loader.py # Config loading logic
│   │   │   └── logger.py        # Logging setup
│   │   ├── core/                # Core processing logic
│   │   │   ├── websocket_server.py    # WebSocket server
│   │   │   ├── connection.py          # Connection handler
│   │   │   ├── http_server.py         # HTTP endpoints
│   │   │   ├── api/                   # API handlers
│   │   │   │   ├── ota_handler.py    # OTA update handler
│   │   │   │   └── vision_handler.py # Vision analysis handler
│   │   │   ├── providers/             # AI provider adapters
│   │   │   │   ├── asr/              # ASR providers
│   │   │   │   ├── tts/               # TTS providers
│   │   │   │   ├── llm/               # LLM providers
│   │   │   │   └── tools/             # Tool/function handlers
│   │   │   ├── handle/                # Message handlers
│   │   │   └── utils/                 # Utility functions
│   │   ├── plugins_func/              # Plugin system
│   │   ├── services/                  # Background services
│   │   ├── models/                    # ML models directory
│   │   └── requirements.txt           # Python dependencies
│   │
│   ├── manager-api/              # Java Spring Boot API
│   │   ├── pom.xml               # Maven configuration
│   │   └── src/main/
│   │       ├── java/            # Java source code
│   │       └── resources/       # Configuration files
│   │           ├── application.yml
│   │           ├── application-dev.yml
│   │           └── db/changelog/ # Database migrations
│   │
│   ├── manager-web/             # Vue.js web console
│   │   ├── package.json
│   │   ├── vue.config.js
│   │   └── src/
│   │
│   └── manager-mobile/          # uni-app mobile console
│       ├── package.json
│       ├── env/                  # Environment variables
│       └── src/
│
├── docs/                         # Documentation
├── OTA/                          # OTA manifest files
│   ├── ota_manifest.json
│   └── ota_manifest.example.json
├── docker-compose.yml            # Docker compose (server only)
├── docker-compose_all.yml        # Docker compose (full stack)
└── Dockerfile-server             # Server Dockerfile
```

---

## Core Components

### 1. xiaozhi-server (Python Server)

**Entry Point**: `app.py`
- Initializes WebSocket and HTTP servers
- Loads configuration from `data/.config.yaml` or API
- Manages signal handling and graceful shutdown

**Key Files**:
- `core/websocket_server.py`: WebSocket server implementation
- `core/connection.py`: Handles individual device connections
- `core/http_server.py`: HTTP endpoints for OTA and vision analysis
- `config/config_loader.py`: Configuration loading and merging logic

**Processing Flow**:
```
Device → WebSocket → ConnectionHandler → VAD → ASR → Intent → LLM → Memory → TTS → Device
```

### 2. manager-api (Java Backend)

**Entry Point**: `src/main/java/xiaozhi/AdminApplication.java`

**Key Features**:
- User authentication and authorization (Apache Shiro)
- Device management and binding
- Configuration management (stored in MySQL)
- OTA update management
- Voiceprint management
- Agent/LLM configuration

**Database**: MySQL with Liquibase migrations

### 3. manager-web (Vue.js Frontend)

**Purpose**: Web-based management console
- User management
- Device configuration
- Model provider settings
- OTA management
- System parameters

### 4. manager-mobile (uni-app)

**Purpose**: Mobile management app (iOS, Android, WeChat Mini Program)
- Cross-platform device management
- Simplified interface for mobile use

---

## Configuration Files

### Critical Configuration Files

#### 1. `main/xiaozhi-server/config.yaml`
**Location**: `main/xiaozhi-server/config.yaml`
**Purpose**: Default configuration template
**Note**: DO NOT edit this file directly. Use `data/.config.yaml` instead.

**Key Sections**:
- `server`: Server IP, ports, WebSocket URL
- `selected_module`: Active modules (VAD, ASR, LLM, TTS, Memory, Intent)
- `ASR`: ASR provider configurations
- `TTS`: TTS provider configurations
- `LLM`: LLM provider configurations
- `VLLM`: Vision model configurations
- `plugins`: Plugin configurations (weather, news, etc.)

#### 2. `data/.config.yaml` (User Configuration)
**Location**: `main/xiaozhi-server/data/.config.yaml`
**Purpose**: User-specific configuration (overrides `config.yaml`)
**Status**: ⚠️ **NOT in GitHub** - Contains sensitive keys

**How it works**:
- System merges `data/.config.yaml` with `config.yaml`
- User config takes precedence
- Only override what you need to change

**Example minimal config**:
```yaml
selected_module:
  LLM: ChatGLMLLM
  ASR: FunASR
  TTS: EdgeTTS

LLM:
  ChatGLMLLM:
    api_key: your-api-key-here
```

#### 3. `main/manager-api/src/main/resources/application.yml`
**Purpose**: Spring Boot base configuration
**Contains**: Server port (8002), context path, MyBatis settings

#### 4. `main/manager-api/src/main/resources/application-dev.yml`
**Purpose**: Development environment configuration
**Contains**: 
- Database connection (MySQL)
- Redis connection
- ⚠️ **NOT in GitHub** - Contains database credentials

**Key Settings**:
```yaml
spring:
  datasource:
    druid:
      url: jdbc:mysql://127.0.0.1:3306/xiaozhi_esp32_server
      username: root
      password: 123456  # ⚠️ Change this!
  data:
    redis:
      host: 127.0.0.1
      port: 6379
      password:  # ⚠️ Set if Redis has password
```

#### 5. `main/manager-web/.env.development`
**Purpose**: Frontend development environment variables
**Contains**: API base URL for development

**Example**:
```env
VUE_APP_API_BASE_URL=http://localhost:8002
```

#### 6. `main/manager-mobile/env/.env.development`
**Purpose**: Mobile app environment variables
**Contains**: Server URL, app IDs, etc.

**Key Variables**:
- `VITE_SERVER_BASEURL`: Backend API URL
- `VITE_UNI_APPID`: uni-app application ID
- `VITE_WX_APPID`: WeChat Mini Program ID

#### 7. `OTA/ota_manifest.json`
**Purpose**: OTA update manifest
**Contains**: Firmware version information for devices

---

## Environment Variables & Secrets

### Python Server (xiaozhi-server)

#### Required Environment Variables (Optional - can use config file)
- `GOOGLE_APPLICATION_CREDENTIALS`: Path to GCP service account JSON (for Firestore)
- `GOOGLE_CLOUD_PROJECT`: GCP project ID
- `MQTT_URL`: MQTT broker URL (e.g., `mqtt://localhost:1883`)
- `TZ`: Timezone (e.g., `Asia/Shanghai`)
- `PYTHONUNBUFFERED`: Set to `1` for Docker logging

#### Configuration via `data/.config.yaml`:
All API keys and secrets should be configured here:
- LLM API keys (ChatGLM, Doubao, DeepSeek, etc.)
- TTS API keys (Aliyun, Tencent, etc.)
- ASR API keys
- Plugin API keys (weather, etc.)
- Manager API secret (if using manager-api)

### Java Backend (manager-api)

#### Environment Variables (Docker/Production)
- `SPRING_DATASOURCE_DRUID_URL`: MySQL connection URL
- `SPRING_DATASOURCE_DRUID_USERNAME`: MySQL username
- `SPRING_DATASOURCE_DRUID_PASSWORD`: MySQL password
- `SPRING_DATA_REDIS_HOST`: Redis host
- `SPRING_DATA_REDIS_PASSWORD`: Redis password
- `SPRING_DATA_REDIS_PORT`: Redis port (default: 6379)
- `TZ`: Timezone

#### Application Properties (Local Development)
Configured in `application-dev.yml`:
- Database credentials
- Redis connection
- Shiro security settings
- Aliyun SMS configuration (if used)

### Frontend (manager-web)

#### Environment Variables
- `VUE_APP_API_BASE_URL`: Backend API base URL
- Development: `http://localhost:8002`
- Production: Your production API URL

### Mobile App (manager-mobile)

#### Environment Variables (in `env/.env.development`)
- `VITE_SERVER_BASEURL`: Backend API URL
- `VITE_UNI_APPID`: uni-app application ID
- `VITE_WX_APPID`: WeChat Mini Program app ID
- `VITE_APP_TITLE`: Application title

---

## Key Modules Deep Dive

### 1. Connection Handler (`core/connection.py`)

**Purpose**: Manages individual device WebSocket connections

**Key Responsibilities**:
- WebSocket message handling
- Audio frame processing
- VAD/ASR/TTS pipeline orchestration
- Device authentication
- Session management
- Error handling and reconnection

**Important Methods**:
- `handle_connection()`: Main connection loop
- `process_audio_frame()`: Processes incoming audio
- `handle_text_message()`: Processes text messages
- `handle_tts_response()`: Handles TTS audio generation

### 2. Configuration Loader (`config/config_loader.py`)

**Purpose**: Loads and merges configuration files

**Loading Priority**:
1. `data/.config.yaml` (user config)
2. `config.yaml` (default template)
3. API configuration (if `read_config_from_api: true`)

**Key Functions**:
- `load_config()`: Main config loader with caching
- `get_config_from_api()`: Fetches config from manager-api
- `merge_configs()`: Recursively merges configs
- `get_private_config_from_api()`: Gets device-specific config

### 3. Module Initialization (`core/utils/modules_initialize.py`)

**Purpose**: Initializes AI provider modules

**Modules**:
- VAD (Voice Activity Detection)
- ASR (Automatic Speech Recognition)
- LLM (Large Language Model)
- TTS (Text-to-Speech)
- Memory (Conversation memory)
- Intent (Intent recognition)

**Provider System**:
- Each module type has multiple provider implementations
- Providers are selected via `selected_module` config
- Supports hot-swapping providers

### 4. OTA Handler (`core/api/ota_handler.py`)

**Purpose**: Handles Over-The-Air firmware updates

**Endpoints**:
- `GET /xiaozhi/ota/`: Device queries for updates
- `POST /xiaozhi/ota/`: Server pushes update info

**Features**:
- Version checking
- Firmware URL generation
- Update manifest management

### 5. Vision Handler (`core/api/vision_handler.py`)

**Purpose**: Handles image analysis requests

**Endpoints**:
- `GET /mcp/vision/explain`: Vision analysis endpoint
- `POST /mcp/vision/explain`: Vision analysis with image

**Features**:
- JWT authentication
- Image upload handling
- VLLM integration

### 6. Plugin System (`plugins_func/`)

**Purpose**: Extensible function calling system

**Structure**:
- `functions/`: Plugin implementations
- `loadplugins.py`: Auto-loading mechanism
- `register.py`: Plugin registration

**Built-in Plugins**:
- `get_weather`: Weather information
- `get_news_from_newsnow`: News fetching
- `play_music`: Local music playback
- `handle_exit_intent`: Exit command handling
- `hass_*`: Home Assistant integration

---

## Deployment Architecture

### Deployment Modes

#### 1. Simplified Installation (Server Only)
- **Components**: `xiaozhi-server` only
- **Storage**: Configuration in `data/.config.yaml`
- **Database**: Not required
- **Use Case**: Personal use, testing, low-resource environments

#### 2. Full Module Installation
- **Components**: All modules (server + manager-api + manager-web)
- **Storage**: MySQL database
- **Features**: Full management console, user management, device binding
- **Use Case**: Production, multi-user, enterprise

### Docker Deployment

#### Server Only (`docker-compose.yml`)
```yaml
services:
  xiaozhi-esp32-server:
    image: ghcr.nju.edu.cn/xinnan-tech/xiaozhi-esp32-server:server_latest
    ports:
      - "8000:8000"  # WebSocket
      - "8003:8003"  # HTTP
    volumes:
      - ./data:/opt/xiaozhi-esp32-server/data
      - ./models:/opt/xiaozhi-esp32-server/models
```

#### Full Stack (`docker-compose_all.yml`)
Includes:
- `xiaozhi-esp32-server`: Python server
- `xiaozhi-esp32-server-web`: Java API + Vue frontend
- `xiaozhi-esp32-server-db`: MySQL database
- `xiaozhi-esp32-server-redis`: Redis cache

### Ports

| Service | Port | Purpose |
|---------|------|---------|
| xiaozhi-server | 8000 | WebSocket server |
| xiaozhi-server | 8003 | HTTP server (OTA, Vision) |
| manager-api | 8002 | REST API |
| manager-web | 8001 | Web console (dev) |
| MySQL | 3306 | Database |
| Redis | 6379 | Cache |

---

## Important Files Not in GitHub

### ⚠️ Critical Files (Must Create Locally)

#### 1. `main/xiaozhi-server/data/.config.yaml`
**Status**: ⚠️ **NOT in GitHub** (contains secrets)
**Purpose**: User-specific configuration with API keys
**How to Create**:
```bash
cd main/xiaozhi-server
mkdir -p data
cp config.yaml data/.config.yaml
# Edit data/.config.yaml with your API keys
```

**Required Configuration**:
- At minimum: LLM API key (e.g., ChatGLM)
- Recommended: All API keys for your selected providers

#### 2. `main/manager-api/src/main/resources/application-dev.yml`
**Status**: ⚠️ **NOT in GitHub** (contains database credentials)
**Purpose**: Development environment configuration
**How to Create**:
```bash
cd main/manager-api/src/main/resources
# Copy from template or create new
```

**Required Settings**:
- MySQL connection URL, username, password
- Redis connection details

#### 3. `main/manager-web/.env.development`
**Status**: ⚠️ **May not be in GitHub**
**Purpose**: Frontend development environment
**Required**: `VUE_APP_API_BASE_URL`

#### 4. `main/manager-mobile/env/.env.development`
**Status**: ⚠️ **NOT in GitHub**
**Purpose**: Mobile app environment
**Required**: `VITE_SERVER_BASEURL`, app IDs

#### 5. `main/xiaozhi-server/data/.gcp/sa.json` (Optional)
**Status**: ⚠️ **NOT in GitHub** (contains GCP credentials)
**Purpose**: Google Cloud service account for Firestore
**When Needed**: If using Firestore for device profiles

#### 6. Model Files
**Status**: ⚠️ **NOT in GitHub** (large files)
**Location**: `main/xiaozhi-server/models/`
**Required Models**:
- `SenseVoiceSmall/model.pt`: FunASR model
- `snakers4_silero-vad/`: VAD model files

**Download Instructions**: See deployment docs

#### 7. `OTA/ota_manifest.json`
**Status**: ⚠️ **May contain device-specific info**
**Purpose**: OTA update manifest
**Note**: `ota_manifest.example.json` is in GitHub as template

### Configuration Checklist for New Team Member

1. ✅ Clone repository
2. ⚠️ Create `data/.config.yaml` with API keys
3. ⚠️ Download model files to `models/`
4. ⚠️ Configure `application-dev.yml` (if using manager-api)
5. ⚠️ Set up MySQL database (if using full stack)
6. ⚠️ Set up Redis (if using full stack)
7. ⚠️ Configure frontend `.env` files
8. ⚠️ Set up GCP credentials (if using Firestore)

---

## Development Workflow

### Local Development Setup

#### 1. Python Server
```bash
cd main/xiaozhi-server
conda create -n xiaozhi-esp32-server python=3.10 -y
conda activate xiaozhi-esp32-server
pip install -r requirements.txt
# Create data/.config.yaml
python app.py
```

#### 2. Java Backend
```bash
cd main/manager-api
# Configure application-dev.yml
# Start MySQL and Redis
mvn spring-boot:run
# Or run AdminApplication.java in IDE
```

#### 3. Web Frontend
```bash
cd main/manager-web
npm install
# Configure .env.development
npm run serve
```

#### 4. Mobile App
```bash
cd main/manager-mobile
pnpm install
# Configure env/.env.development
pnpm dev:h5  # For H5
pnpm dev:mp  # For WeChat Mini Program
```

### Testing

#### Audio Interaction Test
- Location: `main/xiaozhi-server/test/test_page.html`
- Usage: Open in Chrome browser
- Purpose: Test WebSocket audio streaming

#### Performance Tester
- Location: `main/xiaozhi-server/performance_tester.py`
- Usage: `python performance_tester.py`
- Purpose: Test ASR, LLM, TTS response times

---

## Common Issues & Solutions

### Configuration Issues

**Problem**: Server can't find `data/.config.yaml`
**Solution**: Create `data` directory and `.config.yaml` file

**Problem**: API keys not working
**Solution**: Check `data/.config.yaml` has correct keys (not placeholder text)

**Problem**: Model files missing
**Solution**: Download models to `models/` directory (see deployment docs)

### Connection Issues

**Problem**: Device can't connect to WebSocket
**Solution**: 
- Check firewall allows port 8000
- Verify WebSocket URL in config matches server IP
- Check device token if authentication enabled

**Problem**: OTA endpoint not accessible
**Solution**: 
- Check HTTP port 8003 is open
- Verify `server.http_port` in config
- Check if using manager-api (OTA handled differently)

### Database Issues

**Problem**: manager-api can't connect to MySQL
**Solution**: 
- Verify `application-dev.yml` has correct credentials
- Check MySQL is running
- Verify database `xiaozhi_esp32_server` exists

---

## Additional Resources

### Documentation
- Main README: `/README.md`
- Deployment Guide: `/docs/Deployment.md`
- Full Stack Deployment: `/docs/Deployment_all.md`
- FAQ: `/docs/FAQ.md`

### Integration Guides
- Fish Speech: `/docs/fish-speech-integration.md`
- Home Assistant: `/docs/homeassistant-integration.md`
- MCP Endpoint: `/docs/mcp-endpoint-integration.md`
- Weather Plugin: `/docs/weather-integration.md`

### API Documentation
- manager-api Swagger: `http://localhost:8002/xiaozhi/doc.html` (when running)

---

## Quick Start Checklist

For a new team member to get started:

1. **Read this document** ✅
2. **Clone repository**
3. **Set up Python environment** (conda recommended)
4. **Create `data/.config.yaml`** with at least one LLM API key
5. **Download model files** (FunASR model)
6. **Run server**: `python app.py`
7. **Test connection**: Use `test/test_page.html`
8. **Configure providers**: Add more API keys as needed
9. **Set up manager-api** (if using full stack)
10. **Set up manager-web** (if using full stack)

---

## Support & Contribution

- GitHub Issues: Report bugs and feature requests
- Documentation: Check `/docs/` folder
- Code Structure: Refer to this walkthrough
- Configuration: Always use `data/.config.yaml`, never commit secrets

---

**Last Updated**: Based on codebase analysis
**Maintained By**: Development Team
