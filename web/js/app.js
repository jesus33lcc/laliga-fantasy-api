/* ============================================================================
   app.js: armazón y navegación. Decide qué pestaña se ve, pide sus datos una
   sola vez y llama a la vista. Toda la lógica de pintar vive en vistas.js.
   ========================================================================== */

import * as A from './acciones.js';
import * as AJ from './ajustes.js';
import * as B from './buscador.js';
import { registrarManagers } from './equipos.js';
import * as BV from './bienvenida.js';
import * as api from './api.js';
import * as L from './login.js';
import * as E from './once.js';
import * as U from './ui.js';
import * as V from './vistas.js';

const AUTOR = { nombre: 'Jesús Luis Condori Chambi', github: 'https://github.com/jesus33lcc' };

const PESTANAS = {
  hoy:       { titulo: 'Hoy',        icono: 'campana',  sub: [] },
  liga:      { titulo: 'Mis ligas',  icono: 'trofeo',   sub: [] },
  equipo:    { titulo: 'Equipo',     icono: 'campo',    sub: ['Alineación', 'Plantilla', 'Calendario', 'Puntos', 'Óptimo', 'En vivo'] },
  mercado:   { titulo: 'Mercado',    icono: 'grafico',  sub: ['Mercado', 'Momentum', 'Trading', 'Tácticas', 'Buscar', 'Jugadores', 'Mis Ops.', 'Histórico'] },
  radar:     { titulo: 'Radar',      icono: 'radar',    sub: ['Avisos', 'Ventanas'] },
  motor:     { titulo: 'Motor',      icono: 'engranaje', sub: ['Procesos', 'Vigilancias'] },
  api:       { titulo: 'API',        icono: 'enchufe',  sub: ['Backtest', 'Premium', 'Coconut', 'LaLiga'] },
  actividad: { titulo: 'Actividad',  icono: 'lista',    sub: [] },
  ajustes:   { titulo: 'Ajustes',    icono: 'engranaje',sub: [] },
};

/* Arranca en "Hoy": lo primero al abrir debe ser qué hay que atender, no un
   cuadro de datos que aún hay que interpretar. */
const estadoUI = { pestana: 'hoy', sub: null, datos: null, ficha: null, fichaId: null,
                   equipo: null, equipoId: null, equipoJor: null };

const $ = (sel) => document.querySelector(sel);

/* Armazón */

function pintarArmazon() {
  const p = PESTANAS[estadoUI.pestana];

  /* Con una ficha o una plantilla abierta, la cabecera dice DÓNDE ESTÁS y no la
     pestaña de fondo: mirando la plantilla de un rival pondría «Hoy». */
  const donde = estadoUI.equipoId ? (estadoUI.equipo?.manager || 'Equipo')
    : estadoUI.fichaId ? (estadoUI.ficha?.nombre || 'Jugador')
    : p.titulo;
  // El icono en vez de la palabra: a la izquierda del nombre de la sección, como la app
  // oficial. `width`/`height` explícitos para que la cabecera no dé un salto al cargar.
  $('#titulo').innerHTML =
    `<img class="cabecera__icono" src="/app/img/icono.png" width="22" height="22" alt="">`
    + `<em>${U.esc(donde)}</em>`;

  $('#subtabs').innerHTML = p.sub.map((s) => `
    <button class="subtabs__item" role="tab" data-sub="${U.esc(s)}"
      aria-selected="${s === estadoUI.sub}">${U.esc(s)}</button>`).join('');
  $('#subtabs').classList.toggle('u-oculto', !p.sub.length);

  $('#nav').innerHTML = Object.entries(PESTANAS).map(([clave, v]) => `
    <button class="nav__item" data-pestana="${clave}"
      ${clave === estadoUI.pestana ? 'aria-current="page"' : ''}>
      ${U.icono(v.icono, 'nav__icono')}<span>${U.esc(v.titulo)}</span></button>`).join('');
}

/* El nombre de la liga sale de los DATOS, no del código: escrito a fuego, cualquiera
   que instale Coconut vería en su pie el nombre de otra liga. */
function pintarPie() {
  const liga = estadoUI.datos?.estado?.liga?.nombre;
  $('#pie').innerHTML = `
    <span>Hecho por <span class="pie__autor">${U.esc(AUTOR.nombre)}</span></span>
    <a class="pie__enlace" href="${U.esc(AUTOR.github)}" target="_blank" rel="noopener noreferrer">
      ${U.icono('github')} github.com/jesus33lcc</a>
    <span class="u-dimmer">LaLiga Fantasy Coconut${liga ? ` · liga ${U.esc(liga)}` : ''}</span>`;
}

/* Menú lateral
   En el móvil la nav de abajo lleva a las secciones; el menú es para lo demás:
   ver de un vistazo cómo está el sistema y saltar a la documentación viva. */

function pintarMenu() {
  const s = estadoUI.datos?.estado;
  // El nombre de la liga sale de la API, no del HTML: cada instalación tiene la suya.
  const marca = $('#menu-liga');
  if (marca) marca.textContent = s?.liga?.nombre || '';
  const secciones = Object.entries(PESTANAS).map(([clave, v]) => `
    <button class="menu__item" data-pestana="${clave}"
      ${clave === estadoUI.pestana ? 'aria-current="page"' : ''}>
      ${U.icono(v.icono)}<span>${U.esc(v.titulo)}</span></button>`).join('');

  const sistema = s ? `
    <div class="menu__grupo">
      <p class="menu__titulo u-label">Sistema</p>
      <p class="menu__dato">Modo <b class="u-accent">${U.esc(s.sistema.modo)}</b></p>
      <p class="menu__dato">Peticiones hoy <b>${s.sistema.peticiones_al_origen_hoy}</b></p>
      <p class="menu__dato">Interfaz desplegada <b>${U.fecha(s.sistema.web_desplegada)}</b></p>
      <p class="menu__dato">Servidas de caché <b>${s.sistema.servidas_de_cache_hoy}</b></p>
      <p class="menu__dato">WAF
        <b class="${s.sistema.bloqueado_por_waf ? 'u-crit' : 'u-ok'}">
          ${s.sistema.bloqueado_por_waf ? 'BLOQUEANDO' : 'sin bloqueos'}</b></p>
      <p class="menu__dato">Jornada <b>${s.jornada.numero}${s.jornada.en_juego ? ' · en juego' : ''}</b></p>
      <p class="menu__dato">Caja libre <b>${U.millones(s.caja.disponible_tras_reserva)}</b></p>
    </div>` : '';

  $('#menu-cuerpo').innerHTML = `
    <div class="menu__grupo">
      <p class="menu__titulo u-label">Secciones</p>
      ${secciones}
    </div>
    ${sistema}
    <div class="menu__grupo">
      <p class="menu__titulo u-label">Enlaces</p>
      <a class="menu__item" href="/docs" target="_blank" rel="noopener">${U.icono('enchufe')}
        <span>Documentación de la API</span></a>
      <a class="menu__item" href="${U.esc(AUTOR.github)}" target="_blank" rel="noopener noreferrer">
        ${U.icono('github')}<span>github.com/jesus33lcc</span></a>
    </div>
    ${L.sesion.usuario ? `<div class="menu__grupo">
      <p class="menu__titulo u-label">Sesión</p>
      <p class="menu__dato">${U.esc(L.sesion.usuario.nombre || L.sesion.usuario.email)}</p>
      <button class="menu__item" data-accion="salir">${U.icono('candadoCerrado')}
        <span>Cerrar sesión</span></button>
    </div>` : ''}
    <div class="menu__grupo" style="border-bottom:0">
      <p class="menu__dato u-dimmer">${U.esc(AUTOR.nombre)}</p>
      <p class="menu__dato u-dimmer">Datos ${api.modo() === 'demo' ? 'de maqueta' : 'en vivo'}</p>
    </div>`;
}

