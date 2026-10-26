# FaroNet (Fasolis)

Proyecto 1 de Redes de Computadoras (TEC): una red mesh anónima que viaja por luz infrarroja
(el **Faro**) y por TCP/IP, con salida controlada a Internet por el edge
(un Worker de Cloudflare, en `edge/`).

Este README es el **mapa del repositorio**: qué hace cada archivo y cómo se conectan.
El Kick-off (introducción, ambiente, diseño y modelo de amenazas) está en [`docs/Kickoff.md`](docs/Kickoff.md).

---

## Estructura

```
FaSolis/
├── README.md                    este mapa
├── .gitignore
│
├── faronet/                     todo el código Python
│   ├── faro/                    Capa 1 y hardware (la luz)
│   │   ├── capa1.py
│   │   ├── enlace_faro.py
│   │   └── hardware.py
│   ├── mesh/                    Capas 2 y 3 y privacidad (la red anónima)
│   │   ├── paquete.py
│   │   ├── identidad.py
│   │   ├── directorio.py
│   │   ├── cebolla.py
│   │   └── nodo.py
│   └── herramientas/            programas que se ejecutan desde la terminal
│       ├── enviar.py
│       ├── recibir.py
│       ├── medir_enlace.py
│       ├── faro_nodo.py
│       ├── gateway.py           cliente firmado del Worker
│       ├── gateway_nodo.py      nodo gateway: mesh -> edge
│       └── buzon.py             cliente del buzón muerto
│
├── edge/                        Worker de Cloudflare (TypeScript), ver edge/README.md
│   ├── src/                     index, auth, control, sala, feed, buzon, directorio
│   ├── pruebas/                 pruebas del Worker y prueba de humo
│   ├── wrangler.jsonc           configuración y despliegue
│   └── package.json
│
├── pruebas/                     pruebas automáticas de Python (unittest)
│   ├── test_capa1.py
│   ├── test_cebolla.py
│   ├── test_gateway.py
│   └── test_scripts.py
│
├── scripts/
│   └── probar_cebolla.ps1       levanta directorio + 6 nodos en Windows
│
└── docs/
    ├── Kickoff.md                   documento de kick-off
    ├── DiagramaModelo.png           diagrama de arquitectura y hardware
    └── explicación del proyecto.pdf guía de estudio de todo el proyecto
```

Cómo se apilan los módulos (cada uno solo importa de los de abajo):

```
faro_nodo ─┬─ nodo ── cebolla ── directorio ── identidad ── paquete
           ├─ enlace_faro ───────────────────────────────── paquete
           └─ hardware ─┐
enviar / recibir /      ├─ capa1
medir_enlace ───────────┘

gateway_nodo ─┬─ nodo (igual que arriba)
              └─ gateway ── HTTPS firmado ──> edge (Worker de Cloudflare)
buzon ───────────────────── HTTPS ──────────> edge
```

---

## `faronet/faro/` — Capa 1 y hardware

| Archivo | Qué hace |
|---|---|
| `capa1.py` | **Protocolo físico v1 y la biblioteca `DispositivoLuzAdaptador`.** Define la `Trama` (sincronía `AA 55`, versión, tipo, destino, origen, secuencia, fragmento, longitud, datos, CRC-16; máximo 128 bytes), `codificar()`, el `Decodificador` (extrae tramas válidas de bytes con basura o cortes) y el adaptador, que envía y recibe sobre un puerto serie con acceso al medio maestro/esclavo, ACK y reintentos. También guarda la dirección física en `~/.faronet_direccion` (`direccion_persistente`) y permite rotarla. Soporta tramas creadas fuera del Faro (`TIPO_EXTERNA`). |
| `enlace_faro.py` | **El Faro como un vecino más de la mesh.** Le entrega al nodo un par reader/writer de asyncio, pero los paquetes de 512 bytes viajan por luz usando el adaptador (en su propio hilo). Aprende la dirección del otro Faro sola (difusión `FFFF` hasta recibir el primer paquete) y reintenta con espera aleatoria si el medio está ocupado. |
| `hardware.py` | **LEDs y botón de pánico (Raspberry Pi).** `Indicadores` maneja el LED verde (TX, GPIO17), el amarillo (RX, GPIO27) y el botón (GPIO22) con gpiozero; en una laptop sin GPIO no falla. `PuertoConIndicadores` enciende los LEDs con la actividad del puerto, `abrir_faro()` abre el Faro con LEDs, y `ejecutar_panico()` / `borrar_archivo_seguro()` borran las llaves. `python3 -m faronet.faro.hardware` corre la autoprueba del circuito. |

## `faronet/mesh/` — Capas 2 y 3, privacidad

