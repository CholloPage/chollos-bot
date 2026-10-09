"""CholloPage - bot de chollos de Amazon para Facebook e Instagram.

Todo en un archivo a proposito, para que se pueda leer y editar entero
desde la web de GitHub sin descargar nada.

Uso:  python bot.py preparar   ->  elige oferta y genera la imagen
      python bot.py publicar   ->  publica en Meta y Telegram y actualiza la bio

Con EN_SECO=1 no llama a ninguna API: genera imagen, textos y pagina.
"""
import csv
import html
import io
import json
import os
import random
import re
import shutil
import subprocess
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

# --- Telegram (opcional: si falta alguno, el bot ni lo intenta) ---
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CANAL = os.environ.get("TELEGRAM_CANAL", "").strip()   # ej. @chollopage

# --- Que se publica en esta ejecucion ---
# Lo decide el workflow segun la hora: en las franjas punta sale en todas
# partes, en el resto solo en Telegram. Asi Instagram y Facebook reciben
# pocas publicaciones y buenas, y el canal va servido todo el dia.
SOLO_TELEGRAM = os.environ.get("SOLO_TELEGRAM", "0") == "1"
# Instagram y Facebook como Reel. Si el Reel falla, sale la foto de siempre.
REELS = os.environ.get("REELS", "1") == "1"

# --- GitHub Pages (link-in-bio y alojamiento de imagenes) ---
# Ej: https://chollopage.github.io/chollos-bot
PAGES_URL = os.environ.get("PAGES_URL", "").rstrip("/")

# --- Comportamiento ---
# En seco no se llama a ninguna API: genera imagen, copy y pagina, y para.
EN_SECO = os.environ.get("EN_SECO", "0") == "1"
# Cuantas ofertas se muestran en la pagina de bio
BIO_MAX = int(os.environ.get("BIO_MAX", "100"))

MARCA = os.environ.get("MARCA", "CholloPage")

# Divulgacion obligatoria del vinculo de afiliado (Amazon la exige en cada
# publicacion y en la pagina de destino). No la quites.
DIVULGACION = (
    "Enlace de afiliado: si compras, me llevo una pequeña comisión "
    "y a ti no te cuesta nada más. "
    "La oferta es de Amazon.es: nosotros no vendemos nada."
)


# ======================================================================
# TEXTOS DE LAS PUBLICACIONES
# Plantillas rotatorias: sin llamadas a IA, coste cero y predecible.
# ======================================================================

# La primera linea es lo unico que se ve sin pulsar "mas", asi que tiene
# que decir algo. Nada de promesas que no podamos comprobar ("minimo
# historico", "nunca visto"): solo lo que dicen los precios.
APERTURAS = [
    "Ha bajado bastante",
    "Rebajado",
    "Si lo estabas mirando, hoy sale más barato",
    "Chollo",
    "Atento a este",
    "Buena rebaja",
    "Para apuntar",
    "Hoy compensa",
]

CIERRES = [
    "Enlace en la bio.",
    "El enlace lo tienes en la bio.",
    "Lo encuentras en el enlace de la bio, arriba del todo.",
    "Link en la bio.",
]

# Hashtags concretos por categoria. Los genericos (#ofertas, #amazon)
# tienen millones de publicaciones y una cuenta nueva no asoma nunca;
# estos son mas estrechos y tienen publico que busca exactamente eso.
# #chollosamazon y #chollosdeldia van siempre, son los de la casa.
HASHTAGS = {
    "Tecnologia": "#chollosamazon #chollosdeldia #chollostecnologicos #gadgets #tecnologia #ofertasamazon",
    "Hogar": "#chollosamazon #chollosdeldia #ofertashogar #hogar #decoracionhogar #limpiezahogar",
    "Cocina": "#chollosamazon #chollosdeldia #cocina #utensiliosdecocina #ofertascocina #recetasfaciles",
    "Deporte": "#chollosamazon #chollosdeldia #fitnessencasa #deporte #entrenamiento #ofertasdeporte",
    "Moda": "#chollosamazon #chollosdeldia #ofertasmoda #modamujer #modahombre #lookdeldia",
    "Belleza": "#chollosamazon #chollosdeldia #belleza #cuidadodelapiel #skincare #ofertasbelleza",
    "Bebe": "#chollosamazon #chollosdeldia #bebe #maternidad #mamas #ofertasbebe",
    "Mascotas": "#chollosamazon #chollosdeldia #mascotas #perros #gatos #ofertasmascotas",
}
HASHTAGS_POR_DEFECTO = "#chollosamazon #chollosdeldia #ofertasamazon #chollos #descuentos"


def euros(importe: float) -> str:
    """12345.6 -> '12.345,60 €', como se escribe en Espana."""
    t = f"{importe:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"{t} €"


def _nombre_referencia(oferta: dict) -> str:
    ref = referencia(oferta)
    if ref.lower().startswith("precio mediano"):
        return "precio mediano de los últimos 30 días"
    return ref


def linea_precio(oferta: dict) -> str:
    pct = descuento(oferta["precio_ahora"], oferta["precio_antes"])
    if not pct:
        return euros(oferta["precio_ahora"])
    return (f"{euros(oferta['precio_ahora'])} · antes {euros(oferta['precio_antes'])} "
            f"({_nombre_referencia(oferta)}) · -{pct}%")


def hora_madrid(iso: str, formato: str = "%d/%m/%Y %H:%M") -> str:
    """Las fechas se guardan en UTC; al mostrarlas, siempre en hora de Madrid."""
    if not iso:
        return ""
    try:
        d = datetime.fromisoformat(iso)
    except ValueError:
        return iso[:16].replace("T", " ")
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.astimezone(ZoneInfo("Europe/Madrid")).strftime(formato)


def enlace_compartir_canal() -> str:
    """Enlace de Telegram para reenviar el canal a un contacto."""
    from urllib.parse import quote
    canal = f"https://t.me/{usuario_telegram()}"
    texto = "Canal de chollos de Amazon con el precio y el descuento de verdad:"
    return f"https://t.me/share/url?url={quote(canal, safe='')}&text={quote(texto, safe='')}"


def cta_telegram_html() -> str:
    """Boton al canal para las paginas web. Vacio si no hay canal publico."""
    if not usuario_telegram():
        return ""
    canal = html.escape(usuario_telegram(), quote=True)
    return (f'  <a class="telegram" href="https://t.me/{canal}" target="_blank" rel="noopener">'
            f'Avisos al momento en Telegram<span>Cada chollo nuevo te llega al movil, gratis</span></a>\n')


def usuario_telegram() -> str:
    """Nombre publico del canal (sin @) para montar enlaces t.me. Si el
    canal se ha configurado por su id numerico (-100...), no hay enlace
    publico que mostrar y devolvemos cadena vacia."""
    canal = TELEGRAM_CANAL.strip().lstrip("@")
    for prefijo in ("https://t.me/", "http://t.me/", "t.me/"):
        if canal.startswith(prefijo):
            canal = canal[len(prefijo):]
    return canal if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{3,31}", canal) else ""


def linea_telegram() -> str:
    """Promocion cruzada: cada Reel empuja gente hacia el canal, que es
    donde el enlace se puede pulsar."""
    if not (TELEGRAM_TOKEN and usuario_telegram()):
        return ""
    return f"Más chollos cada día en Telegram: t.me/{usuario_telegram()}"


