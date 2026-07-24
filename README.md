# Vacature Radar

Dagelijkse, automatische scan van ~120 organisaties op senior management/directie-vacatures,
met een dashboard dat je als app op je iPhone kunt zetten.

## Hoe het werkt

1. **`scraper.py`** opent elke career-pagina uit `organizations.json` met een headless browser
   (Playwright/Chromium), zodat ook JavaScript-gerenderde vacaturesites goed worden gelezen.
   Het zoekt naar linkteksten met trefwoorden als *directeur, manager, head of, VP, chief,
   senior manager*, etc. (lijst in `scraper.py`, boven aan het bestand).
2. Een **GitHub Action** (`.github/workflows/daily-scan.yml`) draait dit script elke ochtend
   automatisch (06:00 NL-tijd) en slaat het resultaat op in `docs/data/vacatures.json`.
3. **`docs/index.html`** is het dashboard: een mobielvriendelijke webpagina die dat JSON-bestand
   leest en toont, met filters, "nieuw"-badges en directe links naar de vacature.
4. **GitHub Pages** hostet die `docs/`-map gratis als website, die je op je iPhone-beginscherm zet
   zodat het aanvoelt als een app.

Er draait geen server die je zelf hoeft te beheren — GitHub doet het scannen en hosten.

## Eenmalige setup (± 15 minuten)

### 1. GitHub-account
Heb je die nog niet: maak gratis een account op github.com.

### 2. Repository aanmaken
- Klik rechtsboven op **+** → **New repository**.
- Naam bijvoorbeeld `vacature-radar`. Zet 'm op **Public** (nodig voor gratis GitHub Pages,
  tenzij je een betaald account hebt — dan mag ook Private).
- Klik **Create repository**.

### 3. Bestanden uploaden
- Open de repository, klik **Add file → Upload files**.
- Sleep alle bestanden en mappen uit dit pakket erin (let op: ook de verborgen map
  `.github/workflows/daily-scan.yml` moet mee — upload die apart als GitHub 'm niet meepakt met
  drag-and-drop van de hoofdmap).
- Klik **Commit changes**.

### 4. GitHub Pages inschakelen — belangrijk: kies "GitHub Actions"
- Ga naar **Settings → Pages**.
- Bij **Source** (bovenaan, niet "Branch"!): kies **GitHub Actions** — NIET "Deploy from a branch".
  Dat laatste triggert GitHub's eigen Jekyll-bouwproces, dat vastloopt op dit soort platte
  HTML/JSON-sites. Met "GitHub Actions" als source draait alleen onze eigen workflow
  (`pages-deploy.yml`), die Jekyll volledig overslaat.
- Er is geen aparte "Save"-knop nodig; de keuze wordt direct toegepast.

### 5. Eerste keer deployen en scannen
- Ga naar het tabblad **Actions**.
- Kies eerst **Deploy dashboard naar GitHub Pages** links in de lijst → **Run workflow** →
  **Run workflow**. Dit publiceert het (nog lege) dashboard, duurt ~30 seconden.
- Kies daarna **Dagelijkse vacature-scan** → **Run workflow** → **Run workflow**.
  Dit duurt 15–25 minuten (120 sites bezoeken kost tijd, met beleefde pauzes tussen sites).
  Je ziet de voortgang live in de logs.
- Zodra de scan klaar is en de wijziging in `docs/data/` gecommit is, start de
  Pages-deploy-workflow automatisch opnieuw en verschijnt de data op je site:
  `https://<jouw-gebruikersnaam>.github.io/vacature-radar/`
  (deze URL staat ook boven aan **Settings → Pages** zodra de eerste deploy geslaagd is)

Vanaf nu draait dit **elke dag automatisch** vanzelf, zonder dat je iets hoeft te doen.

### 6. Op je iPhone als app zetten
- Open de Pages-URL in **Safari** (moet Safari zijn, niet Chrome).
- Tik op het deel-icoon (vierkant met pijl omhoog) → **Zet op beginscherm**.
- Vanaf nu opent het als een gewone app, met eigen icoon.

## Onderhoud

- **Trefwoorden aanpassen**: pas `SENIOR_KEYWORDS` bovenin `scraper.py` aan als je functies
  mist of te veel ruis krijgt, commit de wijziging — de volgende nachtelijke run gebruikt 'm.
- **Organisaties toevoegen/verwijderen**: bewerk `organizations.json` (naam, url, sector).
- **Handmatig opnieuw scannen**: Actions-tab → Run workflow, wanneer je maar wilt.

## Problemen oplossen

**"pages-build-deployment" faalt met een Jekyll-foutmelding**
Dit betekent dat Settings → Pages nog op **"Deploy from a branch"** staat in plaats van
**"GitHub Actions"**. Zet de Source om naar "GitHub Actions" (zie stap 4 hierboven) — daarna
verschijnt deze legacy-workflow niet meer, en gebruikt Pages uitsluitend onze eigen
`pages-deploy.yml`.

**Dashboard toont "Nog geen data"**
De scan-workflow heeft nog niet succesvol gedraaid. Check het tabblad Actions of
"Dagelijkse vacature-scan" is gelukt (groen vinkje); zo niet, open de run en lees de laatste
regels van de log voor de foutmelding.

**Veel organisaties krijgen een rood "kon niet gecontroleerd worden"-label**
Sommige sites blokkeren bezoekers zonder normale browser-vingerafdruk. Dit is een bekende
beperking (zie hieronder) — bekijk die vacaturepagina's af en toe handmatig.

## Beperkingen — belangrijk om te weten

- **Dit is een heuristiek, geen garantie.** De scraper matcht op linktekst-trefwoorden. Sites die
  vacatures achter een zoekformulier verstoppen (in plaats van directe links) of ongebruikelijke
  functietitels gebruiken, kunnen gemist worden.
- **Sommige sites blokkeren geautomatiseerde bezoekers** (bot-detectie). Die organisaties krijgen
  een rood label "kon niet automatisch gecontroleerd worden" met een link om het handmatig te
  checken.
- **Gratis GitHub-budget**: publieke repositories krijgen onbeperkte Actions-minuten; bij een
  private repo geldt een gratis maandbudget (2.000 minuten), ruim voldoende voor één scan per dag.
- Beschouw dit dashboard als een **eerste filter/signaal**, niet als volledige garantie dat je
  niets mist — bij een belangrijke sollicitatiedeadline blijft een handmatige check verstandig.
