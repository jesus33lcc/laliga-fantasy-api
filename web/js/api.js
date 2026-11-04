/* ============================================================================
   api.js: la única puerta a los datos.

   Dos modos, misma interfaz:
     · **vivo**: servido por la propia FastAPI, así que las rutas son
                   relativas y no hay CORS ni claves en el navegador.
     · **demo**: si existe `window.DATOS_DEMO` (la maqueta empaquetada en un
                   solo fichero), lee de ahí. Sirve para enseñar la interfaz sin
                   VPN y para trabajar en el diseño sin gastar peticiones.

   Ninguna vista hace `fetch` por su cuenta: si algo no está aquí, no se pide.
   ========================================================================== */

const DEMO = typeof window !== 'undefined' && window.DATOS_DEMO;

/** Caché por ruta dentro de la sesión de navegador. La API ya cachea de verdad;
    esto solo evita repetir la misma consulta al cambiar de pestaña. */
const cache = new Map();

export const modo = () => (DEMO ? 'demo' : 'vivo');

async function pedir(ruta, { refrescar = false } = {}) {
  if (DEMO) {
    const d = window.DATOS_DEMO[ruta];
    if (d === undefined) throw new Error(`la maqueta no trae datos para ${ruta}`);
    return structuredClone(d);
  }
  if (!refrescar && cache.has(ruta)) return structuredClone(cache.get(ruta));
  /* La web la sirve la propia API bajo /app, así que la ruta absoluta desde la
     raíz da en el mismo origen: sin CORS, sin claves en el navegador. */
  const r = await fetch(ruta, { headers: { accept: 'application/json' } });
  if (!r.ok) {
    let detalle = `HTTP ${r.status}`;
    try { detalle = (await r.json()).detail || detalle; } catch { /* cuerpo no JSON */ }
    throw new Error(detalle);
  }
  const d = await r.json();
  cache.set(ruta, d);
  return structuredClone(d);
}

export const invalidar = () => cache.clear();

/** Llama a una ruta GET cualquiera de la API propia y devuelve la respuesta cruda.
 *
 * Detrás del botón "Probar" del catálogo. Va aparte de `pedir()` porque no cachea (una
 * copia de hace diez minutos no prueba nada), no lanza ante un status de error (un 503 o
 * un 403 de negocio son justo lo que se quiere ver) y mide el tiempo, que con un límite
 * de 0,5 req/s es media respuesta.
 *
 * Solo GET y solo rutas del catálogo: una ruta libre desde el navegador sería llamar a
 * cualquier cosa saltándose las pantallas que confirman un gasto.
 */
export async function sondear(ruta) {
  const reloj = typeof performance !== 'undefined' ? performance : Date;
  const t0 = reloj.now();
  const ms = () => Math.round(reloj.now() - t0);
  if (DEMO) {
    const d = window.DATOS_DEMO[ruta];
    return { ruta, demo: true, ms: ms(), status: d === undefined ? 404 : 200,
             cuerpo: d === undefined ? { detail: 'la maqueta no trae esta ruta' } : d };
  }
  try {
    const r = await fetch(ruta, { headers: { accept: 'application/json' } });
    const texto = await r.text();
    let cuerpo;
    try { cuerpo = JSON.parse(texto); } catch { cuerpo = texto; }
    return { ruta, status: r.status, ms: ms(), cuerpo };
  } catch (e) {
    // Ni siquiera responde: sin red, sin VPN, o la API caída. Es distinto de
    // un error HTTP y se enseña distinto.
    return { ruta, status: 0, ms: ms(), error: e.message };
  }
}

/* ---------- lecturas ---------- */

