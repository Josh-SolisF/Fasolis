/**
 * edge.test.ts - Pruebas del Worker.   Ejecutar desde edge/:  npm run probar
 *
 * Corren el codigo real de src/ en Node con un entorno falso (ver entorno.ts).
 */
import assert from "node:assert/strict";
import { beforeEach, describe, it, mock } from "node:test";

import worker from "../src/index";
import { Entorno } from "./entorno";

const BASE = "https://edge.test";
const aHex = (b: ArrayBuffer | Uint8Array) => Buffer.from(b instanceof Uint8Array ? b : new Uint8Array(b)).toString("hex");

// ------------------------------------------------------------------ ayudas
let gateway: CryptoKeyPair;
let entorno: Entorno;

async function nuevoEntorno() {
	gateway = (await crypto.subtle.generateKey("Ed25519", true, ["sign", "verify"])) as CryptoKeyPair;
	entorno = new Entorno(aHex(await crypto.subtle.exportKey("raw", gateway.publicKey)));
}

async function pedir(ruta: string, init: RequestInit & { ip?: string } = {}) {
	const { ip, ...resto } = init;
	const headers = new Headers(resto.headers);
	if (ip) headers.set("CF-Connecting-IP", ip);
	return worker.fetch(new Request(BASE + ruta, { ...resto, headers }), entorno.env);
}

/** Peticion firmada como el gateway (mismo formato que faronet/herramientas/gateway.py). */
async function firmada(ruta: string, datos: unknown, opc: { ts?: number; llave?: CryptoKey } = {}) {
	const cuerpo = new TextEncoder().encode(JSON.stringify(datos));
	const ts = opc.ts ?? Math.floor(Date.now() / 1000);
	const hash = aHex(await crypto.subtle.digest("SHA-256", cuerpo));
	const firma = await crypto.subtle.sign("Ed25519", opc.llave ?? gateway.privateKey,
		new TextEncoder().encode(`POST\n${ruta}\n${ts}\n${hash}`));
	return pedir(ruta, {
		method: "POST", body: cuerpo,
		headers: { "X-Faro-Ts": String(ts), "X-Faro-Firma": aHex(firma), "Content-Type": "application/json" },
	});
}

async function crearFicha(nombre: string, o: { expira?: number; par?: CryptoKeyPair } = {}) {
	const par = o.par ?? ((await crypto.subtle.generateKey("Ed25519", true, ["sign", "verify"])) as CryptoKeyPair);
	const pub = new Uint8Array(await crypto.subtle.exportKey("raw", par.publicKey));
	const n = new TextEncoder().encode(nombre);
	const expira = o.expira ?? Math.floor(Date.now() / 1000) + 300;
	const cuerpo = new Uint8Array(2 + n.length + 64 + 8);
	cuerpo[0] = 1;
	cuerpo[1] = n.length;
	cuerpo.set(n, 2);
	cuerpo.set(pub, 2 + n.length);
	cuerpo.set(crypto.getRandomValues(new Uint8Array(32)), 2 + n.length + 32);
	new DataView(cuerpo.buffer).setBigUint64(2 + n.length + 64, BigInt(expira));
	const firma = new Uint8Array(await crypto.subtle.sign("Ed25519", par.privateKey, cuerpo));
	const ficha = new Uint8Array(cuerpo.length + 64);
	ficha.set(cuerpo);
	ficha.set(firma, cuerpo.length);
	return { hex: aHex(ficha), par, bytes: ficha };
}

const id = () => aHex(crypto.getRandomValues(new Uint8Array(16)));
const blob = (relleno = 7) => new Uint8Array(1024).fill(relleno);

beforeEach(nuevoEntorno);

