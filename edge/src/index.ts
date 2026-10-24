/**
 * index.ts - Router del Worker de FaroNet.
 *
 *   POST /gateway/publicar       (firmado) el gateway publica un mensaje publico
 *   POST /gateway/directorio     (firmado) el gateway empuja fichas del directorio
 *   GET  /sala/:tema             pagina de la sala (navegador)
 *   GET  /sala/:tema/ws          WebSocket de la sala
 *   GET  /sala/:tema/historial   ultimos mensajes (JSON)
 *   GET  /sala/:tema/feed.atom   feed Atom del tema
 *   GET  /directorio[/:nombre]   replica publica de solo lectura, con firmas verificables
 *   PUT  /buzon/:id              deposita un blob cifrado de tamano fijo
 *   GET  /buzon/:id              recoge el blob y lo borra
 *
 * Todas las rutas pasan por el limite de peticiones (control.ts). Las del
 * gateway ademas exigen firma Ed25519 + marca de tiempo y rechazan repeticiones.
 *
 * Politica de no registro: de la peticion solo se lee CF-Connecting-IP, y solo
 * para armar (en memoria) la clave del limitador: un hash con sal diaria que
 * nunca se guarda ni se imprime. User-Agent y demas encabezados no se leen.
 */
import { VENTANA_S, bytesAHex, verificarGateway } from "./auth";
import { TAM_BLOB } from "./buzon";
import { construirAtom } from "./feed";
export { Sala } from "./sala";
export { Control } from "./control";
export { Buzon } from "./buzon";
export { Directorio } from "./directorio";

const TEMA_RE = /^[a-z0-9_-]{1,16}$/;   // los temas miden max. 16 bytes en la cebolla
const ID_BUZON_RE = /^[0-9a-f]{32}$/;     // 16 bytes en hex, derivados de un secreto compartido
const NOMBRE_NODO_MAX = 64;
const MAX_CUERPO = 2048;
const MAX_CUERPO_DIRECTORIO = 32 * 1024;
const MAX_FICHAS_POR_ENVIO = 64;
const MAX_TEXTO = 300;

// Peticiones permitidas por cliente en cada ventana (segundos).
const LIMITES = {
	lectura: { limite: 120, ventana: 60 },
	buzon: { limite: 30, ventana: 60 },
	gateway: { limite: 120, ventana: 60 }, // cuenta tambien los intentos con firma mala
} as const;
type Clase = keyof typeof LIMITES;

const CABECERAS = {
	"Cache-Control": "no-store",
	"Referrer-Policy": "no-referrer",
	"X-Content-Type-Options": "nosniff",
};

function texto(msg: string, status = 200): Response {
	return new Response(msg, { status, headers: { ...CABECERAS, "Content-Type": "text/plain; charset=utf-8" } });
}

function json(datos: unknown, extra: Record<string, string> = {}, status = 200): Response {
	return new Response(JSON.stringify(datos), {
		status,
		headers: { ...CABECERAS, "Content-Type": "application/json; charset=utf-8", ...extra },
	});
}

function control(env: Env) {
	return env.CONTROL.get(env.CONTROL.idFromName("global"));
}

/** Clave anonima del cliente: hash(IP | dia | sal). Cambia cada dia y no se guarda. */
async function claveCliente(request: Request, env: Env): Promise<string> {
	const ip = request.headers.get("CF-Connecting-IP") ?? "local";
	const dia = new Date().toISOString().slice(0, 10);
	const sal = (env as { LIMITE_SAL?: string }).LIMITE_SAL ?? "";
	const hash = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(`${ip}|${dia}|${sal}`));
	return bytesAHex(new Uint8Array(hash).slice(0, 16));
}

