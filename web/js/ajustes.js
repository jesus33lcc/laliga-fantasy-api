/* ============================================================================
   ajustes.js: la pantalla de Ajustes: bot de Telegram y qué avisos llegan.

   Se parece a las notificaciones del móvil a propósito: una lista de
   interruptores con el nombre de lo que avisa y una línea de por qué importa. La
   separación es **urgente / lo demás**, no por módulo: al usuario le da igual que
   un aviso lo mande `vigilante.py` y otro `piloto.py`.

   El token nunca vuelve del servidor: solo llega una pista («…8GOU»). El campo va
   vacío y **dejarlo vacío significa "no lo toques"**, que es lo que se quiere el
   99 % de las veces que se entra aquí a cambiar una casilla.
   ========================================================================== */

import * as U from './ui.js';
import * as api from './api.js';

export const estadoAj = { datos: null, error: '', aviso: '', ocupado: false,
                          sucio: false, cargando: false,
                          /* Quién opera. Se lee junto a los ajustes; si falla se queda
                             a `null` con el motivo en `mandoError`, porque un candado
                             ilegible no puede romper la pantalla donde se arregla. */
                          mando: null, mandoError: '' };

/* Conexión con LaLiga (login Google/PKCE). Estado propio: es un flujo de dos pasos con
   una espera larga en medio, así que necesita su `paso` y su `conectando` para pintar el
   círculo de carga sin bloquear el resto de Ajustes. */
export const estadoLogin = {
  info: null,          // respuesta de /auth/laliga/estado
  paso: 0,             // 0 = nada · 1 = URL abierta, esperando el pegado
  conectando: false,   // mientras se canjea el code: círculo «puede tardar»
  error: '', aviso: '',
};

export async function cargarLogin() {
  try {
    estadoLogin.info = await api.loginEstado();
  } catch (e) {
    estadoLogin.error = e.message;
  }
}

/* Paso 1: pedir la URL y abrirla en pestaña nueva. Se abre desde aquí (no con un <a>)
   porque hay que pedirla antes: la URL lleva el `state` que el servidor acaba de guardar. */
export async function loginAbrir(repintar) {
  estadoLogin.error = ''; estadoLogin.aviso = '';
  try {
    const d = await api.loginUrl();
    window.open(d.url, '_blank', 'noopener');
    estadoLogin.paso = 1;
  } catch (e) {
    estadoLogin.error = e.message;
  }
  repintar();
}

/* Paso 2: canjear lo pegado. Aquí vive la espera larga: `conectando` enciende el
   círculo, y pase lo que pase se apaga en el `finally`. */
export async function loginConectar(pegado, repintar) {
  if (!pegado.trim()) { estadoLogin.error = 'Pega la URL o el código primero.'; return repintar(); }
  estadoLogin.conectando = true; estadoLogin.error = ''; estadoLogin.aviso = '';
  repintar();
  try {
    const d = await api.loginCode(pegado);
    estadoLogin.aviso = d.aviso
      ? d.aviso
      : `✅ Conectado. El token se renueva solo (~90 días).`;
    estadoLogin.paso = 0;
    await cargarLogin();
  } catch (e) {
    estadoLogin.error = e.message;
  } finally {
    estadoLogin.conectando = false;
    repintar();
  }
}

export async function cargarAj() {
  if (estadoAj.cargando) return;
  estadoAj.cargando = true;
  estadoAj.error = '';
  try {
    estadoAj.datos = await api.ajustes();
    estadoAj.sucio = false;
    /* Tolerante a propósito: si el mando no se puede leer (GitHub caído, token mal) los
       ajustes se siguen viendo. Un candado que rompe la pantalla de configuración deja
       sin poder arreglar justo lo que lo ha roto. */
    try { estadoAj.mando = await api.mandoLeer({ refrescar: true }); estadoAj.mandoError = ''; }
    catch (e) { estadoAj.mando = null; estadoAj.mandoError = e.message; }
  } catch (e) {
    estadoAj.error = e.message;
  } finally {
    estadoAj.cargando = false;
  }
}

/** Cambia una casilla en local. No guarda: guardar es un acto explícito. */
export function marcarAviso(clave, valor) {
  if (!estadoAj.datos) return;
  estadoAj.datos.avisos[clave] = valor;
  estadoAj.sucio = true;
}

export function marcarActivo(valor) {
  if (!estadoAj.datos) return;
  estadoAj.datos.telegram_activo = valor;
  estadoAj.sucio = true;
}

