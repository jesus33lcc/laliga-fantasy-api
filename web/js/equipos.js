/* ============================================================================
   equipos.js: nombres, siglas y escudos por `teamId`.

   La fuente es **`GET {API}/v3/teams-master`**, que devuelve los 42 equipos del
   sistema con `name`, `shortName`, `slug` y las URLs de los escudos. Su `id` **es**
   el `teamId`. Lo de abajo es una copia de esa respuesta, para no depender de la red
   al pintar: si LaLiga cambia algo, se regenera desde ahí, no se escribe a mano.

   El escudo no se puede adivinar: la URL lleva el **slug al final**, y el slug solo
   sale de ese endpoint.

       /teambadge/t{assetId}/color/t{assetId}_{slug}.png
   ========================================================================== */

export const EQUIPOS = {
  2: "Atlético de Madrid",
  3: "Athletic Club",
  4: "FC Barcelona",
  5: "Real Betis",
  6: "Celta",
  7: "Elche CF",
  8: "RCD Espanyol",
  9: "Getafe CF",
  11: "Levante UD",
  12: "Málaga CF",
  13: "C.A. Osasuna",
  14: "Rayo Vallecano",
  15: "Real Madrid",
  16: "Real Sociedad",
  17: "Sevilla FC",
  18: "Valencia CF",
  20: "Villarreal CF",
  21: "Deportivo Alavés",
  26: "RC Deportivo",
  49: "R. Racing Club",
};

/** Las tres letras que usa la app donde no cabe el nombre. */
export const EQUIPOS_CORTO = {
  2: "ATM",
  3: "ATH",
  4: "BAR",
  5: "BET",
  6: "CEL",
  7: "ELC",
  8: "ESP",
  9: "GET",
  11: "LEV",
  12: "MGA",
  13: "OSA",
  14: "RAY",
  15: "RMA",
  16: "RSO",
  17: "SEV",
  18: "VAL",
  20: "VIL",
  21: "ALA",
  26: "RCD",
  49: "RAC",
};

const SLUG = {
  2: "atletico-de-madrid",
  3: "athletic-club",
  4: "fc-barcelona",
  5: "real-betis",
  6: "rc-celta",
  7: "elche-c-f",
  8: "rcd-espanyol",
  9: "getafe-cf",
  11: "levante-ud",
  12: "malaga-cf",
  13: "c-a-osasuna",
  14: "rayo-vallecano",
  15: "real-madrid",
  16: "real-sociedad",
  17: "sevilla-fc",
  18: "valencia-cf",
  20: "villarreal-cf",
  21: "d-alaves",
  26: "rc-deportivo",
  49: "r-racing-club",
};

/** El id de los assets (`t175`) no es el `teamId`. Sale del propio `badgeColor`. */
export const ASSET_TEAM = { 2: 175, 3: 174, 4: 178, 5: 185, 6: 176, 7: 954, 8: 177, 9: 1450, 11: 855, 12: 182, 13: 450, 14: 184, 15: 186, 16: 188, 17: 179, 18: 191, 20: 449, 21: 173, 26: 180, 49: 189 };

export const nombreEquipo = (id) => EQUIPOS[id] || `Equipo ${id}`;
export const nombreCorto = (id) => EQUIPOS_CORTO[id] || String(id);

/** URL del escudo, o `null` si el equipo no se conoce: así no se pide una imagen rota. */
export const escudo = (id) => {
  const t = ASSET_TEAM[id], s = SLUG[id];
  return t && s
    ? `https://assets-fantasy.llt-services.com/teambadge/t${t}/color/t${t}_${s}.png`
    : null;
};

/** Si aparece un equipo que no está en la tabla (un ascendido a mitad de temporada),
    que se note en vez de pintar un número suelto. */
export const equipoSinResolver = (id) => !EQUIPOS[id];

/* Los managers no se pueden escribir aquí: son distintos en cada liga. Se registran al
   cargar desde `estado.clasificacion`, que ya llega en la primera petición. Mientras el
   registro esté vacío se enseña `manager <id>`, que es feo pero cierto. */
export const MANAGERS = {};
let MI_MANAGER = null;

/** Llena el registro con la clasificación de `/estado`. Idempotente. */
export function registrarManagers(clasificacion) {
  for (const f of clasificacion || []) {
    if (!f.manager_id) continue;
    MANAGERS[f.manager_id] = f.manager || `manager ${f.manager_id}`;
    if (f.es_mio) MI_MANAGER = String(f.manager_id);
  }
}

/** Si el id es el del manager propio. Con el registro vacío devuelve `false`: sin saber
    quién eres, marcar un evento como tuyo sería inventárselo. */
export const soyYo = (id) =>
  MI_MANAGER !== null && id !== undefined && id !== null && String(id) === MI_MANAGER;

/* Sin id no se devuelve `manager undefined`: los eventos de alta de usuario y de
   creación de liga no traen `user1Id`, y un texto con «undefined» dentro parece un dato
   roto aunque el evento sea correcto. */
export const nombreManager = (id) =>
  (id === undefined || id === null || id === '') ? 'LaLiga'
    : (MANAGERS[id] || `manager ${id}`);