/** null si la peticion pasa; una respuesta 429 si el cliente excedio su limite. */
async function limitado(request: Request, env: Env, clase: Clase): Promise<Response | null> {
	const { limite, ventana } = LIMITES[clase];
	try {
		const clave = `${clase}:${await claveCliente(request, env)}`;
		if (await control(env).limitar(clave, limite, ventana)) return null;
	} catch {
		return null; // si el limitador falla, el servicio sigue (mejor eso que cerrarlo)
	}
	return new Response("demasiadas peticiones", {
		status: 429,
		headers: { ...CABECERAS, "Content-Type": "text/plain; charset=utf-8", "Retry-After": String(ventana) },
	});
}

/** Lee el cuerpo sin pasar de 'max' bytes; null si es mas grande. */
async function leerCuerpo(request: Request, max: number): Promise<Uint8Array | null> {
	if (Number(request.headers.get("Content-Length") ?? "0") > max) return null;
	if (!request.body) return new Uint8Array(0);
	const lector = request.body.getReader();
	const partes: Uint8Array[] = [];
	let total = 0;
	for (;;) {
		const { done, value } = await lector.read();
		if (done) break;
		total += value.byteLength;
		if (total > max) {
			await lector.cancel();
			return null;
		}
		partes.push(value);
	}
	const todo = new Uint8Array(total);
	let pos = 0;
	for (const p of partes) {
		todo.set(p, pos);
		pos += p.byteLength;
	}
	return todo;
}

/**
 * Peticiones del gateway: POST, limite, tamano, firma y anti-repeticion.
 * Devuelve el cuerpo ya autenticado, o la respuesta de error que corresponda.
 */
async function autenticarGateway(
	request: Request, env: Env, ruta: string, max: number,
): Promise<Uint8Array | Response> {
	if (request.method !== "POST") return texto("metodo no permitido", 405);
	const lento = await limitado(request, env, "gateway");
	if (lento) return lento;
	const cuerpo = await leerCuerpo(request, max);
	if (!cuerpo) return texto("cuerpo demasiado grande", 413);
	const buffer = cuerpo.buffer as ArrayBuffer;
	if (!(await verificarGateway(request, ruta, buffer, env.GATEWAY_PUBKEY))) {
		return texto("no autorizado", 401);
	}
	const firma = (request.headers.get("X-Faro-Firma") ?? "").toLowerCase();
	let nueva = true;
	try {
		nueva = await control(env).primeraVez(firma, 2 * VENTANA_S);
	} catch { /* sin anti-repeticion, la marca de tiempo sigue protegiendo */ }
	if (!nueva) return texto("peticion repetida", 409);
	return cuerpo;
}

function salaDe(env: Env, tema: string) {
	return env.SALA.get(env.SALA.idFromName(tema));
}

/** Politica de contenido de las paginas HTML: solo el script inline de esta pagina y conexion al mismo origen. */
function csp(nonce: string): string {
	return `default-src 'none'; script-src 'nonce-${nonce}'; style-src 'unsafe-inline'; `
		+ "connect-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'";
}

function paginaInicio(): Response {
	const nonce = btoa(String.fromCharCode(...crypto.getRandomValues(new Uint8Array(16))));
	const html = `<!doctype html>
<html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="referrer" content="no-referrer">
<title>FaroNet</title>
<style>
:root{color-scheme:light dark;font-family:system-ui,sans-serif}
body{max-width:42rem;margin:2rem auto;padding:0 1rem;line-height:1.5}
h1{font-size:1.5rem} input,button{font:inherit;padding:.3rem .6rem}
</style></head><body>
<h1>FaroNet</h1>
<p>Salas p&uacute;blicas de FaroNet. Solo los mensajes marcados como p&uacute;blicos salen de la red an&oacute;nima; no se guardan IPs ni identificadores.</p>
<form id="f"><label>Tema: <input id="t" pattern="[a-z0-9_-]{1,16}" placeholder="general" required></label> <button>Abrir sala</button></form>
<ul>
<li><a href="/directorio">Directorio p&uacute;blico de nodos</a> (solo lectura, con firmas verificables)</li>
</ul>
<script nonce="${nonce}">
document.getElementById("f").addEventListener("submit", function (e) {
  e.preventDefault();
  location.href = "/sala/" + encodeURIComponent(document.getElementById("t").value);
});
</script></body></html>`;
	return new Response(html, {
		headers: { ...CABECERAS, "Content-Type": "text/html; charset=utf-8", "Content-Security-Policy": csp(nonce) },
	});
}

