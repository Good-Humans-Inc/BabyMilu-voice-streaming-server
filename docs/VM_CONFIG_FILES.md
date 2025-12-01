# VM Configuration Files - How to Get Them

## Quick Commands to Find All Config Files

### 1. Docker Compose Configuration Files

```bash
# Find all docker-compose files in current directory
ls -la docker-compose*.yml

# View docker-compose.yml (base config)
cat docker-compose.yml

# View docker-compose.override.yml (your custom overrides)
cat docker-compose.override.yml

# View all compose files together
cat docker-compose.yml docker-compose.override.yml
```

### 2. Application Configuration Files

```bash
# Main application config (if exists)
cat data/.config.yaml

# Or check if it exists first
ls -la data/.config.yaml

# Default config template
cat config.yaml

# Config loaded from API (if using manager-api)
cat config_from_api.yaml
```

### 3. MQTT Configuration

```bash
# MQTT broker config
cat mosquitto.conf

# Verify it's mounted in container
docker exec $(docker ps -q -f name=mqtt) cat /mosquitto/config/mosquitto.conf
```

### 4. Environment Variables

```bash
# Check environment variables in running containers
docker exec $(docker ps -q -f name=xiaozhi) env | sort

# Or for specific container
docker exec current-xiaozhi-esp32-server-1 env | sort

# Check docker-compose environment settings
grep -A 10 "environment:" docker-compose.override.yml
```

### 5. All Configuration Files in One Command

```bash
# Find all YAML config files
find . -name "*.yaml" -o -name "*.yml" | grep -v node_modules | grep -v ".git"

# Find all config files (yaml, yml, conf, env)
find . -type f \( -name "*.yaml" -o -name "*.yml" -o -name "*.conf" -o -name ".env*" \) | grep -v node_modules | grep -v ".git"

# List all important config files
ls -lah docker-compose*.yml mosquitto.conf data/.config.yaml 2>/dev/null
```

## Detailed Commands by Category

### Docker Configuration

```bash
# 1. List all docker-compose files
ls -la docker-compose*.yml

# 2. View base docker-compose.yml
cat docker-compose.yml

# 3. View override file (your custom settings)
cat docker-compose.override.yml

# 4. View combined effective config (if docker compose supports it)
docker compose config

# 5. Check which compose files are being used
docker compose config --services
```

### Application Configuration

```bash
# 1. Check if user config exists (contains secrets - NOT in git)
ls -la data/.config.yaml

# 2. View user config (if exists)
cat data/.config.yaml

# 3. View default config template
cat config.yaml

# 4. Check config in running container
docker exec $(docker ps -q -f name=xiaozhi) cat /opt/xiaozhi-esp32-server/data/.config.yaml 2>/dev/null || echo "Config not found in container"
```

### MQTT Configuration

```bash
# 1. View mosquitto.conf
cat mosquitto.conf

# 2. Verify config in container
docker exec $(docker ps -q -f name=mqtt) cat /mosquitto/config/mosquitto.conf

# 3. Check MQTT logs
docker compose logs mqtt | tail -50
```

### Environment Variables

```bash
# 1. View environment in docker-compose files
grep -A 20 "environment:" docker-compose.override.yml

# 2. View actual environment in running container
docker exec $(docker ps -q -f name=xiaozhi) env

# 3. Check specific environment variables
docker exec $(docker ps -q -f name=xiaozhi) env | grep -E "MQTT_URL|GOOGLE_|TZ|PYTHON"
```

### System/VM Configuration

```bash
# 1. Check VM instance metadata (if GCP)
curl -H "Metadata-Flavor: Google" http://metadata.google.internal/computeMetadata/v1/instance/attributes/

# 2. Check system environment
env | sort

# 3. Check if .env file exists
ls -la .env .env.* 2>/dev/null

# 4. View systemd service files (if services are managed by systemd)
systemctl list-units | grep -E "docker|xiaozhi|mqtt"
```

## Complete Config Export Script

Create a script to export all configs:

```bash
#!/bin/bash
# save as: export_configs.sh

echo "=== Docker Compose Files ===" > /tmp/vm_configs.txt
cat docker-compose.yml >> /tmp/vm_configs.txt
echo -e "\n=== Docker Compose Override ===" >> /tmp/vm_configs.txt
cat docker-compose.override.yml >> /tmp/vm_configs.txt

echo -e "\n=== MQTT Config ===" >> /tmp/vm_configs.txt
cat mosquitto.conf >> /tmp/vm_configs.txt

echo -e "\n=== Application Config (if exists) ===" >> /tmp/vm_configs.txt
if [ -f "data/.config.yaml" ]; then
    cat data/.config.yaml >> /tmp/vm_configs.txt
else
    echo "data/.config.yaml not found" >> /tmp/vm_configs.txt
fi

echo -e "\n=== Running Container Environment ===" >> /tmp/vm_configs.txt
docker exec $(docker ps -q -f name=xiaozhi) env | sort >> /tmp/vm_configs.txt

echo "Configs exported to /tmp/vm_configs.txt"
cat /tmp/vm_configs.txt
```

## Quick Reference - Most Important Files

```bash
# The 4 most important config files:
# 1. Docker compose override (your custom settings)
cat docker-compose.override.yml

# 2. MQTT config
cat mosquitto.conf

# 3. Application config (contains API keys - be careful!)
cat data/.config.yaml

# 4. Environment variables in container
docker exec $(docker ps -q -f name=xiaozhi) env | grep -E "MQTT|GOOGLE|TZ"
```

## Finding Container Names First

If you're not sure of container names:

```bash
# List all running containers
docker ps

# Find xiaozhi server container
docker ps | grep xiaozhi

# Find mqtt container
docker ps | grep mqtt

# Get container IDs
docker ps -q -f name=xiaozhi
docker ps -q -f name=mqtt
```

## Viewing Configs Safely (Without Secrets)

If you want to view configs but hide secrets:

```bash
# View docker-compose without showing full content
grep -v "secret\|password\|key\|token" docker-compose.override.yml

# View app config structure (first 50 lines, no secrets)
head -50 data/.config.yaml 2>/dev/null | grep -v "api_key\|secret\|token\|password"
```