| Archivo | Qué hace |
|---|---|
| `paquete.py` | **Formato común de paquete** (versión 1). Todo mide 512 bytes: versión, tipo, id de circuito, longitud y contenido con relleno aleatorio (padding fijo). Aquí están todos los números de tipo (mesh, cebolla y directorio) para que no se repitan. |
| `identidad.py` | **Llaves y fichas.** Cada nodo tiene una llave de firma Ed25519 (en `~/.faronet/<nombre>.llaves`, que es lo que borra el botón de pánico) y una de cifrado X25519 que vive solo en memoria y rota, con un periodo de gracia. `Ficha` es la entrada firmada del directorio (nombre, llaves públicas, vencimiento, firma). |
| `directorio.py` | **Servicio de directorio de nodos.** Los nodos publican su ficha firmada y cualquiera la consulta. Rechaza firmas inválidas, fichas vencidas, suplantación de nombres y fichas viejas repetidas. Todo en memoria, sin IPs ni bitácoras. |
| `cebolla.py` | **Enrutamiento por capas (onion, 3 saltos).** Construye y procesa celdas de tamaño fijo: cifrado por capa con llaves efímeras, MAC por salto, mensaje sellado extremo a extremo (anónimo, firmado o público para el gateway) y descarte de repeticiones. Solo usa PyNaCl. Las limitaciones conocidas están en el docstring, listas para el RFC. |
| `nodo.py` | **El nodo de la mesh.** Escucha TCP, saluda a los vecinos, aprende rutas con vector de distancias (tipo RIP), reenvía paquetes, publica su ficha en el directorio y expone la terminal interactiva (`anonimo`, `firmado`, `publico`, `rutas`, `directorio`, `rotarllaves`, `vecinos`, `salir`). Tiene el hook `al_recibir_publico` para conectar un gateway. |

## `faronet/herramientas/` — programas de terminal

| Archivo | Qué hace |
|---|---|
| `enviar.py` | Envía un mensaje o archivo por el Faro (maestro). Soporta difusión y tramas externas. |
| `recibir.py` | Espera mensajes por el Faro (esclavo); texto por stdout, estado por stderr. |
| `medir_enlace.py` | Mide **BER** y **tasa efectiva** a una distancia dada (se corre en las dos Pi a la vez). Imprime una línea `RESULTADO;...` para la tabla del informe. |
| `faro_nodo.py` | Nodo completo para la Raspberry Pi: el mismo `nodo.py` más el otro Faro como vecino, LEDs y botón de pánico (borra llaves, rota la dirección física). Comandos extra: `faro`, `rotar`. |
| `gateway.py` | **Cliente del Worker.** Crea o lee la llave de firma propia del gateway (`~/.faronet/gateway.llave`) y firma las peticiones como espera `edge/src/auth.ts` (Ed25519 sobre método, ruta, marca de tiempo y SHA-256 del cuerpo). Publica mensajes en una sala y manda fichas del directorio en grupos de 64. `--mostrar-llave` imprime la llave pública que va en `GATEWAY_PUBKEY`; `--probar TEMA TEXTO` publica un mensaje de prueba. |
| `gateway_nodo.py` | **Nodo gateway.** El mismo `nodo.py` conectado al hook `al_recibir_publico`: cada mensaje público que llega por la cebolla se publica en la sala de su tema en el Worker (reintenta si recibe 409). Cada `--sync-directorio` segundos (60 por defecto) envía las fichas del directorio de la mesh a la réplica pública del edge. No guarda nada a disco. |
| `buzon.py` | **Cliente del buzón muerto.** Del secreto compartido deriva el id opaco y la llave; deposita y recoge blobs cifrados de exactamente 1024 bytes (hasta 982 bytes de texto). El Worker borra el blob al entregarlo. |

## `edge/` — Worker de Cloudflare

La capa de aplicación pública (clearnet), en TypeScript sobre Cloudflare Workers (plan gratuito).
Solo recibe los mensajes marcados como públicos; los privados nunca llegan aquí. Los detalles
(rutas, decisiones de diseño, qué sigue viendo Cloudflare y cómo desplegar) están en
[`edge/README.md`](edge/README.md).

| Archivo | Qué hace |
|---|---|
| `src/index.ts` | **Router del Worker.** Rutas del gateway (`POST /gateway/publicar`, `POST /gateway/directorio`), páginas HTML (`/`, `/sala/:tema`) con CSP y nonce por petición, WebSocket, historial y feed de cada sala, directorio público y buzón. Aplica límite de tamaño y de peticiones a todo. |
| `src/auth.ts` | Verifica la firma Ed25519 del gateway con una ventana de 60 s. |
| `src/control.ts` | Durable Object `Control` (solo en memoria): rate limiting por hash diario de la IP y anti-repetición de firmas (409). |
| `src/sala.ts` | Durable Object `Sala`, uno por tema: guarda solo `(ts, texto)` en SQLite (24 h, máx. 200), reenvía en vivo por WebSockets hibernables de solo lectura y borra lo vencido con una alarma. |
| `src/feed.ts` | Feed Atom de un tema, con XML escapado y autor siempre "anónimo". |
| `src/buzon.ts` | Durable Object `Buzon`: blobs cifrados de 1024 bytes, TTL de 24 h, se borran al recogerlos (`DELETE ... RETURNING`, atómico). |
| `src/directorio.ts` | Durable Object `Directorio`: réplica pública de solo lectura; verifica la firma de cada ficha, así que el gateway puede omitir fichas pero no inventarlas ni alterarlas. |
| `wrangler.jsonc` | Configuración (equivale al `wrangler.toml` del enunciado): migraciones y bindings de los Durable Objects, `GATEWAY_PUBKEY` y `observability` apagada (sin registros). |
| `pruebas/` | `edge.test.ts` (38 pruebas en Node con un entorno falso, sin wrangler ni red) y `humo.mjs` (prueba de humo contra un Worker corriendo, incluye WebSockets). |