function paginaSala(tema: string): Response {
	const nonce = btoa(String.fromCharCode(...crypto.getRandomValues(new Uint8Array(16))));
	// 'tema' ya paso TEMA_RE: es seguro ponerlo en el HTML.
	const html = `<!doctype html>
<html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="referrer" content="no-referrer">
<title>FaroNet - #${tema}</title>
<link rel="alternate" type="application/atom+xml" title="Feed de #${tema}" href="/sala/${tema}/feed.atom">
<style>
:root{color-scheme:light dark;font-family:system-ui,sans-serif}
body{max-width:42rem;margin:2rem auto;padding:0 1rem}
h1{font-size:1.25rem} #estado{font-size:.8rem;opacity:.7}
ul{list-style:none;padding:0} li{padding:.5rem 0;border-bottom:1px solid #8884}
time{font-size:.75rem;opacity:.6;margin-right:.5rem}
</style></head><body>
<h1>#${tema}</h1><p id="estado">conectando...</p><ul id="lista"></ul>
<script nonce="${nonce}">
var tema = ${JSON.stringify(tema)};
var lista = document.getElementById("lista");
var estado = document.getElementById("estado");
function agregar(m) {
  var li = document.createElement("li");
  var t = document.createElement("time");
  t.textContent = new Date(m.ts * 1000).toLocaleTimeString();
  li.appendChild(t);
  li.appendChild(document.createTextNode(m.texto));
  lista.insertBefore(li, lista.firstChild);
}
fetch("/sala/" + tema + "/historial").then(function (r) { return r.json(); })
  .then(function (h) { h.forEach(agregar); });
function conectar() {
  var ws = new WebSocket((location.protocol === "https:" ? "wss://" : "ws://") + location.host + "/sala/" + tema + "/ws");
  ws.onopen = function () { estado.textContent = "en vivo"; };
  ws.onmessage = function (e) { agregar(JSON.parse(e.data)); };
  ws.onclose = function () { estado.textContent = "reconectando..."; setTimeout(conectar, 3000); };
}
conectar();
</script></body></html>`;
	return new Response(html, {
		headers: { ...CABECERAS, "Content-Type": "text/html; charset=utf-8", "Content-Security-Policy": csp(nonce) },
	});
}

