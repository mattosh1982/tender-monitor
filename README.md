# Terradron – monitor dronových tendrov

Denný monitoring verejných obstarávaní (inšpekcie, vnútorné inšpekcie, kanalizácia, nádrže, mapovanie) pre DE, AT, CH, CZ, PL, DK, NO, SE, FI, SK, SI.

- `tender_monitor.py` – zber z API (TED, oeffentlichevergabe.de, BZP, simap.ch, voliteľne UVOstat a Doffin)
- `config.json` – kľúčové slová, CPV, krajiny
- `.github/workflows/monitor.yml` – denné spustenie o 04:15 UTC
- `data/latest.json` – nové nálezy posledného behu (číta ich Claude a posiela notifikáciu)
- `data/digest/YYYY-MM-DD.json` – archív
- `data/seen.json` – už nahlásené ID (deduplikácia)

Voliteľné secrets (Settings → Secrets and variables → Actions): `UVOSTAT_API_TOKEN`, `DOFFIN_API_KEY`.
