/* ============================================================================
   vistas.js: una función por pestaña. Reciben datos ya pedidos y devuelven
   HTML montado con las piezas de ui.js. No hacen `fetch`, y el aspecto lo
   ponen las clases de css/: en línea solo van valores calculados.
   ========================================================================== */

import * as U from './ui.js';
import * as api from './api.js';
import * as B from './buscador.js';
import * as E from './once.js';
import { escudo, nombreCorto, nombreEquipo, nombreManager, soyYo } from './equipos.js';

/* Índice de jugadores del maestro público: foto, equipo y puntos por jornada.
   Se llena una vez al arrancar (app.js) y lo comparten todas las vistas. */
export const ctx = { porNick: new Map(), porId: new Map() };

export function indexarMaestro(maestro, fotosDemo = null) {
  ctx.porNick.clear(); ctx.porId.clear();
  for (const p of maestro) {
    const ficha = {
      id: p.id,
      nombre: p.nickname,
      posicion_id: Number(p.positionId),
      team_id: Number(p.teamId),
      foto: fotosDemo?.[p.nickname] ?? p.image,
      valor: Number(p.marketValue),
      puntos: p.points,
      media: p.averagePoints,
      estado: p.playerStatus,
/* Indexado por JORNADA, no por posición en el array: `weekPoints` no viene ordenado y a
   quien se ficha tarde le faltan jornadas. Recorrerlo tal cual pinta la racha en otro
   orden. Los huecos quedan vacíos, que es la verdad. */
      racha: (p.weekPoints || []).reduce((acc, w) => {
        acc[w.weekNumber - 1] = w.points;
        return acc;
      }, []),
    };
    ctx.porNick.set(p.nickname, ficha);
    ctx.porId.set(String(p.id), ficha);
  }
}

const jug = (nombre) => ctx.porNick.get(nombre) || {};
const foto = (nombre) => jug(nombre).foto || null;
const equipoDe = (nombre) => {
  const t = jug(nombre).team_id;
  return t ? nombreEquipo(t): '-';
};

/* Mis ligas */

export const clasifUI = { jornada: null, datos: null, cargando: false };
export const puntosUI  = { jornada: null, datos: null, cargando: false };

/* Quién ganó cada jornada. La ruta es `standing/{N}`, sin la palabra `week`. */
function bloqueClasifJornada(estado) {
  const actual = estado?.jornada?.numero || 1;
  /* Se arranca en `actual - 1`: `weekNumber` apunta a la PRÓXIMA jornada en cuanto se
     cierra la anterior, así que pedir la actual antes de que abra devuelve el once
     preparado con once ceros, como si nadie hubiera puntuado. */
  const n = clasifUI.jornada ?? Math.max(1, actual - 1);
  const pills = Array.from({ length: Math.max(1, actual) }, (_, i) => i + 1).map((j) => `
    <button class="jornada-pill" data-accion="ver-clasif" data-arg="${j}"
      aria-selected="${j === n}">J${j}</button>`).join('');

  const d = clasifUI.datos;
  const cuerpo = clasifUI.cargando ? U.cargando()
    : !d ? '<p class="vacio">Elige una jornada.</p>'
    : (d.clasificacion || []).map((c, i) => `
        <article class="fila${c.es_mio ? ' fila--mia' : ''}">
          <span class="u-label u-num fila__puesto">${i + 1}</span>
          <div class="fila__cuerpo">
            <p class="fila__titulo">${c.team_id
              ? `<a class="enlace-ficha" href="#manager/${U.esc(c.team_id)}">${U.esc(c.manager || '?')}</a>`
              : U.esc(c.manager || '?')}</p>
            <p class="fila__sub">${c.puntos_totales ?? '-'} totales</p>
          </div>
          <div class="fila__fin">${U.marcador(c.puntos_jornada ?? 0)}</div>
        </article>`).join('');

  return U.tarjeta('Quién ganó cada jornada', `
    <div class="jornada-pills">${pills}</div>${cuerpo}`, {
    extra: `J${n}`,
      pie: 'La clasificación DE esa jornada, no la acumulada.' });
}

export function vistaLiga({ estado, informe, calendario }) {
  const e = estado.equipo, c = estado.caja, j = estado.jornada;
  const subeBaja = e.posicion < e.posicion_anterior ? 'ok' : e.posicion > e.posicion_anterior ? 'crit' : '';

  const cabeceraLiga = `
    <article class="fila fila--mia">
      <div class="fila__cuerpo">
        <p class="fila__titulo">${U.esc(estado.liga?.nombre || 'Tu liga')}</p>
        <p class="fila__sub"><span class="u-num">${e.posicion}/${estado.clasificacion?.length || '?'}</span> ·
          <span class="u-num">${e.puntos_totales}</span> PFSY ·
          ${e.num_jugadores} fichas</p>
      </div>
      <div class="fila__fin">${U.dinero(c.total)}
        <span class="u-label">${U.millones(c.disponible_tras_reserva)} libres</span></div>
    </article>`;

  const avisos = avisosDe(informe, estado).map(U.aviso).join('');
  const porJornada = bloqueClasifJornada(estado);
  const cajaRival = bloqueCajaRival();

  const clasificacion = estado.clasificacion
    .map((m, i) => U.filaClasificacion(m, i + 1)).join('');

  const partidos = (calendario || []).map((m) => {
    const jugado = m.matchState === 7, vivo = m.matchState === 2 || m.matchState === 3;
    const mios = (informe.enfrentamientos?.jugadores || [])
      .filter((x) => x.team_id === m.localId || x.team_id === m.visitorId).length;
    /* Escudo + nombre, como la app. El escudo se pide al CDN de LaLiga igual que las
       fotos: lo carga el navegador, no gasta cuota de la API. */
    const lado = (id, derecha = false) => {
      const img = escudo(id);
      const nom = `<span class="u-trunc">${U.esc(nombreEquipo(id))}</span>`;
      const esc_ = img ? `<img class="escudo" src="${U.esc(img)}" alt="" loading="lazy">` : '';
      return derecha ? esc_ + nom : nom + esc_;
    };
    return `<article class="partido">
      <span class="partido__local u-fila">${lado(m.localId)}</span>
      <span class="partido__marcador">${jugado || vivo ? `${m.localScore ?? 0}-${m.visitorScore ?? 0}` : U.fecha(m.matchDate, false)}</span>
      <span class="u-fila">${lado(m.visitorId, true)}</span>
      <span class="partido__estado">${jugado ? 'finalizado' : vivo ? 'en juego' : U.fecha(m.matchDate)}
        ${mios ? ` · ${mios} jugador${mios === 1 ? '' : 'es'} tuyo${mios === 1 ? '' : 's'}` : ''}</span>
    </article>`;
  }).join('');

  return `
    ${avisos ? `<div class="u-pila-sm">${avisos}</div>` : ''}
    ${U.tarjeta('', cabeceraLiga)}
    <div class="rejilla">
      ${U.tarjeta('Clasificación', clasificacion, {
        extra: `jornada ${j.numero}`,
        pie: subeBaja === 'crit'
          ? `Has bajado del ${e.posicion_anterior}.º al ${e.posicion}.º puesto.`
          : `Vas ${e.posicion}.º de ${estado.clasificacion?.length || '?'}.`,
      })}
      ${cajaRival}
      ${U.tarjeta(`Jornada ${j.numero}`, partidos || U.vacio('Sin partidos'), {
        extra: j.en_juego ? 'en juego' : 'cerrada',
      })}
      ${porJornada}
    </div>`;
}

/* Equipo */

const LINEAS = [['goalkeeper', 1], ['defender', 2], ['midfield', 3], ['striker', 4]];

export function vistaEquipo(sub, datos) {
  if (sub === 'Calendario') return vistaCalendario(datos);
  if (sub === 'Óptimo') return vistaOptimo(datos);
  if (sub === 'En vivo') return vistaEnVivo(datos);
  const { estado, alineacion, informe, candado } = datos;
  const meta = `
    <div class="meta">
      <div><p class="u-label">Fichas</p><p class="meta__valor u-num">${estado.equipo.num_jugadores}/24</p></div>
      <div><p class="u-label">Jornada ${estado.jornada.numero}</p>
        <p class="meta__valor"><span class="meta__punto">●</span> ${U.fecha(estado.jornada.abre)}</p></div>
      <div class="meta__ultimo"><p class="u-label">Valor equipo</p>
        <p class="meta__valor u-num">${U.euros(estado.equipo.valor_plantilla)}</p></div>
    </div>`;

  if (sub === 'Plantilla') {
    const orden = { POR: 0, DEF: 1, MED: 2, DEL: 3 };
    const filas = [...estado.plantilla]
      .sort((a, b) => (orden[a.posicion] - orden[b.posicion]) || b.puntos_temporada - a.puntos_temporada)
      .map((p) => U.filaPlantilla({
        ...p,
        foto: foto(p.nombre),
        media: jug(p.nombre).media ?? 0,
        racha: jug(p.nombre).racha,
      })).join('');
    return meta + U.tarjeta('', filas);
  }

  if (sub === 'Puntos') {
    /* El once que se pinta es el de la píldora elegida; por defecto, la jornada en curso.
       Es el once CONGELADO de esa jornada, que es el que puntuó de verdad. */
    const jr = puntosUI.datos || informe.jornada;
    const pills = Array.from({ length: estado.jornada.numero }, (_, i) => i + 1).map((n) => `
      <button class="jornada-pill" data-accion="ver-once-jornada" data-arg="${n}"
        aria-selected="${n === (puntosUI.jornada ?? estado.jornada.numero)}">
        <span>J${n}</span>${n === estado.jornada.numero ? `<span class="jornada-pill__pts">${jr.puntos_oficiales_jornada}</span>` : ''}
      </button>`).join('');

    const porPos = { POR: [], DEF: [], MED: [], DEL: [] };
    for (const t of jr.titulares) porPos[t.posicion]?.push(t);

  /* El hueco solo se marca donde de verdad falta alguien, y el mínimo sale de lo que
     exigen las 7 formaciones (1 POR, 3 DEF, 3 MED, 1 DEL), no de una formación concreta:
     rellenar hasta un 3-5-2 fijo pinta huecos inventados en cualquier otra.

     Lo que hay que marcar es un once INCOMPLETO de verdad, que anula la jornada. */
    const MINIMO = { POR: 1, DEF: 3, MED: 3, DEL: 1 };
    const campo = Object.entries(porPos).map(([pos, lista]) => {
      const fichas = lista.map((t) => U.ficha({ nombre: t.nombre, foto: foto(t.nombre), puntos: t.puntos }));
      while (fichas.length < MINIMO[pos]) fichas.push(U.ficha({ hueco: true, nombre: 'VACÍA' }));
      return fichas.length ? `<div class="campo__linea">${fichas.join('')}</div>` : '';
    }).join('');

    /* Y si aun cumpliendo los mínimos no llegan a once, los que faltan van en una fila
       aparte: no se puede saber de qué línea eran, y colocarlos a ojo sería inventar. */
    const sinCubrir = Math.max(0, 11 - jr.titulares.length);
    const fila_extra = sinCubrir
      ? `<div class="campo__linea">${Array.from({ length: sinCubrir },
          () => U.ficha({ hueco: true, nombre: 'VACÍA' })).join('')}</div>`
      : '';

    return meta + `
      <div class="jornadas">${pills}</div>
      ${jr.once_completo ? '' : U.aviso({
        tono: 'crit',
        titulo: `El once se congeló con ${jr.num_titulares} jugadores`,
        texto: `Tus titulares suman ${jr.puntos_sumados_del_once} puntos y el marcador oficial marca `
             + `${jr.puntos_oficiales_jornada}. Un once incompleto anula la jornada entera.`,
      })}
      ${U.tarjeta('', `<div class="campo"><div class="campo__lineas"></div><div class="campo__centro"></div>${campo}${fila_extra}</div>`, {
        ancha: true,
        pie: jr.por_jugar?.length
          ? `Aún por jugar: ${jr.por_jugar.join(', ')}.`
          : 'Todos los titulares han jugado ya.',
      })}`;
  }

  /* Alineación: la que cuenta para la PRÓXIMA jornada, se edite o no. */
  const v = informe.validacion_once;
  const objetivo = estado.jornada.en_juego ? estado.jornada.siguiente : estado.jornada.numero;

  /* Con la jornada abierta, el once que puntúa es el congelado al abrir: lo que
     se toque aquí es para la siguiente, y el aviso lo dice en pantalla. */
  const congelado = estado.jornada.en_juego ? U.aviso({
    tono: 'info',
    titulo: `Esto es el once de la jornada ${objetivo}`,
    texto: `La ${estado.jornada.numero} está en juego y su once se congeló al abrirse: `
         + 'lo que cambies ahora no la afecta.',
  }) : '';

  if (E.editor.activo) return meta + congelado + vistaEditor(estado, objetivo);

  const campo = LINEAS.map(([clave]) => {
    const fichas = (alineacion.formation?.[clave] ? [].concat(alineacion.formation[clave]) : [])
      .map((p) => {
        const pm = p.playerMaster || {};
        return U.ficha({
          nombre: pm.nickname,
          foto: pm.images?.transparent?.['256x256'] || foto(pm.nickname),
          alineable: pm.playerStatus === 'ok',
        });
      });
    return fichas.length ? `<div class="campo__linea">${fichas.join('')}</div>` : '';
  }).join('');

  const suplentes = estado.plantilla.filter((p) => !titularesActuales(alineacion).has(String(p.player_team_id)));
  const banquillo = suplentes.length
    ? `<div class="banco"><div class="banco__grupo"><p class="u-label">Banquillo</p>
        <div class="banco__fichas">${suplentes.map((p) => U.ficha({
          nombre: p.nombre, foto: foto(p.nombre), alineable: p.estado === 'ok',
        })).join('')}</div></div></div>`
    : '<p class="banquillo__aviso u-dim">Todos tus jugadores están en el once.</p>';

  /* El candado: mientras está echado, ni el cron recalcula ni se puede vender a un
     titular. Se enseña arriba porque cambia lo que las demás acciones pueden hacer. */
  const cerrado = candado?.once?.bloqueado;
  const panelCandado = U.aviso({
    tono: cerrado ? 'ok' : 'warn',
    icono: cerrado ? 'candadoCerrado' : 'candadoAbierto',
    titulo: cerrado ? 'Once bloqueado' : 'Once sin bloquear',
    texto: cerrado
      ? `${candado.once.motivo || 'sin motivo'} · ${candado.once.titulares_protegidos} titulares `
        + 'protegidos: el bot no recalcula y no se puede vender a ninguno.'
      : 'El bot puede recalcularlo en su próxima pasada y una venta podría dejarlo en 10.',
    accion: U.boton(cerrado ? 'Abrir candado' : 'Bloquear once',
      { tono: cerrado ? '' : 'accion', accion: cerrado ? 'abrir-candado' : 'cerrar-candado' }),
  });

  return meta + congelado + panelCandado + `
    <div class="u-fila">
      <span class="etiqueta">${(v.formacion || []).join(' · ') || '-'}</span>
      <span class="etiqueta ${v.valido ? 'etiqueta--ok' : 'etiqueta--crit'}">
        ${v.valido ? 'once válido' : 'once inválido'}</span>
      <span class="u-crecer"></span>
      ${U.boton('Recalcular', { accion: 'recalcular-once' })}
      ${U.boton('Editar once', { tono: 'accion', accion: 'editar-once' })}
    </div>
    ${U.tarjeta('', `<div class="campo"><div class="campo__lineas"></div><div class="campo__centro"></div>${campo}</div>`, { ancha: true })}
    ${banquillo}
    <p class="guardado"><span class="u-label">Alineación guardada el</span><br>
      <span class="guardado__cuando">${U.fecha(v.actualizado)}</span></p>`;
}

const titularesActuales = (alineacion) => new Set(
  ['goalkeeper', 'defender', 'midfield', 'striker']
    .flatMap((k) => [].concat(alineacion.formation?.[k] || []))
    .map((p) => String(p.playerTeamId)));

/** El modo edición. Se separa para que la vista de solo lectura siga siendo la
    de siempre y no haya que leer condicionales para entenderla. */
function vistaEditor(estado, objetivo) {
  const plantilla = estado.plantilla;
  const fallos = E.problemas(plantilla);
  const bloqueado = fallos.some((f) => !f.includes('puntúan 0'));

  return `
    ${E.pintarControles(plantilla)}
    ${E.editor.aviso ? U.aviso({ tono: 'warn', titulo: 'No se puede hacer ese cambio', texto: E.editor.aviso }) : ''}
    ${U.tarjeta('', E.pintarCampo(plantilla, foto), {
      ancha: true,
      pie: E.editor.seleccion
        ? 'Ahora toca otro jugador de la misma posición para intercambiarlos.'
        : 'Toca un jugador para elegirlo. Solo se intercambian jugadores de la misma posición.',
    })}
    ${E.pintarBanquillo(plantilla, foto)}
    ${fallos.length ? U.aviso({
      tono: bloqueado ? 'crit' : 'warn',
      titulo: bloqueado ? 'Este once no se puede guardar' : 'Revisa antes de guardar',
      lista: fallos,
    }) : ''}
    <div class="u-fila">
      ${U.boton('Cancelar', { accion: 'cancelar-once' })}
      <span class="u-crecer"></span>
      ${U.boton(E.editor.guardando ? 'Guardando…' : `Guardar para la J${objetivo}`,
        { tono: 'accion', accion: 'guardar-once', off: bloqueado || E.editor.guardando })}
    </div>
    <p class="guardado"><span class="u-label">Al guardar</span><br>
      <span class="guardado__cuando">se manda a LaLiga y se <b>fija</b>, para que el bot no lo
      recalcule y borre tu decisión.</span></p>`;
}

