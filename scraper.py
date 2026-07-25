#!/usr/bin/env python3
"""
Vacature-monitor scraper (async, met begrensde concurrency).

Bezoekt elke organisatie-URL uit organizations.json met een headless browser
(zodat JavaScript-gerenderde vacaturesites ook worden gelezen), zoekt naar
links/teksten die op senior-management of directieniveau wijzen, en schrijft
het resultaat naar docs/data/vacatures.json.

Belangrijke robuustheidskeuzes:
- Meerdere sites tegelijk (begrensde concurrency) i.p.v. na elkaar -> veel sneller,
  en één trage site blokkeert de rest niet.
- Harde tijdslimiet (MAX_RUNTIME_SECONDS): het script rondt altijd af binnen die tijd,
  ook als er nog organisaties niet gescand zijn -> nooit een oneindige/hangende run.
- Tussentijds committen + pushen naar git, niet pas aan het eind -> als de workflow
  toch een keer stopt (timeout, gecrashte runner), is de tot dan toe verzamelde data
  al opgeslagen in de repository, niet verloren.
"""

import asyncio
import json
import re
import subprocess
import time
import hashlib
import datetime
from pathlib import Path
from urllib.parse import urljoin

from playwright.async_api import async_playwright

ROOT = Path(__file__).parent
ORG_FILE = ROOT / "organizations.json"
DATA_FILE = ROOT / "docs" / "data" / "vacatures.json"
LOG_FILE = ROOT / "docs" / "data" / "scan-log.json"

MAX_CONCURRENCY = 4          # aantal sites tegelijk (verlaagd voor stabiliteit op CI-runners)
NAV_TIMEOUT_MS = 20000       # max wachttijd om een pagina te laden
NETWORKIDLE_TIMEOUT_MS = 5000
EXTRA_WAIT_MS = 1200
MAX_RUNTIME_SECONDS = 45 * 60   # harde bovengrens voor de hele scan: altijd afronden
COMMIT_EVERY = 15               # tussentijds committen na elke N afgeronde organisaties

# Trefwoorden die duiden op senior management / directieniveau (NL + EN)
SENIOR_KEYWORDS = [
    r"directeur", r"directrice", r"director", r"managing director",
    r"algemeen directeur", r"general manager", r"chief\b", r"\bcio\b",
    r"\bcfo\b", r"\bcoo\b", r"\bceo\b", r"\bcto\b", r"\bcpo\b",
    r"hoofd\s", r"head of", r"vice[\s-]?president", r"\bvp\b",
    r"senior manager", r"senior[- ]?management", r"executive director",
    r"bestuurder", r"bestuurslid", r"raad van bestuur", r"leidinggevende",
    r"teamleider senior", r"programmadirecteur", r"portfoliodirecteur",
    r"business unit director", r"unit director", r"regiodirecteur",
    r"partner\b", r"principal\b",
]
SENIOR_RE = re.compile("|".join(SENIOR_KEYWORDS), re.IGNORECASE)

EXCLUDE_KEYWORDS = [
    r"assistent", r"stagiair", r"intern\b", r"trainee", r"junior",
    r"medewerker klantenservice", r"secretaresse van de directeur",
]
EXCLUDE_RE = re.compile("|".join(EXCLUDE_KEYWORDS), re.IGNORECASE)

# --- Locatiefilter: alleen Nederlandse vacatures tonen -----------------------
# Vooral relevant bij internationale organisaties (Shell, Vopak, Boskalis,
# Trafigura, TotalEnergies, etc.) die wereldwijde vacatureoverzichten hebben.