export async function aplicarAj(token, chatId, horas) {
  estadoAj.ocupado = true; estadoAj.error = ''; estadoAj.aviso = '';
  try {
    const cuerpo = { avisos: estadoAj.datos.avisos, telegram_activo: estadoAj.datos.telegram_activo };
    if (token) cuerpo.telegram_token = token;             // vacío = no tocar
    const odds = document.getElementById('odds-key')?.value?.trim();
    if (odds) cuerpo.odds_api_key = odds;                 // vacío = no tocar; "-" borra
    if (chatId) cuerpo.telegram_chat_id = chatId;
    if (horas) {
      cuerpo.alineacion_horas = horas.split(',').map((h) => h.trim()).filter(Boolean);
    }
    /* Los controles de ruido se leen del DOM al guardar, igual que el token: así no
       hay un segundo sitio donde el estado pueda quedar desincronizado. */
    const val = (id) => document.getElementById(id)?.value?.trim();
    const chk = (k) => document.querySelector(`[data-aviso="${k}"]`)?.checked;
    const millones = Number(val('aj-umbral'));
    if (Number.isFinite(millones) && millones >= 0) cuerpo.umbral_euros = Math.round(millones * 1e6);
    if (chk('__silencio') !== undefined) cuerpo.silencio_activo = chk('__silencio');
    if (val('aj-sil-desde')) cuerpo.silencio_desde = val('aj-sil-desde');
    if (val('aj-sil-hasta')) cuerpo.silencio_hasta = val('aj-sil-hasta');
    if (chk('__resumen') !== undefined) cuerpo.resumen_activo = chk('__resumen');
    /* Los del respaldo salen del mismo sitio. `_camposRespaldo()` omite lo que no esté
       en pantalla, así que desde otra sub-pestaña no manda nada. Sin esta línea, sus dos
       interruptores marcan «hay cambios», Guardar contesta «Guardado» y el valor no se
       mueve: la copia diaria es lo que protege la serie de precios, que no se puede
       reconstruir hacia atrás. */
    Object.assign(cuerpo, _camposRespaldo());
    estadoAj.datos = await api.guardarAjustes(cuerpo);
    estadoAj.sucio = false;
    estadoAj.aviso = 'Guardado. Surte efecto en menos de 15 segundos.';
  } catch (e) {
    estadoAj.error = e.message;
  } finally {
    estadoAj.ocupado = false;
  }
}

/* Comprueba la clave de cuotas SIN guardarla, igual que el botón de Telegram. Se usa
   `/v4/sports`, que no consume créditos: comprobar debe ser gratis. */
export async function comprobarClaveOdds(repintar) {
  estadoAj.ocupado = true; estadoAj.error = ''; estadoAj.aviso = '';
  repintar();
  try {
    const clave = document.getElementById('odds-key')?.value?.trim() || null;
    const r = await api.probarOdds(clave);
    const q = r.creditos_restantes != null ? ` · quedan ${r.creditos_restantes} consultas este mes` : '';
    estadoAj.aviso = `Clave válida: ${r.deportes_disponibles} competiciones disponibles${q}.`;
  } catch (e) {
    estadoAj.error = e.message;
  } finally {
    estadoAj.ocupado = false;
    repintar();
  }
}

export async function probarAj(token, chatId) {
  estadoAj.ocupado = true; estadoAj.error = ''; estadoAj.aviso = '';
  try {
    const r = await api.probarTelegram(token || null, chatId || null);
    estadoAj.aviso = `Mensaje enviado al chat ${r.enviado_a}. Míralo en Telegram.`;
  } catch (e) {
    estadoAj.error = e.message;
  } finally {
    estadoAj.ocupado = false;
  }
}

/* ---------- pintado ---------- */

const interruptor = (clave, activo, titulo, detalle, urgente = false) => `
  <label class="opcion-aviso">
    <span class="opcion-aviso__texto">
      <span class="opcion-aviso__titulo">${U.esc(titulo)}
        ${urgente ? U.etiqueta('urgente', 'crit') : ''}</span>
      <span class="opcion-aviso__detalle">${U.esc(detalle)}</span>
    </span>
    <input type="checkbox" class="interruptor" data-aviso="${U.esc(clave)}" ${activo ? 'checked' : ''}>
  </label>`;

/* La tarjeta de conexión con LaLiga. Va la PRIMERA en Ajustes: sin cuenta conectada,
   todo lo demás da 503. */