/* Eventos del tablón
   El `1` y el `6` se distinguen por su forma: un `6` no trae jugador, y un `1` trae
   `user2Id` con la víctima, que es lo que separa un clausulazo de una compra al sistema. */

/** Un porcentaje con su signo, o "sin serie" si no hay dato.
    Un recién llegado al mercado no tiene retorno a 3 ni a 7 días: con menos de cuatro
    días de valores, ese hueco es `null` y pintarlo daría "3d null%". */
const pct = (v) => (v === null || v === undefined ? 'sin serie'
                                                  : `${v >= 0 ? '+' : ''}${v}%`);

export const TIPOS_EVENTO = {
  1:  { titulo: 'Clausulazo',           tono: 'crit' },
  4:  { titulo: 'Blindaje',             tono: 'info' },
  6:  { titulo: 'Recompensa',           tono: 'ok'   },
  7:  { titulo: 'Jornada anulada',      tono: 'crit' },
  8:  { titulo: 'Jornada anulada',      tono: 'crit' },
  9:  { titulo: 'Alta en la liga',      tono: ''     },
  10: { titulo: 'Baja de mánager',      tono: ''     },
  31: { titulo: 'Operación de mercado', tono: ''     },
  32: { titulo: 'Jornada anulada',      tono: 'crit' },
  33: { titulo: 'Operación de mercado', tono: ''     },
};

/* Cuántas peticiones al día se consideran aceptables. No es un límite de LaLiga ni frena
   nada: es el listón contra el que se lee el contador, para notar un salto raro. El freno
   de verdad es el ritmo (`rate_limit_rps`), que es lo que mira un WAF. */
export const PRESUPUESTO_DIARIO = 1000;

/* Qué política se está editando. Vive fuera de la vista porque pintar es una función
   pura: el estado de la interfaz no puede depender de lo que se pintó la vez anterior. */
export const ventasUI = { abierto: null };

/* El editor. Se despliega DENTRO de la fila y no en un modal, por lo mismo que los
   procesos: así se ve a quién se le está cambiando la regla. */
export function editorPolitica(nombre, p, modos) {
  const opciones = Object.keys(modos || {}).map((m) => [m, m]);
  return `<div class="tarjeta__cuerpo">
    <p class="u-label">Política de venta de ${U.esc(nombre)}</p>
    ${U.desplegable({ id: 'pol-modo', valor: p.modo, opciones, etiqueta: 'Cuándo cobrar' })}
    <p class="fila__sub u-dim">${U.esc(modos?.[p.modo] || '')}</p>
    <label class="u-label" for="pol-prima">Prima mínima (%), solo en modo «prima»</label>
    <input class="campo-texto" id="pol-prima" type="number" step="0.5" min="-10" max="50"
           value="${Number(p.prima_min ?? 3)}">
    <label class="u-fila"><input id="pol-congelar" type="checkbox"
      ${p.no_antes_de_congelar ? 'checked' : ''}>
      <span>No cobrar hasta que la jornada haya arrancado</span></label>
    <label class="u-fila"><input id="pol-relistar" type="checkbox"
      ${p.relistar ? 'checked' : ''}>
      <span>Volver a listarlo si la ficha caduca sin venderse</span></label>
    <div class="u-fila">
      ${U.boton('Guardar', { tono: 'accion', accion: 'guardar-politica', arg: U.esc(nombre) })}
      ${U.boton('Cancelar', { accion: 'cerrar-politica' })}
    </div>
  </div>`;
}

/* Una política, dicha en una línea. Se lee más veces de las que se edita, así que lo que
   importa es que se entienda de un vistazo qué va a pasar con la próxima oferta. */
export function resumenPolitica(p) {
  const cuando = p.no_antes_de_congelar ? ' · no antes de que abra la jornada' : '';
  const re = p.relistar ? ' · se vuelve a listar si caduca' : ' · NO se relista';
  const base = {
    avisar:    'no se cobra sola: te llega para aprobar',
    valor:     'se cobra al llegar al valor de mercado',
    inmediata: 'se cobra la mejor del sistema, sin esperar',
  }[p.modo] || `se cobra a partir de ${(+p.prima_min || 0).toFixed(1)}% sobre el valor`;
  const marca = p.por_defecto ? U.etiqueta('por defecto', '') : '';
  return `${marca} <span class="u-dim">${U.esc(base + cuando + re)}</span>`;
}

/** Un evento del tablón en una frase. Nunca imprime `undefined`: si un tipo es nuevo
    lo dice con su número, que es la pista para ir a mirarlo. */
export function describirEvento(a) {
  const p = ctx.porId.get(String(a.playerMasterId));
  const jugador = p?.nombre || (a.playerMasterId ? `jugador ${a.playerMasterId}` : null);
  const quien = soyYo(a.user1Id) ? 'Tú' : nombreManager(a.user1Id);
  const importe = a.amount ? `${U.euros(a.amount)} €` : null;
  const meta = TIPOS_EVENTO[a.activityTypeId];

  const frase = {
    1:  () => `${quien} ha clausulado a ${jugador} a ${nombreManager(a.user2Id)} por ${importe}`,
    4:  () => `${quien} ha blindado a ${jugador}`,
    6:  () => `En la jornada ${a.weekNumber}, ${quien} ha ganado ${importe}`,
    31: () => `${quien} ha comprado a ${jugador} a LALIGA por ${importe}`,
    33: () => `${quien} ha vendido a ${jugador} a LALIGA por ${importe}`,
    7:  () => `${quien} NO puntuó la jornada ${a.weekNumber}: alineación incompleta`,
    8:  () => `${quien} NO puntuó la jornada ${a.weekNumber}: saldo negativo`,
    9:  () => `${quien} se ha unido a la liga`,
    10: () => 'Un mánager ha dejado la liga',
    32: () => `${quien} NO puntuó la jornada ${a.weekNumber}: alineación incorrecta`,
  }[a.activityTypeId];

  return {
    titulo: meta?.titulo || `Evento tipo ${a.activityTypeId} (sin identificar)`,
    tono: meta?.tono || 'warn',
    texto: frase ? frase() : `${quien}${importe ? ` · ${importe}` : ''}${jugador ? ` · ${jugador}` : ''}`,
    foto: p?.foto, jugador, mio: soyYo(a.user1Id) || soyYo(a.user2Id),
    cuando: a.createdAt,
  };
}

/* Mercado */

export function vistaMercado(sub, datos) {
  if (sub === 'Momentum')  return vistaMomentum(datos);
  if (sub === 'Trading')   return vistaTrading();
  if (sub === 'Tácticas')  return vistaTacticas();
  if (sub === 'Jugadores') return vistaJugadores();
  if (sub === 'Histórico') return vistaHistMercado(datos);
  const { mercado, informe, estado } = datos;
  if (sub === 'Buscar') return B.pintarBuscador(foto);

  if (sub === 'Mis Ops.') {
    /* La app lo parte en Compra y Venta. Aquí lo mismo, porque son dos cosas
       distintas: lo que tienes comprometido y lo que estás intentando soltar. */
    /* `f.playerMaster?.nickname` y no `String(f.playerMaster || {})`: `String(undefined)`
       devuelve la cadena "undefined", que es cierta: esa guarda no corta nada y la línea de
       debajo revienta igual sin `playerMaster`. */
    const mias = mercado.filter((f) => f.discr === 'marketPlayerTeam'
      && f.playerMaster?.nickname
      && estado.plantilla.some((p) => p.nombre === f.playerMaster.nickname));

    /* La política de cada uno: es lo que decide si una oferta se cobra sola o te llega
       para aprobarla. Sin verla aquí, al lado de la ficha, hay que acordarse de qué se
       dejó puesto. */
    const pols = new Map((datos.ventas?.politicas || []).map((p) => [p.nombre, p]));
    const PORDEF = datos.ventas?.por_defecto || {};

    const ventas = mias.map((f) => {
      const pm = f.playerMaster;
      const ofertas = f.numberOfOffers || 0;
      const p = pols.get(pm.nickname) || { ...PORDEF, por_defecto: true };
      return `<article class="fila">
        ${U.retrato(pm.images?.transparent?.['256x256'] || foto(pm.nickname), pm.nickname)}
        <div class="fila__cuerpo">
          <p class="fila__titulo">${U.chipPos(Number(pm.positionId))} ${U.esc(pm.nickname)}</p>
          <p class="fila__sub">pides ${U.millones(f.salePrice)} · vale ${U.millones(pm.marketValue)}</p>
          <p class="fila__sub u-dimmer">expira ${U.fecha(f.expirationDate)}</p>
          <p class="fila__sub">${resumenPolitica(p)}</p>
        </div>
        <div class="fila__fin">
          ${ofertas ? U.etiqueta(`${ofertas} oferta${ofertas === 1 ? '' : 's'}`, 'ok')
                    : U.etiqueta('sin ofertas', 'warn')}
          ${U.boton('Política', { accion: 'politica-venta', arg: U.esc(pm.nickname) })}
          ${U.boton('Retirar', { accion: 'retirar-mercado', arg: String(f.id) })}
        </div>
      </article>
      ${ventasUI.abierto === pm.nickname
        ? editorPolitica(pm.nickname, p, datos.ventas?.modos) : ''}`;
    }).join('');

    const ofertasRecibidas = (datos.misOfertas?.ofertas || []).map((o) => `
      <div class="fila-rival">
        <div class="fila-rival__ppal">
          <div class="fila-rival__nom">
            ${U.chipPos(o.posicion)}
            <a class="enlace-ficha" href="#jugador/${o.player_id}">${U.esc(o.jugador || '?')}</a>
            ${U.etiqueta(o.del_sistema ? 'del sistema' : `de ${o.de || 'un rival'}`,
              o.del_sistema ? 'ok' : 'warn')}
          </div>
          <div class="fila-rival__datos">
            <span><b>${U.millones(o.importe)}</b></span>
            <span class="u-dim">vale ${U.millones(o.valor)}</span>
            <span class="${o.importe >= o.valor ? 'es-sube' : 'es-baja'}">${
              o.valor ? `${((o.importe - o.valor) / o.valor * 100).toFixed(1)}%` : ''}</span>
          </div>
        </div>
        <div class="fila-rival__acc u-fila">
          ${U.boton('Aceptar', { tono: 'accion', accion: 'aceptar-oferta',
            arg: `${o.market_id}:${o.oferta_id}:${o.importe}` })}
          ${U.boton('Rechazar', { accion: 'rechazar-oferta',
            arg: `${o.market_id}:${o.oferta_id}` })}
        </div>
      </div>`).join('');

    const invertido = estado.caja.comprometido_en_pujas ?? 0;
    return `
      ${U.aviso({
        tono: 'info',
        titulo: 'Vender NO es inmediato',
          texto: 'Listar solo pone la ficha. LaLiga manda su oferta cuando quiere y '
               + 'puede tardar días: no cuentes con ese dinero para una puja de hoy.',
      })}
      ${U.tarjeta('Ofertas sobre la mesa',
        ofertasRecibidas || U.vacio('Nadie ha ofertado por tus jugadores'), {
        extra: `${(datos.misOfertas?.ofertas || []).length}`,
        pie: 'Las del SISTEMA son las que llegan solas y pagan cerca del valor. Una de un '
           + 'rival se puede rechazar sin coste: venderle es financiarle.',
      })}
      ${U.tarjeta('Ventas en curso', ventas || U.vacio('No tienes nada listado'), {
        extra: `${mias.length}`,
        pie: 'Las fichas caducan a los 3 días. Un jugador fuera de la liga se puede listar '
           + 'igual y recibir ofertas, pero su valor ya no se recalcula: no se recupera.',
      })}
      ${U.tarjeta('Compras', U.vacio('No has hecho ninguna oferta de compra'), {
        extra: invertido ? U.millones(invertido) + ' comprometidos' : 'sin compromisos',
        pie: 'Lo comprometido en pujas sale de teamInvestment y NO se resta de teamMoney: '
           + 'el saldo real es la diferencia.',
      })}`;
  }

  const libres = informe.mercado.agentes_libres;
  const fichas = mercado.map((f) => {
    const pm = f.playerMaster;
    const libre = f.discr === 'marketPlayerLeague';
    const extra = libres.find((x) => x.nombre === pm.nickname);
    return U.filaMercado({
      nombre: pm.nickname,
      posicion_id: Number(pm.positionId),
      foto: pm.images?.transparent?.['256x256'],
      valor: Number(pm.marketValue),
      puntos: pm.points,
      libre,
      vendedor: f.sellerName || 'un rival',
      pujas: libre ? f.numberOfBids : f.numberOfOffers,
      expira: f.expirationDate,
      market_id: f.id,
      asequible: extra?.asequible,
    });
  }).join('');

  return `
    ${U.aviso({
      tono: 'info',
      titulo: 'Los dos mercados no se compran igual',
      texto: 'Un agente libre se puja y la subasta resuelve sola a las 13:48. Una ficha '
           + 'de otro manager se oferta, y decide él: ni está garantizada ni es privada.',
    })}
    ${U.tarjeta('Mercado', fichas || U.vacio('Mercado vacío'), {
      extra: `${U.millones(informe.mercado.caja_libre)} libres`,
    })}`;
}

/* Radar */

/* Ventanas de clausulazo de toda la liga
   Las dos protecciones (14 días de compra, 24 h de blindaje) vencen a una hora concreta
   y conocida, así que la ventana de ataque de la liga entera es predecible sin mirar
   jugador por jugador. */
function vistaVentanas({ ventanas }) {
  if (!ventanas) {
    return U.aviso({ tono: 'warn', titulo: 'No se pudieron leer las ventanas',
      texto: 'Hace falta recorrer las cuatro plantillas; si la API va lenta, falla.' });
  }
  const v = ventanas;
  const g = v.ventana_global || {};

  const filaObj = (f, conHoras) => `
    <div class="fila-vent ${f.es_mio ? 'fila-vent--mia' : ''}">
      <div class="fila-vent__ppal">
        ${U.chipPos(f.posicion)}
        <a class="fila-vent__nombre enlace-ficha" href="#jugador/${f.player_id || ''}"
           >${U.esc(f.jugador || '?')}</a>
        <span class="fila-vent__eq">${U.esc(f.equipo)}</span>
      </div>
      <div class="fila-vent__datos">
        <span>${f.puntos} pts</span>
        <span>${U.millones(f.clausula)}</span>
        ${f.ratio ? `<span class="${f.ratio <= 1.05 ? 'es-mal-txt' : ''}">×${f.ratio}</span>` : ''}
        ${conHoras ? `<span class="fila-vent__reloj">${f.horas_para_liberarse < 1
          ? 'en menos de 1 h' : `en ${f.horas_para_liberarse} h`}</span>` : ''}
      </div>
      ${conHoras ? `<p class="fila-vent__nota">${U.esc(f.motivo)}${
        f.en_ventana_de_jornada ? '' : ' · ⚠ se libera con los clausulazos ya cerrados' /* sin-rastro-ok: se pinta en pantalla */}</p>` : ''}
      ${!conHoras && !f.es_mio ? `<div class="fila-vent__pie">
        ${U.boton('Clausular', { tono: 'accion', accion: 'clausulazo', arg: f.jugador })}</div>` : ''}
    </div>`;

  return `
    ${g.abierta === false
      ? U.aviso({ tono: 'crit', titulo: 'Clausulazos CERRADOS',
          // No decir "hasta que termine la jornada": se puede clausular CON la jornada en
          // curso. Lo que cierra la ventana son las 24 h previas a la SIGUIENTE.
          texto: `Estamos dentro de las 24 h previas a la jornada ${g.jornada}; `
               + `se reabren en cuanto empiece.` })
      : U.aviso({ tono: g.horas_restantes < 8 ? 'warn' : 'info',
          titulo: `Quedan ${Math.round(g.horas_restantes || 0)} h de ventana`,
          texto: `Los clausulazos se desactivan 24 h antes de la jornada (regla 030.01.17). `
               + `Se cierran el ${U.fecha(g.clausulazos_hasta)}. Tu caja: ${U.millones(v.mi_caja)}.` })}

    ${U.tarjeta('Puedo atacar ahora', v.puedo_atacar_ya.length
      ? v.puedo_atacar_ya.map((f) => filaObj(f, false)).join('')
      : '<p class="vacio">Ninguno de los objetivos libres cabe en tu caja.</p>',
      { extra: `${v.puedo_atacar_ya.length}`,
        pie: 'Solo los que NO están protegidos ni blindados y cuya cláusula te cabe. '
           + 'El ratio en rojo (≤1,05) marca a los que se pagan casi por su valor.' })}

    ${U.tarjeta('Se liberan en 48 h', v.se_liberan_pronto.length
      ? v.se_liberan_pronto.map((f) => filaObj(f, true)).join('')
      : '<p class="vacio">Nadie se libera en las próximas 48 h.</p>',
      { extra: `${v.se_liberan_pronto.length}`,
        pie: 'La agenda de las próximas horas, calculada de las dos protecciones.' })}

    ${U.tarjeta('Me quedo expuesto', v.me_quedo_expuesto.length
      ? v.me_quedo_expuesto.map((f) => filaObj(f, true)).join('')
      : '<p class="vacio">Ninguno de los tuyos queda desprotegido en 48 h.</p>',
      { extra: `${v.me_quedo_expuesto.length}`,
        pie: 'El reverso: cuándo te pueden atacar a ti. Blindar es gratis y dura 24 h.' })}`;
}

