# Príprava pracoviska, zostavenie a testovanie

Odovzdávací postup k Dev16, 20. 11. 2026. Offline sady a zhodnosť prenesených
zdrojov boli overené. Celý čistý build podľa nasledujúcej prenosnej šablóny
sa na novom počítači ešte nevykonal; historický úspešný build používal lokálne
pripravené závislosti. Rozdiel je dôležitý pri riešení chýb prostredia.

## 1. Zdrojové priečinky

Odporúčané rozloženie mimo OneDrive a iných synchronizovaných priečinkov:

```text
C:/CubicOrca/
  integration/       tento repozitár s overlay/patchom/dokumentáciou
  upstream-source/   úplný zdrojový strom obnovený skriptom
  deps-build/        zostavovanie závislostí
  deps-install/      nainštalované závislosti
  build/             zostavovanie aplikácie
  stage/             staging novej zostavy
```

Príkazy pre PowerShell, s Gitom a Pythonom 3.12 dostupnými v PATH:

```powershell
git clone https://github.com/Provaz31/CubicOrca.git C:/CubicOrca/integration
Set-Location C:/CubicOrca/integration
python tools/restore_source.py C:/CubicOrca/upstream-source
```

Obnovovací skript odmietne existujúci cieľ. Pri zlyhaní ho nechá na diagnostiku;
nespúšťaj ho opakovane s rovnakým cieľom bez preverenia obsahu. Patch sa overuje
pred sťahovaním, výsledné zmenené súbory po aplikácii. Overenie zdrojov ešte
nedokazuje, že na novom počítači fungujú kompilátor a všetky závislosti.

## 2. Najprv offline testy

Z `C:/CubicOrca/integration` na Windows:

```powershell
python -I -B overlay/tests/slic3rutils/test_anycubic_cloud_client.py
node overlay/tests/slic3rutils/test_anycubic_camera_viewer.js
python -I -B tools/test_repository_tools.py
```

Výchozí Dev16: 212 Python testov, 21 JS kontrol kamery a 6 testov odovzdávacích
nástrojov. Testy používajú syntetické údaje a náhradné transporty. Python sada
využíva aj Windows DPAPI/CNG so syntetickými údajmi, preto ju nezamieňaj za
platformovo nezávislý dôkaz. Výstupné `.test/` a iné testovacie pracovné
priečinky nepridávaj do commitu.

Po zmene kódu v kompletnom strome spusti jeho zodpovedajúce testy z
`upstream-source/tests/slic3rutils/`; testovanie starej kópie v overlay by
novú zmenu neoverilo. Natívne Catch2 testy vyžadujú zostavené C++ závislosti.
Pravidlá a registrácia sú v upstream `tests/AGENTS.md` a `tests/slic3rutils/CMakeLists.txt`.

## 3. Nástroje pre Windows build

Historicky úspešné prostredie Dev16:

| Časť | Použitá konfigurácia |
|---|---|
| Platforma | Windows x64, Release |
| Kompilátor | Visual Studio Community 2026, MSVC 14.51 |
| SDK | Windows SDK 10.0.26100.0 |
| Zostavovanie | CMake, Ninja, gettext/msgfmt |
| Python aplikácie | Zabalený CPython zo závislostí; presnú verziu určuje CMake zdrojového základu. |
| MQTT runtime | Paho MQTT 2.1.0 v historickom portable runtime. |
| Kamera | Pribalené Agora Web SDK 4.24.9 s overeným hashom. |

Vo Visual Studio Installer musí byť vývoj desktopových aplikácií v C++ a
Windows SDK. Pre Ninja použi vývojové prostredie x64 s nastaveným MSVC/SDK,
napríklad Developer PowerShell for VS; samotné pridanie `cl.exe` do PATH nestačí.
Závislosti môžu vyžadovať ďalšie nástroje podľa upstream `deps/` a jeho
Windows build návodu. Starý `BUILD_WIN.md` obsahuje aj cesty autora upstreamu;
nepreberaj ich ako platné cesty nového počítača.

## 4. Závislosti a konfigurácia

Najspoľahlivejšie je zostaviť závislosti z `upstream-source/deps` tým istým
kompilátorom a s rovnakým `SLIC3R_CAD` ako aplikáciu. Upstream CMake pridáva
pod `DESTDIR` podadresár `usr/local`; hlavný projekt potom dostane tento prefix.
Šablóna vychádza z preskúmaného `deps/CMakeLists.txt`:

```powershell
$projectRoot = 'C:/CubicOrca'
$sourceRoot = "$projectRoot/upstream-source"
$dependencyPrefix = "$projectRoot/deps-install/usr/local"
cmake -S "$sourceRoot/deps" -B "$projectRoot/deps-build" -G Ninja `
  '-DCMAKE_BUILD_TYPE=Release' '-DSLIC3R_CAD=ON' `
  "-DDESTDIR=$projectRoot/deps-install"
# Pokračuj iba po úspešnej konfigurácii.
cmake --build "$projectRoot/deps-build" --parallel 1
```

