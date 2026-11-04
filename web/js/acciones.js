/* ============================================================================
   acciones.js: el diálogo que hay antes de tocar dinero.

   Parte de lo que ejecuta la web no se deshace: una puja ganada, un clausulazo, una
   venta inmediata. Otras sí (cancelar una puja, retirar una ficha del mercado), y el
   diálogo dice de cuál se trata. Un «¿estás seguro?» genérico se acepta sin leer, así
   que este dice qué se va a hacer, cuánto cuesta, con cuánto te quedas y qué queda
   público de ello.

   Ofrece dos vías, y el endpoint cambia con la elegida:

     ahora      POST /accion/{accion}   ejecuta y devuelve el resultado
     programar  POST /programar         crea la tarea, que vive en la base

   Programar no es solo «más tarde»: clausulazos, ventas y blindajes salen en
   el tablón con su hora, así que elegirla es parte de la jugada.
   ========================================================================== */

import * as U from './ui.js';
import * as api from './api.js';

/** Lo que hay que saber de cada acción antes de lanzarla. */
export const ACCIONES = {
  /* Las cuatro de ficha son las que más falta hace poder programar: una puja se cancela
     ANTES de las 13:48 o ya no se cancela, y una oferta recibida caduca. */
  'cancelar-puja': {
    caja: 'libera',
    titulo: 'Cancelar una puja viva',
    riesgo: 'bajo',
    avisos: [
      'Libera el dinero retenido en teamInvestment, que no cuenta como saldo disponible.',
      'Una puja no se rebaja: se cancela y se vuelve a pujar.',
      '⚠️ Solo sirve ANTES de las 13:48. Después la subasta ya se ha resuelto.',  // sin-rastro-ok: lo lee el usuario en el diálogo
    ],
  },
  'retirar-mercado': {
    caja: 'nada',
    titulo: 'Retirar del mercado',
    riesgo: 'bajo',
    avisos: [
      'Deja de ofrecer al jugador. No mueve dinero.',
      '⚠️ NO deshace una venta ya aceptada: si aceptaste la oferta, el jugador ya no es tuyo.',  // sin-rastro-ok: lo lee el usuario en el diálogo
      'Mientras la ficha siga viva, el sistema puede mandar su oferta en cualquier momento.',
    ],
  },
  'aceptar-oferta': {
    caja: 'ingresa',
    titulo: 'Aceptar una oferta recibida',
    riesgo: 'alto',
    avisos: [
      'INGRESA dinero: el jugador deja de ser tuyo en el momento.',
      'LaLiga paga aleatoriamente entre el valor y ±10 %. Cada ciclo de mercado es una tirada nueva.',
      'Sobre 8 ofertas medidas, esperar a una de ≥ +1 % rinde +6,42 pp más que aceptar la primera.',
      '⚠️ La oferta caduca al siguiente ciclo de mercado.',  // sin-rastro-ok: lo lee el usuario en el diálogo
    ],
  },
  'rechazar-oferta': {
    caja: 'nada',
    titulo: 'Rechazar una oferta',
    riesgo: 'bajo',
    avisos: [
      'No mueve dinero: la ficha sigue listada y puede recibir otra oferta.',
      'Es la mitad útil del escaparate: listar no es vender, es quedarse la opción de vender caro.',
    ],
  },
  ofertar: {
    caja: 'gasta',
    titulo: 'Ofertar por la ficha de otro manager',
    riesgo: 'medio',
    avisos: [
      'NO es una compra: el dueño acepta o rechaza, y puede dejarla caducar.',
      'La oferta es pública para él: pedir una posición en la que vas corto se la anuncia.',
      'El importe debe ser >= el VALOR de mercado; el precio de salida no es el mínimo.',
      'El dinero queda retenido en teamInvestment mientras la oferta siga viva.',
    ],
  },
  pujar: {
    caja: 'gasta',
    titulo: 'Pujar por un agente libre',
    riesgo: 'medio',
    avisos: [
      'La subasta es a PRIMER PRECIO: pagas exactamente lo que pujas, no la puja del segundo.',
      'La cláusula del fichaje queda igual al precio pagado. Pagar de más te protege; pagar justo lo deja robable.',
      'No se resuelve ahora: todas las pujas se abren a las 13:48 del día siguiente.',
      'El dinero queda retenido en teamInvestment hasta entonces.',
    ],
  },
  vender: {
    caja: 'nada',
    titulo: 'Listar en el mercado para vender',
    riesgo: 'medio',
    avisos: [
      'Listar NO es vender: LaLiga manda su oferta cuando quiere y hay que aceptarla aparte.',
      'Puede tardar días, así que nunca cuentes con este dinero para una puja del mismo día.',
      'Rechazar es gratis y cada ciclo de mercado trae una oferta nueva.',
      'La ficha caduca a los 3 días.',
    ],
  },
  'venta-inmediata': {
    caja: 'ingresa',
    titulo: 'Venta inmediata (dinero al instante)',
    riesgo: 'alto',
    avisos: [
      'Paga el 50 % del valor de mercado, truncado al euro. Es la mitad, no una rebaja.',
      'A diferencia de listar, esto SÍ da caja al momento.',
      'Listar y esperar paga alrededor del valor (entre −5,4 % y +9 %). La diferencia es '
        + 'enorme: esto solo compensa con prisa real o con un jugador que ya no puntúa.',
      'Una vez vendido, no se recupera.',
    ],
  },
  clausulazo: {
    caja: 'gasta',
    titulo: 'Pagar la cláusula de un rival',
    riesgo: 'alto',
    avisos: [
      'El importe debe ser EXACTO al euro. Se relee la cláusula justo antes de pagar.',
      'Sale en el tablón con tu nombre y la hora: el rival sabrá exactamente cuándo operas.',
      'Si está blindado o protegido, la API lo rechaza (400 / 409).',
      'El dinero va a un rival directo, que lo usará contra ti.',
    ],
  },
  blindar: {
    caja: 'nada',
    titulo: 'Blindar contra el clausulazo',
    riesgo: 'bajo',
    avisos: [
      'Dura 24 h exactas desde que se activa, no hasta el siguiente ciclo de mercado.',
      'Sale en el tablón: señala justo al jugador que temes perder.',
      'No cuesta dinero, pero declara haber visto un anuncio que no se ha visto.',
    ],
  },
  'subir-clausula': {
    caja: 'mitad',
    titulo: 'Subir la cláusula de un jugador tuyo',
    riesgo: 'medio',
    avisos: [
      'factor=2 es el único que existe: la cláusula sube el importe indicado y cuesta la MITAD.',
      'No reinicia la protección de 14 días, así que no abre ninguna ventana.',
      'Sale en el tablón.',
    ],
  },
};