// ----------------------------------------------------------------- gateway
describe("relay del gateway", () => {
	it("rechaza peticiones sin firma", async () => {
		const r = await pedir("/gateway/publicar", { method: "POST", body: '{"tema":"general","texto":"hola"}' });
		assert.equal(r.status, 401);
	});

	it("acepta una peticion firmada y el mensaje aparece en el historial", async () => {
		const r = await firmada("/gateway/publicar", { tema: "general", texto: "hola mundo" });
		assert.equal(r.status, 204);
		const h = (await (await pedir("/sala/general/historial")).json()) as { texto: string }[];
		assert.deepEqual(h.map((m) => m.texto), ["hola mundo"]);
	});

	it("rechaza una firma de otra llave", async () => {
		const otra = (await crypto.subtle.generateKey("Ed25519", true, ["sign", "verify"])) as CryptoKeyPair;
		const r = await firmada("/gateway/publicar", { tema: "general", texto: "x" }, { llave: otra.privateKey });
		assert.equal(r.status, 401);
	});

	it("rechaza una marca de tiempo vieja", async () => {
		const r = await firmada("/gateway/publicar", { tema: "general", texto: "x" },
			{ ts: Math.floor(Date.now() / 1000) - 120 });
		assert.equal(r.status, 401);
	});

	it("rechaza la repeticion exacta de una peticion (replay)", async () => {
		const datos = { tema: "general", texto: "una vez" };
		const ts = Math.floor(Date.now() / 1000);
		assert.equal((await firmada("/gateway/publicar", datos, { ts })).status, 204);
		assert.equal((await firmada("/gateway/publicar", datos, { ts })).status, 409);
		const h = (await (await pedir("/sala/general/historial")).json()) as unknown[];
		assert.equal(h.length, 1);
	});

	it("valida tema y texto", async () => {
		assert.equal((await firmada("/gateway/publicar", { tema: "Mal Tema!", texto: "x" })).status, 400);
		assert.equal((await firmada("/gateway/publicar", { tema: "general", texto: "" })).status, 400);
		assert.equal((await firmada("/gateway/publicar", { tema: "general", texto: "x".repeat(301) })).status, 400);
	});

	it("rechaza cuerpos demasiado grandes y metodos incorrectos", async () => {
		const grande = await pedir("/gateway/publicar", { method: "POST", body: "x".repeat(3000) });
		assert.equal(grande.status, 413);
		assert.equal((await pedir("/gateway/publicar")).status, 405);
	});

	it("la ruta firmada es parte de la firma (no sirve en otra ruta)", async () => {
		const cuerpo = new TextEncoder().encode('{"tema":"general","texto":"x"}');
		const ts = Math.floor(Date.now() / 1000);
		const hash = aHex(await crypto.subtle.digest("SHA-256", cuerpo));
		const firma = await crypto.subtle.sign("Ed25519", gateway.privateKey,
			new TextEncoder().encode(`POST\n/gateway/directorio\n${ts}\n${hash}`));
		const r = await pedir("/gateway/publicar", {
			method: "POST", body: cuerpo, headers: { "X-Faro-Ts": String(ts), "X-Faro-Firma": aHex(firma) },
		});
		assert.equal(r.status, 401);
	});
});

// -------------------------------------------------------------------- salas
describe("salas y feed", () => {
	it("la pagina de la sala anuncia el feed y no pone cookies", async () => {
		const r = await pedir("/sala/general");
		const html = await r.text();
		assert.equal(r.status, 200);
		assert.match(html, /application\/atom\+xml/);
		assert.equal(r.headers.get("Set-Cookie"), null);
	});

	it("rechaza temas invalidos", async () => {
		assert.equal((await pedir("/sala/../etc")).status, 404);
		assert.equal((await pedir("/sala/TEMA_MUY_LARGO_QUE_NO_CABE")).status, 400);
	});

	it("el feed Atom lista los mensajes, del mas nuevo al mas viejo", async () => {
		await firmada("/gateway/publicar", { tema: "noticias", texto: "primero" });
		await firmada("/gateway/publicar", { tema: "noticias", texto: "segundo" });
		const r = await pedir("/sala/noticias/feed.atom");
		const xml = await r.text();
		assert.equal(r.status, 200);
		assert.match(r.headers.get("Content-Type") ?? "", /application\/atom\+xml/);
		assert.match(xml, /<feed xmlns="http:\/\/www\.w3\.org\/2005\/Atom">/);
		assert.ok(xml.indexOf("segundo") < xml.indexOf("primero"));
		assert.equal((xml.match(/<entry>/g) ?? []).length, 2);
		assert.match(xml, /<author><name>an\u00f3nimo<\/name><\/author>/);
	});

	it("el feed escapa el XML (no se puede inyectar etiquetas)", async () => {
		await firmada("/gateway/publicar", { tema: "xss", texto: '<script>alert("x")</script> & <b>' });
		const xml = await (await pedir("/sala/xss/feed.atom")).text();
		assert.ok(!xml.includes("<script>"));
		assert.match(xml, /&lt;script&gt;/);
		assert.match(xml, /&amp;/);
	});

	it("un tema sin mensajes da un feed vacio pero valido", async () => {
		const xml = await (await pedir("/sala/vacio/feed.atom")).text();
		assert.match(xml, /<feed /);
		assert.ok(!xml.includes("<entry>"));
	});

	it("los mensajes vencen: la alarma los borra de verdad", async () => {
		mock.timers.enable({ apis: ["Date"] });
		try {
			await firmada("/gateway/publicar", { tema: "general", texto: "efimero" });
			assert.match(entorno.volcado(), /efimero/);
			mock.timers.tick(25 * 3600 * 1000);
			await entorno.instancia("SALA", "general").alarm();
			assert.ok(!entorno.volcado().includes("efimero"));
			const h = (await (await pedir("/sala/general/historial")).json()) as unknown[];
			assert.equal(h.length, 0);
		} finally {
			mock.timers.reset();
		}
	});
});