Pôvodný súkromný archív už zostavených knižníc nie je na GitHube. Ak sa prenáša
osobitne, môže mať v exportoch absolútne cesty starého počítača. Pri Dev16 sa
museli lokálne opraviť prefixy wxWidgets, wxInspector, OpenCV a libpng.
Nekompatibilný kompilátor alebo presunutý prefix rieš opätovným buildom;
neupravuj náhodne závislosti tak, aby konfigurácia iba prestala hlásiť chyby.

Hlavný projekt používaj s týmito hodnotami:

```powershell
cmake -S $sourceRoot -B "$projectRoot/build" -G Ninja `
  '-DCMAKE_BUILD_TYPE=Release' "-DCMAKE_PREFIX_PATH=$dependencyPrefix" `
  "-DDEP_BUILD_DIR=$projectRoot/deps-build" `
  "-DCMAKE_INSTALL_PREFIX=$projectRoot/stage" `
  '-DORCA_UI_LANGUAGES=en;cs' '-DORCA_CUSTOM_BUILD=ON' `
  '-DSLIC3R_APP_NAME=OrcaCubic Cloud Dev' '-DSLIC3R_APP_KEY=OrcaCubicCloudDev' `
  '-DSoftFever_VERSION=2.5.0-accloud.dev3' `
  '-DSLIC3R_CAD=ON' '-DSLIC3R_GUI=ON' '-DSLIC3R_STATIC=ON' `
  '-DORCA_TOOLS=ON' '-DBUILD_TESTS=OFF'
# Pokračuj iba po úspešnej konfigurácii.
cmake --build "$projectRoot/build" `
  --target gettext_po_to_mo OrcaSlicer_app_gui COPY_DLLS --parallel 4
```

Toto sú zaznamenané voľby Dev16 a prenosná šablóna, nie nový overený build.
Pri požiadavke na C++ testy nastav `BUILD_TESTS=ON`, zostav príslušné testové
targety a riaď sa upstream testovacím návodom. Na kontrolu prekladov slúži:

```powershell
msgfmt --check --check-format -o "$projectRoot/OrcaSlicer_cs.mo" `
  "$sourceRoot/localization/i18n/cs/OrcaSlicer_cs.po"
```

Zabalený Python je runtime požiadavka, nie iba pomocný build nástroj. CMake
hľadá jeho presnú verziu v závislostiach; náhodný systémový Python nemusí
nahradiť embed knižnicu ani runtime balík. Samostatne over, že vo výslednom
runtime existuje `paho.mqtt.client`. Neúspešný import neopravuj tichým
sťahovaním balíkov počas spustenia aplikácie.

## 5. Kontrola hotovej aplikácie a balenie

Úspešné vytvorenie `orca-slicer.exe` nestačí. Over potrebné DLL, zabalený Python,
vlastné `resources/scripts`, kamerové HTML/CSS/JS/SDK, český `OrcaSlicer.mo`
a revízny manifest `OrcaCubicCloudBuild.json` v správnom umiestnení balíka.
Po zostavení skontroluj GUI v EN/CZ a ukončenie workerov pri zatvorení/zmene výberu.

Historický Dev16 sa balil z overeného Dev15 ZIPu výmenou binárok, českého
katalógu, modulu tlače a manifestu. Tento základný ZIP ani pôvodný baliaci skript
nie sú v repozitári. Samotný CMake build preto nevydávaj za totožný overený
portable balík. Nové balenie potrebuje vlastný overený staging a súpis runtime
súborov; neprepisuj ním existujúcu funkčnú zostavu.

Pred zdieľaním balíka skontroluj CRC/SHA, zhody modulov a neprítomnosť účtu,
profilu, logov, pripravených úloh a operačných zámkov. Novú revíziu rozbaľuj do
nového priečinka. Uchovaj možnosť návratu na predchádzajúci funkčný balík.

## 6. Odovzdanie ďalšej zmeny

Aktualizuj overlay, kumulatívny patch, manifest a dokumentáciu spoločne, potom
over obnovu do čistého stromu. Na tomto integračnom repozitári:

```powershell
git add README.md .gitattributes .gitignore LICENSE.txt source-manifest.json docs tools patches overlay
python -I -B tools/check_secrets.py
git diff --cached --stat
```

Zastav sa pri náleze a preskúmaj ho bez vypísania hodnoty. Kontrola nie je
univerzálny detektor každého tajomstva. Výsledok odovzdaj s presným commitom,
verziami nástrojov a oddelenými výsledkami offline, GUI a živých testov.