function pintarConexion() {
  const LG = estadoLogin;

  // Círculo "puede tardar": mientras se canjea el code, esto es lo único que se enseña,
  // para que no parezca que la app se ha colgado.
  if (LG.conectando) {
    return U.tarjeta('Conexión con LaLiga',
      `<div class="conex-cargando">
         <div class="spinner" role="status" aria-label="Conectando"></div>
         <p><b>Conectando con LaLiga…</b></p>
         <p class="u-tenue">Esto puede tardar un rato. No cierres la página.</p>
       </div>`, { extra: '⏳' });
  }

  const info = LG.info || {};
  const avisos =
    (LG.error ? U.aviso({ tono: 'crit', titulo: 'No se pudo conectar', texto: LG.error }) : '') +
    (LG.aviso ? U.aviso({ tono: 'ok', titulo: 'Cuenta', texto: LG.aviso }) : '');

  // Ya conectados: estado tranquilo, con la opción de reconectar por si el refresh muere.
  if (info.conectado && LG.paso === 0) {
    const cad = info.bearer_caduca_min != null
      ? `La sesión se renueva sola. Token actual válido ${info.bearer_caduca_min} min.`
      : 'La sesión se renueva sola.';
    return U.tarjeta('Conexión con LaLiga',
      `<div class="tarjeta__cuerpo u-pila">
         ${avisos}
         ${U.aviso({ tono: 'ok', titulo: 'Cuenta conectada', texto: cad })}
         <div class="u-fila">
           <span class="u-tenue">¿Otra cuenta o la sesión ha caducado?</span>
           <span class="u-crecer"></span>
           ${U.boton('Reconectar', { accion: 'login-abrir' })}
         </div>
       </div>`, { extra: '✅' });
  }

  // Paso 1 ya dado (URL abierta): pedir el pegado.
  if (LG.paso === 1) {
    return U.tarjeta('Conexión con LaLiga',
      `<div class="tarjeta__cuerpo u-pila">
         ${avisos}
         ${U.aviso({ tono: 'info', titulo: 'Paso 2 de 2',
            texto: 'Inicia sesión con Google en la pestaña que se ha abierto. Al terminar, '
                 + 'el navegador intentará ir a una dirección «authredirect://…» y fallará: '
                 + 'es normal. Copia esa URL entera de la barra de direcciones y pégala aquí.' })}
         <div>
           <label class="u-label" for="login-pegado">URL de vuelta (o el código)</label>
           <input class="campo-texto" id="login-pegado" autocomplete="off"
             placeholder="authredirect://com.lfp.laligafantasy?code=…">
         </div>
         <div class="u-fila">
           ${U.boton('Empezar de nuevo', { accion: 'login-reiniciar' })}
           <span class="u-crecer"></span>
           ${U.boton('Conectar', { tono: 'accion', accion: 'login-conectar' })}
         </div>
       </div>`, { extra: '2 / 2' });
  }

  // Estado inicial: sin conectar.
  return U.tarjeta('Conexión con LaLiga',
    `<div class="tarjeta__cuerpo u-pila">
       ${avisos}
       ${U.aviso({ tono: 'warn', titulo: 'Sin conectar',
          texto: 'Para que el sistema lea tu equipo y opere hace falta conectar tu cuenta '
               + 'de LaLiga. Se abre la página REAL de LaLiga: entra como entras siempre '
               + '-con Google (y tu llave, si la tienes) o con tu correo y contraseña de '
               + 'Fantasy-. Aquí nunca se guarda tu contraseña.' })}
       <div class="u-fila">
         <span class="u-tenue">Paso 1 de 2</span>
         <span class="u-crecer"></span>
         ${U.boton('Conectar con LaLiga', { tono: 'accion', accion: 'login-abrir' })}
       </div>
     </div>`, { extra: U.icono('candadoCerrado', 'tarjeta__icono') });
}

