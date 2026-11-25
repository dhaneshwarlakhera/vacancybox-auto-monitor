# Vacancy Box Auto-Monitor (Top 11 Official Govt Portals)

Har **30 minute** mein 11 official Indian government portals ko scan karta hai, aur koi bhi naya notice/PDF aate hi directly **vacancybox.in** ke **Vacancy Box Data Entry Form** mein **Draft** create kar deta hai (+ optional Telegram alert).

## 3-Minute Setup Guide

### Step 1: WordPress Application Password Banayein
1. `https://vacancybox.in` ke WordPress Admin mein **Users → Profile** par jayein.
2. Neeche **Application Passwords** section mein naam likhein: `GitHub Auto Monitor` aur **Add Application Password** click karein.
3. Jo password generate ho (e.g. `xxxx xxxx xxxx xxxx`), use copy kar lein.

### Step 2: GitHub Repository Banayein & Files Upload Karein
1. GitHub par ek naya repository banayein (e.g. `vacancybox-auto-monitor`).
2. Is ZIP ko unzip karke saari files (`.github/workflows/monitor.yml`, `monitor.py`, `portals.json`, `requirements.txt`, `seen_notices.json`, `README.md`) repo mein upload/push kar dein.

### Step 3: GitHub Secrets Add Karein
Repo ke **Settings → Secrets and variables → Actions → New repository secret** mein ye add karein:
- `WP_URL` = `https://vacancybox.in`
- `WP_USERNAME` = Aapka WordPress admin username
- `WP_APP_PASSWORD` = Step 1 mein bana Application Password
- *(Optional)* `TELEGRAM_BOT_TOKEN` = BotFather se mila token
- *(Optional)* `TELEGRAM_CHAT_ID` = Aapka Telegram Chat ID

### Step 4: First Run (Baseline)
- Repo ke **Actions** tab mein **Vacancy Box Auto-Monitor → Run workflow** click karein.
- **Pehla run** sirf purane notices ko `seen_notices.json` mein save karega (taaki purane 200 notices se WordPress flood na ho).
- **Uske baad** har 30 minute mein jab bhi koi **naya official notice** aayega, turant aapke WordPress **Posts → Drafts** mein ready draft ban jayega!

## Naye Portals Kaise Add Karein?
Sirf `portals.json` mein naya portal object add karke commit kar dein — script bina code badle use bhi track karne lagegi!
