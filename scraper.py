#!/usr/bin/env python3
"""
Vacature-monitor scraper.

Bezoekt elke organisatie-URL uit organizations.json met een headless browser
(zodat JavaScript-gerenderde vacaturesites ook worden gelezen), zoekt naar
links/teksten die op senior-management of directieniveau wijzen, en schrijft
het resultaat naar data/vacatures.json.

Vergelijkt met de vorige run (data/vacatures.json, voordat het wordt overschreven)
om nieuwe/verdwenen vacatures te markeren.
"""

import json
import re
import time
import hashlib
import datetime
from pathlib import Path
from urllib.parse import urljoin

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).parent
ORG_FILE = ROOT / "organizations.json"
DATA_FILE = ROOT / "docs" / "data" / "vacatures.json"
LOG_FILE = ROOT / "docs" / "data" / "scan-log.json"

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

# Ruis die we NIET als senior-management willen meetellen ondanks een treffer
EXCLUDE_KEYWORDS = [
    r"assistent", r"stagiair", r"intern\b", r"trainee", r"junior",
    r"medewerker klantenservice", r"secretaresse van de directeur",
]
EXCLUDE_RE = re.compile("|".join(EXCLUDE_KEYWORDS), re.IGNORECASE)


def extract_candidates(page, base_url):
    """Haal alle links + omliggende teksten op die op een vacature lijken."""
    candidates = []
    try:
        anchors = page.eval_on_selector_all(
            "a",
            """(els) => els.map(e => ({
                text: (e.innerText || e.textContent || '').trim(),
                href: e.href
            }))"""
        )
    except Exception:
        anchors = []

    seen = set()
    for a in anchors:
        text = (a.get("text") or "").strip()
        href = a.get("href") or ""
        if not text or len(text) < 4 or len(text) > 140:
            continue
        if not href:
            continue
        if SENIOR_RE.search(text) and not EXCLUDE_RE.search(text):
            key = (text.lower(), href)
            if key in seen:
                continue
            seen.add(key)
            candidates.append({
                "title": text,
                "url": urljoin(base_url, href),
            })
    return candidates


def scan_org(browser, org, attempt=1, max_attempts=2):
    result = {
        "name": org["name"],
        "sector": org.get("sector", ""),
        "source_url": org["url"],
        "checked_at": datetime.datetime.utcnow().isoformat() + "Z",
        "status": "ok",
        "vacancies": [],
        "page_hash": None,
    }
    context = browser.new_context(
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
        ),
        locale="nl-NL",
        viewport={"width": 1366, "height": 900},
        ignore_https_errors=True,  # sommige (overheids)sites hebben verouderde certificaten
    )
    context.set_default_navigation_timeout(45000)
    context.set_default_timeout(15000)

    # Zware/afleidende resources blokkeren: sneller, minder kans op vastlopen door
    # trackers, chat-widgets, video's etc.
    def block_heavy(route):
        if route.request.resource_type in ("image", "media", "font"):
            route.abort()
        else:
            route.continue_()
    context.route("**/*", block_heavy)

    page = context.new_page()
    try:
        # Eerst alleen de DOM laden (snel en betrouwbaar); "networkidle" wachten
        # is best effort, want sommige sites houden permanente verbindingen open
        # (chat-widgets, analytics) waardoor networkidle nooit zou intreden.
        try:
            page.goto(org["url"], wait_until="domcontentloaded")
        except Exception as goto_err:
            raise RuntimeError(f"kon pagina niet laden ({goto_err.__class__.__name__})")

        try:
            page.wait_for_load_state("networkidle", timeout=8000)
        except Exception:
            pass  # niet fataal, we werken met wat er al geladen is

        page.wait_for_timeout(2000)  # extra marge voor lazy-loaded content/JS-frameworks

        content = page.content()
        result["page_hash"] = hashlib.sha256(content.encode("utf-8", "ignore")).hexdigest()
        result["vacancies"] = extract_candidates(page, org["url"])

        if not result["vacancies"] and len(content) < 500:
            # Verdacht lege pagina (mogelijk blokkade/bot-detectie) -> markeren, niet
            # stilzwijgend als "geen vacatures" tonen.
            result["status"] = "warning: pagina leeg of geblokkeerd (mogelijk bot-detectie)"

    except Exception as e:
        if attempt < max_attempts:
            page.close()
            context.close()
            return scan_org(browser, org, attempt=attempt + 1, max_attempts=max_attempts)
        result["status"] = f"error: {e}"
    finally:
        try:
            page.close()
            context.close()
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


def build_output(results):
    return {
        "generated_at": datetime.datetime.utcnow().isoformat() + "Z",
        "organizations": results,
    }


def save_output(results):
    output = build_output(results)
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = DATA_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(output, indent=2, ensure_ascii=False))
    tmp.replace(DATA_FILE)  # atomische schrijfactie: nooit een half-geschreven bestand
    return output


def main():
    orgs = json.loads(ORG_FILE.read_text())
    previous = load_previous()
    prev_by_org = {o["name"]: o for o in previous.get("organizations", [])}

    results = []
    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--disable-dev-shm-usage"])

        for i, org in enumerate(orgs, start=1):
            print(f"[{i}/{len(orgs)}] Scannen: {org['name']} ...")
            try:
                res = scan_org(browser, org)
            except Exception as e:
                # Laatste redmiddel: ook onverwachte fouten (bv. gecrashte browser)
                # mogen de hele run niet laten stoppen. Probeer de browser opnieuw
                # te starten voor de resterende organisaties.
                print(f"  Onverwachte fout bij {org['name']}: {e} -> browser herstarten")
                try:
                    browser.close()
                except Exception:
                    pass
                browser = p.chromium.launch(args=["--disable-dev-shm-usage"])
                res = {
                    "name": org["name"], "sector": org.get("sector", ""),
                    "source_url": org["url"],
                    "checked_at": datetime.datetime.utcnow().isoformat() + "Z",
                    "status": f"error: {e}", "vacancies": [], "page_hash": None,
                }

            prev = prev_by_org.get(org["name"])
            prev_urls = {v["url"] for v in prev["vacancies"]} if prev else set()
            for v in res["vacancies"]:
                v["is_new"] = v["url"] not in prev_urls
            res["new_count"] = sum(1 for v in res["vacancies"] if v["is_new"])

            results.append(res)

            # Elke 15 organisaties tussentijds opslaan, zodat een eventuele
            # time-out van de job niet alle voortgang verloren laat gaan.
            if i % 15 == 0:
                save_output(results)

            time.sleep(1.5)  # beleefde pauze tussen sites

        browser.close()

    output = save_output(results)

    log_entry = {
        "run_at": output["generated_at"],
        "total_orgs": len(results),
        "orgs_with_hits": sum(1 for r in results if r["vacancies"]),
        "total_vacancies": sum(len(r["vacancies"]) for r in results),
        "errors": [r["name"] for r in results if r["status"] != "ok"],
    }
    log = []
    if LOG_FILE.exists():
        try:
            log = json.loads(LOG_FILE.read_text())
        except Exception:
            log = []
    log.append(log_entry)
    LOG_FILE.write_text(json.dumps(log[-90:], indent=2, ensure_ascii=False))

    print(f"Klaar. {log_entry['total_vacancies']} kandidaat-vacatures gevonden "
          f"bij {log_entry['orgs_with_hits']} organisaties. "
          f"{len(log_entry['errors'])} fouten/waarschuwingen.")


if __name__ == "__main__":
    main()