def referencia(oferta: dict) -> str:
    """Como se llama el precio tachado. Amazon no siempre muestra un PVP:
    unas fichas traen 'Precio recomendado', otras 'Precio mediano'. Poner
    PVP donde no lo hay seria decir algo que no es cierto."""
    return (oferta.get("referencia") or "PVP").strip()


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
    partes = [f"{random.choice(APERTURAS)} en Amazon: {oferta['titulo']}", linea_precio(oferta)]
    if oferta.get("gancho"):
        partes.append(oferta["gancho"] + ".")
    partes.append(enlace_afiliado(oferta["asin"]))
    if linea_telegram():
        partes.append(linea_telegram())
    partes.append(DIVULGACION)
    partes.append(HASHTAGS.get(oferta.get("categoria"), HASHTAGS_POR_DEFECTO))
    return "\n\n".join(partes)


def texto_instagram(oferta: dict) -> str:
    """En Instagram el enlace del pie no es clicable, asi que el copy
    empuja a la bio, donde vive el enlace real."""
    partes = [f"{random.choice(APERTURAS)} en Amazon: {oferta['titulo']}", linea_precio(oferta)]
    if oferta.get("gancho"):
        partes.append(oferta["gancho"] + ".")
    partes.append(random.choice(CIERRES))
    if linea_telegram():
        partes.append(linea_telegram())
    partes.append(DIVULGACION)
    partes.append(HASHTAGS.get(oferta.get("categoria"), HASHTAGS_POR_DEFECTO))
    return "\n\n".join(partes)


def texto_telegram(oferta: dict) -> str:
    """Telegram admite HTML sencillo. El enlace va en un boton debajo de
    la foto, no en el texto, que queda mas limpio."""
    pct = descuento(oferta["precio_ahora"], oferta["precio_antes"])
    lineas = ["<b>OFERTA EN AMAZON.ES</b>", f"<b>{html.escape(oferta['titulo'])}</b>", ""]
    if pct:
        lineas.append(f"<b>{euros(oferta['precio_ahora'])}</b>  "
                      f"<s>{euros(oferta['precio_antes'])}</s>  -{pct}%")
        lineas.append(html.escape(f"Referencia: {_nombre_referencia(oferta)}"))
    else:
        lineas.append(f"<b>{euros(oferta['precio_ahora'])}</b>")
    if oferta.get("gancho"):
        lineas.append(html.escape(oferta["gancho"]))
    lineas += ["", f"<i>{html.escape(DIVULGACION)}</i>"]
    return "\n".join(lineas)


def texto_tiktok(oferta: dict) -> str:
    pct = descuento(oferta["precio_ahora"], oferta["precio_antes"])
    cabeza = f"{oferta['titulo']} por {oferta['precio_ahora']:.2f} € en Amazon"
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
AMAZON = (255, 153, 0)  # naranja de acento para dejar claro que la oferta es de Amazon


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
    _centrar(dib, 956, f"{MARCA.upper()}  ·  CHOLLO DE AMAZON.ES", _fuente(32), AMAZON)
    _centrar(dib, 1008, "NO VENDEMOS NADA  ·  ENLACE EN LA BIO  ·  PUBLICIDAD",
             _fuente(23, negrita=False), APAGADO)