function abrirMenu(abierto) {
  const panel = $('#menu');
  if (!abierto && panel.hidden) return;   // no robar el foco al cerrar lo ya cerrado
  pintarMenu();
  panel.hidden = !abierto;
  $('#abrir-menu').setAttribute('aria-expanded', String(abierto));
  /* Sin esto el fondo se desplaza bajo el panel al arrastrar en el móvil. */
  document.body.style.overflow = abierto ? 'hidden' : '';
  if (abierto) $('#cerrar-menu').focus();
  else $('#abrir-menu').focus();
}

/* Datos */

async function cargar() {
  $('#contenido').innerHTML = U.cargando('Conectando con LaLiga…');

  /* Si el servidor exige sesión y no la hay, se enseña la puerta y no se pide
     ni un dato: pedirlos daría una ristra de 401 y una pantalla rota. */
  await L.comprobar();
  if (L.hayQueEntrar()) {
    $('#contenido').innerHTML = L.pintarLogin();
    $('#subtabs').classList.add('u-oculto');
    return;
  }

  /* CARGA EN DOS FASES: se pinta con lo imprescindible y el resto entra después,
     repintando una vez. Con un `Promise.all` de las veinte peticiones, la app tarda lo
     que el endpoint más lento y la pantalla se queda en blanco todo ese rato. Las
     vistas toleran `null` en todo lo de la segunda fase. */
  try {
    const fallos = [];
    /* Cada petición se captura POR SEPARADO. En un `Promise.all` sin `catch`, una que
       falle aborta la carga entera y `estadoUI.datos` se queda en `null`; como `pintar()`
       sale antes de repintar sin datos, cambiar de pestaña deja de tener efecto. Y sin
       cuenta conectada al menos una falla siempre. Lo que falle vale `null` y lo que
       funcione se usa: `maestro` es público, así que el buscador de jugadores funciona
       desde el primer arranque. */
    const pedirTolerante = (p) => p.catch((e) => { fallos.push(e); return null; });
    const [estado, informe, mercado, alineacion, maestro] = await Promise.all([
      pedirTolerante(api.estado()), pedirTolerante(api.informe()),
      pedirTolerante(api.mercado()), pedirTolerante(api.alineacion()),
      pedirTolerante(api.maestro()),
    ]);

    // Si NO hay nada de nada, no es "falta la cuenta": es que la API no responde. Se
    // enseña el error de verdad, que es lo único accionable en ese caso.
    if (!maestro && !estado) throw (fallos[0] || new Error('la API no responde'));

    V.indexarMaestro(maestro, window.DATOS_DEMO?.fotos);
    const calendario = estado?.jornada?.numero
      ? await api.calendario(estado.jornada.numero).catch(() => [])
      : [];
    // Lo que haga falta cuenta y no se pudo traer queda anotado, para que cada pestaña
    // pueda decir "conecta la cuenta" en vez de reventar o quedarse en blanco.
    /* Basta con que falte `estado`, sin mirar el TEXTO del error: depender del mensaje
     * se rompe en cuanto cambia una palabra. El caso "la API no responde" ya está
     * descartado arriba, así que si llegó el maestro y no `estado`, falta la cuenta. */
    estadoUI.faltaCuenta = !estado;
    /* Quiénes son los managers de ESTA liga. Va antes de pintar porque el tablón pone
       nombre a cada evento, y sin registro saldrían como `manager <id>`. */
    registrarManagers(estado?.clasificacion);
    estadoUI.datos = { estado, informe, mercado, alineacion, calendario,
                       actividad: null, tareas: null, openapi: null, candado: null,
                       hoy: null, trabajos: null, seguimientos: null, ventanas: null,
                       momentum: null, backtest: null, misOfertas: { ofertas: [] }, ventas: null,
                       histMercado: null, enVivo: null, calDif: null, premium: null };
    // El pie lleva el nombre de la liga, que viene en los datos: hay que repintarlo
    // cuando llegan. Al arrancar se pinta sin él, que es lo correcto: es preferible un
    // pie sin nombre a uno con el nombre equivocado.
    pintarPie();
    pintar();

    /* Segunda fase: lo pesado. No bloquea. Cada uno entra cuando llega y se repinta una
       sola vez al final, para no provocar veinte reflows seguidos. */
    Promise.all([
      api.actividad().catch(() => null),
      api.tareas().catch(() => null),
      api.catalogo().catch(() => null),
      api.candado().catch(() => null),
      api.hoy().catch(() => null),
      api.trabajos().catch(() => null),
      api.seguimientos().catch(() => null),
      api.ventanas().catch(() => null),
      api.momentum().catch(() => null),
      api.backtest().catch(() => null),
      api.misOfertas().catch(() => ({ ofertas: [] })),
      api.ventas().catch(() => null),
      api.histMercado().catch(() => null),
      api.enVivo().catch(() => null),
      api.calendario2().catch(() => null),
      api.premium().catch(() => null),
    ]).then(([actividad, tareas, openapi, candado, hoy, trabajos, seguimientos, ventanas,
              momentum, backtest, misOfertas, ventas, histMercado, enVivo, calDif, premium]) => {
      Object.assign(estadoUI.datos, { actividad, tareas, openapi, candado, hoy, trabajos,
                                      seguimientos, ventanas, momentum, backtest,
                                      misOfertas, ventas, histMercado, enVivo, calDif, premium });
      pintar();
    });
  } catch (e) {
    $('#contenido').innerHTML = pantallaDeError(e);
  }
}

