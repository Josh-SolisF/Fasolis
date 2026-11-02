#!/usr/bin/env node
/**
 * humo.mjs - Prueba de humo de un Worker YA corriendo (wrangler dev o el despliegue real).
 *
 * A diferencia de `npm run probar` (entorno falso, sin WebSockets), esto habla con el
 * Worker de verdad: relay firmado, anti-replay, sala en vivo por WebSocket, historial,
 * feed Atom, buzon muerto y directorio publico.
 *
 *   node pruebas/humo.mjs http://localhost:8787
 *   node pruebas/humo.mjs https://faronet.USUARIO.workers.dev
 *   node pruebas/humo.mjs <url> --sin-ws        (si el entorno no soporta WebSocket)
 *
 * Firma con la llave del gateway (~/.faronet/gateway.llave, la misma de
 * faronet/herramientas/gateway.py) o con la ruta de la variable GATEWAY_LLAVE.
 * Su llave PUBLICA debe ser la GATEWAY_PUBKEY del Worker. Sale con codigo 1 si algo falla.
 */
import { createHash, createPrivateKey, randomBytes, sign } from "node:crypto";
import { readFileSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";

const args = process.argv.slice(2);
const base = (args.find((a) => !a.startsWith("--")) ?? "http://localhost:8787").replace(/\/$/, "");
const conWs = !args.includes("--sin-ws");
const rutaLlave = process.env.GATEWAY_LLAVE ?? join(homedir(), ".faronet", "gateway.llave");

// La llave de nacl son 32 bytes (semilla); node la quiere en PKCS8.
const semilla = readFileSync(rutaLlave);
if (semilla.length !== 32) throw new Error(`${rutaLlave}: se esperaban 32 bytes`);
const llave = createPrivateKey({
	key: Buffer.concat([Buffer.from("302e020100300506032b657004220420", "hex"), semilla]),
	format: "der", type: "pkcs8",
});

let fallos = 0;
function paso(nombre, ok, detalle = "") {
	if (!ok) fallos++;
	console.log(`${ok ? "OK   " : "FALLA"} ${nombre}${!ok && detalle ? `  -> ${detalle}` : ""}`);
}

function peticionFirmada(ruta, datos) {
	const cuerpo = Buffer.from(JSON.stringify(datos));
	const ts = Math.floor(Date.now() / 1000);
	const hash = createHash("sha256").update(cuerpo).digest("hex");
	const firma = sign(null, Buffer.from(`POST\n${ruta}\n${ts}\n${hash}`), llave).toString("hex");
	return {
		method: "POST", body: cuerpo,
		headers: { "Content-Type": "application/json", "X-Faro-Ts": String(ts), "X-Faro-Firma": firma },
	};
}

function esperarMensaje(ws, texto, ms = 8000) {
	return new Promise((resolver, rechazar) => {
		const t = setTimeout(() => rechazar(new Error("el WebSocket no recibio el mensaje")), ms);
		ws.addEventListener("message", (e) => {
			if (JSON.parse(e.data).texto === texto) { clearTimeout(t); resolver(); }
		});
	});
}

async function main() {
	console.log(`Probando ${base}\n`);
	const tema = "humo";
	const texto = `humo-${Date.now()}`;

	let r = await fetch(base + "/");
	paso("pagina de inicio", r.status === 200 && r.headers.has("content-security-policy"), `HTTP ${r.status}`);

	r = await fetch(`${base}/gateway/publicar`, { method: "POST", body: "{}" });
	paso("el gateway sin firma recibe 401", r.status === 401, `HTTP ${r.status}`);

	// Sala en vivo: conectar el WebSocket ANTES de publicar.
	let ws = null, llegada = null;
	if (conWs) {
		try {
			ws = new WebSocket(base.replace(/^http/, "ws") + `/sala/${tema}/ws`);
			await new Promise((ok, no) => {
				ws.addEventListener("open", ok);
				ws.addEventListener("error", () => no(new Error("no abrio")));
				setTimeout(() => no(new Error("tiempo agotado")), 8000);
			});
			paso("WebSocket de la sala abre", true);
			llegada = esperarMensaje(ws, texto);
			llegada.catch(() => {});
		} catch (e) {
			paso("WebSocket de la sala abre", false, e.message);
			ws = null;
		}
	}

	const pub = peticionFirmada("/gateway/publicar", { tema, texto });
	r = await fetch(base + "/gateway/publicar", pub);
	paso("publicar firmado (204)", r.status === 204, `HTTP ${r.status}` + (r.status === 401 ? " (GATEWAY_PUBKEY no coincide con la llave local)" : ""));

	r = await fetch(base + "/gateway/publicar", pub);   // exactamente la misma peticion
	paso("repeticion rechazada (409)", r.status === 409, `HTTP ${r.status}`);

	if (ws) {
		try { await llegada; paso("el mensaje llega en vivo por WebSocket", true); }
		catch (e) { paso("el mensaje llega en vivo por WebSocket", false, e.message); }
		ws.close();
	}

	r = await fetch(`${base}/sala/${tema}/historial`);
	const hist = r.ok ? await r.json() : [];
	paso("el historial incluye el mensaje", hist.some((m) => m.texto === texto), `HTTP ${r.status}`);

	r = await fetch(`${base}/sala/${tema}/feed.atom`);
	const atom = await r.text();
	paso("feed Atom incluye el mensaje", r.status === 200 && /atom\+xml/.test(r.headers.get("content-type") ?? "") && atom.includes(texto), `HTTP ${r.status}`);

	// Buzon muerto: 1024 bytes, se borra al leer.
	const id = randomBytes(16).toString("hex");
	const blob = randomBytes(1024);
	r = await fetch(`${base}/buzon/${id}`, { method: "PUT", body: blob });
	paso("buzon: depositar (201)", r.status === 201, `HTTP ${r.status}`);
	r = await fetch(`${base}/buzon/${id}`, { method: "PUT", body: blob });
	paso("buzon: mismo id rechazado (409)", r.status === 409, `HTTP ${r.status}`);
	r = await fetch(`${base}/buzon/${randomBytes(16).toString("hex")}`, { method: "PUT", body: randomBytes(10) });
	paso("buzon: tamano distinto rechazado (400)", r.status === 400, `HTTP ${r.status}`);
	r = await fetch(`${base}/buzon/${id}`);
	const recogido = Buffer.from(await r.arrayBuffer());
	paso("buzon: recoger devuelve el mismo blob", r.status === 200 && recogido.equals(blob), `HTTP ${r.status}`);
	r = await fetch(`${base}/buzon/${id}`);
	paso("buzon: se borro al leerlo (404)", r.status === 404, `HTTP ${r.status}`);

	r = await fetch(`${base}/directorio`);
	const dir = r.ok ? await r.json() : null;
	paso("directorio publico (JSON con fichas)", r.status === 200 && Array.isArray(dir?.fichas), `HTTP ${r.status}`);

	console.log(fallos ? `\n${fallos} paso(s) fallaron` : "\nTodo bien");
	process.exit(fallos ? 1 : 0);
}

main().catch((e) => { console.error("No se pudo completar la prueba:", e.message); process.exit(1); });