def _etiqueta_amazon(dib, x: int, y: int, tam: int = 30) -> int:
    """Etiqueta 'OFERTA EN AMAZON.ES' (texto, sin logo): deja claro de un
    vistazo que el chollo es de Amazon y que nosotros no vendemos nada."""
    f = _fuente(tam)
    texto = "OFERTA EN AMAZON.ES"
    an = _ancho(dib, texto, f)
    pad = int(tam * 0.55)
    al = int(tam * 1.9)
    dib.rounded_rectangle([(x, y), (x + an + 2 * pad, y + al)], radius=al // 2, fill=AMAZON)
    dib.text((x + pad, y + (al - tam) / 2 - tam * 0.12), texto, font=f, fill=FONDO)
    return an + 2 * pad


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
    _etiqueta_amazon(dib, 120, 84, 30)

    # Titulo en una linea
    titulo = oferta["titulo"]
    f_tit = _fuente(38)
    while _ancho(dib, titulo, f_tit) > LADO - 140 and len(titulo) > 12:
        titulo = titulo[:-2].rstrip()
    if titulo != oferta["titulo"]:
        titulo = titulo.rsplit(" ", 1)[0] + "..."
    _centrar(dib, 782, titulo, f_tit, TEXTO)

    # Precio actual y precio anterior tachado, centrados como un bloque
    ahora = f"{oferta['precio_ahora']:.2f} €"
    antes = f"{referencia(oferta)} {oferta['precio_antes']:.2f} €"
    hueco = 26

    # Buscamos el cuerpo mas grande con el que el bloque entero quepa.
    tam = 76
    while tam > 44:
        f_ahora = _fuente(tam)
        f_antes = _fuente(int(tam * 0.53), negrita=False)
        total = _ancho(dib, ahora, f_ahora) + hueco + (_ancho(dib, antes, f_antes) if pct else 0)
        if total <= LADO - 90:
            break
        tam -= 4

    an_ahora = _ancho(dib, ahora, f_ahora)

    if pct:
        an_antes = _ancho(dib, antes, f_antes)
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
    _etiqueta_amazon(dib, 70, 84, 30)
    _centrar(dib, 230, (oferta.get("categoria") or "CHOLLO").upper(), _fuente(30), APAGADO)

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
    _centrar(dib, y, f"{oferta['precio_ahora']:.2f} €", _fuente(140), ACENTO)

    if pct:
        f_ant = _fuente(46, negrita=False)
        antes = f"{referencia(oferta)} {oferta['precio_antes']:.2f} €"
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

    _etiqueta_amazon(dib, 120, 240, 40)
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

    # En vertical hay sitio de sobra, asi que el PVP va debajo y centrado:
    # al lado se salia de la imagen.
    f_ahora = _fuente(110)
    ahora = f"{oferta['precio_ahora']:.2f} €"
    _centrar(dib, 1315, ahora, f_ahora, ACENTO)
    if pct:
        f_antes = _fuente(50, negrita=False)
        antes = f"{referencia(oferta)} {oferta['precio_antes']:.2f} €"
        an = _ancho(dib, antes, f_antes)
        x = (LADO - an) / 2
        dib.text((x, 1462), antes, font=f_antes, fill=APAGADO)
        dib.line([(x, 1494), (x + an, 1494)], fill=APAGADO, width=4)

    dib.line([(LADO / 2 - 70, 1660), (LADO / 2, 1590), (LADO / 2 + 70, 1660)],
             fill=ACENTO, width=22, joint="curve")
    _centrar(dib, 1700, "ENLACE EN LA BIO", _fuente(44), ACENTO)
    _centrar(dib, 1768, "CHOLLO DE AMAZON.ES  ·  NO VENDEMOS NADA", _fuente(32), AMAZON)
    _centrar(dib, 1822, "PUBLICIDAD  ·  ENLACE DE AFILIADO  ·  GANAMOS UNA COMISION",
             _fuente(26, negrita=False), APAGADO)

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
    """Publica una entrada normal en el muro de la Pagina, con la foto
    adjunta. El enlace de afiliado va en el texto, que aqui si es clicable.

    Se sube la foto sin publicar y luego se adjunta a una entrada de /feed,
    en vez de publicar directamente en /photos. Las dos formas salen en el
    muro, pero /photos crea un objeto de tipo foto (la Pagina lo ensena como
    una foto del album) y /feed crea una publicacion de verdad, igual que si
    la escribieras tu desde el cuadro de publicar.
    """
    ids = []
    for url in urls_imagen[:10]:
        subida = requests.post(
            f"{_base()}/{FB_PAGE_ID}/photos",
            data={
                "url": url,
                "published": "false",   # sin publicar: solo queda subida
                "temporary": "true",    # y se descarta si no la adjuntamos
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



def _esperar_contenedor(contenedor: str, intentos: int = 12, espera: int = 5) -> None:
    """Instagram procesa cada imagen en segundo plano; hay que esperar.
    Con video tarda bastante mas, de ahi los parametros."""
    for _ in range(intentos):
        time.sleep(espera)
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


# ----------------------------------------------------------------------
# Reels
#
# El video se sube directamente a Meta ("subida resumible") en vez de
# alojarlo en el repositorio: diez videos al dia inflarian el repo en
# cientos de megas al mes, y asi no hay que esperar a que GitHub lo sirva.
# ----------------------------------------------------------------------

RUPLOAD = "https://rupload.facebook.com"
DURACION_REEL = 9


def generar_reel(ruta_imagen: str, destino: str) -> str:
    """Video vertical de 9 segundos con un zoom lento sobre la historia.

    Cumple lo que pide Instagram para un Reel: MP4, H.264, AAC, 9:16 y
    1080x1920. El zoom es de solo un 5% para que no se corte nada, ni el
    precio ni el aviso de publicidad de abajo. Lleva una pista de audio
    en silencio porque algunos reproductores rechazan video sin audio.
    """
    fotogramas = DURACION_REEL * 30
    filtro = (
        "[0:v]scale=2160:3840,"
        f"zoompan=z='1+0.05*on/{fotogramas - 1}':"
        "x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':"
        f"d={fotogramas}:s=1080x1920:fps=30,format=yuv420p[v]"
    )
    orden = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-loop", "1", "-framerate", "30", "-i", ruta_imagen,
        "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100",
        "-filter_complex", filtro,
        "-map", "[v]", "-map", "1:a", "-t", str(DURACION_REEL),
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "22",
        "-profile:v", "high", "-level", "4.1", "-r", "30",
        "-c:a", "aac", "-b:a", "128k", "-ar", "44100",
        "-shortest", "-movflags", "+faststart", destino,
    ]
    os.makedirs(os.path.dirname(destino) or ".", exist_ok=True)
    r = subprocess.run(orden, capture_output=True, text=True, timeout=180)
    if r.returncode != 0 or not os.path.exists(destino):
        raise RuntimeError(f"ffmpeg fallo: {r.stderr[-300:]}")
    return destino


def _subir_resumible(url: str, ruta_video: str) -> None:
    """Sube el video entero de una vez al servidor de subidas de Meta."""
    tam = os.path.getsize(ruta_video)
    with open(ruta_video, "rb") as f:
        resp = requests.post(
            url,
            headers={
                "Authorization": f"OAuth {META_TOKEN}",
                "offset": "0",
                "file_size": str(tam),
            },
            data=f,
            timeout=300,
        )
    datos = _comprobar(resp)
    if datos.get("success") is False:
        raise RuntimeError(f"La subida del video no se completo: {datos}")


def publicar_reel_instagram(ruta_video: str, texto: str) -> str:
    """Reel de Instagram: contenedor, subida del video, espera a que Meta
    lo procese (con video tarda minutos, no segundos) y publicacion."""
    creacion = requests.post(
        f"{_base()}/{IG_USER_ID}/media",
        data={
            "media_type": "REELS",
            "upload_type": "resumable",
            "caption": texto,
            "share_to_feed": "true",
            "access_token": META_TOKEN,
        },
        timeout=TIEMPO_ESPERA,
    )
    datos = _comprobar(creacion)
    contenedor = datos["id"]
    uri = datos.get("uri") or f"{RUPLOAD}/ig-api-upload/{META_VERSION}/{contenedor}"
    _subir_resumible(uri, ruta_video)
    _esperar_contenedor(contenedor, intentos=40, espera=10)
    publicacion = requests.post(
        f"{_base()}/{IG_USER_ID}/media_publish",
        data={"creation_id": contenedor, "access_token": META_TOKEN},
        timeout=TIEMPO_ESPERA,
    )
    return _comprobar(publicacion)["id"]


def publicar_reel_facebook(ruta_video: str, texto: str) -> str:
    """Reel en la Pagina de Facebook, en tres fases: empezar, subir, cerrar.
    Los Reels si llegan a gente que no sigue la pagina; las publicaciones
    con enlace externo apenas salen del circulo de seguidores."""
    inicio = requests.post(
        f"{_base()}/{FB_PAGE_ID}/video_reels",
        data={"upload_phase": "start", "access_token": META_TOKEN},
        timeout=TIEMPO_ESPERA,
    )
    datos = _comprobar(inicio)
    video_id = datos["video_id"]
    url = datos.get("upload_url") or f"{RUPLOAD}/video-upload/{META_VERSION}/{video_id}"
    _subir_resumible(url, ruta_video)
    fin = requests.post(
        f"{_base()}/{FB_PAGE_ID}/video_reels",
        data={
            "upload_phase": "finish",
            "video_id": video_id,
            "video_state": "PUBLISHED",
            "description": texto,
            "access_token": META_TOKEN,
        },
        timeout=TIEMPO_ESPERA,
    )
    datos = _comprobar(fin)
    if datos.get("success") is False:
        raise RuntimeError(f"Facebook no acepto el Reel: {datos}")
    return str(video_id)


# ----------------------------------------------------------------------
# Telegram
# ----------------------------------------------------------------------

def telegram_activo() -> bool:
    return bool(TELEGRAM_TOKEN and TELEGRAM_CANAL)


def publicar_telegram(ruta_foto: str, oferta: dict) -> str:
    """Foto al canal con el texto y un boton que lleva a Amazon.

    La foto se sube desde el disco, asi que no hay que esperar a que
    GitHub la sirva. Los errores se limpian antes de imprimirlos: la URL
    de la API lleva el token dentro y no queremos verlo en un log.
    """
    teclado = {"inline_keyboard": [[{
        "text": "Ver oferta en Amazon",
        "url": enlace_afiliado(oferta["asin"]),
    }]]}
    if usuario_telegram():
        teclado["inline_keyboard"].append([{
            "text": "Compartir el canal con alguien",
            "url": enlace_compartir_canal(),
        }])
    try:
        with open(ruta_foto, "rb") as f:
            resp = requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto",
                data={
                    "chat_id": TELEGRAM_CANAL,
                    "caption": texto_telegram(oferta),
                    "parse_mode": "HTML",
                    "reply_markup": json.dumps(teclado),
                },
                files={"photo": f},
                timeout=TIEMPO_ESPERA,
            )
        datos = resp.json()
    except Exception as e:
        raise RuntimeError(str(e).replace(TELEGRAM_TOKEN, "***")) from None
    if not datos.get("ok"):
        raise RuntimeError(f"Telegram: {datos.get('description', datos)}")
    return str(datos["result"]["message_id"])


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
<meta name="description" content="Los ultimos chollos de Amazon, nuevos varias veces al dia.">
<link rel="canonical" href="{url_pagina}">
<meta property="og:type" content="website">
<meta property="og:title" content="{marca}: chollos de Amazon cada dia">
<meta property="og:description" content="Ofertas de Amazon con el precio de referencia real y el descuento calculado. Nuevas varias veces al dia.">
<meta property="og:url" content="{url_pagina}">
<meta property="og:image" content="{og_imagen}">
<meta name="twitter:card" content="summary_large_image">
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
  a.telegram {{
    display: block; text-align: center; text-decoration: none;
    background: #229ed9; color: #fff; font-weight: 700; font-size: 15px;
    border-radius: 999px; padding: 12px 16px; margin: 0 0 22px;
  }}
  a.telegram span {{ display: block; font-weight: 400; font-size: 12.5px; opacity: .9; }}
  button.compartir {{
    display: block; width: 100%; margin: 18px 0 0; padding: 12px 16px;
    background: transparent; color: var(--texto); border: 1px solid var(--borde);
    border-radius: 999px; font: inherit; font-size: 14px; font-weight: 600; cursor: pointer;
  }}
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
    <p class="lema">Chollos de Amazon, nuevos varias veces al dia</p>
  </header>
  <p class="aviso">Como afiliado de Amazon, gano una comision por las compras
  que cumplan los requisitos. No te cuesta nada de mas.</p>
{telegram}  {tarjetas}
  <button class="compartir" type="button" id="compartir">Pasale esta pagina a alguien</button>
  <footer>Ultima actualizacion: {actualizado} (hora peninsular)</footer>
</div>
<script>
  (function () {{
    var b = document.getElementById("compartir");
    if (!b) return;
    b.addEventListener("click", function () {{
      var url = location.href.split("#")[0].split("?")[0];
      var datos = {{ title: document.title, text: "Chollos de Amazon con el descuento de verdad:", url: url }};
      if (navigator.share) {{ navigator.share(datos).catch(function () {{}}); return; }}
      window.open("https://wa.me/?text=" + encodeURIComponent(datos.text + " " + url), "_blank", "noopener");
    }});
  }})();
</script>
</body>
</html>
"""

TARJETA = """  <a class="oferta" href="{enlace}" target="_blank" rel="nofollow sponsored noopener">
    <div class="fila">
      <img class="miniatura" src="{imagen}" alt="" loading="lazy">
      <div>
        <p class="titulo">{titulo}</p>
        <div class="precios">
          <span class="ahora">{ahora} €</span>
          {bloque_antes}
        </div>
      </div>
    </div>
    <div class="cuando">Oferta en Amazon.es · Publicado el {cuando} (hora peninsular)</div>
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
                f'<span class="antes">{referencia(e)} {e["precio_antes"]:.2f} €</span>'
                f'<span class="pct">-{pct}%</span>'
            )
        tarjetas.append(
            TARJETA.format(
                enlace=html.escape(enlace_afiliado(e["asin"]), quote=True),
                imagen=html.escape(e.get("imagen", ""), quote=True),
                titulo=html.escape(e["titulo"]),
                ahora=f'{e["precio_ahora"]:.2f}',
                bloque_antes=bloque_antes,
                cuando=hora_madrid(e.get("fecha", "")),
            )
        )

    if not tarjetas:
        tarjetas = ['  <p class="vacio">Todavia no hay ofertas publicadas.</p>']

    boton_telegram = cta_telegram_html()
    raiz = PAGES_URL.rstrip("/") if PAGES_URL else ""
    og_imagen = f'{raiz}/{entradas[0].get("imagen", "")}' if (raiz and entradas) else ""

    pagina = PLANTILLA.format(
        marca=html.escape(MARCA),
        telegram=boton_telegram,
        url_pagina=html.escape(f"{raiz}/" if raiz else "", quote=True),
        og_imagen=html.escape(og_imagen, quote=True),
        tarjetas="\n".join(tarjetas),
        actualizado=datetime.now(ZoneInfo("Europe/Madrid")).strftime("%d/%m/%Y a las %H:%M"),
    )
    os.makedirs(os.path.dirname(destino), exist_ok=True)
    with open(destino, "w", encoding="utf-8") as f:
        f.write(pagina)
    return destino


