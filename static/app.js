const $ = id => document.getElementById(id);
const state = { status: 'offline', channels: [], contacts: {}, events: [], stats: {}, info: {}, device: {} };
let selection = null, paused = false, sending = false, backendOnline = false, historyVersion = 0;
let messageRows = [], olderAvailable = false, connectionError = false;
const drafts = new Map();
const encoder = new TextEncoder();
const statusNames = {offline:'Offline', connecting:'Verbinde …', connected:'Verbunden', reconnecting:'Neuverbindung …'};
const eventNames = {RX_LOG_DATA:'Funkpaket', RAW_DATA:'Rohdaten', ADVERTISEMENT:'Advertisement', NEW_CONTACT:'Neuer Kontakt', PATH_UPDATE:'Route aktualisiert', ACK:'Bestätigung', MESSAGE_SENT:'Nachricht gesendet', CHANNEL_MSG_RECV:'Channel-Nachricht', CONTACT_MSG_RECV:'Direktnachricht', CONNECTED:'Verbunden', CONNECTION_ERROR:'Verbindungsfehler', TRACE_DATA:'Route / Trace'};
const radioTypes = ['RX_LOG_DATA','RAW_DATA'];
function node(tag, className, text) { const el = document.createElement(tag); if(className) el.className=className; if(text!==undefined) el.textContent=text; return el; }
function error(message) { $('error').textContent=message || ''; $('error').hidden=!message; }
async function api(path, body) {
  const response=await fetch(path, body===undefined ? {} : {method:'POST',headers:{'Content-Type':'application/json','X-Meshcore-Client':'web'},body:JSON.stringify(body)});
  const result=await response.json();
  if(!response.ok) throw new Error(typeof result.detail==='string'?result.detail:'Die Anfrage konnte nicht verarbeitet werden.');
  return result;
}
function contactName(key) { const found=Object.entries(state.contacts).find(([k])=>k.startsWith(key)); return found?.[1]?.adv_name || key; }
function keyFor(s) {return s ? `${s.kind}:${s.target}` : '';}
function renderNav() {
  $('channel-count').textContent=state.channels.length;
  $('channels').replaceChildren();
  if(!state.channels.length) $('channels').append(node('p','nav-empty','Keine Channels geladen.'));
  for(const channel of state.channels) {
    const s={kind:'channel',target:String(channel.index),name:channel.name};
    const b=node('button','nav-item'+(keyFor(selection)===keyFor(s)?' active':''));
    b.append(node('span','','#'),node('span','nav-name',channel.name.replace(/^#/,''))); b.onclick=()=>openChat(s); $('channels').append(b);
  }
  const contacts=Object.entries(state.contacts).filter(([,c])=>c.type===1).sort((a,b)=>(a[1].adv_name||a[0]).localeCompare(b[1].adv_name||b[0]));
  $('contact-count').textContent=contacts.length;
  $('contacts').replaceChildren();
  const search=$('contact-search').value.toLowerCase();
  for(const [key,c] of contacts) {
    const name=c.adv_name||key;
    if(!`${name} ${key}`.toLowerCase().includes(search)) continue;
    const s={kind:'dm',target:key,name,unknown:!!c.unknown};
    const b=node('button','nav-item'+(keyFor(selection)===keyFor(s)?' active':'')); b.title=key;
    b.append(node('span','contact-avatar',name.slice(0,2).toUpperCase()),node('span','nav-name',name)); b.onclick=()=>openChat(s); $('contacts').append(b);
  }
  if(!contacts.length) $('contacts').append(node('p','nav-empty','Chat-Kontakte erscheinen nach dem Verbinden.'));
  $('monitor-nav').classList.toggle('active',!selection);
}
function applyState(data) {
  Object.assign(state,data); backendOnline=true;
  $('endpoint').textContent=`${state.host}:${state.port}`;
  $('device-name').textContent=state.info.name||'Dein Companion';
  $('metric-endpoint').textContent=`TCP · ${state.host}:${state.port}`;
  $('metric-contacts').textContent=Object.keys(state.contacts).length || '0';
  $('metric-rx').textContent=state.stats.STATS_PACKETS?.recv ?? '—';
  $('metric-rx-hint').textContent=state.stats.STATS_PACKETS?'Seit Gerätestart':'Warte auf Funkstatistik';
  $('metric-rssi').textContent=state.stats.STATS_RADIO?.last_rssi!==undefined ? `${state.stats.STATS_RADIO.last_rssi} dBm` : '—';
  $('metric-snr').textContent=state.stats.STATS_RADIO?.last_snr!==undefined ? `SNR ${state.stats.STATS_RADIO.last_snr} dB` : 'RSSI / SNR';
  const info=[['Adresse',`${state.host}:${state.port}`],['Gerät',state.info.name||state.device.model||'—'],['Firmware',state.device.ver||'—'],['Frequenz',state.info.radio_freq ? `${state.info.radio_freq} MHz`:'—'],['Rauschpegel',state.stats.STATS_RADIO?.noise_floor!==undefined?`${state.stats.STATS_RADIO.noise_floor} dBm`:'—']];
  $('radio-info').replaceChildren(...info.map(([key,value])=>{const row=node('div');row.append(node('dt','',key),node('dd','',value));return row;}));
  updateConnection(); renderNav(); renderChart(); if(!paused) renderEvents();
  if(state.error) {connectionError=true;error(`Verbindung: ${state.error} · Erneuter Versuch erfolgt automatisch.`);}
  else if(connectionError) {connectionError=false;error(null);}
}
function updateConnection() {
  const connected=backendOnline&&state.status==='connected';
  $('connection-status').textContent=backendOnline?statusNames[state.status]:'Server nicht erreichbar';
  $('metric-status').textContent=backendOnline?statusNames[state.status]:'Offline';
  $('device-dot').classList.toggle('online',connected);
  $('connect').disabled=!backendOnline||state.status!=='offline';
  $('connect').textContent=connected?'Verbunden':state.status==='offline'?'Verbinden':'Verbinde …';
  $('refresh').disabled=!connected;
  updateComposer();
}
function renderEvents() {
  const filter=$('event-filter').value;
  const events=state.events.filter(e=>filter==='all'||(filter==='radio'?radioTypes.includes(e.type):['CHANNEL_MSG_RECV','CONTACT_MSG_RECV','MESSAGE_SENT','ACK'].includes(e.type)));
  $('event-count').textContent=state.events.length;
  $('events-empty').hidden=events.length>0;
  $('events').replaceChildren(...events.slice().reverse().map(e=>{
    const p=e.payload, row=node('tr');
    const summary=p.text||p.message||p.address||p.pubkey_prefix||p.public_key||p.payload||JSON.stringify(p);
    const values=[new Date(e.time*1000).toLocaleTimeString('de-DE'), eventNames[e.type]||e.type, typeof summary==='string'?summary:JSON.stringify(summary), p.rssi??p.RSSI??'—', p.snr??p.SNR??'—'];
    for(const value of values){const cell=node('td','',value);cell.title=String(value);row.append(cell);} return row;
  }));
}
function renderChart() {
  const now=Date.now()/1000, bins=Array(30).fill(0);
  for(const e of state.events) if(radioTypes.includes(e.type)) {const i=Math.floor((e.time-(now-60))/2);if(i>=0&&i<30) bins[i]++;}
  const max=Math.max(3,...bins);
  $('activity-chart').replaceChildren(...bins.map(count=>{const bar=node('div','bar');bar.style.height=`${Math.max(2,count/max*100)}%`;bar.title=`${count} Funkpakete / 2 s`;return bar;}));
}
function rememberDraft() {if(selection) drafts.set(keyFor(selection),$('message-text').value);}
async function openChat(s) {
  rememberDraft();selection=s;historyVersion++;messageRows=[];olderAvailable=false;
  $('monitor').hidden=true;$('chat').hidden=false;
  $('breadcrumb-title').textContent=s.kind==='channel'?`# ${s.name.replace(/^#/,'')}`:s.name;
  $('chat-title').textContent=s.name;
  $('chat-icon').textContent=s.kind==='channel'?'#':s.name.slice(0,2).toUpperCase();
  $('chat-subtitle').textContent=s.kind==='channel'?'Channel · Nachrichten über dein Mesh':s.unknown?'Unbekannter Absender · zum Antworten zuerst im Companion speichern':`Direktnachricht · ${s.target.slice(0,12)}`;
  $('message-text').value=drafts.get(keyFor(s))||'';
  $('messages').replaceChildren(node('p','nav-empty','Nachrichten werden geladen …'));
  renderNav();updateComposer();await loadMessages();
}
async function loadMessages(before=null) {
  if(!selection)return;
  const current=keyFor(selection),version=++historyVersion;
  try {
    const items=await api(`/api/messages?kind=${selection.kind}&target=${encodeURIComponent(selection.target)}${before?`&before=${before}`:''}`);
    if(version!==historyVersion||keyFor(selection)!==current)return;
    const list=$('messages'), nearBottom=list.scrollHeight-list.scrollTop-list.clientHeight<120;
    const firstLoad=messageRows.length===0, oldHeight=list.scrollHeight, oldTop=list.scrollTop;
    if(firstLoad||before) olderAvailable=items.length===100;
    const merged=new Map(messageRows.map(m=>[m.id,m]));
    for(const item of items) merged.set(item.id,item);
    messageRows=[...merged.values()].sort((a,b)=>a.id-b.id);
    list.replaceChildren();
    const fragment=document.createDocumentFragment();
    if(olderAvailable){const older=node('button','button load-older','Ältere Nachrichten laden');older.onclick=()=>loadMessages(messageRows[0].id);fragment.append(older);}
    if(!messageRows.length){const empty=node('div','empty-state');empty.append(node('span','empty-symbol',selection.kind==='channel'?'#':'↗'),node('h3','','Hier beginnt das Gespräch'),node('p','','Neue Nachrichten werden lokal gespeichert. Bereits im Companion vorhandene Nachrichten werden beim Verbinden abgeholt.'));fragment.append(empty);}
    for(const message of messageRows) {
      const wrap=node('article',`message ${message.direction}`);
      const labels={received:'Empfangen',sent:selection.kind==='dm'?'An Companion übergeben · unbestätigt':'An Companion übergeben',delivered:'Zugestellt ✓'};
      const sender=message.direction==='out'?'Du':selection.kind==='dm'?contactName(message.target):selection.name;
      wrap.append(node('div','bubble',message.text),node('div','message-meta',`${sender} · ${new Date(message.timestamp*1000).toLocaleString('de-DE',{dateStyle:'short',timeStyle:'short'})} · ${labels[message.status]||message.status}`));fragment.append(wrap);
    }
    list.append(fragment);
    if(before) list.scrollTop=oldTop+list.scrollHeight-oldHeight;
    else list.scrollTop=nearBottom||firstLoad?list.scrollHeight:oldTop;
  } catch(e){error(e.message);}
}
function updateComposer() {
  const length=encoder.encode($('message-text').value.trim()).length;
  $('message-counter').textContent=`${length} / 160 Bytes`;
  $('message-counter').classList.toggle('danger',length>160);
  $('send').disabled=sending||!backendOnline||state.status!=='connected'||!selection||selection.unknown||length===0||length>160;
  $('send').textContent=sending?'Wird gesendet …':'Nachricht senden ↗';
}
$('monitor-nav').onclick=()=>{rememberDraft();selection=null;historyVersion++;$('monitor').hidden=false;$('chat').hidden=true;$('breadcrumb-title').textContent='Netzmonitor';renderNav();};
$('contact-search').oninput=renderNav;
$('event-filter').onchange=renderEvents;
$('pause').onclick=()=>{paused=!paused;$('pause').textContent=paused?'Fortsetzen':'Pausieren';if(!paused)renderEvents();};
$('connect').onclick=async()=>{try{$('connect').disabled=true;await api('/api/connect',{});}catch(e){error(e.message);updateConnection();}};
$('refresh').onclick=async()=>{try{$('refresh').disabled=true;applyState(await api('/api/refresh',{}));}catch(e){error(e.message);}finally{updateConnection();}};
$('message-text').oninput=()=>{rememberDraft();updateComposer();};
$('message-text').onkeydown=e=>{if(e.key==='Enter'&&!e.shiftKey&&!e.isComposing){e.preventDefault();if(!$('send').disabled)$('composer').requestSubmit();}};
$('composer').onsubmit=async e=>{
  e.preventDefault();if($('send').disabled)return;
  const sentSelection={...selection},text=$('message-text').value;
  sending=true;updateComposer();error(null);
  try {await api('/api/messages',{kind:sentSelection.kind,target:sentSelection.target,text});drafts.delete(keyFor(sentSelection));if(keyFor(selection)===keyFor(sentSelection)&&$('message-text').value===text){$('message-text').value='';await loadMessages();}}
  catch(e){error(e.message);}finally{sending=false;updateComposer();}
};
const stream=new EventSource('/api/events');
stream.addEventListener('state',e=>{const wasOnline=backendOnline;applyState(JSON.parse(e.data));if(selection&&!wasOnline)loadMessages();});
stream.addEventListener('radio',e=>{const event=JSON.parse(e.data);state.events.push(event);state.events=state.events.slice(-300);renderChart();if(!paused)renderEvents();});
stream.addEventListener('message',()=>{if(selection)loadMessages();});
stream.onerror=()=>{backendOnline=false;connectionError=true;updateConnection();error('Der Webserver ist nicht erreichbar. Die Verbindung wird automatisch wiederhergestellt.');};
setInterval(renderChart,2000);renderNav();renderChart();updateConnection();
