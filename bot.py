"""CholloPage - bot de chollos de Amazon para Facebook e Instagram.

Todo en un archivo a proposito, para que se pueda leer y editar entero
desde la web de GitHub sin descargar nada.

Uso:  python bot.py preparar   ->  elige oferta y genera la imagen
      python bot.py publicar   ->  publica en Meta y actualiza la bio

Con EN_SECO=1 no llama a ninguna API: genera imagen, textos y pagina.
"""
import csv
import html
import io
import json
import os
import random
import shutil
import sys
import textwrap
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import requests
from PIL import Image, ImageDraw, ImageFont


# ======================================================================
# CONFIGURACION  (todo se lee de variables de entorno)
# ======================================================================

# --- Amazon Afiliados ---
AMAZON_TAG = os.environ.get("AMAZON_TAG", "tuetiqueta-21")
AMAZON_DOMINIO = os.environ.get("AMAZON_DOMINIO", "www.amazon.es")

# --- Meta (Facebook + Instagram) ---
META_VERSION = os.environ.get("META_VERSION", "v26.0")
META_TOKEN = os.environ.get("META_TOKEN", "")          # token de larga duracion
FB_PAGE_ID = os.environ.get("FB_PAGE_ID", "")
IG_USER_ID = os.environ.get("IG_USER_ID", "")

# --- GitHub Pages (link-in-bio y alojamiento de imagenes) ---
# Ej: https://chollopage.github.io/chollos-bot
PAGES_URL = os.environ.get("PAGES_URL", "").rstrip("/")

# --- Comportamiento ---
# En seco no se llama a ninguna API: genera imagen, copy y pagina, y para.
EN_SECO = os.environ.get("EN_SECO", "0") == "1"
# Cuantas ofertas se muestran en la pagina de bio
BIO_MAX = int(os.environ.get("BIO_MAX", "12"))

MARCA = os.environ.get("MARCA", "CholloPage")

# Divulgacion obligatoria del vinculo de afiliado (Amazon la exige en cada
# publicacion y en la pagina de destino). No la quites.
DIVULGACION = (
    "Enlace de afiliado: si compras, gano una pequena comision sin coste extra para ti."
)


# ======================================================================
# TEXTOS DE LAS PUBLICACIONES
# Plantillas rotatorias: sin llamadas a IA, coste cero y predecible.
# ======================================================================

APERTURAS = [
    "Ojo a esto",
    "Chollo del dia",
    "Esto no dura",
    "Se ha desplomado",
    "Baja de precio ahora",
    "Vaya precio",
]

CIERRES = [
    "El enlace esta en la bio.",
    "Enlace en la bio.",
    "Te lo dejo en la bio.",
]

HASHTAGS = {
    "Tecnologia": "#chollos #ofertas #tecnologia #gadgets #amazon",
    "Hogar": "#chollos #ofertas #hogar #cocina #amazon",
    "Moda": "#chollos #ofertas #moda #amazon",
    "Deporte": "#chollos #ofertas #deporte #fitness #amazon",
}
HASHTAGS_POR_DEFECTO = "#chollos #ofertas #amazon #descuentos"


def descuento(precio_ahora: float, precio_antes: float) -> int:
    if not precio_antes or precio_antes <= precio_ahora:
        return 0
    return round((1 - precio_ahora / precio_antes) * 100)


def enlace_afiliado(asin: str) -> str:
    """Enlace directo y legible. Amazon prohibe acortadores que oculten
    que el destino es Amazon, asi que nada de bit.ly aqui."""
    return (
        f"https://{AMAZON_DOMINIO}/dp/{asin}"
        f"?tag={AMAZON_TAG}&linkCode=ll1"
    )


def texto_facebook(oferta: dict) -> str:
    pct = descuento(oferta["precio_ahora"], oferta["precio_antes"])
    partes = [f"{random.choice(APERTURAS)}: {oferta['titulo']}"]
    if pct:
        partes.append(f"{oferta['precio_ahora']:.2f} EUR en vez de los {oferta['precio_antes']:.2f} EUR de PVP (-{pct}%)")
    else:
        partes.append(f"{oferta['precio_ahora']:.2f} EUR")
    if oferta.get("gancho"):
        partes.append(oferta["gancho"] + ".")
    partes.append(enlace_afiliado(oferta["asin"]))
    partes.append(DIVULGACION)
    return "\n\n".join(partes)