/** Por encima de esto, confirmar deja de ser un clic y hay que teclear el importe. */
export const UMBRAL_TECLEAR = 10_000_000;

export const dialogo = {
  abierto: false, accion: null, datos: null,
  cuando: 'ahora',     // 'ahora' | una hora ISO
  tecleado: '',
  ocupado: false,
  leyendo: false,      // esperando a que el servidor diga el importe exacto
  error: '',
  hecho: null,
};

export function abrirAccion(accion, datos, { leyendo = false } = {}) {
  Object.assign(dialogo, {
    abierto: true, accion, datos, cuando: 'ahora',
    tecleado: '', ocupado: false, leyendo, error: '', hecho: null,
  });
}

/** Paso previo: qué se le puede hacer a este jugador. Evita llenar cada fila de
    la plantilla de botones que en el móvil no caben. */
export function abrirAcciones(jugador) {
  Object.assign(dialogo, {
    abierto: true, accion: null, datos: { jugador }, cuando: 'ahora',
    tecleado: '', ocupado: false, error: '', hecho: null,
  });
}

export function cerrarDialogo() { dialogo.abierto = false; }

/** Atajos de hora para no teclear la fecha entera: el cuarto más cercano primero (si
    cae antes que la hora en punto siguiente) y después las horas en punto. Son
    sugerencias: el endpoint acepta cualquier hora futura. */
