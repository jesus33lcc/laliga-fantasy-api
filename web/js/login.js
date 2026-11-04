/* ============================================================================
   login.js: la puerta, cuando está echada.

   Solo aparece si el servidor dice `auth_activa: true`. Con `COCONUT_AUTH=0`, que
   es lo de serie, este módulo no pinta nada: quien ponga Coconut tras una VPN o en
   su red ya tiene una puerta.

   El token no se toca desde aquí. Vive en una cookie `HttpOnly` que este código **no
   puede leer**: un XSS no se lleva la sesión.
   ========================================================================== */

import * as U from './ui.js';
import * as api from './api.js';

export const sesion = { activa: false, usuario: null, google: false, error: '', ocupado: false };

export async function comprobar() {
  try {
    const r = await api.yo();
    Object.assign(sesion, { activa: r.auth_activa, usuario: r.usuario, google: r.google });
  } catch {
    // Si `/auth/yo` no responde, no se bloquea la app: se asume abierta y ya
    // fallarán las acciones con un 401 claro.
    Object.assign(sesion, { activa: false, usuario: null });
  }
  return sesion;
}

export const hayQueEntrar = () => sesion.activa && !sesion.usuario;

export function pintarLogin() {
  return `<div class="portada">
    <form class="portada__caja" id="form-login" autocomplete="on">
      <div class="portada__marca">COCONUT</div>
      <p class="portada__lema">Análisis y automatización para LaLiga Fantasy</p>

      <label class="u-label" for="email">Correo</label>
      <input class="campo-texto" id="email" name="email" type="email"
        autocomplete="username" required inputmode="email">

      <label class="u-label" for="clave">Contraseña</label>
      <input class="campo-texto" id="clave" name="clave" type="password"
        autocomplete="current-password" required minlength="8">

      ${sesion.error ? U.aviso({ tono: 'crit', titulo: 'No se ha podido entrar', texto: sesion.error }) : ''}

      <button class="btn btn--accion btn--bloque" type="submit" ${sesion.ocupado ? 'disabled' : ''}>
        ${sesion.ocupado ? 'Entrando…' : 'Entrar'}</button>

      ${sesion.google ? `
        <div class="portada__o"><span>o</span></div>
        <button class="btn btn--bloque" type="button" data-google>Entrar con Google</button>`
      : `<p class="portada__nota">El acceso con Google no está configurado:
          faltan <code>GOOGLE_CLIENT_ID</code> y <code>GOOGLE_CLIENT_SECRET</code> en el
          <code>.env</code>.</p>`}

      <p class="portada__nota">La sesión dura 400 días: entras una vez y el móvil
        no vuelve a preguntar.</p>
    </form>
  </div>`;
}

export async function entrar(email, clave) {
  sesion.ocupado = true; sesion.error = '';
  try {
    await api.login(email, clave);
    await comprobar();
    return true;
  } catch (e) {
    sesion.error = e.message;
    return false;
  } finally {
    sesion.ocupado = false;
  }
}

export async function salir() {
  try { await api.logout(); } finally {
    sesion.usuario = null;
  }
}

export async function irAGoogle() {
  const { url } = await api.googleInicio();
  location.href = url;
}