NL_LOCATION_KEYWORDS = [
    r"nederland", r"netherlands", r"the netherlands", r"dutch\b", r"\bnl\b",
    r"holland", r"randstad",
    r"rotterdam", r"amsterdam", r"den haag", r"the hague", r"'s-gravenhage",
    r"utrecht", r"groningen", r"moerdijk", r"terneuzen", r"vlissingen",
    r"ijmuiden", r"schiphol", r"eindhoven", r"breda", r"tilburg", r"nijmegen",
    r"arnhem", r"zwolle", r"leeuwarden", r"assen", r"maastricht", r"zoetermeer",
    r"rijswijk", r"zaandam", r"alkmaar", r"haarlem", r"dordrecht", r"gouda",
    r"delft", r"leiden", r"apeldoorn", r"almere", r"hoorn", r"den helder",
    r"vlaardingen", r"schiedam", r"spijkenisse", r"capelle aan den ijssel",
    r"barendrecht", r"westland", r"noord-holland", r"zuid-holland",
    r"noord-brabant", r"gelderland", r"overijssel", r"flevoland", r"drenthe",
    r"friesland", r"limburg\b", r"zeeland",
]
NL_LOCATION_RE = re.compile("|".join(NL_LOCATION_KEYWORDS), re.IGNORECASE)

FOREIGN_LOCATION_KEYWORDS = [
    r"united states", r"\busa\b", r"u\.s\.a?\.?\b",
    r"united kingdom", r"\buk\b(?!raine)", r"england", r"scotland", r"london",
    r"germany", r"deutschland", r"hamburg", r"berlin", r"munich", r"frankfurt",
    r"france", r"paris", r"belgium", r"brussels", r"antwerp\b",
    r"spain", r"madrid", r"barcelona", r"italy", r"milan", r"rome",
    r"poland", r"warsaw", r"norway", r"oslo", r"sweden", r"stockholm",
    r"denmark", r"copenhagen", r"finland", r"helsinki",
    r"switzerland", r"geneva", r"zurich", r"austria", r"vienna",
    r"portugal", r"lisbon", r"ireland", r"dublin",
    r"singapore", r"hong kong", r"china\b", r"shanghai", r"beijing",
    r"india\b", r"mumbai", r"delhi", r"bangalore",
    r"japan", r"tokyo", r"south korea", r"seoul",
    r"brazil", r"sao paulo", r"canada", r"toronto", r"mexico",
    r"australia", r"sydney", r"melbourne",
    r"dubai", r"abu dhabi", r"\buae\b", r"saudi arabia", r"qatar", r"kuwait",
    r"nigeria", r"angola", r"egypt", r"south africa",
    r"russia", r"turkey", r"istanbul",
    r"czech republic", r"romania", r"hungary", r"greece",
]
FOREIGN_LOCATION_RE = re.compile("|".join(FOREIGN_LOCATION_KEYWORDS), re.IGNORECASE)


def is_dutch_location(title, context, url):
    """Heuristiek: sluit alleen uit als er een duidelijk buitenlandse locatie
    wordt genoemd EN geen Nederlandse locatie erbij staat. Bij twijfel (geen
    enkele locatie-aanwijzing) wordt de vacature gewoon getoond -- uitsluiten
    zonder bewijs zou meer missen dan het waard is."""
    combined = f"{title} {context} {url}"
    has_nl = bool(NL_LOCATION_RE.search(combined))
    has_foreign = bool(FOREIGN_LOCATION_RE.search(combined))
    if has_foreign and not has_nl:
        return False
    return True


def log(msg):
    try:
        print(msg, flush=True)
    except Exception:
        try:
            print(str(msg).encode("ascii", "replace").decode("ascii"), flush=True)
        except Exception:
            pass  # loggen mag nooit de run zelf laten crashen


async def extract_candidates(page, base_url):
    candidates = []
    try:
        anchors = await page.eval_on_selector_all(
            "a",
            """(els) => els.map(e => {
                const text = (e.innerText || e.textContent || '').trim();
                let el = e, context = text;
                for (let i = 0; i < 3 && el.parentElement; i++) {
                    el = el.parentElement;
                    const t = (el.innerText || el.textContent || '').trim();
                    if (t.length > context.length) context = t;
                    if (context.length > 400) break;
                }
                return { text, href: e.href, context: context.slice(0, 400) };
            })"""
        )
    except Exception:
        anchors = []

    seen = set()
    for a in anchors:
        text = (a.get("text") or "").strip()
        href = a.get("href") or ""
        context = a.get("context") or ""
        if not text or len(text) < 4 or len(text) > 140 or not href:
            continue
        if SENIOR_RE.search(text) and not EXCLUDE_RE.search(text):
            if not is_dutch_location(text, context, href):
                continue
            key = (text.lower(), href)
            if key in seen:
                continue
            seen.add(key)
            candidates.append({"title": text, "url": urljoin(base_url, href)})
    return candidates


