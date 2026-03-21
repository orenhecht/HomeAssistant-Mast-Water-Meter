# MAST Water Meter — Home Assistant Integration

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/hacs/integration)
[![Validate](https://github.com/leranp/mast-water-ha/actions/workflows/validate.yaml/badge.svg)](https://github.com/leranp/mast-water-ha/actions/workflows/validate.yaml)

אינטגרציה לHome Assistant לקריאת נתוני מונה מים מחברת [מאסט](https://www.mast.co.il/).

## תכונות

- **קריאה נוכחית** — קריאת המד המצטברת (m³), תואמת ל-Energy Dashboard
- **צריכה יומית** — צריכה ביום האחרון (m³)
- **היסטוריה** — שנה שלמה של נתונים יומיים מוזרקת אוטומטית ל-Energy Dashboard בטעינה הראשונה
- **ריענון אוטומטי** של הsession לפני כל קריאת נתונים
- **Re-auth אוטומטי** — כשהטוקן פג, HA מציג התראה לאימות מחדש

## התקנה דרך HACS

1. פתח HACS ב-Home Assistant
2. עבור לـ **Integrations** → תפריט שלוש הנקודות → **Custom repositories**
3. הכנס את כתובת הrepo ובחר **Integration**
4. חפש **MAST Water Meter** והתקן
5. הפעל מחדש את Home Assistant

## הגדרה

1. עבור לـ **Settings → Devices & Services → Add Integration**
2. חפש **MAST Water Meter**
3. הכנס מספר טלפון → קבל SMS עם קוד OTP → הכנס קוד

**לחלופין** — ניתן להכניס JWT token ידנית (דלג על OTP):
- השג טוקן מהדפדפן (Network tab ← `jwt` header)
- הכנס בשדה "טוקן JWT ידני"

## Energy Dashboard

לאחר הגדרה, עבור לـ **Settings → Energy → Water consumption → Add water source** ובחר:
- `mast_water:<מספר_מונה>` — להיסטוריה מלאה (מומלץ)

## דרישות

- Home Assistant 2023.1.0 ומעלה
- חשבון פעיל במאסט עם מונה מים חכם
