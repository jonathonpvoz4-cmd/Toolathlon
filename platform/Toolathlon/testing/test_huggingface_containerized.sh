#!/bin/bash

set -e

# Test Hugging Face MCP Server in containerized environment
# Usage: ./test_huggingface_containerized.sh [image_name] [runmode]

echo "Preparing Hugging Face server test..."

image_name=${1:-"lockon0927/toolathlon-task-image:1016beta"}
runmode=${2:-"normal"}

IMAGE_NAME="$image_name"

# Get project root directory
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
cd "$PROJECT_ROOT"

# Generate unique container name
TIMESTAMP=$(date +%Y%m%d-%H%M%S)
SAFE_TASK_NAME="test-huggingface"
CONTAINER_NAME="toolathlon-${SAFE_TASK_NAME}-${TIMESTAMP}"

# Read container runtime configuration
CONTAINER_RUNTIME=$(uv run python -c "
import sys
sys.path.append('$PROJECT_ROOT/configs')
try:
    from global_configs import global_configs
    runtime = global_configs.get('podman_or_docker', 'podman')
    print(runtime)
except Exception as e:
    print('podman')
" 2>/dev/null)

echo "Using container runtime: $CONTAINER_RUNTIME"

# List of files and directories to copy
FILES_TO_COPY=(
    "configs"
    "deployment/k8s"
    "scripts"
    "deployment/canvas/logs"
    "testing/test_huggingface_server.py"
    "local_binary/github-mcp-server"
    "utils"
    "main.py"
)

# Verify all required files/directories exist
echo "Verifying file existence..."
for item in "${FILES_TO_COPY[@]}"; do
    if [ ! -e "$PROJECT_ROOT/$item" ]; then
        echo "  Warning: $item does not exist, skipping"
    else
        echo "  ✓ $item exists"
    fi
done

echo "Preparing to start container..."

# Step 1: Start container and keep it running
echo "Step 1: Starting container..."

# Container startup parameters
START_CONTAINER_ARGS=(
    "$CONTAINER_RUNTIME" "run"
    "-d"  # Run in background
    "--name" "$CONTAINER_NAME"
    "--network" "host" # Use host network
)

# Add environment variables if needed
for envarg in "${EXTRA_ENV_ARGS[@]}"; do
    START_CONTAINER_ARGS+=("$envarg")
done

# Add socket mount based on container runtime
if [ "$CONTAINER_RUNTIME" = "podman" ]; then
    echo "Configuring Podman environment..."
    PODMAN_SOCKET_FOUND=false

    # Check system-level podman socket
    if [ -S "/run/podman/podman.sock" ]; then
        START_CONTAINER_ARGS+=(
            "-v" "/run/podman/podman.sock:/run/podman/podman.sock"
        )
        echo "Using system-level podman socket: /run/podman/podman.sock"
        PODMAN_SOCKET_FOUND=true
    # Check user-level podman socket
    elif [ -S "/run/user/$(id -u)/podman/podman.sock" ]; then
        START_CONTAINER_ARGS+=(
            "-v" "/run/user/$(id -u)/podman/podman.sock:/run/podman/podman.sock"
        )
        echo "Using user-level podman socket: /run/user/$(id -u)/podman/podman.sock"
        PODMAN_SOCKET_FOUND=true
    fi

    if [ "$PODMAN_SOCKET_FOUND" = false ]; then
        echo "Warning: Podman socket not found"
        echo "Tip: Please run 'systemctl --user start podman.socket' or 'sudo systemctl start podman.socket'"
    fi

    START_CONTAINER_ARGS+=(
        "-e" "KIND_EXPERIMENTAL_PROVIDER=podman"
    )
elif [ "$CONTAINER_RUNTIME" = "docker" ]; then
    echo "Configuring Docker environment..."
    START_CONTAINER_ARGS+=(
        "-v" "/var/run/docker.sock:/var/run/docker.sock"
    )
fi

# Add working directory and image
START_CONTAINER_ARGS+=(
    "-w" "/workspace"
    "$IMAGE_NAME"
    "sleep" "3600"
)

echo "Container start command: ${START_CONTAINER_ARGS[*]}"
echo ""

# Start the container
echo "Starting container..."
CONTAINER_ID=$("${START_CONTAINER_ARGS[@]}")
START_EXIT_CODE=$?

if [ $START_EXIT_CODE -eq 0 ]; then
    echo "✓ Container started successfully"
    echo "  Container ID: $CONTAINER_ID"
    echo "  Container name: $CONTAINER_NAME"
else
    echo "✗ Container startup failed, exit code: $START_EXIT_CODE"
    exit $START_EXIT_CODE
fi

# Step 2: Wait for container to be ready
echo ""
echo "Step 2: Waiting for container to be ready..."

MAX_WAIT=30
WAIT_COUNT=0
CONTAINER_READY=false

while [ $WAIT_COUNT -lt $MAX_WAIT ]; do
    if $CONTAINER_RUNTIME ps -q --filter "name=$CONTAINER_NAME" | grep -q .; then
        if $CONTAINER_RUNTIME exec "$CONTAINER_NAME" echo "container ready" >/dev/null 2>&1; then
            CONTAINER_READY=true
            break
        fi
    else
        echo "✗ Container unexpectedly stopped"
        exit 1
    fi

    echo "  Waiting for container to be ready... (${WAIT_COUNT}/${MAX_WAIT})"
    sleep 1
    WAIT_COUNT=$((WAIT_COUNT + 1))
done

if [ "$CONTAINER_READY" = true ]; then
    echo "✓ Container is ready"
else
    echo "✗ Container not ready within ${MAX_WAIT} seconds"
    exit 1
fi

# Step 2.5: Copy project files to container
echo ""
echo "Step 2.5: Copying project files to container..."

# Create directory structure
echo "  Creating directory structure..."
$CONTAINER_RUNTIME exec "$CONTAINER_NAME" mkdir -p "/workspace/deployment"
$CONTAINER_RUNTIME exec "$CONTAINER_NAME" mkdir -p "/workspace/deployment/canvas"
$CONTAINER_RUNTIME exec "$CONTAINER_NAME" mkdir -p "/workspace/testing"

# Copy files
for item in "${FILES_TO_COPY[@]}"; do
    if [ -e "$PROJECT_ROOT/$item" ]; then
        echo "  Copying $item..."
        if [ -d "$PROJECT_ROOT/$item" ]; then
            parent_dir=$(dirname "$item")
            if [ "$parent_dir" != "." ]; then
                $CONTAINER_RUNTIME exec "$CONTAINER_NAME" mkdir -p "/workspace/$parent_dir"
            fi
        fi
        $CONTAINER_RUNTIME cp "$PROJECT_ROOT/$item" "$CONTAINER_NAME:/workspace/$item"
    fi
done

# Step 2.6: Execute configurations
echo ""
echo "Step 2.6: Executing configurations..."
copy_config_cmd='
  for dir in ~/.gmail-mcp ~/.calendar-mcp; do
    mkdir -p $dir
    cp ./configs/gcp-oauth.keys.json $dir/
    cp ./configs/google_credentials.json $dir/credentials.json
  done
'
if [ "$runmode" = "quickstart" ]; then
    $CONTAINER_RUNTIME exec "$CONTAINER_NAME" bash -c "$copy_config_cmd" || echo "Warning: Failed to copy config files"
else
    $CONTAINER_RUNTIME exec "$CONTAINER_NAME" bash -c "$copy_config_cmd"
fi

# Copy MCP auth directory
if [ -d "$PROJECT_ROOT/configs/.mcp-auth" ]; then
    echo "  Copying MCP auth from ./configs/.mcp-auth..."
    $CONTAINER_RUNTIME exec "$CONTAINER_NAME" mkdir -p /root/.mcp-auth
    $CONTAINER_RUNTIME cp "$PROJECT_ROOT/configs/.mcp-auth/." "$CONTAINER_NAME:/root/.mcp-auth/"
    echo "✓ MCP auth data copied from project configs"
elif [ -d "$HOME/.mcp-auth" ]; then
    echo "  Copying MCP auth from ~/.mcp-auth..."
    $CONTAINER_RUNTIME exec "$CONTAINER_NAME" mkdir -p /root/.mcp-auth
    $CONTAINER_RUNTIME cp "$HOME/.mcp-auth/." "$CONTAINER_NAME:/root/.mcp-auth/"
    echo "✓ MCP auth data copied from home directory"
else
    echo "  Warning: MCP auth not found, skipping"
fi

# Step 3: Run Hugging Face test
echo ""
echo "Step 3: Testing Hugging Face server..."

CONTAINER_CMD="uv run -m testing.test_huggingface_server"

echo "Executing command: $CONTAINER_CMD"
echo ""

$CONTAINER_RUNTIME exec -it "$CONTAINER_NAME" bash -c "$CONTAINER_CMD"
EXEC_EXIT_CODE=$?

echo ""
if [ $EXEC_EXIT_CODE -eq 0 ]; then
    echo "✓ Hugging Face test completed successfully, exit code: $EXEC_EXIT_CODE"
else
    echo "✗ Hugging Face test failed, exit code: $EXEC_EXIT_CODE"
fi

# Step 4: Cleanup
echo ""
echo "Step 4: Cleanup..."
echo "Stopping and removing container: $CONTAINER_NAME"
$CONTAINER_RUNTIME stop "$CONTAINER_NAME" >/dev/null 2>&1 || true
$CONTAINER_RUNTIME rm "$CONTAINER_NAME" >/dev/null 2>&1 || true
echo "✓ Cleanup completed"

exit $EXEC_EXIT_CODE