// -------------------------------------------------------------------- buzon
describe("buzon muerto", () => {
	it("deposita, recoge y borra al leer", async () => {
		const k = id();
		const b = blob(42);
		assert.equal((await pedir(`/buzon/${k}`, { method: "PUT", body: b })).status, 201);
		const r = await pedir(`/buzon/${k}`);
		assert.equal(r.status, 200);
		assert.deepEqual(new Uint8Array(await r.arrayBuffer()), b);
		assert.equal((await pedir(`/buzon/${k}`)).status, 404);   // ya no esta
	});

	it("no deja sobrescribir un id vigente", async () => {
		const k = id();
		await pedir(`/buzon/${k}`, { method: "PUT", body: blob(1) });
		assert.equal((await pedir(`/buzon/${k}`, { method: "PUT", body: blob(2) })).status, 409);
		const r = await pedir(`/buzon/${k}`);
		assert.deepEqual(new Uint8Array(await r.arrayBuffer()), blob(1));
	});

	it("exige tamano fijo e id opaco valido", async () => {
		assert.equal((await pedir(`/buzon/${id()}`, { method: "PUT", body: new Uint8Array(100) })).status, 400);
		assert.equal((await pedir(`/buzon/${id()}`, { method: "PUT", body: new Uint8Array(5000) })).status, 413);
		assert.equal((await pedir("/buzon/no-es-hex", { method: "PUT", body: blob() })).status, 400);
		assert.equal((await pedir(`/buzon/${id()}`, { method: "DELETE" })).status, 405);
	});

	it("un id que no existe da 404, igual que uno ya recogido", async () => {
		assert.equal((await pedir(`/buzon/${id()}`)).status, 404);
	});

	it("dos lectores simultaneos no reciben el mismo blob", async () => {
		const k = id();
		await pedir(`/buzon/${k}`, { method: "PUT", body: blob(9) });
		const [a, b] = await Promise.all([pedir(`/buzon/${k}`), pedir(`/buzon/${k}`)]);
		assert.deepEqual([a.status, b.status].sort(), [200, 404]);
	});

	it("el blob vence con su TTL y la alarma lo borra", async () => {
		mock.timers.enable({ apis: ["Date"] });
		try {
			const k = id();
			await pedir(`/buzon/${k}`, { method: "PUT", body: blob(5) });
			mock.timers.tick(25 * 3600 * 1000);
			await entorno.instancia("BUZON", "global").alarm();
			assert.ok(!entorno.volcado().includes(k));
			assert.equal((await pedir(`/buzon/${k}`)).status, 404);
		} finally {
			mock.timers.reset();
		}
	});
});