export const maestro     = (o) => pedir('/fantasy/players', o);
export const estado      = (o) => pedir('/estado', o);
export const informe     = (o) => pedir('/informe', o);
export const hoy         = (o) => pedir('/informe/hoy', o);
export const trabajos    = (o) => pedir('/trabajos', o);
export const guardarConfigJob = (n, cambios) => enviar(`/trabajos/config/${n}`, cambios, 'PUT');
export const ventanas    = (o) => pedir('/informe/ventanas-liga', o);
export const momentum    = (o) => pedir('/informe/momentum', o);
export const saldos      = (o) => pedir('/informe/saldos', o);
export const tradingOportunidades = (o) => pedir('/trading/oportunidades', o);
export const tradingCartera       = (o) => pedir('/trading/cartera', o);
export const tradingBalance       = (o) => pedir('/trading/balance', o);
export const tacticas          = (o) => pedir('/tacticas', o);
export const tacticaEscaparate = (o) => pedir('/tacticas/escaparate', o);
export const tacticaClausulas  = (m, o) => pedir(`/tacticas/clausulas-baratas?modo=${m || 'disuasorio'}`, o);
export const tacticaRivales    = (o) => pedir('/tacticas/rivales', o);
export const calendario2 = (o) => pedir('/informe/calendario-dificultad', o);
export const backtest    = (o) => pedir('/informe/backtest', o);
export const premium     = (o) => pedir('/informe/premium', o);
export const misOfertas  = (o) => pedir('/informe/mis-ofertas', o);
export const ventas      = (o) => pedir('/ventas', o);
export const guardarVenta = (jug, pol) => enviar(`/ventas/${encodeURIComponent(jug)}`, pol, 'PUT');
export const histMercado = (o) => pedir('/informe/mercado-historico', o);
export const enVivo      = (o) => pedir('/informe/en-vivo', o);
export const clasifJor   = (n, o) => pedir(`/informe/clasificacion/${n}`, o);
export const jornadaN    = (n, o) => pedir(`/informe/jornada?numero=${n}`, o);
export const optimo      = (n, o) => pedir(`/informe/optimo/${n}`, o);
export const jugador     = (id, o) => pedir(`/informe/jugador/${id}`, o);
export const plantillaDe = (id, j, o) => pedir(`/informe/equipo/${id}${j ? `?jornada=${j}` : ''}`, o);
export const seguimientos = (o) => pedir('/seguimiento', o);
export const mercado     = (o) => pedir('/fantasy/market', o);
export const alineacion  = (o) => pedir('/fantasy/lineup', o);
export const actividad   = (o) => pedir('/fantasy/activity', o);
export const jornada     = (o) => pedir('/fantasy/week/current', o);
export const calendario  = (n, o) => pedir(`/fantasy/calendar/${n}`, o);
export const tareas      = (o) => pedir('/programar', o);
export const catalogo    = (o) => pedir('/openapi.json', o);

/* ---------- escrituras ----------
   Las que gastan (`simular`, `actuar`, `programar`) mandan `confirmar: true` y pasan por
   los frenos de `politica.py` en el servidor. El resto son configuración y estado propio.
   El navegador NO decide: solo propone. */

/** Identificador único para `Idempotency-Key`.
 *
 * No usar `crypto.randomUUID()` a secas: solo existe en contexto seguro (HTTPS o
 * localhost), y Coconut se sirve por HTTP, así que ahí rompe todas las escrituras del
 * navegador.
 *
 * `getRandomValues` sí está disponible fuera de contexto seguro, y es lo que se usa. El
 * `Math.random` final es solo para que nunca falte la cabecera. */
export function uuid() {
  if (globalThis.crypto?.randomUUID) return globalThis.crypto.randomUUID();
  const b = new Uint8Array(16);
  if (globalThis.crypto?.getRandomValues) globalThis.crypto.getRandomValues(b);
  else for (let i = 0; i < 16; i++) b[i] = Math.floor(Math.random() * 256);
  b[6] = (b[6] & 0x0f) | 0x40;      // versión 4
  b[8] = (b[8] & 0x3f) | 0x80;      // variante RFC 4122
  const h = [...b].map((x) => x.toString(16).padStart(2, '0')).join('');
  return `${h.slice(0, 8)}-${h.slice(8, 12)}-${h.slice(12, 16)}-${h.slice(16, 20)}-${h.slice(20)}`;
}

async function enviar(ruta, cuerpo, metodo = 'POST') {
  if (DEMO) throw new Error('la maqueta no ejecuta acciones reales: esto es una foto de los datos');
  const r = await fetch(ruta, {
    method: metodo,
    headers: { 'content-type': 'application/json', 'idempotency-key': uuid() },
    body: cuerpo === null ? undefined : JSON.stringify(cuerpo),
  });
  const d = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(d.detail || `HTTP ${r.status}`);
  invalidar();
  return d;
}

/* ---------- alineación ----------
   La única escritura que no pregunta importe: no mueve dinero, así que se salta el
   diálogo de `acciones.js` y la pregunta de cómo se autoriza un gasto desde aquí. El
   freno del servidor ya distingue los dos casos (`Acciones._escribir`), igual que para
   el bot.

   El payload va TAL CUAL, sin campos añadidos: `guardar_alineacion` lo reenvía a
   LaLiga y un campo de más rompe la petición. `coach` y `bench` la rompen. */

export const guardarOnce = (payload) => enviar('/accion/alineacion', payload);
export const recalcular  = () => enviar('/accion/alineacion?auto=true', null);

/** Fijar es obligatorio tras guardar a mano: si no, el cron de `job_alineacion`
    recalcula y borra la decisión sin avisar a nadie. */
export const fijarOnce = (payload, jornada, motivo) =>
  enviar(`/accion/fijar-once?jornada=${jornada}&motivo=${encodeURIComponent(motivo)}`, payload);