/** El mensaje de "no hay datos", adaptado a POR QUÉ no los hay.
 *
 * Falta de cuenta y fallo real no son lo mismo y no se cuentan igual: a quien acaba de
 * instalar la app en el móvil, un mensaje sobre Docker y VPN no le sirve, y además da a
 * entender que algo se ha roto cuando solo falta conectar la cuenta.
 *
 * El camino normal no llega aquí: lo resuelve `faltaCuenta` sin mirar ningún texto. Esto
 * solo cubre el `catch` de red, y ahí sí hay que mirar el mensaje porque es lo único que
 * llega. Cuando lo fabrica la propia carga, se compone para que el regex acierte.
 */
function pantallaDeError(e) {
  const msg = String(e.message || '');
  const sinCuenta = /refresh token|sesión caducada|login interactivo/i.test(msg);

  if (sinCuenta) {
    // La primera vez, la pantalla de bienvenida entera: qué es esto, quién lo hace y un
    // botón. A partir de ahí (o si se ha saltado), el aviso corto.
    if (BV.toca(false)) return BV.pintarBienvenida();
    return U.aviso({
      tono: 'info',
      icono: 'enchufe',
      titulo: 'Falta conectar tu cuenta',
      texto: 'Para ver tu equipo, tus rivales y el mercado de tu liga hay que conectar '
           + 'tu cuenta de LaLiga Fantasy. Se hace una vez y se queda guardada.',
      accion: U.boton('Conectar mi cuenta', { tono: 'ok', accion: 'ir-conectar' }),
    }) + U.aviso({
      tono: 'ok',
      titulo: 'Mientras tanto ya puedes mirar',
      texto: 'Los precios y el buscador de jugadores salen del maestro público de '
           + 'LaLiga, así que funcionan sin conectar nada: mira en Mercado.',
      accion: U.boton('Ir al mercado', { accion: 'ir-mercado' }),
    });
  }

  return U.aviso({
    tono: 'crit',
    titulo: 'No se han podido cargar los datos',
    texto: msg,
  });
}

function pintar() {
  if (estadoUI.sub === 'Buscar' && !B.busq.avisos.length && !B.busq.res) {
    B.cargarAvisos().then(() => { if (estadoUI.sub === 'Buscar') pintar(); });
  }
  if (estadoUI.pestana === 'ajustes' && !AJ.estadoAj.datos && !AJ.estadoAj.cargando) {
    AJ.cargarAj().then(pintar);
  }
  // El estado de conexión con LaLiga se pide una vez al entrar en Ajustes.
  if (estadoUI.pestana === 'ajustes' && !AJ.estadoLogin.info && !AJ.estadoLogin.error) {
    AJ.cargarLogin().then(pintar);
  }
  /* La ficha manda sobre la pestaña: si hay una abierta, es lo que se ve. */
  if (estadoUI.fichaId) {
    $('#contenido').innerHTML = V.vistaFicha(estadoUI.ficha, estadoUI.fichaId);
    $('#subtabs').classList.add('u-oculto');
    return;
  }
  if (estadoUI.equipoId) {
    $('#contenido').innerHTML = V.vistaEquipoDe(estadoUI.equipo);
    $('#subtabs').classList.add('u-oculto');
    return;
  }
  /* Ajustes va ANTES del guard de datos: es la única pestaña que no necesita nada de la
     liga y es justo a donde hay que poder ir cuando todavía no hay cuenta. Detrás del
     guard, «Conectar mi cuenta» no llevaría a ninguna parte. */
  if (estadoUI.pestana === 'ajustes') {
    try {
      $('#contenido').innerHTML = AJ.pintarAj() + A.pintarDialogoHTML(cajaLibre());
    } catch (err) {
      console.error('ajustes no pudo pintarse:', err);
      $('#contenido').innerHTML = U.aviso({
        tono: 'crit', titulo: 'Ajustes no se ha podido pintar', texto: String(err.message),
      });
    }
    return;
  }

  /* Falta la cuenta: bienvenida la primera vez, aviso corto después.

     `faltaCuenta` lo calcula `cargar()` y se lee aquí. Es el camino normal, no el único:
     si fallan el maestro y `estado` a la vez, `cargar()` lanza y `pantallaDeError` puede
     llegar también a la bienvenida. */
  const d = estadoUI.datos;
  if (!d || estadoUI.faltaCuenta) {
    // Nunca se sale sin pintar: dejar el contenido anterior deja la navegación sin
    // respuesta visible al cambiar de pestaña.
    $('#contenido').innerHTML = pantallaDeError(new Error('no hay refresh token'));
    return;
  }
  const sub = estadoUI.sub;
  const vistas = {
    hoy:       () => V.vistaHoy(d),
    liga:      () => V.vistaLiga(d),
    equipo:    () => V.vistaEquipo(sub, d),
    mercado:   () => V.vistaMercado(sub, d),
    radar:     () => V.vistaRadar(sub, d),
    motor:     () => V.vistaMotor(sub, d),
    api:       () => V.vistaApi(sub, d),
    actividad: () => V.vistaActividad(d),
    ajustes:   () => AJ.pintarAj(),
  };
  /* Si una vista revienta por falta de datos, se dice: no se deja la pantalla en
     blanco ni el contenido anterior. Sin cuenta conectada la mitad de las pestañas no
     tienen con qué pintarse, y una pantalla vacía parece la app rota cuando lo único
     que falta es conectar la cuenta. */
  /* El TRY envuelve el pintado ENTERO, diálogo incluido: `A.pintarDialogoHTML()` se
     compone en la misma expresión que la vista, y una excepción suya se escaparía hasta
     el `catch` de `cargar()`, saliendo como «no se han podido cargar los datos». Eso
     manda a buscar un problema de red cuando el fallo es de pintado. */
  let cuerpo;
  try {
    cuerpo = vistas[estadoUI.pestana]() + A.pintarDialogoHTML(cajaLibre()) + V.pintarSondaHTML();
  } catch (err) {
    console.error('la vista', estadoUI.pestana, 'no pudo pintarse:', err);
    cuerpo = U.aviso({
      tono: 'info',
      titulo: 'Esto necesita tu cuenta de LaLiga',
      texto: 'Esta pantalla muestra datos de tu liga, y para eso hay que conectar la '
           + 'cuenta. El buscador de jugadores funciona sin conectar nada.',
      accion: U.boton('Conectar mi cuenta', { tono: 'accion', accion: 'ir-conectar' }),
    });
  }
  $('#contenido').innerHTML = cuerpo;
  if (!A.dialogo.abierto) window.scrollTo({ top: 0, behavior: 'instant' });
}

