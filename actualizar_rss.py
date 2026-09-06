import html
import os
import re
import sys
import time
from datetime import datetime, timezone
from email.utils import format_datetime, parsedate_to_datetime
from pathlib import Path
from urllib.parse import urljoin, urlsplit, urlunsplit
import xml.etree.ElementTree as ET
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup


URL_PRINCIPAL = "https://www.oryzon.com/es/noticias-eventos/noticias"
DOMINIO = "https://www.oryzon.com"
RUTA_NOTICIAS = "/es/noticias-eventos/noticias/"
ARCHIVO_RSS = Path("rss.xml")

MAXIMO_PAGINAS = 100
MAXIMO_NOTICIAS_RSS = 2000

CABECERAS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/140.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;"
        "q=0.9,image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "es-ES,es;q=0.9,en;q=0.6",
    "Cache-Control": "no-cache",
}

MESES = {
    "enero": 1,
    "febrero": 2,
    "marzo": 3,
    "abril": 4,
    "mayo": 5,
    "junio": 6,
    "julio": 7,
    "agosto": 8,
    "septiembre": 9,
    "setiembre": 9,
    "octubre": 10,
    "noviembre": 11,
    "diciembre": 12,
}


def ejecucion_permitida():
    """
    Las ejecuciones manuales siempre se permiten.

    Las ejecuciones programadas solo continúan de lunes a viernes
    y entre las 07:00 y las 19:59, hora peninsular española.
    """
    if os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch":
        print("Ejecución manual: se ignora el límite horario.")
        return True

    ahora = datetime.now(ZoneInfo("Europe/Madrid"))

    print(
        "Hora en España:",
        ahora.strftime("%d/%m/%Y %H:%M:%S %Z"),
    )

    if ahora.weekday() >= 5:
        print("Hoy es sábado o domingo. No se actualiza el RSS.")
        return False

    if not 7 <= ahora.hour <= 19:
        print("Fuera del horario de 07:00 a 19:59.")
        return False

    return True


def limpiar_texto(texto):
    if not texto:
        return ""

    return re.sub(
        r"\s+",
        " ",
        html.unescape(str(texto)),
    ).strip()


def limpiar_url(url):
    partes = urlsplit(url)

    return urlunsplit(
        (
            partes.scheme.lower(),
            partes.netloc.lower(),
            partes.path.rstrip("/"),
            "",
            "",
        )
    )


def descargar(session, url):
    ultimo_error = None

    for intento in range(1, 4):
        try:
            respuesta = session.get(
                url,
                headers=CABECERAS,
                timeout=40,
                allow_redirects=True,
            )
            respuesta.raise_for_status()
            respuesta.encoding = (
                respuesta.apparent_encoding or "utf-8"
            )

            print(
                f"Descargada: {url} "
                f"({len(respuesta.content)} bytes)"
            )

            return respuesta.text

        except requests.RequestException as error:
            ultimo_error = error

            print(
                f"Intento {intento}/3 fallido: {url}: {error}",
                file=sys.stderr,
            )

            if intento < 3:
                time.sleep(intento * 2)

    raise RuntimeError(
        f"No se pudo descargar {url}: {ultimo_error}"
    )


def convertir_fecha(dia, mes, anio):
    try:
        fecha = datetime(
            int(anio),
            int(mes),
            int(dia),
            12,
            0,
            tzinfo=ZoneInfo("Europe/Madrid"),
        )

        return fecha.astimezone(timezone.utc)

    except (ValueError, TypeError):
        return None


def extraer_fecha(texto):
    texto = limpiar_texto(texto).lower()

    coincidencia = re.search(
        r"\b([0-3]?\d)[/\-.]([01]?\d)[/\-.]((?:19|20)\d{2})\b",
        texto,
    )

    if coincidencia:
        return convertir_fecha(
            coincidencia.group(1),
            coincidencia.group(2),
            coincidencia.group(3),
        )

    coincidencia = re.search(
        r"\b([0-3]?\d)\s+(?:de\s+)?"
        r"(enero|febrero|marzo|abril|mayo|junio|julio|"
        r"agosto|septiembre|setiembre|octubre|noviembre|diciembre)"
        r"\s+(?:de\s+)?((?:19|20)\d{2})\b",
        texto,
    )

    if coincidencia:
        return convertir_fecha(
            coincidencia.group(1),
            MESES[coincidencia.group(2)],
            coincidencia.group(3),
        )

    return None


