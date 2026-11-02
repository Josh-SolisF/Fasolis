# edge/ - Worker de Cloudflare de FaroNet

La capa de aplicación **pública** (clearnet). Todo corre en el plan gratuito de
Cloudflare Workers. Lo único que sale de FaroNet son los mensajes marcados como
públicos; los privados nunca llegan aquí.

## Qué hace

| Ruta | Quién | Qué |
|---|---|---|
| `POST /gateway/publicar` | gateway (firmado) | publica un mensaje público en la sala de un tema |
| `POST /gateway/directorio` | gateway (firmado) | empuja fichas del directorio de la mesh |
| `GET /` | navegador | página de inicio: abrir una sala por tema y enlace al directorio |
| `GET /sala/:tema` | navegador | página de la sala, en vivo |
| `GET /sala/:tema/ws` | navegador | WebSocket de la sala (solo lectura) |
| `GET /sala/:tema/historial` | cualquiera | últimos 50 mensajes (JSON) |
| `GET /sala/:tema/feed.atom` | lector de feeds | feed Atom del tema |
| `GET /directorio`, `/directorio/:nombre` | cualquiera | réplica pública de solo lectura, con firmas verificables |
| `PUT /buzon/:id` | cualquiera | deposita un blob cifrado de exactamente 1024 bytes |
| `GET /buzon/:id` | cualquiera | recoge el blob y **lo borra** (404 si no hay o venció) |

Requisitos del enunciado y dónde están:

- **Relay del gateway con firmas:** `auth.ts`. Ed25519 sobre `METODO\nRUTA\nTS\nSHA256(cuerpo)`, ventana de 60 s, y `index.ts` rechaza (409) una firma ya vista (anti-replay, en `control.ts`).
- **Salas en tiempo real:** `sala.ts`, un Durable Object por tema con WebSockets hibernables.
- **Feed por tema:** `feed.ts` (Atom, XML escapado, autor siempre "anónimo").
- **Buzón muerto:** `buzon.ts`. El Worker solo ve bytes cifrados de tamaño fijo, con TTL de 24 h.
- **Directorio público:** `directorio.ts`. El Worker verifica cada ficha por su cuenta: el gateway puede omitir fichas, pero no inventarlas ni alterarlas.
- **Rate limiting:** `control.ts` + `index.ts`. 120 lecturas/min, 30 usos del buzón/min y 120 intentos/min al gateway, por cliente.
- **Páginas HTML:** `index.ts` les pone una *Content-Security-Policy* con nonce por petición (`default-src 'none'`, solo el script de la propia página, conexión al mismo origen, sin iframes).
- **Sin registro:** `observability` apagada en `wrangler.jsonc`; ninguna tabla guarda IP ni encabezados (hay una prueba que lo comprueba).
- **TTL de verdad:** salas (24 h), buzón (24 h) y directorio (vencimiento de cada ficha) borran con una *alarma*, no solo esconden lo vencido.

### Una decisión que conviene conocer

El enunciado sugiere KV, D1 o R2 para el buzón. Aquí se usa el **SQLite de un Durable Object** (la misma tecnología que D1, y la que ya usan las salas) porque:
1. no hay que crear ni enlazar nada antes de desplegar (`wrangler deploy` basta);
2. "recoger y borrar" es un solo `DELETE ... RETURNING`, atómico: dos lectores a la vez nunca reciben el mismo blob. Con KV sí podría pasar.

Si el profesor exige D1 o KV, solo cambia `buzon.ts`; el resto no se entera.

## Probar

```bash
cd edge
npm install              # actualiza package-lock.json (se agregó tsx)
npm run tipos            # revisa los tipos contra el runtime de Workers
npm run probar           # 38 pruebas del Worker, sin wrangler ni red
```

`npm run probar` ejecuta el código real de `src/` en Node con un entorno falso
(`pruebas/entorno.ts`: SQLite en memoria, sin WebSockets). Comprueba la lógica,
**no** el comportamiento de Cloudflare: antes de entregar, probar también con
`wrangler dev` y con el despliegue real.

### Prueba de humo contra el Worker de verdad

`npm run humo` habla con un Worker **ya corriendo** y sí cubre lo que el entorno falso no: la
sala en vivo por WebSocket. Firma con la llave del gateway (`~/.faronet/gateway.llave`, la misma
de `gateway.py`), así que su llave pública debe ser la `GATEWAY_PUBKEY` del Worker.

