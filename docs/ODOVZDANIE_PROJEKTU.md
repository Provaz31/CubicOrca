# CubicOrca – odovzdanie projektu ďalšiemu vývojárovi

Stav k 20. 11. 2026, funkčný základ Dev16. Dokumentácia je určená človeku,
ktorý nemá pôvodnú konverzáciu ani prístup k pôvodnému počítaču.

## 1. Čo je cieľom

Cieľom je používať Anycubic Kobra X priamo z prostredia OrcaSliceru:
prihlásenie do Anycubic Cloudu, výber tlačiarne, prenos filamentov do Prípravy,
odoslanie naslicovanej platne, sledovanie tlače, kamera a vybrané ovládanie.
Používateľ preferuje natívne rozhranie, jednoduché ovládanie a EN/CZ.
Slovenčina zatiaľ nie je súčasťou zostavy.

Názvy sa ľahko zamieňajú:

| Názov | Význam |
|---|---|
| OrcaSlicer | Pôvodná rodina slicera, z ktorej vychádza zdrojový základ. |
| OrcaCubic | Upstream fork od marcodiniz; poskytol slicer, LAN integráciu a ďalšie funkcie. |
| CubicOrca | Tento používateľský projekt a repozitár s vlastnými cloudovými zmenami. |
| OrcaCubic Cloud Dev | Meno vlastnej zostavy v aplikácii. |
| OrcaCubicCloudDev | Oddelený dátový/profilový kľúč vlastnej zostavy. |

## 2. Čo presne sa odovzdáva

