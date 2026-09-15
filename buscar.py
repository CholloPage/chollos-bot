"""Buscador de ofertas con la Creators API de Amazon.

Rastrea unas cuantas categorias, se queda con lo que trae un descuento
de verdad y escribe los candidatos en candidatos.csv. No publica nada:
el workflow abre una pull request y decides tu.

Uso:  python buscar.py
      python buscar.py --volcar   (guarda una respuesta cruda en
                                   respuesta_ejemplo.json, para ver como
                                   viene de verdad la parte de precios)

Notas para tu yo del futuro:

  - Esto es la Creators API, no la PA-API 5.0. Amazon retiro la 5.0 el
    15 de mayo de 2026. Cambia la autenticacion (OAuth 2.0 en vez de
    firmar con AWS SigV4), el host, y los campos van en lowerCamelCase.
  - No hay canal de "ofertas del dia": hay que buscar por categoria y
    palabra clave y quedarse con lo que trae rebaja.
  - Amazon pide 10 ventas cualificadas en los ultimos 30 dias. Si no
    llegas, responde 403 AssociateNotEligible. Eso no es un fallo del
    bot: lo avisamos y salimos bien, para que la cola manual siga.
"""
import csv
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request


# ======================================================================
# CONFIGURACION
# ======================================================================

CREATORS_ID = os.environ.get("CREATORS_ID", "")
CREATORS_SECRET = os.environ.get("CREATORS_SECRET", "")
AMAZON_TAG = os.environ.get("AMAZON_TAG", "")

TOKEN_URL = "https://api.amazon.com/auth/o2/token"
API_BASE = "https://creatorsapi.amazon/catalog/v1"
MERCADO = os.environ.get("AMAZON_DOMINIO", "www.amazon.es")

DESCUENTO_MINIMO = int(os.environ.get("DESCUENTO_MINIMO", "20"))
MAX_CANDIDATOS = int(os.environ.get("MAX_CANDIDATOS", "12"))
PRECIO_MINIMO = float(os.environ.get("PRECIO_MINIMO", "12"))

# Que buscar. El indice es la categoria de Amazon; la palabra clave
# acota dentro de ella. La tercera es nuestra categoria, la que usa el
# bot para elegir los hashtags.
BUSQUEDAS = [
    ("Electronics",        "auriculares bluetooth",     "Tecnologia"),
    ("Electronics",        "smartwatch",                "Tecnologia"),
    ("Computers",          "teclado raton",             "Tecnologia"),
    ("HomeAndKitchen",     "aspirador",                 "Hogar"),
    ("HomeAndKitchen",     "organizador armario",       "Hogar"),
    ("HomeAndKitchen",     "freidora de aire",          "Cocina"),
    ("HomeAndKitchen",     "cafetera",                  "Cocina"),
    ("SportsAndOutdoors",  "mancuernas fitness",        "Deporte"),
    ("SportsAndOutdoors",  "zapatillas running",        "Deporte"),
    ("Beauty",             "cuidado facial",            "Belleza"),
    ("HealthPersonalCare", "cepillo dientes electrico", "Belleza"),
    ("Baby",               "carrito bebe",              "Bebe"),
    ("PetSupplies",        "accesorios perro",          "Mascotas"),
]

RUTA_CANDIDATOS = "candidatos.csv"
RUTA_OFERTAS = "ofertas.csv"
RUTA_ESTADO = "estado.json"
RUTA_VOLCADO = "respuesta_ejemplo.json"

CABECERA = ["asin", "titulo", "precio_ahora", "precio_antes",
            "categoria", "gancho", "imagen_url", "referencia"]


class SinPermiso(Exception):
    """Amazon nos ha cerrado la API por no llegar a las ventas minimas."""


class ErrorToken(Exception):
    """No hemos podido autenticarnos. Sin token no hay nada que hacer,
    asi que paramos en vez de reintentar una vez por categoria: el
    endpoint de tokens tambien bloquea si lo aporreas."""


# ======================================================================
# AUTENTICACION
# ======================================================================

_token = {"valor": None, "caduca": 0.0}