def buscar_fecha_detalle(soup):
    selectores = [
        "meta[property='article:published_time']",
        "meta[name='date']",
        "meta[name='publication_date']",
        "meta[itemprop='datePublished']",
        "time[datetime]",
    ]

    for selector in selectores:
        elemento = soup.select_one(selector)

        if not elemento:
            continue

        valor = (
            elemento.get("content")
            or elemento.get("datetime")
            or elemento.get_text(" ", strip=True)
        )

        if not valor:
            continue

        try:
            fecha = datetime.fromisoformat(
                valor.strip().replace("Z", "+00:00")
            )

            if fecha.tzinfo is None:
                fecha = fecha.replace(
                    tzinfo=ZoneInfo("Europe/Madrid")
                )

            return fecha.astimezone(timezone.utc)

        except (ValueError, TypeError):
            fecha = extraer_fecha(valor)

            if fecha:
                return fecha

    candidatos = soup.select(
        "main time, article time, "
        ".date, .fecha, .field--name-field-date"
    )

    for candidato in candidatos:
        fecha = extraer_fecha(
            candidato.get_text(" ", strip=True)
        )

        if fecha:
            return fecha

    return extraer_fecha(soup.get_text(" ", strip=True))


def localizar_noticias(session):
    encontradas = {}
    urls_vistas_por_pagina = set()

    for numero_pagina in range(MAXIMO_PAGINAS):
        if numero_pagina == 0:
            url_pagina = URL_PRINCIPAL
        else:
            url_pagina = (
                f"{URL_PRINCIPAL}?page={numero_pagina}"
            )

        try:
            contenido = descargar(session, url_pagina)
        except Exception as error:
            print(
                f"AVISO: no se pudo leer la página "
                f"{numero_pagina}: {error}",
                file=sys.stderr,
            )
            break

        soup = BeautifulSoup(contenido, "html.parser")
        noticias_pagina = {}

        for enlace in soup.find_all("a", href=True):
            href = enlace.get("href", "").strip()

            if not href:
                continue

            url = limpiar_url(urljoin(URL_PRINCIPAL, href))
            partes = urlsplit(url)

            if partes.netloc not in {
                "www.oryzon.com",
                "oryzon.com",
            }:
                continue

            if not partes.path.startswith(RUTA_NOTICIAS):
                continue

            if partes.path.rstrip("/") == URL_PRINCIPAL.replace(
                DOMINIO,
                "",
            ).rstrip("/"):
                continue

            titulo = limpiar_texto(
                enlace.get_text(" ", strip=True)
            )

            if len(titulo) < 12:
                titulo = limpiar_texto(
                    enlace.get("title")
                    or enlace.get("aria-label")
                    or ""
                )

            if len(titulo) < 12:
                continue

            noticias_pagina[url] = titulo

        conjunto_actual = frozenset(noticias_pagina.keys())

        print(
            f"Página {numero_pagina}: "
            f"{len(noticias_pagina)} noticias."
        )

        if not noticias_pagina:
            break

        # Evita bucles si la web ignora el parámetro de página.
        if conjunto_actual in urls_vistas_por_pagina:
            print(
                "La página repite los mismos resultados. "
                "Finaliza la paginación."
            )
            break

        urls_vistas_por_pagina.add(conjunto_actual)

        nuevas_en_pagina = 0

        for url, titulo in noticias_pagina.items():
            if url not in encontradas:
                encontradas[url] = titulo
                nuevas_en_pagina += 1

        if numero_pagina > 0 and nuevas_en_pagina == 0:
            break

        time.sleep(0.3)

    return encontradas


def extraer_titulo(soup, titulo_listado, url):
    elemento = soup.select_one(
        "main h1, article h1, h1"
    )

    if elemento:
        titulo = limpiar_texto(
            elemento.get_text(" ", strip=True)
        )
    else:
        titulo = ""

    if not titulo:
        meta = soup.select_one("meta[property='og:title']")

        if meta:
            titulo = limpiar_texto(
                meta.get("content", "")
            )

    if not titulo:
        titulo = limpiar_texto(titulo_listado)

    if not titulo:
        slug = urlsplit(url).path.split("/")[-1]
        titulo = slug.replace("-", " ").capitalize()

    titulo = re.sub(
        r"\s*[|–-]\s*Oryzon\s*$",
        "",
        titulo,
        flags=re.IGNORECASE,
    ).strip()

    return titulo


def obtener_contenedor(soup):
    selectores = [
        "main article",
        "article",
        ".node__content",
        ".field--name-body",
        ".field-name-body",
        ".news-detail",
        ".noticia-detalle",
        "main",
    ]

    for selector in selectores:
        contenedor = soup.select_one(selector)

        if contenedor:
            return contenedor

    return soup.body or soup


