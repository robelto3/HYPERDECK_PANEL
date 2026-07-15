# HyperDeck Panel

Webový ovládací panel pro rekordéry Blackmagic HyperDeck, určený především pro
provoz na Raspberry Pi v uzavřené produkční síti.

Panel umožňuje:

- sledovat stav, vstupní formát, timecode a kapacitu médií jednotlivých decků,
- spouštět a zastavovat záznam jednotlivě i hromadně,
- porovnávat timecode aktivních rekordérů,
- měnit základní nastavení decků a jejich timecode vstup,
- zobrazit externí ATEM logger,
- formátovat slot 1 vybraných HyperDecků.

## Požadavky

- Raspberry Pi nebo jiný Linux se systémovým Pythonem 3,
- Chromium pro kiosk režim,
- síťové spojení s HyperDecky,
- dostupný HyperDeck Ethernet Protocol na TCP portu `9993`.

Aplikace používá pouze standardní knihovnu Pythonu a nepotřebuje instalaci
balíčků z internetu.

## Ruční spuštění

```bash
cd /home/hyperdeck/hyperdeck-panel
python3 app.py
```

Webové rozhraní poté poslouchá na všech síťových rozhraních na portu `8080`:

```text
http://IP_RASPBERRY_PI:8080/
```

## Konfigurace

- `config.json` obsahuje adresu panelu a ATEM loggeru, nastavení porovnávání
  timecodu a seznam rekordérů.
- `advanced_nvm.json` obsahuje nastavení formátovacích zařízení a očekávaný
  videoformát jednotlivých decků.

Konfigurace se běžně spravuje přes stránky **Settings** a **Advanced**. Před
větší ruční úpravou nebo přesunem systému oba JSON soubory zazálohujte.

## Systemd služba

Přiložená služba používá projekt v adresáři
`/home/hyperdeck/hyperdeck-panel` a systémový `/usr/bin/python3`.

Instalace nebo aktualizace služby:

```bash
cd /home/hyperdeck/hyperdeck-panel
sudo cp deploy_rpi5/hyperdeck-panel.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now hyperdeck-panel.service
```

Běžná správa:

```bash
sudo systemctl restart hyperdeck-panel.service
sudo systemctl status hyperdeck-panel.service
journalctl -u hyperdeck-panel.service
```

Po změně `app.py` je nutné službu restartovat a stránku v prohlížeči obnovit.

## Kiosk prohlížeč

Adresář `deploy_rpi5` obsahuje desktopové soubory pro spuštění Chromia v kiosk
režimu a doporučenou Chromium policy pro stahování EDL souborů. Cesty a síťové
adresy v těchto souborech před nasazením ověřte podle cílového systému.

## Automatické testy

Kompletní testovací sada se spouští příkazem:

```bash
cd /home/hyperdeck/hyperdeck-panel
python3 -B -m unittest discover -s tests -v
```

Testy používají simulované odpovědi a neposílají příkazy skutečným HyperDeckům.
Nemění ani provozní JSON konfiguraci.

## Diagnostika spojení

Skript `diag_hyperdeck_link.py` opakovaně kontroluje ping, TCP spojení a odpovědi
HyperDeck protokolu. Cílová IP adresa a parametry běhu jsou zatím nastavené
přímo na začátku skriptu.

Spuštění:

```bash
python3 diag_hyperdeck_link.py
```

Diagnostiku lze ukončit pomocí `Ctrl+C`.

## Bezpečnost

Panel používá CSRF token pro všechny měnící HTTP požadavky. Nemá však uživatelské
přihlášení a poslouchá na `0.0.0.0:8080`. Provozujte jej pouze v důvěryhodné síti
a podle potřeby omezte přístup k portu `8080` firewallem.

Po restartu aplikace se vytvoří nový CSRF token. Dříve otevřenou stránku je proto
nutné obnovit, jinak server její ovládací požadavky odmítne.

## Varování k formátování

Funkce **FORMAT HD MINI** a **FORMAT 4K Pro** formátují slot 1 jako exFAT a
nevratně smažou obsah vložené karty. Před potvrzením vždy zkontrolujte vybrané
zařízení, jeho IP adresu a obsah média.
