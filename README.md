# MAST Water Meter — Home Assistant Integration

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/hacs/integration)
[![Validate](https://github.com/leranp/HomeAssistant-Mast-Water-Meter/actions/workflows/validate.yaml/badge.svg)](https://github.com/leranp/HomeAssistant-Mast-Water-Meter/actions/workflows/validate.yaml)

<img src="logo.png" alt="MAST Logo" width="120"/>

Home Assistant integration for reading water meter data from [MAST](https://www.mast.co.il/) (Israeli water company).

## Features

- **Current Reading** — cumulative meter value (m³), compatible with the Energy Dashboard
- **Daily Usage** — consumption on the most recent available day (m³)
- **Full History** — one year of daily readings is injected into the Energy Dashboard on first load
- **Automatic session refresh** before every data fetch
- **Auto re-auth** — when the token expires, HA prompts for re-authentication

## Installation via HACS

### Method 1 — One click

[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=leranp&repository=HomeAssistant-Mast-Water-Meter&category=integration)

### Method 2 — Manual

1. Open **HACS** in Home Assistant
2. Click the three-dot menu (⋮) → **Custom repositories**
3. Enter the repository URL:
   ```
   https://github.com/leranp/HomeAssistant-Mast-Water-Meter
   ```
4. Select category: **Integration** → click **Add**
5. Search for **MAST Water Meter** → **Download**
6. Restart Home Assistant

## Configuration

1. Go to **Settings → Devices & Services → Add Integration**
2. Search for **MAST Water Meter**
3. Enter your phone number → receive an SMS with an OTP → enter the code

**Alternative** — enter a JWT token manually (skip OTP):
- Obtain a token from the browser (Network tab → `jwt` header)
- Paste it into the "Manual JWT token" field

## Energy Dashboard

After setup, go to **Settings → Energy → Water consumption → Add water source** and select:
- `mast_water:<meter_number>` — full historical data (recommended)

## Requirements

- Home Assistant 2023.1.0 or later
- An active MAST account with a smart water meter
