#!/data/data/com.termux/files/usr/bin/bash
set -e

echo "=== NewscastAI Installer ==="
echo ""

# System packages
echo "[1/4] Installing system packages..."
pkg install -y ffmpeg python 2>/dev/null || echo "  (some already installed)"

# Python packages
echo "[2/4] Installing Python packages..."
pip3 install --quiet \
    yt-dlp \
    edge-tts \
    anthropic \
    trafilatura \
    beautifulsoup4 \
    requests \
    pillow \
    mcp \
    faster-whisper \
    moviepy

# yt-dlp update
echo "[3/4] Updating yt-dlp..."
yt-dlp -U 2>/dev/null || pip3 install -U yt-dlp

# Create output dirs
echo "[4/4] Setting up directories..."
mkdir -p ~/newscast-ai/{output,temp,assets}

echo ""
echo "=== Installation complete! ==="
echo ""
echo "Quick start:"
echo "  cd ~/newscast-ai"
echo "  python3 tools/pipeline.py https://your-news-url.com"
echo ""
echo "To register MCP server with Claude Code:"
echo "  claude mcp add newscast-ai -- python3 ~/newscast-ai/mcp/server.py"