```bash
npm run humo -- http://localhost:8787                     # con `wrangler dev` en otra terminal
npm run humo -- https://faronet.USUARIO.workers.dev       # el despliegue real (para la revisión)
```

Comprueba: inicio con CSP, 401 sin firma, publicar firmado, repetición (409), mensaje en vivo por
WebSocket, historial, feed Atom, buzón (depositar, 409, 400, recoger, 404 al releer) y directorio.
Sale con código 1 si algo falla. (`npm run humo:falso` levanta el Worker con el entorno falso para
ensayar el script sin wrangler; ahí usar `--sin-ws`.)

## Desplegar

```bash
# 1. La llave del gateway (desde la raíz del repositorio)
py -m faronet.herramientas.gateway --mostrar-llave        # imprime 64 caracteres hex

# 2. Local: va en edge/.dev.vars (no se sube; hay un .dev.vars.example)
echo GATEWAY_PUBKEY=<la llave> > edge/.dev.vars
cd edge && npx wrangler dev                                # http://localhost:8787

# 3. Producción: pegar la llave PÚBLICA en "vars" de wrangler.jsonc y
npx wrangler login
npx wrangler deploy                                        # aplica las migraciones solo
npx wrangler types                                         # regenera worker-configuration.d.ts

# 4. Opcional: sal propia para el limitador (si no, usa solo la fecha)
npx wrangler secret put LIMITE_SAL
```

Cómo probarlo de punta a punta:

```bash
py -m faronet.herramientas.gateway --edge http://localhost:8787 --probar general "hola"
# abrir http://localhost:8787/sala/general   y   /sala/general/feed.atom

py -m faronet.herramientas.buzon --edge http://localhost:8787 depositar --secreto "<16+ caracteres al azar>" "hola"
py -m faronet.herramientas.buzon --edge http://localhost:8787 recoger   --secreto "<el mismo>"

# El gateway real (nodo de la mesh): mensajes por la cebolla -> sala, y directorio -> edge
py -m faronet.herramientas.gateway_nodo --nombre GW --puerto 9010 --vecino 127.0.0.1:9006 \
   --directorio 127.0.0.1:9000 --edge http://localhost:8787
# y desde otro nodo de la mesh:   publico GW general hola desde la cebolla
```

## Qué sigue viendo Cloudflare (para el modelo de amenazas del RFC)

El Worker no guarda IPs ni encabezados, pero el **proveedor** ve todo lo que pasa por su red, y no hay forma de evitarlo desde el código:

- La **IP** de quien se conecta, el `User-Agent`, los tiempos y el tamaño de cada petición, y los metadatos de TLS.
- Las **rutas**: qué temas se leen y qué ids de buzón se piden. Quien deposita y quien recoge un blob quedan enlazables por IP y por tiempo. Mitigación: hacer esas peticiones desde un nodo detrás de la mesh o desde otra red.
- El **texto de los mensajes públicos** (son públicos por definición) y los blobs cifrados del buzón (cifrados, de tamaño fijo, pero visibles como bytes).
- Todo lo guardado en los Durable Objects, aunque el código no lo use para nada más.
- La **IP del gateway** y cuándo publica. El gateway sabe el texto y el tema, pero no quién lo originó (llega por la cebolla).
- El limitador usa un hash de la IP con sal diaria, solo en memoria. Protege de abuso, **no** del propio Cloudflare: con la IP delante, calcular el hash es trivial.

## Limitaciones conocidas

- `npm run probar` no cubre los WebSockets (el entorno falso no los tiene); los cubre `npm run humo` contra `wrangler dev` o el despliegue.
- El limitador y el anti-replay viven en memoria de un solo Durable Object: si Cloudflare lo reinicia, se pierden (un replay en esa fracción de segundo cabría) y todo el tráfico pasa por él. Para este proyecto basta.
- Una firma idéntica en el mismo segundo se toma por repetición. `gateway_nodo.py` lo maneja reintentando con otra marca de tiempo.
- Cada petición gasta además una petición al Durable Object `Control`; el plan gratuito permite 100 000 al día.
- Sin secreto hacia adelante en el buzón: el id y la llave salen del secreto compartido; quien lo adivine recoge el blob antes que el destinatario. Usar secretos largos y al azar.