def extraer_descripcion(soup):
    contenedor = obtener_contenedor(soup)

    for elemento in contenedor.select(
        "script, style, nav, form, button, "
        "footer, aside, noscript"
    ):
        elemento.decompose()

    fragmentos = []
    longitud = 0

    for elemento in contenedor.find_all(
        ["p", "li", "h2", "h3"]
    ):
        texto = limpiar_texto(
            elemento.get_text(" ", strip=True)
        )

        if len(texto) < 25:
            continue

        texto_minusculas = texto.lower()

        exclusiones = (
            "alertas por email",
            "suscríbase",
            "política de privacidad",
            "política de cookies",
            "todos los derechos reservados",
        )

        if any(
            exclusión in texto_minusculas
            for exclusión in exclusiones
        ):
            continue

        if texto in fragmentos:
            continue

        fragmentos.append(texto)
        longitud += len(texto)

        if longitud >= 2500:
            break

    descripcion = " ".join(fragmentos)

    if len(descripcion) < 50:
        meta = soup.select_one(
            "meta[property='og:description'], "
            "meta[name='description']"
        )

        if meta:
            descripcion = limpiar_texto(
                meta.get("content", "")
            )

    if len(descripcion) > 3000:
        descripcion = (
            descripcion[:2997].rsplit(" ", 1)[0] + "..."
        )

    return descripcion or "Noticia publicada por Oryzon."


def extraer_imagen(soup, url):
    for selector in [
        "meta[property='og:image']",
        "meta[name='twitter:image']",
    ]:
        elemento = soup.select_one(selector)

        if elemento and elemento.get("content"):
            imagen = urljoin(
                url,
                elemento["content"].strip(),
            )

            if imagen.startswith("http"):
                return imagen

    contenedor = obtener_contenedor(soup)
    imagen = contenedor.find("img", src=True)

    if imagen:
        return urljoin(url, imagen["src"])

    return ""


def extraer_documentos(soup, url):
    documentos = []
    vistos = set()

    for enlace in soup.find_all("a", href=True):
        absoluta = urljoin(
            url,
            enlace.get("href", "").strip(),
        )

        texto = limpiar_texto(
            enlace.get_text(" ", strip=True)
        )

        es_documento = re.search(
            r"\.(pdf|doc|docx|xls|xlsx)(?:$|\?)",
            absoluta,
            flags=re.IGNORECASE,
        )

        es_descarga = (
            "descargar" in texto.lower()
            or "download" in texto.lower()
        )

        if not es_documento and not es_descarga:
            continue

        if absoluta in vistos:
            continue

        vistos.add(absoluta)

        if not texto:
            texto = "Descargar documento"

        documentos.append(
            f'<a href="{html.escape(absoluta, quote=True)}">'
            f"{html.escape(texto)}</a>"
        )

    return documentos


def procesar_noticia(session, url, titulo_listado):
    try:
        contenido = descargar(session, url)
        soup = BeautifulSoup(contenido, "html.parser")

        titulo = extraer_titulo(
            soup,
            titulo_listado,
            url,
        )
        fecha = buscar_fecha_detalle(soup)
        descripcion = extraer_descripcion(soup)
        imagen = extraer_imagen(soup, url)
        documentos = extraer_documentos(soup, url)

        if not fecha:
            print(
                f"AVISO: descartada por no encontrar fecha: {url}",
                file=sys.stderr,
            )
            return None

        descripcion_html = (
            f"<p>{html.escape(descripcion)}</p>"
        )

        if documentos:
            descripcion_html += (
                "<p><strong>Documentos:</strong><br>"
                + "<br>".join(documentos)
                + "</p>"
            )

        if imagen:
            descripcion_html = (
                f'<p><img src="'
                f'{html.escape(imagen, quote=True)}" '
                f'alt="{html.escape(titulo, quote=True)}">'
                f"</p>"
                + descripcion_html
            )

        return {
            "titulo": titulo,
            "url": limpiar_url(url),
            "fecha": fecha,
            "descripcion": descripcion_html,
            "imagen": imagen,
        }

    except Exception as error:
        print(
            f"AVISO: no se pudo procesar {url}: {error}",
            file=sys.stderr,
        )
        return None


