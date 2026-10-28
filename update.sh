#!/data/data/com.termux/files/usr/bin/bash

set -e

cd "$(dirname "$0")"

echo "Checking for updates..."

git pull --ff-only

echo "Installing required dependencies..."

if [ -f requirements.txt ]; then
    pip install -r requirements.txt
fi

echo "Update completed successfully!"