def texto_instagram(oferta: dict) -> str:
    """En Instagram el enlace del pie no es clicable, asi que el copy
    empuja a la bio, donde vive el enlace real."""
    pct = descuento(oferta["precio_ahora"], oferta["precio_antes"])
    partes = [f"{random.choice(APERTURAS)}: {oferta['titulo']}"]
    if pct:
        partes.append(f"{oferta['precio_ahora']:.2f} EUR en vez de los {oferta['precio_antes']:.2f} EUR de PVP (-{pct}%)")
    else:
        partes.append(f"{oferta['precio_ahora']:.2f} EUR")
    if oferta.get("gancho"):
        partes.append(oferta["gancho"] + ".")
    partes.append(random.choice(CIERRES))
    partes.append(DIVULGACION)
    partes.append(HASHTAGS.get(oferta.get("categoria"), HASHTAGS_POR_DEFECTO))
    return "\n\n".join(partes)


def texto_tiktok(oferta: dict) -> str:
    pct = descuento(oferta["precio_ahora"], oferta["precio_antes"])
    cabeza = f"{oferta['titulo']} por {oferta['precio_ahora']:.2f} EUR"
    if pct:
        cabeza += f" (-{pct}%)"
    return "\n".join([
        cabeza,
        random.choice(CIERRES),
        DIVULGACION,
        HASHTAGS.get(oferta.get("categoria"), HASHTAGS_POR_DEFECTO),
    ])


# ======================================================================
# GENERACION DE LA IMAGEN
#
# La foto la aportas tu: columna imagen_url en ofertas.csv, o un archivo
# en imagenes/<ASIN>.jpg. El bot no la descarga de la ficha de Amazon
# porque su acuerdo de afiliados solo permite obtener sus imagenes a
# traves de la Product Advertising API.
# ======================================================================

LADO = 1080
RUTAS_FUENTE = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
]

FONDO = (17, 20, 28)
ACENTO = (255, 214, 61)
TEXTO = (245, 246, 250)
APAGADO = (150, 156, 170)


def _fuente(tam: int, negrita: bool = True):
    for ruta in RUTAS_FUENTE:
        if os.path.exists(ruta):
            try:
                return ImageFont.truetype(ruta, tam)
            except OSError:
                continue
    return ImageFont.load_default()


def _ancho(draw, texto, fuente):
    caja = draw.textbbox((0, 0), texto, font=fuente)
    return caja[2] - caja[0]


def _centrar(draw, y, texto, fuente, color):
    draw.text(((LADO - _ancho(draw, texto, fuente)) / 2, y), texto, font=fuente, fill=color)


def cargar_foto(oferta: dict):
    """Foto del producto: primero un archivo local, luego una URL."""
    for ext in ("jpg", "jpeg", "png", "webp"):
        ruta = os.path.join("imagenes", f"{oferta['asin']}.{ext}")
        if os.path.exists(ruta):
            try:
                return Image.open(ruta).convert("RGB")
            except OSError:
                pass
    url = (oferta.get("imagen_url") or "").strip()
    if url.startswith("http"):
        try:
            r = requests.get(url, timeout=30, headers={"User-Agent": "bot-chollos/1.0"})
            r.raise_for_status()
            return Image.open(io.BytesIO(r.content)).convert("RGB")
        except (requests.RequestException, OSError) as e:
            print(f"AVISO no se pudo cargar la foto del producto: {e}")
    return None


def _fondo():
    """Degradado de marca, siempre el mismo, para que las publicaciones
    se lean como una serie."""
    img = Image.new("RGB", (LADO, LADO), FONDO)
    dib = ImageDraw.Draw(img)
    for y in range(LADO):
        f = y / LADO
        dib.line(
            [(0, y), (LADO, y)],
            fill=(int(17 + 16 * f), int(20 + 14 * f), int(28 + 24 * f)),
        )
    return img


def _pie(dib) -> None:
    dib.line([(80, 940), (LADO - 80, 940)], fill=(60, 66, 80), width=2)
    _centrar(dib, 968, MARCA.upper(), _fuente(32), APAGADO)
    _centrar(dib, 1014, "ENLACE EN LA BIO  ·  PUBLICIDAD", _fuente(24, negrita=False), APAGADO)