export function vistaRadar(sub, { estado, informe, tareas, ventanas }) {
  if (sub === 'Ventanas') return vistaVentanas({ ventanas });
  const ex = informe.exposicion;
  const rival = ex.puede_pagar_hasta;

  const expuestos = [...estado.plantilla]
    .sort((a, b) => a.ratio - b.ratio)
    .map((p) => {
      const alcance = p.clausula <= rival;
      const libre = !p.blindado && !U.enDias(p.protegido_hasta);
      const riesgo = alcance && libre;
      /* La barra es la INVERSA del ratio: cuanto más barato sale robarlo en proporción
         a lo que vale, más llena. Un ratio 1,00 la deja al 100 %. */
      const pct = Math.min(100, (1 / p.ratio) * 100);
      return `<article class="fila">
        ${U.retrato(foto(p.nombre), p.nombre, 'retrato--sm')}
        <div class="fila__cuerpo">
          <p class="fila__titulo">${U.chipPos(p.posicion)} ${U.esc(p.nombre)}</p>
          <p class="fila__sub">${U.millones(p.clausula)} de cláusula · vale ${U.millones(p.valor)}</p>
          ${U.barra(pct, riesgo ? 'crit' : libre ? 'warn' : 'ok')}
        </div>
        <div class="fila__fin">
          ${riesgo ? U.etiqueta('a su alcance', 'crit') : U.clausula(p)}
          <span class="u-label u-num">×${p.ratio.toFixed(2).replace('.', ',')}</span>
        </div>
      </article>`;
    }).join('');

  const cajas = ex.caja_rival_estimada.map((r) => `
    <article class="fila">
      <div class="fila__cuerpo"><p class="fila__titulo">${U.esc(r.manager)}</p></div>
      <span class="u-num">${U.millones(r.estimado)}</span>
    </article>`).join('');

  /* `|| []`: en la primera fase de carga `tareas` todavía es null. */
  const programadas = (tareas || []).filter((t) => t.estado === 'pendiente').map((t) => `
    <article class="fila">
      <div class="fila__cuerpo">
        <p class="fila__titulo">${U.esc(t.accion)} · ${U.esc(String(t.params.jugador || ''))}
          ${t.params.autorenovar ? U.etiqueta('se renueva sola', 'ok') : ''}</p>
        <p class="fila__sub">${U.fecha(t.cuando)}: ${U.esc(t.nota || 'sin nota')}</p>
      </div>
      ${U.boton('Cancelar', { accion: 'cancelar-tarea', arg: t.id })}
    </article>`).join('');

  /* Crear una tarea desde aquí, sin pasar por la ficha del jugador. */
  const nueva = `
    <div class="tarjeta__cuerpo u-pila">
      <div class="rejilla">
        <div>
          ${U.desplegable({ id: 'tp-accion', etiqueta: 'Qué', opciones: [
            ['blindar', 'Blindar'], ['vender', 'Vender (listar)'],
            ['subir-clausula', 'Subir cláusula'], ['clausulazo', 'Clausulazo'],
            ['venta-inmediata', 'Venta inmediata'], ['alineacion', 'Guardar alineación'],
          ] })}
        </div>
        <div>
          <label class="u-label" for="tp-jugador">A quién</label>
          <input class="campo-texto" id="tp-jugador" placeholder="nombre o apodo" autocomplete="off">
        </div>
      </div>
      <div class="rejilla">
        <div>
          <label class="u-label" for="tp-cuando">Cuándo</label>
          <input class="campo-texto" id="tp-cuando" type="datetime-local">
        </div>
        <div>
          <label class="u-label" for="tp-nota">Por qué (se guarda)</label>
          <input class="campo-texto" id="tp-nota" placeholder="motivo" autocomplete="off">
        </div>
      </div>
      <label class="opcion-aviso">
        <span class="opcion-aviso__texto">
          <span class="opcion-aviso__titulo">Autorrenovable</span>
          <span class="opcion-aviso__detalle">Al terminar se reprograma sola 5 min después de
            caducar. Para el blindaje: lo mantiene puesto sin acordarse.</span>
        </span>
        <input type="checkbox" class="interruptor" id="tp-auto">
      </label>
      ${U.boton('Programar', { tono: 'accion', accion: 'crear-tarea', bloque: true })}
    </div>`;

  const duelos = (informe.enfrentamientos.duelos_internos || []).map((d) => {
    const lados = Object.entries(d.lados)
      .map(([t, jugadores]) => `<b>${U.esc(nombreEquipo(Number(t)))}</b>: ${U.esc(jugadores.join(', '))}`)
      .join(' &nbsp;vs&nbsp; ');
    return `<article class="fila"><div class="fila__cuerpo"><p class="fila__sub">${lados}</p></div></article>`;
  }).join('');

  return `
    <div class="u-pila-sm">${avisosDe(informe, estado).map(U.aviso).join('')}</div>
    <div class="rejilla">
      ${U.tarjeta('Exposición', expuestos, {
        extra: `rival: ${U.millones(rival)}`,
        pie: 'La barra mide lo barato que sales de robar: cuanto más llena, más apetecible.',
      })}
      <div class="u-pila">
        ${U.tarjeta('Caja rival estimada', cajas, {
          pie: `Estimación por partida doble desde el tablón, sobre un presupuesto inicial de ${U.millones(ex.presupuesto_inicial)}.`,
        })}
        ${U.tarjeta('Programado', programadas || U.vacio('Nada en cola'), {
          pie: 'Las tareas viven en la base de datos: sobreviven a un reinicio del contenedor. Una que '
             + 'venciera con el proceso caído se marca perdida y NO se ejecuta tarde.',
        })}
        ${U.tarjeta('Programar algo nuevo', nueva, {
          pie: 'Se acepta cualquier hora futura. Clausulazos, ventas y blindajes salen en el '
             + 'tablón con la suya, así que elegirla es parte de la jugada.',
        })}
        ${duelos ? U.tarjeta(`Duelos internos · jornada ${informe.enfrentamientos.jornada}`, duelos, {
          pie: 'Tus jugadores en lados opuestos del mismo partido se anulan entre ellos.',
        }) : ''}
      </div>
    </div>`;
}

/* API */

/* El catálogo de rutas de LaLiga. No hay documentación oficial, así que cada una se ha
   sacado a mano: del APK, de los dos clientes públicos o chocando contra la API. Las
   secciones dicen en qué estado está cada grupo: una ruta copiada de otro proyecto no
   está verificada hasta que se ejecuta.
   sin-rastro-ok: los símbolos de las descripciones se pintan en «API › LaLiga». */
export const RUTAS_LALIGA = [
 // Lectura
  ['GET', '{CMP}/players', 'Maestro público: todos los jugadores con valor y foto. Sin token, cache 8 h'],
  ['GET', '{CMP}/leagues', '⭐ Mis ligas + config: qué features Premium tiene la liga'],  // sin-rastro-ok: se pinta en «API › LaLiga»
  ['GET', '{API}/v4/user/me', '⭐ Mi usuario: id, managerName, avatar, banned, region. Espacio v4'],  // sin-rastro-ok: se pinta en «API › LaLiga»
  ['GET', '{CMP}/week/current', 'weekNumber, isLive, openingWeekDate, closingWeekDate'],
  ['GET', '{CMP}/calendar?weekNumber=N', 'Los 10 partidos con matchDate y matchState'],
  ['GET', '{CMP}/leagues/{lg}/standing', 'Clasificación actual. Ojo: /ranking da 404'],
  ['GET', '{CMP}/leagues/{lg}/standing/{N}', '⭐ Clasificación de la jornada N. NO lleva la palabra "week"'],  // sin-rastro-ok: se pinta en «API › LaLiga»
  ['GET', '{CMP}/leagues/{lg}/teams', 'Todas las plantillas de golpe. NO trae isShielded'],
  ['GET', '{CMP}/leagues/{lg}/teams/{teamId}', 'Plantilla completa, esta sí con isShielded'],
  ['GET', '{CMP}/player/{playerId}/league/{lg}', '⭐ Jugador en contexto: quién lo tiene, cláusula, shieldedEndDate'],  // sin-rastro-ok: se pinta en «API › LaLiga»
  ['GET', '{CMP}/league/{lg}/market', 'Mercado. Singular "league", no "leagues"'],
  ['GET', '{CMP}/teams/{teamId}/money', 'teamMoney + teamInvestment. Solo el equipo propio'],
  ['GET', '{CMP}/teams/{teamId}/lineup', 'El once actual, para la próxima jornada'],
  ['GET', '{CMP}/teams/{teamId}/lineup/week/{N}', '⭐ El once CONGELADO de esa jornada y sus puntos'],  // sin-rastro-ok: se pinta en «API › LaLiga»
  ['GET', '{CMP}/leagues/{lg}/activity/{index}', 'Tablón: 1 clausulazo, 4 blindaje, 6 recompensa, 31 compra, 33 venta'],
  ['GET', '{CMP}/league/{lg}/playerTeam/{ptid}/offer', 'Ofertas recibidas. 403 si no es tuyo'],
  ['GET', '{CMP}/league/{lg}/player-team/{ptid}/check-shield', 'Si se puede blindar a ese jugador'],
  ['GET', '{CMP}/daily-reward', '⭐ Tarifa de la recompensa diaria: 100.000 €/día en liga privada'],  // sin-rastro-ok: se pinta en «API › LaLiga»
 // Escritura
  ['POST', '{CMP}/league/{lg}/market/{mkId}/bid', 'Pujar por agente libre. Subasta a primer precio'],
  ['PUT', '{CMP}/league/{lg}/market/{mkId}/bid/{bidId}', 'Modificar una puja viva'],
  ['DELETE', '{CMP}/league/{lg}/market/{mkId}/bid/{bidId}/cancel', '⭐ Cancelar una puja viva. Con POST da 405'],  // sin-rastro-ok: se pinta en «API › LaLiga»
  ['POST', '{CMP}/league/{lg}/market/sell', 'Listar para vender. LaLiga oferta entre el valor y ±10%'],
  ['DELETE', '{CMP}/league/{lg}/market/{mkId}/delete', '⭐ Retirar del mercado algo que listaste. Con POST da 405'],  // sin-rastro-ok: se pinta en «API › LaLiga»
  ['POST', '{CMP}/league/{lg}/market/{mkId}/offer/{offerId}/accept', 'Aceptar oferta. Exige el importe'],
  ['POST', '{CMP}/league/{lg}/market/{mkId}/offer/{offerId}/reject', '⭐ Rechazar una oferta recibida'],  // sin-rastro-ok: se pinta en «API › LaLiga»
  ['DELETE', '{CMP}/league/{lg}/market/{mkId}/offer/{offerId}/cancel', '⭐ Cancelar una oferta que hiciste. Con POST da 405'],  // sin-rastro-ok: se pinta en «API › LaLiga»
  ['POST', '{CMP}/league/{lg}/market/direct-offer', 'Ofertar por la ficha de otro manager. El campo `playerId` espera el playerTeamId. No garantiza la compra: decide el dueño'],
  ['POST', '{CMP}/league/{lg}/buyout/{ptid}/pay', 'Clausulazo. Importe EXACTO al euro o 409'],
  ['PUT', '{CMP}/league/{lg}/buyout/player', 'Subir cláusula. factor=2 es el único que existe'],
  ['POST', '{CMP}/league/{lg}/buyout/{ptid}/increase', '⭐ Otra ruta para subir cláusula (sin verificar)'],  // sin-rastro-ok: se pinta en «API › LaLiga»
  ['PUT', '{CMP}/league/{lg}/shield/player', 'Blindar 24 h. rewardedAd:1, no se valida en servidor'],
  ['PUT', '{CMP}/teams/{teamId}/lineup', 'Guardar once. tactical_formation en snake_case'],
 // Recompensa diaria
  ['GET', '{CMP}/league/{lg}/team/{teamId}/check-daily-reward', '⭐ ¿Queda recompensa hoy? dailyRewardsRedeemed'],  // sin-rastro-ok: se pinta en «API › LaLiga»
  ['POST', '{CMP}/league/{lg}/team/daily-reward', '⭐⭐ COBRA 100.000 €. {teamId, rewardedAdType:"dailyreward", rewardedAd:1}'],  // sin-rastro-ok: se pinta en «API › LaLiga»
 // Verificadas contra la API real
  ['GET', '{API}/v3/teams-master', '⭐⭐ Los 42 equipos con nombre, siglas, slug y ESCUDOS'],  // sin-rastro-ok: se pinta en «API › LaLiga»
  ['GET', '{API}/v5/activity-types', '⭐ Cada activityTypeId con su plantilla de texto, por idioma'],  // sin-rastro-ok: se pinta en «API › LaLiga»
  ['GET', '{CMP}/player/{id}/market-value', '⭐⭐ Histórico OFICIAL de precios desde junio. El campo bids vale 0 siempre'],  // sin-rastro-ok: se pinta en «API › LaLiga»
  ['GET', '{CMP}/league/{lg}/market/history', '⭐ Histórico del mercado de la liga (30 últimas)'],  // sin-rastro-ok: se pinta en «API › LaLiga»
  ['GET', '{CMP}/ranking/global', 'Top 100 mundial. El 1.º va por 260 pts'],
  ['GET', '{CMP}/profitable/{jornada}', '⭐ El once más rentable de la jornada: backtesting gratis'],  // sin-rastro-ok: se pinta en «API › LaLiga»
  ['GET', '{CMP}/players/live-points', 'Puntos en vivo. Vacío si no hay jornada en juego'],
  ['GET', '{CMP}/config', '8 flags de configuración de la competición'],
  ['GET', '{CMP}/seasons/last', 'Temporada actual y anterior, con fechas'],
  ['GET', '{API}/v3/time', 'Hora del servidor'],
  ['GET', '{API}/v4/regions', 'Regiones'],
  ['POST', '{CMP}/league/{lg}/market/immediate-sale', '⭐ Vender AL INSTANTE. {playerId, salePrice}'],  // sin-rastro-ok: se pinta en «API › LaLiga»
  ['405', '{CMP}/leagues/{lg}/me', 'Existe, no por GET. Falta dar con el verbo'],
  ['GET', '{CMP}/team/{teamId}/rewards', '❌ 400 "feature not enabled": la liga no es premium'],  // sin-rastro-ok: se pinta en «API › LaLiga»
 // Conocidas pero sin probar todavía
  ['GET', '{CMP}/ranking/global/week/{N} · /global/month/{M}', 'Clasificación global por jornada y por mes'],
  ['GET', '{CMP}/ideal/{weekNumber}', 'El once ideal de la jornada (solo Premium)'],
  ['GET', '{API}/stats/v1/competition/1/players/status', 'Espacio /stats/v1: estado de los jugadores'],
  ['POST', '{CMP}/league/{lg}/playerTeam/{ptid}/reject-all-offers', 'Rechazar TODAS las ofertas de un jugador'],
 // Existen pero no por GET (405): pista de que hay algo ahí
  ['405', '{CMP}/notifications', '⚠️ Existe, pero no por GET. Probablemente registro de push'],  // sin-rastro-ok: se pinta en «API › LaLiga»
  ['500', '{API}/v4/leagues · /v4/leagues/{lg}', '⚠️ Existe y falla: le faltan parámetros sin identificar'],  // sin-rastro-ok: se pinta en «API › LaLiga»
 // Que NO existen (comprobado, para no volver a probarlas)
  ['GET', '{CMP}/news · /rewards · /reward', '❌ 404. Las noticias no están en esta API'],  // sin-rastro-ok: se pinta en «API › LaLiga»
  ['GET', '{CMP}/teams · /standing · /formations', '❌ 404. El catálogo de equipos está en {API}/v3/teams-master'],  // sin-rastro-ok: se pinta en «API › LaLiga»
  ['GET', '{API}/stats/v1/...', '❌ 404 todo lo probado de ese espacio'],  // sin-rastro-ok: se pinta en «API › LaLiga»
  ['POST', '{CMP}/daily-reward', '❌ 405. Existe en GET y solo da la tarifa; el cobro va por /league/{lg}/team/daily-reward'],  // sin-rastro-ok: se pinta en «API › LaLiga»
  ['GET', '{CMP}/leagues/{lg}/standing/week/{N}', '❌ 404. Es sin "week" (ver arriba)'],  // sin-rastro-ok: se pinta en «API › LaLiga»
  ['GET', '{CMP}/week/{N}/ideal-formation', '❌ 404. El once ideal es Premium y la liga no lo tiene'],  // sin-rastro-ok: se pinta en «API › LaLiga»
];

/* Motor: los trabajos del worker
   Los procesos viven dentro del worker y su única traza son los logs del contenedor.
   Esto se opera desde el móvil, así que tienen que verse en pantalla.

   `motorUI.abierto` es el proceso desplegado para configurar. Uno cada vez: con dos
   paneles abiertos no se sabe cuál se está tocando. */