export function pintarAj() {
  if (estadoAj.error && !estadoAj.datos) {
    return U.aviso({ tono: 'crit', titulo: 'No se han podido leer los ajustes', texto: estadoAj.error });
  }
  if (!estadoAj.datos) return U.cargando('Cargando ajustes…');

  const d = estadoAj.datos;
  const cats = Object.entries(d.categorias);
  const grupo = (urgentes) => cats
    .filter(([, c]) => !!c.urgente === urgentes)
    .map(([k, c]) => interruptor(k, d.avisos[k], c.titulo, c.detalle, c.urgente))
    .join('');

  const bot = `
    <div class="tarjeta__cuerpo u-pila">
      ${d.telegram_configurado
        ? U.aviso({ tono: 'ok', titulo: `Configurado · token ${d.telegram_token_pista}`,
            texto: `Los avisos van al chat ${d.telegram_chat_id}.` })
        : U.aviso({ tono: 'info', titulo: 'Sin configurar · es opcional',
            texto: 'Sirve para que Coconut te avise al móvil cuando te clausulen a '
                 + 'alguien o no se pueda alinear. Sin esto todo funciona igual, '
                 + 'solo que no te avisa.' })}

      ${d.telegram_configurado ? '' : `
      <details class="tg-pasos">
        <summary>Cómo crear el bot (2 minutos)</summary>
        <ol class="tg-pasos__lista">
          <li>Abre Telegram y busca <b>@BotFather</b> (el de la marca azul de verificado).</li>
          <li>Escríbele <code>/newbot</code>. Te pedirá un nombre y luego un usuario, que
              <b>tiene que acabar en <code>bot</code></b>: por ejemplo
              <code>mi_fantasy_bot</code>.</li>
          <li>Te responde con una línea tipo
              <code>123456789:AAG...</code>. <b>Ese es el token</b>: cópialo entero y
              pégalo abajo.</li>
          <li><b>Escríbele a TU bot</b> cualquier cosa («hola» vale). Telegram no deja que
              un bot escriba primero, así que sin este paso no te llegará nada.</li>
          <li>Para el <b>Chat ID</b>: busca <b>@userinfobot</b>, escríbele, y te contesta
              con tu <code>Id</code>. Es un número; cópialo abajo.</li>
        </ol>
        <p class="tg-pasos__nota">
          El token da control total sobre el bot: no lo compartas. Se guarda en este
          dispositivo y nunca se vuelve a mostrar entero.
        </p>
      </details>`}

      <div>
        <label class="u-label" for="tg-token">Token del bot</label>
        <input class="campo-texto" id="tg-token" type="password" autocomplete="off"
          placeholder="Guardado: ${U.esc(d.telegram_token_pista)}, déjalo vacío para no tocarlo">
      </div>
      <div>
        <label class="u-label" for="tg-chat">Chat ID</label>
        <input class="campo-texto u-num" id="tg-chat" inputmode="numeric" autocomplete="off"
          placeholder="${U.esc(d.telegram_chat_id || 'el id numérico de tu chat')}">
      </div>

      ${interruptor('__activo', d.telegram_activo, 'Mandar avisos a Telegram',
        'Apagarlo silencia todo sin perder la configuración.')}

      <div class="u-fila">
        ${U.boton('Mandar prueba', { accion: 'probar-telegram' })}
        <span class="u-crecer"></span>
        ${U.boton(estadoAj.ocupado ? 'Guardando…' : 'Guardar',
          { tono: 'accion', accion: 'guardar-ajustes', off: estadoAj.ocupado })}
      </div>
    </div>`;

  const odds = `
    <div class="tarjeta__cuerpo u-pila">
      ${d.odds_configurado
        ? U.aviso({ tono: 'ok', titulo: `Configurada · clave ${d.odds_api_key_pista}`,
            texto: 'Se usará para calcular la probabilidad de portería a cero de porteros '
                 + 'y defensas a partir de las cuotas reales.' })
        : U.aviso({ tono: 'info', titulo: 'Opcional, pero es la señal que más puntos vale',
            texto: 'Para un portero, la portería a cero son +4 puntos directos, más los −2 '
                 + 'por cada 2 goles que evita: el mayor bloque de la tabla de puntuación. '
                 + 'Sin clave se usa una aproximación sin calibrar. Consíguela gratis en '
                 + 'the-odds-api.com (500 consultas al mes, de sobra para una liga).' })}

      <div>
        <label class="u-label" for="odds-key">Clave de the-odds-api</label>
        <input class="campo-texto" id="odds-key" type="password" autocomplete="off"
          placeholder="${d.odds_configurado
            ? `Guardada: ${U.esc(d.odds_api_key_pista)}, vacío para no tocarla`
            : 'pega aquí tu API key'}">
        <p class="u-tenue">Escribe <b>-</b> y guarda para borrarla.</p>
      </div>

      <div class="u-fila">
        ${U.boton('Comprobar clave', { accion: 'probar-odds' })}
        <span class="u-crecer"></span>
        ${U.boton(estadoAj.ocupado ? 'Guardando…' : 'Guardar',
          { tono: 'accion', accion: 'guardar-ajustes', off: estadoAj.ocupado })}
      </div>
    </div>`;

  return `
    ${pintarConexion()}
    ${estadoAj.aviso ? U.aviso({ tono: 'ok', titulo: 'Hecho', texto: estadoAj.aviso }) : ''}
    ${estadoAj.error ? U.aviso({ tono: 'crit', titulo: 'Algo ha fallado', texto: estadoAj.error }) : ''}
    ${estadoAj.sucio ? U.aviso({ tono: 'warn', titulo: 'Hay cambios sin guardar',
        texto: 'Pulsa Guardar para aplicarlos.' }) : ''}
    ${tarjetaServidor()}
    ${tarjetaRespaldo(d)}
    ${U.tarjeta('Avisos por Telegram', bot, { extra: 'opcional' })}
    ${U.tarjeta('Cuotas de apuestas · portería a cero', odds, {
      extra: d.odds_configurado ? '✅' : 'sin configurar' })}
    ${U.tarjeta('Horario de la alineación', `
      <div class="tarjeta__cuerpo u-pila">
        <label class="u-label" for="aln-horas">Horas del cron, separadas por comas</label>
        <input class="campo-texto u-num" id="aln-horas" autocomplete="off"
          value="${U.esc((d.alineacion_horas || []).join(', '))}" placeholder="10:15, 18:15, 20:15">
        <p class="modal__nota">Se aplica en menos de 10 minutos, sin reiniciar nada.</p>
      </div>`, {
      pie: 'Esto solo reparte el día. La red de seguridad real es la guardia horaria, que mira '
         + 'cuándo abre la jornada de verdad y fuerza una pasada en los 75 min previos.' })}
    ${U.tarjeta('Cuánto ruido quieres', `
      <div class="tarjeta__cuerpo u-pila">
        <div>
          <label class="u-label" for="aj-umbral">No avises por menos de (millones)</label>
          <input class="campo-texto u-num" id="aj-umbral" type="number" min="0" step="0.1"
            value="${((d.umbral_euros ?? 0) / 1e6).toFixed(1)}">
          <p class="modal__nota">Solo afecta a los avisos que se miden en dinero. 0 = sin filtro.</p>
        </div>

        ${interruptor('__silencio', d.silencio_activo, 'Silencio nocturno',
          'De noche solo pasa lo urgente; lo demás espera al resumen.')}
        <div class="u-fila">
          <div><label class="u-label" for="aj-sil-desde">Desde</label>
            <input class="campo-texto u-num" id="aj-sil-desde" type="time"
              value="${U.esc(d.silencio_desde || '23:30')}"></div>
          <div><label class="u-label" for="aj-sil-hasta">Hasta</label>
            <input class="campo-texto u-num" id="aj-sil-hasta" type="time"
              value="${U.esc(d.silencio_hasta || '08:00')}"></div>
        </div>

        ${interruptor('__resumen', d.resumen_activo, 'Resumen diario',
          'Un mensaje con lo que hay que atender, en vez de goteo.')}
      </div>`, {
      pie: 'Robo, once roto y fallo del sistema se saltan el umbral y el silencio: '
         + 'que te quiten un jugador de madrugada sigue siendo urgente de madrugada.' })}
    ${U.tarjeta('Avisos urgentes', `<div class="avisos-lista">${grupo(true)}</div>`, {
      pie: 'Estos son los que hay que saber aunque no estés mirando la app.' })}
    ${U.tarjeta('Los demás', `<div class="avisos-lista">${grupo(false)}</div>`, {
      pie: 'Un bot que avisa de todo se acaba silenciando, y el día que importa no lo lees.' })}`;
}