PLANTILLA_TIKTOK = """<!doctype html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{marca} · para TikTok</title>
<meta name="robots" content="noindex">
<style>
  :root {{
    --fondo: #0f1218; --tarjeta: #171b24; --borde: #262c38;
    --texto: #f2f4f8; --apagado: #99a1b3; --acento: #ffd63d;
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0; padding: 26px 16px 70px;
    background: var(--fondo); color: var(--texto);
    font: 16px/1.55 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  }}
  .envoltorio {{ max-width: 560px; margin: 0 auto; }}
  h1 {{ font-size: 22px; margin: 0 0 4px; }}
  .lema {{ color: var(--apagado); font-size: 14px; margin: 0 0 20px; }}
  .como {{
    background: rgba(255,214,61,.08); border: 1px solid rgba(255,214,61,.22);
    border-radius: 12px; padding: 12px 14px; margin-bottom: 22px;
    font-size: 13.5px; color: var(--apagado); line-height: 1.5;
  }}
  .como b {{ color: var(--texto); }}
  article {{
    background: var(--tarjeta); border: 1px solid var(--borde);
    border-radius: 16px; padding: 14px; margin-bottom: 14px;
    display: flex; gap: 14px; align-items: flex-start;
  }}
  article img {{
    width: 92px; border-radius: 8px; display: block; background: var(--fondo);
  }}
  .cuerpo {{ flex: 1; min-width: 0; }}
  .titulo {{ font-weight: 600; font-size: 15px; margin: 0 0 2px; }}
  .precio {{ color: var(--acento); font-weight: 700; font-size: 15px; margin: 0 0 10px; }}
  pre {{
    background: var(--fondo); border: 1px solid var(--borde); border-radius: 8px;
    padding: 10px; margin: 0 0 10px; font-size: 12px; line-height: 1.45;
    white-space: pre-wrap; word-break: break-word; color: var(--apagado);
    max-height: 132px; overflow: auto;
  }}
  button {{
    background: var(--acento); color: #0f1218; border: 0; border-radius: 999px;
    padding: 9px 16px; font-size: 13.5px; font-weight: 700; cursor: pointer;
    font-family: inherit;
  }}
  button:active {{ transform: translateY(1px); }}
  .vacio {{ color: var(--apagado); text-align: center; padding: 40px 0; }}
  footer {{ color: var(--apagado); font-size: 12px; text-align: center; margin-top: 26px; }}
  @media (prefers-color-scheme: light) {{
    :root {{
      --fondo: #f6f7fa; --tarjeta: #ffffff; --borde: #e2e6ee;
      --texto: #12161f; --apagado: #626b7d; --acento: #b07d00;
    }}
    button {{ color: #fff; }}
  }}
</style>
</head>
<body>
<div class="envoltorio">
  <h1>{marca} · para TikTok</h1>
  <p class="lema">Los verticales de las ultimas ofertas, listos para subir.</p>
  <p class="como">Desde el movil: <b>pulsa la imagen</b> para abrirla a tamano completo,
  mantenla pulsada y guardala en Fotos. Luego <b>Copiar texto</b>, abres TikTok, subes la
  foto, pegas el texto y le pones un sonido de tendencia. El sonido es lo que hace que
  llegue a gente, asi que no lo saltes.</p>
  {tarjetas}
  <footer>Actualizado el {actualizado}</footer>
</div>
<script>
document.addEventListener("click", function (ev) {{
  const b = ev.target.closest("button[data-destino]");
  if (!b) return;
  const t = document.getElementById(b.dataset.destino).textContent;
  navigator.clipboard.writeText(t).then(function () {{
    const antes = b.textContent;
    b.textContent = "Copiado";
    setTimeout(function () {{ b.textContent = antes; }}, 1600);
  }});
}});
</script>
</body>
</html>
"""

