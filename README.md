# CholloPage

Publica una oferta de Amazon cada dos horas en Facebook e Instagram,
mantiene al dia una pagina de link-in-bio y deja preparado el paquete
para TikTok. Corre sobre GitHub Actions y GitHub Pages: coste cero.

## Que hace cada ejecucion

1. Coge la primera fila de `ofertas.csv` que aun no se haya publicado.
2. Genera la imagen 1080x1080 en `docs/img/`: foto del producto, precio
   actual, precio anterior tachado e insignia de descuento.
3. Hace commit de la imagen para que tenga URL publica (Instagram lo exige).
4. Publica en la Pagina de Facebook y en la cuenta de Instagram Business.
5. Anade la oferta arriba del todo en la pagina de bio.
6. Deja el paquete de TikTok como artefacto descargable de la ejecucion.

## Como anadir ofertas

Edita `ofertas.csv` desde la web de GitHub. Una fila por chollo:

    asin,titulo,precio_ahora,precio_antes,categoria,gancho,imagen_url
    B0CHX1W1XY,Auriculares Bluetooth ANC,39.99,79.99,Tecnologia,Minimo historico,https://...

El ASIN son los diez caracteres detras de `/dp/` en la URL de Amazon.
Titulos cortos: el largo se recorta a una linea en la imagen.

`imagen_url` es la foto del producto. Si la dejas vacia, el bot publica
una tarjeta solo de texto en lugar de fallar.

## Configuracion

En Settings, Secrets and variables, Actions.

Secrets: `AMAZON_TAG`, `META_TOKEN`, `FB_PAGE_ID`, `IG_USER_ID`.
Variables: `MARCA`, `PAGES_URL`.

## Probarlo sin publicar nada

    EN_SECO=1 AMAZON_TAG=prueba-21 python bot.py preparar
    EN_SECO=1 AMAZON_TAG=prueba-21 python bot.py publicar

Genera la imagen y la bio e imprime los textos, sin tocar Meta.

## Lo que no hace, y por que

- **No publica en TikTok.** Su API deja las publicaciones en privado
  hasta pasar una auditoria de cumplimiento. Te deja imagen y texto
  listos en el artefacto `tiktok-N` de cada ejecucion.
- **No descarga las fotos de la ficha de Amazon.** Su acuerdo de
  afiliados solo permite obtenerlas via Product Advertising API.
- **No acorta enlaces.** Amazon prohibe los acortadores que ocultan que
  el destino es Amazon.
- **No quita la divulgacion de afiliado.** Es obligatoria en cada
  publicacion. Esta arriba del todo de `bot.py` por si quieres
  reformularla, pero no la borres.

## Cuando llegues a 3 ventas

Amazon te concede la Product Advertising API y el bot puede pasar a
buscar las ofertas solo, con foto oficial. Ojo: desde noviembre de 2025
hay que mantener 10 ventas cada 30 dias para conservar el acceso.
