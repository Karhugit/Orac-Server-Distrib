#!/bin/bash
cd "$(dirname "$0")"

echo "============================================"
echo "  Orac Server Launcher"
echo "============================================"
echo
echo "Tip: If you get a permission error, run: chmod +x start_server.sh"
echo

# Check if venv exists
if [ -f "venv/bin/python" ]; then
    echo "Checking for updated dependencies..."
    venv/bin/pip install -q -r requirements.txt
    echo "Starting Orac Server..."
    venv/bin/python -W ignore::SyntaxWarning run_server.py
    exit $?
fi

echo "First run detected - setting up environment..."
echo

# Find Python
PYTHON_CMD=""
if command -v python3 &> /dev/null; then
    PYTHON_CMD="python3"
elif command -v python &> /dev/null; then
    PYTHON_CMD="python"
else
    echo "ERROR: Python not found. Please install Python 3.8+"
    echo "  Ubuntu/Debian: sudo apt install python3 python3-venv python3-pip"
    echo "  Fedora:        sudo dnf install python3"
    echo "  macOS:         brew install python3"
    exit 1
fi

echo "Found Python: $PYTHON_CMD"
$PYTHON_CMD --version
echo

# Create virtual environment
echo "Creating virtual environment..."
$PYTHON_CMD -m venv venv
if [ $? -ne 0 ]; then
    echo "ERROR: Failed to create virtual environment."
    echo "You may need to install python3-venv:"
    echo "  sudo apt install python3-venv"
    exit 1
fi
echo "Virtual environment created successfully."
echo

# Upgrade pip first to avoid install failures on older pip versions
echo "Upgrading pip..."
venv/bin/python -m pip install --upgrade pip --quiet

# Install requirements
echo "Installing dependencies..."
venv/bin/pip install -r requirements.txt
if [ $? -ne 0 ]; then
    echo "ERROR: Failed to install dependencies."
    echo "Try running: sudo apt install python3-dev build-essential"
    exit 1
fi
echo "Dependencies installed successfully."
echo

# Check for config.json
if [ ! -f "config.json" ]; then
    if [ -f "config.example.json" ]; then
        echo "Copying config.example.json to config.json..."
        cp config.example.json config.json
        echo
        echo "IMPORTANT: Please edit config.json with your API keys before running."
        echo "           - TRAKT client_id and client_secret"
        echo "           - TMDB api_key"
        echo
        exit 0
    fi
fi

echo "Setup complete!"
echo

# Start server
echo "Starting Orac Server..."
venv/bin/python -W ignore::SyntaxWarning run_server.py
