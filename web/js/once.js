/* ============================================================================
   once.js: el editor de alineación. Cuatro reglas, y el motivo de cada una:

   1. Se intercambian jugadores de la MISMA posición. Para cambiar el reparto
      está el selector de formación, que recoloca solo. Mover un defensa a la
      línea de medios produce onces que la API rechaza con un 500 sin explicar
      por qué.
   2. Solo se ofrecen las formaciones que la plantilla cubre. Con tres defensas
      exactos, un 4-4-2 no se puede formar; sale deshabilitado con el motivo.
   3. Guardar SIEMPRE fija el once. Si no queda fijado, la pasada automática lo
      recalcula y sustituye la decisión sin avisar.
   4. Un sancionado o lesionado se PUEDE alinear: la API lo permite y puntúa 0.
      Se marca en la ficha, no se prohíbe.

   Sin estado compartido con las vistas: recibe la plantilla y devuelve HTML,
   para no crear una dependencia circular con `vistas.js`.
   ========================================================================== */

import * as U from './ui.js';

/* Las mismas 7 que prueba `models/alineacion.py`. Si se tocan allí, aquí también. */
export const FORMACIONES = [[5, 4, 1], [5, 3, 2], [4, 5, 1], [4, 4, 2], [4, 3, 3], [3, 5, 2], [3, 4, 3]];

const BLOQUE_DE = { POR: 'goalkeeper', DEF: 'defender', MED: 'midfield', DEL: 'striker' };

export const editor = {
  activo: false,
  formacion: [3, 5, 2],
  once: { goalkeeper: null, defender: [], midfield: [], striker: [] },
  seleccion: null,
  sucio: false,
  aviso: '',
  guardando: false,
};

/* ---------- estado ---------- */

const posDe = (plantilla, ptid) =>
  plantilla.find((p) => String(p.player_team_id) === String(ptid))?.posicion || null;

/** Cuántos hay de cada posición: decide qué formaciones existen de verdad. */
export function inventario(plantilla) {
  const n = { POR: 0, DEF: 0, MED: 0, DEL: 0 };
  for (const p of plantilla) if (n[p.posicion] !== undefined) n[p.posicion]++;
  return n;
}

export const formacionesViables = (plantilla) => {
  const n = inventario(plantilla);
  return FORMACIONES.filter(([d, m, dl]) => n.POR >= 1 && n.DEF >= d && n.MED >= m && n.DEL >= dl);
};

/** Arranca el editor desde el once que hay guardado ahora mismo. */
export function iniciar(lineup, plantilla) {
  const f = lineup.formation || {};
  const ids = (clave) => [].concat(f[clave] || []).map((x) => String(x.playerTeamId));
  editor.once = {
    goalkeeper: ids('goalkeeper')[0] || null,
    defender: ids('defender'),
    midfield: ids('midfield'),
    striker: ids('striker'),
  };
  editor.formacion = [editor.once.defender.length, editor.once.midfield.length, editor.once.striker.length];
  editor.seleccion = null;
  editor.sucio = false;
  editor.aviso = '';
  /* Si el once guardado no cuadra con ninguna formación (pasa cuando se ha
     vendido a alguien), se cae a la primera viable y se recoloca. */
  if (!FORMACIONES.some((x) => x.join() === editor.formacion.join())) {
    const v = formacionesViables(plantilla);
    if (v.length) cambiarFormacion(v[0], plantilla);
  }
}

const enOnce = (ptid) => {
  const p = String(ptid);
  return editor.once.goalkeeper === p || ['defender', 'midfield', 'striker']
    .some((k) => editor.once[k].includes(p));
};

export const titulares = () => [editor.once.goalkeeper, ...editor.once.defender,
  ...editor.once.midfield, ...editor.once.striker].filter(Boolean);

export const banquillo = (plantilla) => plantilla.filter((p) => !enOnce(p.player_team_id));

/** Recoloca al cambiar de formación: mantiene a los que caben y rellena con los
    del banquillo que más han puntuado. */
export function cambiarFormacion(nueva, plantilla) {
  const porPos = (pos) => plantilla.filter((p) => p.posicion === pos)
    .sort((a, b) => b.puntos_temporada - a.puntos_temporada)
    .map((p) => String(p.player_team_id));

  const cupo = { defender: nueva[0], midfield: nueva[1], striker: nueva[2] };
  for (const [clave, pos] of [['defender', 'DEF'], ['midfield', 'MED'], ['striker', 'DEL']]) {
    const disponibles = porPos(pos);
    const conservados = editor.once[clave].filter((id) => disponibles.includes(id)).slice(0, cupo[clave]);
    const relleno = disponibles.filter((id) => !conservados.includes(id));
    editor.once[clave] = [...conservados, ...relleno].slice(0, cupo[clave]);
  }
  if (!editor.once.goalkeeper) editor.once.goalkeeper = porPos('POR')[0] || null;
  editor.formacion = nueva;
  editor.seleccion = null;
  editor.sucio = true;
  editor.aviso = '';
}