/* `?.` en CADA eslabón, no solo en el primero: con `datos?.estado.caja.total`, en cuanto
   `datos` existe, `.estado` puede ser null y `.caja` revienta. Con la carga tolerante
   `datos` existe siempre, con campos a null. */
const cajaLibre = () => estadoUI.datos?.estado?.caja?.total ?? 0;

/** Repinta SOLO el diálogo. Si se repintara la vista entera, el campo de importe
    perdería el cursor en cada tecla. */
function pintarDialogo() {
  const viejo = document.getElementById('modal');
  const caja = document.createElement('div');
  caja.innerHTML = A.pintarDialogoHTML(cajaLibre());
  const nuevo = caja.firstElementChild;
  if (viejo && nuevo) viejo.replaceWith(nuevo);
  else if (viejo) viejo.remove();
  else if (nuevo) $('#contenido').append(nuevo);
}

/* Navegación */

function ir(pestana, sub = null) {
  estadoUI.pestana = pestana;
  estadoUI.sub = sub ?? PESTANAS[pestana].sub[0] ?? null;
  location.hash = sub ? `${pestana}/${sub}` : pestana;
  pintarArmazon();
  pintar();
}

function desdeHash() {
  const [p, s] = decodeURIComponent(location.hash.slice(1)).split('/');
  /* `#jugador/2534` no es una pestaña: es una ficha que se superpone. Se carga bajo
     demanda porque son ~700 jugadores y solo se mira uno. */
  if (p === 'jugador' && s) {
    if (estadoUI.fichaId !== s) {
      estadoUI.fichaId = s; estadoUI.ficha = null;
      api.jugador(s).then((f) => { estadoUI.ficha = f; pintarArmazon(); pintar(); })
        .catch((e) => { estadoUI.ficha = { error: e.message }; pintar(); });
    }
    return;
  }
  /* `#manager/{teamId}` o `#manager/{teamId}:{jornada}`: la plantilla de cualquier
     manager. Se carga bajo demanda porque recorre su plantilla y su once.
     NO puede llamarse `#equipo/…`: `equipo` ya es una pestaña con sub-pestañas, así que
     `#equipo/Plantilla` se leería como «el manager con id Plantilla». */
  if (p === 'manager' && s) {
    const [tid, jor] = s.split(':');
    const clave = `${tid}:${jor || ''}`;
    if (estadoUI.equipoId !== clave) {
      estadoUI.equipoId = clave; estadoUI.equipo = null;
      api.plantillaDe(tid, jor).then((e) => { estadoUI.equipo = e; pintarArmazon(); pintar(); })
        .catch((e) => { estadoUI.equipo = { error: e.message }; pintar(); });
    }
    return;
  }
  estadoUI.equipoId = null; estadoUI.equipo = null;
  estadoUI.fichaId = null; estadoUI.ficha = null;
  if (PESTANAS[p]) { estadoUI.pestana = p; estadoUI.sub = s || PESTANAS[p].sub[0] || null; }
  else { estadoUI.sub = PESTANAS[estadoUI.pestana].sub[0] || null; }
}

/* Desplegables
   Un único manejador para todos: se identifican por `data-desple`, así que un
   desplegable nuevo no necesita código. */
function cerrarDesplegables(menos = null) {
  document.querySelectorAll('[data-desple]').forEach((d) => {
    if (d === menos) return;
    d.querySelector('.desple__lista')?.setAttribute('hidden', '');
    d.querySelector('.desple__btn')?.setAttribute('aria-expanded', 'false');
  });
}

document.addEventListener('click', (ev) => {
  const opcion = ev.target.closest('.desple__opcion');
  if (opcion) {
    const caja = opcion.closest('[data-desple]');
    const id = caja.dataset.desple;
    caja.querySelector(`#${CSS.escape(id)}`).value = opcion.dataset.valor;
    caja.querySelector('.desple__valor').textContent = opcion.textContent;
    caja.querySelectorAll('.desple__opcion').forEach((o) =>
      o.setAttribute('aria-selected', String(o === opcion)));
    cerrarDesplegables();
    /* Quien lo lea puede querer reaccionar al cambio, igual que con un select. */
    caja.querySelector(`#${CSS.escape(id)}`).dispatchEvent(
      new Event('change', { bubbles: true }));
    return;
  }
  const boton = ev.target.closest('.desple__btn');
  if (boton) {
    const caja = boton.closest('[data-desple]');
    const lista = caja.querySelector('.desple__lista');
    const abierto = !lista.hasAttribute('hidden');
    cerrarDesplegables(caja);
    lista.toggleAttribute('hidden', abierto);
    boton.setAttribute('aria-expanded', String(!abierto));
    return;
  }
  cerrarDesplegables();
});

document.addEventListener('keydown', (ev) => {
  if (ev.key === 'Escape') cerrarDesplegables();
});

document.addEventListener('input', (ev) => {
  if (ev.target.id === 'bj-texto') {
    V.buscadorUI.texto = ev.target.value;
    /* Se repinta solo la lista: repintar la vista entera perdería el cursor del
       campo en cada tecla, que es el mismo motivo por el que el diálogo se repinta
       aparte. */
    const cont = document.querySelector('#contenido');
    if (cont) {
      const foco = document.activeElement?.id;
      cont.innerHTML = V.vistaJugadores() + A.pintarDialogoHTML(cajaLibre());
      const campo = document.getElementById(foco);
      if (campo) { campo.focus(); campo.setSelectionRange?.(campo.value.length, campo.value.length); }
    }
  }
});

