/**
 * Recorre las combinaciones de pestaña y sub-pestaña contra un DOM falso y falla si
 * alguna se rompe.
 *
 * Los datos de maqueta salen de `web/dist/datos_demo.json` y, si no está, de
 * `web/demo/datos.json`. El primero no lo genera nadie, así que en una copia limpia solo
 * existe el segundo. `build_web.py` mira además `COCONUT_DATOS_DEMO` antes que los dos:
 * con esa variable puesta, el paquete se construye con unos datos y se prueba con otros.
 *
 * No es un framework de pruebas: es el mínimo que evita publicar una pantalla rota.
 * Busca tres cosas al pintar: una excepción, una salida sospechosamente corta, y las
 * cadenas `undefined` / `NaN` / `[object Object]`. Ese es el fallo que se da aquí: el
 * código no revienta, imprime un dato falso.
 *
 * Y una cuarta que no se ve pintando: que el botón de confirmar llegue a la red, con su
 * cuerpo y a su endpoint. Eso necesita una SEGUNDA instancia del paquete sin datos de
 * maqueta, porque con ellos `api.enviar()` lanza antes de llamar a `fetch`. Ver
 * `instanciaViva()`.
 *
 *   python3 scripts/build_web.py && node scripts/probar_web.mjs
 *
 * El aspecto no lo ve esto. Para medirlo, la app va en un iframe del ancho exacto:
 * `chromium --window-size` renderiza siempre a 500 px y solo cambia el tamaño de la
 * imagen, así que una captura a 390 sale recortada y aparenta un desborde que no existe.
 */
import fs from 'node:fs';
const html = fs.readFileSync(new URL('../web/dist/coconut.html', import.meta.url), 'utf8');
const datos = JSON.parse(leerDatosDemo());

function leerDatosDemo() {
  const candidatos = ['../web/dist/datos_demo.json', '../web/demo/datos.json'];
  for (const c of candidatos) {
    try { return fs.readFileSync(new URL(c, import.meta.url), 'utf8'); } catch { /* el siguiente */ }
  }
  console.error('no encuentro los datos de maqueta. Se buscan, en este orden:');
  for (const c of candidatos) console.error('  ' + c.replace('../', ''));
  process.exit(1);
}
const bundle = html.match(/<script>\n\(\(\) => \{(.*?)\}\)\(\);\n<\/script>/s)[1];

// DOM mínimo: solo lo que toca el bundle.
const nodos = {};
const nuevo = (id) => ({ id, innerHTML: '', hidden: true, style: {}, classList: { toggle() {} },
  prepend() {}, scrollIntoView() {}, focus() {}, setAttribute() {}, closest: () => null });
for (const id of ['titulo','subtabs','nav','contenido','pie','menu','menu-cuerpo',
                  'abrir-menu','cerrar-menu','velo','recargar']) nodos[id] = nuevo(id);
globalThis.__nodos = nodos;
globalThis.document = {
  body: { style: {}, appendChild() {} },
  querySelector: (s) => nodos[s.replace('#', '')] ?? nuevo(s),
  /* Las vistas que se pintan en dos tiempos (Trading, la caja rival) devuelven un
     contenedor vacío y lo rellenan en un `setTimeout` con `getElementById`. Sin esto
     revientan con «is not a function». */
  getElementById: (id) => nodos[id] ?? nuevo(id),
  /* Se guardan los creados porque `recado()` escribe el aviso ahí antes de colgarlo:
     es el único sitio donde se puede leer qué le dice la interfaz al usuario tras una
     escritura, que es justo lo que distingue «guardado» de «guardado pero no fijado». */
  createElement: () => { const n = nuevo('tmp'); creados.push(n); return n; },
  addEventListener() {},
};
const creados = [];
const ultimoRecado = () => creados.at(-1)?.innerHTML ?? '';
globalThis.location = { hash: '' };
globalThis.window = { DATOS_DEMO: datos, addEventListener() {}, scrollTo() {} };
globalThis.fetch = () => { throw new Error('la maqueta no debería llamar a fetch'); };

const fallos = [];

/* Editor del once: lógica pura, se prueba de verdad. */
function probarEditor(E, datos, fallos) {
  const plantilla = datos['/estado'].plantilla;
  const lineup = datos['/fantasy/lineup'];
  const di = (c, m) => { if (!c) fallos.push(`editor: ${m}`); else console.log(`  ✔ editor — ${m}`); };

  E.iniciar(lineup, plantilla);
  di(E.titulares().length === 11, 'arranca con 11 titulares');
  di(E.problemas(plantilla).every((p) => p.includes('puntúan 0')), 'el once de partida es válido');

  const total = E.titulares().length + E.banquillo(plantilla).length;
  di(total === plantilla.length, `campo + banquillo = la plantilla (${total})`);

  // Intercambio entre iguales: el del banquillo entra, el titular sale.
  const fuera = E.banquillo(plantilla).find((p) => p.posicion === 'MED');
  const dentro = plantilla.find((p) => p.posicion === 'MED'
    && E.titulares().includes(String(p.player_team_id)));
  if (fuera && dentro) {
    E.tocar(dentro.player_team_id, plantilla);
    E.tocar(fuera.player_team_id, plantilla);
    const t = E.titulares();
    di(t.includes(String(fuera.player_team_id)) && !t.includes(String(dentro.player_team_id))
       && t.length === 11, 'intercambia dos de la misma posición sin perder plazas');
  }

  // Posiciones distintas: debe negarse, no romper la formación.
  const por = plantilla.find((p) => p.posicion === 'POR');
  const del = plantilla.find((p) => p.posicion === 'DEL');
  E.editor.seleccion = null;
  E.tocar(por.player_team_id, plantilla);
  E.tocar(del.player_team_id, plantilla);
  di(!!E.editor.aviso && E.titulares().length === 11, 'rechaza cambiar POR por DEL y avisa');

  // Cada formación viable debe dejar un once de 11 y coherente con su reparto.
  for (const f of E.formacionesViables(plantilla)) {
    E.cambiarFormacion(f, plantilla);
    const ok = E.titulares().length === 11
      && E.editor.once.defender.length === f[0]
      && E.editor.once.midfield.length === f[1]
      && E.editor.once.striker.length === f[2]
      && new Set(E.titulares()).size === 11;
    di(ok, `formación ${f.join('-')} deja 11 sin repetidos`);
  }

  // Y las que no son viables no deben ofrecerse.
  const inv = E.inventario(plantilla);
  const ofrecidas = E.formacionesViables(plantilla);
  di(ofrecidas.every(([d, m, dl]) => inv.DEF >= d && inv.MED >= m && inv.DEL >= dl),
     `solo ofrece formaciones que la plantilla cubre (${ofrecidas.length} de 7)`);

  const pl = E.payload();
  di(Object.keys(pl).length === 5 && pl.goalkeeper && pl.tactical_formation.length === 3,
     'el payload lleva exactamente los 5 campos que acepta la API');
}

