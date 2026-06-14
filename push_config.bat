@echo off
cd /d "C:\Users\MOON\Documents\airplane_ticket_crawler"
git add config.json
git commit -m "config: 설정 업데이트 %date% %time%"
git pull --rebase origin main
git push
echo.
echo 완료! config.json 이 GitHub 에 반영됐어요.
pause