export const motorUI = { abierto: null };

function panelConfig(nombre, cat, cfg) {
  const campo = (o) => {
    const v = cfg[o.clave];
    const id = `cfg-${nombre}-${o.clave}`;
    const entrada =
      o.tipo === 'numero'
        ? `<input class="campo-texto u-num" id="${id}" type="number"
             min="${o.min ?? 0}" max="${o.max ?? 999}" value="${U.esc(v ?? '')}">`
      : o.tipo === 'horas'
        ? `<input class="campo-texto u-num" id="${id}" autocomplete="off"
             value="${U.esc(Array.isArray(v) ? v.join(', ') : (v ?? ''))}"
             placeholder="09:40, 19:40">`
      : o.tipo === 'intervalo'
        ? `<input class="campo-texto u-num" id="${id}" autocomplete="off"
             value="${U.esc(Array.isArray(v) ? v.join(', ') : (v ?? ''))}"
             placeholder="20, 50">`
        : `<input type="checkbox" class="interruptor" id="${id}" ${v ? 'checked' : ''}>`;
    return `<div class="cfg__campo">
      <label class="u-label" for="${id}">${U.esc(o.titulo)}</label>
      ${entrada}
      ${o.ayuda ? `<p class="cfg__ayuda">${U.esc(o.ayuda)}</p>` : ''}
    </div>`;
  };

  return `
    <div class="cfg">
      <p class="cfg__que"><b>Qué hace.</b> ${U.esc(cat.que_hace)}</p>
      <p class="cfg__que"><b>Para qué sirve.</b> ${U.esc(cat.para_que)}</p>
      <p class="cfg__coste"><b>Coste:</b> ${U.esc(cat.coste)}</p>
      ${cat.critico ? U.aviso({ tono: 'warn', titulo: 'Proceso crítico',
        texto: 'Apagarlo puede costar puntos o datos que no se recuperan. Reprogramarlo '
             + 'es seguro; apagarlo, no.' }) : ''}
      <div class="cfg__campos">${(cat.opciones || []).map(campo).join('')}</div>
      <div class="cfg__pie">
        ${U.boton('Guardar', { tono: 'accion', accion: 'guardar-job', arg: nombre })}
        ${U.boton('Cancelar', { accion: 'cerrar-config' })}
      </div>
    </div>`;
}

function vistaProcesos({ trabajos }) {
  if (!trabajos) {
    return U.aviso({ tono: 'warn', titulo: 'El motor no responde',
      texto: 'No se pudo leer el estado de los procesos del worker.' });
  }
  const cat = trabajos.catalogo || {};
  const cfgs = trabajos.config || {};
  const porNombre = Object.fromEntries((trabajos.trabajos || []).map((t) => [t.nombre, t]));
  const pend = (trabajos.peticiones || []).filter((p) => p.estado !== 'hecha');
  /* El catálogo trae más procesos de los que se pueden lanzar: `LANZABLES` es una lista
     blanca cerrada, porque dejar que el navegador nombre funciones del worker sería
     ejecución remota. Pintar el botón para todos da un 400 en `centinela`, `puntos` y
     `trading`. */
  const lanzables = trabajos.lanzables || {};

  const fila = (nombre) => {
    const c = cat[nombre] || {};
    const cfg = cfgs[nombre] || {};
    const t = porNombre[nombre];
    const on = cfg.activo !== false;
    const abierto = motorUI.abierto === nombre;
    return `
      <div class="proceso ${on ? '' : 'proceso--off'}">
        <div class="proceso__cab">
          <label class="proceso__sw" title="${on ? 'Encendido' : 'Apagado'}">
            <input type="checkbox" class="interruptor" data-job-on="${U.esc(nombre)}"
              ${on ? 'checked' : ''}>
          </label>
          <div class="proceso__id">
            <b>${U.esc(c.titulo || nombre)}</b>
            <span class="proceso__clave">${U.esc(nombre)}</span>
          </div>
          <div class="proceso__estado">
            ${t ? `<span class="${t.ok === false ? 'es-baja' : 'es-sube'}">
                     ${t.ok === false ? 'falló' : 'ok'}</span>
                   <span class="u-label">hace ${t.hace_min < 60
                     ? `${Math.round(t.hace_min)} min` : `${(t.hace_min / 60).toFixed(1)} h`}
                     · ${t.ms} ms</span>`
                : `<span class="u-label">${on ? 'sin ejecutar aún' : 'apagado'}</span>`}
          </div>
        </div>
        <p class="proceso__que">${U.esc(c.que_hace || '')}</p>
        <div class="proceso__pie">
          <span class="u-label">${U.esc(resumenHorario(cfg))}${
            t ? ` · ${t.veces_7d} veces en 7 días` : ''}${
            t?.fallos_7d ? ` · <b class="es-mal-txt">${t.fallos_7d} con fallo</b>` : ''}</span>
          <span class="u-fila">
            ${U.boton(abierto ? 'Cerrar' : 'Configurar',
              { accion: abierto ? 'cerrar-config' : 'config-job', arg: nombre })}
            ${U.boton('Ejecutar ahora',
              { accion: 'lanzar-job', arg: nombre, off: !on || !(nombre in lanzables) })}
          </span>
        </div>
        ${abierto ? panelConfig(nombre, c, cfg) : ''}
      </div>`;
  };

  return `
    ${pend.length ? U.aviso({ tono: 'info', titulo: 'Ejecuciones en curso',
      lista: pend.map((p) => `${p.nombre}, ${p.estado}`) }): ''}
    ${U.tarjeta('Procesos del worker', Object.keys(cat).map(fila).join(''), {
      extra: `${Object.values(cfgs).filter((c) => c.activo !== false).length} de ${Object.keys(cat).length} encendidos`,
      pie: 'Los cambios se aplican en menos de 10 minutos, sin reiniciar nada. '
         + '«Ejecutar ahora» deja la petición en la tabla y el worker la recoge en 20 s.' })}`;
}

/* Traduce la configuración a una frase legible: "cada 30 min" dice más que un cron, y
   es lo que hay que poder comprobar de un vistazo.

   `cada_minutos` va ANTES que `cada_horas` porque manda sobre él en el scheduler. Al
   final de la lista cae en el caso del minuto suelto y pinta "cada hora en el minuto 25"
   para un proceso que corre cada 30: la mitad de frecuencia, justo en la pantalla que
   existe para ver lo que cuesta cada proceso en peticiones. */
function resumenHorario(cfg) {
  if (cfg.activo === false) return 'apagado';
  if (cfg.horas?.length) return `a las ${cfg.horas.join(', ')}`;
  if (cfg.cada_minutos) return `cada ${cfg.cada_minutos} min`;
  if (cfg.cada_horas) return `cada ${cfg.cada_horas} h en el minuto ${cfg.minuto ?? 0}`;
  if (cfg.minutos?.length) return `en los minutos ${[].concat(cfg.minutos).join(', ')} de cada hora`;
  if (cfg.minuto !== undefined) return `cada hora en el minuto ${cfg.minuto}`;
  return 'sin horario';
}

export function vistaMotor(sub, datos) {
  if (sub === 'Vigilancias') return vistaVigilancias(datos);
  return vistaProcesos(datos);
}

/* Los textos vienen pensados para Telegram: llevan etiquetas HTML y emojis. Aquí el
   HTML no se renderiza, y la fuente de emoji no se puede dar por supuesta. */
const limpio = (t) => String(t || '')
  .replace(/<[^>]+>/g, '')
  .replace(/[\u{1F000}-\u{1FAFF}\u{2600}-\u{27BF}\u{FE0F}]/gu, '')
  .trim();

/* Vigilancias: tareas que esperan hasta completarse */
function vistaVigilancias({ seguimientos }) {
  if (!seguimientos) {
    return U.aviso({ tono: 'warn', titulo: 'No se pudieron leer las vigilancias', texto: '' });
  }
  const ss = seguimientos.seguimientos || [];
  const activos = ss.filter((s) => s.estado === 'activo');
  const cerrados = ss.filter((s) => s.estado !== 'activo');

  const fila = (s) => `
    <div class="trabajo ${s.estado === 'fallido' ? 'trabajo--mal' : ''}">
      <div class="trabajo__cab">
        <span class="trabajo__punto ${s.estado === 'activo' ? 'es-vivo'
          : s.estado === 'hecho' ? 'es-bien' : 'es-mal'}"></span>
        <b class="trabajo__nombre">${U.esc(s.tipo)}</b>
        <span class="trabajo__meta">${U.esc(s.params?.nombre || '')} · ${s.intentos} comprobación(es)</span>
      </div>
      ${s.nota ? `<p class="trabajo__desc">${U.esc(s.nota)}</p>` : ''}
      ${s.resultado ? `<p class="trabajo__detalle">${U.esc(limpio(s.resultado))}</p>` : ''}
      ${s.historial?.length ? `<ul class="trabajo__hist">${s.historial.slice(-3).map((h) =>
        `<li><span>${U.esc(U.fecha(h.ts).slice(-5))}</span> ${U.esc(limpio(h.texto))}</li>`).join('')}</ul>` : ''}
      <p class="trabajo__pie">${s.estado === 'activo'
        ? `revisa de nuevo: ${U.fecha(s.proxima_revision)}`
        : `terminó ${U.fecha(s.actualizada)}`}</p>
      ${s.estado === 'activo' ? `<div class="trabajo__acc">${
        U.boton('Cancelar', { accion: 'cancelar-seg', arg: s.id })}</div>` : ''}
    </div>`;

  return `
    ${U.aviso({ tono: 'info', titulo: 'Qué es esto',
      texto: 'Tareas que no se disparan y ya está: se quedan mirando hasta completarse. '
           + 'Viven en la base de datos, así que si el sistema se apaga, al arrancar '
           + 'retoman donde lo dejaron. Antes de actuar contrastan contra la API en vivo.' })}
    ${U.tarjeta('Vigilando ahora', activos.map(fila).join('')
      || '<p class="vacio">Ninguna vigilancia activa.</p>', { extra: `${activos.length}` })}
    ${cerrados.length ? U.tarjeta('Terminadas', cerrados.slice(0, 10).map(fila).join(''),
      { extra: `${cerrados.length}` }) : ''}`;
}

export function vistaApi(sub, datos) {
  const { openapi, estado } = datos;
  if (sub === 'Backtest') return vistaBacktest(datos);
  if (sub === 'Premium') return vistaPremium(datos);
  /* `openapi` llega en la SEGUNDA fase de carga: hasta entonces se avisa en vez de
     reventar con `null.paths`. La app pinta antes de tener los endpoints lentos. */
  if (sub === 'Coconut' && !openapi) {
    return U.aviso({ tono: 'info', titulo: 'Cargando el catálogo de la API',
                     texto: 'Un momento: se pide después de lo esencial.' });
  }
  if (sub === 'LaLiga') {
    const filas = RUTAS_LALIGA
      .map(([metodo, ruta, desc]) => U.filaEndpoint({ metodo, ruta, desc })).join('');
    return `
      ${U.aviso({
        tono: 'warn',
        titulo: 'Ninguna de estas rutas está documentada',
        texto: 'LaLiga Fantasy no publica documentación de su API. Si cambia una ruta a '
             + 'mitad de temporada (lo hará), se corrige aquí.',
      })}
      ${U.tarjeta('Endpoints de LaLiga Fantasy', filas, {
        extra: `${RUTAS_LALIGA.length} rutas`,
        pie: '{CMP} = https://fantasy-api.llt-services.com/api/v1/competition/1 · '
           + 'el id de liga va con su cero delante, o responde 404. '
           + 'El catálogo completo está en docs/RUTAS-LALIGA.md.',
      })}`;
  }

  const grupos = new Map();
  for (const [ruta, metodos] of Object.entries(openapi.paths || {})) {
    const grupo = ruta.split('/')[1] || 'raíz';
    for (const [metodo, def] of Object.entries(metodos)) {
      if (!grupos.has(grupo)) grupos.set(grupo, []);
      grupos.get(grupo).push({
        metodo: metodo.toUpperCase(), ruta,
        desc: def.summary || def.description?.split('\n')[0] || '',
        probar: metodo === 'get' && !ruta.includes('{'),
      });
    }
  }
  const bloques = [...grupos.entries()].map(([grupo, rutas]) =>
    U.tarjeta(`/${grupo}`, rutas.map(U.filaEndpoint).join(''), { extra: `${rutas.length}` })).join('');

  const total = Object.values(openapi.paths || {}).reduce((n, m) => n + Object.keys(m).length, 0);
  return `
    ${U.aviso({
      tono: 'ok',
      titulo: `La API de Coconut expone ${total} rutas`,
      texto: `Modo ${estado.sistema.modo} · ${estado.sistema.peticiones_al_origen_hoy} peticiones al origen hoy `
           + `· WAF ${estado.sistema.bloqueado_por_waf ? 'BLOQUEANDO' : 'sin bloqueos'}.`,
    })}
    <div class="rejilla">${bloques}</div>`;
}

/* El botón "Probar" del catálogo
 *
 * Solo se ofrece en GET sin parámetros: son lecturas, no mueven dinero y no hay nada que
 * rellenar. Lo decide `vistaApi` al montar los grupos (el campo `probar`); `filaEndpoint`
 * solo pinta el botón si esa bandera viene puesta.
 *
 * Su manejador vive en `app.js`. Un `data-accion` sin manejador deja un botón que no
 * hace nada; quien lo caza es `probarAccionesHuerfanas`, que cruza los emitidos con los
 * atendidos sin pintar nada.
 */
export const sondaUI = { ruta: '', cargando: false, r: null };

export function pintarSondaHTML() {
  if (!sondaUI.ruta) return '';
  const { r } = sondaUI;
  const tono = sondaUI.cargando ? 'info'
    : !r ? 'info'
      : r.status === 0 ? 'crit'
        : r.status < 300 ? 'ok' : r.status < 500 ? 'warn' : 'crit';
  const cuerpo = sondaUI.cargando
    ? U.cargando('Llamando…')
    : r?.error ? `<p class="sonda__err">${U.esc(r.error)}</p>`
      : `<pre class="sonda__json">${U.esc(recortar(r?.cuerpo))}</pre>`;
  return `
    <div class="modal">
      <!-- Ojo: el atributo de cerrar va SOLO en el velo. Puesto también en el contenedor
           .modal, un clic sobre el JSON lo encuentra al subir por el árbol y cierra el
           panel justo cuando lo estás leyendo. -->
      <div class="modal__velo" data-cerrar-sonda></div>
      <div class="modal__caja">
        <div class="modal__cabecera modal__cabecera--${tono === 'ok' ? 'bajo' : tono === 'crit' ? 'alto' : 'medio'}">
          <h2>GET ${U.esc(sondaUI.ruta)}</h2>
          ${U.boton('Cerrar', { accion: 'cerrar-sonda' })}
        </div>
        <div class="modal__cuerpo">
          ${sondaUI.cargando ? '' : `<p class="sonda__meta">
            ${r?.status ? `HTTP ${r.status}` : 'sin respuesta'} · ${r?.ms ?? '?'} ms
            ${r?.demo ? ' · maqueta' : ''}</p>`}
          ${cuerpo}
        </div>
      </div>
    </div>`;
}

/* El maestro pasa de 800 fichas: volcarlo entero cuelga el navegador del móvil y además
   no se lee. Se enseña el principio, que es lo que sirve para ver la forma de la
   respuesta, y se dice cuánto se ha recortado. */
const TOPE_SONDA = 12000;
function recortar(cuerpo) {
  const texto = typeof cuerpo === 'string' ? cuerpo : JSON.stringify(cuerpo, null, 2);
  if (!texto) return '(respuesta vacía)';
  return texto.length <= TOPE_SONDA ? texto
    : `${texto.slice(0, TOPE_SONDA)}\n\n… recortado: ${texto.length.toLocaleString('es')} caracteres en total.`;
}

/* Actividad */

/* Cuántos eventos se ven. El tablón trae cientos y pintarlos todos de golpe hace
   una página larguísima que en el móvil no se recorre. Se amplía con «ver más». */
export const actividadUI = { visibles: 30, filtro: '' };

export function vistaActividad({ actividad }) {
  const TODOS = actividad || [];
  const f = actividadUI.filtro;
  /* El filtro por tipo es lo que convierte cientos de líneas en una respuesta:
     «enséñame solo los clausulazos» o «solo los blindajes». */
  const filtrados = f ? TODOS.filter((a) => String(a.activityTypeId) === f) : TODOS;
  const mostrados = filtrados.slice(0, actividadUI.visibles);

  const opciones = [['', `Todo (${TODOS.length})`]].concat(
    Object.entries(TIPOS_EVENTO)
      .map(([id, t]) => [id, `${t.titulo} (${TODOS.filter((a) => String(a.activityTypeId) === id).length})`])
      .filter(([id]) => TODOS.some((a) => String(a.activityTypeId) === id)));

  const filas = mostrados.map(describirEvento).map((e) => `
    <article class="fila${e.mio ? ' fila--mia' : ''}">
      <div class="fila__cuerpo">
        <p class="fila__titulo">${U.esc(e.titulo)}
          ${e.jugador ? U.etiqueta(equipoDe(e.jugador)) : ''}</p>
        <p class="fila__sub">${U.esc(e.texto)}</p>
      </div>
      <span class="u-label">${U.fecha(e.cuando)}</span>
    </article>`).join('');

  const sinIdentificar = TODOS.filter((a) => !TIPOS_EVENTO[a.activityTypeId]).length;
  const quedan = filtrados.length - mostrados.length;

  const cabecera = `
    <div class="act__barra">
      ${U.desplegable({ id: 'act-filtro', valor: f, opciones })}
    </div>`;

  const pie = quedan > 0
    ? `<div class="act__mas">${U.boton(`Ver más (quedan ${quedan})`, { accion: 'mas-actividad', bloque: true })}</div>`
    : '';

  return U.tarjeta('Actividad de la liga', cabecera + (filas || U.vacio('Sin movimientos')) + pie, {
    extra: `${mostrados.length} de ${filtrados.length}`,
    pie: sinIdentificar
      ? `${sinIdentificar} evento(s) de tipo desconocido: se guardan crudos para interpretarlos luego.`
      : 'El tablón llega entero: se recorren todas sus páginas. Aquí se enseñan los más '
        + 'recientes y el resto sale con «Ver más».',
  });
}