/* `uuid()` con y sin contexto seguro. `crypto.randomUUID` solo existe en HTTPS o
   localhost, y la interfaz se sirve por http: sin respaldo propio, las tres escrituras
   del navegador (once, acciones y ajustes) quedan rotas.

   Cubre la CABECERA de idempotencia, no la escritura: quien comprueba que la petición
   sale y a dónde es `probarConfirmar`, sobre la instancia sin maqueta. */
function probarClaveIdempotencia(api, fallos) {
  const di = (c, m) => { if (!c) fallos.push(`uuid: ${m}`); else console.log(`  ✔ api — ${m}`); };
  const real = globalThis.crypto;
  const poner = (v) => Object.defineProperty(globalThis, 'crypto',
    { value: v, configurable: true, writable: true });
  const RE_UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;

  di(RE_UUID.test(api.uuid()), 'uuid() con crypto completo');

  // Contexto no seguro: hay getRandomValues pero no randomUUID. Es el caso real.
  poner({ getRandomValues: (a) => real.getRandomValues(a) });
  di(RE_UUID.test(api.uuid()), 'uuid() sin crypto.randomUUID (el caso real: http, no https)');

  // Sin nada de crypto: no debe reventar, la cabecera no puede faltar.
  poner(undefined);
  di(RE_UUID.test(api.uuid()), 'uuid() sin crypto ninguno');

  poner(real);
  const dos = new Set(Array.from({ length: 200 }, () => api.uuid()));
  di(dos.size === 200, 'uuid() no repite en 200 tiradas');
}

/* La pantalla de Conexión con LaLiga (login Google) se pinta dentro de Ajustes con estado
   propio. Se prueban sus cuatro estados con el DOM falso ya montado, incluido el spinner
   y que el botón exista en el estado inicial. */
function probarConexion(AJ, U, fallos) {
  if (!AJ || !AJ.estadoLogin) { console.log('  — conexión: AJ.estadoLogin no exportado'); return; }
  const casos = [
    ['sin conectar', { info: { conectado: false }, paso: 0, conectando: false, error: '', aviso: '' }, 'Conectar con LaLiga'],
    ['esperando pegado', { info: { conectado: false }, paso: 1, conectando: false, error: '', aviso: '' }, 'Paso 2'],
    ['conectando', { info: { conectado: false }, paso: 1, conectando: true, error: '', aviso: '' }, 'spinner'],
    ['ya conectado', { info: { conectado: true, bearer_caduca_min: 120 }, paso: 0, conectando: false, error: '', aviso: '' }, 'conectada'],
  ];
  AJ.estadoAj.datos = { categorias: {}, avisos: {}, telegram_configurado: false,
                        telegram_token_pista: '', telegram_chat_id: '', telegram_activo: false };
  for (const [nombre, est, debeTener] of casos) {
    Object.assign(AJ.estadoLogin, est);
    let out;
    try { out = AJ.pintarAj(); } catch (e) { fallos.push(`conexión (${nombre}): ${e.message}`); continue; }
    if (/undefined|NaN|\[object Object\]/.test(out)) fallos.push(`conexión (${nombre}): undefined en la salida`);
    else if (!out.includes(debeTener)) fallos.push(`conexión (${nombre}): falta «${debeTener}»`);
    else console.log(`  ✔ conexión — ${nombre}`);
  }
  Object.assign(AJ.estadoLogin, { paso: 0, conectando: false, error: '', aviso: '', info: null });

  /* Nada de esta instalación puede aparecer en la app de otra persona. El handle del
     bot, la IP de la VPN y el nombre de la liga no rompen nada si se cuelan, pero
     confunden: parecen datos que quien instala el APK tiene que usar. */
  const aj = AJ.pintarAj();
  const filtrados = [
    [/tu_bot/i, 'el handle del bot de esta instalación'],
    [/10\.8\.1\.1/, 'la IP de la VPN de esta casa'],
    [/MI LIGA/, 'el nombre de la liga de esta instalación'],
  ].filter(([re]) => re.test(aj)).map(([, q]) => q);
  if (filtrados.length) {
    fallos.push(`ajustes: se filtra ${filtrados.join(', ')}`);
  } else {
    console.log('  ✔ ajustes — no se filtra nada de esta instalación');
  }
  /* La tarjeta de servidor SOLO dentro del APK. En el navegador no hay nada que
     configurar (ya estás en el servidor que usas) y ofrecerlo confundiría. */
  if (/Dónde vive tu Coconut/.test(aj)) {
    fallos.push('ajustes: sale la tarjeta de servidor SIN puente nativo (en el navegador)');
  } else {
    console.log('  ✔ ajustes — la tarjeta de servidor no sale en el navegador');
  }

  // Y con el puente simulado, tiene que salir y explicar la limitación.
  globalThis.Coconut = { servidor: () => '', modoLocal: () => true, fijarServidor() {} };
  try {
    const conApk = AJ.pintarAj();
    const falta = [
      [/Dónde vive tu Coconut/, 'la tarjeta'],
      [/srv-url/, 'el campo de la dirección'],
      [/srv-guardar/, 'el botón de guardar'],
      [/misma red wifi|VPN/, 'el aviso de que hay que llegar a esa dirección'],
    ].filter(([re]) => !re.test(conApk)).map(([, q]) => q);
    if (falta.length) fallos.push(`ajustes (con APK): falta ${falta.join(', ')}`);
    else console.log('  ✔ ajustes — con el puente sale la tarjeta de servidor completa');
  } finally {
    delete globalThis.Coconut;
  }

  // Crear el bot de Telegram son dos pasos fuera de la aplicación, así que la
  // pantalla los explica y aquí se comprueba que siguen estando.
  if (!/BotFather/.test(aj) || !/userinfobot/i.test(aj)) {
    fallos.push('ajustes: faltan las instrucciones para crear el bot de Telegram');
  } else {
    console.log('  ✔ ajustes — instrucciones del bot completas');
  }
}

