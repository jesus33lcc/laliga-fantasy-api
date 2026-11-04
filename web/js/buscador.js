/* ============================================================================
   buscador.js: filtrar el mercado y guardar la búsqueda para que avise sola.

   El mercado se renueva **cada día a las 13:48** con una baraja nueva, así que un
   filtro guardado se revisa en cada renovación y avisa por Telegram **solo cuando
   algo lo cumple**.

   `solo_libres` viene activado: una ficha de rival la acepta o la rechaza su dueño,
   así que no es una compra sino una petición, y lleva tres rechazos de tres.
   ========================================================================== */

import * as U from './ui.js';
import * as api from './api.js';

export const busq = {
  filtro: { posicion: '', min_puntos: 0, max_precio: '', solo_libres: true },
  res: null, avisos: [], cargando: false, error: '', aviso: '',
};

export async function buscarAhora() {
  busq.cargando = true; busq.error = '';
  try {
    busq.res = await api.buscarMercado(busq.filtro);
  } catch (e) { busq.error = e.message; }
  finally { busq.cargando = false; }
}

export async function cargarAvisos() {
  try { busq.avisos = await api.avisosMercado(); } catch { busq.avisos = []; }
}

export async function guardarComoAviso(nombre) {
  busq.error = ''; busq.aviso = '';
  try {
    await api.crearAviso({ nombre, ...busq.filtro,
      max_precio: busq.filtro.max_precio === '' ? null : Number(busq.filtro.max_precio) });
    await cargarAvisos();
    busq.aviso = 'Guardado. Te avisará por Telegram en cada mercado, solo si hay algo.';
  } catch (e) { busq.error = e.message; }
}

export async function quitarAviso(nombre) {
  try { await api.borrarAviso(nombre); await cargarAvisos(); }
  catch (e) { busq.error = e.message; }
}

/* ---------- pintado ---------- */

const POSICIONES = ['', 'POR', 'DEF', 'MED', 'DEL', 'ENT'];

export function pintarBuscador(fotoDe) {
  const f = busq.filtro;
  const controles = `
    <div class="tarjeta__cuerpo u-pila">
      <div>
        <p class="u-label">Posición</p>
        <div class="formaciones">
          ${POSICIONES.map((p) => `<button class="formacion" data-buscar-pos="${p}"
            aria-pressed="${f.posicion === p}">${p || 'todas'}</button>`).join('')}
        </div>
      </div>
      <div class="rejilla">
        <div>
          <label class="u-label" for="bq-pts">Puntos acumulados, mínimo</label>
          <input class="campo-texto u-num" id="bq-pts" inputmode="numeric" value="${f.min_puntos}">
        </div>
        <div>
          <label class="u-label" for="bq-max">Precio máximo (€, vacío = sin tope)</label>
          <input class="campo-texto u-num" id="bq-max" inputmode="numeric" value="${f.max_precio}">
        </div>
      </div>
      <label class="opcion-aviso">
        <span class="opcion-aviso__texto">
          <span class="opcion-aviso__titulo">Solo agentes libres</span>
          <span class="opcion-aviso__detalle">Lo único que se puede fichar: a un rival no se le compra.</span>
        </span>
        <input type="checkbox" class="interruptor" data-buscar-libres ${f.solo_libres ? 'checked' : ''}>
      </label>
      <div class="u-fila">
        ${U.boton(busq.cargando ? 'Buscando…' : 'Buscar', { tono: 'accion', accion: 'buscar-mercado', off: busq.cargando })}
        <span class="u-crecer"></span>
        ${U.boton('Guardar como aviso', { accion: 'guardar-busqueda', off: !busq.res })}
      </div>
    </div>`;

  const filas = (busq.res?.fichas || []).map((x) => `
    <article class="fila">
      ${U.retrato(fotoDe(x.nombre), x.nombre)}
      <div class="fila__cuerpo">
        <p class="fila__titulo">${U.chipPos(x.posicion)} ${U.esc(x.nombre)}</p>
        <p class="fila__sub">${U.dinero(x.precio)} · vale ${U.millones(x.valor)}
          ${x.libre ? '' : ' · <span class="u-crit">de un rival</span>'}</p>
        <p class="fila__sub u-dimmer">cierra ${U.fecha(x.expira)}${x.competencia ? ` · ${x.competencia} pujas` : ''}</p>
      </div>
      <div class="fila__fin">
        ${U.marcador(x.puntos)}
        ${x.libre ? U.boton('Pujar', { tono: 'accion', accion: 'pujar', arg: x.market_id })
                  : U.boton('No se compra a rivales', { off: true })}
      </div>
    </article>`).join('');

  const guardados = busq.avisos.map((a) => `
    <article class="fila">
      <div class="fila__cuerpo">
        <p class="fila__titulo">${U.icono('buscar', 'fila__icono')} ${U.esc(a.nombre)}</p>
        <p class="fila__sub">${a.posicion || 'cualquier posición'} · ≥${a.min_puntos} pts
          ${a.max_precio ? ` · ≤${U.millones(a.max_precio)}` : ''}
          ${a.solo_libres ? ' · solo libres' : ''}</p>
      </div>
      ${U.boton('Quitar', { accion: 'borrar-aviso', arg: a.nombre })}
    </article>`).join('');

  return `
    ${busq.aviso ? U.aviso({ tono: 'ok', titulo: 'Hecho', texto: busq.aviso }) : ''}
    ${busq.error ? U.aviso({ tono: 'crit', titulo: 'Algo ha fallado', texto: busq.error }) : ''}
    ${U.tarjeta('Buscar en el mercado', controles)}
    ${busq.res ? U.tarjeta('Resultados', filas || U.vacio('Nada cumple ese filtro'), {
      extra: `${busq.res.encontrados} de ${busq.res.en_mercado}`,
    }) : ''}
    ${U.tarjeta('Búsquedas guardadas', guardados || U.vacio('Ninguna guardada'), {
      extra: `${busq.avisos.length}`,
      pie: 'Se revisan a las 13:50, justo tras la renovación del mercado. Avisan por '
         + 'Telegram solo si hay algo: un bot que escribe cada día se acaba silenciando.',
    })}`;
}