export function horasRedondas(cuantas = 8) {
  const out = [];
  const t = new Date();
  t.setSeconds(0, 0);
  t.setMinutes(t.getMinutes() + 6);          // margen para que dé tiempo a confirmar
  const primera = new Date(t);
  primera.setMinutes(0);
  if (primera <= t) primera.setHours(primera.getHours() + 1);
  for (let i = 0; i < cuantas; i++) {
    const h = new Date(primera);
    h.setHours(h.getHours() + i);
    out.push(h);
  }
  // Un cuarto cercano, para cuando no se quiere esperar a la hora en punto.
  const cuarto = new Date(t);
  cuarto.setMinutes(Math.ceil(cuarto.getMinutes() / 15) * 15, 0, 0);
  if (cuarto > t && cuarto < out[0]) out.unshift(cuarto);
  return out;
}

const exigeTeclear = () => Number(dialogo.datos?.importe || 0) >= UMBRAL_TECLEAR;

export function puedeConfirmar() {
  if (dialogo.ocupado) return false;
  if (!exigeTeclear()) return true;
  return dialogo.tecleado.replace(/\D/g, '') === String(dialogo.datos.importe);
}

/** Ejecuta o programa. El endpoint cambia según lo elegido. */
export async function confirmarAccion() {
  const { accion, datos, cuando } = dialogo;
  dialogo.ocupado = true; dialogo.error = '';
  try {
    if (cuando === 'ahora') {
      const r = await api.actuar(accion, datos.params);
      dialogo.hecho = { modo: 'ahora', resultado: r };
    } else {
      const r = await api.programar(accion, datos.params, cuando,
        datos.motivo || `${accion} desde la web`);
      dialogo.hecho = { modo: 'programado', resultado: r, cuando };
    }
  } catch (e) {
    dialogo.error = e.message;
  } finally {
    dialogo.ocupado = false;
  }
  return dialogo.hecho;
}

/* ---------- pintado ---------- */

/** Qué se puede hacer con un jugador propio, dado su estado real. Una acción que
    la API va a rechazar no se ofrece: se explica por qué no está. */
export function opcionesDe(j) {
  const protegido = j.protegido_hasta && new Date(j.protegido_hasta) > Date.now();
  return [
    { accion: 'vender', etiqueta: 'Vender al mercado', ok: true,
      motivo: 'Se lista y LaLiga ofrece entre el valor y ±10 %, pero puede tardar días.' },
    { accion: 'blindar', etiqueta: 'Blindar 24 h', ok: !j.blindado,
      motivo: j.blindado ? `Ya está blindado hasta ${U.fecha(j.blindado_hasta)}.` : 'Gratis, y dura 24 h exactas.' },
    { accion: 'subir-clausula', etiqueta: 'Subir la cláusula', ok: true,
      motivo: protegido
        ? `Protegido ${U.enDias(j.protegido_hasta)} días más: hoy nadie puede clausularlo igualmente.`
        : `Cláusula ${U.millones(j.clausula)} · vale ${U.millones(j.valor)}. Subirla cuesta la mitad de lo que sube.` },
  ];
}