def _insignia(dib, pct: int) -> None:
    if not pct:
        return
    f_pct = _fuente(64)
    etiqueta = f"-{pct}%"
    an = _ancho(dib, etiqueta, f_pct)
    dib.rounded_rectangle([(LADO - an - 150, 70), (LADO - 70, 190)], radius=24, fill=ACENTO)
    dib.text((LADO - an - 110, 92), etiqueta, font=f_pct, fill=FONDO)


def generar_foto(oferta: dict, foto, destino: str) -> str:
    """La imagen de la publicacion: foto del producto, precio actual,
    precio anterior tachado e insignia de descuento.

    Se cuadra a 1080x1080 porque es la proporcion que mejor rinde en el
    feed de Instagram y de Facebook, y no recorta en ninguno de los dos.
    """
    img = _fondo()
    dib = ImageDraw.Draw(img)
    pct = descuento(oferta["precio_ahora"], oferta["precio_antes"])

    # Tarjeta blanca con la foto dentro, sin deformarla
    caja = (90, 56, 990, 756)
    dib.rounded_rectangle(caja, radius=28, fill=(255, 255, 255))
    hueco_an, hueco_al = caja[2] - caja[0] - 80, caja[3] - caja[1] - 80
    producto = foto.copy()
    producto.thumbnail((hueco_an, hueco_al), Image.LANCZOS)
    img.paste(
        producto,
        (
            caja[0] + (caja[2] - caja[0] - producto.width) // 2,
            caja[1] + (caja[3] - caja[1] - producto.height) // 2,
        ),
    )

    _insignia(dib, pct)

    # Titulo en una linea
    titulo = oferta["titulo"]
    f_tit = _fuente(38)
    while _ancho(dib, titulo, f_tit) > LADO - 140 and len(titulo) > 12:
        titulo = titulo[:-2].rstrip()
    if titulo != oferta["titulo"]:
        titulo = titulo.rsplit(" ", 1)[0] + "..."
    _centrar(dib, 782, titulo, f_tit, TEXTO)

    # Precio actual y precio anterior tachado, centrados como un bloque
    f_ahora = _fuente(76)
    ahora = f"{oferta['precio_ahora']:.2f} EUR"
    an_ahora = _ancho(dib, ahora, f_ahora)

    if pct:
        f_antes = _fuente(40, negrita=False)
        antes = f"PVP {oferta['precio_antes']:.2f} EUR"
        an_antes = _ancho(dib, antes, f_antes)
        hueco = 26
        x = (LADO - (an_ahora + hueco + an_antes)) / 2
        dib.text((x, 840), ahora, font=f_ahora, fill=ACENTO)
        x_antes = x + an_ahora + hueco
        dib.text((x_antes, 872), antes, font=f_antes, fill=APAGADO)
        dib.line(
            [(x_antes - 4, 894), (x_antes + an_antes + 4, 894)],
            fill=APAGADO,
            width=3,
        )
    else:
        _centrar(dib, 840, ahora, f_ahora, ACENTO)

    _pie(dib)
    os.makedirs(os.path.dirname(destino), exist_ok=True)
    img.save(destino, "JPEG", quality=90, optimize=True)
    return destino


def generar(oferta: dict, destino: str, foto=None) -> str:
    """Version sin foto: tarjeta tipografica. Es el plan B cuando una fila
    de ofertas.csv no trae imagen."""
    img = _fondo()
    dib = ImageDraw.Draw(img)
    pct = descuento(oferta["precio_ahora"], oferta["precio_antes"])

    _insignia(dib, pct)
    _centrar(dib, 130, (oferta.get("categoria") or "CHOLLO").upper(), _fuente(30), APAGADO)

    titulo = oferta["titulo"]
    if len(titulo) > 90:
        titulo = titulo[:87].rsplit(" ", 1)[0] + "..."
    f_tit = _fuente(58)
    lineas = textwrap.wrap(titulo, width=26)[:4]
    y = 300 - (len(lineas) - 1) * 34
    for linea in lineas:
        _centrar(dib, y, linea, f_tit, TEXTO)
        y += 76

    y = max(y + 50, 500)
    _centrar(dib, y, f"{oferta['precio_ahora']:.2f} EUR", _fuente(140), ACENTO)

    if pct:
        f_ant = _fuente(46, negrita=False)
        antes = f"PVP {oferta['precio_antes']:.2f} EUR"
        an = _ancho(dib, antes, f_ant)
        x = (LADO - an) / 2
        dib.text((x, y + 190), antes, font=f_ant, fill=APAGADO)
        dib.line([(x, y + 215), (x + an, y + 215)], fill=APAGADO, width=3)

    if oferta.get("gancho"):
        _centrar(dib, y + 285, oferta["gancho"].upper(), _fuente(36), TEXTO)

    _pie(dib)
    os.makedirs(os.path.dirname(destino), exist_ok=True)
    img.save(destino, "JPEG", quality=88, optimize=True)
    return destino