/* Tácticas se pinta en DIFERIDO, así que el recorrido de pestañas solo ve el
   «Cargando…». Aquí se pinta con las respuestas REALES de la API, que es donde una
   clave mal escrita acaba en un `undefined` en pantalla. */
function probarTacticas(pintar, fallos) {
  let fx;
  try {
    fx = ['cat', 'esc', 'cla', 'riv'].map((n) => JSON.parse(
      fs.readFileSync(new URL(`./fixtures/tacticas_${n}.json`, import.meta.url), 'utf8')));
  } catch { console.log('  — tácticas: sin fixtures, se salta'); return; }

  const [cat, esc, cla, riv] = fx;
  const casos = [
    ['con datos reales', [cat, esc, cla, riv, 'disuasorio']],
    // El caso que de verdad rompe: listas vacías y campos ausentes. Una liga recién
    // creada, o un día en que no haya candidatos, llega exactamente así.
    ['vacío', [{ aviso: '' }, { distribucion: {}, candidatos: [], descartados: [], riesgos: [] },
               { propuestas: [], descartados: [], riesgos: [] }, { rivales: [], como_leerlo: [] },
               'disuasorio']],
  ];
  for (const [nombre, args] of casos) {
    try {
      const out = pintar(...args);
      if (/undefined|NaN|\[object Object\]/.test(out)) {
        const m = out.match(/.{0,60}(undefined|NaN|\[object Object\]).{0,60}/);
        fallos.push(`tácticas (${nombre}): "${m[0].replace(/\s+/g, ' ')}"`);
      } else if (!out || out.length < 200) {
        fallos.push(`tácticas (${nombre}): salida sospechosamente corta (${out.length})`);
      } else {
        console.log(`  ✔ tácticas — ${nombre}`);
      }
    } catch (e) { fallos.push(`tácticas (${nombre}): ${e.message}`); }
  }
  // Y el invariante: ningún titular puede aparecer entre los candidatos.
  const nombresCand = new Set((esc.candidatos || []).map((c) => c.nombre));
  const titulares = (esc.descartados || []).filter((d) => /titular/.test(d.motivo || ''));
  const colados = titulares.filter((t) => nombresCand.has(t.nombre));
  if (colados.length) fallos.push(`tácticas: ${colados.length} titulares propuestos para listar`);
  else console.log(`  ✔ tácticas — ningún titular entre los candidatos (${titulares.length} excluidos)`);
}

/** Que las acciones de ficha abran su diálogo, y que su entrada del catálogo exista.
 *
 * Se llama a `abrirAccion()` de verdad en vez de mirar el código: un manejador que
 * invoca un export inexistente (`A.abrir` por `A.abrirAccion`) pinta el botón igual y
 * lanza un TypeError al pulsarlo. El harness renderiza y no pulsa, y las capturas de
 * chromium solo cargan la página.
 */
function probarAccionesFicha(A, fallos) {
  if (!A || !A.abrirAccion || !A.pintarDialogoHTML) {
    fallos.push('acciones: no se exportan abrirAccion/pintarDialogoHTML'); return;
  }
  const casos = [
    ['cancelar-puja',   { market_id: '1', bid_id: '2' }],
    ['retirar-mercado', { market_id: '1' }],
    ['aceptar-oferta',  { market_id: '1', offer_id: '2', importe: 1_000_000 }],
    ['rechazar-oferta', { market_id: '1', offer_id: '2' }],
  ];
  for (const [accion, params] of casos) {
    if (!A.ACCIONES[accion]) {
      fallos.push(`acciones: «${accion}» no está en el catálogo ACCIONES`); continue;
    }
    let out;
    try {
      A.abrirAccion(accion, { params, importe: params.importe, motivo: 'prueba' });
      out = A.pintarDialogoHTML(50_000_000);
    } catch (e) { fallos.push(`acciones (${accion}): ${e.message}`); continue; }
    if (/undefined|NaN|\[object Object\]/.test(out)) {
      fallos.push(`acciones (${accion}): undefined en el diálogo`);
    } else if (!out.includes(A.ACCIONES[accion].titulo)) {
      fallos.push(`acciones (${accion}): el diálogo no trae su título`);
    } else if (!/data-cuando="\d{4}-/.test(out)) {
      // Enrutarlas por aquí es lo que permite PROGRAMARLAS, así que se comprueba que
      // salen los atajos de hora, no la palabra "programar", que la
      // interfaz no usa (dice "¿Cuándo?" y lista las horas).
      fallos.push(`acciones (${accion}): el diálogo no ofrece horas a las que programar`);
    } else {
      console.log(`  ✔ acciones — ${accion} abre su diálogo y se puede programar`);
    }
    A.cerrarDialogo();
  }
}

