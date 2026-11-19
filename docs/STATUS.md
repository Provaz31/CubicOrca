# Stav Dev16 – 20. 11. 2026

Pre odovzdanie novému vývojárovi čítaj [Odovzdanie projektu](ODOVZDANIE_PROJEKTU.md),
[Históriu a zdroje](HISTORIA_A_ZDROJE.md) a
[Zostavenie a testovanie](ZOSTAVENIE_A_TESTOVANIE.md).

## Integrácie v zdrojoch

- Anycubic Cloud HTTPS: import prihlásenia, zoznam tlačiarní, nahrávanie G-code,
  príprava/potvrdenie štartu, knižnica a správa úloh.
- MQTT: Paho prijíma stav vybranej tlačiarne, kontroluje čerstvosť správ
  a odlišuje živé údaje od cloudovej snímky. Overovanie TLS ostáva zapnuté.
- Kamera: natívny panel s lokálnym Agora Web SDK 4.24.9, spustenie na kliknutie,
  Stop a fullscreen. Nezapína sa automaticky.
- Device: teploty, ventilátor, svetlo a jednotlivé posuvy osí/Home.
  Ochrany a potvrdenie úmyslu zostávajú v backende; extrúzia je vypnutá.
- Dev16: pohyb na pozadí bez opakovaných dialógov a uložené voľby vyrovnania,
  kompenzácie vibrácií a kalibrácie prietoku pred potvrdeným štartom tlače.
- Vlastný profil `OrcaCubicCloudDev`, EN/CZ, doména gettext `OrcaSlicer`.
  Lokálny správca kompatibilných portable ZIPov; bez automatického sťahovania.

## Hranice overenia

Pri tomto prenose prešlo 212 offline Python testov a 21 testov prehrávača.
Prešlo aj šesť testov nástrojov obnovy a ochrany pred zverejnením prihlasovania.
Patch prešiel reverznou kontrolou proti všetkým 57 aktuálnym lokálnym súborom.
Lokálny checkout je stále na pôvodnom upstream commite a obsahuje 18 upravených
sledovaných a 39 nových súborov. Základ GitHub repozitára obsahoval iba README.

Predchádzajúca lokálna dokumentácia zaznamenáva úspešný Windows Release Dev16
a používateľské potvrdenie kamery/MQTT a pohybu v Dev15. Tento prenos tieto živé
funkcie opakovane netestoval. GUI Dev16, Home a skutočné vykonanie nových
kalibrácií nemajú dodatočné potvrdenie. Nová kompilácia celého slicera ani úplné
stiahnutie zdrojového základu sa pri prenose nevykonali. CAD/Design sa nemenil.

## Obsah určený na zdieľanie

Prenesené sú iba zdroje, testy, kumulatívny patch, licencie a nástroje obnovy.
Vylúčené sú používateľské profily, šifrované aj nešifrované prihlasovanie,
logy, tlačové projekty, build cache a hotové portable balíky.

`protocol.py` obsahuje spoločné identifikátory a podpisové konštanty aplikácie;
nejde o token používateľa. `mqtt_trust.py` obsahuje verejnú CA a verejný RSA
kľúč, nie privátny kľúč či klientsky certifikát. Testy používajú syntetické
hodnoty. Agora SDK je zachované s pôvodnou atribúciou a SHA-256.

Kontrola pred odoslaním číta všetky indexované súbory vrátane odstraňovaných
riadkov patchu a hľadá JWT, privátne kľúče, známe formáty prístupových tokenov,
prihlasovacie URL, citlivé názvy súborov a vybrané credential literály.
Ide o cielenú kontrolu známych vzorov; nenahrádza revíziu zmien.

## Hotový portable program

Existujúci Dev16 je priložený v
[Releases → Dev.16](https://github.com/Provaz31/CubicOrca/releases/tag/Dev.16).
Archív `OrcaCubic_Cloud_Dev16_EN_CZ_portable.zip` má 230 947 309 bajtov
a 18 664 položiek. Opakovaná kontrola CRC, hashov binárok a natívnych modulov
prešla. Kontrola názvov súkromných/runtime súborov, JWT, privátnych PEM kľúčov
a známych GitHub/AWS tokenových vzorov v obsahu archívu nenašla nález.
SHA-256: `17cb5e29a775b5dd4bf2b21fc0daacc83e534eff914758c49007f59726a87298`.
Táto kontrola nepridáva nové GUI ani živé overenie a nejde o novú kompiláciu.