/* ---------- dónde vive tu coconut ----------
 *
 * Solo aparece dentro de la app Android: en el navegador ya estás en el servidor que
 * quieres usar. La app expone `window.Coconut` (ver `MainActivity.Puente`); sin él, la
 * tarjeta no se dibuja.
 *
 * Es la vía prevista para cambiar de servidor después del primer arranque, que es cuando
 * la app pregunta la dirección.
 */
function tarjetaServidor() {
  const P = globalThis.Coconut;
  if (!P || typeof P.servidor !== 'function') return '';

  let actual = '';
  let local = true;
  /* Si la consulta al puente falla, se conservan los valores iniciales: sin dirección y
     en modo local. El `catch` es amplio a propósito, así que también cubre un puente que
     no exponga `modoLocal`. */
  try { actual = P.servidor() || ''; local = P.modoLocal(); } catch { /* valores iniciales */ }

  const cuerpo = `
    <div class="tarjeta__cuerpo u-pila">
      ${local
        ? U.aviso({ tono: 'ok', titulo: 'Funcionando en este dispositivo',
            texto: 'Coconut corre dentro del móvil con su propia base de datos. No hace '
                 + 'falta nada más.' })
        : U.aviso({ tono: 'info', titulo: `Conectado a ${U.esc(actual)}`,
            texto: 'Los datos salen de ese servidor, no del móvil.' })}

      <div>
        <label class="u-label" for="srv-url">Dirección del servidor</label>
        <input class="campo-texto" id="srv-url" type="url" inputmode="url"
          autocomplete="off" spellcheck="false"
          value="${U.esc(actual)}" placeholder="http://192.168.1.50:8140">
      </div>

      ${U.aviso({ tono: 'warn', titulo: 'Solo funciona si llegas a esa dirección',
        texto: 'El servidor está en tu casa, no en internet. Tienes que estar en la '
             + 'misma red wifi, o con la VPN conectada. Fuera de eso la app no podrá '
             + 'contactar con él y no cargará nada.' })}

      <details class="tg-pasos">
        <summary>¿Para qué sirve tener servidor?</summary>
        <p>
          Con Coconut dentro del móvil tienes <b>la herramienta completa</b>, pero los
          trabajos automáticos solo corren <b>mientras la app está abierta</b>: Android no
          garantiza ejecutar nada en segundo plano.
        </p>
        <p>
          Un servidor (un Raspberry Pi, un NAS o cualquier máquina con Docker) está
          encendido siempre, así que puede <b>guardar la alineación antes de que se
          congele</b>, vigilar clausulazos cada 10 minutos y resolver las pujas de las
          13:48. Eso es lo que el móvil solo no puede darte.
        </p>
        <p class="tg-pasos__nota">
          Se instala con <code>docker run</code>; está explicado en el README del
          repositorio. Pon aquí su dirección con <code>http://</code> y el puerto.
        </p>
      </details>
    </div>`;

  const pie = `<div class="tarjeta__pie">
      ${local ? '' : U.boton('Usar el del móvil', { accion: 'srv-local' })}
      ${U.boton('Guardar y conectar', { tono: 'accion', accion: 'srv-guardar' })}
    </div>`;

  return U.tarjeta('Dónde vive tu Coconut', cuerpo + pie,
                   { extra: local ? 'en el móvil' : 'servidor propio' });
}

