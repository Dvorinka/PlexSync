#!/bin/bash

echo "Installing required Python packages..."
pip install -r requirements.txt

echo ""
echo "Starting Plex Playlist Sync Web Interface..."
python3 app.py

echo ""
read -p "Press Enter to continue..."
