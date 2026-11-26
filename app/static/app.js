const $ = s => document.querySelector(s);
const esc = t => String(t ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const COLOR = {energia:'--energia',agua:'--agua',facturacion:'--facturacion',atencion:'--atencion',otros:'--otros'};
const NOMBRE_CAT = {energia:'Energía',agua:'Agua',facturacion:'Facturación',atencion:'Atención',otros:'Otros'};
const NOMBRE_TIPO = {peticion:'Petición',queja:'Queja',reclamo:'Reclamo',sugerencia:'Sugerencia'};
const NOMBRE_EST = {nuevo:'Nuevo',en_proceso:'En proceso',resuelto:'Resuelto'};
const ORDEN_P = {alta:0,media:1,baja:2};
let datos = [], filtro = '', sel = null;
// La sesión vive solo mientras la pestaña esté abierta (sessionStorage); el token caduca en el servidor.
let TOKEN = sessionStorage.getItem('pqrs_token') || '';
let YO = JSON.parse(sessionStorage.getItem('pqrs_yo') || 'null');
let CONFIG = null;
async function api(url, opts = {}){
  const r = await fetch(url, {...opts, headers: {...(opts.headers || {}), ...(TOKEN ? {Authorization: 'Bearer ' + TOKEN} : {})}});
  if (r.status === 401){ cerrarLocal(); pedirLogin(); }
  return r;
}
function cerrarLocal(){ TOKEN = ''; YO = null; CONFIG = null; sessionStorage.removeItem('pqrs_token'); sessionStorage.removeItem('pqrs_yo'); pintarBarra(); }
function pedirLogin(){ const d = $('#dlgLogin'); if (!d.open){ d.showModal(); $('#loginUsuario').focus(); } }
function pintarBarra(){
  $('#quien').textContent = YO ? `${YO.usuario} (${YO.rol})` : '';
  $('#salir').hidden = !YO;
}
const SOLO_LECTURA = () => YO && YO.rol === 'consulta';

function hace(iso){
  const s = Math.max(0, (Date.now() - new Date(iso + 'Z')) / 1000);
  if (s < 60) return 'ahora';
  if (s < 3600) return `hace ${Math.floor(s/60)} min`;
  if (s < 86400) return `hace ${Math.floor(s/3600)} h`;
  return `hace ${Math.floor(s/86400)} d`;
}
async function mensajeError(r){
  const j = await r.json().catch(() => ({}));
  if (typeof j.detail === 'string') return j.detail;
  if (Array.isArray(j.detail)) return j.detail.map(x => (x.msg || '').replace(/^Value error, /, '')).join('. ');
  return 'No se pudo completar la acción';
}
function aviso(t){ const a=$('#aviso'); a.textContent=t; a.classList.add('ver'); setTimeout(()=>a.classList.remove('ver'),2200); }

async function cargar(){
  if (!CONFIG){ const rc = await api('/config'); if (rc.ok) CONFIG = await rc.json(); }
  const r = await api('/solicitudes'); if (!r.ok) return; datos = await r.json();
  if (sel && !datos.some(d => d.id === sel)) sel = null;
  pintar();
}

function visibles(){
  let v = datos.filter(d => (!filtro || d.estado === filtro) && (!$('#soloAltas').checked || d.prioridad === 'alta'));
  return v.sort((a,b) => (a.estado==='resuelto') - (b.estado==='resuelto')
                      || ORDEN_P[a.prioridad] - ORDEN_P[b.prioridad]
                      || new Date(b.creado) - new Date(a.creado));
}

function pintar(){
  const cuenta = e => datos.filter(d => d.estado === e).length;
  $('#tabs').innerHTML = [['', 'Todas', datos.length], ['nuevo','Nuevas',cuenta('nuevo')], ['en_proceso','En proceso',cuenta('en_proceso')], ['resuelto','Resueltas',cuenta('resuelto')]]
    .map(([v,n,c]) => `<button class="tab" data-f="${v}" aria-pressed="${filtro===v}">${n}<b>${c}</b></button>`).join('');

  const lista = visibles();
  $('#cola').innerHTML = lista.map(d => `
    <button class="item ${d.estado==='resuelto'?'resuelto':''}" data-id="${d.id}" data-p="${d.prioridad}" aria-current="${d.id===sel}">
      <div class="linea1"><span>${esc(d.ciudadano)}</span><span class="hora">${hace(d.creado)}</span></div>
      <p>${esc(d.resumen)}</p>
      <div class="meta"><span><span class="punto" style="--cat:var(${COLOR[d.categoria]||'--otros'})"></span>${NOMBRE_CAT[d.categoria]}</span>
        <span>${NOMBRE_TIPO[d.tipo]}</span><span class="p">Prioridad ${d.prioridad}</span><span>${NOMBRE_EST[d.estado]}</span>${plazoCorto(d) ? `<span class="p venc">${plazoCorto(d)}</span>` : ''}</div>
    </button>`).join('') || '<div class="vacio">No hay solicitudes con estos filtros.</div>';
  document.body.classList.toggle('detalle-abierto', sel !== null);  // en móvil, el detalle ocupa toda la pantalla
  pintarDetalle();
}

const DIA = 86400000;
const t_ = iso => new Date(iso + 'Z');
const fechaLarga = iso => t_(iso).toLocaleDateString('es-CO', {day:'numeric', month:'long', year:'numeric'});
function diasRestantes(d){ return Math.ceil((t_(d.fecha_limite) - Date.now()) / DIA); }
function esVencido(d){ return !d.respondido_en && diasRestantes(d) < 0; }
function plazoTexto(d){
  if (d.respondido_en) return t_(d.respondido_en) <= t_(d.fecha_limite) ? 'Respondida a tiempo' : 'Respondida fuera de plazo';
  const n = diasRestantes(d);
  if (n < 0) return `Vencido hace ${-n} ${-n === 1 ? 'día' : 'días'}`;
  if (n === 0) return 'Vence hoy';
  const fecha = t_(d.fecha_limite).toLocaleDateString('es-CO', {day:'numeric', month:'long'});
  return `Vence el ${fecha} (en ${n} ${n === 1 ? 'día' : 'días'})`;
}
function plazoCorto(d){
  if (d.entrega === 'rebotado') return 'Correo rebotó';
  if (d.respondido_en) return '';
  const n = diasRestantes(d);
  return n < 0 ? 'Vencida' : n <= 3 ? `Vence en ${n} d` : '';
}

// Estado de entrega según el proveedor de correo (webhook). Textos fijos.
function avisoEntrega(d){
  if (d.canal_respuesta !== 'correo') return '';
  if (d.entrega === 'rebotado') return '<p class="rebote" role="alert">El correo rebotó: la persona no lo recibió. Contáctela por otro medio.</p>';
  if (d.entrega === 'entregado') return '<p class="entregado">Entrega confirmada por el proveedor de correo.</p>';
  return '<p class="ayuda">Entrega sin confirmar todavía.</p>';
}

// Texto fijo (no proviene del usuario): indica si el dueño del correo ya lo confirmó
function etiquetaCorreo(d){ return !d.correo ? '' : d.correo_verificado ? ' (verificado)' : ' (sin verificar)'; }

function bloqueRespuesta(d){
  const puede = YO && YO.rol !== 'consulta';
  if (d.respondido_en){
    const donde = d.canal_respuesta === 'correo' ? 'Enviada por correo (aceptada por el servidor; la entrega no se confirma)' : 'Registrada como respondida por otro medio';
    return `<div class="borrador enviada">
      <h3 class="s">Respuesta oficial</h3>
      <p class="ayuda">${donde} el ${fechaLarga(d.respondido_en)} · ${esc(d.respondido_por)}</p>
      <div class="texto-enviado">${esc(d.respuesta_final)}</div>
      ${avisoEntrega(d)}
    </div>`;
  }
  const motivo = !puede ? 'Su rol es de solo consulta.'
    : !d.correo ? 'Esta solicitud no tiene correo: responda por otro medio.'
    : !d.correo_verificado ? 'El correo de esta persona aún no está verificado: responda por otro medio o espere a que lo confirme.'
    : (CONFIG && !CONFIG.correo_configurado) ? 'El envío de correo no está configurado (ver DESPLIEGUE.md). Puede registrar la respuesta por otro medio.' : '';
  return `<div class="borrador">
      <label class="titulo" for="resp">Borrador de respuesta</label>
      <p class="ayuda">Revise y ajuste el texto. Lo que envíe quedará guardado como respuesta oficial.</p>
      <textarea id="resp" ${puede ? '' : 'readonly'}>${esc(d.respuesta_sugerida)}</textarea>
      <div class="acciones">
        <button class="btn" id="enviarCorreo" type="button" ${motivo ? 'disabled' : ''}>Enviar por correo</button>
        <button class="btn claro" id="otroMedio" type="button" ${puede ? '' : 'disabled'}>Respondí por otro medio</button>
        <button class="btn claro" id="copiar" type="button">Copiar texto</button>
      </div>
      ${motivo ? `<p class="ayuda motivo">${motivo}</p>` : ''}
    </div>`;
}

function pintarDetalle(){
  const d = datos.find(x => x.id === sel);
  if (!d){ $('#detalle').innerHTML = '<div class="nada"><div><b>Ninguna solicitud abierta</b><br>Elija una de la cola para revisarla.</div></div>'; return; }
  const c = {alta:'--alta',media:'--media',baja:'--baja'}[d.prioridad];
  const opciones = d.respondido_en ? ['en_proceso','resuelto'] : ['nuevo','en_proceso'];
  $('#detalle').innerHTML = `<div class="contenido">
    <button class="volver" id="volver" type="button">← Volver a la cola</button>
    <h2>${esc(d.ciudadano)}</h2>
    <p class="sub">${esc(d.correo || 'Sin correo')}${etiquetaCorreo(d)} · llegó ${hace(d.creado)} · solicitud ${d.id}</p>
    <div class="cols"><div class="izq">
    <blockquote>${esc(d.texto)}</blockquote>
    ${bloqueRespuesta(d)}
    </div>
    <div class="der">
    <h3 class="s">Clasificación</h3>
    <dl>
      <dt>Tipo</dt><dd>${NOMBRE_TIPO[d.tipo]}</dd>
      <dt>Categoría</dt><dd><span class="punto" style="--cat:var(${COLOR[d.categoria]||'--otros'})"></span>${NOMBRE_CAT[d.categoria]}</dd>
      <dt>Prioridad</dt><dd><span class="pri" style="--c:var(${c})"><i></i>${d.prioridad[0].toUpperCase()+d.prioridad.slice(1)}</span></dd>
      <dt>Área responsable</dt><dd>${esc(d.area)}</dd>
      <dt>Plazo</dt><dd class="${esVencido(d) ? 'venc' : ''}">${plazoTexto(d)}</dd>
      <dt>Clasificada por</dt><dd>${d.metodo==='ia'?'IA gratuita, revisada por reglas':'Reglas automáticas'}</dd>
    </dl>
    <label class="titulo">Estado</label>
    <div class="seg" role="group" aria-label="Estado de la solicitud">
      ${opciones.map(e => `<button data-e="${e}" aria-pressed="${d.estado===e}" ${SOLO_LECTURA() ? 'disabled title="Su rol es de solo consulta"' : ''}>${NOMBRE_EST[e]}</button>`).join('')}
    </div>
    ${d.respondido_en ? '' : '<p class="ayuda">Se marca como resuelta al enviar la respuesta.</p>'}
    </div></div></div>`;
}

function confirmar(titulo, texto, etiqueta){
  return new Promise(resolver => {
    const dlg = $('#dlgConfirmar');
    $('#confTitulo').textContent = titulo; $('#confTexto').textContent = texto; $('#confOk').textContent = etiqueta;
    dlg.returnValue = '';
    dlg.addEventListener('close', () => resolver(dlg.returnValue === 'ok'), {once: true});
    dlg.showModal();
  });
}

async function responder(canal, boton){
  const d = datos.find(x => x.id === sel), texto = $('#resp').value.trim();
  if (texto.split(/\s+/).length < 3){ aviso('Escriba una respuesta de al menos 3 palabras.'); $('#resp').focus(); return; }
  const ok = canal === 'correo'
    ? await confirmar('¿Enviar esta respuesta?', 'Se enviará por correo a ' + d.correo + '. No se puede deshacer.', 'Enviar respuesta')
    : await confirmar('¿Registrar la respuesta?', 'Confirme que ya respondió a esta persona por otro medio. El texto quedará guardado como respuesta oficial.', 'Registrar respuesta');
  if (!ok) return;
  const etiqueta = boton.textContent;
  boton.disabled = true; boton.textContent = canal === 'correo' ? 'Enviando…' : 'Guardando…';
  const r = await api('/solicitudes/' + sel + '/responder', {method:'POST', headers:{'Content-Type':'application/json'},
                                                              body: JSON.stringify({respuesta: texto, canal})});
  if (r.ok){ aviso(canal === 'correo' ? 'Respuesta enviada. Revise la bandeja de la cuenta por si el correo rebota.' : 'Respuesta registrada'); await cargar(); }
  else { aviso(await mensajeError(r)); boton.disabled = false; boton.textContent = etiqueta; }
}

document.addEventListener('click', async e => {
  const t = e.target.closest('button'); if (!t) return;
  if (t.classList.contains('tab')){ filtro = t.dataset.f; pintar(); }
  else if (t.classList.contains('item')){ sel = +t.dataset.id; pintar(); $('#detalle').scrollTop = 0; }
  else if (t.id === 'volver'){ sel = null; pintar(); }
  else if (t.id === 'copiar'){ await navigator.clipboard.writeText($('#resp').value); aviso('Texto copiado'); }
  else if (t.id === 'enviarCorreo'){ await responder('correo', t); }
  else if (t.id === 'otroMedio'){ await responder('otro', t); }
  else if (t.dataset.e){
    const r = await api('/solicitudes/'+sel,{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify({estado:t.dataset.e})});
    aviso(r.ok ? 'Estado: ' + NOMBRE_EST[t.dataset.e] : await mensajeError(r)); cargar();
  }
});
$('#soloAltas').addEventListener('change', pintar);
$('#nueva').onclick = () => $('#dlg').showModal();
$('#cancelar').onclick = () => $('#dlg').close();
$('#form').addEventListener('submit', async e => {
  const body = Object.fromEntries(new FormData(e.target));
  const r = await api('/solicitudes',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  if (r.ok){ const n = await r.json(); e.target.reset(); sel = n.id; aviso('Solicitud registrada y clasificada'); cargar(); }
  else aviso(await mensajeError(r));
});
cargar();

$('#formLogin').addEventListener('submit', async e => {
  e.preventDefault();
  const f = new FormData(e.target);
  const usuario = f.get('usuario').trim(), clave = f.get('clave');
  const error = $('#errorLogin'), boton = $('#entrar');
  if (!usuario || !clave){
    error.textContent = !usuario ? 'Escriba su usuario.' : 'Escriba su clave.';
    $(!usuario ? '#loginUsuario' : '#loginClave').focus();
    return;
  }
  error.textContent = '';
  boton.disabled = true; boton.textContent = 'Entrando…';   // la verificación de la clave tarda un instante a propósito
  try {
    const r = await fetch('/auth/login', {method:'POST', headers:{'Content-Type':'application/json'},
                                          body: JSON.stringify({usuario, clave})});
    if (!r.ok){
      error.textContent = r.status === 429 ? await mensajeError(r) : 'Usuario o clave incorrectos. Revise e intente de nuevo.';
      $('#loginClave').select();
      return;
    }
    const j = await r.json();
    TOKEN = j.token; YO = {usuario: j.usuario, rol: j.rol};
    sessionStorage.setItem('pqrs_token', TOKEN); sessionStorage.setItem('pqrs_yo', JSON.stringify(YO));
    e.target.reset(); $('#dlgLogin').close();
    pintarBarra(); cargar();
  } catch {
    error.textContent = 'No pudimos conectar con el servidor. Revise su conexión e intente de nuevo.';
  } finally {
    boton.disabled = false; boton.textContent = 'Entrar';
  }
});
$('#verClave').addEventListener('click', e => {
  const campo = $('#loginClave'), visible = campo.type === 'text';
  campo.type = visible ? 'password' : 'text';
  e.target.textContent = visible ? 'Mostrar' : 'Ocultar';
  e.target.setAttribute('aria-pressed', String(!visible));
  campo.focus();
});
// Sin sesión no hay panel: Esc no puede cerrar la pantalla de acceso
$('#dlgLogin').addEventListener('cancel', e => e.preventDefault());
$('#salir').onclick = async () => { await api('/auth/logout', {method:'POST'}); cerrarLocal(); datos = []; sel = null; pintar(); pedirLogin(); };
pintarBarra();
if (!TOKEN) pedirLogin();
