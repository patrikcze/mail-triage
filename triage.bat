@echo off
rem Mail Triage for Windows (reads exported .eml/.mbox folders today; Outlook support is planned).
py -3 "%~dp0triage.py" %*
