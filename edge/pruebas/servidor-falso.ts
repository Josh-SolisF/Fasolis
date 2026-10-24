/**
 * servidor-falso.ts - Expone el Worker con el entorno falso como un servidor HTTP local.
 * Sirve para ensayar humo.mjs sin wrangler (sin WebSockets: usar --sin-ws).
 *   npx tsx --require ./pruebas/registro.cjs pruebas/servidor-falso.ts [puerto]
 * Usa la llave del gateway de ~/.faronet/gateway.llave (o GATEWAY_LLAVE).
 */
import { createPrivateKey, createPublicKey } from "node:crypto";
import { readFileSync } from "node:fs";
import { createServer } from "node:http";
import { homedir } from "node:os";
import { join } from "node:path";
import worker from "../src/index";
import { Entorno } from "./entorno";

const semilla = readFileSync(process.env.GATEWAY_LLAVE ?? join(homedir(), ".faronet", "gateway.llave"));
const privada = createPrivateKey({
	key: Buffer.concat([Buffer.from("302e020100300506032b657004220420", "hex"), semilla]), format: "der", type: "pkcs8",
});
const spki = createPublicKey(privada).export({ format: "der", type: "spki" });
const entorno = new Entorno(Buffer.from(spki.subarray(spki.length - 32)).toString("hex"));

const puerto = Number(process.argv[2] ?? 8787);
createServer(async (req, res) => {
	const cuerpo: Buffer[] = [];
	for await (const c of req) cuerpo.push(c as Buffer);
	const metodo = req.method ?? "GET";
	const r = await worker.fetch(new Request(`http://localhost:${puerto}${req.url}`, {
		method: metodo, headers: req.headers as Record<string, string>,
		body: metodo === "GET" || metodo === "HEAD" ? undefined : Buffer.concat(cuerpo),
	}), entorno.env);
	res.writeHead(r.status, Object.fromEntries(r.headers));
	res.end(Buffer.from(await r.arrayBuffer()));
}).listen(puerto, () => console.log(`Worker falso en http://localhost:${puerto}`));