/** Copia de seguridad a un repositorio privado de GitHub.
 *
 * Enseña a la vista, y no dentro de un `details`, la lista de lo que no se copia: el
 * token de LaLiga queda fuera, y eso condiciona qué se recupera al restaurar.
 *
 * El botón de restaurar se ofrece desde el principio para poder probar la copia.
 */
function tarjetaRespaldo(d) {
  const conf = d.respaldo_configurado;
  /* `enmascarar('')` devuelve el literal "(sin token)", no una cadena vacía, así que
     `d.github_token_pista` es SIEMPRE cierto. Sin descartar ese literal, la etiqueta
     pone «guardado: (sin token)» y el campo ofrece «dejar vacío para no cambiarlo»
     cuando no hay nada que no cambiar. */
  const hayToken = !!d.github_token_pista && !d.github_token_pista.startsWith('(');
  const ultima = d.respaldo_ultima
    ? U.fecha(d.respaldo_ultima)
    : 'nunca';

  const cuerpo = `
    <div class="tarjeta__cuerpo u-pila">
      ${conf
        ? U.aviso({ tono: 'ok', titulo: `Última copia: ${ultima}`,
            texto: `Se guarda en ${d.respaldo_repo}. Solo se sube lo que haya cambiado, `
                 + 'así que una copia diaria cuesta unos pocos kilobytes.' })
        : U.aviso({ tono: 'warn', titulo: 'Sin copia de seguridad',
            texto: 'Tu configuración, tus tareas y tu historial viven solo en este '
                 + 'dispositivo. Si se pierde, se pierden.' })}

      <div class="rejilla">
        <div>
          <label class="u-label" for="rp-repo">Repositorio</label>
          <input class="campo-texto" id="rp-repo" autocomplete="off" spellcheck="false"
            value="${U.esc(d.respaldo_repo || '')}" placeholder="usuario/coconut-respaldo">
        </div>
        <div>
          <label class="u-label" for="rp-rama">Rama</label>
          <input class="campo-texto" id="rp-rama" autocomplete="off" spellcheck="false"
            value="${U.esc(d.respaldo_rama || 'main')}" placeholder="main">
        </div>
      </div>

      <div>
        <label class="u-label" for="rp-token">Token de GitHub ${
          hayToken ? `<span class="u-dim">(guardado: ${U.esc(d.github_token_pista)})</span>` : ''}</label>
        <input class="campo-texto" id="rp-token" type="password" autocomplete="off"
          spellcheck="false" placeholder="${hayToken ? 'dejar vacío para no cambiarlo' : 'github_pat_…'}">
      </div>

      <div class="rejilla">
        <div>
          <label class="u-label" for="rp-hora">A qué hora</label>
          <input class="campo-texto" id="rp-hora" value="${U.esc(d.respaldo_hora || '04:20')}"
            placeholder="04:20" autocomplete="off">
        </div>
      </div>

      ${interruptor('__rp_activo', d.respaldo_activo, 'Copia automática cada día',
        'Sube la copia a la hora indicada, y solo si algo ha cambiado.', false)}
      ${interruptor('__rp_completa', d.respaldo_completa, 'Incluir la serie de precios',
        'Son unos 30 MB la primera vez. Sin esto se copia la configuración y el '
        + 'historial, que es lo que no se puede recuperar de ninguna otra parte.', false)}

      ${U.aviso({ tono: 'info', titulo: 'Lo que NO se copia, a propósito',
        lista: [
          'El token de LaLiga: es la llave de tu cuenta y opera sin contraseña ni 2FA.',
          'Los tokens de Telegram y GitHub y la clave de las cuotas.',
          'El maestro de jugadores: se vuelve a descargar solo.',
        ],
        texto: 'Al restaurar hay que volver a conectar la cuenta y pegar los tokens. Es '
             + 'un minuto, y a cambio una copia filtrada no sirve de nada.' })}

      <details class="tg-pasos">
        <summary>Cómo preparar el repositorio y el token (3 minutos)</summary>
        <ol class="tg-pasos__lista">
          <li>En GitHub, <b>New repository</b>. Ponle el nombre que quieras
              (<code>coconut-respaldo</code> va bien) y márcalo
              <b>Private</b>. No hace falta añadir README: Coconut lo crea.</li>
          <li>Escribe aquí arriba <code>tu-usuario/el-nombre</code>, sin la URL entera.</li>
          <li>Ve a <b>Settings → Developer settings → Personal access tokens →
              Fine-grained tokens → Generate new token</b>.</li>
          <li>En <b>Repository access</b> elige <b>Only select repositories</b> y marca
              solo ese. En <b>Permissions → Repository permissions</b> pon
              <b>Contents: Read and write</b>.</li>
          <li>Genera, copia el token y pégalo aquí. Pulsa <b>Probar</b> antes de guardar.</li>
        </ol>
        <p class="tg-pasos__nota">
          Dale acceso <b>solo a ese repositorio</b>: si el token se filtra, no sirve para
          tocar nada más tuyo. Se guarda en este dispositivo y no se vuelve a mostrar.
        </p>
      </details>
    </div>`;

  const pie = `<div class="tarjeta__pie">
      ${U.boton('Probar', { accion: 'rp-probar' })}
      ${conf ? U.boton('Restaurar', { accion: 'rp-restaurar' }) : ''}
      ${U.boton('Copiar ahora', { tono: 'accion', accion: 'rp-copiar' })}
    </div>`;

  const mando = bloqueMando(conf);

  return U.tarjeta('Copia de seguridad', cuerpo + pie,
                   { extra: conf ? (d.respaldo_activo ? 'automática' : 'manual') : 'sin configurar' })
       + mando;
}