def token() -> str:
    """Token OAuth de la Creators API. Dura una hora; lo reutilizamos
    porque el endpoint de tokens tambien tiene limite de peticiones."""
    if _token["valor"] and time.time() < _token["caduca"]:
        return _token["valor"]

    cuerpo = json.dumps({
        "grant_type": "client_credentials",
        "client_id": CREATORS_ID,
        "client_secret": CREATORS_SECRET,
        "scope": "creatorsapi::default",
    }).encode("utf-8")

    peticion = urllib.request.Request(
        TOKEN_URL, data=cuerpo,
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(peticion, timeout=30) as r:
            datos = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detalle = e.read().decode("utf-8", "replace")[:400]
        raise ErrorToken(f"No he podido sacar el token ({e.code}): {detalle}") from e

    _token["valor"] = datos["access_token"]
    # Un minuto de margen, para no usarlo justo cuando caduca.
    _token["caduca"] = time.time() + int(datos.get("expires_in", 3600)) - 60
    return _token["valor"]


def _llamar(operacion: str, carga: dict) -> dict:
    cuerpo = json.dumps(carga).encode("utf-8")
    peticion = urllib.request.Request(
        f"{API_BASE}/{operacion}", data=cuerpo, method="POST",
        headers={
            "Authorization": f"Bearer {token()}",
            "Content-Type": "application/json",
            "x-marketplace": MERCADO,
        })
    try:
        with urllib.request.urlopen(peticion, timeout=30) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detalle = e.read().decode("utf-8", "replace")[:500]
        if e.code == 403 and "AssociateNotEligible" in detalle:
            raise SinPermiso(detalle) from e
        if e.code == 429:
            # ThrottleException: nos hemos pasado de peticiones.
            espera = 5
            try:
                espera = int(json.loads(detalle).get("retryAfterSeconds", 5))
            except Exception:
                pass
            print(f"AVISO throttling, espero {espera}s y reintento una vez.")
            time.sleep(espera)
            with urllib.request.urlopen(peticion, timeout=30) as r:
                return json.loads(r.read().decode("utf-8"))
        raise RuntimeError(f"Creators API {e.code}: {detalle}") from e


# ======================================================================
# BUSQUEDA
# ======================================================================

RECURSOS = [
    "itemInfo.title",
    "images.primary.large",
    "offersV2.listings.price",
]


def buscar(indice: str, palabras: str) -> list:
    datos = _llamar("searchItems", {
        "keywords": palabras,
        "searchIndex": indice,
        "itemCount": 10,
        "resources": RECURSOS,
        "partnerTag": AMAZON_TAG,
        "partnerType": "Associates",
    })
    resultado = datos.get("searchResult") or datos.get("SearchResult") or {}
    return resultado.get("items") or resultado.get("Items") or []


# ======================================================================
# LECTURA DE PRECIOS
#
# offersV2 es nuevo y la documentacion publica no termina de fijar como
# se llama el precio tachado. En vez de apostar por un nombre, buscamos
# el que venga, y si no hay ninguno el producto se descarta. Prefiero
# perder un candidato a inventarme un precio de referencia.
# ======================================================================

def _importe(valor) -> float | None:
    """Saca un numero de lo que venga: 12.3, {'amount': 12.3}, '12,30 EUR'."""
    if isinstance(valor, (int, float)):
        return float(valor)
    if isinstance(valor, dict):
        for clave in ("amount", "value", "Amount"):
            if clave in valor:
                return _importe(valor[clave])
        return None
    if isinstance(valor, str):
        limpio = "".join(c for c in valor if c.isdigit() or c in ".,")
        limpio = limpio.replace(".", "").replace(",", ".") if limpio.count(",") == 1 else limpio.replace(",", "")
        try:
            return float(limpio)
        except ValueError:
            return None
    return None


# Donde puede venir el precio tachado, por orden de preferencia, y como
# se llama en la tarjeta. Si Amazon anade o renombra alguno, se toca
# aqui y ya esta.
CLAVES_REFERENCIA = [
    ("basisPrice", None),
    ("savingBasis", None),
    ("strikeThroughPrice", None),
    ("listPrice", "PVP"),
    ("medianPrice", "Precio mediano"),
]


def precios(listado: dict) -> tuple:
    """Devuelve (precio_ahora, precio_antes, etiqueta) o (None, None, '')."""
    precio = listado.get("price") or {}
    ahora = _importe(precio)

    for clave, etiqueta_fija in CLAVES_REFERENCIA:
        crudo = precio.get(clave) if isinstance(precio, dict) else None
        if crudo is None:
            crudo = listado.get(clave)
        antes = _importe(crudo)
        if not antes:
            continue
        etiqueta = etiqueta_fija
        if etiqueta is None:
            tipo = ""
            if isinstance(crudo, dict):
                tipo = str(crudo.get("type") or crudo.get("savingBasisType") or "")
            if not tipo and isinstance(precio, dict):
                tipo = str(precio.get("savingBasisType") or "")
            etiqueta = {
                "LIST_PRICE": "PVP",
                "MEDIAN_PRICE": "Precio mediano",
            }.get(tipo.upper(), "Precio mediano")
        return ahora, antes, etiqueta

    return ahora, None, ""


def _limpiar_titulo(titulo: str) -> str:
    """Los titulos de Amazon son kilometricos y llevan comas. En la
    tarjeta no cabe mas de una linea y el CSV no lleva comillas."""
    t = " ".join((titulo or "").split()).replace(",", " ").replace(";", " ")
    t = " ".join(t.split())
    if len(t) > 52:
        t = t[:52].rsplit(" ", 1)[0]
    return t


def _dentro(dic, *camino):
    """Baja por un diccionario anidado sin reventar si falta algo."""
    for paso in camino:
        if not isinstance(dic, dict):
            return None
        dic = dic.get(paso)
    return dic


def candidato(item: dict, categoria: str) -> dict | None:
    """Convierte un resultado de la API en una fila, o lo descarta."""
    asin = item.get("asin") or item.get("ASIN")
    if not asin:
        return None

    titulo = _dentro(item, "itemInfo", "title", "displayValue")
    imagen = _dentro(item, "images", "primary", "large", "url")
    listados = _dentro(item, "offersV2", "listings") or []
    if not titulo or not imagen or not listados:
        return None

    ahora, antes, etiqueta = precios(listados[0])

    # Sin precio de referencia no hay descuento que anunciar, y
    # inventarselo es justo lo que no queremos hacer.
    if not ahora or not antes or antes <= ahora:
        return None
    if ahora < PRECIO_MINIMO:
        return None
    if round((1 - ahora / antes) * 100) < DESCUENTO_MINIMO:
        return None

    return {
        "asin": asin,
        "titulo": _limpiar_titulo(titulo),
        "precio_ahora": f"{ahora:.2f}",
        "precio_antes": f"{antes:.2f}",
        "categoria": categoria,
        "gancho": "Oferta flash",
        "imagen_url": imagen,
        "referencia": etiqueta,
    }


# ======================================================================
# QUE NOS SALTAMOS
# ======================================================================

def ya_vistos() -> set:
    """ASIN que ya estan publicados o en cola. No los repetimos."""
    vistos = set()
    if os.path.exists(RUTA_ESTADO):
        with open(RUTA_ESTADO, encoding="utf-8") as f:
            for p in json.load(f).get("publicadas", []):
                vistos.add(p.get("asin"))
    for ruta in (RUTA_OFERTAS, RUTA_CANDIDATOS):
        if os.path.exists(ruta):
            with open(ruta, encoding="utf-8-sig", newline="") as f:
                for fila in csv.DictReader(f):
                    vistos.add((fila.get("asin") or "").strip())
    vistos.discard("")
    vistos.discard(None)
    return vistos


def recoger(volcar: bool = False) -> list:
    vistos = ya_vistos()
    print(f"{len(vistos)} ASIN ya conocidos, esos los saltamos.")

    encontrados = []
    for indice, palabras, categoria in BUSQUEDAS:
        if len(encontrados) >= MAX_CANDIDATOS:
            break
        try:
            items = buscar(indice, palabras)
        except (SinPermiso, ErrorToken):
            raise
        except Exception as e:
            print(f"AVISO fallo la busqueda {indice}/{palabras}: {e}")
            continue

        if volcar and items and not os.path.exists(RUTA_VOLCADO):
            with open(RUTA_VOLCADO, "w", encoding="utf-8") as f:
                json.dump(items[0], f, ensure_ascii=False, indent=2)
            print(f"Guardado un resultado crudo en {RUTA_VOLCADO}.")

        nuevos = 0
        for item in items:
            fila = candidato(item, categoria)
            if not fila or fila["asin"] in vistos:
                continue
            vistos.add(fila["asin"])
            encontrados.append(fila)
            nuevos += 1
            if len(encontrados) >= MAX_CANDIDATOS:
                break
        print(f"{indice}/{palabras}: {len(items)} resultados, {nuevos} sirven.")
        time.sleep(1.5)

    # Los mejores descuentos arriba, que son los que vas a querer aprobar.
    encontrados.sort(
        key=lambda f: float(f["precio_antes"]) / float(f["precio_ahora"]),
        reverse=True)
    return encontrados


def escribir(filas: list) -> None:
    with open(RUTA_CANDIDATOS, "w", encoding="utf-8", newline="") as f:
        escritor = csv.DictWriter(f, fieldnames=CABECERA)
        escritor.writeheader()
        escritor.writerows(filas)


def main() -> int:
    if not CREATORS_ID or not CREATORS_SECRET:
        print("AVISO faltan CREATORS_ID o CREATORS_SECRET. No busco nada.")
        print("Se generan en Afiliados > Herramientas > Creators API.")
        return 0
    if not AMAZON_TAG:
        print("AVISO falta AMAZON_TAG. Sin etiqueta no tiene sentido buscar.")
        return 0

    try:
        filas = recoger(volcar="--volcar" in sys.argv)
    except SinPermiso:
        print("AVISO Amazon ha cortado el acceso (AssociateNotEligible).")
        print("Hacen falta 10 ventas cualificadas en los ultimos 30 dias.")
        print("El bot sigue publicando de ofertas.csv con normalidad.")
        return 0
    except ErrorToken as e:
        print(f"AVISO {e}")
        print("Revisa CREATORS_ID y CREATORS_SECRET en los Secrets del repositorio.")
        return 0
    except RuntimeError as e:
        # Un fallo de red no debe tumbar el repositorio.
        print(f"AVISO no he podido buscar: {e}")
        return 0

    if not filas:
        print("No he encontrado nada que merezca la pena hoy.")
        if os.path.exists(RUTA_CANDIDATOS):
            os.remove(RUTA_CANDIDATOS)
        return 0

    escribir(filas)
    print(f"\n{len(filas)} candidatos en {RUTA_CANDIDATOS}:")
    for f in filas:
        pct = round((1 - float(f["precio_ahora"]) / float(f["precio_antes"])) * 100)
        print(f"  -{pct}%  {f['titulo']}  {f['precio_ahora']} EUR "
              f"(antes {f['precio_antes']} {f['referencia']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
