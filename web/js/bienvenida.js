/** La primera pantalla de una instalación sin cuenta conectada.
 *
 * El botón abre la página de login real de LaLiga y no un formulario propio, porque
 * LaLiga no expone ROPC: el flujo que validaría usuario y contraseña contra un
 * servidor propio no existe, así que un formulario aquí no podría funcionar. El sondeo
 * de las policies está en `clients/auth.py`, que es donde vive el flujo PKCE.
 *
 * Esa página ya ofrece las dos vías: Google, Apple y Facebook, y el formulario de correo
 * y contraseña. La contraseña no pasa por Coconut en ningún caso.
 */

import * as U from './ui.js';

const CLAVE = 'coconut.bienvenida.vista';

/* Iconos en SVG, no emoji.
 *
 * Un emoji depende de que el sistema traiga esa fuente, y dentro de un WebView eso no se
 * controla: sale un cuadro vacío, la fila queda coja y parece un fallo de la app. El SVG
 * se ve siempre y además toma el color del tema. */
const ICONOS = {
  precio: svg('<polyline points="3 17 9 11 13 15 21 7"/><polyline points="15 7 21 7 21 13"/>'),
  diana:  svg('<circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="5"/>'
            + '<circle cx="12" cy="12" r="1.5" fill="currentColor" stroke="none"/>'),
  campo:  svg('<rect x="3" y="4" width="18" height="16" rx="1"/><line x1="12" y1="4" x2="12" y2="20"/>'
            + '<circle cx="12" cy="12" r="3"/>'),
};

function svg(cuerpo) {
  return `<svg viewBox="0 0 24 24" width="22" height="22" fill="none"
    stroke="currentColor" stroke-width="1.8" stroke-linecap="round"
    stroke-linejoin="round" aria-hidden="true">${cuerpo}</svg>`;
}

export function toca(hayCuenta) {
  if (hayCuenta) return false;
  try {
    return localStorage.getItem(CLAVE) !== '1';
  } catch {
    // Sin localStorage (modo privado, almacenamiento bloqueado) se enseña igual: es
    // preferible repetir la bienvenida que dejar a alguien sin saber qué hacer.
    return true;
  }
}

export function marcarVista() {
  try { localStorage.setItem(CLAVE, '1'); } catch { /* da igual */ }
}

export function pintarBienvenida() {
  return `
  <section class="bienv">
    <img class="bienv__logo" src="img/logo-transparente.png"
         alt="LaLiga Fantasy Coconut" width="260" height="180">

    <h1 class="bienv__titulo">Tu liga, con datos</h1>
    <p class="bienv__lema">
      Coconut lee la API de LaLiga Fantasy y te dice lo que la app oficial no:
      quién puede robarte y por cuánto, qué precios se están moviendo, qué once
      puntúa más y cuánta caja tiene cada rival.
    </p>

    <div class="bienv__puntos">
      ${puntoBienv(ICONOS.precio, 'Precios de verdad',
              'Guarda su propia serie de precios desde el primer arranque. El momentum '
            + 'está medido sobre una temporada entera, no supuesto.')}
      ${puntoBienv(ICONOS.diana, 'Cláusulas y ventanas',
              'A quién puedes clausular, cuándo se abre la ventana y a quién te pueden '
            + 'quitar a ti.')}
      ${puntoBienv(ICONOS.campo, 'El mejor once',
              'Prueba las 7 formaciones con puntos esperados y probabilidad de ser '
            + 'titular.')}
    </div>

    <button class="btn btn--accion btn--bloque bienv__entrar" data-accion="bienv-entrar">
      Iniciar sesión con LaLiga
    </button>

    <p class="bienv__nota">
      <strong>Aquí no se crean cuentas.</strong> Se usa la que ya tienes en LaLiga
      Fantasy: al pulsar se abre la página oficial de LaLiga, con Google, Facebook, Apple
      o tu correo y contraseña. <strong>Coconut nunca ve tu contraseña.</strong>
    </p>

    <button class="btn bienv__saltar" data-accion="bienv-saltar">
      Mirar sin conectar la cuenta
    </button>
    <p class="bienv__nota bienv__nota--flojo">
      Los precios y el buscador de jugadores funcionan sin conectar nada.
    </p>

    <details class="bienv__legal">
      <summary>Quién lo hace y qué no es</summary>
      <p>
        Hecho por <strong>Jesús Luis Condori Chambi</strong>
        (<a href="https://github.com/jesus33lcc" target="_blank" rel="noopener">github.com/jesus33lcc</a>)
        como proyecto personal.
      </p>
      <p>
        <strong>No es una aplicación oficial de LaLiga</strong> ni está asociada a ella.
        LaLiga, LaLiga Fantasy y sus escudos y marcas pertenecen a sus dueños.
      </p>
      <p>
        Se ofrece <strong>tal cual, sin garantía de ninguna clase</strong>. Los datos
        pueden estar desfasados o equivocados, y las recomendaciones son estimaciones
        sobre datos públicos: <strong>las decisiones son tuyas</strong>. El autor no se
        responsabiliza de fichajes, puntos perdidos ni del uso que se le dé.
      </p>
      <p>
        Tus credenciales las gestiona <strong>LaLiga</strong>, no Coconut. El token de
        acceso se guarda <strong>solo en este dispositivo</strong> y no se manda a ningún
        sitio.
      </p>
    </details>
  </section>`;
}

const puntoBienv = (icono, titulo, texto) => `
  <div class="bienv__punto">
    <span class="bienv__icono" aria-hidden="true">${icono}</span>
    <div>
      <p class="bienv__punto-t">${U.esc(titulo)}</p>
      <p class="bienv__punto-x">${U.esc(texto)}</p>
    </div>
  </div>`;