document.addEventListener('change', async (ev) => {
  const sw = ev.target.closest?.('[data-job-on]');
  if (sw) {
    const nombre = sw.dataset.jobOn;
    try {
      await api.guardarConfigJob(nombre, { activo: sw.checked });
      recado(`«${nombre}» ${sw.checked ? 'encendido' : 'apagado'}.`, 'ok');
      api.invalidar(); await cargar();
    } catch (e) { recado(`No se ha podido cambiar: ${e.message}`, 'crit'); }
    return;
  }
  if (ev.target.id === 'bj-pos') { V.buscadorUI.pos = ev.target.value; return pintar(); }
  if (ev.target.id === 'act-filtro') {
    V.actividadUI.filtro = ev.target.value;
    V.actividadUI.visibles = 30;   // al cambiar de filtro se empieza de nuevo
    pintar();
  }
});

document.addEventListener('click', (ev) => {
  if (ev.target.closest('#abrir-menu')) return abrirMenu(true);
  if (ev.target.closest('#cerrar-menu, #velo')) return abrirMenu(false);
  if (ev.target.closest('#recargar')) { api.invalidar(); return cargar(); }

  const nav = ev.target.closest('[data-pestana]');
  if (nav) { abrirMenu(false); return ir(nav.dataset.pestana); }

  const sub = ev.target.closest('[data-sub]');
  if (sub) return ir(estadoUI.pestana, sub.dataset.sub);

  const ficha = ev.target.closest('[data-ptid]');
  if (ficha) { E.tocar(ficha.dataset.ptid, estadoUI.datos.estado.plantilla); return pintar(); }

  const pos = ev.target.closest('[data-buscar-pos]');
  if (pos) { B.busq.filtro.posicion = pos.dataset.buscarPos; return pintar(); }

  const form = ev.target.closest('[data-formacion]');
  if (form) {
    E.cambiarFormacion(form.dataset.formacion.split('-').map(Number), estadoUI.datos.estado.plantilla);
    return pintar();
  }

  if (ev.target.closest('[data-google]')) return L.irAGoogle();

  if (ev.target.closest('[data-cerrar-modal]')) {
    const habiaHecho = !!A.dialogo.hecho;
    A.cerrarDialogo();
    return habiaHecho ? cargar() : pintarDialogo();
  }
  const cuando = ev.target.closest('[data-cuando]');
  if (cuando) { A.dialogo.cuando = cuando.dataset.cuando; return pintarDialogo(); }

  const elegir = ev.target.closest('[data-elegir]');
  if (elegir) return abrirConfirmacion(elegir.dataset.elegir, A.dialogo.datos.jugador);

  if (ev.target.closest('[data-confirmar]')) return confirmarDialogo();

  if (ev.target.closest('[data-cerrar-sonda]')) {
    V.sondaUI.ruta = ''; V.sondaUI.r = null; return pintar();
  }

  const accion = ev.target.closest('[data-accion]');
  if (accion) manejar(accion.dataset.accion, accion.dataset.arg);
});

/* Login: se maneja con `submit` y no con `click` para que el navegador ofrezca
   guardar la contraseña y el Intro funcione desde el teclado del móvil. */
document.addEventListener('submit', async (ev) => {
  if (ev.target.id !== 'form-login') return;
  ev.preventDefault();
  const d = new FormData(ev.target);
  $('#contenido').innerHTML = L.pintarLogin();       // repinta con «Entrando…»
  if (await L.entrar(d.get('email'), d.get('clave'))) cargar();
  else $('#contenido').innerHTML = L.pintarLogin();
});

/* Interruptores: el filtro del buscador y las casillas de avisos. */
document.addEventListener('change', (ev) => {
  if (ev.target.matches('[data-buscar-libres]')) {
    B.busq.filtro.solo_libres = ev.target.checked; return pintar();
  }
  const sw = ev.target.closest('[data-aviso]');
  if (!sw) return;
  if (sw.dataset.aviso === '__activo') AJ.marcarActivo(sw.checked);
  /* Los demás `__` son ajustes sueltos: su valor lo lee `aplicarAj()` del DOM al
     guardar, así que aquí basta con marcar que hay cambios pendientes. */
  else if (sw.dataset.aviso.startsWith('__')) AJ.estadoAj.sucio = true;
  else AJ.marcarAviso(sw.dataset.aviso, sw.checked);
  pintar();
});

/* El campo de importe: se escucha aparte para no repintar en cada tecla. */
document.addEventListener('input', (ev) => {
  if (ev.target.id === 'teclear') {
    A.dialogo.tecleado = ev.target.value;
    const boton = document.querySelector('[data-confirmar]');
    if (boton) boton.disabled = !A.puedeConfirmar();
  }
});

document.addEventListener('keydown', (ev) => {
  if (ev.key === 'Escape' && A.dialogo.abierto && !A.dialogo.ocupado) { A.cerrarDialogo(); pintarDialogo(); }
});

/* Acciones */