export default {
	async fetch(request: Request, env: Env): Promise<Response> {
		const url = new URL(request.url);
		const p = url.pathname.split("/").filter(Boolean);

		if (p.length === 0) {
			if (request.method !== "GET" && request.method !== "HEAD") return texto("metodo no permitido", 405);
			return paginaInicio();
		}

		// ---- gateway (firmado) ----
		if (p.length === 2 && p[0] === "gateway" && p[1] === "publicar") {
			const cuerpo = await autenticarGateway(request, env, url.pathname, MAX_CUERPO);
			if (cuerpo instanceof Response) return cuerpo;
			let datos: { tema?: unknown; texto?: unknown };
			try {
				datos = JSON.parse(new TextDecoder().decode(cuerpo));
			} catch {
				return texto("JSON invalido", 400);
			}
			const tema = typeof datos.tema === "string" ? datos.tema : "";
			const msg = typeof datos.texto === "string" ? datos.texto.trim() : "";
			if (!TEMA_RE.test(tema)) return texto("tema invalido", 400);
			if (!msg || msg.length > MAX_TEXTO) return texto("texto invalido", 400);
			return salaDe(env, tema).fetch("https://sala/publicar", {
				method: "POST",
				body: JSON.stringify({ texto: msg }),
			});
		}

		if (p.length === 2 && p[0] === "gateway" && p[1] === "directorio") {
			const cuerpo = await autenticarGateway(request, env, url.pathname, MAX_CUERPO_DIRECTORIO);
			if (cuerpo instanceof Response) return cuerpo;
			let datos: { fichas?: unknown };
			try {
				datos = JSON.parse(new TextDecoder().decode(cuerpo));
			} catch {
				return texto("JSON invalido", 400);
			}
			const fichas = datos.fichas;
			if (!Array.isArray(fichas) || fichas.length > MAX_FICHAS_POR_ENVIO
				|| !fichas.every((f) => typeof f === "string")) {
				return texto("se esperaba { fichas: [hex, ...] } con pocas fichas", 400);
			}
			const dir = env.DIRECTORIO.get(env.DIRECTORIO.idFromName("global"));
			return json(await dir.publicar(fichas as string[]));
		}

		// ---- directorio publico (solo lectura) ----
		if (p[0] === "directorio" && (p.length === 1 || p.length === 2)) {
			if (request.method !== "GET") return texto("metodo no permitido", 405);
			const lento = await limitado(request, env, "lectura");
			if (lento) return lento;
			const dir = env.DIRECTORIO.get(env.DIRECTORIO.idFromName("global"));
			const abierto = { "Access-Control-Allow-Origin": "*", "Cache-Control": "public, max-age=15" };
			if (p.length === 1) {
				return json({ generado: Math.floor(Date.now() / 1000), fichas: await dir.listar() }, abierto);
			}
			let nombre = "";
			try { nombre = decodeURIComponent(p[1]); } catch { /* queda vacio */ }
			if (!nombre || nombre.length > NOMBRE_NODO_MAX) return texto("nombre invalido", 400);
			const ficha = await dir.obtener(nombre);
			return ficha ? json(ficha, abierto) : texto("no encontrado", 404);
		}

		// ---- buzon muerto ----
		if (p[0] === "buzon" && p.length === 2) {
			const id = p[1];
			if (!ID_BUZON_RE.test(id)) return texto("id invalido", 400);
			if (request.method !== "PUT" && request.method !== "GET") return texto("metodo no permitido", 405);
			const lento = await limitado(request, env, "buzon");
			if (lento) return lento;
			const buzon = env.BUZON.get(env.BUZON.idFromName("global"));
			if (request.method === "PUT") {
				const blob = await leerCuerpo(request, TAM_BLOB);
				if (!blob) return texto("blob demasiado grande", 413);
				if (blob.byteLength !== TAM_BLOB) return texto(`el blob debe medir exactamente ${TAM_BLOB} bytes`, 400);
				const r = await buzon.depositar(id, blob.buffer as ArrayBuffer);
				if (r === "existe") return texto("ese id ya tiene un blob", 409);
				if (r === "lleno") return texto("buzon lleno", 503);
				return texto("depositado", 201);
			}
			const blob = await buzon.recoger(id);   // lo borra al leerlo
			if (!blob) return texto("no hay nada", 404);
			return new Response(blob, {
				headers: { ...CABECERAS, "Content-Type": "application/octet-stream" },
			});
		}

		// ---- salas publicas ----
		if (p[0] === "sala" && p.length >= 2) {
			const tema = p[1];
			if (!TEMA_RE.test(tema)) return texto("tema invalido", 400);
			if (request.method !== "GET") return texto("metodo no permitido", 405);
			const lento = await limitado(request, env, "lectura");
			if (lento) return lento;
			if (p.length === 2) return paginaSala(tema);
			if (p.length === 3 && (p[2] === "ws" || p[2] === "historial")) {
				return salaDe(env, tema).fetch(request);
			}
			if (p.length === 3 && p[2] === "feed.atom") {
				const entradas = await salaDe(env, tema).ultimos(50);
				return new Response(construirAtom(tema, url.origin, entradas), {
					headers: {
						...CABECERAS,
						"Content-Type": "application/atom+xml; charset=utf-8",
						"Cache-Control": "public, max-age=30",
					},
				});
			}
		}

		return texto("no encontrado", 404);
	},
} satisfies ExportedHandler<Env>;
