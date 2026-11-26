@echo off
rem Pull the latest online records, rebuild the chart page and open it.
cd /d "%~dp0"
git pull --ff-only
python cloud_fetch.py
python dashboard.py
start "" dashboard.html