// --------------------------------------------------------------- directorio
describe("directorio publico", () => {
	const empujar = (fichas: string[]) => firmada("/gateway/directorio", { fichas });

	it("publica fichas con firma valida y las expone con la ficha cruda", async () => {
		const f = await crearFicha("alfa");
		const r = await empujar([f.hex]);
		assert.equal(r.status, 200);
		assert.deepEqual(await r.json(), { aceptadas: 1, rechazadas: [] });

		const lista = (await (await pedir("/directorio")).json()) as { fichas: any[] };
		assert.equal(lista.fichas.length, 1);
		assert.equal(lista.fichas[0].nombre, "alfa");
		assert.equal(lista.fichas[0].ficha, f.hex);
		assert.equal(lista.fichas[0].llave_firma, aHex(await crypto.subtle.exportKey("raw", f.par.publicKey)));
	});

	it("cualquiera puede re-verificar la firma de lo que publica el edge", async () => {
		const f = await crearFicha("beta");
		await empujar([f.hex]);
		const { fichas } = (await (await pedir("/directorio")).json()) as { fichas: { ficha: string; llave_firma: string }[] };
		const crudo = Buffer.from(fichas[0].ficha, "hex");
		const llave = await crypto.subtle.importKey("raw", Buffer.from(fichas[0].llave_firma, "hex"), "Ed25519", false, ["verify"]);
		assert.ok(await crypto.subtle.verify("Ed25519", llave, crudo.subarray(crudo.length - 64), crudo.subarray(0, crudo.length - 64)));
	});

	it("el gateway no puede alterar ni inventar fichas", async () => {
		const f = await crearFicha("gama");
		const mala = f.bytes.slice();
		mala[mala.length - 1] ^= 1;             // firma alterada
		const alterada = f.bytes.slice();
		alterada[5] ^= 1;                        // nombre alterado
		const r = (await (await empujar([aHex(mala), aHex(alterada), "zz", "00"])).json()) as { aceptadas: number; rechazadas: string[] };
		assert.equal(r.aceptadas, 0);
		assert.equal(r.rechazadas.length, 4);
		assert.equal(((await (await pedir("/directorio")).json()) as { fichas: unknown[] }).fichas.length, 0);
	});

	it("rechaza fichas vencidas o con vencimiento demasiado lejano", async () => {
		const ahora = Math.floor(Date.now() / 1000);
		const vieja = await crearFicha("vieja", { expira: ahora - 10 });
		const lejana = await crearFicha("lejana", { expira: ahora + 86400 });
		const r = (await (await empujar([vieja.hex, lejana.hex])).json()) as { rechazadas: string[] };
		assert.deepEqual(r.rechazadas, ["ficha vencida", "vencimiento demasiado lejano"]);
	});

	it("un nombre no puede cambiar de llave mientras su ficha viva", async () => {
		await empujar([(await crearFicha("delta")).hex]);
		const impostor = await crearFicha("delta");
		const r = (await (await empujar([impostor.hex])).json()) as { aceptadas: number; rechazadas: string[] };
		assert.equal(r.aceptadas, 0);
		assert.match(r.rechazadas[0], /otra llave/);
	});

	it("acepta renovar y rechaza una ficha mas vieja que la registrada", async () => {
		const ahora = Math.floor(Date.now() / 1000);
		const par = ((await crypto.subtle.generateKey("Ed25519", true, ["sign", "verify"])) as CryptoKeyPair);
		const nueva = await crearFicha("eps", { par, expira: ahora + 300 });
		const vieja = await crearFicha("eps", { par, expira: ahora + 100 });
		assert.equal(((await (await empujar([nueva.hex])).json()) as { aceptadas: number }).aceptadas, 1);
		const r = (await (await empujar([vieja.hex])).json()) as { rechazadas: string[] };
		assert.match(r.rechazadas[0], /mas vieja/);
	});

	it("consulta una ficha por nombre", async () => {
		await empujar([(await crearFicha("zeta")).hex]);
		const r = await pedir("/directorio/zeta");
		assert.equal(r.status, 200);
		assert.equal(((await r.json()) as { nombre: string }).nombre, "zeta");
		assert.equal((await pedir("/directorio/nadie")).status, 404);
		assert.equal(r.headers.get("Access-Control-Allow-Origin"), "*");
	});

	it("es de solo lectura para el publico: publicar exige la firma del gateway", async () => {
		const f = await crearFicha("intruso");
		const r = await pedir("/gateway/directorio", { method: "POST", body: JSON.stringify({ fichas: [f.hex] }) });
		assert.equal(r.status, 401);
		assert.equal((await pedir("/directorio", { method: "POST", body: "{}" })).status, 405);
	});

	it("las fichas vencidas desaparecen del directorio y de la base", async () => {
		mock.timers.enable({ apis: ["Date"] });
		try {
			await empujar([(await crearFicha("breve")).hex]);
			mock.timers.tick(10 * 60 * 1000);
			await entorno.instancia("DIRECTORIO", "global").alarm();
			assert.ok(!entorno.volcado().includes("breve"));
			assert.equal(((await (await pedir("/directorio")).json()) as { fichas: unknown[] }).fichas.length, 0);
		} finally {
			mock.timers.reset();
		}
	});

	it("rechaza envios con demasiadas fichas o mal formados", async () => {
		assert.equal((await empujar(Array(65).fill("00"))).status, 400);
		assert.equal((await firmada("/gateway/directorio", { fichas: "no" })).status, 400);
	});
});

