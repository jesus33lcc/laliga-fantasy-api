/* ============================================================================
   ui.js: componentes reutilizables. Cada función devuelve HTML y toma su
   aspecto de css/: aquí solo van en línea los valores que dependen de los datos
   (el ancho de una barra, el índice de una animación), que no caben en una hoja
   de estilos. Si una vista necesita una pieza nueva, se añade aquí, no dentro
   de la vista.
   ========================================================================== */

export const POS = { 1: 'POR', 2: 'DEF', 3: 'CEN', 4: 'DEL', 5: 'ENT' };
export const POS_LARGO = { 1: 'Portero', 2: 'Defensa', 3: 'Centrocampista', 4: 'Delantero', 5: 'Entrenador' };
/* /estado habla de MED donde la app dice CEN. Se traduce aquí y ya. */
export const POS_ID = { POR: 1, DEF: 2, MED: 3, CEN: 3, DEL: 4, ENT: 5 };

/* El estado del jugador llega de LaLiga en inglés. Se traduce en un solo sitio: escrito
   a mano en cada vista acaba saliendo crudo en alguna. */
const ESTADOS = {
  ok: 'disponible',
  injured: 'lesionado',
  suspended: 'sancionado',
  doubtful: 'duda',
  out_of_league: 'fuera de la liga',
};
export const estadoJugador = (e) => ESTADOS[String(e || '').toLowerCase()] || e || '';

/* ---------- utilidades de formato ---------- */