/** Una SEGUNDA instancia del paquete, cargada como se sirve `/app/`: sin maqueta.
 *
 * Arriba se monta `window.DATOS_DEMO` para que las vistas tengan datos que pintar, y
 * `api.js` lee ese indicador UNA vez al cargar (`const DEMO = … && window.DATOS_DEMO`).
 * Con él puesto, `enviar()` lanza antes de tocar la red, así que toda escritura queda
 * fuera del alcance del harness por construcción. Ahí es donde caen los dos fallos que
 * llegan al navegador: `crypto.randomUUID` y `A.abrir`.
 *
 * `/auth/yo` contesta que hay sesión por pedir, que es lo que hace a `cargar()` pintar la
 * puerta y parar. Sin eso arrancaría las veinte peticiones de la carga normal.
 */
const respuestaFalsa = (status, cuerpo) => ({ ok: status < 400, status,
  json: async () => cuerpo, text: async () => JSON.stringify(cuerpo) });

/** La red, servida desde la misma maqueta. Con ella `cargar()` deja `estadoUI` igual que
    en producción, que es lo que necesitan las pruebas de escritura para no medir sobre un
    estado inventado. `extra` atiende primero, para las rutas que sí se quieren observar. */
const redDeMaqueta = (datos, extra = () => null) => async (url, opciones) => {
  const ruta = String(url);
  const mia = await extra(ruta, opciones);
  if (mia) return mia;
  if (ruta === '/auth/yo') return respuestaFalsa(200, { auth_activa: false, usuario: null });
  const d = datos[ruta.split('?')[0]];
  return d === undefined ? respuestaFalsa(404, { detail: `sin maqueta para ${ruta}` })
                         : respuestaFalsa(200, d);
};

async function instanciaViva(datos) {
  const previo = { window: globalThis.window, fetch: globalThis.fetch };
  globalThis.window = { addEventListener() {}, scrollTo() {} };
  globalThis.fetch = redDeMaqueta(datos);
  try {
    const inst = await new Function('return (async () => { ' + bundle
      + '\n return { A, api, E, estadoUI, accionGuardarOnce }; })()')();
    // `cargar()` se lanza al final del paquete sin esperarla. Sin esto, las pruebas
    // medirían sobre un `estadoUI` a medio llenar.
    for (let i = 0; i < 50 && !inst.estadoUI.datos?.estado; i++) {
      await new Promise((r) => setTimeout(r, 10));
    }
    return inst;
  } finally {
    Object.assign(globalThis, previo);
  }
}

/** Pulsar CONFIRMAR, que es el paso que gasta el dinero.
 *
 * `probarAccionesFicha` solo ABRE el diálogo. Abrir y confirmar son dos
 * caminos distintos, y el segundo es el único que llega a la red: si se rompe, el botón
 * no hace nada o manda a otro sitio, y una prueba que solo pinta no lo ve.
 *
 * Se sustituye `fetch` y no `api.actuar`, para que el recorrido incluya el cuerpo y las
 * cabeceras de verdad: un `confirmar: true` que se perdiera por el camino devuelve 428 y
 * eso solo se ve llegando hasta aquí. Recibe el `A` de `instanciaViva()`: el de la
 * maqueta no llega a la red.
 */
async function probarConfirmar(A, fallos) {
  const di = (c, m) => { if (!c) fallos.push(`confirmar: ${m}`); else console.log(`  ✔ confirmar — ${m}`); };
  const real = globalThis.fetch;
  let visto = null;
  const responder = (ok) => {
    globalThis.fetch = async (url, opciones = {}) => {
      visto = { url: String(url), ...opciones, cuerpo: JSON.parse(opciones.body || '{}') };
      if (!ok) throw new Error('la red se ha caído');
      return { ok: true, status: 200, json: async () => ({ ok: true }) };
    };
  };
  const abrir = () => A.abrirAccion('blindar',
    { params: { jugador: 'Fulano' }, motivo: 'prueba' });

  // 1) Ejecutar ahora: tiene que ir a /accion/<accion> y llevar la confirmación.
  responder(true); abrir(); visto = null;
  await A.confirmarAccion();
  di(visto && /\/accion\/blindar$/.test(visto.url), 'ejecutar ahora llama a /accion/blindar');
  di(visto && visto.cuerpo.confirmar === true, 'el cuerpo lleva confirmar: true');
  di(visto && visto.cuerpo.jugador === 'Fulano', 'el cuerpo lleva los parámetros del jugador');
  di(A.dialogo.hecho && A.dialogo.hecho.modo === 'ahora', 'el diálogo queda marcado como hecho');

  // 2) Programado: cambia el endpoint, no solo la hora.
  responder(true); abrir(); A.dialogo.cuando = '2026-10-01T11:00:00'; visto = null;
  await A.confirmarAccion();
  di(visto && /\/programar$/.test(visto.url), 'programado llama a /programar, no a /accion');
  di(visto && visto.cuerpo.accion === 'blindar' && visto.cuerpo.cuando,
     'el cuerpo de /programar lleva la acción y la hora');

  // 3) Si la red falla, el diálogo NO se queda en «Enviando…» para siempre.
  responder(false); abrir(); visto = null;
  await A.confirmarAccion();
  di(A.dialogo.error, 'un fallo de red deja el motivo a la vista');
  di(A.dialogo.ocupado === false, 'y suelta el «Enviando…» en vez de colgarse');

  A.cerrarDialogo();
  globalThis.fetch = real;
}