TARJETA_TIKTOK = """  <article>
    <a href="{imagen}" target="_blank" rel="noopener"><img src="{imagen}" alt="" loading="lazy"></a>
    <div class="cuerpo">
      <p class="titulo">{titulo}</p>
      <p class="precio">{precio} €</p>
      <pre id="texto{i}">{texto}</pre>
      <button data-destino="texto{i}">Copiar texto</button>
    </div>
  </article>
"""


def regenerar_tiktok(ruta_estado: str = "estado.json",
                     destino: str = "docs/tiktok.html") -> str:
    """Pagina privada para el movil con los verticales y sus textos.

    No es para tu publico: es tu bandeja de subida. TikTok no permite
    publicar en publico por API sin pasar su auditoria, asi que esto
    reduce el trabajo a guardar la foto y pegar el texto.
    """
    estado = _leer_estado(ruta_estado)
    entradas = [e for e in reversed(estado.get("publicadas", [])) if e.get("historia")][:10]

    tarjetas = []
    for i, e in enumerate(entradas):
        tarjetas.append(
            TARJETA_TIKTOK.format(
                i=i,
                imagen=html.escape(e["historia"], quote=True),
                titulo=html.escape(e["titulo"]),
                precio=f'{e["precio_ahora"]:.2f}',
                texto=html.escape(e.get("texto_tiktok", "")),
            )
        )

    if not tarjetas:
        tarjetas = ['  <p class="vacio">Todavia no hay verticales.</p>']

    pagina = PLANTILLA_TIKTOK.format(
        marca=html.escape(MARCA),
        tarjetas="\n".join(tarjetas),
        actualizado=datetime.now(ZoneInfo("Europe/Madrid")).strftime("%d/%m/%Y a las %H:%M"),
    )
    os.makedirs(os.path.dirname(destino), exist_ok=True)
    with open(destino, "w", encoding="utf-8") as f:
        f.write(pagina)
    return destino

# ======================================================================
# SITIO SEO: una ficha por oferta, archivo y sitemap
#
# Es el unico canal que acumula: una ficha de hace tres meses te sigue
# trayendo gente de Google, mientras que un post se evapora en un dia.
# Y en tu propia web los enlaces de afiliado no los discute nadie,
# siempre que la divulgacion este a la vista.
# ======================================================================

CABECERA_SEO = """<!doctype html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{titulo_seo}</title>
<meta name="description" content="{descripcion}">
<link rel="canonical" href="{canonica}">
<meta property="og:type" content="{og_tipo}">
<meta property="og:title" content="{titulo_seo}">
<meta property="og:description" content="{descripcion}">
<meta property="og:url" content="{canonica}">
<meta property="og:image" content="{og_imagen}">
<meta property="og:site_name" content="{marca}">
<meta name="twitter:card" content="summary_large_image">
<style>
  :root {{
    --fondo: #0f1218; --tarjeta: #171b24; --borde: #262c38;
    --texto: #f2f4f8; --apagado: #99a1b3; --acento: #ffd63d;
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0; padding: 28px 18px 70px;
    background: var(--fondo); color: var(--texto);
    font: 16.5px/1.6 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  }}
  .envoltorio {{ max-width: 640px; margin: 0 auto; }}
  a {{ color: var(--acento); }}
  .migas {{ font-size: 13px; color: var(--apagado); margin-bottom: 18px; }}
  .migas a {{ color: var(--apagado); }}
  h1 {{ font-size: 27px; line-height: 1.25; margin: 0 0 14px; }}
  h2 {{ font-size: 19px; margin: 34px 0 12px; }}
  .aviso {{
    background: rgba(255,214,61,.08); border: 1px solid rgba(255,214,61,.22);
    border-radius: 12px; padding: 12px 14px; color: var(--apagado);
    font-size: 13.5px; line-height: 1.5; margin: 26px 0;
  }}
  figure {{ margin: 0 0 22px; }}
  figure img {{ width: 100%; border-radius: 14px; display: block; }}
  .precios {{ display: flex; gap: 12px; align-items: baseline; flex-wrap: wrap; margin: 0 0 6px; }}
  .ahora {{ color: var(--acento); font-weight: 800; font-size: 34px; }}
  .antes {{ color: var(--apagado); text-decoration: line-through; font-size: 17px; }}
  .pct {{
    background: var(--acento); color: #0f1218; font-weight: 800;
    font-size: 13px; padding: 3px 9px; border-radius: 999px;
  }}
  .fecha {{ color: var(--apagado); font-size: 13.5px; margin: 0 0 22px; }}
  .boton {{
    display: block; text-align: center; text-decoration: none;
    background: var(--acento); color: #0f1218; font-weight: 800; font-size: 17px;
    padding: 16px; border-radius: 14px; margin: 0 0 12px;
  }}
  ul.lista {{ padding-left: 20px; }}
  ul.lista li {{ margin-bottom: 7px; }}
  article.item {{
    background: var(--tarjeta); border: 1px solid var(--borde); border-radius: 14px;
    padding: 13px; margin-bottom: 11px; display: flex; gap: 13px; align-items: center;
  }}
  article.item img {{ width: 74px; height: 74px; object-fit: cover; border-radius: 9px; }}
  article.item a {{ text-decoration: none; color: var(--texto); font-weight: 600; font-size: 15.5px; }}
  article.item .p {{ color: var(--acento); font-weight: 700; font-size: 15px; }}
  a.telegram {{
    display: block; text-align: center; text-decoration: none;
    background: #229ed9; color: #fff; font-weight: 700; font-size: 15px;
    border-radius: 14px; padding: 12px 16px; margin: 0 0 22px;
  }}
  a.telegram span {{ display: block; font-weight: 400; font-size: 12.5px; opacity: .9; }}
  footer {{ margin-top: 40px; color: var(--apagado); font-size: 13px; }}
  @media (prefers-color-scheme: light) {{
    :root {{
      --fondo: #f6f7fa; --tarjeta: #ffffff; --borde: #e2e6ee;
      --texto: #12161f; --apagado: #626b7d; --acento: #b07d00;
    }}
    .pct, .boton {{ color: #fff; }}
  }}
</style>
{extra}
</head>
<body>
<div class="envoltorio">
"""

PIE_SEO = """  <footer>
    <p><a href="{raiz}/">{marca}</a> · <a href="{raiz}/ofertas.html">Todas las ofertas</a></p>
    <p>Como afiliado de Amazon, gano una comision por las compras que cumplan los
    requisitos. El precio que ves es el que tenia el producto cuando lo publicamos:
    Amazon puede cambiarlo en cualquier momento, asi que comprueba siempre el precio
    final en su pagina antes de comprar.</p>
  </footer>
</div>
</body>
</html>
"""