async def scan_org(browser, org):
    result = {
        "name": org["name"],
        "sector": org.get("sector", ""),
        "source_url": org["url"],
        "checked_at": datetime.datetime.utcnow().isoformat() + "Z",
        "status": "ok",
        "vacancies": [],
        "page_hash": None,
    }
    context = None
    page = None
    try:
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
            ),
            locale="nl-NL",
            viewport={"width": 1366, "height": 900},
            ignore_https_errors=True,
        )

        async def block_heavy(route):
            if route.request.resource_type in ("image", "media", "font"):
                await route.abort()
            else:
                await route.continue_()
        await context.route("**/*", block_heavy)

        page = await context.new_page()

        try:
            await page.goto(org["url"], wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
        except Exception as goto_err:
            raise RuntimeError(f"kon pagina niet laden ({goto_err.__class__.__name__})")

        try:
            await page.wait_for_load_state("networkidle", timeout=NETWORKIDLE_TIMEOUT_MS)
        except Exception:
            pass  # best effort

        await page.wait_for_timeout(EXTRA_WAIT_MS)

        content = await page.content()
        result["page_hash"] = hashlib.sha256(content.encode("utf-8", "ignore")).hexdigest()
        result["vacancies"] = await extract_candidates(page, org["url"])

        if not result["vacancies"] and len(content) < 500:
            result["status"] = "warning: pagina leeg of geblokkeerd (mogelijk bot-detectie)"

    except Exception as e:
        result["status"] = f"error: {e}"
    finally:
        try:
            if page is not None:
                await page.close()
            if context is not None:
                await context.close()
        except Exception:
            pass
    return result


def load_previous():
    if DATA_FILE.exists():
        try:
            return json.loads(DATA_FILE.read_text())
        except Exception:
            return {}
    return {}


def save_output(results, partial=False):
    output = {
        "generated_at": datetime.datetime.utcnow().isoformat() + "Z",
        "partial_run": partial,
        "organizations": results,
    }
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = DATA_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(output, indent=2, ensure_ascii=False))
    tmp.replace(DATA_FILE)
    return output


def write_log(results):
    log_entry = {
        "run_at": datetime.datetime.utcnow().isoformat() + "Z",
        "total_orgs": len(results),
        "orgs_with_hits": sum(1 for r in results if r["vacancies"]),
        "total_vacancies": sum(len(r["vacancies"]) for r in results),
        "errors": [r["name"] for r in results if r["status"] != "ok"],
    }
    history = []
    if LOG_FILE.exists():
        try:
            history = json.loads(LOG_FILE.read_text())
        except Exception:
            history = []
    history.append(log_entry)
    LOG_FILE.write_text(json.dumps(history[-90:], indent=2, ensure_ascii=False))
    return log_entry


def git_commit_push(message):
    """Best-effort tussentijdse commit. Faalt stil (met waarschuwing) als er geen
    git-repo/credentials beschikbaar zijn, bv. bij lokaal handmatig draaien."""
    try:
        subprocess.run(["git", "add", "docs/data/vacatures.json", "docs/data/scan-log.json"],
                        cwd=ROOT, check=True, capture_output=True)
        staged = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=ROOT)
        if staged.returncode != 0:  # er staan wijzigingen klaar
            subprocess.run(["git", "commit", "-m", message], cwd=ROOT, check=True, capture_output=True)
            subprocess.run(["git", "push"], cwd=ROOT, check=True, capture_output=True)
            log(f"  -> tussentijds gecommit en gepusht ({message})")
    except Exception as e:
        log(f"  Waarschuwing: tussentijds committen mislukt ({e})")