/** Quién manda cuando hay más de una instalación.
 *
 * Va justo DEBAJO de la copia y no en su propia sección a propósito: el candado vive
 * dentro del repositorio de la copia, porque es el único sitio que dos dispositivos con
 * bases de datos distintas pueden ver. Separarlo haría creer que funciona sin configurar
 * la copia, y sin ella no hay candado en absoluto.
 */
function bloqueMando(conf) {
  if (!conf) return '';
  const m = estadoAj.mando;
  if (!m) return U.tarjeta('Quién opera', `
    <div class="tarjeta__cuerpo">${estadoAj.mandoError
      ? U.aviso({ tono: 'warn', titulo: 'No se ha podido leer el candado',
                  texto: `${U.esc(estadoAj.mandoError)}. Los ajustes de abajo funcionan igual.` })
      : U.cargando('Consultando…')}</div>`);

  const suyo = m.mando && !m.es_mio;
  const cuerpo = `
    <div class="tarjeta__cuerpo u-pila">
      ${m.es_mio
        ? U.aviso({ tono: 'ok', titulo: 'Este dispositivo es el que opera',
            texto: `El mando se renueva solo con cada copia. Caduca a las ${m.horas} h `
                 + 'sin renovar, así que si este aparato se apaga otro podrá tomarlo.' })
        : suyo
          ? U.aviso({ tono: 'warn', titulo: `Ahora opera «${U.esc(m.mando.nombre || m.mando.id)}»`,
              texto: m.motivo })
          : U.aviso({ tono: 'info', titulo: 'Nadie tiene el mando',
              texto: 'Cualquier instalación puede operar. Si tienes más de una, conviene '
                   + 'que solo una lo haga: dos aparatos pueden gastar la misma caja dos veces.' })}

      <div>
        <label class="u-label" for="rp-nombre">Cómo se llama este dispositivo</label>
        <input class="campo-texto" id="rp-nombre" autocomplete="off"
          value="${U.esc(m.yo.nombre || '')}" placeholder="Servidor de casa">
      </div>
      <p class="u-dim">Leer, mirar el mercado y simular funciona siempre. Lo único que
        exige tener el mando es <b>ejecutar</b>: pujar, clausular, vender o blindar.</p>
    </div>`;

  const pie = `<div class="tarjeta__pie">
      ${m.es_mio ? U.boton('Soltar el mando', { accion: 'md-soltar' }) : ''}
      ${!m.es_mio ? U.boton(suyo ? 'Quitárselo y operar aquí' : 'Operar desde aquí',
          { tono: 'accion', accion: suyo ? 'md-forzar' : 'md-tomar' }) : ''}
    </div>`;

  return U.tarjeta('Quién opera', cuerpo + pie,
                   { extra: m.es_mio ? 'este' : (suyo ? 'otro' : 'libre') });
}

/* ---------- acciones de la copia de seguridad ---------- */

/** Lee del DOM lo que el usuario haya escrito. Como el token de Telegram: vacío = no
    tocar, así que solo se manda lo que tiene algo. */
