@echo off
rem Opens Facebook so you can log in again, without starting a search.
rem
rem The app keeps its own browser login, separate from Edge and Chrome, so
rem logging in with your normal browser doesn't renew it. Double-click this
rem whenever a run says the login has expired.

cd /d "%~dp0"
call "Start Faceplace Marketbook (Windows).bat" --login