export const esc = (v) => String(v ?? '').replace(/[&<>"']/g, (c) =>
  ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

export const euros = (n) => Number(n || 0).toLocaleString('es-ES', { maximumFractionDigits: 0 });

export const millones = (n) => {
  const m = Number(n || 0) / 1e6;
  return (m >= 100 ? m.toFixed(0) : m.toFixed(2)).replace('.', ',') + 'M';
};

export const fecha = (iso, conHora = true) => {
  if (!iso) return '-';
  const d = new Date(iso);
  const dia = d.toLocaleDateString('es-ES', { day: '2-digit', month: 'short' });
  return conHora ? `${dia} ${d.toLocaleTimeString('es-ES', { hour: '2-digit', minute: '2-digit' })}` : dia;
};

export const enDias = (iso) => {
  if (!iso) return null;
  const d = Math.ceil((new Date(iso) - Date.now()) / 86400000);
  return d > 0 ? d : 0;
};

/* ---------- iconos ---------- */

const PATHS = {
  menu: 'M3 6h18M3 12h18M3 18h18',
  campana: 'M18 8a6 6 0 1 0-12 0c0 7-3 9-3 9h18s-3-2-3-9M13.7 21a2 2 0 0 1-3.4 0',
  buscar: 'M11 19a8 8 0 1 0 0-16 8 8 0 0 0 0 16zM21 21l-4.3-4.3',
  trofeo: 'M7 4h10v6a5 5 0 0 1-10 0zM17 5h3v1.5a3.5 3.5 0 0 1-3.5 3.5M7 5H4v1.5A3.5 3.5 0 0 0 7.5 10M12 15v3M8.5 21h7',
  barras: 'M4 20V10M10 20V4M16 20v-7M22 20h-20',
  // Campo de fútbol, como en la app: rectángulo, línea central y círculo.
  campo: 'M3 5h18v14H3zM12 5v14M12 9.5a2.5 2.5 0 1 0 0 5 2.5 2.5 0 1 0 0-5M3 9h2v6H3M21 9h-2v6h2',
  grafico: 'M3 17l5-5 4 4 9-9M21 7h-5M21 7v5',
  radar: 'M12 12L19 5M12 21a9 9 0 1 1 9-9M12 17a5 5 0 1 1 5-5',
  enchufe: 'M9 2v6M15 2v6M6 8h12v3a6 6 0 0 1-12 0V8zM12 17v5',
  lista: 'M8 6h13M8 12h13M8 18h13M3 6h.01M3 12h.01M3 18h.01',
  candadoAbierto: 'M7 11V7a5 5 0 0 1 9.9-1M5 11h14v10H5z',
  candadoCerrado: 'M7 11V7a5 5 0 0 1 10 0v4M5 11h14v10H5z',
  escudo: 'M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z',
  github: 'M9 19c-5 1.5-5-2.5-7-3m14 6v-3.9a3.4 3.4 0 0 0-.9-2.6c3-.3 6.2-1.5 6.2-6.7A5.2 5.2 0 0 0 20 5.8a4.9 4.9 0 0 0-.1-3.6s-1.1-.3-3.6 1.4a12.4 12.4 0 0 0-6.6 0C7.2 1.9 6.1 2.2 6.1 2.2A4.9 4.9 0 0 0 6 5.8 5.2 5.2 0 0 0 4.6 9.4c0 5.2 3.2 6.4 6.2 6.7a3.4 3.4 0 0 0-.9 2.5V22',
  reloj: 'M12 22a10 10 0 1 0 0-20 10 10 0 0 0 0 20zM12 6v6l4 2',
  'flecha-abajo': 'M6 9l6 6 6-6',
  cerrar: 'M6 6l12 12M18 6L6 18',
  camiseta: 'M8 3 5 5 3 9l3 1.5V21h12V10.5L21 9l-2-4-3-2c0 0-1 2.5-4 2.5S8 3 8 3Z',
  engranaje: 'M12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6zM19.4 15a1.7 1.7 0 0 0 .3 1.9l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.9-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1A1.7 1.7 0 0 0 8.9 19a1.7 1.7 0 0 0-1.9.4l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.9 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1A1.7 1.7 0 0 0 5 8.9a1.7 1.7 0 0 0-.4-1.9l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.9.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.9-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.9V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z',
};

export const icono = (nombre, clase = '') => `<svg class="${esc(clase)}" viewBox="0 0 24 24" fill="none"
  stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"
  width="22" height="22" aria-hidden="true"><path d="${PATHS[nombre] || ''}"/></svg>`;

/* Los colores salen de `tokens.css` con `var()`, que un SVG en línea sí resuelve. Escritos
 * a mano quedarían fuera del único sitio donde viven los colores, y al cambiar la paleta
 * estos dos se quedarían con el tono antiguo sin que nada avisara. */
const CHECK_OK = `<svg class="ficha__estado" viewBox="0 0 16 16" aria-hidden="true">
  <circle cx="8" cy="8" r="8" fill="var(--c-ok)"/><path d="M4.5 8.2l2.3 2.3 4.6-4.6"
  stroke="var(--c-sobre-ok)" stroke-width="2" fill="none" stroke-linecap="round"
  stroke-linejoin="round"/></svg>`;
const CHECK_NO = `<svg class="ficha__estado" viewBox="0 0 16 16" aria-hidden="true">
  <circle cx="8" cy="8" r="8" fill="var(--c-crit)"/><path d="M5.5 5.5l5 5M10.5 5.5l-5 5"
  stroke="var(--c-ink)" stroke-width="2" stroke-linecap="round"/></svg>`;

/* ---------- piezas ---------- */

export const chipPos = (pos) => {
  const id = typeof pos === 'number' ? pos : (POS_ID[pos] || 0);
  return `<span class="chip-pos chip-pos--${id}" title="${esc(POS_LARGO[id] || '')}">${esc(POS[id] || '?')}</span>`;
};

export const retrato = (src, alt, mod = '') =>
  src ? `<img class="retrato ${mod}" src="${esc(src)}" alt="${esc(alt)}" loading="lazy" decoding="async">`
      : `<div class="retrato retrato--vacio ${mod}" role="img" aria-label="${esc(alt)}">?</div>`;

export const dinero = (n) => `<span class="dinero"><span class="dinero__moneda">€</span>${euros(n)}</span>`;

export const marcador = (pts, unidad = 'PFSY') => {
  const mod = pts > 0 ? ' marcador--ok' : pts < 0 ? ' marcador--crit' : '';
  return `<span class="marcador${mod}"><span class="marcador__unidad">${esc(unidad)}</span>
    <span class="marcador__cifra">${esc(pts ?? 0)}</span></span>`;
};

/** Los 5 cuadraditos de jornada. `pts` es un array; null = no jugada. */
export const racha = (pts, huecos = 5) => {
  const casillas = Array.from({ length: huecos }, (_, i) => {
    const p = pts?.[i];
    if (p === undefined || p === null) return `<span class="racha__casilla racha__casilla--vacia">·</span>`;
    const grado = p >= 6 ? 'alta' : p >= 0 ? 'media' : 'baja';
    return `<span class="racha__casilla racha__casilla--${grado}">${esc(p)}</span>`;
  });
  return `<span class="racha">${casillas.join('')}</span>`;
};

/** Cláusula con su candado: verde si te la pueden pagar, rojo si está protegida. */
export const clausula = (j) => {
  if (j.blindado) {
    return `<span class="clausula clausula--blindada">${icono('escudo', 'clausula__icono')}
      blindado</span>`;
  }
  const d = enDias(j.protegido_hasta);
  if (d) {
    return `<span class="clausula clausula--protegida">${icono('candadoCerrado', 'clausula__icono')}
      ${d} día${d === 1 ? '' : 's'}</span>`;
  }
  return `<span class="clausula clausula--libre">${icono('candadoAbierto', 'clausula__icono')}
    ${euros(j.clausula)}</span>`;
};

export const etiqueta = (texto, tono = '') =>
  `<span class="etiqueta${tono ? ` etiqueta--${tono}` : ''}">${esc(texto)}</span>`;

export const boton = (texto, { tono = '', accion = '', arg = '', bloque = false, off = false } = {}) =>
  `<button class="btn${tono ? ` btn--${tono}` : ''}${bloque ? ' btn--bloque' : ''}"
    ${accion ? `data-accion="${esc(accion)}"` : ''} ${arg ? `data-arg="${esc(arg)}"` : ''}
    ${off ? 'disabled' : ''}>${esc(texto)}</button>`;

/** `ancha` la saca del reparto en columnas de las pantallas grandes.

    Lo pide el campo de alineación: envuelto en una tarjeta queda fuera del alcance de
    `.principal > .campo`, así que ese `column-span` no le llega y el terreno de juego
    termina dentro de una columna de 26rem. */
export const tarjeta = (titulo, cuerpo,
                        { extra = '', pie = '', centrada = false, ancha = false } = {}) => `
  <section class="tarjeta${centrada ? ' tarjeta--centrada' : ''}${ancha ? ' tarjeta--ancha' : ''}">
    ${titulo ? `<h2 class="tarjeta__cabecera">${esc(titulo)}
      ${extra ? `<span class="tarjeta__extra u-label">${extra}</span>` : ''}</h2>` : ''}
    ${cuerpo}
    ${pie ? `<footer class="tarjeta__pie">${pie}</footer>` : ''}
  </section>`;

/** `lista` recoge los detalles largos: en el móvil, cinco avisos abiertos se
    comen la primera pantalla y dejan de leerse.

    `icono` es el nombre de uno de `PATHS`. Va en SVG y no en emoji: hereda el color del
    tono por `currentColor`, escala con la tipografía y se ve igual en cualquier sistema,
    mientras que un emoji depende de la fuente instalada. */
export const aviso = ({ tono = 'info', titulo, texto = '', lista = null, accion = '',
                        icono: nombreIcono = '' }) => `
  <article class="aviso aviso--${esc(tono)}">
    <div class="aviso__cuerpo">
      <p class="aviso__titulo">${nombreIcono ? icono(nombreIcono, 'aviso__icono') : ''}${esc(titulo)}</p>
      ${texto ? `<p class="aviso__texto">${esc(texto)}</p>` : ''}
      ${lista?.length ? `<details class="aviso__mas">
        <summary>ver ${lista.length} detalle${lista.length === 1 ? '' : 's'}</summary>
        <ul class="aviso__lista">${lista.map((l) => `<li>${esc(l)}</li>`).join('')}</ul>
      </details>` : ''}
    </div>
    ${accion}
  </article>`;

export const barra = (pct, tono = '') =>
  `<span class="barra"><span class="barra__relleno${tono ? ` barra__relleno--${tono}` : ''}"
    style="width:${Math.max(0, Math.min(100, pct))}%"></span></span>`;

/** Indicador de carga. Por VPN una petición tarda varios segundos, así que toda espera
 * larga necesita señal en pantalla.
 *
 * El balón va en imagen y no en emoji: un emoji depende de la fuente del sistema y puede
 * salir como un cuadro vacío.
 *
 * La animación se apaga entera con `prefers-reduced-motion`.
 */
export const cargando = (texto = 'Cargando…') => `
  <div class="cargando" role="status" aria-live="polite">
    <div class="cargando__balones">
      ${[0, 1, 2].map((i) => `
        <span class="cargando__hueco" style="--i:${i}">
          <span class="cargando__balon">${BALON}</span>
          <span class="cargando__sombra"></span>
        </span>`).join('')}
    </div>
    <p class="cargando__texto">${esc(texto)}</p>
  </div>`;

/* El balón es `web/img/balon.png`. No se retoca ni se sustituye por un SVG generado.
 * No lo produce ningún script, así que las condiciones del fichero se anotan aquí por si
 * hay que rehacerlo:
 *
 * - Va recortado al contenido y cuadrado exacto, o se deforma al escalarlo. 128 px
 *   cubren los 26 a los que se pinta, incluso a 3x.
 * - El fondo se quita por CONECTIVIDAD (relleno desde las cuatro esquinas) y no por
 *   umbral de color: un umbral se llevaría también el blanco de dentro del dibujo.
 *
 * `loading="eager"`: es lo primero que se ve al abrir. */
const BALON = `
  <img src="img/balon.png" width="26" height="26" alt="" aria-hidden="true"
       loading="eager" decoding="async">`;

export const vacio = (texto, icono = '·') =>
  `<div class="vacio"><p class="vacio__icono">${esc(icono)}</p><p>${esc(texto)}</p></div>`;

/* ---------- ficha del campo ---------- */

export const ficha = (j) => {
  if (j.hueco) {
    return `<div class="ficha ficha--hueco">
      <div class="ficha__foto">${icono('camiseta')}</div>
      <p class="ficha__nombre"><span>${esc(j.nombre || 'VACÍA')}</span></p></div>`;
  }
  /* `puntos: null` significa dos cosas y hay que separarlas: en el editor de once es
     «aquí no se enseñan puntos» (no se pinta nada), y en un once ya disputado es «su
     partido todavía no se ha jugado». Lo segundo se pide con `pendiente: true` y sale
     como un guion, porque en pantalla un 0 y un «aún no» se ven igual **y son lo
     contrario** al leer el once de un rival. */
  const p = j.puntos;
  const grado = p == null ? '' : p >= 6 ? ' ficha__puntos--alta' : p < 0 ? ' ficha__puntos--baja' : '';
  const marcador = p != null
    ? `<span class="ficha__puntos${grado}">${esc(p)}</span>`
    : j.pendiente
      ? '<span class="ficha__puntos ficha__puntos--pend" title="aún no ha jugado">-</span>'
      : '';
  /* En modo edición es un botón de verdad, no un div con un `onclick`: así se
     alcanza con el tabulador y el lector de pantalla dice que se puede pulsar. */
  const etiqueta = j.seleccionable
    ? ` aria-pressed="${!!j.seleccionada}" data-ptid="${esc(j.ptid)}"
        aria-label="${esc(j.nombre)}${j.seleccionada ? ', seleccionado' : ''}"`
    : '';
  const T = j.seleccionable ? 'button' : 'div';
  return `<${T} class="ficha${j.seleccionable ? ' ficha--elegible' : ''}${j.seleccionada ? ' ficha--elegida' : ''}"${etiqueta}>
    <div class="ficha__foto">
      ${marcador}
      ${j.etiqueta ? `<span class="ficha__marca">${esc(j.etiqueta)}</span>` : ''}
      ${j.foto ? `<img src="${esc(j.foto)}" alt="" loading="lazy" decoding="async">` : ''}
    </div>
    <p class="ficha__nombre"><span>${esc(j.nombre)}</span>${j.alineable === false ? CHECK_NO : CHECK_OK}</p>
  </${T}>`;
};

/* ---------- filas ---------- */

export const filaPlantilla = (j) => `
  <article class="fila">
    ${retrato(j.foto, j.nombre)}
    <div class="fila__cuerpo">
      <p class="fila__titulo">${chipPos(j.posicion)} ${esc(j.nombre)}</p>
      <p class="fila__sub">${dinero(j.valor)} · media ${esc((j.media ?? 0).toFixed(2).replace('.', ','))}</p>
      ${racha(j.racha)}
    </div>
    <div class="fila__fin">
      ${marcador(j.puntos_temporada)}
      ${clausula(j)}
      ${boton('Acciones', { tono: 'accion', accion: 'jugador', arg: j.player_team_id })}
    </div>
  </article>`;

export const filaMercado = (f) => `
  <article class="fila">
    ${retrato(f.foto, f.nombre)}
    <div class="fila__cuerpo">
      <p class="fila__titulo">${chipPos(f.posicion_id)} ${esc(f.nombre)}</p>
      <p class="fila__sub">${f.libre ? 'LALIGA' : esc(f.vendedor)} ·
        ${f.pujas ? `${f.pujas} ${f.libre ? 'pujas' : 'ofertas'}` : 'sin pujas'}</p>
      <p class="fila__sub u-dimmer">${icono('reloj', 'clausula__icono')} cierra ${fecha(f.expira)}</p>
    </div>
    <div class="fila__fin">
      ${marcador(f.puntos)}
      <span class="u-label">valor ${millones(f.valor)}</span>
      ${f.libre
        ? boton('Pujar', { tono: 'accion', accion: 'pujar', arg: f.market_id })
        : boton('Ofertar', { accion: 'ofertar', arg: f.market_id })}
    </div>
  </article>`;

/* El nombre enlaza a su plantilla, que es pública entera.
   El ancho del puesto va en `.fila__puesto`, no en un `style` inline: la regla del
   proyecto es que ningún estilo viva dentro del JS. */
export const filaClasificacion = (c, pos) => `
  <article class="fila${c.es_mio ? ' fila--mia' : ''}">
    <span class="u-label u-num fila__puesto">${pos}</span>
    <div class="fila__cuerpo">
      <p class="fila__titulo">${c.team_id
        ? `<a class="enlace-ficha" href="#manager/${esc(c.team_id)}">${esc(c.manager)}</a>`
        : esc(c.manager)}</p>
      <p class="fila__sub">${dinero(c.valor_plantilla)} · ${esc(c.num_jugadores)} fichas</p>
    </div>
    <div class="fila__fin">
      ${marcador(c.puntos_jornada)}
      <span class="u-num u-dim">${esc(c.puntos)} total</span>
    </div>
  </article>`;

export const filaEndpoint = (e) => `
  <article class="endpoint">
    <span class="endpoint__metodo endpoint__metodo--${esc(e.metodo.toLowerCase())}">${esc(e.metodo)}</span>
    <div class="u-crecer">
      <p class="endpoint__ruta">${esc(e.ruta)}</p>
      <p class="endpoint__desc">${esc(e.desc)}</p>
    </div>
    ${e.probar ? boton('Probar', { accion: 'probar', arg: e.ruta }) : ''}
  </article>`;

/* ---------- desplegable propio ----------
   Un `<select>` nativo se puede estilar por fuera, pero **la lista que despliega
   la pinta el sistema operativo**: las `<option>` no aceptan CSS en la práctica.
   En Android sale un panel gris claro que rompe el tema oscuro entero.

   Esto es el desplegable de la casa: un botón, una lista propia y un
   `<input type="hidden">` con el valor, para que quien lo lea siga haciendo
   `document.getElementById(id).value` exactamente igual que con un `<select>`.

   Reutilizable: `U.desplegable({ id, valor, opciones })` donde `opciones` es
   `[[valor, texto], …]` o `{valor: texto}`. */
export const desplegable = ({ id, valor, opciones, etiqueta = '' }) => {
  const pares = Array.isArray(opciones) ? opciones : Object.entries(opciones);
  const sel = pares.find(([v]) => String(v) === String(valor)) || pares[0] || ['', ''];
  return `
    <div class="desple" data-desple="${esc(id)}">
      ${etiqueta ? `<label class="u-label" for="${esc(id)}-btn">${esc(etiqueta)}</label>` : ''}
      <input type="hidden" id="${esc(id)}" value="${esc(sel[0])}">
      <button type="button" class="desple__btn" id="${esc(id)}-btn"
        aria-haspopup="listbox" aria-expanded="false">
        <span class="desple__valor">${esc(sel[1])}</span>
        ${icono('flecha-abajo', 'desple__flecha')}
      </button>
      <ul class="desple__lista" role="listbox" hidden>
        ${pares.map(([v, t]) => `
          <li role="option" class="desple__opcion" data-valor="${esc(v)}"
              aria-selected="${String(v) === String(sel[0])}">${esc(t)}</li>`).join('')}
      </ul>
    </div>`;
};
