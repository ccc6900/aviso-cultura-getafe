import os
import re
import sys
import time
import hashlib
import smtplib
from email.message import EmailMessage
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
SUPABASE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
GMX_EMAIL = os.environ["GMX_EMAIL"]
GMX_PASSWORD = os.environ["GMX_PASSWORD"]
SMTP_HOST = "mail.gmx.es"
SMTP_PORT = 587
HEADERS_BASE = {"apikey": SUPABASE_KEY, "Content-Type": "application/json"}
if not SUPABASE_KEY.startswith("sb_secret_"):
    HEADERS_BASE["Authorization"] = f"Bearer {SUPABASE_KEY}"

session = requests.Session()
session.headers.update({"User-Agent": "AvisoCulturaGetafe/1.0 (+periodic event availability checker)"})

def supabase_get(path, params=None):
    r = requests.get(f"{SUPABASE_URL}/rest/v1/{path}", headers=HEADERS_BASE, params=params, timeout=30)
    if not r.ok:
        raise RuntimeError(f"Supabase GET {path}: HTTP {r.status_code} - {r.text[:400]}")
    return r.json()

def supabase_post(path, rows, prefer="return=minimal"):
    if not rows:
        return
    headers = dict(HEADERS_BASE)
    headers["Prefer"] = prefer
    r = requests.post(f"{SUPABASE_URL}/rest/v1/{path}", headers=headers, json=rows, timeout=30)
    if not r.ok:
        raise RuntimeError(f"Supabase POST {path}: HTTP {r.status_code} - {r.text[:500]}")

def normalize_text(value):
    return re.sub(r"\s+", " ", value or "").strip()

def event_candidates(html, page_url):
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "svg", "nav", "footer", "header"]):
        tag.decompose()

    events = []
    # Most event listings use h2/h3 for event titles. Avoid generic site headings.
    for heading in soup.find_all(["h2", "h3"]):
        title = normalize_text(heading.get_text(" ", strip=True))
        if not title or len(title) < 3 or len(title) > 180:
            continue
        if title.lower() in {"eventos", "próximos eventos", "agenda", "entradas", "cartelera"}:
            continue

        container = heading
        best_url = None
        context = ""
        # Inspect the heading and a few parent containers for event details / ticket links.
        for _ in range(5):
            if container is None:
                break
            context = normalize_text(container.get_text(" ", strip=True))
            links = container.find_all("a", href=True)
            ranked = []
            for a in links:
                label = normalize_text(a.get_text(" ", strip=True)).lower()
                href = urljoin(page_url, a["href"])
                if href.startswith(("mailto:", "tel:", "javascript:")):
                    continue
                score = 0
                if "comprar" in label or "entradas" in label or "tickets" in label:
                    score += 5
                if "info" in label or "detalle" in label:
                    score += 2
                if urlparse(href).netloc == urlparse(page_url).netloc:
                    score += 1
                ranked.append((score, href))
            if ranked:
                ranked.sort(reverse=True)
                best_url = ranked[0][1]
            # Stop once the container has enough meaningful text and a link.
            if best_url and len(context) > len(title) + 8:
                break
            container = container.parent

        if not best_url:
            # If there is no detail/ticket link nearby, use the page URL as a safe fallback.
            best_url = page_url

        # Keep a short context (often contains date, venue, price and availability).
        context = context[:1200]
        fingerprint_source = f"{title.lower()}|{best_url.rstrip('/')}"
        fingerprint = hashlib.sha256(fingerprint_source.encode("utf-8")).hexdigest()
        events.append({
            "titulo": title,
            "url": best_url,
            "contexto": context,
            "huella": fingerprint,
        })

    # De-duplicate repeated headings within the same page.
    unique = {}
    for event in events:
        unique[event["huella"]] = event
    return list(unique.values())

def fetch_all_event_candidates(page_url):
    """Lee todas las páginas de resultados enlazadas por la web."""
    first_response = session.get(page_url, timeout=35)
    first_response.raise_for_status()
    first_response.encoding = first_response.apparent_encoding or first_response.encoding
    first_html = first_response.text

    # Descubrir las páginas disponibles a partir de los enlaces de paginación.
    pagination_soup = BeautifulSoup(first_html, "html.parser")
    page_urls = {1: first_response.url}
    for link in pagination_soup.find_all("a", href=True):
        href = urljoin(first_response.url, link["href"])
        parsed = urlparse(href)
        match = re.search(r"(?:[?&])pagina=(\d+)", parsed.query, flags=re.IGNORECASE)
        if parsed.netloc == urlparse(first_response.url).netloc and match:
            page_number = int(match.group(1))
            if 1 <= page_number <= 20:
                page_urls[page_number] = href

    # La web suele mostrar enlaces a todas las páginas; limitar a 20 por seguridad.
    max_page = min(max(page_urls), 20)
    all_events = []
    seen = set()

    for page_number in range(1, max_page + 1):
        current_url = page_urls.get(page_number)
        if not current_url:
            # Si falta el enlace de un número intermedio, construirlo sobre la
            # URL de resultados que ya hemos descubierto.
            base = next((u for n, u in page_urls.items() if n == 1), first_response.url)
            parsed = urlparse(base)
            from urllib.parse import parse_qsl, urlencode, urlunparse
            query_items = [(k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True) if k.lower() != "pagina"]
            query_items.append(("pagina", str(page_number)))
            current_url = urlunparse(parsed._replace(query=urlencode(query_items)))

        if page_number == 1:
            html = first_html
        else:
            response = session.get(current_url, timeout=35)
            response.raise_for_status()
            response.encoding = response.apparent_encoding or response.encoding
            html = response.text

        page_events = event_candidates(html, current_url)
        print(f"[PÁGINA] {page_number}/{max_page}: {len(page_events)} elementos")
        for event in page_events:
            if event["huella"] not in seen:
                seen.add(event["huella"])
                all_events.append(event)

    return all_events


