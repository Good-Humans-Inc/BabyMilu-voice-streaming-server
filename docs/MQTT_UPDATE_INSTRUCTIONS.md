# How to Update MQTT Configuration on VM

## Quick Steps

Since mosquitto is running in Docker, you need to:

1. **Update the `mosquitto.conf` file** (already done in the repo)
2. **Restart the Docker container** to apply changes

## Finding Your Docker Setup

### Check Docker Version and Commands

```bash
# Check if docker is installed
docker --version

# Check if docker compose (newer version, without hyphen) is available
docker compose version

# Or check if docker-compose (older version, with hyphen) is available
docker-compose --version

# List all running containers
docker ps

# List all containers (including stopped)
docker ps -a
```

## Restarting Mosquitto Container

### Option 1: Using `docker compose` (newer, recommended)

```bash
# Restart just the mqtt service
docker compose restart mqtt

# Or with specific compose files
docker compose -f docker-compose.yml -f docker-compose.override.yml restart mqtt
```

### Option 2: Using `docker` commands directly

If docker-compose isn't available, you can restart the container directly:

```bash
# Find the mosquitto container name/ID
docker ps | grep mqtt
# or
docker ps -a | grep mqtt

# Restart by container name (replace 'mqtt' with actual container name)
docker restart mqtt

# Or restart by container ID
docker restart <container-id>

# Example: if container is named 'current-mqtt-1'
docker restart current-mqtt-1
```

### Option 3: Using systemd (if container is managed by systemd)

```bash
# Check if there's a systemd service
systemctl list-units | grep mqtt
systemctl list-units | grep docker

# Restart the service
sudo systemctl restart <service-name>
```

## Detailed Instructions

### Step 1: Find Your Mosquitto Container

```bash
# List all containers to find mosquitto
docker ps -a

# Look for a container with 'mqtt' or 'mosquitto' in the name
# Common names: mqtt, mosquitto, current-mqtt-1, staging-mqtt-1, etc.
```

### Step 2: Verify Configuration File

The `mosquitto.conf` file should be in your deployment directory:

```bash
# You're already in the right directory
cd /srv/staging/current

# Verify the mosquitto.conf file exists and has the updated settings
cat mosquitto.conf | grep -A 3 "persistence"
```

### Step 3: Restart the Container

Once you know the container name/ID:

```bash
# Method 1: Using docker compose (if available)
docker compose restart mqtt

# Method 2: Using docker directly
docker restart <container-name-or-id>

# Method 3: Stop and start
docker stop <container-name-or-id>
docker start <container-name-or-id>
```

### Step 4: Verify Configuration Applied

Check the logs to ensure mosquitto started correctly:

```bash
# View mosquitto logs
docker logs <container-name-or-id>

# Or follow logs in real-time
docker logs -f <container-name-or-id>

# Check if mosquitto is listening on port 1883
netstat -tlnp | grep 1883
# or
ss -tlnp | grep 1883
```

### Step 5: Verify the Config is Loaded

```bash
# Check if mosquitto is using the new config
docker exec <container-name-or-id> cat /mosquitto/config/mosquitto.conf | grep persistence

# Check if persistence directory exists
docker exec <container-name-or-id> ls -la /mosquitto/data/ 2>/dev/null || echo "Directory will be created on first use"
```

## Troubleshooting

### If docker-compose command not found

**Newer Docker installations** use `docker compose` (without hyphen) as a plugin:

```bash
# Try this instead
docker compose version
docker compose ps
docker compose restart mqtt
```

**If that doesn't work**, use `docker` commands directly:

```bash
# Find the container
docker ps -a | grep mqtt

# Restart it
docker restart <container-name>
```

### Container Won't Start

```bash
# Check container logs for errors
docker logs <container-name-or-id>

# Check if config file is valid
docker exec <container-name-or-id> mosquitto -c /mosquitto/config/mosquitto.conf -v
```

### Permission Issues

If you see permission errors for `/mosquitto/data` or `/mosquitto/log`:

```bash
# Check if volumes are mounted correctly
docker inspect <container-name-or-id> | grep -A 10 Mounts

# The config should show the volume mount for mosquitto.conf
```

### Port Already in Use

If port 1883 is already in use:

```bash
# Find what's using port 1883
sudo lsof -i :1883
# or
sudo netstat -tlnp | grep 1883

# Stop the conflicting service or change mosquitto port
```

## Quick Reference Commands

```bash
# 1. Find mosquitto container
docker ps -a | grep mqtt

# 2. Restart it (replace 'mqtt' with actual container name)
docker restart mqtt

# 3. Check logs
docker logs -f mqtt

# 4. Verify config
docker exec mqtt cat /mosquitto/config/mosquitto.conf | grep persistence
```

## Summary

**Most likely you need:**
```bash
# Find the container
docker ps -a | grep mqtt

# Restart it
docker restart <container-name>

# Verify
docker logs <container-name>
```

**Or if docker compose plugin is available:**
```bash
docker compose restart mqtt
docker compose logs -f mqtt
```