def _slug(texto: str) -> str:
    """Convierte un titulo en algo apto para una URL."""
    import unicodedata
    t = unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode()
    t = "".join(c.lower() if c.isalnum() else "-" for c in t)
    while "--" in t:
        t = t.replace("--", "-")
    return t.strip("-")[:60] or "oferta"


def _raiz() -> str:
    return PAGES_URL.rstrip("/") if PAGES_URL else ""


def ruta_ficha(e: dict) -> str:
    return f'oferta/{e["asin"]}-{_slug(e["titulo"])}.html'


# Categorias de la web. El CSV tiene nombres sueltos ("Informatica",
# "Cuidado personal"...); aqui se agrupan en ocho paginas.
CATEGORIAS_WEB = {
    "tecnologia": ("Tecnología", "Móviles, auriculares, cargadores, informática y demás "
                   "cacharros con el precio rebajado."),
    "hogar": ("Hogar", "Aspiradoras, textiles, muebles, bricolaje y cosas de casa que han "
              "bajado de precio."),
    "cocina": ("Cocina", "Sartenes, freidoras de aire, menaje y pequeño electrodoméstico "
               "de cocina en oferta."),
    "deporte": ("Deporte", "Material para entrenar en casa o fuera con descuento."),
    "moda": ("Moda y viaje", "Ropa, maletas y complementos rebajados."),
    "belleza": ("Belleza y cuidado personal", "Afeitado, depilación, higiene bucal y "
                "cuidado personal con el precio rebajado."),
    "bebe": ("Bebé", "Pañales, higiene y cosas para los peques en oferta."),
    "mascotas": ("Mascotas", "Comida, arena y accesorios para perros y gatos con descuento."),
}
_ALIAS_CATEGORIA = {
    "tecnologia": "tecnologia", "informatica": "tecnologia", "informatcia": "tecnologia",
    "moviles": "tecnologia", "audio": "tecnologia", "hi-fi": "tecnologia", "tv": "tecnologia",
    "fotografia": "tecnologia", "videocamara": "tecnologia", "kindle": "tecnologia",
    "juegos": "tecnologia", "videojuegos": "tecnologia",
    "hogar": "hogar", "muebles": "hogar", "bricolaje": "hogar", "bricolage": "hogar",
    "grandes electrodomesticos": "hogar", "papeleria": "hogar", "coche": "hogar",
    "cocina": "cocina", "alimentacion": "cocina",
    "deporte": "deporte", "deportes": "deporte",
    "moda": "moda", "ropa": "moda", "equipaje": "moda",
    "belleza": "belleza", "cuidado personal": "belleza", "salud": "belleza",
    "bebe": "bebe", "juguetes": "bebe",
    "mascotas": "mascotas",
}
MIN_POR_CATEGORIA = 3   # menos que esto seria una pagina vacia para Google


def categoria_web(nombre: str) -> str:
    """Slug de la categoria de la web para un nombre del CSV, o '' si no encaja."""
    import unicodedata
    t = unicodedata.normalize("NFKD", nombre or "").encode("ascii", "ignore").decode()
    return _ALIAS_CATEGORIA.get(t.strip().lower(), "")


def _categorias_por_asin(entradas: list, ruta_csv: str = "ofertas.csv") -> dict:
    """ASIN -> slug. Usa la categoria guardada en el estado y, para las
    entradas antiguas que no la tienen, la del ofertas.csv."""
    del_csv = {}
    if os.path.exists(ruta_csv):
        with open(ruta_csv, encoding="utf-8-sig", newline="") as f:
            for fila in csv.DictReader(f):
                del_csv[(fila.get("asin") or "").strip()] = fila.get("categoria") or ""
    return {e["asin"]: categoria_web(e.get("categoria") or del_csv.get(e["asin"], ""))
            for e in entradas}


def _items_html(entradas: list) -> str:
    raiz = _raiz()
    filas = []
    for e in entradas:
        pct = descuento(e["precio_ahora"], e.get("precio_antes") or 0)
        filas.append(f"""  <article class="item">
    <img src="{html.escape(raiz + "/" + e.get("imagen", ""), quote=True)}" alt="" loading="lazy">
    <div>
      <a href="{html.escape(raiz + "/" + ruta_ficha(e), quote=True)}">{html.escape(e["titulo"])}</a>
      <div class="p">{e["precio_ahora"]:.2f} €{f" · -{pct}%" if pct else ""}</div>
    </div>
  </article>""")
    return "\n".join(filas)


def _pagina_categoria(slug: str, entradas: list) -> str:
    raiz = _raiz()
    nombre, intro = CATEGORIAS_WEB[slug]
    canonica = f"{raiz}/categoria/{slug}.html"
    cuerpo = (f'  <p class="migas"><a href="{raiz}/">{html.escape(MARCA)}</a> · '
              f'<a href="{raiz}/ofertas.html">Ofertas</a></p>\n'
              f'  <h1>Chollos de {html.escape(nombre.lower())} en Amazon</h1>\n'
              f'  <p class="fecha">{html.escape(intro)} Cada oferta indica sobre qué precio '
              f'se calcula el descuento y el día en que se comprobó.</p>\n'
              + cta_telegram_html() + _items_html(entradas) + "\n")
    return (CABECERA_SEO.format(
                titulo_seo=html.escape(f"Chollos de {nombre.lower()} en Amazon | {MARCA}", quote=True),
                descripcion=html.escape(f"{intro} {len(entradas)} ofertas publicadas en {MARCA}.",
                                        quote=True),
                canonica=html.escape(canonica, quote=True),
                og_imagen=html.escape(f'{raiz}/{entradas[0].get("imagen", "")}', quote=True),
                og_tipo="website", marca=html.escape(MARCA), extra="")
            + cuerpo + PIE_SEO.format(raiz=raiz, marca=html.escape(MARCA)))