def generar_historia(oferta: dict, foto, destino: str) -> str:
    """Version vertical 1080x1920 para historias de Instagram.

    Ojo: Meta no permite anadir el sticker de enlace por API, asi que la
    historia sale sin enlace clicable. Por eso el pie empuja a la bio.
    """
    AL = 1920
    img = Image.new("RGB", (LADO, AL), FONDO)
    dib = ImageDraw.Draw(img)
    for y in range(AL):
        f = y / AL
        dib.line([(0, y), (LADO, y)],
                 fill=(int(17 + 16 * f), int(20 + 14 * f), int(28 + 24 * f)))

    pct = descuento(oferta["precio_ahora"], oferta["precio_antes"])

    _centrar(dib, 150, MARCA.upper(), _fuente(34), APAGADO)

    caja = (90, 300, 990, 1160)
    dib.rounded_rectangle(caja, radius=32, fill=(255, 255, 255))
    if foto is not None:
        hueco_an, hueco_al = caja[2] - caja[0] - 90, caja[3] - caja[1] - 90
        producto = foto.copy()
        producto.thumbnail((hueco_an, hueco_al), Image.LANCZOS)
        img.paste(producto, (caja[0] + (caja[2] - caja[0] - producto.width) // 2,
                             caja[1] + (caja[3] - caja[1] - producto.height) // 2))

    if pct:
        f_pct = _fuente(72)
        etiqueta = f"-{pct}%"
        an = _ancho(dib, etiqueta, f_pct)
        dib.rounded_rectangle([(LADO - an - 160, 240), (LADO - 60, 372)],
                              radius=28, fill=ACENTO)
        dib.text((LADO - an - 110, 264), etiqueta, font=f_pct, fill=FONDO)

    titulo = oferta["titulo"]
    f_tit = _fuente(46)
    while _ancho(dib, titulo, f_tit) > LADO - 140 and len(titulo) > 12:
        titulo = titulo[:-2].rstrip()
    if titulo != oferta["titulo"]:
        titulo = titulo.rsplit(" ", 1)[0] + "..."
    _centrar(dib, 1240, titulo, f_tit, TEXTO)

    f_ahora = _fuente(110)
    ahora = f"{oferta['precio_ahora']:.2f} EUR"
    if pct:
        f_antes = _fuente(52, negrita=False)
        antes = f"PVP {oferta['precio_antes']:.2f} EUR"
        a1, a2 = _ancho(dib, ahora, f_ahora), _ancho(dib, antes, f_antes)
        x = (LADO - (a1 + 32 + a2)) / 2
        dib.text((x, 1340), ahora, font=f_ahora, fill=ACENTO)
        xa = x + a1 + 32
        dib.text((xa, 1392), antes, font=f_antes, fill=APAGADO)
        dib.line([(xa - 6, 1422), (xa + a2 + 6, 1422)], fill=APAGADO, width=4)
    else:
        _centrar(dib, 1340, ahora, f_ahora, ACENTO)

    dib.line([(LADO / 2 - 70, 1660), (LADO / 2, 1590), (LADO / 2 + 70, 1660)],
             fill=ACENTO, width=22, joint="curve")
    _centrar(dib, 1700, "ENLACE EN LA BIO", _fuente(44), ACENTO)
    _centrar(dib, 1810, "PUBLICIDAD  ·  ENLACE DE AFILIADO",
             _fuente(28, negrita=False), APAGADO)

    os.makedirs(os.path.dirname(destino), exist_ok=True)
    img.save(destino, "JPEG", quality=88, optimize=True)
    return destino

# ======================================================================
# PUBLICACION EN FACEBOOK E INSTAGRAM
#
# Las dos cuentas son tuyas y la app se queda en modo desarrollo:
# publicar en tu propia Pagina y en tu propio Instagram Business no
# requiere pasar la App Review de Meta.
# ======================================================================

TIEMPO_ESPERA = 60


def _base() -> str:
    return f"https://graph.facebook.com/{META_VERSION}"


def _comprobar(resp) -> dict:
    try:
        datos = resp.json()
    except ValueError:
        resp.raise_for_status()
        raise RuntimeError(f"Respuesta no JSON de Meta: {resp.text[:300]}")
    if "error" in datos:
        err = datos["error"]
        raise RuntimeError(
            f"Meta {err.get('code')}/{err.get('error_subcode')}: {err.get('message')}"
        )
    resp.raise_for_status()
    return datos


def publicar_facebook(urls_imagen: list, texto: str) -> str:
    """Post en la Pagina. El enlace de afiliado va en el texto, que aqui
    si es clicable."""
    if len(urls_imagen) == 1:
        resp = requests.post(
            f"{_base()}/{FB_PAGE_ID}/photos",
            data={
                "url": urls_imagen[0],
                "caption": texto,
                "published": "true",
                "access_token": META_TOKEN,
            },
            timeout=TIEMPO_ESPERA,
        )
        datos = _comprobar(resp)
        return datos.get("post_id") or datos.get("id", "")

    ids = []
    for url in urls_imagen:
        subida = requests.post(
            f"{_base()}/{FB_PAGE_ID}/photos",
            data={
                "url": url,
                "published": "false",
                "temporary": "true",
                "access_token": META_TOKEN,
            },
            timeout=TIEMPO_ESPERA,
        )
        ids.append(_comprobar(subida)["id"])

    datos_post = {"message": texto, "access_token": META_TOKEN}
    for i, media_id in enumerate(ids):
        datos_post[f"attached_media[{i}]"] = json.dumps({"media_fbid": media_id})

    resp = requests.post(
        f"{_base()}/{FB_PAGE_ID}/feed", data=datos_post, timeout=TIEMPO_ESPERA
    )
    return _comprobar(resp).get("id", "")


def _esperar_contenedor(contenedor: str) -> None:
    """Instagram procesa cada imagen en segundo plano; hay que esperar."""
    for _ in range(12):
        time.sleep(5)
        estado = requests.get(
            f"{_base()}/{contenedor}",
            params={"fields": "status_code", "access_token": META_TOKEN},
            timeout=TIEMPO_ESPERA,
        )
        codigo = _comprobar(estado).get("status_code")
        if codigo == "FINISHED":
            return
        if codigo == "ERROR":
            raise RuntimeError(f"Instagram no pudo procesar el contenedor {contenedor}")
    raise RuntimeError(f"El contenedor {contenedor} no estuvo listo a tiempo")


def publicar_instagram(urls_imagen: list, texto: str) -> str:
    """Instagram va en dos pasos: se crea el contenedor y luego se publica."""
    if len(urls_imagen) == 1:
        creacion = requests.post(
            f"{_base()}/{IG_USER_ID}/media",
            data={
                "image_url": urls_imagen[0],
                "caption": texto,
                "access_token": META_TOKEN,
            },
            timeout=TIEMPO_ESPERA,
        )
        contenedor = _comprobar(creacion)["id"]
        _esperar_contenedor(contenedor)
    else:
        hijos = []
        for url in urls_imagen[:10]:
            hijo = requests.post(
                f"{_base()}/{IG_USER_ID}/media",
                data={
                    "image_url": url,
                    "is_carousel_item": "true",
                    "access_token": META_TOKEN,
                },
                timeout=TIEMPO_ESPERA,
            )
            hijos.append(_comprobar(hijo)["id"])
        for hijo in hijos:
            _esperar_contenedor(hijo)

        padre = requests.post(
            f"{_base()}/{IG_USER_ID}/media",
            data={
                "media_type": "CAROUSEL",
                "children": ",".join(hijos),
                "caption": texto,
                "access_token": META_TOKEN,
            },
            timeout=TIEMPO_ESPERA,
        )
        contenedor = _comprobar(padre)["id"]
        _esperar_contenedor(contenedor)

    publicacion = requests.post(
        f"{_base()}/{IG_USER_ID}/media_publish",
        data={"creation_id": contenedor, "access_token": META_TOKEN},
        timeout=TIEMPO_ESPERA,
    )
    return _comprobar(publicacion)["id"]


def publicar_historia_instagram(url_imagen: str) -> str:
    """Historia de Instagram: dura 24 horas y no admite pie de texto.
    El sticker de enlace hay que ponerlo a mano desde la app, porque Meta
    no lo expone por API."""
    creacion = requests.post(
        f"{_base()}/{IG_USER_ID}/media",
        data={
            "image_url": url_imagen,
            "media_type": "STORIES",
            "access_token": META_TOKEN,
        },
        timeout=TIEMPO_ESPERA,
    )
    contenedor = _comprobar(creacion)["id"]
    _esperar_contenedor(contenedor)
    publicacion = requests.post(
        f"{_base()}/{IG_USER_ID}/media_publish",
        data={"creation_id": contenedor, "access_token": META_TOKEN},
        timeout=TIEMPO_ESPERA,
    )
    return _comprobar(publicacion)["id"]

# ======================================================================
# PAGINA LINK-IN-BIO
#
# La pieza que hace funcionar Instagram y TikTok: el enlace de la bio
# apunta siempre aqui, y aqui aparece cada oferta nueva arriba del todo.
# ======================================================================

PLANTILLA = """<!doctype html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{marca}</title>
<meta name="description" content="Los ultimos chollos de Amazon, actualizados cada dos horas.">
<style>
  :root {{
    --fondo: #0f1218; --tarjeta: #171b24; --borde: #262c38;
    --texto: #f2f4f8; --apagado: #99a1b3; --acento: #ffd63d;
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0; padding: 32px 18px 64px;
    background: var(--fondo); color: var(--texto);
    font: 16px/1.55 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  }}
  .envoltorio {{ max-width: 520px; margin: 0 auto; }}
  header {{ text-align: center; margin-bottom: 12px; }}
  h1 {{ font-size: 26px; margin: 0 0 6px; letter-spacing: -0.4px; }}
  .lema {{ color: var(--apagado); font-size: 14px; margin: 0; }}
  .aviso {{
    margin: 22px 0; padding: 12px 14px; border-radius: 12px;
    background: rgba(255,214,61,.08); border: 1px solid rgba(255,214,61,.22);
    color: var(--apagado); font-size: 13px; line-height: 1.5;
  }}
  a.oferta {{
    display: block; text-decoration: none; color: inherit;
    background: var(--tarjeta); border: 1px solid var(--borde);
    border-radius: 16px; padding: 16px; margin-bottom: 12px;
    transition: border-color .15s ease, transform .15s ease;
  }}
  a.oferta:hover {{ border-color: var(--acento); transform: translateY(-2px); }}
  .fila {{ display: flex; gap: 14px; align-items: center; }}
  .miniatura {{
    width: 76px; height: 76px; border-radius: 10px; object-fit: cover;
    flex: 0 0 auto; background: var(--fondo);
  }}
  .titulo {{ font-weight: 600; font-size: 15px; margin: 0 0 6px; }}
  .precios {{ display: flex; gap: 8px; align-items: baseline; flex-wrap: wrap; }}
  .ahora {{ color: var(--acento); font-weight: 700; font-size: 18px; }}
  .antes {{ color: var(--apagado); text-decoration: line-through; font-size: 13px; }}
  .pct {{
    background: var(--acento); color: #0f1218; font-weight: 700;
    font-size: 12px; padding: 2px 7px; border-radius: 999px;
  }}
  .cuando {{ color: var(--apagado); font-size: 12px; margin-top: 8px; }}
  footer {{ text-align: center; color: var(--apagado); font-size: 12px; margin-top: 28px; }}
  .vacio {{ text-align: center; color: var(--apagado); padding: 40px 0; }}
  @media (prefers-color-scheme: light) {{
    :root {{
      --fondo: #f6f7fa; --tarjeta: #ffffff; --borde: #e2e6ee;
      --texto: #12161f; --apagado: #626b7d; --acento: #b07d00;
    }}
    .pct {{ color: #fff; }}
  }}
</style>
</head>
<body>
<div class="envoltorio">
  <header>
    <h1>{marca}</h1>
    <p class="lema">Chollos de Amazon, actualizados cada dos horas</p>
  </header>
  <p class="aviso">Como afiliado de Amazon, gano una comision por las compras
  que cumplan los requisitos. No te cuesta nada de mas.</p>
  {tarjetas}
  <footer>Ultima actualizacion: {actualizado} (hora peninsular)</footer>
</div>
</body>
</html>
"""

TARJETA = """  <a class="oferta" href="{enlace}" target="_blank" rel="nofollow sponsored noopener">
    <div class="fila">
      <img class="miniatura" src="{imagen}" alt="" loading="lazy">
      <div>
        <p class="titulo">{titulo}</p>
        <div class="precios">
          <span class="ahora">{ahora} EUR</span>
          {bloque_antes}
        </div>
      </div>
    </div>
    <div class="cuando">Publicado el {cuando}</div>
  </a>
"""


def regenerar(ruta_estado: str = "estado.json", destino: str = "docs/index.html") -> str:
    estado = _leer_estado(ruta_estado)
    entradas = list(reversed(estado.get("publicadas", [])))[:BIO_MAX]

    tarjetas = []
    for e in entradas:
        pct = descuento(e["precio_ahora"], e.get("precio_antes") or 0)
        bloque_antes = ""
        if pct:
            bloque_antes = (
                f'<span class="antes">PVP {e["precio_antes"]:.2f} EUR</span>'
                f'<span class="pct">-{pct}%</span>'
            )
        tarjetas.append(
            TARJETA.format(
                enlace=html.escape(enlace_afiliado(e["asin"]), quote=True),
                imagen=html.escape(e.get("imagen", ""), quote=True),
                titulo=html.escape(e["titulo"]),
                ahora=f'{e["precio_ahora"]:.2f}',
                bloque_antes=bloque_antes,
                cuando=e.get("fecha", "")[:16].replace("T", " "),
            )
        )

    if not tarjetas:
        tarjetas = ['  <p class="vacio">Todavia no hay ofertas publicadas.</p>']

    pagina = PLANTILLA.format(
        marca=html.escape(MARCA),
        tarjetas="\n".join(tarjetas),
        actualizado=datetime.now(ZoneInfo("Europe/Madrid")).strftime("%d/%m/%Y a las %H:%M"),
    )
    os.makedirs(os.path.dirname(destino), exist_ok=True)
    with open(destino, "w", encoding="utf-8") as f:
        f.write(pagina)
    return destino


# ======================================================================
# ORQUESTADOR
#
# Dos fases, porque Instagram exige que la imagen ya este accesible en
# una URL publica antes de publicarla:
#   preparar -> elige oferta y genera la imagen en docs/img/
#               (el workflow hace commit y push, y GitHub ya la sirve)
#   publicar -> espera a que la URL responda, publica y regenera la bio
# ======================================================================

RUTA_OFERTAS = "ofertas.csv"
RUTA_ESTADO = "estado.json"
RUTA_PENDIENTE = "pendiente.json"
DIR_IMG = "docs/img"
DIR_TIKTOK = "salida_tiktok"


def _leer_estado(ruta: str = RUTA_ESTADO) -> dict:
    if not os.path.exists(ruta):
        return {"publicadas": []}
    with open(ruta, encoding="utf-8") as f:
        return json.load(f)


def _guardar_estado(estado: dict) -> None:
    with open(RUTA_ESTADO, "w", encoding="utf-8") as f:
        json.dump(estado, f, ensure_ascii=False, indent=2)


def _a_float(valor) -> float:
    return float(str(valor).replace(",", ".").replace("EUR", "").strip() or 0)


def _leer_ofertas() -> list:
    if not os.path.exists(RUTA_OFERTAS):
        return []
    ofertas = []
    with open(RUTA_OFERTAS, encoding="utf-8-sig", newline="") as f:
        for fila in csv.DictReader(f):
            asin = (fila.get("asin") or "").strip()
            titulo = (fila.get("titulo") or "").strip()
            if not asin or not titulo:
                continue
            ofertas.append({
                "asin": asin,
                "titulo": titulo,
                "precio_ahora": _a_float(fila.get("precio_ahora", 0)),
                "precio_antes": _a_float(fila.get("precio_antes", 0)),
                "categoria": (fila.get("categoria") or "").strip(),
                "gancho": (fila.get("gancho") or "").strip(),
                "imagen_url": (fila.get("imagen_url") or "").strip(),
            })
    return ofertas


def _siguiente(ofertas: list, estado: dict):
    ya = {p["asin"] for p in estado.get("publicadas", [])}
    for oferta in ofertas:
        if oferta["asin"] not in ya and oferta["precio_ahora"] > 0:
            return oferta
    return None


def _base_imagenes() -> str:
    base = os.environ.get("IMAGENES_BASE", "").rstrip("/")
    if base:
        return base
    if PAGES_URL:
        return f"{PAGES_URL}/img"
    raise RuntimeError("Falta PAGES_URL o IMAGENES_BASE para servir la imagen")


def _esperar_url(url: str, intentos: int = 20, espera: int = 6) -> None:
    for i in range(intentos):
        try:
            r = requests.head(url, timeout=20, allow_redirects=True)
            if r.status_code == 200:
                print(f"Imagen accesible tras {i * espera}s: {url}")
                return
        except requests.RequestException:
            pass
        time.sleep(espera)
    raise RuntimeError(f"La imagen no llego a estar accesible: {url}")


def preparar() -> int:
    estado = _leer_estado()
    oferta = _siguiente(_leer_ofertas(), estado)
    if not oferta:
        print("No hay ofertas pendientes en ofertas.csv. Nada que hacer.")
        if os.path.exists(RUTA_PENDIENTE):
            os.remove(RUTA_PENDIENTE)
        return 0

    marca_tiempo = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M")
    nombre = f"{marca_tiempo}-{oferta['asin']}.jpg"
    ruta = os.path.join(DIR_IMG, nombre)

    foto = cargar_foto(oferta)
    if foto is not None:
        generar_foto(oferta, foto, ruta)
        print(f"Imagen con foto de producto: {nombre}")
    else:
        generar(oferta, ruta)
        print(f"Sin foto (ni imagenes/<ASIN> ni imagen_url): tarjeta de precio {nombre}")

    nombre_historia = f"{marca_tiempo}-{oferta['asin']}-historia.jpg"
    generar_historia(oferta, foto, os.path.join(DIR_IMG, nombre_historia))
    print(f"Historia vertical: {nombre_historia}")

    with open(RUTA_PENDIENTE, "w", encoding="utf-8") as f:
        json.dump({"oferta": oferta, "imagenes": [nombre], "historia": nombre_historia},
                  f, ensure_ascii=False, indent=2)
    return 0


def publicar() -> int:
    if not os.path.exists(RUTA_PENDIENTE):
        print("No hay nada preparado. Fin.")
        return 0

    with open(RUTA_PENDIENTE, encoding="utf-8") as f:
        pendiente = json.load(f)
    oferta, nombres = pendiente["oferta"], pendiente["imagenes"]
    nombre_historia = pendiente.get("historia")

    # Paquete de TikTok: eso lo publicas tu a mano desde el movil
    os.makedirs(DIR_TIKTOK, exist_ok=True)
    for nombre in nombres + ([nombre_historia] if nombre_historia else []):
        shutil.copy(os.path.join(DIR_IMG, nombre), os.path.join(DIR_TIKTOK, nombre))
    with open(os.path.join(DIR_TIKTOK, "texto.txt"), "w", encoding="utf-8") as f:
        f.write(texto_tiktok(oferta))
    print("Paquete de TikTok listo en salida_tiktok/")

    if EN_SECO:
        print("--- MODO EN SECO, no se llama a Meta ---")
        print("FACEBOOK:\n" + texto_facebook(oferta))
        print("\nINSTAGRAM:\n" + texto_instagram(oferta))
    else:
        urls = [f"{_base_imagenes()}/{n}" for n in nombres]
        for url in urls:
            _esperar_url(url)
        try:
            print(f"Facebook publicado: {publicar_facebook(urls, texto_facebook(oferta))}")
        except Exception as e:  # si falla uno, el otro puede seguir
            print(f"AVISO Facebook fallo: {e}")
        try:
            print(f"Instagram publicado: {publicar_instagram(urls, texto_instagram(oferta))}")
        except Exception as e:
            print(f"AVISO Instagram fallo: {e}")
        if nombre_historia:
            try:
                url_h = f"{_base_imagenes()}/{nombre_historia}"
                _esperar_url(url_h)
                print(f"Historia publicada: {publicar_historia_instagram(url_h)}")
            except Exception as e:
                print(f"AVISO la historia fallo: {e}")

    estado = _leer_estado()
    estado.setdefault("publicadas", []).append({
        "asin": oferta["asin"],
        "titulo": oferta["titulo"],
        "precio_ahora": oferta["precio_ahora"],
        "precio_antes": oferta["precio_antes"],
        "imagen": f"img/{nombres[0]}",
        "fecha": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    })
    _guardar_estado(estado)
    regenerar(RUTA_ESTADO, "docs/index.html")
    print("Pagina de bio regenerada con la oferta nueva arriba del todo.")

    os.remove(RUTA_PENDIENTE)
    return 0


if __name__ == "__main__":
    accion = sys.argv[1] if len(sys.argv) > 1 else "preparar"
    sys.exit(preparar() if accion == "preparar" else publicar())
