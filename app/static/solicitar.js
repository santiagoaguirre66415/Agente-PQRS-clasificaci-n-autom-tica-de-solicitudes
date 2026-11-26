const $ = s => document.querySelector(s);

// ---- Prueba de trabajo: un cálculo breve que demuestra que quien envía es un navegador real con una persona detrás ----
function cerosIniciales(h){
  let n = 0;
  for (const b of h){
    if (b === 0){ n += 8; continue; }
    return n + Math.clz32(b) - 24;
  }
  return n;
}

async function resolver(reto, bits){
  const enc = new TextEncoder(), LOTE = 64;
  for (let n = 0; ; n += LOTE){
    const hashes = await Promise.all(Array.from({length: LOTE}, (_, i) =>
      crypto.subtle.digest('SHA-256', enc.encode(reto + ':' + (n + i)))));
    for (let i = 0; i < LOTE; i++){
      if (cerosIniciales(new Uint8Array(hashes[i])) >= bits) return String(n + i);
    }
  }
}

async function nuevaPrueba(){
  if (!(window.crypto && crypto.subtle)) throw new Error('Su navegador no permite la comprobación. Abra esta página con una conexión segura (https).');
  const r = await fetch('/api/reto');
  if (!r.ok) throw new Error('No pudimos iniciar la comprobación. Espere un momento e intente de nuevo.');
  const { reto, bits } = await r.json();
  return { reto, prueba: await resolver(reto, bits) };
}

// El cálculo empieza al abrir la página, mientras la persona escribe; casi siempre ya está listo al enviar
let pruebaLista = null;
function precalcular(){ pruebaLista = nuevaPrueba(); pruebaLista.catch(() => {}); }

async function mensajeError(r){
  const j = await r.json().catch(() => ({}));
  if (typeof j.detail === 'string') return j.detail;
  if (Array.isArray(j.detail)) return j.detail.map(x => (x.msg || '').replace(/^Value error, /, '')).join('. ');
  return 'No pudimos enviar su solicitud. Intente de nuevo.';
}

async function enviar(cuerpo, intento = 1){
  const { reto, prueba } = await pruebaLista;
  const r = await fetch('/solicitudes', {method: 'POST', headers: {'Content-Type': 'application/json'},
                                          body: JSON.stringify({...cuerpo, reto, prueba})});
  if (r.status === 400 && intento === 1){   // el reto venció o se usó: se pide otro y se reintenta una vez
    precalcular();
    return enviar(cuerpo, 2);
  }
  return r;
}

$('#form').addEventListener('submit', async e => {
  e.preventDefault();
  const f = new FormData(e.target), error = $('#error'), estado = $('#estado'), boton = $('#enviar');
  error.textContent = '';
  const cuerpo = {ciudadano: f.get('ciudadano').trim(), correo: f.get('correo').trim(), texto: f.get('texto').trim(),
                  web: f.get('web'), acepto: $('#acepto').checked};
  if (cuerpo.ciudadano.length < 2){ error.textContent = 'Escriba su nombre.'; $('#nombre').focus(); return; }
  if (cuerpo.texto.split(/\s+/).length < 3){ error.textContent = 'Cuéntenos su solicitud con al menos 3 palabras.'; $('#texto').focus(); return; }
  if (!cuerpo.acepto){ error.textContent = 'Para enviar su solicitud debe autorizar el tratamiento de sus datos.'; $('#acepto').focus(); return; }

  boton.disabled = true; boton.textContent = 'Enviando…';
  estado.textContent = 'Comprobando que usted es una persona…';
  try {
    const r = await enviar(cuerpo);
    if (!r.ok){ error.textContent = await mensajeError(r); return; }
    const j = await r.json();
    $('#numero').textContent = j.id ? 'n.º ' + j.id : '';
    $('#mensajeListo').textContent = j.mensaje;
    $('#formulario').hidden = true; $('#listo').hidden = false; $('#listo').focus();
    e.target.reset();
  } catch (err) {
    error.textContent = err.message || 'No pudimos conectar con el servidor. Revise su conexión e intente de nuevo.';
  } finally {
    boton.disabled = false; boton.textContent = 'Enviar solicitud'; estado.textContent = '';
  }
});

$('#otra').addEventListener('click', () => {
  $('#listo').hidden = true; $('#formulario').hidden = false; precalcular(); $('#nombre').focus();
});

precalcular();
