@echo off
echo Installing required Python packages...
pip install -r requirements.txt

echo.
echo Starting Plex Playlist Sync Web Interface...
python app.py

pause