def leer_rss_anterior():
    noticias = {}

    if not ARCHIVO_RSS.exists():
        return noticias

    try:
        raiz = ET.parse(ARCHIVO_RSS).getroot()
        canal = raiz.find("channel")

        if canal is None:
            return noticias

        for item in canal.findall("item"):
            titulo = limpiar_texto(
                item.findtext("title", "")
            )
            url = limpiar_url(
                item.findtext("link", "").strip()
            )
            descripcion = item.findtext(
                "description",
                "",
            )
            fecha_texto = item.findtext(
                "pubDate",
                "",
            )

            if not titulo or not url:
                continue

            try:
                fecha = parsedate_to_datetime(
                    fecha_texto
                )

                if fecha.tzinfo is None:
                    fecha = fecha.replace(
                        tzinfo=timezone.utc
                    )

                fecha = fecha.astimezone(timezone.utc)

            except (ValueError, TypeError):
                fecha = datetime(
                    1970,
                    1,
                    1,
                    tzinfo=timezone.utc,
                )

            enclosure = item.find("enclosure")
            imagen = ""

            if enclosure is not None:
                imagen = enclosure.get("url", "")

            noticias[url] = {
                "titulo": titulo,
                "url": url,
                "fecha": fecha,
                "descripcion": descripcion,
                "imagen": imagen,
            }

    except (ET.ParseError, OSError) as error:
        print(
            f"AVISO: no se pudo leer el RSS anterior: {error}",
            file=sys.stderr,
        )

    return noticias


def escribir_rss(noticias):
    ET.register_namespace(
        "atom",
        "http://www.w3.org/2005/Atom",
    )

    rss = ET.Element(
        "rss",
        {"version": "2.0"},
    )

    canal = ET.SubElement(rss, "channel")

    ET.SubElement(canal, "title").text = (
        "Noticias de Oryzon Genomics"
    )
    ET.SubElement(canal, "link").text = URL_PRINCIPAL
    ET.SubElement(canal, "description").text = (
        "Noticias, resultados, patentes, ensayos clínicos "
        "y comunicados de Oryzon Genomics."
    )
    ET.SubElement(canal, "language").text = "es-ES"
    ET.SubElement(canal, "lastBuildDate").text = (
        format_datetime(datetime.now(timezone.utc))
    )
    ET.SubElement(canal, "ttl").text = "60"

    enlace_atom = ET.SubElement(
        canal,
        "{http://www.w3.org/2005/Atom}link",
    )
    enlace_atom.set(
        "href",
        "https://raw.githubusercontent.com/"
        "plis2100/oryzon-noticias-rss/main/rss.xml",
    )
    enlace_atom.set("rel", "self")
    enlace_atom.set(
        "type",
        "application/rss+xml",
    )

    for noticia in noticias[:MAXIMO_NOTICIAS_RSS]:
        item = ET.SubElement(canal, "item")

        ET.SubElement(item, "title").text = (
            noticia["titulo"]
        )
        ET.SubElement(item, "link").text = (
            noticia["url"]
        )
        ET.SubElement(
            item,
            "guid",
            {"isPermaLink": "true"},
        ).text = noticia["url"]

        ET.SubElement(item, "pubDate").text = (
            format_datetime(
                noticia["fecha"].astimezone(
                    timezone.utc
                )
            )
        )

        ET.SubElement(item, "description").text = (
            noticia["descripcion"]
        )

        if noticia.get("imagen"):
            ET.SubElement(
                item,
                "enclosure",
                {
                    "url": noticia["imagen"],
                    "type": "image/jpeg",
                },
            )

    ET.indent(rss, space="  ")

    ET.ElementTree(rss).write(
        ARCHIVO_RSS,
        encoding="utf-8",
        xml_declaration=True,
    )


def main():
    if not ejecucion_permitida():
        return

    session = requests.Session()
    session.headers.update(CABECERAS)

    enlaces = localizar_noticias(session)

    print(
        f"Total de enlaces únicos encontrados: "
        f"{len(enlaces)}"
    )

    nuevas = {}

    for numero, (url, titulo) in enumerate(
        enlaces.items(),
        start=1,
    ):
        print(
            f"Procesando {numero}/{len(enlaces)}: {url}"
        )

        noticia = procesar_noticia(
            session,
            url,
            titulo,
        )

        if noticia:
            nuevas[noticia["url"]] = noticia

        time.sleep(0.25)

    anteriores = leer_rss_anterior()

    todas = dict(anteriores)
    todas.update(nuevas)

    ordenadas = sorted(
        todas.values(),
        key=lambda noticia: noticia["fecha"],
        reverse=True,
    )

    print(f"Noticias recuperadas ahora: {len(nuevas)}")
    print(
        f"Noticias del RSS anterior: {len(anteriores)}"
    )
    print(
        f"Noticias totales que se publicarán: "
        f"{len(ordenadas)}"
    )

    if not ordenadas:
        raise RuntimeError(
            "No se encontró ninguna noticia de Oryzon "
            "y tampoco existe un RSS anterior."
        )

    escribir_rss(ordenadas)

    print("RSS de Oryzon generado correctamente.")


if __name__ == "__main__":
    main()