async function manejar(accion, arg) {
  const d = estadoUI.datos;
  if (accion === 'editar-once') {
    E.iniciar(d.alineacion, d.estado.plantilla);
    E.editor.activo = true;
    return pintar();
  }
  if (accion === 'cancelar-once') { E.editor.activo = false; return pintar(); }
  if (accion === 'guardar-once') return accionGuardarOnce();
  if (accion === 'recalcular-once') return accionRecalcular();

  if (accion === 'jugador') {
    const j = d.estado.plantilla.find((p) => String(p.player_team_id) === String(arg));
    if (!j) return recado('Ese jugador ya no está en tu plantilla. Recarga.', 'crit');
    A.abrirAcciones(j);
    return pintarDialogo();
  }

  /* Las dos vías del mercado comparten diálogo y se diferencian solo en el endpoint:
     `bid` para el agente libre, `direct-offer` para la ficha de otro manager. El importe
     mínimo es el mismo en las dos, el VALOR de mercado, no el precio de salida. */
  if (accion === 'pujar' || accion === 'ofertar') {
    const f = d.mercado.find((x) => String(x.id) === String(arg));
    if (!f) return recado('Esa ficha ya no está en el mercado.', 'crit');
    const pm = f.playerMaster;
    A.abrirAccion(accion, {
      nombre: pm.nickname,
      importe: Number(pm.marketValue),
      params: { jugador: pm.nickname, importe: Number(pm.marketValue) },
      motivo: `${accion} desde la web`,
    });
    return pintarDialogo();
  }

  /* La bienvenida se marca como vista en LOS DOS caminos: tanto si conectas la cuenta
     como si eliges mirar sin conectar. Marcándola solo al conectar, quien pulse
     «mirar» la vuelve a ver en cada arranque. */
  if (accion === 'bienv-entrar') {
    BV.marcarVista();
    ir('ajustes');
    // Se abre el login directamente: el usuario ya ha dicho que quiere entrar, hacerle
    // buscar el botón otra vez dentro de Ajustes es un paso de más sin motivo.
    return AJ.loginAbrir(pintar);
  }
  /* A "Jugadores", no a "Mercado" a secas. La sub-pestaña `Mercado` pide la baraja
     de TU liga y necesita cuenta; `Jugadores` es el buscador sobre el maestro público,
     que es lo único que de verdad se puede mirar sin conectar nada. */
  if (accion === 'bienv-saltar') { BV.marcarVista(); return ir('mercado', 'Jugadores'); }
  /* Cambiar de servidor. Lo hace el PUENTE nativo, no la web: la URL vive en las
     preferencias de Android, no en la página. */
  if (accion === 'srv-guardar') {
    const v = (document.getElementById('srv-url')?.value || '').trim();
    if (!v) { recado('Escribe la dirección, o pulsa «Usar el del móvil».', 'crit'); return; }
    if (!/^https?:\/\//i.test(v)) {
      recado('Tiene que empezar por http:// o https://', 'crit'); return;
    }
    try { globalThis.Coconut.fijarServidor(v); }
    catch (e) { recado(`No se pudo cambiar: ${e.message}`, 'crit'); }
    return;
  }
  if (accion === 'srv-local') {
    try { globalThis.Coconut.fijarServidor(''); }
    catch (e) { recado(`No se pudo cambiar: ${e.message}`, 'crit'); }
    return;
  }
  if (accion === 'ir-conectar') { return ir('ajustes'); }
  if (accion === 'ir-mercado')  { return ir('mercado', 'Jugadores'); }
  if (accion === 'login-abrir') { await AJ.loginAbrir(pintar); return; }
  if (accion === 'login-reiniciar') {
    AJ.estadoLogin.paso = 0; AJ.estadoLogin.error = ''; AJ.estadoLogin.aviso = '';
    return pintar();
  }
  if (accion === 'login-conectar') {
    const pegado = document.getElementById('login-pegado')?.value || '';
    await AJ.loginConectar(pegado, pintar);
    return;
  }

  if (accion === 'probar-odds') { await AJ.comprobarClaveOdds(pintar); return; }

  if (accion === 'rp-probar')    { await AJ.respProbar(pintar); return; }
  if (accion === 'rp-copiar')    { await AJ.respCopiar(pintar); return; }
  if (accion === 'rp-restaurar') { await AJ.respRestaurar(pintar); return; }
  if (accion === 'md-tomar')     { await AJ.mandoAccion('tomar', pintar); return; }
  if (accion === 'md-forzar')    { await AJ.mandoAccion('forzar', pintar); return; }
  if (accion === 'md-soltar')    { await AJ.mandoAccion('soltar', pintar); return; }

  /* El diálogo NO se abre con la cláusula que hay en pantalla: se recalcula a diario y la
   * API exige el euro exacto (409 `030.01.49`). Se pide un `dry` al servidor, que la relee
   * contra LaLiga y comprueba blindaje, protección y caja. Lo que se enseña es lo que se
   * va a pagar, y por encima de 10M hay que teclearlo. */
  if (accion === 'clausulazo') {
    A.abrirAccion('clausulazo', { nombre: arg, importe: 0, params: { jugador: arg } },
                  { leyendo: true });
    pintarDialogo();
    try {
      const r = await api.simular('clausulazo', { jugador: arg });
      A.dialogo.datos.importe = Number(r?.haria?.importe || 0);
      A.dialogo.leyendo = false;
    } catch (e) {
      // Un rechazo aquí es información, no una avería: "está blindado", "te faltan 3M".
      // Cerrar el diálogo y decirlo es más útil que dejarlo abierto con un error dentro.
      A.cerrarDialogo();
      pintarDialogo();
      return recado(`No se puede clausular a ${arg}: ${e.message}`, 'crit');
    }
    return pintarDialogo();
  }

  /* Probar un endpoint del catálogo. Solo GET sin parámetros, así que no mueve dinero.
     Se pinta primero el cargador y luego el resultado: la primera llamada de una ruta
     lenta tarda segundos por el límite de peticiones, y sin eso el botón parece muerto. */
  if (accion === 'probar') {
    V.sondaUI.ruta = arg; V.sondaUI.cargando = true; V.sondaUI.r = null;
    pintar();
    V.sondaUI.r = await api.sondear(arg);
    V.sondaUI.cargando = false;
    return pintar();
  }
  if (accion === 'cerrar-sonda') { V.sondaUI.ruta = ''; V.sondaUI.r = null; return pintar(); }

  if (accion === 'guardar-ajustes' || accion === 'probar-telegram') {
    const token = document.getElementById('tg-token')?.value.trim() || '';
    const chat = document.getElementById('tg-chat')?.value.trim() || '';
    const horas = document.getElementById('aln-horas')?.value.trim() || '';
    pintar();
    await (accion === 'guardar-ajustes'
      ? AJ.aplicarAj(token, chat, horas) : AJ.probarAj(token, chat));
    return pintar();
  }

  if (accion === 'buscar-mercado') {
    B.busq.filtro.min_puntos = Number(document.getElementById('bq-pts')?.value || 0);
    B.busq.filtro.max_precio = document.getElementById('bq-max')?.value.trim() || '';
    pintar();
    await B.buscarAhora();
    return pintar();
  }
  if (accion === 'guardar-busqueda') {
    const n = prompt('Nombre para la búsqueda (aparecerá en el aviso de Telegram):');
    if (!n) return;
    await B.guardarComoAviso(n);
    return pintar();
  }
  if (accion === 'borrar-aviso') { await B.quitarAviso(arg); return pintar(); }

  if (accion === 'cerrar-candado' || accion === 'abrir-candado') {
    try {
      if (accion === 'cerrar-candado') {
        await api.bloquearOnce('fijado desde Coconut');
        recado('Once bloqueado: el bot no lo tocará y no se puede vender a un titular.', 'ok');
      } else {
        await api.desbloquearOnce();
        recado('Candado abierto. Acuérdate de volver a cerrarlo al terminar.', 'ok');
      }
      return cargar();
    } catch (e) { return recado(e.message, 'crit'); }
  }

  if (accion === 'salir') { await L.salir(); abrirMenu(false); return cargar(); }

  if (accion === 'crear-tarea') {
    const g = (id) => document.getElementById(id)?.value.trim() || '';
    const cuando = g('tp-cuando');
    if (!cuando) return recado('Falta la hora.', 'crit');
    try {
      const r = await api.programar(
        g('tp-accion'),
        g('tp-jugador') ? { jugador: g('tp-jugador') } : {},
        cuando + ':00',
        g('tp-nota') || 'programado desde Coconut',
        document.getElementById('tp-auto')?.checked || false);
      recado(`Programado para el ${new Date(r.cuando).toLocaleString('es-ES')}.`, 'ok');
      return cargar();
    } catch (e) { return recado(e.message, 'crit'); }
  }

  if (accion === 'cerrar-ficha') { location.hash = estadoUI.pestana; return; }
  if (accion === 'ver-jornada') {
    const [tid] = estadoUI.equipoId.split(':');
    location.hash = `manager/${tid}:${arg}`;
    return;
  }

  if (accion === 'ver-once-jornada') {
    V.puntosUI.jornada = Number(arg); V.puntosUI.cargando = true;
    api.jornadaN(arg).then((d) => { V.puntosUI.datos = d; })
      .catch(() => { V.puntosUI.datos = null; })
      .finally(() => { V.puntosUI.cargando = false; pintar(); });
    return;
  }

  if (accion === 'ver-clasif') {
    V.clasifUI.jornada = Number(arg); V.clasifUI.cargando = true; V.clasifUI.datos = null;
    pintar();
    api.clasifJor(arg).then((d) => { V.clasifUI.datos = d; })
      .catch((e) => { V.clasifUI.datos = { clasificacion: [], error: e.message }; })
      .finally(() => { V.clasifUI.cargando = false; pintar(); });
    return;
  }

  if (accion === 'ver-optimo') {
    V.optimoUI.jornada = Number(arg); V.optimoUI.cargando = true; V.optimoUI.datos = null;
    pintar();
    api.optimo(arg).then((d) => { V.optimoUI.datos = d; })
      .catch((e) => { V.optimoUI.datos = { error: e.message }; })
      .finally(() => { V.optimoUI.cargando = false; pintar(); });
    return;
  }

  /* Las cuatro acciones de ficha van por el MISMO diálogo que las demás: así se pueden
     programar, que es la mitad de su utilidad (una puja se cancela antes de las 13:48 o
     ya no se cancela), y ninguna se ejecuta sin confirmación, que importa porque dos de
     ellas son irreversibles.

     Los datos van envueltos en `params`: es lo que lee `confirmarAccion()`. */
  if (accion === 'aceptar-oferta') {
    const [mk, of, imp] = arg.split(':');
    A.abrirAccion('aceptar-oferta', {
      params: { market_id: mk, offer_id: of, importe: Number(imp) },
      importe: Number(imp), motivo: 'cobro de una oferta recibida',
    });
    return pintar();
  }

  if (accion === 'rechazar-oferta') {
    const [mk, of] = arg.split(':');
    A.abrirAccion('rechazar-oferta', {
      params: { market_id: mk, offer_id: of }, motivo: 'oferta por debajo del mínimo',
    });
    return pintar();
  }

  if (accion === 'cancelar-puja') {
    const [mk, bid] = arg.split(':');
    A.abrirAccion('cancelar-puja', {
      params: { market_id: mk, bid_id: bid }, motivo: 'liberar la caja retenida',
    });
    return pintar();
  }

  if (accion === 'retirar-mercado') {
    A.abrirAccion('retirar-mercado', {
      params: { market_id: arg }, motivo: 'ya no se quiere vender',
    });
    return pintar();
  }

  /* La política de venta se edita con un formulario, no con el diálogo de acciones: no
     ejecuta nada contra LaLiga, solo deja escrita una regla para cuando llegue la oferta.
     Meterla en el diálogo de confirmación diría «vas a gastar», que sería falso. */
  if (accion === 'politica-venta') { V.ventasUI.abierto = arg; return pintar(); }
  if (accion === 'cerrar-politica') { V.ventasUI.abierto = null; return pintar(); }

  if (accion === 'guardar-politica') {
    const $v = (id) => document.getElementById(id);
    const cuerpo = {
      modo: $v('pol-modo')?.value || 'prima',
      prima_min: Number($v('pol-prima')?.value || 0),
      no_antes_de_congelar: !!$v('pol-congelar')?.checked,
      relistar: !!$v('pol-relistar')?.checked,
    };
    try {
      await api.guardarVenta(arg, cuerpo);
      /* Se recarga la lista en vez de parchear el objeto en memoria: si el servidor
         normaliza algo (un modo inválido, un porcentaje fuera de rango), lo que se pinta
         tiene que ser lo que queda guardado, no lo que se manda. */
      estadoUI.datos.ventas = await api.ventas().catch(() => estadoUI.datos.ventas);
      V.ventasUI.abierto = null;
      recado('Política guardada', 'ok');
    } catch (e) {
      recado(`No se pudo guardar: ${e.message}`, 'crit');
    }
    return pintar();
  }

  if (accion === 'mas-actividad') { V.actividadUI.visibles += 40; return pintar(); }

  if (accion === 'config-job')   { V.motorUI.abierto = arg;  return pintar(); }
  if (accion === 'cerrar-config') { V.motorUI.abierto = null; return pintar(); }

  if (accion === 'guardar-job') {
    /* Se lee del DOM al guardar, igual que los ajustes: así no hay un segundo sitio
       donde el estado pueda quedar desincronizado con lo que se ve. */
    const cat = estadoUI.datos?.trabajos?.catalogo?.[arg];
    if (!cat) return;
    const cambios = {};
    for (const o of cat.opciones || []) {
      const el = document.getElementById(`cfg-${arg}-${o.clave}`);
      if (!el) continue;
      if (o.tipo === 'bool') cambios[o.clave] = el.checked;
      else if (o.tipo === 'numero') {
        const n = Number(el.value);
        if (Number.isFinite(n)) cambios[o.clave] = n;
      } else {
        /* Horas y minutos se escriben separados por comas. Una lista vacía dejaría el
           proceso sin horario, así que se descarta en vez de guardarla. */
        const lista = el.value.split(',').map((x) => x.trim()).filter(Boolean);
        if (lista.length) cambios[o.clave] = lista;
      }
    }
    try {
      await api.guardarConfigJob(arg, cambios);
      recado('Guardado. Se aplica en menos de 10 minutos.', 'ok');
      V.motorUI.abierto = null;
      api.invalidar(); await cargar();
    } catch (e) { recado(`No se ha podido guardar: ${e.message}`, 'crit'); }
    return;
  }

  if (accion === 'lanzar-job') {
    /* No se pide confirmación porque ninguno gasta caja: eso vive en /accion/* con su
       diálogo. Ojo, `ofertas` sí puede vender, así que cambia la plantilla. Y lo que se
       puede lanzar es una lista blanca cerrada (`LANZABLES`); el resto da 400. */
    try {
      await api.lanzarJob(arg);
      recado(`«${arg}» lanzado. El worker lo recoge en menos de 20 s.`, 'ok');
      setTimeout(() => { api.invalidar(); cargar(); }, 22000);
    } catch (e) { recado(`No se ha podido lanzar: ${e.message}`, 'crit'); }
    return;
  }

  if (accion === 'cancelar-seg') {
    try { await api.cancelarSeguimiento(arg); recado('Vigilancia cancelada.', 'ok'); await cargar(); }
    catch (e) { recado(`No se ha podido cancelar: ${e.message}`, 'crit'); }
    return;
  }

  if (accion === 'cancelar-tarea') {
    try { await api.cancelarTarea(arg); recado('Tarea cancelada.', 'ok'); await cargar(); }
    catch (e) { recado(`No se ha podido cancelar: ${e.message}`, 'crit'); }
    return;
  }

  avisarPendiente(accion);
}

