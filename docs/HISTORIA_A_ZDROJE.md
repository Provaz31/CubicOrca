# Pôvod implementácie a história práce

Rekonštrukcia podľa lokálnych technických záznamov projektu, doplnená
20. 11. 2026. Historické potvrdenia nižšie sa pri odovzdaní neopakovali.
Odkazy na zdroje identifikujú referencie použité pri vývoji; neoznačujú
aktuálnu kompatibilitu ich dnešných verzií.

## Z čoho projekt vychádza

| Zdroj | Úloha v projekte |
|---|---|
| [OrcaCubic, presný základ](https://github.com/marcodiniz/OrcaCubic/tree/386775d947a4ce43d8637d98ca95e2ef49300c8b) | Skutočný zdrojový základ aplikácie: slicer, GUI, LAN host AnycubicLink, pôvodný webový Workbench a experimentálny CAD. |
| [OrcaSlicer](https://github.com/OrcaSlicer/OrcaSlicer) | Pôvodná rodina aplikácie; `version.inc` v základe uvádza OrcaSlicer v2.5.0-alpha (`3880f2c58`). Nejde o tvrdenie, že vlastný patch pasuje priamo na tento commit OrcaSliceru. |
| Používateľom dodaný funkčný Anycubic Cloud plugin 0.8.3 a starší natívny adaptér | Referencia funkčného protokolu, účtovej výmeny a správania. Dnešný backend je vstavaný worker, nie nasadenie tohto starého pluginu. V kóde zostala pôvodná atribúcia protokolových konštánt. |
| [anycubic-cloud-api](https://github.com/Nino6689/anycubic-cloud-api) | Komunitná referencia endpointov, reportov a príkazových dátových modelov. Nie je to oficiálna garancia API od Anycubic. Vendorové privátne kľúče sa z nej nepreberali. |
| [orders.py – referenčný snapshot](https://github.com/Nino6689/anycubic-cloud-api/blob/5663f5bf33584a5cf9abcdf6ff53acca924934bd/src/anycubic_cloud_api/data_models/orders.py) | Historicky použitý model cloudových príkazov a mapovania. |
| [KX-Bridge](https://github.com/thysson2701/KX-Bridge) | Doplňujúca referencia rozdielu logických nástrojov a fyzických slotov Kobra X. Nie je runtime závislosť vlastného cloudového workera. |
| [AnycubicSlicerNext](https://github.com/ANYCUBIC-3D/AnycubicSlicerNext) | Preskúmaná architektúra a verejné rozhrania. Historické snapshoty: main `6103ed8b511609658d00d0538cc7f0609cdb57da`, develop/WL_Master `987a3c2bf9ed13934137326bfd522896c70e5101`. |
| [Anycubic open-plugins](https://github.com/ANYCUBIC-3D/open-plugins) | Preskúmané rozhranie SDK, snapshot `8743cea620b081ee876130c011d8ab7215d0776f`. Podľa historickej revízie sa jeho kód nekopíroval do vlastnej implementácie. |
| Nainštalovaný AnycubicSlicerNext 2.0.0.3 | Porovnanie dostupných funkcií a modulov. Uzavreté `Workbench.dll`, `cloud_sdk_cpp.dll` ani vendorové privátne kľúče sa nepreberali. |
| [Paho MQTT Python](https://eclipse.dev/paho/files/paho.mqtt.python/html/client.html) | MQTT klient; historická zostava používala už pribalenú verziu 2.1.0. |
| [Agora Web SDK](https://api-ref.agora.io/en/video-sdk/web/4.x/index.html) | Kamerový prehrávač 4.24.9. Konkrétny balík, licencia MIT a integrita sú zaznamenané pri SDK v repozitári. |

Protokolové konštanty aplikácie, verejná CA a verejný RSA kľúč sú odlišné od
účtového tokenu. Zverejnené zdroje neobsahujú používateľovo prihlásenie.
Úplné atribúcie a licencie jednotlivých častí sa musia zachovať.

## Hlavné kroky vývoja

Staré označenia native.6/native.7 patria skoršiemu pluginovému/agentovému
experimentu. Nasledujúce Dev1–Dev16 označujú vlastnú natívnu cloudovú zostavu.
Čísla testov zo starých krokov sú historické; aktuálny výsledok je v STATUS.md.

| Revízia | Čo sa robilo a prečo |
|---|---|
| Skorší prototyp | Overovalo sa napojenie cloudu cez printer agenta, kamera a synchronizácia. Prenos na OrcaCubic ukázal, že LAN Host Type nie je cloudový klient. |
| Dev1 | Vstavaný C++/Python most, samostatný účet chránený DPAPI, import relácie, zoznam tlačiarní a upload do knižnice. Oddelený profil zabránil miešaniu s bežným slicerom. Používateľ potvrdil import a upload. |
| Dev2–Dev3 | Natívny Workbench a postup príprava/potvrdenie štartu aktuálnej platne, mapovanie štyroch slotov a ochrana pred duplicitným/nejasným štartom. |
| Dev4–Dev4d | Cloudový Device pre Kobra X, prenos filamentov do Prípravy, obnova na pozadí, firmvér, teploty, úloha a vrstvy. Opravil sa príjem rozdeleného CRLF v lokálnom OAuth prijímači; používateľ potvrdil Google prihlásenie do OrcaCloudu. To je samostatný účet od Anycubic Cloudu. |
| Dev5–Dev5a | Receive-only MQTT, vek a validácia reportov; následne odstránenie preblikávania pri opakovanom prekresľovaní. Používateľ potvrdil živé údaje aj opravu prekresľovania. |
| Dev6–Dev6b | Natívna kamera, Stop/Start a fullscreen. Po zlyhaniach sa doplnila obmedzená diagnostika a čakanie na prvú snímku pri úvodnom crypt-error. Používateľ potvrdil obraz a ovládanie. |
| Dev7–Dev8 | Teplotné ciele, ventilátor výtlačku a svetlo s kontrolou stavu tlačiarne. Vývojové testy ostali bez fyzických príkazov. |
| Dev9 | Pause/resume/stop viazané na čerstvú identitu konkrétnej úlohy; dvojkrokové review/execute a ochrana pred opakovaním nejasného výsledku. |
| Dev10 | Čítací zoznam cloudovej knižnice so stránkovaním a filtrovanými metadátami; výber položky nič automaticky nespúšťa ani nemaže. |
| Dev11–Dev13 | Kompaktnejší Device, oprava odberov light/fan reportov, prijatie nahláseného typu svetla a jedno priame On/Off tlačidlo. Funkčné svetlo používateľ potvrdil v Dev12. |
| Dev14 | Panel jednotlivých posuvov X/Y/Z a Home. Extrúzia ostala vypnutá pre chýbajúci overený bezpečný kontrakt Kobra X. |
| CAD audit | Oddelene sa preverili existujúce CAD/skicovacie testy a dostupnosť Design. Nešlo o vývoj nového CADu ani dôkaz celého GUI workflow; téma bola následne odložená. |
| Dev15 | Oprava objavenia českého katalógu, tlačový prehľad pri kamere, vek spojenia, filtrovaný náhľad úlohy a lokálny správca kompatibilných ZIPov. |
| Dev16 – 2. 11. 2026 | Po potvrdení pohybu v Dev15 sa odstránili opakované dialógy pohybu. Pribudli uložené voľby Auto Leveling, Resonance Compensation a Flow Calibration pred štartom. |
| Prenos a build cache | Zachovali sa zdroje a staršie balíky, vytvorili návody prenosu. Historický build cache sa 3. 11. presunul na iný disk; nové pracovisko od tejto cesty nezávisí. |
| GitHub – 20. 11. 2026 | Prázdny repozitár sa doplnil overeným Dev16 overlay/patchom, manifestom, licenciami, obnovou zdrojov a kontrolou citlivých údajov. Nové native funkcie sa v tomto kroku nepridávali. |

## Čo historické poznámky neznamenajú

Staršie odseky obsahovali vtedajšie otvorené úlohy, napríklad že kamera alebo
MQTT ešte neexistujú. Neskoršie revízie ich zaviedli. Pri rozhodovaní používaj
aktuálny kód a najnovšiu hranicu overenia, nie prvý starý odsek.
Potvrdenie staršej verzie neoveruje automaticky novú verziu, nový počítač,
iný model tlačiarne ani novú verziu cloudového API.

Podkladmi boli lokálne `README_POKRACOVANIE.md`,
`outputs/Stav_nativnej_integracie.md`, `outputs/Send_Print_technicke_poznamky.md`,
`outputs/OrcaCubic_technicky_rozbor.md`, návod Dev16 a migračné build/baliace
skripty. Pôvodné interné záznamy nie sú súčasťou tohto verejného prenosu;
potrebné technické informácie sú zhrnuté tu bez profilov a účtov.