[Repozitár Provaz31/CubicOrca](https://github.com/Provaz31/CubicOrca)
obsahuje zdrojovú nadstavbu nad OrcaCubic, nie celý upstream fork.
Import Dev16 je zachytený commitom
[`cabf7d9`](https://github.com/Provaz31/CubicOrca/commit/cabf7d98949957bdbd26b47b82d946987d13e557).

- `overlay/`: 57 úplných zmenených/pridaných súborov s pôvodnými relatívnymi cestami.
- `patches/dev16.patch`: kumulatívny rozdiel vrátane nových súborov proti presnému základu.
- `source-manifest.json`: adresa upstreamu, commit a kontrolné súčty patchu/súborov.
- `tools/restore_source.py`: stiahne presný základ do nového priečinka a aplikuje patch.
- `tools/check_secrets.py`: preverí obsah Git indexu pred odoslaním.
- `docs/`: táto odovzdávacia dokumentácia a stav overenia.

Zdrojový základ je výhradne commit
`386775d947a4ce43d8637d98ca95e2ef49300c8b` z
[marcodiniz/OrcaCubic](https://github.com/marcodiniz/OrcaCubic/tree/386775d947a4ce43d8637d98ca95e2ef49300c8b).
Patch neaplikuj priamo na dnešnú vetvu main alebo na čistý oficiálny OrcaSlicer.
Najprv obnov Dev16, až potom rieš samostatný upstream merge/rebase.

Hotová aplikácia s EXE/DLL a runtime je samostatne v
[Releases → Dev.16 → Assets](https://github.com/Provaz31/CubicOrca/releases/tag/Dev.16)
ako `OrcaCubic_Cloud_Dev16_EN_CZ_portable.zip`. Rozbaľ celý archív do nového
priečinka a spusti `orca-slicer.exe`. Ide o historický build Dev16, nie novú
kompiláciu; hranice živého a GUI overenia platia aj pre tento balík.
Knižnice kompilátora, build závislosti, build cache a používateľské účty nie sú
na GitHube. Príjemca vie preskúmať vlastné zdroje, spustiť offline testy aj
vyskúšať portable aplikáciu; úplné zostavenie vyžaduje ďalšie nástroje.

## 3. Ako je implementácia rozdelená

Tok natívnej cloudovej integrácie:

```text
Natívne C++ GUI / výber platne
    -> AnycubicCloudService
    -> izolovaný zabalený Python proces cez súkromné stdin/stdout
    -> anycubic_cloud.worker
       -> HTTPS klient: účet, knižnica, upload a potvrdené príkazy
       -> MQTT prijímač: živé reporty vybranej tlačiarne
       -> kamerová relácia -> lokálny Agora prehrávač vo WebView
```

Ide o vstavanú službu aplikácie. Aktuálny cloudový worker sa neregistruje ako
Python printer plugin a nevystavuje lokálny HTTP server. Upstream LAN démon
a jeho webový Workbench sú iná cesta; ich existenciu nepovažuj za cloudové API.
Ani zmena Host Type/API Key/Device UI sama osebe nenahrádza cloudovú integráciu.

### Mapa miest, kde začať čítať kód

Nasledujúce cesty sú relatívne ku kompletnému obnovenému stromu; v tomto
repozitári sú zmenené verzie pod rovnakou cestou v `overlay/`.

| Oblasť | Kľúčové súbory |
|---|---|
| Účet, Device, tlačový dialóg a ovládanie | `src/slic3r/GUI/AnycubicCloudAccountDialog.cpp/.hpp` |
| Zapojenie do slicera | `GUI/MainFrame.cpp`, `GUI/Plater.cpp`, `GUI/GUI_App.cpp` pod `src/slic3r/` |
| Natívny kamerový panel/fullscreen | `src/slic3r/GUI/AnycubicCloudCamera.cpp/.hpp` |
| Procesný most a zrušenie operácie | `src/slic3r/Utils/AnycubicCloudService.cpp/.hpp`, `AnycubicCloudReadTask.hpp` |
| Vstup a validácia worker metód | `resources/scripts/anycubic_cloud_worker.py`, `anycubic_cloud/worker.py` |
| HTTPS, protokol a šifrované prihlásenie | `anycubic_cloud/client.py`, `protocol.py`, `session_store.py`, `_windows_secret.py` |
| Normalizácia tlačiarne a MQTT | `anycubic_cloud/device.py`, `monitor.py`, `mqtt_trust.py`, `_windows_rsa.py` |
| Upload/príprava/štart a mapovanie | `anycubic_cloud/printing.py`, `src/slic3r/GUI/AnycubicFilamentMatch.hpp` |
| Teploty, svetlo/ventilátor, pohyb | `anycubic_cloud/temperature.py`, `peripherals.py`, `motion.py` |
| Pause/resume/stop a knižnica | `anycubic_cloud/job_control.py`, `library.py`, `job_preview.py` |
| Kompatibilné ZIP aktualizácie | `anycubic_cloud/portable_update.py` |
| Kamerový prehrávač a SDK | `resources/web/anycubic_camera/` |
| Testy cloudového klienta a kamery | `tests/slic3rutils/test_anycubic_cloud_client.py`, `test_anycubic_camera_viewer.js` |
| EN/CZ, meno zostavy | `CMakeLists.txt`, `version.inc`, `localization/i18n/cs/OrcaSlicer_cs.po` |

Skrátené cesty `anycubic_cloud/…` v tabuľke začínajú v
`resources/scripts/anycubic_cloud/`. Označenie Dev16 je revízia balíka;
historická hodnota jadra `2.5.0-accloud.dev3` nie je chyba v číslovaní Dev16.

## 4. Dôležité správanie, ktoré treba zachovať

### Štart tlače a ovládanie

Upload do cloudovej knižnice ešte nie je štart tlače. Príprava vytvorí nemennú
kópiu G-code, overí zariadenie a mapovanie, uloží jednorazovú pripravenú úlohu.
Až potvrdený štart spotrebuje túto úlohu. Nejasný výsledok nesmie automaticky
viesť k zopakovaniu fyzického príkazu.

Podpora mapovania je zameraná na štyri vstavané sloty Kobra X. Identita boxu
`-1`, logický `paint_index` z nástroja G-code a fyzický `ams_index` 0–3 majú
odlišný význam. Pri G-code používajúcom T0/T3 ich nesmieš prečíslovať na T0/T1.
Externé ACE, virtuálne/miešané sloty a iné modely nemožno považovať za podporené.

V Dev16 kliknutie na konečný posuv alebo Home autorizuje jeden príkaz bez
opakovaného dialógu. Odosielanie beží na pozadí, ostatné ovládanie je dočasne
blokované a príkaz sa automaticky neopakuje. Extrúzia je zámerne vypnutá.
Kalibračné voľby sa ukladajú do pripravenej úlohy a štart používa tento uložený
výber, nie dodatočne zmenené parametre.

### Účet, údaje a kamera

Prihlásenie je chránené Windows DPAPI. Perzistencia na macOS/Linux nemá hotový
ekvivalent; neoznačuj projekt za overený multiplatformový produkt. Šifrovaný
súbor z jedného Windows účtu/počítača nemusí fungovať na inom. Nový vývojár
použije vlastný účet cez rozhranie aplikácie, nie autorov profil alebo token.

Worker dostáva požiadavku cez súkromnú rúru. Citlivé údaje nepatria do argumentov
procesu, prostredia, webových správ, logov ani výnimiek pre používateľa.
MQTT monitor iba prijíma; ovládanie ide oddelenými potvrdenými cloudovými
operáciami. Hodnoty MQTT expirujú po 60 sekundách. Čas načítania z cloudu
nie je časom merania tlačiarne a neznáma hodnota sa nesmie nahradiť nulou.

Kamera sa otvára na výslovné kliknutie. SDK/prehrávač sú overené v predchádzajúcej
zostave; bez dôvodu ich nemeň. Pri Stop, zmene zariadenia alebo zatvorení treba
ukončiť vlastnú reláciu a ignorovať oneskorené výsledky. SDK nevysiela lokálne médiá.

### Profil, jazyk a aktualizácie

Zachovaj `SLIC3R_APP_KEY=OrcaCubicCloudDev` a doménu prekladov `OrcaSlicer`.
Nezamieňaj názov katalógu s názvom profilu; práve to predtým skrylo češtinu.
`ORCA_CUSTOM_BUILD=ON` oddeľuje aktualizácie vlastnej zostavy od bežného OrcaSliceru.
Správca ZIPov overuje kompatibilitu a rozbaľuje do nového priečinka. Automatické
sťahovanie, zlúčenie upstreamu a zostavenie novej verzie zatiaľ nerobí.
SHA-256 manifest nie je kryptografický podpis vydavateľa.

## 5. Čo bolo naozaj overené

| Oblasť | Dôkaz a hranica |
|---|---|
| Aktuálny prenos zdrojov | Zhodných 57 súborov, reverzná/aplikačná kontrola patchu a pôvodné Git blob hashe 18 upravených súborov. |
| Offline klient | 212 Python testov, opakované 20. 11. 2026 bez skutočného účtu a tlačiarne. |
| Kamerový prehrávač | 21 izolovaných JS kontrol, bez siete a bez otvorenia kamery. |
| Odovzdávacie nástroje | 6 testov kontroly citlivých údajov a ochrany cieľového priečinka. |
| Windows Release Dev16 | Úspešný historický lokálny build podľa záznamov; nový build z GitHubu sa pri prenose nerobil. |
| Účet/upload/filamenty | Historická dokumentácia zaznamenáva používateľské potvrdenia v skorších revíziách. |
| MQTT/kamera/pohyb | Historicky potvrdené používateľom, pohyb v Dev15; nejde o nový živý test Dev16. |
| Svetlo | Historicky potvrdená oprava Dev12; neskoršie UI zmeny majú samostatnú hranicu overenia. |
| GUI Dev16, Home, cloudové kalibrácie | Bez dodatočného praktického potvrdenia. |
| Náhľad úlohy, knižnica, nový správca ZIPov | Implementované a offline overované; chýba úplné aktuálne živé/GUI potvrdenie. |

Prijatie príkazu cloudom nie je dôkaz fyzického vykonania. Pôvodný slicer má
aj experimentálny Design/CAD, ale cloudové odovzdanie ho nerozvíja. Pri tomto
prenose sa CAD kód nemenil.

## 6. Odporúčaný postup pokračovania

1. Prečítaj tento dokument, [históriu a zdroje](HISTORIA_A_ZDROJE.md)
   a [zostavenie a testovanie](ZOSTAVENIE_A_TESTOVANIE.md).
2. Naklonuj repozitár, spusti tri offline sady a obnov kompletné zdroje do
   nového priečinka. Zaznamenaj verzie nástrojov a výsledky; až potom upravuj kód.
3. Obnov Windows Release v samostatnom build priečinku. Prvý cieľ je reprodukcia
   existujúceho Dev16, nie súčasný upgrade upstreamu a pridávanie funkcií.
4. Over GUI v EN/CZ: výber tlačiarne, rozloženie pri rôznych DPI, jazyk, skrytie
   Device, zmena profilu a ukončenie aplikácie bez zvyšných workerov/okien.
5. S vlastníkom tlačiarne naplánuj živý test Dev16 a kalibrácií. Home overuj
   samostatne až po kontrole aktuálneho fyzického stavu a voľnej podložky.
6. Ďalšie modely, ACE, extrúziu alebo automatické aktualizácie rieš ako oddelené
   úlohy s doloženým protokolom a testami. Bez overenia neodhaduj príkazy.

Pri vývoji nepoužívaj reálny účet na automatické testy a nezapínaj kameru ani
fyzické ovládanie automaticky. Po každom zásahu rozlišuj offline test, skutočný
GUI test a vlastníkom potvrdený živý výsledok.

## 7. Ako udržiavať tento repozitár

Kompletný obnovený strom je pracovisko pre kompiláciu. Tento repozitár je
odovzdávacia nadstavba. Ak zmeníš iba `overlay/`, obnovovací skript tieto zmeny
nepoužije: aplikuje patch. Pred odoslaním musia byť súčasne aktuálne **overlay,
kumulatívny patch aj manifest**.

Pri novej revízii zachovaj pôvodný základ, zahrň aj nové sledované súbory do
kumulatívneho rozdielu, prenes ich do overlay a obnov SHA-256 manifestu.
Patch zapisuj ako pôvodné bajty z Gitu, nie cez textové presmerovanie starého
Windows PowerShellu. Over obnovu do čistého stromu a zhodu všetkých súborov.
Nemeň funkčný zdrojový základ bez samostatného rozhodnutia o migrácii.

Pred commitom spusti `tools/check_secrets.py` nad pripraveným Git indexom a
skontroluj samotný diff. Zálohy účtu, celý AppData profil, logy, dočasné tlačové
úlohy a nepreverené archívy nikdy nepridávaj. Pôvodné lokálne zdroje a balíky
zachovaj; staré nepoužiteľné build cesty nie sú dôvod na reset rozpracovanej práce.
