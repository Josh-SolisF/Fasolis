/**
 * index.ts - Router del Worker de FaroNet.
 *   POST /gateway/publicar     (firmado) el gateway publica un mensaje publico
 *   GET  /sala/:tema           pagina de la sala (navegador)
 *   GET  /sala/:tema/ws        WebSocket de la sala
 *   GET  /sala/:tema/historial ultimos mensajes (JSON)
 */
import { verificarGateway } from "./auth";
export { Sala } from "./sala";

const TEMA_RE = /^[a-z0-9_-]{1,16}$/;   // los temas miden max. 16 bytes en la cebolla
const MAX_CUERPO = 2048;
const MAX_TEXTO = 300;

const CABECERAS = {
	"Cache-Control": "no-store",
	"Referrer-Policy": "no-referrer",
	"X-Content-Type-Options": "nosniff",
};

function texto(msg: string, status = 200): Response {
	return new Response(msg, { status, headers: { ...CABECERAS, "Content-Type": "text/plain; charset=utf-8" } });
}

function salaDe(env: Env, tema: string) {
	return env.SALA.get(env.SALA.idFromName(tema));
}

function paginaSala(tema: string): Response {
	// 'tema' ya paso TEMA_RE: es seguro ponerlo en el HTML.
	const html = `<!doctype html>
<html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="referrer" content="no-referrer">
<title>FaroNet - #${tema}</title>
<style>
:root{color-scheme:light dark;font-family:system-ui,sans-serif}
body{max-width:42rem;margin:2rem auto;padding:0 1rem}
h1{font-size:1.25rem} #estado{font-size:.8rem;opacity:.7}
ul{list-style:none;padding:0} li{padding:.5rem 0;border-bottom:1px solid #8884}
time{font-size:.75rem;opacity:.6;margin-right:.5rem}
</style></head><body>
<h1>#${tema}</h1><p id="estado">conectando...</p><ul id="lista"></ul>
<script>
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
	return new Response(html, { headers: { ...CABECERAS, "Content-Type": "text/html; charset=utf-8" } });
}

export default {
	async fetch(request: Request, env: Env): Promise<Response> {
		const url = new URL(request.url);
		const p = url.pathname.split("/").filter(Boolean);

		if (p.length === 0) return texto("FaroNet edge");

		if (p.length === 2 && p[0] === "gateway" && p[1] === "publicar") {
			if (request.method !== "POST") return texto("metodo no permitido", 405);
			const cuerpo = await request.arrayBuffer();
			if (cuerpo.byteLength > MAX_CUERPO) return texto("cuerpo demasiado grande", 413);
			if (!(await verificarGateway(request, url.pathname, cuerpo, env.GATEWAY_PUBKEY))) {
				return texto("no autorizado", 401);
			}
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

		if (p[0] === "sala" && p.length >= 2) {
			const tema = p[1];
			if (!TEMA_RE.test(tema)) return texto("tema invalido", 400);
			if (p.length === 2) return paginaSala(tema);
			if (p.length === 3 && (p[2] === "ws" || p[2] === "historial") && request.method === "GET") {
				return salaDe(env, tema).fetch(request);
			}
		}

		return texto("no encontrado", 404);
	},
} satisfies ExportedHandler<Env>;