/** Del menú del jugador al diálogo de confirmación, con el importe que toca. */
function abrirConfirmacion(accion, j) {
  const importe = { vender: j.valor, 'subir-clausula': j.valor, blindar: 0 }[accion] ?? 0;
  A.abrirAccion(accion, {
    nombre: j.nombre,
    importe,
    params: { jugador: j.nombre, ...(importe ? { importe } : {}) },
    motivo: `${accion} desde la web`,
  });
  pintarDialogo();
}

async function confirmarDialogo() {
  if (!A.puedeConfirmar()) return;
  /* Por encima del umbral la API exige el importe repetido; el diálogo ya lo ha
     hecho teclear, así que se manda tal cual. */
  if (A.dialogo.tecleado) {
    A.dialogo.datos.params.importe_confirmado = Number(A.dialogo.tecleado.replace(/\D/g, ''));
  }
  A.dialogo.ocupado = true;
  pintarDialogo();          // enseña «Enviando…» antes de irse a la red
  await A.confirmarAccion();
  pintarDialogo();
}

/** Guardar el once son DOS escrituras y las dos importan: mandarlo a LaLiga, y
    fijarlo para que `job_alineacion` no lo recalcule esta noche. Si la segunda
    falla hay que decirlo, porque el once se ha guardado pero es efímero. */