/* Avisos: lo que el sistema ve y la app oficial no */

function avisosDe(informe, estado) {
  const out = [];
  const jr = informe.jornada;

  if (!jr.once_completo) {
    out.push({
      tono: 'crit',
      titulo: `Once incompleto: la jornada ${jr.jornada} no puntúa`,
      texto: `Se congeló con ${jr.num_titulares} jugadores. Llevas ${jr.puntos_sumados_del_once} puntos `
           + `hechos y el marcador oficial marca ${jr.puntos_oficiales_jornada}. Ya no se puede corregir.`,
    });
  }
  if (!informe.validacion_once.valido) {
    out.push({
      tono: 'crit', titulo: 'El once de la próxima jornada no es válido',
      lista: informe.validacion_once.problemas,
    });
  }
  /* Los avisos de enfrentamiento van en UNO solo: repartidos salen tres tarjetas con
     el mismo título, y tres títulos iguales no informan de nada. */
  const enfrentamientos = informe.enfrentamientos.avisos || [];
  if (enfrentamientos.length) {
    out.push({
      tono: 'warn',
      titulo: `${enfrentamientos.length} aviso${enfrentamientos.length === 1 ? '' : 's'} `
            + `para la jornada ${informe.enfrentamientos.jornada}`,
      texto: 'Duelos entre jugadores tuyos y rivales fuertes: se anulan o puntúan poco.',
      lista: enfrentamientos.map((a) => a.replace(/^⚠️?\s*/, '')),  // sin-rastro-ok: viene del servidor
    });
  }
  const enPeligro = (informe.exposicion.atacables || []).filter((p) => p.atacable);
  if (enPeligro.length) {
    out.push({
      tono: 'warn',
      titulo: `${enPeligro.length} jugador${enPeligro.length === 1 ? '' : 'es'} al alcance del rival más rico`,
      texto: `Puede pagar hasta ${U.millones(informe.exposicion.puede_pagar_hasta)}.`,
      lista: enPeligro.map((p) => `${p.nombre}, cláusula ${U.millones(p.clausula)}`),
    });
  }
  if (estado.sistema.bloqueado_por_waf) {
    out.push({ tono: 'crit', titulo: 'Parada por WAF', texto: 'El transporte se ha detenido. No se hará ninguna petición más.' });
  }
  return out;
}

/* Hoy
   La pantalla de inicio: qué hay que atender, ordenado por lo que cuesta no
   hacerlo. Reúne en una lo que si no habría que cruzar entre cinco pestañas. */

/* El icono de cada tipo de asunto. Lo que no esté aquí cae a `campana`, incluidos los
   `bloque_roto:{que}` que compone `informe/comun.py`.

   Los títulos vienen del servidor con un emoji delante para el log y Telegram; en la web
   se quita, porque el headless los pinta como cajas vacías y la fuente del visitante no
   se puede dar por supuesta. */
const ICONO_ASUNTO = {
  once_invalido: 'campo', rival_no_alinea: 'campo',
  ventana_clausulazo: 'reloj', clausulazos_cerrados: 'escudo',
  saldo_negativo: 'barras', saldo_justo: 'barras',
  exposicion: 'escudo', momentum: 'grafico', sin_momentum: 'grafico',
  premium_nuevo: 'enchufe',
};

/* Quita el emoji inicial (y su espacio) sin tocar el resto del texto. */
const sinEmoji = (t) => String(t || '').replace(/^[^\p{L}\p{N}]+/u, '').trim();

const TONO_URGENCIA = (u) => (u >= 85 ? 'crit' : u >= 60 ? 'alto' : u >= 30 ? 'medio' : 'bajo');
export function vistaHoy(d) {
  const { hoy, estado, alineacion, informe, tareas, seguimientos, trabajos, candado } = d;
  const e = estado.equipo, c = estado.caja, j = estado.jornada;

 /* 1. La foto: dónde vas y con cuánto */
  const abre = j.abre ? new Date(j.abre) : null;
  const faltan = abre ? (abre - new Date()) / 36e5 : null;
  const cabecera = `
    <section class="home__cab">
      <div class="home__pos">
        <span class="home__posnum">${e.posicion}<sup>º</sup></span>
        <span class="u-label">de ${estado.clasificacion?.length || '?'} · ${e.puntos_totales} pts</span>
      </div>
      <div class="home__caja">
        <span class="u-label">Caja libre</span>
        ${U.dinero(c.total)}
        ${c.comprometido_en_pujas
          ? `<span class="u-label">${U.millones(c.comprometido_en_pujas)} en pujas</span>` : ''}
      </div>
      <div class="home__jor">
        <span class="u-label">Jornada ${j.numero}</span>
        <b>${j.en_juego ? 'en juego'
: faltan === null ? '-'
          : faltan > 0 ? `abre en ${faltan < 24 ? `${Math.round(faltan)} h` : `${Math.round(faltan / 24)} días`}`
          : 'cerrada'}</b>
      </div>
    </section>`;

 /* 2. Lo que hay que atender, sin repetir el listado entero */
  const asuntos = (hoy?.asuntos || []);
  const urgentes = asuntos.filter((a) => a.urgencia >= 40);
  const bloqueAtencion = urgentes.length
    ? U.tarjeta('Atención', urgentes.map((a) => `
        <div class="asunto asunto--${TONO_URGENCIA(a.urgencia)}">
          <div class="asunto__cab">
            ${U.icono(ICONO_ASUNTO[a.clave] || 'campana', 'asunto__icono')}
            <h3 class="asunto__titulo">${U.esc(sinEmoji(a.titulo))}</h3>
          </div>
          ${a.detalle ? `<p class="asunto__detalle">${U.esc(a.detalle)}</p>` : ''}
          <p class="asunto__accion">${U.esc(a.accion || '')}</p>
        </div>`).join(''), { extra: `${urgentes.length}` })
    : U.aviso({ tono: 'ok', titulo: 'Nada urgente',
        texto: 'Ninguna comprobación ha encontrado algo que atender ahora mismo.' });

 /* 3. El equipo de un vistazo */
  const val = informe?.validacion_once;
  /* `/estado` da la posición como texto ('POR','DEF','MED','DEL'), no como id: con
     `posicion_id` los contadores salen todos a cero. */
  const porPos = {};
  for (const p2 of estado.plantilla) {
    const k = p2.posicion || '?';
    (porPos[k] ||= { total: 0, ok: 0 }).total++;
    if ((p2.estado || 'ok') === 'ok') porPos[k].ok++;
  }
  /* Y `protegido_hasta` puede estar EN EL PASADO: tener fecha no es estar protegido.
     Sin comparar contra ahora, un jugador ya expuesto cuenta como a salvo. */
  const ahora = Date.now();
  const expuestos = estado.plantilla.filter((p2) => !p2.blindado
    && !(p2.protegido_hasta && new Date(p2.protegido_hasta).getTime() > ahora));
  const bloqueEquipo = U.tarjeta('Tu equipo', `
    <div class="home__rejilla">
      <div><span class="u-label">Once</span>
        <b class="${val?.valido ? 'es-sube' : 'es-baja'}">${
          val ? (val.valido ? `válido · ${(val.formacion || []).join('-')}`: 'INCOMPLETO'): '-'}</b></div>
      <div><span class="u-label">Plantilla</span><b>${estado.plantilla.length} fichas</b></div>
      <div><span class="u-label">Valor</span><b>${U.millones(e.valor_plantilla)}</b></div>
      <div><span class="u-label">Candado</span>
        <b>${candado?.once?.bloqueado ? 'echado' : 'abierto'}</b></div>
    </div>
    <div class="home__pos-linea">
      ${['POR', 'DEF', 'MED', 'DEL'].map((k) => {
        const x = porPos[k] || { total: 0, ok: 0 };
        /* Los mínimos de la formación que menos usa de cada línea. Un jugador que no
           puede puntuar (lesionado, sancionado) NO cuenta como cobertura. */
        const falta = x.ok < ({ POR: 1, DEF: 3, MED: 3, DEL: 1 })[k];
        return `<span class="home__chip ${falta ? 'home__chip--mal' : ''}">
          ${U.chipPos(k)} <b>${x.ok}</b>${x.total !== x.ok ? `<em>/${x.total}</em>` : ''}</span>`;
      }).join('')}
    </div>`, {
    extra: `${estado.blindados.length} blindado(s)`,
    pie: expuestos.length
      ? `${expuestos.length} jugador(es) sin protección ni blindaje.`
      : 'Todos protegidos o blindados.' });

 /* 4. Lo que el sistema hará solo */
  const pend = (tareas || []).filter((t) => t.estado === 'pendiente');
  const vig = (seguimientos?.seguimientos || []).filter((x) => x.estado === 'activo');
  /* El cupo de blindajes es POR JORNADA y el número exacto no consta. Enseñar dos
     blindajes programados como si ambos fueran a funcionar promete algo que la API
     puede rechazar. */
  const blindajesProg = pend.filter((t) => t.accion === 'blindar');
  const avisoCupo = blindajesProg.length > 1
    ? U.aviso({ tono: 'warn', titulo: `${blindajesProg.length} blindajes programados`,
        texto: 'El cupo es POR JORNADA y puede que no dé para todos: LaLiga rechazará los '
             + 'que sobren con 030.02.03. El sistema encadena el siguiente solo si la '
             + 'respuesta dice que queda cupo.' })
    : '';

  const bloqueProgramado = avisoCupo + U.tarjeta('Programado', `
    ${pend.length ? pend.map((t) => `
      <div class="home__linea">
        <span>${U.esc(t.accion)} · <b>${U.esc(t.params?.jugador || '')}</b></span>
        <span class="u-label">${U.fecha(t.cuando)}${
          t.autorenovar ? ' · se renueva sola' : ''}</span>
      </div>`).join('') : '<p class="vacio">Nada programado.</p>'}
    ${vig.length ? `<div class="home__sep"></div>` + vig.map((x) => `
      <div class="home__linea">
        <span>vigila <b>${U.esc(x.params?.nombre || x.tipo)}</b></span>
        <span class="u-label">${x.intentos} comprobaciones</span>
      </div>`).join('') : ''}`, {
    extra: `${pend.length + vig.length}`,
    pie: 'Las tareas y las vigilancias viven en la base de datos: sobreviven a un reinicio.' });

 /* 5. ¿Va bien el motor? */
  const tr = trabajos?.trabajos || [];
  const conFallo = tr.filter((t) => t.fallos_7d > 0);
  const ultimo = tr[0];
  const bloqueMotor = U.tarjeta('Motor', `
    <div class="home__rejilla">
      <div><span class="u-label">Trabajos</span><b>${tr.length} activos</b></div>
      <div><span class="u-label">Con fallos</span>
        <b class="${conFallo.length ? 'es-baja' : 'es-sube'}">${conFallo.length}</b></div>
      <div><span class="u-label">Último</span>
        <b>${ultimo ? `${U.esc(ultimo.nombre)}`: '-'}</b>
        ${ultimo ? `<em>hace ${Math.round(ultimo.hace_min)} min</em>` : ''}</div>
      <div><span class="u-label">Peticiones hoy</span>
        <b>${estado.sistema.peticiones_al_origen_hoy}</b><em>de ${PRESUPUESTO_DIARIO}</em></div>
    </div>`, {
    extra: estado.sistema.bloqueado_por_waf ? '⚠ WAF' : 'ok',  // sin-rastro-ok: se ve en la tarjeta
    pie: `Modo ${estado.sistema.modo}. El bot vigila y alinea, pero no mueve dinero solo.` });

 /* 5.bis Lo que viene: rivales de la próxima jornada */
  const cal = d.calDif;
  const prox = cal ? cal.jugadores
    .map((x) => ({ ...x, sig: x.proximas?.[0] }))
    .filter((x) => x.sig?.nivel)
    .sort((a, b) => b.sig.nivel - a.sig.nivel) : [];
  const duros = prox.filter((x) => x.sig.nivel >= 4).slice(0, 4);
  const faciles = prox.filter((x) => x.sig.nivel <= 2).slice(0, 4);
  const bloqueCalendario = cal ? U.tarjeta(`Rivales de la J${cal.desde_jornada}`, `
    <div class="home__dos">
      <div>
        <p class="u-label">Lo tienen difícil</p>
        ${duros.length ? duros.map((x) => `
          <div class="home__linea">
            <span>${U.chipPos(x.posicion)} ${U.esc(x.nombre)}</span>
            <span class="dif ${COLOR_DIF[x.sig.nivel]}">${U.esc(x.sig.etiqueta)}</span>
          </div>`).join('') : '<p class="vacio">Ninguno.</p>'}
      </div>
      <div>
        <p class="u-label">Lo tienen de cara</p>
        ${faciles.length ? faciles.map((x) => `
          <div class="home__linea">
            <span>${U.chipPos(x.posicion)} ${U.esc(x.nombre)}</span>
            <span class="dif ${COLOR_DIF[x.sig.nivel]}">${U.esc(x.sig.etiqueta)}</span>
          </div>`).join('') : '<p class="vacio">Ninguno.</p>'}
      </div>
    </div>`, {
    extra: `${prox.filter((x) => x.riesgo_rotacion).length} con riesgo de rotación`,
    pie: 'La dificultad depende de la posición: para un defensa cuenta el ataque del '
       + 'rival; para un delantero, su defensa.' }) : '';

 /* 5.ter El mercado, resumido */
  const mom = d.momentum;
  const compra = (mom?.comprables || []).filter((x) => x.sube_1d_pct > 0.5 && x.me_llega);
  const bloqueMercado = mom ? U.tarjeta('Mercado', `
    <div class="home__rejilla">
      <div><span class="u-label">Agentes libres</span><b>${(mom.comprables || []).length}</b></div>
      <div><span class="u-label">Suben y caben</span>
        <b class="${compra.length ? 'es-sube' : ''}">${compra.length}</b></div>
      <div><span class="u-label">De rival</span><b>${mom.de_rival_ignorados}</b>
        <em>no se compran</em></div>
    </div>
    ${compra.length ? compra.slice(0, 3).map((x) => `
      <div class="home__linea">
        <span>${U.chipPos(x.posicion === 1 ? 'POR' : x.posicion === 2 ? 'DEF'
          : x.posicion === 3 ? 'MED' : 'DEL')} ${U.esc(x.nombre)}</span>
        <span class="es-sube">${x.sube_1d_pct > 0 ? '+' : ''}${x.sube_1d_pct}% · ${U.millones(x.valor)}</span>
      </div>`).join('')
      : '<p class="home__linea vacio">Nada que comprar hoy.</p>'}`, {
    pie: 'El momentum rinde +2,5 %/día sobre la media (validado sobre la 24/25 entera: 326 días, gana el 99 %) y persiste ~5 días, pero solo se '
       + 'explota sobre agentes libres.' }) : '';

 /* 6. Atajos: lo que se usa a diario, a un toque */
  const atajo = (h, ico, txt) => `
    <a class="atajo" href="#${h}">${U.icono(ico, 'atajo__ico')}<span>${U.esc(txt)}</span></a>`;
  const bloqueAtajos = U.tarjeta('Atajos', `
    <div class="atajos">
      ${atajo('equipo/Alineación', 'campo', 'Editar el once')}
      ${atajo('radar/Ventanas', 'reloj', 'Ventanas de clausulazo')}
      ${atajo('mercado/Momentum', 'grafico', 'Qué sube')}
      ${atajo('mercado/Jugadores', 'buscar', 'Buscar jugador')}
      ${atajo('radar/Avisos', 'campana', 'Programar algo')}
      ${atajo('motor/Procesos', 'engranaje', 'Procesos')}
      ${atajo('motor/Vigilancias', 'radar', 'Vigilancias')}
      ${atajo('equipo/Calendario', 'lista', 'Calendario')}
      ${atajo('actividad', 'lista', 'Tablón')}
    </div>`, { pie: 'Programar algo incluye el blindaje autorrenovable.' });

  /* `masonry` de pobre: las tarjetas tienen alturas muy distintas y una rejilla
     normal deja huecos enormes debajo de las cortas. Con columnas CSS cada una
     ocupa lo que necesita y la siguiente sube. */
  return cabecera + bloqueAtencion + `<div class="home__cols">
      ${bloqueEquipo}${bloqueCalendario}${bloqueProgramado}${bloqueMercado}
      ${bloqueMotor}${bloqueAtajos}
    </div>`;
}

