@echo off
set "CLOUDFLARED=C:\Program Files (x86)\cloudflared\cloudflared.exe"
echo ============================================
echo  TASS LMS - Cloudflare Quick Tunnel
echo  Forwards a public HTTPS URL to localhost:8000
echo  COPY the https://____.trycloudflare.com URL
echo  shown below and share it with your testers.
echo  (Keep this window OPEN during the demo)
echo ============================================
"%CLOUDFLARED%" tunnel --url http://localhost:8000