/** Un toque: selecciona, deselecciona o intercambia. */
export function tocar(ptid, plantilla) {
  const id = String(ptid);
  editor.aviso = '';

  if (editor.seleccion === null) { editor.seleccion = id; return; }
  if (editor.seleccion === id) { editor.seleccion = null; return; }

  const a = editor.seleccion, b = id;
  const posA = posDe(plantilla, a), posB = posDe(plantilla, b);

  if (posA !== posB) {
    editor.aviso = `No se puede cambiar un ${posA} por un ${posB}. `
                 + 'Para mover una plaza de línea, cambia la formación.';
    editor.seleccion = null;
    return;
  }
  if (!enOnce(a) && !enOnce(b)) {
    editor.aviso = 'Los dos están en el banquillo: elige uno del campo.';
    editor.seleccion = null;
    return;
  }

  const clave = BLOQUE_DE[posA];
  if (clave === 'goalkeeper') {
    editor.once.goalkeeper = enOnce(a) ? b : a;
  } else {
    const lista = editor.once[clave];
    const iA = lista.indexOf(a), iB = lista.indexOf(b);
    if (iA >= 0 && iB >= 0) { lista[iA] = b; lista[iB] = a; }
    else if (iA >= 0) lista[iA] = b;
    else lista[iB] = a;
  }
  editor.seleccion = null;
  editor.sucio = true;
}

/* ---------- validación ---------- */

export function problemas(plantilla) {
  const out = [];
  const t = titulares();
  if (t.length !== 11) out.push(`El once tiene ${t.length} jugadores, no 11: la jornada no puntuaría.`);
  if (!editor.once.goalkeeper) out.push('Falta el portero.');
  if (new Set(t).size !== t.length) out.push('Hay un jugador repetido.');
  const mios = new Set(plantilla.map((p) => String(p.player_team_id)));
  const fuera = t.filter((id) => !mios.has(id));
  if (fuera.length) out.push(`${fuera.length} jugador(es) ya no están en tu plantilla.`);
  const tocados = plantilla.filter((p) => t.includes(String(p.player_team_id)) && p.estado !== 'ok');
  if (tocados.length) {
    out.push(`Alineas a ${tocados.map((p) => `${p.nombre} (${U.estadoJugador(p.estado)})`).join(', ')}: `
           + 'la API lo permite, pero puntúan 0.');
  }
  return out;
}

export const payload = () => ({
  goalkeeper: editor.once.goalkeeper,
  defender: [...editor.once.defender],
  midfield: [...editor.once.midfield],
  striker: [...editor.once.striker],
  tactical_formation: [...editor.formacion],
});

/* ---------- pintado ---------- */

const fichaEditable = (p, foto, sel) => U.ficha({
  nombre: p.nombre, foto, alineable: p.estado === 'ok',
  ptid: p.player_team_id, seleccionable: true, seleccionada: sel,
  etiqueta: p.estado !== 'ok' ? U.estadoJugador(p.estado) : null,
});

export function pintarCampo(plantilla, fotoDe) {
  const por = (ptid) => plantilla.find((x) => String(x.player_team_id) === String(ptid));
  const linea = (ids) => {
    const fichas = ids.map((id) => {
      const p = por(id);
      return p ? fichaEditable(p, fotoDe(p.nombre), editor.seleccion === String(id))
               : U.ficha({ hueco: true, nombre: 'VACÍA' });
    });
    return `<div class="campo__linea">${fichas.join('')}</div>`;
  };
  return `<div class="campo"><div class="campo__lineas"></div><div class="campo__centro"></div>
    ${linea([editor.once.goalkeeper])}
    ${linea(editor.once.defender)}
    ${linea(editor.once.midfield)}
    ${linea(editor.once.striker)}
  </div>`;
}

export function pintarBanquillo(plantilla, fotoDe) {
  const fuera = banquillo(plantilla);
  if (!fuera.length) {
    return `<p class="banquillo__aviso u-dim">Los ${plantilla.length} jugadores están en el once.</p>`;
  }
  const grupos = ['POR', 'DEF', 'MED', 'DEL'].map((pos) => {
    const gente = fuera.filter((p) => p.posicion === pos);
    if (!gente.length) return '';
    return `<div class="banco__grupo">
      <p class="u-label">${U.POS[U.POS_ID[pos]]}</p>
      <div class="banco__fichas">${gente.map((p) =>
        fichaEditable(p, fotoDe(p.nombre), editor.seleccion === String(p.player_team_id))).join('')}</div>
    </div>`;
  }).join('');
  return `<div class="banco">${grupos}</div>`;
}

export function pintarControles(plantilla) {
  const viables = formacionesViables(plantilla);
  const n = inventario(plantilla);
  const botones = FORMACIONES.map((f) => {
    const ok = viables.some((v) => v.join() === f.join());
    const activa = f.join() === editor.formacion.join();
    const falta = ok ? '' : `no tienes ${['DEF', 'MED', 'DEL']
      .filter((p, i) => n[p] < f[i]).join(' ni ')} suficientes`;
    return `<button class="formacion" data-formacion="${f.join('-')}"
      aria-pressed="${activa}" ${ok ? '' : 'disabled'}
      title="${U.esc(falta || `Formación ${f.join('-')}`)}">${f.join('-')}</button>`;
  }).join('');
  return `<div class="controles">
    <p class="u-label">Formación</p>
    <div class="formaciones">${botones}</div>
  </div>`;
}
