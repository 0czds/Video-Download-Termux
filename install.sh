#!/data/data/com.termux/files/usr/bin/bash

set -e

REPO="https://github.com/0czds/Video-Download-Termux.git"
INSTALL_DIR="$HOME/video-downloader"

echo "================================="
echo "   Video Downloader Installer"
echo "================================="
echo

echo "[1/5] Updating packages..."
pkg update -y

echo "[2/5] Installing dependencies..."
pkg install -y git python ffmpeg

echo "[3/5] Downloading project..."

if [ -d "$INSTALL_DIR/.git" ]; then
    echo "Project already exists. Updating..."
    git -C "$INSTALL_DIR" pull
else
    if [ -d "$INSTALL_DIR" ]; then
        echo "Existing folder found."
        echo "Please remove or rename it before installation."
        exit 1
    fi

    git clone "$REPO" "$INSTALL_DIR"
fi

echo "[4/5] Installing Python requirements..."
cd "$INSTALL_DIR"

python -m pip install --upgrade pip
pip install -r requirements.txt

echo "[5/5] Creating 'video' command..."

cat > "$PREFIX/bin/video" <<EOF
#!/data/data/com.termux/files/usr/bin/bash
cd "$INSTALL_DIR"
python app.py
EOF

chmod +x "$PREFIX/bin/video"

echo
echo "================================="
echo " Installation completed!"
echo "================================="
echo
echo "Project location:"
echo "$INSTALL_DIR"
echo
echo "Run the program with:"
echo "video"
echo