def send_email(to_addresses, subject, body):
    msg = EmailMessage()
    msg["From"] = GMX_EMAIL
    msg["To"] = ", ".join(to_addresses)
    msg["Subject"] = subject
    msg.set_content(body)
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as smtp:
        smtp.ehlo()
        smtp.starttls()
        smtp.ehlo()
        smtp.login(GMX_EMAIL, GMX_PASSWORD)
        smtp.send_message(msg)

def main():
    print("Consultando páginas guardadas en Supabase...")
    # Consultamos primero sin filtro para diagnosticar cualquier discrepancia
    # entre el valor de la columna activa y el filtro REST.
    todas_las_webs = supabase_get("webs", {
        "select": "id,nombre,url,activa,destinatarios(correo)",
        "order": "id.asc",
    })
    print(f"Filas visibles en la tabla webs: {len(todas_las_webs)}")
    for fila in todas_las_webs:
        print(
            f"[CONFIG] nombre={fila.get('nombre')!r}; "
            f"activa={fila.get('activa')!r}; "
            f"URL={'sí' if fila.get('url') else 'no'}; "
            f"destinatarios={len(fila.get('destinatarios') or [])}"
        )

    webs = [
        fila for fila in todas_las_webs
        if fila.get("activa") is True or str(fila.get("activa", "")).strip().lower() in ("true", "t", "1")
    ]
    print(f"Páginas activas: {len(webs)}")
    if not todas_las_webs:
        print(
            "[DIAGNÓSTICO] Supabase no devuelve filas de webs a la clave configurada. "
            "Comprueba que el secreto de GitHub contiene la clave secreta del mismo proyecto."
        )

    for web in webs:
        web_id = str(web["id"])
        nombre = web.get("nombre") or "Página vigilada"
        page_url = web.get("url") or ""
        recipients = sorted({
            d.get("correo", "").strip()
            for d in (web.get("destinatarios") or [])
            if d.get("correo", "").strip()
        })
        if not page_url or not recipients:
            print(f"[OMITIDA] {nombre}: falta URL o destinatarios.")
            continue

        try:
            events = fetch_all_event_candidates(page_url)
            print(f"[OK] {nombre}: {len(events)} elementos detectados en total.")
            print(f"[LISTADO] Elementos detectados en {nombre}:")
            for numero, evento in enumerate(events, start=1):
                print(f"  {numero}. {evento['titulo']} | {evento['url']}")
        except Exception as exc:
            print(f"[ERROR] No se pudo revisar {nombre} ({page_url}): {exc}")
            continue

        try:
            known_rows = supabase_get("alertas_enviadas", {
                "select": "huella",
                "web_id": f"eq.{web_id}",
            })
            known = {row["huella"] for row in known_rows}
        except Exception as exc:
            print(f"[ERROR] No se pudo consultar el historial de {nombre}: {exc}")
            continue

        # Primera revisión de cada web: crear una línea base para no enviar decenas
        # de correos por eventos que ya estaban publicados antes de activar el sistema.
        first_scan = len(known_rows) == 0
        new_events = [e for e in events if e["huella"] not in known]

        if first_scan:
            print(f"[INICIO] {nombre}: guardando {len(new_events)} elementos como referencia, sin enviar avisos iniciales.")
            if new_events:
                supabase_post("alertas_enviadas", [
                    {"web_id": web_id, "huella": e["huella"], "titulo": e["titulo"], "url": e["url"]}
                    for e in new_events
                ], prefer="resolution=ignore-duplicates,return=minimal")
            continue

        # La primera versión solo leía una parte de la web. Si el historial
        # es claramente incompleto, completar la referencia actual sin avisar
        # de eventos antiguos que simplemente estaban en páginas no leídas.
        if len(known_rows) < len(events) and len(new_events) >= 5:
            print(
                f"[REFERENCIA] {nombre}: el historial tenía {len(known_rows)} elementos "
                f"y ahora se ven {len(events)}. Guardando {len(new_events)} elementos "
                "faltantes como referencia inicial, sin enviar avisos."
            )
            supabase_post("alertas_enviadas", [
                {"web_id": web_id, "huella": e["huella"], "titulo": e["titulo"], "url": e["url"]}
                for e in new_events
            ], prefer="resolution=ignore-duplicates,return=minimal")
            continue

        for event in new_events:
            body = (
                f"Se ha detectado una novedad en: {nombre}\n\n"
                f"Evento: {event['titulo']}\n"
                f"Página / compra: {event['url']}\n\n"
                f"Información encontrada en la página:\n{event['contexto']}\n\n"
                "Aviso automático de aviso-cultura-getafe. Comprueba los detalles y la disponibilidad en la web oficial."
            )
            try:
                send_email(recipients, f"Nuevo evento detectado: {event['titulo']}", body)
                supabase_post("alertas_enviadas", [{
                    "web_id": web_id,
                    "huella": event["huella"],
                    "titulo": event["titulo"],
                    "url": event["url"],
                }], prefer="resolution=ignore-duplicates,return=minimal")
                print(f"[AVISO ENVIADO] {nombre}: {event['titulo']} -> {len(recipients)} destinatario(s)")
            except Exception as exc:
                print(f"[ERROR] Envío o registro fallido para '{event['titulo']}': {exc}")

        if not new_events:
            print(f"[SIN NOVEDADES] {nombre}")

if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERROR FATAL: {exc}", file=sys.stderr)
        sys.exit(1)