export function pintarDialogoHTML(saldoLibre) {
  if (!dialogo.abierto) return '';
  if (!dialogo.accion) return pintarMenuJugador();

  const meta = ACCIONES[dialogo.accion] || { titulo: dialogo.accion, avisos: [], riesgo: 'medio' };
  const d = dialogo.datos;
  const importe = Number(d.importe || 0);
  /* Lo que cada acción le hace a la caja lo declara su entrada del catálogo. Restando
     el importe siempre, cobrar una oferta pinta un "No te llega" cuando lo que hace es
     INGRESAR, y subir una cláusula descuenta el doble de lo que cuesta. */
  const coste = { ingresa: -importe, libera: -importe, mitad: Math.floor(importe / 2),
                  nada: 0 }[meta.caja] ?? importe;
  const despues = saldoLibre - coste;

  if (dialogo.hecho) return pintarResultado(meta);

  /* Hay importes que no se saben al pulsar: la cláusula se recalcula a diario y el
     servidor la relee al euro. Esa lectura tarda segundos, así que el diálogo se abre ya
     diciendo qué está haciendo. Abrirlo con una cifra provisional sería peor: se
     confirma un pago exacto y la cifra cambiaría delante. */
  if (dialogo.leyendo) {
    return `<div class="modal" id="modal">
      <div class="modal__velo" data-cerrar-modal></div>
      <div class="modal__caja" role="dialog" aria-modal="true">
        <header class="modal__cabecera modal__cabecera--${meta.riesgo}">
          <h2>${U.esc(meta.titulo)}</h2>
          <button class="menu__cerrar" data-cerrar-modal aria-label="Cerrar">${U.icono("cerrar")}</button>
        </header>
        <div class="modal__cuerpo">
          <p class="modal__sujeto">${U.esc(d.nombre)}</p>
          ${U.cargando('Releyendo la cláusula exacta…')}
        </div>
      </div>
    </div>`;
  }

  const horas = horasRedondas().map((h) => {
    /* `toISOString()` pasa a UTC, así que se le resta el desfase para que la cadena
       represente la hora LOCAL pulsada. Es el valor que va a `/programar`: sin la resta,
       un clausulazo pedido a las 14:00 se lanza a las 12:00. */
    const iso = new Date(h.getTime() - h.getTimezoneOffset() * 60000).toISOString().slice(0, 16);
    const et = h.toLocaleTimeString('es-ES', { hour: '2-digit', minute: '2-digit' });
    const hoy = h.getDate() === new Date().getDate();
    return `<button class="hora" data-cuando="${iso}" aria-pressed="${dialogo.cuando === iso}">
      ${et}${hoy ? '' : ' <small>mañana</small>'}</button>`;
  }).join('');

  return `<div class="modal" id="modal">
    <div class="modal__velo" data-cerrar-modal></div>
    <div class="modal__caja" role="dialog" aria-modal="true" aria-labelledby="modal-titulo">
      <header class="modal__cabecera modal__cabecera--${meta.riesgo}">
        <h2 id="modal-titulo">${U.esc(meta.titulo)}</h2>
        <button class="menu__cerrar" data-cerrar-modal aria-label="Cerrar">${U.icono("cerrar")}</button>
      </header>

      <div class="modal__cuerpo">
        <p class="modal__sujeto">${U.esc(d.nombre)}</p>
        ${importe ? `<dl class="modal__cuentas">
          <div><dt>${coste < 0 ? 'Ingresas' : coste === 0 ? 'Importe' : 'Cuesta'}</dt>
            <dd class="u-num">${U.euros(Math.abs(coste) || importe)} €</dd></div>
          ${meta.caja === 'mitad' ? `<div><dt>Sube la cláusula</dt>
            <dd class="u-num">${U.euros(importe)} €</dd></div>` : ''}
          <div><dt>Caja libre ahora</dt><dd class="u-num">${U.euros(saldoLibre)} €</dd></div>
          <div><dt>Te quedarías con</dt>
            <dd class="u-num ${despues < 0 ? 'u-crit' : ''}">${U.euros(despues)} €</dd></div>
        </dl>` : ''}

        ${despues < 0 ? U.aviso({ tono: 'crit', titulo: 'No te llega',
          texto: `Faltan ${U.euros(-despues)} €. El servidor lo rechazará.` }) : ''}

        <div class="modal__avisos">
          <p class="u-label">Antes de confirmar</p>
          <ul class="aviso__lista">${meta.avisos.map((a) => `<li>${U.esc(a)}</li>`).join('')}</ul>
        </div>

        <div class="modal__cuando">
          <p class="u-label">¿Cuándo?</p>
          <div class="horas">
            <button class="hora hora--ya" data-cuando="ahora" aria-pressed="${dialogo.cuando === 'ahora'}">
              Ahora mismo</button>
            ${horas}
          </div>
          <p class="modal__nota">${dialogo.cuando === 'ahora'
            ? '⚠️ Ejecutar ahora deja tu hora exacta en el tablón, al segundo.'  // sin-rastro-ok: lo lee el usuario en el diálogo
            : 'Se guardará como tarea en la base de datos y sobrevive a un reinicio.'}</p>
        </div>

        ${exigeTeclear() ? `<div class="modal__teclear">
          <label class="u-label" for="teclear">Son más de 10 M. Escribe el importe para confirmar</label>
          <input class="campo-texto u-num" id="teclear" inputmode="numeric" autocomplete="off"
            placeholder="${U.euros(importe)}" value="${U.esc(dialogo.tecleado)}">
        </div>` : ''}

        ${dialogo.error ? U.aviso({ tono: 'crit', titulo: 'La API lo ha rechazado', texto: dialogo.error }) : ''}
      </div>

      <footer class="modal__pie">
        <button class="btn" data-cerrar-modal>Cancelar</button>
        <button class="btn btn--accion" data-confirmar ${puedeConfirmar() ? '' : 'disabled'}>
          ${dialogo.ocupado ? 'Enviando…'
            : dialogo.cuando === 'ahora' ? 'Ejecutar ahora' : 'Programar'}</button>
      </footer>
    </div>
  </div>`;
}