async def run_scan():
    orgs = json.loads(ORG_FILE.read_text())
    previous = load_previous()
    prev_by_org = {o["name"]: o for o in previous.get("organizations", [])}

    results_by_name = {}
    start = time.monotonic()
    sem = asyncio.Semaphore(MAX_CONCURRENCY)

    async with async_playwright() as p:
        browser = await p.chromium.launch(args=["--disable-dev-shm-usage", "--no-sandbox"])

        async def bounded_scan(org):
            async with sem:
                try:
                    return await scan_org(browser, org)
                except Exception as e:
                    # Laatste redmiddel: scan_org zelf vangt al bijna alles af, maar
                    # mocht er toch iets doorheen glippen, dan geeft de taak altijd
                    # een geldig resultaat terug -- nooit een onbehandelde exception.
                    return {
                        "name": org["name"], "sector": org.get("sector", ""),
                        "source_url": org["url"],
                        "checked_at": datetime.datetime.utcnow().isoformat() + "Z",
                        "status": f"error: {e}", "vacancies": [], "page_hash": None,
                    }

        tasks = {asyncio.ensure_future(bounded_scan(org)): org for org in orgs}
        completed = 0
        timed_out = False

        # Let op: asyncio.as_completed() geeft interne wrapper-objecten terug, NIET
        # de originele taken uit `tasks` -- die dict mag dus hier niet gebruikt worden
        # om van een voltooide future terug te redeneren naar de organisatie. Omdat
        # bounded_scan() hierboven altijd al een geldig resultaat teruggeeft (met
        # "name" erin), is dat ook niet nodig.
        for fut in asyncio.as_completed(list(tasks.keys())):
            elapsed = time.monotonic() - start
            if elapsed > MAX_RUNTIME_SECONDS:
                timed_out = True
                break

            # Alles hieronder in één vangnet: wat er ook misgaat bij het verwerken
            # van één organisatie (rare tekens, een corrupt resultaat, een
            # tussentijdse commit die faalt), de run als geheel mag nooit crashen.
            try:
                res = await fut

                prev = prev_by_org.get(res.get("name"))
                prev_urls = {v["url"] for v in prev["vacancies"]} if prev else set()
                for v in res.get("vacancies") or []:
                    v["is_new"] = v["url"] not in prev_urls
                res["new_count"] = sum(1 for v in (res.get("vacancies") or []) if v.get("is_new"))

                results_by_name[res["name"]] = res
                completed += 1
                log(f"[{completed}/{len(orgs)}] {res['name']}: "
                    f"{len(res.get('vacancies') or [])} treffer(s), status={res['status']}")

                if completed % COMMIT_EVERY == 0:
                    ordered = [results_by_name.get(o["name"]) for o in orgs if o["name"] in results_by_name]
                    save_output(ordered, partial=True)
                    write_log(ordered)
                    git_commit_push(f"Tussentijdse scan-update ({completed}/{len(orgs)})")
            except Exception as loop_err:
                log(f"  Waarschuwing: onverwachte fout bij verwerken van een resultaat "
                    f"({loop_err}) — doorgaan met volgende organisatie.")
                continue

        if timed_out:
            for f in tasks:
                if not f.done():
                    f.cancel()
            await asyncio.gather(*tasks.keys(), return_exceptions=True)

        await browser.close()

        if timed_out:
            log(f"Tijdslimiet ({MAX_RUNTIME_SECONDS/60:.0f} min) bereikt — "
                f"run wordt afgerond met {completed}/{len(orgs)} organisaties.")

    # Onbereikte organisaties (bij timeout) toch als 'niet gecontroleerd' opnemen,
    # zodat het dashboard nooit stilzwijgend data van eerdere runs blijft tonen.
    ordered = []
    for o in orgs:
        if o["name"] in results_by_name:
            ordered.append(results_by_name[o["name"]])
        else:
            ordered.append({
                "name": o["name"], "sector": o.get("sector", ""),
                "source_url": o["url"],
                "checked_at": datetime.datetime.utcnow().isoformat() + "Z",
                "status": "warning: niet gescand binnen tijdslimiet van deze run",
                "vacancies": [], "page_hash": None, "new_count": 0,
            })

    return ordered


def main():
    results = asyncio.run(run_scan())
    save_output(results, partial=False)
    log_entry = write_log(results)
    git_commit_push(f"Automatische scan afgerond: {log_entry['run_at']}")

    log(f"Klaar. {log_entry['total_vacancies']} kandidaat-vacatures gevonden "
        f"bij {log_entry['orgs_with_hits']} organisaties. "
        f"{len(log_entry['errors'])} fouten/waarschuwingen.")


if __name__ == "__main__":
    main()