async function accionGuardarOnce() {
  const d = estadoUI.datos;
  const objetivo = d.estado.jornada.en_juego ? d.estado.jornada.siguiente : d.estado.jornada.numero;
  const cuerpo = E.payload();
  E.editor.guardando = true; pintar();
  try {
    await api.guardarOnce(cuerpo);
    try {
      await api.fijarOnce(cuerpo, objetivo, 'editado a mano desde la web');
      recado(`Once guardado y fijado para la J${objetivo}.`, 'ok');
    } catch (e) {
      recado(`Once guardado, pero NO se pudo fijar (${e.message}): el bot puede recalcularlo `
           + 'esta noche y borrarlo.', 'crit');
    }
    E.editor.activo = false;
    await cargar();
  } catch (e) {
    recado(`No se ha guardado: ${e.message}`, 'crit');
  } finally {
    E.editor.guardando = false;
    if (E.editor.activo) pintar();
  }
}

async function accionRecalcular() {
  try {
    const r = await api.recalcular();
    recado(`Once recalculado por el modelo: ${(r.payload?.tactical_formation || []).join('-')}.`, 'ok');
    await cargar();
  } catch (e) {
    recado(`No se ha podido recalcular: ${e.message}`, 'crit');
  }
}

/** Un aviso al principio del contenido. Se queda hasta el siguiente repintado:
    un mensaje que se desvanece solo se pierde justo cuando importa leerlo. */
function recado(texto, tono = 'info') {
  const nodo = document.createElement('div');
  nodo.innerHTML = U.aviso({ tono, titulo: tono === 'crit' ? 'Algo ha fallado' : 'Hecho', texto });
  $('#contenido').prepend(nodo.firstElementChild);
  window.scrollTo({ top: 0, behavior: 'smooth' });
}

document.addEventListener('keydown', (ev) => {
  if (ev.key === 'Escape' && !$('#menu').hidden) abrirMenu(false);
});

/* Comodín para un `data-accion` que nadie atiende. Decirlo en pantalla convierte un
   botón muerto en un fallo visible. El caso se caza antes en
   `probarAccionesHuerfanas`; esto es la red por si algo se emite en tiempo de ejecución. */
function avisarPendiente(accion) {
  const aviso = document.createElement('div');
  aviso.className = 'aviso aviso--warn';
  aviso.innerHTML = `<div class="aviso__cuerpo"><p class="aviso__titulo">«${U.esc(accion)}» no tiene manejador</p>
    <p class="aviso__texto">Ese botón no está cableado a ninguna acción.</p></div>`;
  $('#contenido').prepend(aviso);
  aviso.scrollIntoView({ block: 'center', behavior: 'smooth' });
  setTimeout(() => aviso.remove(), 6000);
}

window.addEventListener('hashchange', () => { desdeHash(); pintarArmazon(); pintar(); });

desdeHash();
pintarArmazon();
pintarPie();
cargar();
