// Sustituto minimo del modulo "cloudflare:workers" para correr el Worker en Node.
class DurableObject {
	constructor(ctx, env) {
		this.ctx = ctx;
		this.env = env;
	}
}
module.exports = { DurableObject };