/* Ficha de jugador
   Explica POR QUÉ un jugador hizo sus puntos: `playerStats` da el desglose por
   concepto y jornada. Se abre con `#jugador/{id}` desde cualquier lista. */

function chispa(serie, alto = 34) {
  /* Gráfico de precio en SVG, sin librería: son ~30 puntos y una polilínea basta.
     Cargar una librería de charts para esto sería pagar 100 KB por una línea. */
  if (!serie || serie.length < 2) return '';
  const vs = serie.map((p) => p.valor);
  const min = Math.min(...vs); const max = Math.max(...vs);
  const rango = max - min || 1;
  const paso = 100 / (serie.length - 1);
  const pts = vs.map((v, i) => `${(i * paso).toFixed(2)},${(alto - ((v - min) / rango) * alto).toFixed(2)}`);
  const sube = vs[vs.length - 1] >= vs[0];
  return `
    <svg class="chispa" viewBox="0 0 100 ${alto}" preserveAspectRatio="none" aria-hidden="true">
      <polyline points="${pts.join(' ')}" fill="none"
        stroke="var(${sube ? '--c-ok' : '--c-crit'})" stroke-width="1.4"
        vector-effect="non-scaling-stroke" />
    </svg>
    <div class="chispa__pie">
      <span>${U.millones(min)}</span>
      <span>${serie.length} días</span>
      <span>${U.millones(max)}</span>
    </div>`;
}

export function vistaFicha(f, id) {
  if (!f) return U.cargando('Cargando la ficha…');
  if (f.error) {
    /* La acción de salir de la ficha se llama `cerrar-ficha`, y va en `U.boton`: el campo
       `accion` de `U.aviso` se interpola como HTML, así que una cadena suelta ahí se
       pintaría como texto en vez de como botón. */
    return U.aviso({ tono: 'crit', titulo: 'No se pudo cargar la ficha',
      texto: `${f.error} · id ${U.esc(id)}`,
      accion: U.boton('← Volver', { accion: 'cerrar-ficha' }) });
  }

  const serie = f.precio_oficial?.length > f.precio_propio?.length
    ? f.precio_oficial : f.precio_propio;
  const mom = f.momentum;

  const cab = `
    <div class="ficha__cab">
      ${f.foto ? U.retrato(f.foto, f.nombre, 'retrato--grande') : ''}
      <div class="ficha__id">
        <div class="ficha__titulo">${U.chipPos(f.posicion)}
          <h2>${U.esc(f.nombre || '?')}</h2></div>
        <p class="ficha__sub">${U.esc(f.nombre_largo || '')}${
          f.dueño ? ` · de <b>${U.esc(f.dueño)}</b>` : ' · agente libre'}</p>
        ${f.estado && f.estado !== 'ok'
          ? U.etiqueta(U.estadoJugador(f.estado), 'crit') : ''}
      </div>
    </div>`;

  const datos = `
    <div class="ficha__datos">
      <div><span>Valor</span><b>${U.millones(f.valor)}</b></div>
      <div><span>Puntos</span><b>${f.puntos ?? '-'}</b></div>
      <div><span>Media</span><b>${f.media ? f.media.toFixed(1): '-'}</b></div>
      ${f.clausula ? `<div><span>Cláusula</span><b>${U.millones(f.clausula)}</b>
        <em>×${f.ratio}</em></div>` : ''}
      ${mom ? `<div><span>Ayer</span><b class="${mom.sube_1d_pct >= 0 ? 'es-sube' : 'es-baja'}">${
        mom.sube_1d_pct >= 0 ? '+' : ''}${mom.sube_1d_pct}%</b></div>` : ''}
    </div>`;

  const estadoClausula = f.clausula ? (
    f.blindado ? U.aviso({ tono: 'ok', titulo: 'Blindado',
        texto: `No se le puede clausular hasta ${U.fecha(f.blindado_hasta)}.` })
    : f.protegido_hasta && new Date(f.protegido_hasta) > new Date()
      ? U.aviso({ tono: 'info', titulo: 'Protegido (14 días desde su compra)',
          texto: `Queda libre el ${U.fecha(f.protegido_hasta)}.` })
      : U.aviso({ tono: f.dueño ? 'warn' : 'info',
          titulo: 'Sin protección: es clausulable ahora',
          texto: `Cuesta ${U.millones(f.clausula)} exactos.` })) : '';

  const jornadas = (f.jornadas || []).slice().reverse().map((j) => `
    <div class="jorn">
      <div class="jorn__cab">
        <b>Jornada ${j.jornada}</b>
        ${j.en_once_ideal ? U.etiqueta('once ideal', 'ok') : ''}
        <span class="jorn__pts">${j.puntos} pts</span>
      </div>
      <ul class="jorn__lista">
        ${(j.detalle || []).map((x) => `
          <li><span>${U.esc(x.concepto)}</span>
              <em>${x.cantidad}</em>
              <b class="${x.puntos > 0 ? 'es-sube' : x.puntos < 0 ? 'es-baja' : ''}">${
                x.puntos > 0 ? '+' : ''}${x.puntos}</b></li>`).join('')}
      </ul>
    </div>`).join('');

  return `
    ${U.boton('← Volver', { accion: 'cerrar-ficha' })}
    ${U.tarjeta('', cab + datos, { centrada: false })}
    ${estadoClausula}
    ${serie?.length ? U.tarjeta('Valor de mercado', chispa(serie), {
      extra: f.precio_oficial?.length ? 'oficial desde junio' : 'serie propia',
      pie: mom ? `1 día ${pct(mom.sube_1d_pct)} · 3 días ${pct(mom.sube_3d_pct)} · `
               + `7 días ${pct(mom.sube_7d_pct)}` : '' }) : ''}
    ${jornadas ? U.tarjeta('Puntos por jornada, concepto a concepto', jornadas, {
      extra: `${f.jornadas.length} jornadas`,
      pie: 'Sale de `playerStats`: cada concepto con su cantidad y los puntos que dio.' })
      : U.aviso({ tono: 'info', titulo: 'Sin estadísticas todavía',
          texto: 'No ha puntuado ninguna jornada de esta temporada.' })}`;
}

/* Plantilla de cualquier manager
   Los mismos endpoints que el equipo propio, cambiando el `teamId`. La plantilla
   rival es pública entera: cláusulas, blindajes y once. */

/** Un once ya jugado, sobre el campo. Solo lectura.
 *
 * Reaprovecha el `.campo` y la `U.ficha` del editor sin llamar a `once.js`: ese módulo
 * pinta leyendo su estado `editor`, que es el del equipo propio, así que aquí pintaría la
 * alineación propia con el nombre del rival encima. Se comparte el CSS y la ficha.
 *
 * Las líneas salen del campo `linea` que manda la API, no de contar posiciones: un
 * jugador puede estar alineado en una línea distinta de su demarcación y lo que importa
 * es dónde lo puso su dueño. */
function campoDeUnOnce(once) {
  const linea = (k) => {
    const gente = once.filter((j) => j.linea === k);
    if (!gente.length) return '';
    return `<div class="campo__linea">${gente.map((j) => U.ficha({
      nombre: j.nombre || '?',
      foto: j.foto,
      puntos: j.puntos,
      // `jugado === false` es "su partido aún no se ha disputado". `null` es "no se pudo
      // saber" (el calendario no respondió), y entonces NO se promete un guion.
      pendiente: j.jugado === false,
      alineable: (j.estado || 'ok') === 'ok',
      etiqueta: j.estado && j.estado !== 'ok' ? U.estadoJugador(j.estado) : null,
    })).join('')}</div>`;
  };
  return `<div class="campo"><div class="campo__lineas"></div><div class="campo__centro"></div>
    ${linea('goalkeeper')}${linea('defender')}${linea('midfield')}${linea('striker')}
  </div>`;
}

export function vistaEquipoDe(e) {
  if (!e) return U.cargando('Cargando la plantilla…');
  if (e.error) {
    return U.aviso({ tono: 'crit', titulo: 'No se pudo cargar el equipo', texto: e.error });
  }

  const jugador = (j) => `
    <div class="fila-rival">
      ${j.foto ? U.retrato(j.foto, j.nombre) : ''}
      <div class="fila-rival__ppal">
        <div class="fila-rival__nom">
          ${U.chipPos(j.posicion)}
          <a class="enlace-ficha" href="#jugador/${j.player_id}">${U.esc(j.nombre || '?')}</a>
          ${j.estado && j.estado !== 'ok' ? U.etiqueta(U.estadoJugador(j.estado), 'crit') : ''}
        </div>
        <div class="fila-rival__datos">
          <span>${j.puntos} pts</span>
          <span>${U.millones(j.valor)}</span>
          <span>cláusula ${U.millones(j.clausula)}</span>
          ${j.ratio ? `<span class="${j.ratio <= 1.05 ? 'es-mal-txt' : ''}">×${j.ratio}</span>` : ''}
        </div>
      </div>
      <div class="fila-rival__acc">
        ${j.blindado ? U.etiqueta('blindado', 'ok')
          : j.atacable
            ? (e.es_mio ? U.etiqueta('expuesto', 'crit')
               : (j.clausula <= e.mi_caja
                  ? U.boton('Clausular', { tono: 'accion', accion: 'clausulazo', arg: j.nombre })
                  : U.etiqueta('no te llega', '')))
            : U.etiqueta(`protegido`, '')}
      </div>
    </div>`;

  const pills = Array.from({ length: e.jornada_actual }, (_, i) => i + 1).map((n) => `
    <button class="jornada-pill" data-accion="ver-jornada" data-arg="${n}"
      aria-selected="${n === e.jornada}">J${n}</button>`).join('');

  return `
    ${U.boton('← Volver', { accion: 'cerrar-ficha' })}

    ${U.tarjeta(U.esc(e.manager || '?'), `
      <div class="ficha__datos">
        <div><span>Puntos</span><b>${e.puntos ?? '-'}</b></div>
        <div><span>Posición</span><b>${e.posicion_liga ?? '-'}º</b></div>
        <div><span>Plantilla</span><b>${U.millones(e.valor_plantilla)}</b></div>
        <div><span>Jugadores</span><b>${e.jugadores.length}</b></div>
        <div><span>${e.ultima_jornada_cerrada ? `J${e.ultima_jornada_cerrada}` : 'Últ. jornada'}</span><b>${e.puntos_ultima_jornada ?? '-'}</b></div>
        ${e.caja_estimada != null
          ? `<div><span>Caja${e.es_mio ? '' : ' (est.)'}</span><b>${U.millones(e.caja_estimada)}</b></div>`
          : ''}
      </div>`, { extra: e.es_mio ? 'tu equipo' : 'rival' })}
    ${e.caja_estimada != null && !e.es_mio ? U.aviso({ tono: 'info',
      titulo: `Con ${U.millones(e.caja_estimada)} puede pagar…`,
      texto: 'Caja deducida del tablón, no declarada. Es una COTA SUPERIOR: no se '
           + 'restan sus subidas de cláusula, que no se publican, así que su saldo real '
           + 'es igual o menor. Y para clausular no se puede uno endeudar, o sea que '
           + 'esta cifra es justo la que decide a quién te puede quitar.' }) : ''}

    ${!e.puede_alinear ? U.aviso({ tono: e.es_mio ? 'crit' : 'ok', icono: 'campo',
        titulo: e.es_mio ? 'NO PUEDES ALINEAR' : `${e.manager} no puede alinear`,
        lista: e.le_falta,
        texto: 'Un once incompleto anula la jornada entera (tipo 7). '
             + (e.es_mio ? 'Hay que arreglarlo antes de que abra.'
                : 'Si no lo arregla antes de que abra la jornada, puntúa 0.') }) : ''}

    ${U.tarjeta('Once de la jornada', `
      <div class="jornada-pills">${pills}</div>
      ${e.once.length ? campoDeUnOnce(e.once)
        : '<p class="vacio">Sin once guardado para esa jornada.</p>'}`,
      { extra: `${e.puntos_once} pts${e.pendientes ? ` · ${e.pendientes} sin jugar` : ''}`,
        pie: 'Es el once CONGELADO de esa jornada: el que puntuó de verdad, no el que '
           + 'tiene preparado ahora.'
           + (e.pendientes ? ' El guion (-) es un partido que todavía no se ha jugado, '
                           + 'que no es lo mismo que un cero.' : '') })}

    ${!e.es_mio && e.atacables_que_me_llegan.length ? U.tarjeta(
      'Atacables que te caben en caja',
      e.atacables_que_me_llegan.map(jugador).join(''),
      { extra: `${e.atacables_que_me_llegan.length}`,
        pie: `Tu caja: ${U.millones(e.mi_caja)}. Sin blindaje ni protección de 14 días.` }) : ''}

    ${U.tarjeta('Plantilla completa', e.jugadores.map(jugador).join(''),
      { extra: `${e.jugadores.length} jugadores` })}`;
}

/* Momentum
   Dos listas separadas y el aviso del backtest al lado: el ranking de momentum
   completo y, aparte, los que además se pueden comprar (agentes libres dentro de
   la caja). Casi nunca coinciden. */

export function vistaMomentum({ momentum, mercado }) {
  if (!momentum) return U.aviso({ tono: 'warn', titulo: 'Sin datos de momentum', texto: '' });
  const m = momentum;

  /* El botón de pujar necesita el `market_id` de la ficha y `/informe/momentum` solo trae
     el `player_id`, así que se cruza aquí con el mercado, que ya está cargado. Pasando el
     nombre, el manejador (que busca por id de ficha) responde siempre "esa ficha ya no
     está en el mercado", y el botón no funciona. */
  const fichaDe = new Map();
  for (const f of (mercado || [])) {
    const pid = String((f.playerMaster || {}).id || '');
    if (pid) fichaDe.set(pid, String(f.id));
  }

  const fila = (x, comprable) => `
    <div class="fila-mom">
      <div class="fila-mom__ppal">
        ${U.chipPos(x.posicion === 1 ? 'POR' : x.posicion === 2 ? 'DEF'
          : x.posicion === 3 ? 'MED' : x.posicion === 4 ? 'DEL' : '?')}
        <a class="enlace-ficha" href="#jugador/${x.player_id}">${U.esc(x.nombre || '?')}</a>
        <span class="fila-mom__val">${U.millones(x.valor)}</span>
      </div>
      <div class="fila-mom__pcts">
        <span class="${x.sube_1d_pct >= 0 ? 'es-sube' : 'es-baja'}">1d ${x.sube_1d_pct >= 0 ? '+' : ''}${x.sube_1d_pct}%</span>
        <span class="u-dim">3d ${pct(x.sube_3d_pct)}</span>
        <span class="u-dim">7d ${pct(x.sube_7d_pct)}</span>
      </div>
      ${comprable ? `<div class="fila-mom__acc">${!x.me_llega
        ? U.etiqueta('no te llega', '')
        : fichaDe.has(String(x.player_id))
          ? U.boton('Pujar', { tono: 'accion', accion: 'pujar',
                               arg: fichaDe.get(String(x.player_id)) })
          : U.etiqueta('sin ficha', '')}</div>` : ''}
    </div>`;

  const suben = (m.comprables || []).filter((c) => c.sube_1d_pct > 0.5);

  return `
    ${U.aviso({ tono: 'info', titulo: 'Lo que dice el backtest',
      texto: 'Un modelo de 8 variables NO bate a la inercia. Pero comprar '
           + 'lo que más subió ayer rinde +2,5 %/día sobre la media y persiste ~5 días, '
           + 'validado sobre 326 días de la temporada 24/25: gana 322. '
           + 'Lo que limita la señal no es la muestra, son las ocasiones: solo se puede '
           + 'comprar a un agente libre, y casi ninguno viene subiendo.' })}

    ${U.tarjeta('Se puede comprar', suben.length
      ? suben.map((c) => fila(c, true)).join('')
      : `<p class="vacio">Ninguno de los ${(m.comprables || []).length} agentes libres sube hoy.</p>`,
      { extra: `caja ${U.millones(m.caja)}`,
        pie: 'Solo agentes libres: la subasta los resuelve sola, a las 13:48 del día '
           + 'siguiente. Una ficha de otro manager depende de que él acepte.' })}

    ${U.tarjeta('Los que más suben de toda la liga', (m.suben || []).map((x) => fila(x, false)).join('')
      || '<p class="vacio">Hoy no sube nadie.</p>',
      { extra: `${(m.suben || []).length}`,
        pie: 'Referencia: la mayoría no está a la venta. Sirve para saber por dónde va el mercado.' })}`;
}

/* Backtesting
   Con 38 jornadas y una liga pequeña la varianza es enorme: sin esto, al final
   gana alguien y no se sabe si fue el mejor sistema o el más afortunado. */