function _camposRespaldo() {
  const v = (id) => document.getElementById(id)?.value?.trim() || '';
  const cuerpo = {};
  if (v('rp-repo')) cuerpo.respaldo_repo = v('rp-repo');
  if (v('rp-rama')) cuerpo.respaldo_rama = v('rp-rama');
  if (v('rp-token')) cuerpo.github_token = v('rp-token');
  if (v('rp-hora')) cuerpo.respaldo_hora = v('rp-hora');
  const chk = (k) => document.querySelector(`[data-aviso="${k}"]`)?.checked;
  if (chk('__rp_activo') !== undefined) cuerpo.respaldo_activo = chk('__rp_activo');
  if (chk('__rp_completa') !== undefined) cuerpo.respaldo_completa = chk('__rp_completa');
  return cuerpo;
}

/** Probar no guarda nada: valida las credenciales que hay escritas.
 *  Enterarse de que el token está mal el día que hace falta restaurar es justo lo que
 *  esto evita: mismo criterio que el botón de Telegram. */
export async function respProbar(repintar) {
  estadoAj.ocupado = true; estadoAj.error = ''; estadoAj.aviso = '';
  repintar();
  try {
    const c = _camposRespaldo();
    const r = await api.respaldoProbar({
      github_token: c.github_token, respaldo_repo: c.respaldo_repo,
      respaldo_rama: c.respaldo_rama });
    estadoAj.aviso = r.aviso
      ? r.aviso
      : `✅ ${r.repo} accesible, privado y con permiso de escritura.`;
  } catch (e) {
    estadoAj.error = e.message;
  } finally {
    estadoAj.ocupado = false;
    repintar();
  }
}

/** Copiar guarda antes la configuración: si no, el primer "Copiar ahora" tras pegar el
    token falla por no tener credenciales. */
export async function respCopiar(repintar) {
  estadoAj.ocupado = true; estadoAj.error = ''; estadoAj.aviso = '';
  repintar();
  try {
    const c = _camposRespaldo();
    if (Object.keys(c).length) estadoAj.datos = await api.guardarAjustes(c);
    const r = await api.respaldoCopiar();
    estadoAj.aviso = r.subido
      ? `Copia subida (commit ${r.commit}) · ${r.ficheros.length} fichero(s) · ${r.tamano_kb} KB`
      : `No hacía falta: ${r.motivo}.`;
    await cargarAj();
  } catch (e) {
    estadoAj.error = e.message;
  } finally {
    estadoAj.ocupado = false;
    repintar();
  }
}

/** Restaurar pide confirmación con el `confirm` del navegador y no pisa nada.
 *  Es una escritura masiva sobre la base de datos, aunque por defecto solo añada lo
 *  que falte. */
export async function respRestaurar(repintar) {
  if (!globalThis.confirm?.(
      'Traer la copia del repositorio y meterla en esta instalación.\n\n'
      + 'Solo AÑADE lo que falte: no pisa nada de lo que ya tienes.\n'
      + 'Los tokens no vienen en la copia, hay que volver a ponerlos.\n\n¿Seguimos?')) return;
  estadoAj.ocupado = true; estadoAj.error = ''; estadoAj.aviso = '';
  repintar();
  try {
    const r = await api.respaldoRestaurar(false);
    const filas = Object.entries(r.filas).filter(([, n]) => n)
      .map(([t, n]) => `${t}: ${n}`).join(' · ');
    estadoAj.aviso = `Restaurado. ${filas || 'no había nada que añadir'}.`;
    await cargarAj();
  } catch (e) {
    estadoAj.error = e.message;
  } finally {
    estadoAj.ocupado = false;
    repintar();
  }
}


/* ---------- el mando ---------- */

export async function mandoAccion(que, repintar) {
  estadoAj.ocupado = true; estadoAj.error = ''; estadoAj.aviso = '';
  repintar();
  try {
    const nombre = document.getElementById('rp-nombre')?.value?.trim();
    // El nombre se guarda antes de tomar el mando: si no, el otro dispositivo ve el
    // nombre viejo hasta la siguiente renovación, y ese texto es lo que le permite
    // decidir si se lo quita.
    if (nombre && nombre !== estadoAj.mando?.yo?.nombre) {
      await api.guardarAjustes({ dispositivo_nombre: nombre });
    }
    if (que === 'soltar') {
      await api.mandoSoltar();
      estadoAj.aviso = 'Mando libre: cualquier instalación puede operar.';
    } else {
      const r = await api.mandoTomar(que === 'forzar');
      estadoAj.aviso = `Este dispositivo opera hasta ${U.fecha(r.mando.hasta)}.`;
    }
    estadoAj.mando = await api.mandoLeer({ refrescar: true });
  } catch (e) {
    estadoAj.error = e.message;
  } finally {
    estadoAj.ocupado = false;
    repintar();
  }
}