/** Guardar el once son DOS escrituras, y la segunda es la que impide que el cron de
 *  `job_alineacion` recalcule esta noche y borre la decisión. Que la primera vaya bien y
 *  la segunda falle es el caso peligroso: el once queda guardado y EFÍMERO, y sin aviso
 *  nadie se entera hasta ver otro once al día siguiente.
 */
async function probarGuardarOnce({ E, estadoUI, accionGuardarOnce }, datos, fallos) {
  const di = (c, m) => { if (!c) fallos.push(`once: ${m}`); else console.log(`  ✔ once — ${m}`); };
  const real = globalThis.fetch;
  let rutas = [];
  const conFallo = (patron) => {
    globalThis.fetch = redDeMaqueta(datos, async (ruta) => {
      if (!/\/accion\//.test(ruta)) return null;
      rutas.push(ruta);
      return patron && patron.test(ruta) ? respuestaFalsa(500, { detail: 'LaLiga dice que no' })
                                         : respuestaFalsa(200, { ok: true });
    });
  };
  const guardar = async (patron) => {
    E.iniciar(datos['/fantasy/lineup'], datos['/estado'].plantilla);
    conFallo(patron); rutas = [];
    await accionGuardarOnce();
  };

  await guardar(null);
  di(rutas.some((r) => r.startsWith('/accion/alineacion')), 'guarda el once en /accion/alineacion');
  di(rutas.some((r) => r.startsWith('/accion/fijar-once')), 'y lo FIJA, que es lo que para al cron');
  const fijar = rutas.find((r) => r.startsWith('/accion/fijar-once')) || '';
  di(/jornada=\d+/.test(fijar) && /motivo=[^&]+/.test(fijar), 'fijar lleva jornada y motivo');
  di(/Once guardado y fijado/.test(ultimoRecado()), 'y lo dice');

  await guardar(/fijar-once/);
  di(/NO se pudo fijar/.test(ultimoRecado()) && /recalcular/.test(ultimoRecado()),
     'si falla FIJAR, avisa de que el once es efímero en vez de decir «hecho»');

  await guardar(/alineacion/);
  di(/No se ha guardado/.test(ultimoRecado()), 'si falla GUARDAR, lo dice y no promete nada');
  di(!rutas.some((r) => r.startsWith('/accion/fijar-once')), 'y no intenta fijar lo que no se guardó');

  globalThis.fetch = real;
  estadoUI.datos = estadoUI.datos || {};
}


/** Que un usuario NUEVO vea una invitación, no un error técnico.
 *
 * "No hay refresh token" es el estado normal de una instalación recién puesta, no una
 * avería. Presentarlo como "No se han podido cargar los datos" con una orden de
 * `docker compose` al lado no le sirve a quien está en un móvil, y sugiere que algo se ha
 * roto cuando solo falta conectar la cuenta.
 */
function probarPrimerArranque(pantalla, fallos) {
  if (typeof pantalla !== 'function') {
    fallos.push('primer arranque: pantallaDeError no exportada'); return;
  }
  const casos = [
    ['sin cuenta', new Error('sesión caducada: no hay refresh token. Ejecuta el login '
      + 'interactivo: docker compose run --rm worker python scripts/login.py'), true],
    ['sin cuenta (otro texto)', new Error('no hay refresh token'), true],
    ['fallo de verdad', new Error('Failed to fetch'), false],
  ];
  for (const [nombre, err, esOnboarding] of casos) {
    let out;
    try { out = pantalla(err); }
    catch (e) { fallos.push(`primer arranque (${nombre}): ${e.message}`); continue; }

    if (esOnboarding) {
      // La primera vez sale la bienvenida entera; después, el aviso corto. El harness
      // corre con localStorage limpio, así que aquí toca la bienvenida.
      const bienvenida = /bienv__logo|Tu liga, con datos/.test(out);
      if (bienvenida) {
        const falta = [
          [/logo-transparente\.png/, 'el logo completo'],
          [/Aquí no se crean cuentas/, 'el aviso de que no se crean cuentas'],
          [/Jesús Luis Condori Chambi/, 'la autoría'],
          [/no se responsabiliza|sin garantía/, 'el descargo de responsabilidad'],
          [/No es una aplicación oficial de LaLiga/, 'el aviso de que no es oficial'],
          [/bienv-entrar/, 'el botón de iniciar sesión'],
          [/bienv-saltar/, 'la salida sin conectar'],
        ].filter(([re]) => !re.test(out)).map(([, q]) => q);
        if (falta.length) {
          fallos.push(`bienvenida: falta ${falta.join(', ')}`);
        } else if (/<input[^>]+type=["']?password/i.test(out)) {
          // Guarda deliberada: Coconut nunca pide la contraseña de LaLiga en una
          // pantalla propia. No funcionaría (LaLiga no expone ROPC) y además es la forma
          // que tiene el phishing. El login abre la página REAL de LaLiga.
          fallos.push('bienvenida: hay un campo de CONTRASEÑA, y eso no se hace nunca');
        } else {
          console.log(`  ✔ primer arranque — ${nombre}: bienvenida completa`);
        }
        continue;
      }
      if (!/Bienvenido|conectar tu cuenta/i.test(out)) {
        fallos.push(`primer arranque (${nombre}): no invita, sigue siendo un error`);
      } else if (/docker|compose|scripts\/login\.py|vive en la Pi|VPN/i.test(out)) {
        fallos.push(`primer arranque (${nombre}): sigue hablando de Docker, la Pi o la VPN`);
      } else if (!/ir-conectar/.test(out)) {
        fallos.push(`primer arranque (${nombre}): sin botón para conectar la cuenta`);
      } else {
        console.log(`  ✔ primer arranque — ${nombre}: invita a conectar la cuenta`);
      }
    } else if (/Bienvenido/.test(out)) {
      fallos.push(`primer arranque (${nombre}): un fallo real se presenta como bienvenida`);
    } else {
      console.log(`  ✔ primer arranque — ${nombre}: se presenta como error`);
    }
  }
}

/** Navegar SIN cuenta conectada: que cada pestaña pinte algo.
 *
 * Sin cuenta conectada, `estadoUI.datos` se queda en null. Un `if (!d) return` en
 * `pintar()` deja el menú cambiando de pestaña y el contenido quieto: la app se ve
 * congelada y no hay un solo error en consola.
 *
 * Se comprueba lo mínimo: que al cambiar de pestaña el contenido cambie. Vacío o con un
 * aviso vale; exactamente igual, no.
 */
function probarSinCuenta(ir, PESTANAS, estadoUI, fallos) {
  const guardado = estadoUI.datos;
  const guardadoFalta = estadoUI.faltaCuenta;

  /* DOS escenarios, y el segundo es el que de verdad ocurre hoy. Con `datos = null` se
     simula la carga que revienta entera; con el objeto de campos a null, la carga
     tolerante. Solo el segundo produce el "Cannot read properties of null (reading
     'caja')", así que simular únicamente el primero deja la prueba en verde con la app
     rota. */
  const escenarios = [
    ['carga caída', null, true],
    ['carga tolerante sin cuenta', {
      estado: null, informe: null, mercado: null, alineacion: null, calendario: [],
      actividad: null, tareas: null, openapi: null, candado: null, hoy: null,
      trabajos: null, seguimientos: null, ventanas: null, momentum: null,
      backtest: null, misOfertas: { ofertas: [] }, histMercado: null, enVivo: null,
      calDif: null, premium: null,
    }, true],
  ];
  try {
    for (const [escenario, datos, falta] of escenarios) {
      estadoUI.datos = datos;
      estadoUI.faltaCuenta = falta;
      recorrer(escenario, ir, PESTANAS, fallos);
    }
  } finally {
    estadoUI.datos = guardado;
    estadoUI.faltaCuenta = guardadoFalta;
  }
}

function recorrer(escenario, ir, PESTANAS, fallos) {
  const vistos = new Map();
  {
    for (const pestana of Object.keys(PESTANAS)) {
      let salida;
      try { ir(pestana); salida = document.getElementById('contenido').innerHTML; }
      catch (e) { fallos.push(`${escenario} (${pestana}): LANZA ${e.message}`); continue; }

      if (!salida || salida.length < 30) {
        fallos.push(`${escenario} (${pestana}): el contenido queda vacío`);
      } else if (/Cannot read|undefined is not|is not a function|NaN/.test(salida)) {
        fallos.push(`${escenario} (${pestana}): hay un error de JavaScript en la pantalla`);
      } else {
        vistos.set(pestana, salida);
      }
    }
    /* Y lo PRIMERO que se ve tiene que ser la bienvenida, no un error. Se mira QUÉ
       pinta la pestaña de inicio, no solo que pinte algo: con `faltaCuenta` calculado y
       sin leer en ningún sitio, la app pinta una pantalla entera y es la equivocada. */
    const inicio = vistos.get('hoy') || '';
    if (inicio && !/bienv__logo|Tu liga, con datos|conectar tu cuenta/i.test(inicio)) {
      fallos.push(`${escenario}: la pantalla de inicio no invita a conectar la cuenta`);
    } else if (/No se han podido cargar los datos/.test(inicio)) {
      fallos.push(`${escenario}: la pantalla de inicio sale como un ERROR`);
    }

    if (vistos.size) {
      console.log(`  ✔ ${escenario} — las ${vistos.size} pestañas pintan sin reventar`);
    }
  }
}

/* Una ruta "superpuesta" (`#manager/{id}`, `#jugador/{id}`) no puede llamarse como una
   pestaña: `#equipo/Plantilla` se leería como «el manager con id Plantilla». Y el daño
   no se queda ahí: deja `equipoId` puesto, y con eso `pintar()` sale antes de tiempo y
   ninguna pestaña vuelve a cambiar. */
function probarRutas(fallos, PESTANAS) {
  const di = (c, m) => { if (!c) fallos.push(`rutas: ${m}`); else console.log(`  ✔ rutas — ${m}`); };
  for (const p of ['manager', 'jugador']) {
    di(!PESTANAS[p], `«${p}» no choca con ninguna pestaña`);
  }
}

/* Botones sin manejador. Un botón se pinta igual esté cableado o no, así que ni el
 * harness (que renderiza y no pulsa) ni las capturas de chromium lo ven: `A.abrir`, el
 * "Probar" del catálogo y el "volver" de la ficha de jugador salieron así.
 *
 * Pulsarlo todo no es viable; comprobar la correspondencia sí: cada acción que una vista
 * EMITE tiene que tener un sitio donde se ATIENDA. Es estático y cuesta milisegundos.
 *
 * Lee los ficheros fuente y no el paquete: en el paquete aplanado los nombres siguen
 * estando, pero se pierde qué fichero los emite y cuál los atiende.
 */
function probarAccionesHuerfanas(fallos) {
  const dir = new URL('../web/js/', import.meta.url);
  /* Sin comentarios. Estos ficheros documentan los bugs citando el código que los
     causó, así que un comentario que dice «esto era accion: 'volver'» hace que la
     comprobación se denuncie a sí misma: mediría el texto y no el programa. */
  const leer = (f) => fs.readFileSync(new URL(f, dir), 'utf8')
    .replace(/\/\*[\s\S]*?\*\//g, '')
    .replace(/^\s*\/\/.*$/gm, '');
  const todo = fs.readdirSync(dir).filter((f) => f.endsWith('.js')).map(leer).join('\n');

  const emitidas = new Set([...todo.matchAll(/data-accion="([a-z0-9-]+)"/g),
                            ...todo.matchAll(/accion:\s*'([a-z0-9-]+)'/g)].map((m) => m[1]));

  /* Las opciones del menú del jugador se declaran con la misma forma (`accion: 'vender'`)
     pero se pintan con `data-elegir`, no con `data-accion`: su clic lo atiende
     `abrirConfirmacion`, no `manejar`. Llevan `etiqueta` al lado, que es lo que las
     distingue, así que se descuentan de las emitidas. No se dan por «atendidas»: esa es
     la vía blanda que dejó pasar el clausulazo. */
  for (const m of leer('acciones.js').matchAll(/accion:\s*'([a-z0-9-]+)',\s*etiqueta:/g)) {
    emitidas.delete(m[1]);
  }

  const app = leer('app.js');
  const atendidas = new Set([...app.matchAll(/accion === '([a-z0-9-]+)'/g)].map((m) => m[1]));
  for (const [, lista] of app.matchAll(/\[([^\]]+)\]\.includes\(accion\)/g)) {
    for (const m of lista.matchAll(/'([a-z0-9-]+)'/g)) atendidas.add(m[1]);
  }
  /* Atendida es solo la que `app.js` engancha a un clic. El catálogo `ACCIONES` de
     `acciones.js` no cuenta: describe el contenido del diálogo (título, riesgo, avisos)
     y no engancha nada. Darlo por bueno deja pasar el caso de `clausulazo`: entrada en
     el catálogo y botón sin cablear. */

  const huerfanas = [...emitidas].filter((a) => !atendidas.has(a)).sort();
  if (huerfanas.length) fallos.push(`acciones sin manejador (botón muerto): ${huerfanas.join(', ')}`);
  else console.log(`  ✔ acciones — las ${emitidas.size} que se emiten tienen manejador`);
}

/* Que la acción tenga manejador no basta.
   `probarAccionesHuerfanas` caza el botón sin cablear, pero no el botón cableado que
   recibe el dato equivocado. Caso: el botón «Pujar» de Momentum con el NOMBRE en el sitio
   del `market_id`. Un nombre es una cadena plausible, así que no aparece ningún
   `undefined` que lo delate. Aquí cada `data-arg` se resuelve contra los datos de
   maqueta igual que lo resuelve `app.js`.

   La maqueta va indexada por RUTA (`'/estado'`, `'/fantasy/market'`), no con la forma de
   `estadoUI.datos` que ve `app.js` (`d.estado`, `d.mercado`). Copiar la expresión del
   manejador tal cual da un resolutor que no encuentra nada y acusa de rotos a los botones
   que sí funcionan. */
const RESUELVEN = {
  jugador: (d, arg) => (d['/estado']?.plantilla || [])
    .some((p) => String(p.player_team_id) === String(arg)),
  pujar: (d, arg) => (d['/fantasy/market'] || []).some((x) => String(x.id) === String(arg)),
};

function probarArgumentos(fallos, datos, vistas) {
  /* Primero, que esta tabla no se quede atrás. Si mañana otro manejador resuelve su
     `arg` con un `.find()` y nadie lo añade aquí, la prueba seguiría en verde sin
     haberlo mirado. */
  const app = fs.readFileSync(new URL('../web/js/app.js', import.meta.url), 'utf8')
    .replace(/\/\*[\s\S]*?\*\//g, '').replace(/^\s*\/\/.*$/gm, '');
  const sinCubrir = [];
  for (const m of app.matchAll(/accion === '([a-z0-9-]+)'\)\s*\{([\s\S]{0,260}?)\n\s*\}/g)) {
    const [, nombre, cuerpo] = m;
    if (/\.find\(/.test(cuerpo) && /\barg\b/.test(cuerpo) && !(nombre in RESUELVEN)) {
      sinCubrir.push(nombre);
    }
  }
  if (sinCubrir.length) {
    fallos.push(`RESUELVEN no cubre acciones que resuelven su arg: ${sinCubrir.join(', ')}`);
  }

  let mirados = 0;
  for (const [clave, sub, html] of vistas) {
    for (const m of html.matchAll(/data-accion="([a-z0-9-]+)"[^>]*?data-arg="([^"]*)"/g)) {
      const [, accion, arg] = m;
      const resolver = RESUELVEN[accion];
      if (!resolver) continue;
      mirados += 1;
      if (!resolver(datos, arg)) {
        fallos.push(`${clave}/${sub}: el botón "${accion}" manda arg="${arg}", `
                    + 'que no resuelve contra los datos (botón que no hará nada)');
      }
    }
  }
  console.log(`  ✔ acciones — ${mirados} argumento(s) resuelven contra los datos`);
}

/* El desplegable sustituye al `<select>` nativo, así que tiene que hacer lo mismo que
   él: guardar el valor donde alguien lo va a leer. */
function probarDesplegable(fallos, U) {
  const di = (c, m) => { if (!c) fallos.push(`desplegable: ${m}`); else console.log(`  ✔ desplegable — ${m}`); };
  const html = U.desplegable({ id: 'x', valor: 'b', opciones: [['a', 'Uno'], ['b', 'Dos']] });
  di(/type="hidden" id="x" value="b"/.test(html), 'guarda el valor en un input que se puede leer');
  di(/aria-selected="true"[^>]*>Dos</.test(html) || html.includes('>Dos<'), 'marca la opción activa');
  di(!/<select/.test(html), 'no usa <select> (su lista la pinta el sistema y rompe el tema)');
  di(html.includes('desple__lista') && html.includes('hidden'), 'la lista arranca cerrada');
  const vacio = U.desplegable({ id: 'y', valor: 'zzz', opciones: [['a', 'Uno']] });
  di(vacio.includes('value="a"'), 'con un valor que no existe cae a la primera opción');
}

globalThis.__probar = async (ir, PESTANAS, E, datos, api, U, estadoUI, pintarTacticas, AJ, A, pantallaDeError) => {
  await new Promise((r) => setTimeout(r, 60));
  // El HTML de cada vista se guarda para `probarArgumentos`, que necesita el resultado
  // pintado: los `data-arg` salen de interpolar los datos, no del código fuente.
  const vistas = [];
  for (const [clave, def] of Object.entries(PESTANAS)) {
    for (const sub of def.sub.length ? def.sub : [null]) {
      try {
        ir(clave, sub);
        // Alguna vista pide sus datos al pintarse (Ajustes). Se le da un respiro
        // para que la promesa se resuelva antes de mirar el resultado.
        await new Promise((r) => setTimeout(r, 40));
        const salida = nodos.contenido.innerHTML;
        vistas.push([clave, sub, salida]);
        /* Algunas vistas se pintan en dos tiempos: devuelven un contenedor y lo
           rellenan con un fetch. En el harness ese fetch no existe, así que su salida
           corta es CORRECTA. Se distingue por el marcador de carga: no se da por buena
           cualquier salida corta. */
        const asincrona = /Cargando|cargando/.test(salida) && /id="[a-z-]+"/.test(salida);
        if (!salida || (salida.length < 200 && !asincrona)) {
          fallos.push(`${clave}/${sub}: salida vacía (${salida.length})`);
        } else if (asincrona) console.log(`  ✔ ${clave}/${sub} — asíncrona, se rellena por fetch`);
        else if (/undefined|NaN|\[object Object\]/.test(salida)) {
          const m = salida.match(/.{0,60}(undefined|NaN|\[object Object\]).{0,60}/);
          fallos.push(`${clave}/${sub}: "${m[0].replace(/\s+/g, ' ')}"`);
        } else console.log(`  ✔ ${clave}${sub ? '/' + sub : ''} — ${(salida.length / 1024).toFixed(1)} KB`);
      } catch (e) { fallos.push(`${clave}/${sub}: ${e.message}`); }
    }
  }
  /* La PRIMERA FASE de carga, con todo lo pesado todavía a `null`.
     La app pinta antes de tener los 15 endpoints lentos, así que cada vista tiene que
     aguantar sin ellos. Sin esta prueba, un `hoy.asuntos.map` sobre null dejaría la
     pantalla en blanco justo en el primer segundo, que es cuando el usuario mira. */
  {
    const guardado = { ...estadoUI.datos };
    const nulos = ['actividad', 'tareas', 'openapi', 'candado', 'hoy', 'trabajos',
                   'seguimientos', 'ventanas', 'momentum', 'backtest', 'histMercado',
                   'enVivo', 'calDif', 'premium'];
    for (const k of nulos) estadoUI.datos[k] = null;
    estadoUI.datos.misOfertas = { ofertas: [] };
    for (const [clave, def] of Object.entries(PESTANAS)) {
      for (const sub of def.sub.length ? def.sub : [null]) {
        try {
          ir(clave, sub);
          await new Promise((r) => setTimeout(r, 30));
          const salida = nodos.contenido.innerHTML;
          if (/undefined|NaN|\[object Object\]/.test(salida)) {
            const m = salida.match(/.{0,50}(undefined|NaN|\[object Object\]).{0,50}/);
            fallos.push(`FASE-1 ${clave}/${sub}: "${m[0].replace(/\s+/g, ' ')}"`);
          }
        } catch (e) { fallos.push(`FASE-1 ${clave}/${sub}: ${e.message}`); }
      }
    }
    Object.assign(estadoUI.datos, guardado);
    console.log('  ✔ fase 1: las vistas aguantan sin los datos lentos');
  }

  probarDesplegable(fallos, U);
  probarAccionesHuerfanas(fallos);
  probarArgumentos(fallos, datos, vistas);
  probarRutas(fallos, PESTANAS);
  probarEditor(E, datos, fallos);
  probarClaveIdempotencia(api, fallos);
  probarTacticas(pintarTacticas, fallos);
  probarConexion(AJ, U, fallos);
  probarAccionesFicha(A, fallos);
  probarPrimerArranque(pantallaDeError, fallos);
  probarSinCuenta(ir, PESTANAS, estadoUI, fallos);
  /* Las últimas, porque cargan otra copia del paquete y repintan el DOM compartido.
     Van sobre la instancia SIN maqueta: es la única en la que una escritura llega a la
     red, y por tanto la única que puede ver un botón que no hace nada. */
  const viva = await instanciaViva(datos);
  await probarConfirmar(viva.A, fallos);
  await probarGuardarOnce(viva, datos, fallos);
  console.log(fallos.length ? '\n✖ FALLOS:\n' + fallos.map((f) => '  ' + f).join('\n') : `\n✔ las ${Object.keys(PESTANAS).length} pestañas renderizan`);
  process.exit(fallos.length ? 1 : 0);
};
eval(`(async () => { ${bundle}\n await __probar(ir, PESTANAS, E, datos, api, U, estadoUI, pintarTacticas, AJ, A, pantallaDeError); })()`);