export function vistaBacktest({ backtest }) {
  if (!backtest) return U.aviso({ tono: 'warn', titulo: 'Sin datos de backtest', texto: '' });
  const filas = (backtest.modelos || []).map((m) => `
    <div class="trabajo">
      <div class="trabajo__cab">
        <span class="trabajo__punto ${m.fiable ? 'es-bien' : ''}"></span>
        <b class="trabajo__nombre">${U.esc(m.model)}</b>
        <span class="trabajo__meta">${m.n} predicciones desde ${U.esc(m.desde)}</span>
      </div>
      <div class="ficha__datos">
        <div><span>Acierto de signo</span><b>${(m.acierto_signo * 100).toFixed(1)}%</b></div>
        <div><span>Error medio</span><b>${m.error_medio}%</b></div>
        <div><span>Movimiento real</span><b>${m.movimiento_real_medio > 0 ? '+' : ''}${m.movimiento_real_medio}%</b></div>
        <div><span>¿Fiable?</span><b>${m.fiable ? 'sí (n≥100)' : 'no, n corta'}</b></div>
      </div>
    </div>`).join('');

  return `
    ${U.aviso({ tono: 'warn', titulo: 'Un 100 % es una alarma, no un éxito',
      texto: 'Un acierto de signo perfecto lo saca también la inercia: el 38,6 % de los '
           + 'días el valor no cambia, y ordenar por |predicción| selecciona rachas '
           + 'monótonas. Toda métrica necesita una referencia tonta al lado.' })}
    ${U.tarjeta('Modelos evaluados', filas || '<p class="vacio">Aún no hay nada liquidado.</p>', {
      extra: `${backtest.pendientes_de_liquidar} pendientes`,
      pie: U.esc(backtest.nota || '') })}`;
}

/* Histórico de mercado
   Lo que se paga DE VERDAD frente al valor de mercado: es lo que calibra cuánto
   sobrepujar. Necesita la serie propia de precios para saber cuánto vale cada
   jugador el día de la operación. */

export function vistaHistMercado({ histMercado }) {
  if (!histMercado || histMercado.error) {
    return U.aviso({ tono: 'warn', titulo: 'Sin histórico de mercado',
      texto: histMercado?.error || '' });
  }
  const h = histMercado;
  const filas = (h.operaciones || []).map((o) => `
    <div class="fila-rival">
      <div class="fila-rival__ppal">
        <div class="fila-rival__nom">
          ${U.chipPos(o.posicion)}<b>${U.esc(o.jugador || '?')}</b>
          ${U.etiqueta(o.operacion === 'sale' ? 'venta' : 'compra',
            o.operacion === 'sale' ? 'ok' : '')}
        </div>
        <div class="fila-rival__datos">
          <span>${U.fecha(o.fecha)}</span>
          <span>${U.millones(o.importe)}</span>
          ${o.valor_ese_dia ? `<span class="u-dim">valía ${U.millones(o.valor_ese_dia)}</span>` : ''}
        </div>
      </div>
      <div class="fila-rival__acc">${o.sobre_valor_pct !== null
        ? `<b class="${o.sobre_valor_pct >= 0 ? 'es-sube' : 'es-baja'}">${
            o.sobre_valor_pct >= 0 ? '+' : ''}${o.sobre_valor_pct}%</b>`
        : U.etiqueta('sin serie', '')}</div>
    </div>`).join('');

  return `
    ${U.tarjeta('Lo que se paga de verdad', `
      <div class="ficha__datos">
        <div><span>Ventas</span><b>${h.media_sobre_valor_ventas_pct > 0 ? '+' : ''}${h.media_sobre_valor_ventas_pct}%</b>
          <em>sobre el valor</em></div>
        <div><span>Compras</span><b>${h.media_sobre_valor_compras_pct > 0 ? '+' : ''}${h.media_sobre_valor_compras_pct}%</b>
          <em>media, la disparan los clausulazos</em></div>
        <div><span>Operaciones</span><b>${h.n}</b><em>${h.con_valor_conocido} con valor</em></div>
      </div>`, {
      pie: 'LaLiga paga alrededor del valor, no por debajo: las ventas medidas van de '
         + '−5,4 % a +9 %. Es lo que permite calibrar cuánto sobrepujar.' })}
    ${U.tarjeta('Últimas operaciones de la liga', filas, { extra: `${h.n}`,
      pie: U.esc(h.nota || '') })}`;
}

/* Buscador de jugadores
   El maestro entero (`ctx.porId`, más de 800 fichas) está en el navegador desde el
   arranque, así que buscar aquí no cuesta ni una petición contra LaLiga. */

export const buscadorUI = { texto: '', pos: '', limite: 40 };

export function vistaJugadores() {
  const q = buscadorUI.texto.trim().toLowerCase();
  const todos = [...ctx.porId.values()];

  let res = todos;
  if (q) res = res.filter((p) => (p.nombre || '').toLowerCase().includes(q));
  if (buscadorUI.pos) res = res.filter((p) => String(p.posicion_id) === buscadorUI.pos);
  res.sort((a, b) => (b.puntos || 0) - (a.puntos || 0));
  const vistos = res.slice(0, buscadorUI.limite);

  const filas = vistos.map((p) => `
    <div class="fila-rival">
      ${p.foto ? U.retrato(p.foto, p.nombre) : ''}
      <div class="fila-rival__ppal">
        <div class="fila-rival__nom">
          ${U.chipPos(U.POS[p.posicion_id] || '?')}
          <a class="enlace-ficha" href="#jugador/${p.id}">${U.esc(p.nombre || '?')}</a>
          ${U.etiqueta(nombreEquipo(p.team_id) || '-')}
        </div>
        <div class="fila-rival__datos">
          <span>${p.puntos || 0} pts</span>
          <span>${U.millones(p.valor)}</span>
        </div>
      </div>
    </div>`).join('');

  return `
    ${U.tarjeta('Buscar jugador', `
      <div class="tarjeta__cuerpo u-pila">
        <div>
          <label class="u-label" for="bj-texto">Nombre</label>
          <input class="campo-texto" id="bj-texto" type="search" autocomplete="off"
            value="${U.esc(buscadorUI.texto)}" placeholder="Escribe parte del nombre…">
        </div>
        ${U.desplegable({ id: 'bj-pos', valor: buscadorUI.pos, etiqueta: 'Posición',
          opciones: [['', 'Todas'], ['1', 'Portero'], ['2', 'Defensa'],
                     ['3', 'Centrocampista'], ['4', 'Delantero'], ['5', 'Entrenador']] })}
      </div>`, {
      extra: `${res.length} de ${todos.length}`,
      pie: 'El maestro está en el navegador desde que arranca la app: buscar aquí no gasta '
         + 'ni una petición contra LaLiga.' })}

    ${U.tarjeta('Resultados', filas || '<p class="vacio">Nadie coincide.</p>', {
      extra: `${vistos.length}`,
      pie: res.length > vistos.length
        ? `Se muestran los ${vistos.length} de más puntos; afina la búsqueda para ver el resto.`
        : '' })}`;
}

/* Once óptimo vs el tuyo
   `profitable/{jornada}` da el once más RENTABLE (puntos por millón), no el de
   más puntos: es una referencia de cuánto se deja en la mesa, no algo a copiar. */

export const optimoUI = { jornada: null, datos: null, cargando: false };

export function vistaOptimo({ estado }) {
  const actual = estado?.jornada?.numero || 1;
  // `actual - 1` por lo mismo que en `bloqueClasifJornada`: `weekNumber` ya apunta a la
  // siguiente en cuanto la anterior cierra.
  const n = optimoUI.jornada ?? Math.max(1, actual - 1);
  const pills = Array.from({ length: Math.max(1, actual) }, (_, i) => i + 1).map((j) => `
    <button class="jornada-pill" data-accion="ver-optimo" data-arg="${j}"
      aria-selected="${j === n}">J${j}</button>`).join('');

  const d = optimoUI.datos;
  const cuerpo = optimoUI.cargando ? U.cargando()
    : !d ? '<p class="vacio">Elige una jornada.</p>'
    : d.error ? U.aviso({ tono: 'warn', titulo: 'No disponible', texto: d.error })
    /* Los campos vienen ANIDADOS (`d.tuyo.puntos`, `d.optimo.valor`), no planos. Leerlos
       mal no da error: con `undefined ?? '-'` la pantalla sale entera con guiones y
       «0,00M», como si no hubiera datos habiéndolos. Un desajuste de forma se disfraza
       de «sin datos», así que ante una pantalla vacía hay que mirar la respuesta. */
    : `
      <div class="ficha__datos">
        <div><span>Tu once</span><b>${d.tuyo?.puntos ?? '-'} pts</b>
          <em>${U.millones(d.tuyo?.valor)}</em></div>
        <div><span>El más rentable</span><b>${d.optimo?.puntos ?? '-'} pts</b>
          <em>${U.millones(d.optimo?.valor)}</em></div>
        <div><span>Tu eficiencia</span><b>${d.tuyo?.pts_por_millon ?? '-'}</b><em>pts/M</em></div>
        <div><span>La suya</span><b>${d.optimo?.pts_por_millon ?? '-'}</b><em>pts/M</em></div>
        ${d.diferencia != null
          ? `<div><span>Dejado en la mesa</span><b>${d.diferencia} pts</b><em>de diferencia</em></div>`
          : ''}
      </div>
      ${(d.optimo?.once || []).length
        /* `rotulo` y no `u-label` a secas: una fila suelta dentro de una tarjeta NO
           hereda el padding (`.tarjeta` no lo pone para que el campo pueda ir a
           sangre), y sin él el texto sale pegado al borde y cortado por la derecha. */
        ? `<p class="u-label rotulo">El once más rentable de la J${d.jornada}${
            (d.optimo.formacion || []).length ? ` · ${d.optimo.formacion.join('-')}` : ''}</p>
           ${campoDeUnOnce(d.optimo.once)}`
        : ''}
      ${(d.te_faltaron || []).length ? U.tarjeta('Los que te faltaron',
        d.te_faltaron.map((j) => `
          <div class="fila">
            <div class="fila__cuerpo">
              <p class="fila__titulo">${U.chipPos(j.posicion || '?')} ${U.esc(j.nombre || '?')}</p>
              <p class="fila__sub">${U.millones(j.valor)}</p>
            </div>
            <span class="u-num"><b>${j.puntos ?? ''}</b> pts</span>
          </div>`).join(''),
        { extra: `${d.te_faltaron.length} de 11`,
          pie: 'Estaban en el once óptimo y no en el tuyo.' }) : ''}`;

  return U.tarjeta('Tu once contra el más rentable', `
    <div class="jornada-pills">${pills}</div>${cuerpo}`, {
    extra: `J${n}`,
    pie: '«Rentable» aquí es PUNTOS POR MILLÓN, no puntos absolutos: no es un once a '
       + 'copiar, es la medida de cuánto rendimiento se deja en la mesa por tener '
       + 'plantilla cara.' });
}

/* Puntos en vivo */

export function vistaEnVivo({ enVivo }) {
  if (!enVivo) return U.aviso({ tono: 'warn', titulo: 'Sin datos en vivo', texto: '' });

  /* "Jornada abierta" ≠ "partido rodando", y `en_juego` es lo segundo: mirando solo ese
     campo, una jornada viva entre partidos sale como «no hay jornada en juego» y 0
     puntos teniendo puntos hechos. El acumulado se enseña SIEMPRE que la jornada esté
     abierta; lo minuto a minuto, solo cuando de verdad hay partidos. */
  if (!enVivo.jornada_abierta && !enVivo.en_juego) {
    return U.aviso({ tono: 'info', titulo: 'No hay jornada en juego',
      texto: `${enVivo.motivo || ''} Esta pantalla solo tiene sentido con partidos rodando; `
           + 'la lista vacía no es un error.' });
  }

  /* El nombre sale del maestro: `live-points` solo trae `playerMasterId`, que es por
     donde hay que cruzar (por `id` no casa con nadie y las filas salen vacías). */
  const fila = (j) => `
    <div class="rival-once__j">
      <span>${U.esc(ctx.porId.get(String(j.playerMasterId))?.nombre || '?')}</span>
      <b>${j.points ?? 0}</b>
    </div>`;

  /* El once congelado: lo que de verdad puntúa esta jornada. Un jugador sin partido
     todavía se marca como pendiente, NO como cero: son cosas opuestas. */
  const filaOnce = (j) => `
    <div class="rival-once__j">
      <span>${U.esc(j.nombre || '?')}</span>
      <b>${j.ha_jugado ? (j.puntos ?? '–') : '<span class="tenue">⏳</span>' /* sin-rastro-ok: se pinta en pantalla */}</b>
    </div>`;

  /* Las filas VAN dentro de `.rival-once`: el padding lateral lo pone el contenedor,
     no `.rival-once__j`. Sueltas en la tarjeta salen pegadas al borde y desbordan por
     la derecha. */
  const acumulado = enVivo.jornada_abierta ? U.tarjeta(
    `Jornada ${enVivo.jornada}: lo que llevas`,
    `<div class="rival-once">${(enVivo.once || []).map(filaOnce).join('')}</div>`,
    { extra: `${enVivo.mis_puntos_jornada} pts · ${enVivo.jugadores_completados}/${enVivo.de}` },
  ) : '';

  if (!enVivo.en_juego) {
    return acumulado + U.aviso({ tono: 'info', titulo: 'Ahora mismo no juega nadie',
      texto: `${enVivo.motivo || ''}. Los puntos de arriba son los ya consolidados; `
           + 'el detalle minuto a minuto vuelve cuando empiece el próximo partido.' });
  }

  const mios = (enVivo.jugadores || []).filter((j) => j.es_mio);
  return `
    ${acumulado}
    ${U.tarjeta('Tus puntos ahora mismo', mios.length
      ? `<div class="rival-once">${mios.map(fila).join('')}</div>`
      : '<p class="vacio">Ninguno de los tuyos está jugando.</p>',
      { extra: `${enVivo.mis_puntos_en_vivo} pts` })}
    ${U.tarjeta('Toda la liga',
      `<div class="rival-once">${(enVivo.jugadores || []).slice(0, 40).map(fila).join('')}</div>`,
      { extra: `${(enVivo.jugadores || []).length}` })}`;
}

/* Calendario de dificultad
   Un jugador no vale lo mismo según el rival, y la dificultad NO es la misma para
   un defensa que para un delantero: cuenta el ataque del rival en un caso y su
   defensa en el otro. */

const COLOR_DIF = { 1: 'dif--1', 2: 'dif--2', 3: 'dif--3', 4: 'dif--4', 5: 'dif--5' };

export function vistaCalendario({ calDif }) {
  if (!calDif) {
    return U.aviso({ tono: 'warn', titulo: 'Sin calendario de dificultad',
      texto: 'Hace falta recorrer varias jornadas; si la API va lenta, falla.' });
  }
  const d = calDif;
  const jornadas = (d.jugadores[0]?.proximas || []).map((x) => x.jornada);

  const fila = (j) => `
    <div class="fila-cal">
      <div class="fila-cal__ppal">
        ${U.chipPos(j.posicion)}
        <a class="enlace-ficha" href="#jugador/${j.player_id}">${U.esc(j.nombre || '?')}</a>
        ${j.riesgo_rotacion ? U.etiqueta('rotación', 'warn') : ''}
        ${j.estado && j.estado !== 'ok' ? U.etiqueta(U.estadoJugador(j.estado), 'crit') : ''}
      </div>
      <div class="fila-cal__celdas">
        ${j.proximas.map((x) => `
          <span class="dif ${COLOR_DIF[x.nivel] || 'dif--0'}"
            title="${U.esc(x.etiqueta || '')}${x.rival ? ` · ${nombreEquipo(x.rival) || 'equipo ' + x.rival}` : ''}${
              x.porteria_cero ? ` · portería a cero ${Math.round(x.porteria_cero * 100)} %` : ''}">
            ${x.rival ? (nombreCorto(x.rival) || x.rival): '-'}
            ${x.en_casa === false ? '<em>fuera</em>' : ''}
          </span>`).join('')}
      </div>
      <div class="fila-cal__fin">
        <span class="u-label">${j.prob_titular !== null
          ? `titular ${Math.round(j.prob_titular * 100)} %` : 'sin dato'}</span>
      </div>
    </div>`;

  const porterosDef = d.jugadores.filter((j) => ['POR', 'DEF'].includes(j.posicion));
  const cero = porterosDef.map((j) => ({
    nombre: j.nombre, player_id: j.player_id, posicion: j.posicion,
    p: j.proximas.find((x) => x.porteria_cero != null)?.porteria_cero,
  })).filter((x) => x.p != null).sort((a, b) => b.p - a.p);

  return `
    ${U.aviso({ tono: 'info', titulo: 'Cómo leer esto',
      texto: 'La dificultad depende de la POSICIÓN: para un defensa cuenta el ataque del '
           + 'rival; para un delantero, su defensa. Un equipo puede ser blando para lo uno '
           + 'y durísimo para lo otro. Verde = fácil, rojo = durísimo.' })}

    ${U.tarjeta(`Próximas ${d.jornadas} jornadas`, `
      <div class="cal-cab">
        <span></span>
        <div class="fila-cal__celdas">${jornadas.map((n) => `<span class="cal-jor">J${n}</span>`).join('')}</div>
        <span></span>
      </div>
      ${d.jugadores.map(fila).join('')}`, {
      extra: `desde J${d.desde_jornada}`,
      pie: U.esc(d.nota || '') })}

    ${cero.length ? U.tarjeta('Probabilidad de portería a cero (próxima jornada)',
      cero.map((x) => `
        <div class="home__linea">
          <span>${U.chipPos(x.posicion)}
            <a class="enlace-ficha" href="#jugador/${x.player_id}">${U.esc(x.nombre)}</a></span>
          <b class="${x.p >= 0.35 ? 'es-sube' : x.p <= 0.22 ? 'es-baja' : ''}">${Math.round(x.p * 100)} %</b>
        </div>`).join(''), {
      pie: '⚠️ Aproximada desde el ataque del rival, no calibrada: lo correcto serían las '  // sin-rastro-ok: se pinta en pantalla
         + 'cuotas de apuestas. Sirve para ordenar porteros y defensas entre sí.' }) : ''}`;
}

