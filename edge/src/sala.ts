/**
 * sala.ts - Durable Object: una sala en tiempo real por tema.
 *
 * Guarda solo (ts, texto) en su SQLite, con TTL y tope de mensajes.
 * Nunca guarda IPs, encabezados ni identificadores. Los navegadores solo leen.
 * Usa la API de hibernacion de WebSockets para no gastar duracion en vano.
 */
import { DurableObject } from "cloudflare:workers";

const TTL_S = 24 * 3600;   // los mensajes viven 24 h
const MAX_MENSAJES = 200;

export class Sala extends DurableObject<Env> {
	constructor(ctx: DurableObjectState, env: Env) {
		super(ctx, env);
		ctx.blockConcurrencyWhile(async () => {
			ctx.storage.sql.exec(
				"CREATE TABLE IF NOT EXISTS mensajes (id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, texto TEXT NOT NULL)",
			);
		});
	}

	async fetch(request: Request): Promise<Response> {
		const ruta = new URL(request.url).pathname;

		if (ruta.endsWith("/ws")) {
			if (request.headers.get("Upgrade") !== "websocket") {
				return new Response("se esperaba WebSocket", { status: 426 });
			}
			const par = new WebSocketPair();
			const [cliente, servidor] = Object.values(par);
			this.ctx.acceptWebSocket(servidor);
			return new Response(null, { status: 101, webSocket: cliente });
		}

		if (ruta.endsWith("/publicar") && request.method === "POST") {
			const { texto } = await request.json<{ texto: string }>();
			const ahora = Math.floor(Date.now() / 1000);
			const sql = this.ctx.storage.sql;
			sql.exec("INSERT INTO mensajes (ts, texto) VALUES (?, ?)", ahora, texto);
			sql.exec("DELETE FROM mensajes WHERE ts < ?", ahora - TTL_S);
			sql.exec(
				"DELETE FROM mensajes WHERE id <= (SELECT id FROM mensajes ORDER BY id DESC LIMIT 1 OFFSET ?)",
				MAX_MENSAJES,
			);
			const salida = JSON.stringify({ ts: ahora, texto });
			for (const ws of this.ctx.getWebSockets()) {
				try { ws.send(salida); } catch { /* cliente ya cerrado */ }
			}
			return new Response(null, { status: 204 });
		}

		if (ruta.endsWith("/historial")) {
			const corte = Math.floor(Date.now() / 1000) - TTL_S;
			const filas = this.ctx.storage.sql
				.exec("SELECT ts, texto FROM mensajes WHERE ts >= ? ORDER BY id DESC LIMIT 50", corte)
				.toArray();
			return Response.json(filas.reverse(), { headers: { "Cache-Control": "no-store" } });
		}

		return new Response("no encontrado", { status: 404 });
	}

	// Sala de solo lectura: lo que escriba un navegador se ignora.
	webSocketMessage(_ws: WebSocket, _mensaje: string | ArrayBuffer): void {}

	webSocketClose(ws: WebSocket): void {
		try { ws.close(); } catch { /* ya cerrado */ }
	}
}