/* ---------- las que mueven dinero ----------
   Entran por el diálogo de `acciones.js`, que es quien enseña el importe y la caja
   antes y después. */

/** El `dry` lo usa el clausulazo, que relee el importe exacto al euro antes de
    enseñarlo: un céntimo de más da 409 `030.01.49`. Las demás se confirman con el
    valor que ya está en pantalla. */
export const simular = (accion, params) => enviar(`/accion/${accion}?dry=true`, { ...params, confirmar: true });
export const actuar  = (accion, params) => enviar(`/accion/${accion}`, { ...params, confirmar: true });

/** La otra mitad del diálogo: en vez de ejecutar, se deja en cola. El endpoint
    es OTRO, y por eso la elección de «cuándo» no es cosmética. */
export const programar = (accion, params, cuando, nota, autorenovar = false) =>
  enviar('/programar', { accion, params: { ...params, confirmar: true }, cuando, nota,
                         autorenovar });

export const cancelarTarea = (id) => enviar(`/programar/${id}`, null, 'DELETE');

/* El worker no tiene servidor HTTP: lanzar deja una petición en la tabla y él la
   recoge en ≤20 s. Por eso la respuesta es «pendiente», no el resultado. */
export const lanzarJob = (n) => enviar(`/trabajos/${n}/lanzar`, null);
export const cancelarSeguimiento = (id) => enviar(`/seguimiento/${id}`, null, 'DELETE');

/* ---------- candado del once ----------
   Con el candado echado, ni el bot recalcula ni se puede vender a un titular. */

export const candado = () => pedir('/bloqueo', { refrescar: true });
export const bloquearOnce = (motivo) => enviar('/bloqueo/once', { motivo });
export const desbloquearOnce = () => enviar('/bloqueo/once', null, 'DELETE');

/* ---------- búsquedas del mercado ---------- */

export const buscarMercado = (q) =>
  pedir('/mercado/buscar?' + new URLSearchParams(
    Object.entries(q).filter(([, v]) => v !== '' && v != null)), { refrescar: true });
export const avisosMercado = () => pedir('/mercado/avisos', { refrescar: true });
export const crearAviso = (f) => enviar('/mercado/avisos', f);
export const borrarAviso = (n) => enviar(`/mercado/avisos/${encodeURIComponent(n)}`, null, 'DELETE');

/* ---------- ajustes ---------- */

export const ajustes = () => pedir('/ajustes', { refrescar: true });
export const guardarAjustes = (cuerpo) => enviar('/ajustes', cuerpo, 'PUT');

// Conexión con LaLiga (login Google/PKCE). `loginUrl` y `loginEstado` son lecturas, pero
// no se cachean: el estado cambia justo al conectar y una foto vieja confundiría.
export const loginEstado = () => pedir('/auth/laliga/estado', { refrescar: true });
export const loginUrl    = () => pedir('/auth/laliga/url', { refrescar: true });
export const loginCode   = (pegado) => enviar('/auth/laliga/code', { pegado });
export const probarTelegram = (telegram_token, telegram_chat_id) =>
  enviar('/ajustes/probar', { telegram_token, telegram_chat_id });
export const probarOdds = (odds_api_key) => enviar('/ajustes/probar-odds', { odds_api_key });

/* ---------- sesión ----------
   El token nunca pasa por aquí: va en una cookie HttpOnly que el navegador adjunta
   solo. Estas funciones solo abren y cierran esa cookie. */

export const yo = () => (DEMO
  ? Promise.resolve({ auth_activa: false, usuario: null, google: false })
  : fetch('/auth/yo').then((r) => r.json()));

export const login = (email, clave) => enviar('/auth/login', { email, clave });
export const logout = () => enviar('/auth/logout', null);
export const googleInicio = () => pedir('/auth/google/inicio', { refrescar: true });

/* ---------- copia de seguridad ---------- */

export const respaldoProbar = (cuerpo) => enviar('/respaldo/probar', cuerpo || {});
export const respaldoCopiar = () => enviar('/respaldo/copiar', null);
/* `reemplazar` va por query y por defecto no: restaurar solo AÑADE lo que falte. El
   momento de restaurar es el peor para descubrir que has sustituido algo bueno por algo
   viejo, así que pisar hay que pedirlo. */
export const respaldoRestaurar = (reemplazar = false) =>
  enviar(`/respaldo/restaurar?reemplazar=${reemplazar ? 'true' : 'false'}`, null);
export const mandoLeer    = (o) => pedir('/respaldo/mando', o);
export const mandoTomar   = (forzar = false) => enviar(`/respaldo/mando?forzar=${forzar ? 'true' : 'false'}`, null);
export const mandoSoltar  = () => enviar('/respaldo/mando', null, 'DELETE');