def _ficha(e: dict, cat: str = "") -> str:
    """Una ficha por oferta, con datos estructurados para Google."""
    pct = descuento(e["precio_ahora"], e.get("precio_antes") or 0)
    raiz = _raiz()
    canonica = f'{raiz}/{ruta_ficha(e)}'
    imagen = f'{raiz}/{e.get("imagen", "")}'
    fecha = hora_madrid(e.get("fecha", ""), "%d/%m/%Y")

    titulo_seo = f'{e["titulo"]} por {e["precio_ahora"]:.2f} €'
    if pct:
        titulo_seo += f' (-{pct}%)'
    titulo_seo += f' | {MARCA}'

    descripcion = (
        f'{e["titulo"]} a {e["precio_ahora"]:.2f} €'
        + (f', frente a {e["precio_antes"]:.2f} € (-{pct}% sobre {referencia(e)}).' if pct else '.')
        + f' Precio comprobado el {fecha}.'
    )

    datos = {
        "@context": "https://schema.org",
        "@type": "Product",
        "name": e["titulo"],
        "image": imagen,
        "offers": {
            "@type": "Offer",
            "price": f'{e["precio_ahora"]:.2f}',
            "priceCurrency": "EUR",
            "availability": "https://schema.org/InStock",
            "url": enlace_afiliado(e["asin"]),
        },
    }
    extra = ('<script type="application/ld+json">'
             + json.dumps(datos, ensure_ascii=False) + '</script>')

    bloque_antes = ""
    if pct:
        bloque_antes = (f'<span class="antes">{referencia(e)} {e["precio_antes"]:.2f} €</span>'
                        f'<span class="pct">-{pct}%</span>')

    miga_cat = ""
    if cat in CATEGORIAS_WEB:
        miga_cat = f' · <a href="{raiz}/categoria/{cat}.html">{html.escape(CATEGORIAS_WEB[cat][0])}</a>'
    cuerpo = f"""  <p class="migas"><a href="{raiz}/">{html.escape(MARCA)}</a> ·
  <a href="{raiz}/ofertas.html">Ofertas</a>{miga_cat}</p>
  <h1>{html.escape(e["titulo"])}</h1>
  <figure><img src="{html.escape(imagen, quote=True)}" alt="{html.escape(e["titulo"], quote=True)}"></figure>
  <div class="precios"><span class="ahora">{e["precio_ahora"]:.2f} €</span>{bloque_antes}</div>
  <p class="fecha">Precio comprobado el {fecha}.</p>
  <a class="boton" href="{html.escape(enlace_afiliado(e["asin"]), quote=True)}"
     target="_blank" rel="nofollow sponsored noopener">Ver el precio en Amazon</a>
  <div class="aviso">Enlace de afiliado. Si compras a traves de el, gano una pequena
  comision y a ti no te cuesta nada de mas.</div>
{cta_telegram_html()}  <h2>Antes de comprar</h2>
  <ul class="lista">
    <li>El precio tachado es la referencia que muestra la ficha de Amazon
    ({html.escape(referencia(e))}), no necesariamente lo que costaba la semana pasada.</li>
    <li>Amazon cambia precios a lo largo del dia. Comprueba el importe final antes de pagar.</li>
    <li>Publicamos el precio del dia {fecha}; si has llegado aqui mucho despues, es
    probable que haya cambiado.</li>
  </ul>
"""
    return (CABECERA_SEO.format(titulo_seo=html.escape(titulo_seo, quote=True),
                                descripcion=html.escape(descripcion, quote=True),
                                canonica=html.escape(canonica, quote=True),
                                og_imagen=html.escape(imagen, quote=True),
                                og_tipo="article", marca=html.escape(MARCA), extra=extra)
            + cuerpo + PIE_SEO.format(raiz=raiz, marca=html.escape(MARCA)))


def _archivo(entradas: list, cats: dict = None) -> str:
    raiz = _raiz()
    cats = cats or {}
    indice = ""
    if cats:
        enlaces = " · ".join(
            f'<a href="{raiz}/categoria/{slug}.html">{html.escape(CATEGORIAS_WEB[slug][0])}</a> ({n})'
            for slug, n in cats.items())
        indice = f'  <p class="migas">Por categoría: {enlaces}</p>\n'
    filas = [_items_html(entradas)]
    cuerpo = (f'  <h1>Todas las ofertas de {html.escape(MARCA)}</h1>\n'
              f'  <p class="fecha">Chollos de Amazon publicados hasta hoy, con el precio '
              f'que tenian el dia de la publicacion.</p>\n' + indice + cta_telegram_html()
              + "\n".join(filas) + "\n")
    return (CABECERA_SEO.format(
                titulo_seo=html.escape(f"Todas las ofertas | {MARCA}", quote=True),
                descripcion=html.escape(
                    f"Listado de todos los chollos de Amazon publicados en {MARCA}.", quote=True),
                canonica=html.escape(f"{raiz}/ofertas.html", quote=True),
                og_imagen="", og_tipo="website", marca=html.escape(MARCA), extra="")
            + cuerpo + PIE_SEO.format(raiz=raiz, marca=html.escape(MARCA)))