## `pruebas/`

| Archivo | Qué prueba |
|---|---|
| `test_capa1.py` | Trama, CRC, recuperación ante basura y cortes, y el adaptador, con puertos virtuales (`PuertoVirtual`); no necesita hardware. |
| `test_cebolla.py` | Lógica criptográfica de la cebolla sobre una mesh falsa en memoria. Requiere `pynacl`. |
| `test_gateway.py` | Que la firma del gateway coincida con la que verifica el Worker, el envío de fichas en grupos de 64 y el cliente del buzón (ida y vuelta, tamaño fijo, secreto equivocado). Usa un servidor falso, sin red. |
| `test_scripts.py` | `enviar` y `recibir` de punta a punta sobre puertos virtuales. |

Se corren todas con `py -m unittest discover -v` (84 pruebas). Las del Worker (38) están en
`edge/pruebas/` y se corren con `npm run probar` dentro de `edge/`.

## Otros archivos

| Archivo | Qué hace |
|---|---|
| `scripts/probar_cebolla.ps1` | En Windows abre el directorio y 6 nodos encadenados (N1…N6) en `127.0.0.1`. |
| `.gitignore` | Evita subir llaves (`*.llaves`, `.faronet/`), `.env`, `.dev.vars`, `__pycache__`, `node_modules`, `.wrangler/`, etc. |

---

## Cómo ejecutar

Todos los comandos se corren **desde la raíz del repositorio** con `-m` (en Linux/Raspberry, `python3` en lugar de `py`).

```bash
pip install pynacl                     # mesh, cebolla, gateway y buzón
pip install pyserial gpiozero          # solo en la Raspberry Pi (Faro)

# Pruebas
py -m unittest discover -v

# Mesh en una laptop: directorio + cadena A - B - C - D
py -m faronet.mesh.directorio --puerto 9000
py -m faronet.mesh.nodo --nombre A --puerto 9001 --directorio 127.0.0.1:9000
py -m faronet.mesh.nodo --nombre B --puerto 9002 --vecino 127.0.0.1:9001 --directorio 127.0.0.1:9000

# O todo junto en Windows (directorio + 6 nodos)
.\scripts\probar_cebolla.ps1

# Faro (en la Raspberry Pi)
python3 -m faronet.faro.hardware                      # autoprueba del circuito
python3 -m faronet.herramientas.recibir --una
python3 -m faronet.herramientas.enviar 1234 --texto "Hola"
python3 -m faronet.herramientas.medir_enlace receptor --baudios 2400 --distancia 50cm
python3 -m faronet.herramientas.faro_nodo --nombre PA --puerto 9001 --baudios 2400

# Edge (Worker de Cloudflare)
py -m faronet.herramientas.gateway --mostrar-llave     # desde la raíz: va en GATEWAY_PUBKEY (edge/.dev.vars o wrangler.jsonc)
cd edge
npm install
npm run probar                                         # 38 pruebas, sin wrangler ni red
npx wrangler dev                                       # http://localhost:8787
npx wrangler deploy                                    # despliegue real

# Gateway y buzón (desde la raíz, con el Worker corriendo)
py -m faronet.herramientas.gateway --edge http://localhost:8787 --probar general "hola"
py -m faronet.herramientas.gateway_nodo --nombre GW --puerto 9010 --vecino 127.0.0.1:9006 --directorio 127.0.0.1:9000 --edge http://localhost:8787
py -m faronet.herramientas.buzon --edge http://localhost:8787 depositar --secreto "<16+ caracteres al azar>" "hola"
py -m faronet.herramientas.buzon --edge http://localhost:8787 recoger --secreto "<el mismo>"
```

Desde otro nodo de la mesh, `publico GW general hola` manda un mensaje por la cebolla al gateway,
que lo publica en `http://localhost:8787/sala/general` (y en `/sala/general/feed.atom`).

Cada programa tiene `--help` y ejemplos más completos en su docstring.

---

## Pendiente (según el enunciado)

Ya están el Faro (Capa 1), la mesh con la cebolla, el gateway y el edge en Cloudflare. Falta:

- **RFC** en ASCII (Capa 1 y FaroNet, con Consideraciones de Privacidad y de Seguridad), sugerido en `docs/`.
  Los docstrings de `capa1.py` y `cebolla.py` y el README de `edge/` ya traen formatos y limitaciones listos para usar.
- **Documentación final**, esquemático, BOM, fotos del Faro soldado y bitácora de multímetro.
- **`requirements.txt`**: no está en el repositorio (por ahora las dependencias se instalan a mano, ver "Cómo ejecutar").
