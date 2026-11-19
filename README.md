# CubicOrca

Natívna integrácia Anycubic Cloud pre OrcaCubic/OrcaSlicer, aktuálne Dev16,
zameraná na Anycubic Kobra X. Rozhranie je anglické a české.

## Hotový program pre Windows

**[Stiahnuť aplikáciu Dev16 – otvor Releases → Assets](https://github.com/Provaz31/CubicOrca/releases/tag/Dev.16)**

Vyber `OrcaCubic_Cloud_Dev16_EN_CZ_portable.zip` (približne 231 MB), rozbaľ
**celý ZIP do nového priečinka** a spusti `orca-slicer.exe`. Potrebné DLL,
Python a resources sú pribalené. Samotné EXE ani automatický `Source code (zip)`
nestačia. Pred spustením zavri staršiu verziu. Balík neobsahuje používateľský
účet; na inom počítači môže byť potrebné nové prihlásenie.

Ide o existujúcu vývojovú zostavu Dev16, nie novú kompiláciu. GUI Dev16 a nové
cloudové kalibrácie ešte čakajú na praktické overenie. SHA-256 archívu:
`17cb5e29a775b5dd4bf2b21fc0daacc83e534eff914758c49007f59726a87298`.

**Pre nového vývojára začni tu:** [Odovzdanie projektu](docs/ODOVZDANIE_PROJEKTU.md).
Pôvod implementácie a vývoj verzií opisuje [história a zdroje](docs/HISTORIA_A_ZDROJE.md),
prípravu pracoviska a overenie [zostavenie a testovanie](docs/ZOSTAVENIE_A_TESTOVANIE.md).

Repozitár obsahuje 57 zmenených alebo pridaných zdrojových súborov v `overlay/`
a úplný kumulatívny patch. Celý slicer sa obnovuje nad presným upstream commitom;
samotný priečinok `overlay/` nie je samostatný slicer ani inštalačný balík.
Zdrojový základ je [marcodiniz/OrcaCubic](https://github.com/marcodiniz/OrcaCubic),
commit `386775d947a4ce43d8637d98ca95e2ef49300c8b`.

## Obnovenie úplných zdrojov

Vyžaduje Git, Python 3.12 a internet. V koreňovom priečinku tohto repozitára:

```powershell
python tools/restore_source.py C:/CubicOrca/upstream-source
```

Cieľový priečinok ešte nesmie existovať. Skript overí patch, stiahne presný
zdrojový základ, aplikuje zmeny a overí súčty 57 súborov. Pri zlyhaní ponechá
pracovný priečinok na kontrolu. Aplikáciu ani tlačiareň nespúšťa.
Na úplný Windows build treba navyše MSVC, Windows SDK, CMake, Ninja/gettext
a závislosti OrcaCubicu. Build cache nie je súčasťou repozitára; hotový portable
balík sa poskytuje samostatne v Releases. Nový build z GitHubu zatiaľ nebol vykonaný.

## Offline testy

Na Windows s Pythonom 3.12 a Node.js, z koreňa repozitára:

```powershell
python -I -B overlay/tests/slic3rutils/test_anycubic_cloud_client.py
node overlay/tests/slic3rutils/test_anycubic_camera_viewer.js
python -I -B tools/test_repository_tools.py
```

Testy používajú syntetické účty a náhradné transporty. Testy kamery nepoužijú
sieť ani kameru. Záznam aktuálneho overenia je v [docs/STATUS.md](docs/STATUS.md).

## Kontrola pred commitom

```powershell
git add README.md .gitattributes .gitignore LICENSE.txt source-manifest.json docs tools patches overlay
python -I -B tools/check_secrets.py
git diff --cached --stat
```

Kontrola číta skutočný obsah Git indexu a nevypisuje nájdené hodnoty.
Súkromné profily, prihlásenie, logy, runtime stav a portable ZIPy sem nepatria.
Neposielajte tokeny cez issues. Ignorovanie súborov nenahrádza kontrolu obsahu.

Licencia zdrojov: AGPL-3.0, pozri [LICENSE.txt](LICENSE.txt).
Agora Web SDK má samostatnú licenciu MIT a atribúciu v
`overlay/resources/web/anycubic_camera/AGORA_LICENSE.txt`.
