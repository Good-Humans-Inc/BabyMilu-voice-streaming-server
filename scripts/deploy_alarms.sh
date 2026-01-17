#!/bin/bash
set -e

# Usage check
if [ -z "$1" ]; then
    echo "❌ Usage: ./deploy_alarms.sh [dev|staging|prod]"
    exit 1
fi

ENV="$1"
PROJECT_ROOT="$(pwd)"
BUILD_DIR="_build_alarms"
FUNCTION_SOURCE="services/alarms/cloud/functions.py"
FUNCTION_ENTRYPOINT="scan_due_alarms"
RUNTIME="python310"
REGION="us-central1"

# Environment-specific configuration
case "$ENV" in
    dev)
        FUNCTION_NAME="scan-due-alarms-dev"
        ENV_FILE="env.dev.yaml"
        ;;
    staging)
        FUNCTION_NAME="scan-due-alarms-staging"
        ENV_FILE="env.staging.yaml"
        ;;
    prod)
        FUNCTION_NAME="scan-due-alarms-prod"
        ENV_FILE="env.prod.yaml"
        ;;
    *)
        echo "❌ Error: Environment must be one of: dev, staging, prod"
        exit 1
        ;;
esac

# Ensure we are in the right directory
if [ ! -f "config.yaml" ]; then
    echo "❌ Error: config.yaml not found. Please run this script from main/xiaozhi-server/"
    exit 1
fi

# Check for env file
if [ ! -f "$ENV_FILE" ]; then
    echo "❌ Error: $ENV_FILE not found."
    echo "   Please create it with ALARM_WS_URL and ALARM_MQTT_URL."
    exit 1
fi

echo "🚧 Preparing build in $BUILD_DIR for environment: $ENV"

# 1. Clean and create build directory
rm -rf "$BUILD_DIR"
mkdir "$BUILD_DIR"

# 2. Copy dependency files
echo "📋 Copying configuration and requirements..."
cp services/alarms/requirements.txt "$BUILD_DIR/requirements.txt"
cp config.yaml "$BUILD_DIR/"

# 3. Copy source modules
echo "📦 Copying source modules..."
# Copy config directory
cp -r config "$BUILD_DIR/"

# Copy services directory
cp -r services "$BUILD_DIR/"

# Copy core directory
echo "📦 Copying core (excluding heavy submodules)..."
mkdir -p "$BUILD_DIR/core"

if command -v rsync >/dev/null 2>&1; then
    rsync -av --exclude='providers/' --exclude='models/' core/ "$BUILD_DIR/core/" > /dev/null
else
    # Fallback if rsync is missing
    cp -r core "$BUILD_DIR/"
    rm -rf "$BUILD_DIR/core/providers"
    rm -rf "$BUILD_DIR/core/models"
fi

# 4. Copy the function code to main.py
echo "📄 Setting up entry point..."
cp "$FUNCTION_SOURCE" "$BUILD_DIR/main.py"

# 5. Deploy
echo "🚀 Deploying function $FUNCTION_NAME to $REGION..."
echo "   Source: $BUILD_DIR"
echo "   Entry Point: $FUNCTION_ENTRYPOINT"
echo "   Env vars from: $ENV_FILE"

# Navigate to build directory
cd "$BUILD_DIR"

# Deploy command
gcloud functions deploy "$FUNCTION_NAME" \
    --gen2 \
    --runtime "$RUNTIME" \
    --region "$REGION" \
    --source . \
    --entry-point "$FUNCTION_ENTRYPOINT" \
    --trigger-http \
    --allow-unauthenticated \
    --env-vars-file "../$ENV_FILE"

# 6. Cleanup
echo "🧹 Cleaning up..."
cd "$PROJECT_ROOT"
rm -rf "$BUILD_DIR"

echo "✅ Deployment to $ENV complete!"