function pintarMenuJugador() {
  const j = dialogo.datos.jugador;
  const filas = opcionesDe(j).map((o) => `
    <button class="opcion" data-elegir="${U.esc(o.accion)}" ${o.ok ? '' : 'disabled'}>
      <span class="opcion__titulo">${U.esc(o.etiqueta)}</span>
      <span class="opcion__motivo">${U.esc(o.motivo)}</span>
    </button>`).join('');

  return `<div class="modal" id="modal">
    <div class="modal__velo" data-cerrar-modal></div>
    <div class="modal__caja" role="dialog" aria-modal="true" aria-label="Acciones del jugador">
      <header class="modal__cabecera modal__cabecera--bajo">
        <h2>${U.esc(j.nombre)}</h2>
        <button class="menu__cerrar" data-cerrar-modal aria-label="Cerrar">${U.icono("cerrar")}</button>
      </header>
      <div class="modal__cuerpo">
        <p class="u-dim">${U.chipPos(j.posicion)} · vale ${U.millones(j.valor)} ·
          cláusula ${U.millones(j.clausula)} · ${j.puntos_temporada} pts</p>
        <div class="opciones">${filas}</div>
      </div>
      <footer class="modal__pie"><button class="btn" data-cerrar-modal>Cancelar</button></footer>
    </div>
  </div>`;
}

function pintarResultado(meta) {
  const h = dialogo.hecho;
  const cuando = h.modo === 'programado'
    ? new Date(h.cuando).toLocaleString('es-ES', { day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit' })
    : null;
  return `<div class="modal" id="modal">
    <div class="modal__velo" data-cerrar-modal></div>
    <div class="modal__caja" role="dialog" aria-modal="true">
      <header class="modal__cabecera modal__cabecera--bajo"><h2>Hecho</h2></header>
      <div class="modal__cuerpo">
        ${U.aviso({
          tono: 'ok',
          titulo: h.modo === 'programado' ? `Programado para el ${cuando}` : `${meta.titulo}: ejecutado`,
          texto: h.modo === 'programado'
            ? 'Queda en la lista de tareas. Puedes cancelarla desde el Radar hasta que se dispare.'
            : 'La API lo ha aceptado. Los datos se recargan al cerrar.',
        })}
        <pre class="modal__crudo">${U.esc(JSON.stringify(h.resultado, null, 1).slice(0, 900))}</pre>
      </div>
      <footer class="modal__pie"><button class="btn btn--accion" data-cerrar-modal>Cerrar</button></footer>
    </div>
  </div>`;
}