// ------------------------------------------------------------- rate limiting
describe("limite de peticiones", () => {
	it("corta con 429 a quien se pasa, y no afecta a otros clientes", async () => {
		const estados: number[] = [];
		for (let i = 0; i < 35; i++) {
			estados.push((await pedir(`/buzon/${id()}`, { ip: "198.51.100.1" })).status);
		}
		assert.equal(estados.filter((s) => s === 429).length, 5);       // limite 30 por minuto
		const otro = await pedir(`/buzon/${id()}`, { ip: "198.51.100.2" });
		assert.equal(otro.status, 404);                                  // pasa el limite (no hay nada)
		const bloqueado = await pedir(`/buzon/${id()}`, { ip: "198.51.100.1" });
		assert.equal(bloqueado.status, 429);
		assert.equal(bloqueado.headers.get("Retry-After"), "60");
	});

	it("la ventana se reinicia", async () => {
		mock.timers.enable({ apis: ["Date"] });
		try {
			for (let i = 0; i < 31; i++) await pedir(`/buzon/${id()}`, { ip: "198.51.100.3" });
			assert.equal((await pedir(`/buzon/${id()}`, { ip: "198.51.100.3" })).status, 429);
			mock.timers.tick(61_000);
			assert.equal((await pedir(`/buzon/${id()}`, { ip: "198.51.100.3" })).status, 404);
		} finally {
			mock.timers.reset();
		}
	});

	it("los intentos con firma mala tambien cuentan (y no bloquean al gateway legitimo)", async () => {
		for (let i = 0; i < 125; i++) {
			await pedir("/gateway/publicar", { method: "POST", body: "{}", ip: "203.0.113.50" });
		}
		const atacante = await pedir("/gateway/publicar", { method: "POST", body: "{}", ip: "203.0.113.50" });
		assert.equal(atacante.status, 429);
		const legitimo = await firmada("/gateway/publicar", { tema: "general", texto: "sigo vivo" });
		assert.equal(legitimo.status, 204);   // otra IP (aqui "local"), otro cubo
	});
});

// ---------------------------------------------------------------- privacidad
describe("politica de no registro", () => {
	it("ninguna base guarda IP, User-Agent ni encabezados de las peticiones", async () => {
		const ip = "203.0.113.9";
		const ua = "navegador-secreto/1.0";
		const k = id();
		await pedir("/sala/general", { ip, headers: { "User-Agent": ua } });
		await firmada("/gateway/publicar", { tema: "general", texto: "mensaje" });
		await pedir(`/buzon/${k}`, { method: "PUT", body: blob(), ip, headers: { "User-Agent": ua } });
		await pedir(`/buzon/${k}`, { ip, headers: { "User-Agent": ua } });
		await pedir("/directorio", { ip, headers: { "User-Agent": ua } });
		await firmada("/gateway/directorio", { fichas: [(await crearFicha("nodo1")).hex] });
		const todo = entorno.volcado();
		assert.ok(todo.includes("mensaje"));       // la prueba si ve lo que se guarda
		assert.ok(!todo.includes(ip));
		assert.ok(!todo.includes(ua));
		assert.ok(!/user-agent|cf-connecting-ip/i.test(todo));
	});

	it("las respuestas no ponen cookies y piden no cachear ni enviar referer", async () => {
		const r = await pedir("/");
		assert.equal(r.headers.get("Set-Cookie"), null);
		assert.equal(r.headers.get("Cache-Control"), "no-store");
		assert.equal(r.headers.get("Referrer-Policy"), "no-referrer");
	});
});

// ------------------------------------------------------------ paginas HTML
describe("paginas HTML", () => {
	it("la pagina de inicio responde y solo permite GET/HEAD", async () => {
		const r = await pedir("/");
		assert.equal(r.status, 200);
		assert.match(r.headers.get("Content-Type") ?? "", /text\/html/);
		assert.equal((await pedir("/", { method: "POST" })).status, 405);
	});

	it("las paginas llevan una CSP con nonce y el script usa ese mismo nonce", async () => {
		for (const ruta of ["/", "/sala/general"]) {
			const r = await pedir(ruta);
			const csp = r.headers.get("Content-Security-Policy") ?? "";
			assert.match(csp, /default-src 'none'/);
			assert.match(csp, /frame-ancestors 'none'/);
			const nonce = /'nonce-([^']+)'/.exec(csp)?.[1];
			assert.ok(nonce, "la CSP debe traer un nonce");
			assert.ok((await r.text()).includes(`<script nonce="${nonce}">`));
		}
	});

	it("el nonce cambia en cada peticion", async () => {
		const n = async () => /'nonce-([^']+)'/.exec((await pedir("/sala/general")).headers.get("Content-Security-Policy") ?? "")?.[1];
		assert.notEqual(await n(), await n());
	});
});
