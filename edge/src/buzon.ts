/**
 * buzon.ts - Buzon muerto (dead drop): depositar y recoger blobs cifrados.
 *
 * El Worker NUNCA ve el contenido en claro: los clientes cifran antes de
 * depositar (ver faronet/herramientas/buzon.py). Reglas:
 *   - el identificador es opaco: lo deriva el cliente de un secreto compartido;
 *   - todos los blobs miden exactamente TAM_BLOB bytes (no se filtra el largo);
 *   - cada blob vive como maximo TTL_BUZON_S y se BORRA al ser leido.
 *
 * Almacenamiento: SQLite del Durable Object (misma familia que D1, sin
 * aprovisionar nada). El borrado al leer es un solo DELETE ... RETURNING, asi
 * que dos lectores simultaneos nunca reciben el mismo blob (con KV si podria).
 * Una alarma borra de verdad lo vencido aunque nadie vuelva a preguntar.
 */
import { DurableObject } from "cloudflare:workers";

export const TAM_BLOB = 1024;
export const TTL_BUZON_S = 24 * 3600;
const MAX_BLOBS = 2000;

const segundos = () => Math.floor(Date.now() / 1000);

export class Buzon extends DurableObject<Env> {
	constructor(ctx: DurableObjectState, env: Env) {
		super(ctx, env);
		ctx.blockConcurrencyWhile(async () => {
			ctx.storage.sql.exec(
				"CREATE TABLE IF NOT EXISTS buzon (id TEXT PRIMARY KEY, vence INTEGER NOT NULL, blob BLOB NOT NULL)",
			);
		});
	}

	/** "ok", "existe" (ese id ya tiene un blob vigente) o "lleno". */
	async depositar(id: string, blob: ArrayBuffer): Promise<"ok" | "existe" | "lleno"> {
		if (blob.byteLength !== TAM_BLOB) throw new Error("tamano de blob incorrecto");
		const sql = this.ctx.storage.sql;
		const ahora = segundos();
		sql.exec("DELETE FROM buzon WHERE vence <= ?", ahora);
		if (sql.exec("SELECT 1 AS x FROM buzon WHERE id = ?", id).toArray().length > 0) return "existe";
		const { n } = sql.exec<{ n: number }>("SELECT COUNT(*) AS n FROM buzon").one();
		if (n >= MAX_BLOBS) return "lleno";
		sql.exec("INSERT INTO buzon (id, vence, blob) VALUES (?, ?, ?)", id, ahora + TTL_BUZON_S, blob);
		await this.programarAlarma();
		return "ok";
	}

	/** Devuelve el blob y lo elimina; null si no existe o ya vencio. */
	async recoger(id: string): Promise<ArrayBuffer | null> {
		const sql = this.ctx.storage.sql;
		const ahora = segundos();
		sql.exec("DELETE FROM buzon WHERE vence <= ?", ahora);
		const filas = sql
			.exec<{ blob: ArrayBuffer }>("DELETE FROM buzon WHERE id = ? RETURNING blob", id)
			.toArray();
		return filas.length ? filas[0].blob : null;
	}

	async alarm(): Promise<void> {
		this.ctx.storage.sql.exec("DELETE FROM buzon WHERE vence <= ?", segundos());
		await this.programarAlarma(true);
	}

	/** La alarma salta cuando vence el blob mas viejo. */
	private async programarAlarma(forzar = false): Promise<void> {
		if (!forzar && (await this.ctx.storage.getAlarm()) !== null) return;
		const { m } = this.ctx.storage.sql
			.exec<{ m: number | null }>("SELECT MIN(vence) AS m FROM buzon")
			.one();
		if (m !== null) await this.ctx.storage.setAlarm(m * 1000 + 1000);
	}
}