def regenerar_seo(ruta_estado: str = "estado.json", dir_docs: str = "docs") -> int:
    """Reescribe todas las fichas, el archivo, el sitemap y el robots.

    Se regenera todo cada vez a proposito: asi un cambio de plantilla
    alcanza tambien a las fichas viejas.
    """
    raiz = _raiz()
    if not raiz:
        print("AVISO sin PAGES_URL no se puede generar el sitio SEO.")
        return 0

    estado = _leer_estado(ruta_estado)
    entradas = list(reversed(estado.get("publicadas", [])))
    if not entradas:
        return 0

    por_asin = _categorias_por_asin(entradas)
    grupos = {}
    for e in entradas:
        if por_asin.get(e["asin"]):
            grupos.setdefault(por_asin[e["asin"]], []).append(e)
    grupos = {slug: grupos[slug] for slug in CATEGORIAS_WEB
              if len(grupos.get(slug, [])) >= MIN_POR_CATEGORIA}

    os.makedirs(os.path.join(dir_docs, "oferta"), exist_ok=True)
    for e in entradas:
        destino = os.path.join(dir_docs, ruta_ficha(e))
        cat = por_asin.get(e["asin"], "")
        with open(destino, "w", encoding="utf-8") as f:
            f.write(_ficha(e, cat if cat in grupos else ""))

    os.makedirs(os.path.join(dir_docs, "categoria"), exist_ok=True)
    for slug, lista in grupos.items():
        with open(os.path.join(dir_docs, "categoria", f"{slug}.html"), "w", encoding="utf-8") as f:
            f.write(_pagina_categoria(slug, lista))

    with open(os.path.join(dir_docs, "ofertas.html"), "w", encoding="utf-8") as f:
        f.write(_archivo(entradas, {slug: len(lista) for slug, lista in grupos.items()}))

    urls = ([f"{raiz}/", f"{raiz}/ofertas.html"]
            + [f"{raiz}/categoria/{slug}.html" for slug in grupos]
            + [f"{raiz}/{ruta_ficha(e)}" for e in entradas])
    fechas = (["", ""] + [lista[0].get("fecha", "")[:10] for lista in grupos.values()]
              + [e.get("fecha", "")[:10] for e in entradas])
    cuerpo = "\n".join(
        f"  <url><loc>{html.escape(u, quote=True)}</loc>"
        + (f"<lastmod>{d}</lastmod>" if d else "") + "</url>"
        for u, d in zip(urls, fechas))
    with open(os.path.join(dir_docs, "sitemap.xml"), "w", encoding="utf-8") as f:
        f.write('<?xml version="1.0" encoding="UTF-8"?>\n'
                '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
                + cuerpo + "\n</urlset>\n")

    with open(os.path.join(dir_docs, "robots.txt"), "w", encoding="utf-8") as f:
        f.write(f"User-agent: *\nAllow: /\nDisallow: /tiktok.html\n\nSitemap: {raiz}/sitemap.xml\n")

    print(f"Sitio SEO regenerado: {len(entradas)} fichas, {len(grupos)} categorias, "
          f"archivo y sitemap.")
    return len(entradas)

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
    return float(str(valor).replace(",", ".").replace("€", "").strip() or 0)


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
                "referencia": (fila.get("referencia") or "").strip(),
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
    if SOLO_TELEGRAM and not telegram_activo():
        # Franja reservada a Telegram pero el canal aun no esta montado:
        # no gastamos una oferta que no va a salir en ningun sitio.
        print("Franja solo para Telegram y Telegram no esta configurado. No hago nada.")
        if os.path.exists(RUTA_PENDIENTE):
            os.remove(RUTA_PENDIENTE)
        return 0

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

    canales = []

    # El Reel se genera siempre que haya historia: tambien sirve para
    # TikTok, que va en el paquete descargable.
    ruta_reel = None
    if REELS and nombre_historia:
        try:
            ruta_reel = generar_reel(os.path.join(DIR_IMG, nombre_historia),
                                     os.path.join(DIR_TIKTOK, "reel.mp4"))
            print(f"Reel generado: {os.path.getsize(ruta_reel) // 1024} KB")
        except Exception as e:
            print(f"AVISO no se pudo generar el Reel, tiro de foto: {e}")
            ruta_reel = None

    if EN_SECO:
        print("--- MODO EN SECO, no se llama a ninguna API ---")
        print("SOLO TELEGRAM" if SOLO_TELEGRAM else "TODAS LAS REDES")
        print("FACEBOOK:\n" + texto_facebook(oferta))
        print("\nINSTAGRAM:\n" + texto_instagram(oferta))
        print("\nTELEGRAM:\n" + texto_telegram(oferta))
    else:
        # Telegram primero: no depende de que GitHub sirva la imagen.
        if telegram_activo():
            try:
                mid = publicar_telegram(os.path.join(DIR_IMG, nombres[0]), oferta)
                print(f"Telegram publicado: {mid}")
                canales.append("telegram")
            except Exception as e:
                print(f"AVISO Telegram fallo: {e}")
        elif SOLO_TELEGRAM:
            print("AVISO franja de Telegram sin Telegram configurado.")

        if not SOLO_TELEGRAM:
            urls = [f"{_base_imagenes()}/{n}" for n in nombres]
            for url in urls:
                _esperar_url(url)

            # Facebook: Reel si se puede; si no, la foto con enlace.
            hecho = False
            if ruta_reel:
                try:
                    print(f"Facebook Reel publicado: {publicar_reel_facebook(ruta_reel, texto_facebook(oferta))}")
                    canales.append("facebook-reel")
                    hecho = True
                except Exception as e:
                    print(f"AVISO el Reel de Facebook fallo, publico la foto: {e}")
            if not hecho:
                try:
                    print(f"Facebook publicado: {publicar_facebook(urls, texto_facebook(oferta))}")
                    canales.append("facebook")
                except Exception as e:  # si falla uno, los demas siguen
                    print(f"AVISO Facebook fallo: {e}")

            # Instagram: igual.
            hecho = False
            if ruta_reel:
                try:
                    print(f"Instagram Reel publicado: {publicar_reel_instagram(ruta_reel, texto_instagram(oferta))}")
                    canales.append("instagram-reel")
                    hecho = True
                except Exception as e:
                    print(f"AVISO el Reel de Instagram fallo, publico la foto: {e}")
            if not hecho:
                try:
                    print(f"Instagram publicado: {publicar_instagram(urls, texto_instagram(oferta))}")
                    canales.append("instagram")
                except Exception as e:
                    print(f"AVISO Instagram fallo: {e}")

            if nombre_historia:
                try:
                    url_h = f"{_base_imagenes()}/{nombre_historia}"
                    _esperar_url(url_h)
                    print(f"Historia publicada: {publicar_historia_instagram(url_h)}")
                    canales.append("historia")
                except Exception as e:
                    print(f"AVISO la historia fallo: {e}")

    estado = _leer_estado()
    estado.setdefault("publicadas", []).append({
        "asin": oferta["asin"],
        "titulo": oferta["titulo"],
        "precio_ahora": oferta["precio_ahora"],
        "precio_antes": oferta["precio_antes"],
        "imagen": f"img/{nombres[0]}",
        "historia": f"img/{nombre_historia}" if nombre_historia else "",
        "referencia": referencia(oferta),
        "categoria": oferta.get("categoria", ""),
        "texto_tiktok": texto_tiktok(oferta),
        "canales": canales,
        "fecha": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    })
    _guardar_estado(estado)
    regenerar(RUTA_ESTADO, "docs/index.html")
    regenerar_tiktok(RUTA_ESTADO, "docs/tiktok.html")
    regenerar_seo(RUTA_ESTADO, "docs")
    print("Paginas de bio y de TikTok regeneradas.")

    os.remove(RUTA_PENDIENTE)
    return 0


BIENVENIDA = """<b>Bienvenido a {marca}</b>

Aquí salen unas diez ofertas de Amazon al día, de 7 de la mañana a 9 de la noche (hora peninsular).

En cada una ves el precio de ahora, el precio de referencia que muestra Amazon (el PVP o el precio mediano) y el descuento calculado sobre ese número. Si el PVP tachado es de fantasía, la oferta no entra.

Consejo: silencia el canal si los avisos te molestan y échale un ojo cuando te venga bien. Y si conoces a alguien que compra mucho en Amazon, pásale el canal.

<i>{divulgacion}</i>"""

DESCRIPCION_CANAL = ("Chollos de Amazon cada día, con el descuento calculado sobre el precio "
                     "de referencia real. Contiene enlaces de afiliado.")


def _telegram(metodo: str, datos: dict) -> dict:
    try:
        resp = requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/{metodo}",
                             data=datos, timeout=TIEMPO_ESPERA)
        respuesta = resp.json()
    except Exception as e:
        raise RuntimeError(str(e).replace(TELEGRAM_TOKEN, "***")) from None
    if not respuesta.get("ok"):
        raise RuntimeError(f"Telegram {metodo}: {respuesta.get('description', respuesta)}")
    return respuesta.get("result", {})


def bienvenida_telegram() -> int:
    """Mensaje de bienvenida fijado arriba del canal. Se lanza a mano una
    sola vez desde Actions (workflow 'Telegram: mensaje fijado')."""
    if not telegram_activo():
        print("Telegram no esta configurado.")
        return 1
    botones = [[{"text": "Ver todos los chollos", "url": f"{PAGES_URL}/" if PAGES_URL
                 else "https://chollopage.github.io/chollos-bot/"}]]
    if usuario_telegram():
        botones.append([{"text": "Compartir el canal con alguien", "url": enlace_compartir_canal()}])
    msg = _telegram("sendMessage", {
        "chat_id": TELEGRAM_CANAL,
        "text": BIENVENIDA.format(marca=html.escape(MARCA), divulgacion=html.escape(DIVULGACION)),
        "parse_mode": "HTML",
        "disable_notification": "true",
        "disable_web_page_preview": "true",
        "reply_markup": json.dumps({"inline_keyboard": botones}),
    })
    print(f"Bienvenida enviada: {msg.get('message_id')}")
    try:
        _telegram("pinChatMessage", {"chat_id": TELEGRAM_CANAL,
                                     "message_id": msg["message_id"],
                                     "disable_notification": "true"})
        print("Bienvenida fijada arriba del canal.")
    except Exception as e:
        print(f"AVISO no se pudo fijar (dale al bot permiso para fijar mensajes): {e}")
    try:
        _telegram("setChatDescription", {"chat_id": TELEGRAM_CANAL,
                                         "description": DESCRIPCION_CANAL})
        print("Descripcion del canal actualizada.")
    except Exception as e:
        print(f"AVISO no se pudo cambiar la descripcion: {e}")
    return 0


if __name__ == "__main__":
    accion = sys.argv[1] if len(sys.argv) > 1 else "preparar"
    if accion == "bienvenida":
        sys.exit(bienvenida_telegram())
    sys.exit(preparar() if accion == "preparar" else publicar())