/* Premium
   Va por LIGA, no por cuenta: si no lo es, no lo es para nadie y no hay
   desventaja que compensar. Lo que aporta esta pantalla es que, si algún día
   se activa, el sistema ya sabe qué aparece y qué reglas lo gobiernan. */

export function vistaPremium({ premium }) {
  if (!premium) {
    return U.aviso({ tono: 'warn', titulo: 'No se pudo leer la configuración de la liga',
      texto: '' });
  }
  const p = premium;

  const ficha = (f) => `
    <div class="prem ${f.activa ? 'prem--on' : ''}">
      <div class="prem__cab">
        <span class="prem__punto ${f.activa ? 'es-bien' : ''}"></span>
        <b>${U.esc(f.titulo)}</b>
        ${U.etiqueta(f.activa ? 'activa' : 'no disponible', f.activa ? 'ok' : '')}
        ${f.activa && !f.implementada
          ? U.etiqueta('sin implementar', 'crit') : ''}
      </div>
      <p class="prem__que">${U.esc(f.que_hace)}</p>
      <p class="prem__impacto">${U.esc(f.impacto)}</p>
      ${f.regla ? `<p class="prem__regla">⚖️ ${U.esc(f.regla)}</p>` : '' /* sin-rastro-ok: se pinta en pantalla */}
      ${f.ajustes ? `<p class="prem__regla">⚙️ ${U.esc(JSON.stringify(f.ajustes))}</p>` : '' /* sin-rastro-ok: se pinta en pantalla */}
      ${f.rutas?.length ? `<ul class="prem__rutas">${
        f.rutas.map((r) => `<li>${U.esc(r)}</li>`).join('')}</ul>` : ''}
    </div>`;

  return `
    ${p.premium
      ? U.aviso({ tono: 'crit', titulo: `Esta liga ES premium (${p.activas} funciones)`,
          texto: 'Hay funciones activas que el sistema NO implementa todavía. Las marcadas '
               + '«sin implementar» son trabajo pendiente real, no informativo.' })
      : U.aviso({ tono: 'ok', titulo: 'Esta liga NO es premium',
          texto: 'Y va por LIGA, no por cuenta: tampoco lo tienen los rivales, así que '
               + 'no hay desventaja que compensar. Nada de esto hay que perseguirlo hoy.' })}

    ${U.tarjeta('Funciones', p.funciones.map(ficha).join(''), {
      extra: `${p.activas} de ${p.total}`,
      pie: 'Se conocen las rutas, pero NINGUNA se ha ejecutado nunca. Si la liga pasara '
         + 'a premium, esto es el punto de partida en vez de descubrirlo a base de 400s.' })}`;
}


/* Trading: comprar para vender
   Modo PROPUESTA. No hay botón de comprar a propósito: la señal es fuerte
   (77,6 % de persistencia) pero solo el 10,6 % de los agentes libres está en
   racha y el retorno real está medido sobre una muestra mínima. Primero se mide,
   después se automatiza. */
export function vistaTrading() {
  const cont = 'trading-cont';
  setTimeout(async () => {
    const el = document.getElementById(cont);
    if (!el) return;
    try {
      const [op, cart, bal] = await Promise.all([
        api.tradingOportunidades(), api.tradingCartera(), api.tradingBalance(),
      ]);
      el.innerHTML = pintarTrading(op, cart, bal);
    } catch (e) {
      el.innerHTML = U.aviso({ tono: 'warn', titulo: 'No se pudo cargar', texto: String(e) });
    }
  }, 0);
  return `<div id="${cont}">${U.cargando()}</div>`;
}

function pintarTrading(op, cart, bal) {
  const M = (n) => `${(n / 1e6).toFixed(2)}M`;
  const cap = op.capital || {};

  const aviso = U.aviso({
    tono: 'info', titulo: 'Esto propone, no compra',
    texto: 'Lo que subió ayer sube hoy el 77,6 % de las veces (+2,36 %). Pero solo el '
         + '10,6 % de los agentes libres viene subiendo, y el ciclo completo de 3 días '
         + 'está medido sobre 6 casos: dio +5,31 %, sin la corrección a la baja que la '
         + 'temporada 24/25 impuso al momentum (+2,5 %/día, no +5,5 %). Mientras el '
         + 'balance de abajo no lo confirme, esto es una hipótesis.',
  });

  const cabecera = U.tarjeta('Capital de trading',
    `<div class="rival-once">
       <div class="rival-once__j"><span>Tope</span><b>${M(cap.tope || 0)}</b></div>
       <div class="rival-once__j"><span>Invertido</span><b>${M(cap.invertido || 0)}</b></div>
       <div class="rival-once__j"><span>Libre</span><b>${M(cap.libre || 0)}</b></div>
       <div class="rival-once__j"><span>Posiciones abiertas</span><b>${op.abiertas} / ${(op.criterio || {}).plazas_max}</b></div>
       <div class="rival-once__j"><span>Plazas de plantilla</span><b>${op.plazas_libres}</b></div>
     </div>`, { extra: op.bloqueo ? 'bloqueado' : 'operativo' });

  const bloqueo = op.bloqueo
    ? U.aviso({ tono: 'warn', titulo: 'Hoy no se opera', texto: op.bloqueo })
    : '';

  const cands = (op.candidatos || []).length
    ? U.tarjeta('Candidatos de hoy',
        `<div class="rival-once">${op.candidatos.map((c) => `
          <div class="rival-once__j">
            <span>${U.esc(c.nombre)} · ${M(c.valor)} · ${c.pujas} pujas</span>
            <b>${(c.momentum * 100).toFixed(1)}% → pujar ${c.pujar.toLocaleString('es-ES')} €</b>
          </div>`).join('')}</div>`, { extra: `${op.candidatos.length}` })
    : U.tarjeta('Candidatos de hoy',
        '<p class="vacio">Ninguno cumple. Es lo normal: el 56,7 % de lo que sale al '
        + 'mercado libre viene bajando.</p>');

  const desc = (op.descartados || []).length
    ? U.tarjeta('Descartados y por qué',
        `<div class="rival-once">${op.descartados.slice(0, 12).map((c) => `
          <div class="rival-once__j"><span>${U.esc(c.nombre || '?')}</span>
          <b class="tenue">${U.esc(c.motivo || '')}</b></div>`).join('')}</div>`,
        { extra: `${op.descartados.length}` })
    : '';

  const abiertas = (cart.abiertas || []).length
    ? U.tarjeta('Cartera',
        `<div class="rival-once">${cart.abiertas.map((o) => `
          <div class="rival-once__j">
            <span>${U.esc(o.nombre)} · ${o.dias.toFixed(1)} días</span>
            <b class="${o.toca_vender ? 'u-accent' : ''}">${
              U.esc(o.toca_vender ? 'toca vender' : o.estado)}</b>
          </div>`).join('')}</div>`, { extra: M(cart.invertido || 0) })
    : U.tarjeta('Cartera', '<p class="vacio">Sin posiciones abiertas.</p>');

  const balance = U.tarjeta('Balance realizado',
    bal.n
      ? `<div class="rival-once">
           <div class="rival-once__j"><span>Operaciones cerradas</span><b>${bal.n}</b></div>
           <div class="rival-once__j"><span>Beneficio</span><b>${(bal.beneficio_total || 0).toLocaleString('es-ES')} €</b></div>
           <div class="rival-once__j"><span>Retorno medio</span><b>${((bal.retorno_medio || 0) * 100).toFixed(2)}%</b></div>
           <div class="rival-once__j"><span>Acierto</span><b>${((bal.acierto || 0) * 100).toFixed(0)}%</b></div>
         </div>`
      : '<p class="vacio">Todavía ninguna operación cerrada.</p>',
    { extra: bal.n ? `${bal.n}`: '-' });

  /* Los descartados van DENTRO de la pantalla: una lista de candidatos sin los que se
     han caído obliga a rehacer el análisis para saber si fiarse de ella. */
  return aviso + cabecera + bloqueo + cands + desc + abiertas + balance
       + U.aviso({ tono: 'info', titulo: 'Nota', texto: bal.aviso || '' });
}

/* Caja de cada manager
   Es lo que decide a quién te pueden clausular, así que vive junto a la
   clasificación. Se pinta asíncrono para no retrasar la vista. */
function bloqueCajaRival() {
  const id = 'caja-rival';
  setTimeout(async () => {
    const el = document.getElementById(id);
    if (!el) return;
    try {
      const d = await api.saldos();
      const M = (n) => `${(n / 1e6).toFixed(2)}M`;
      const filas = d.saldos.map((x) => `
        <div class="rival-once__j${x.es_mio ? ' fila--mia' : ''}">
          <span>${U.esc(x.manager)}${x.es_mio ? ' (tú)' : ''}</span>
          <b>${M(x.caja_estimada)}<span class="tenue"> · plantilla ${M(x.valor_plantilla)}</span></b>
        </div>`).join('');
      el.innerHTML = U.tarjeta('Caja estimada de cada manager',
        `<div class="rival-once">${filas}</div>`,
        { extra: `${d.eventos_en_el_tablon} eventos` })
        + U.aviso({ tono: 'info', titulo: 'Cómo leerlo', texto: d.aviso });
    } catch (e) {
      el.innerHTML = U.aviso({ tono: 'warn', titulo: 'Sin caja rival', texto: String(e) });
    }
  }, 0);
  return `<div id="${id}"></div>`;
}

/* Tácticas: el catálogo de jugadas de mercado
   Modo PROPUESTA, como Trading. Aquí NO hay botones de ejecutar a propósito:
   implementar una táctica no es adoptarla, están disponibles por si se quieren usar.

   Cada bloque enseña la ventaja medida Y su muestra al lado, para que el porcentaje
   no se pueda leer sin la «n» que lo sostiene. */
export function vistaTacticas() {
  const cont = 'tacticas-cont';
  setTimeout(async () => {
    const el = document.getElementById(cont);
    if (!el) return;
    const modo = (document.getElementById('tac-modo') || {}).value || 'disuasorio';
    try {
      const [cat, esc, cla, riv] = await Promise.all([
        api.tacticas(), api.tacticaEscaparate(),
        api.tacticaClausulas(modo), api.tacticaRivales(),
      ]);
      el.innerHTML = pintarTacticas(cat, esc, cla, riv, modo);
    } catch (e) {
      el.innerHTML = U.aviso({ tono: 'warn', titulo: 'No se pudo cargar', texto: String(e) });
    }
  }, 0);
  return `<div id="${cont}">${U.cargando()}</div>`;
}

// Exportada para que las pruebas puedan pintarla con datos de ejemplo: la vista se carga
// en diferido, así que un recorrido de pestañas solo ve el "Cargando…" y no prueba nada.
export function pintarTacticas(cat, esc, cla, riv, modo) {
  const M = (n) => `${(n / 1e6).toFixed(2)}M`;
  const E = (n) => (n || 0).toLocaleString('es-ES');
  const d = esc.distribucion || {};

  const intro = U.aviso({
    tono: 'info', titulo: 'Esto propone, no ejecuta',
    texto: (cat.aviso || '') + ' Cada táctica trae su ventaja medida, su muestra y qué la '
         + 'falsaría: si el número no aguanta, se apaga.',
  });

 // T1 · Escaparate
  const avisoCruce = esc.aviso
    ? U.aviso({ tono: 'warn', titulo: 'Revisar antes de listar', texto: esc.aviso })
    : '';

  // Sin muestra no hay números que enseñar: se pinta un guion en vez de "undefined%".
  // Una liga recién creada no tiene ni una venta en `market/history`.
  const pc = (v) => (v === null || v === undefined ? '-': `${v}%`);
  const dist = d.n
    ? U.tarjeta('Lo que paga el sistema, medido',
      `<div class="rival-once">
         <div class="rival-once__j"><span>Aceptando la primera oferta</span><b>${pc(d.prima_aceptando_todo)}</b></div>
         <div class="rival-once__j"><span>Aceptando solo por encima de +${esc.umbral_aceptar_pct ?? 1}%</span><b>${pc(d.prima_esperando)}</b></div>
         <div class="rival-once__j"><span>Ventaja de esperar</span><b>${d.ventaja_pp == null ? '-': `+${d.ventaja_pp} pp`}</b></div>
         <div class="rival-once__j"><span>Sale buena</span><b>${Math.round((d.prob_oferta_buena || 0) * 100)}% de los ciclos</b></div>
         <div class="rival-once__j"><span>Muestra</span><b>n=${d.n}${d.fiable ? '' : ' · ordena, no mide'}</b></div>
       </div>`, { extra: 'T1' })
    : U.tarjeta('Lo que paga el sistema, medido',
      '<p class="vacio">Todavía no hay ventas medidas en esta liga. La táctica sigue '
      + 'siendo válida (el reglamento dice ±10 % aleatorio) pero sin muestra propia no '
      + 'se puede poner un número al lado.</p>', { extra: 'T1' });

  const cands = (esc.candidatos || []).length
    ? U.tarjeta('A quién listar',
        `<div class="rival-once">${esc.candidatos.map((c) => `
          <div class="rival-once__j">
            <span>${U.esc(c.nombre)} · ${M(c.valor)}${c.prioridad !== 'banquillo' ? ` · ${U.esc(c.prioridad)}` : ''}</span>
            <b>+${E(c.ganancia_esperada_si_esperas)} €</b>
          </div>`).join('')}</div>`, { extra: `${esc.candidatos.length}` })
    : U.tarjeta('A quién listar', '<p class="vacio">Nadie: todos son titulares o hacen falta para cubrir posición.</p>');

  const descEsc = (esc.descartados || []).length
    ? U.tarjeta('Descartados y por qué',
        `<div class="rival-once">${esc.descartados.map((c) => `
          <div class="rival-once__j"><span>${U.esc(c.nombre || '?')}</span>
          <b class="u-tenue">${U.esc(c.motivo)}</b></div>`).join('')}</div>`,
        { extra: `${esc.descartados.length}` })
    : '';

 // T2 · Cláusulas
  // `opciones` son PARES [valor, texto], no objetos. Pasarlos como objetos no da un
  // error de tipo: revienta con "object is not iterable" dentro del componente.
  const selector = U.desplegable({
    id: 'tac-modo', valor: modo,
    opciones: [
      ['disuasorio', 'Disuasorio: encarecer en proporción al valor'],
      ['inalcanzable', 'Inalcanzable: por encima de la caja del más rico'],
    ],
  });

  const clausulas = U.tarjeta('Subir cláusula: dónde sale barato',
    `<div class="fila">${selector}</div>
     <p class="u-tenue rotulo">${U.esc(cla.que_significa_el_modo || '')}</p>
     <div class="rival-once">${(cla.propuestas || []).slice(0, 8).map((p) => `
       <div class="rival-once__j">
         <span>${U.esc(p.nombre)} · ${M(p.valor)} → cláusula ${M(p.subir_hasta)}</span>
         <b>${M(p.coste)} · ${E(p.coste_por_punto_protegido)} €/pt</b>
       </div>`).join('')}</div>`, { extra: 'T2' });

  const descCla = (cla.descartados || []).length
    ? U.tarjeta('No tocar, y por qué',
        `<div class="rival-once">${cla.descartados.map((c) => `
          <div class="rival-once__j"><span>${U.esc(c.nombre || '?')}</span>
          <b class="u-tenue">${U.esc(c.motivo)}</b></div>`).join('')}</div>`)
    : '';

 // T3 · Radiografía
  const rivales = U.tarjeta('Qué táctica usa cada rival',
    `<div class="rival-once">${(riv.rivales || []).map((r) => `
       <div class="rival-once__j">
         <span>${U.esc(r.manager)} · ${r.listados}/${r.plantilla} listados</span>
         <b>${r.pct_listado}%</b>
       </div>
       <div class="rival-once__j"><span class="u-tenue">${U.esc(r.lectura)}</span><b></b></div>
     `).join('')}</div>`, { extra: 'T3' });

  const leer = U.tarjeta('Cómo leerlo',
    `<div class="rival-once">${(riv.como_leerlo || []).map((t) => `
      <div class="rival-once__j"><span class="u-tenue">${U.esc(t)}</span><b></b></div>`).join('')}</div>`);

  const riesgos = U.tarjeta('Lo que cuesta (no es cero)',
    `<div class="rival-once">${(esc.riesgos || []).concat(cla.riesgos || []).map((t) => `
      <div class="rival-once__j"><span class="u-tenue">${U.esc(t)}</span><b></b></div>`).join('')}</div>`);

  const falsar = U.tarjeta('Qué las falsaría',
    `<div class="rival-once">
      <div class="rival-once__j"><span class="u-tenue">Escaparate: ${U.esc(esc.que_lo_falsaria || '')}</span><b></b></div>
      <div class="rival-once__j"><span class="u-tenue">Cláusulas: ${U.esc(cla.que_lo_falsaria || '')}</span><b></b></div>
     </div>`);

  return intro + avisoCruce + dist + cands + descEsc + clausulas + descCla
       + rivales + leer + riesgos + falsar;
}
