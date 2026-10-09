const $ = id => document.getElementById(id);
const state = { status: 'offline', channels: [], contacts: {}, events: [], stats: {}, info: {}, device: {}, last_hops: {} };
let activeTab = 'terminal', pausedEvents = [], scopeChannel = '';
let selection = null, paused = false, sending = false, backendOnline = false, historyVersion = 0;
let messageRows = [], olderAvailable = false, connectionError = false;
let scopeSaving = false, defaultScopeDirty = false, channelScopeDirty = false;
let channelSaving = false, contactSaving = false;
let pathHashSaving = false, pathHashDirty = false;
let multiAcksSaving = false, multiAcksDirty = false;
let roomSaving = false, discoveryBusy = false, repeaterBusy = false;
let traceBusy=false;
const traceDraft=[''];
const traceStatuses={sending:'Wird gesendet',waiting:'Warte auf Antwort',complete:'Vollständig',partial:'Messwerte fehlen',timeout:'Keine Antwort innerhalb der Wartezeit',interrupted:'Unterbrochen',unconfirmed:'Sendestatus unbestätigt'};
const drafts = new Map();
const encoder = new TextEncoder();
const statusNames = {offline:'Offline', connecting:'Verbinde …', connected:'Verbunden', reconnecting:'Neuverbindung …'};
const eventNames = {DISCOVER_RESPONSE:'DISCOVER-Antwort', DISCOVER_SENT:'DISCOVER gesendet', RX_LOG_DATA:'Funkpaket', RAW_DATA:'Rohdaten', ADVERTISEMENT:'Advertisement', NEW_CONTACT:'Neuer Kontakt', PATH_UPDATE:'Route aktualisiert', ACK:'Bestätigung', MESSAGE_SENT:'Nachricht gesendet', CHANNEL_MSG_RECV:'Channel-Nachricht', CONTACT_MSG_RECV:'Direktnachricht', CONNECTED:'Verbunden', CONNECTION_ERROR:'Verbindungsfehler', TRACE_DATA:'Route / Trace'};
function node(tag, className, text) { const el = document.createElement(tag); if(className) el.className=className; if(text!==undefined) el.textContent=text; return el; }
function error(message) { $('error').textContent=message || ''; $('error').hidden=!message; }
async function api(path, body) {
  const response=await fetch(path, body===undefined ? {} : {method:'POST',headers:{'Content-Type':'application/json','X-Meshcore-Client':'web'},body:JSON.stringify(body)});
  const result=await response.json();
  if(!response.ok) throw new Error(typeof result.detail==='string'?result.detail:'Die Anfrage konnte nicht verarbeitet werden.');
  return result;
}
function contactName(key) { const found=Object.entries(state.contacts).find(([k])=>k.startsWith(key)); return found?.[1]?.adv_name || key; }
function roomAuthor(key) {
  if(!key) return 'Absender unbekannt';
  const matches=Object.entries(state.contacts).filter(([k])=>k.startsWith(key));
  return matches.length===1 ? (matches[0][1].adv_name||key) : `Absender ${key}`;
}

function keyFor(s) {return s ? `${s.kind}:${s.target}` : '';}
const navigationStorageKey='meshcore-navigation-v1';
let navigation={sort:'abc',read:{},opened:{}};
try {
  const saved=JSON.parse(localStorage.getItem(navigationStorageKey));
  if(saved && typeof saved==='object') navigation={
    sort:saved.sort==='recent'?'recent':'abc',
    read:saved.read&&typeof saved.read==='object'?saved.read:{},
    opened:saved.opened&&typeof saved.opened==='object'?saved.opened:{}
  };
} catch {} // Storage may be unavailable; navigation still works for this session.
function saveNavigation() {try {localStorage.setItem(navigationStorageKey,JSON.stringify(navigation));} catch {}}
function navigationKey(s) {
  return JSON.stringify([state.host,state.port,s.kind,s.target,s.kind==='channel'?s.name:null]);
}
function conversationInfo(s) {
  const target=s.kind==='channel'?s.target:s.target.slice(0,12);
  return (state.conversations||[]).find(c=>c.kind===s.kind&&c.target===target)||{};
}
function sortConversations(items) {
  return items.sort((a,b)=>{
    if(navigation.sort==='recent') {
      const activity=s=>Math.max(Number(navigation.opened[navigationKey(s)])||0,(conversationInfo(s).last_activity||0)*1000);
      const difference=activity(b)-activity(a);
      if(difference)return difference;
    }
    return a.name.replace(/^#/,'').localeCompare(b.name.replace(/^#/,''),'de',{sensitivity:'base'})||a.target.localeCompare(b.target);
  });
}
function appendUnread(button,s) {
  if((conversationInfo(s).incoming_id||0)>(Number(navigation.read[navigationKey(s)])||0)) {
    const dot=node('span','unread-dot');dot.title='Ungelesene Nachrichten';dot.setAttribute('aria-hidden','true');
    button.append(dot,node('span','sr-only',' · Ungelesene Nachrichten'));
  }
}
function markVisibleRead() {
  const list=$('messages');
  if(activeTab!=='terminal'||!selection||document.visibilityState!=='visible'||list.scrollHeight-list.scrollTop-list.clientHeight>=120)return;
  const latest=Math.max(0,...messageRows.filter(m=>m.direction==='in').map(m=>m.id));
  const key=navigationKey(selection);
  if(latest>(Number(navigation.read[key])||0)) {navigation.read[key]=latest;saveNavigation();renderNav();}
}
$('conversation-sort').value=navigation.sort;
$('conversation-sort').onchange=()=>{navigation.sort=$('conversation-sort').value;saveNavigation();renderNav();};
$('messages').addEventListener('scroll',markVisibleRead);
document.addEventListener('visibilitychange',markVisibleRead);
function renderNav() {
  $('channel-count').textContent=state.channels.length;
  $('channels').replaceChildren();
  if(!state.channels.length) $('channels').append(node('p','nav-empty','Keine Channels geladen.'));
  for(const s of sortConversations(state.channels.map(channel=>({kind:'channel',target:String(channel.index),name:channel.name})))) {
    const b=node('button','nav-item'+(keyFor(selection)===keyFor(s)?' active':''));
    b.append(node('span','','#'),node('span','nav-name',s.name.replace(/^#/,''))); appendUnread(b,s); b.onclick=()=>openChat(s); $('channels').append(b);
  }
  const contacts=Object.entries(state.contacts).filter(([,c])=>c.type===1).sort((a,b)=>(a[1].adv_name||a[0]).localeCompare(b[1].adv_name||b[0]));
  $('contact-count').textContent=contacts.length;
  $('contacts').replaceChildren();
  const search=$('contact-search').value.toLowerCase();
  for(const s of sortConversations(contacts.map(([key,c])=>({kind:'dm',target:key,name:c.adv_name||key,unknown:!!c.unknown})))) {
    const {name,target:key}=s;
    if(!`${name} ${key}`.toLowerCase().includes(search)) continue;
    const b=node('button','nav-item'+(keyFor(selection)===keyFor(s)?' active':'')); b.title=key;
    b.append(node('span','contact-avatar',name.slice(0,2).toUpperCase()),node('span','nav-name',name)); appendUnread(b,s); b.onclick=()=>openChat(s); $('contacts').append(b);
  }
  if(!contacts.length) $('contacts').append(node('p','nav-empty','Chat-Kontakte erscheinen nach dem Verbinden.'));
  const rooms=Object.entries(state.contacts).filter(([,c])=>c.type===3).sort((a,b)=>(a[1].adv_name||a[0]).localeCompare(b[1].adv_name||b[0]));
  $('room-count').textContent=rooms.length;
  $('rooms').replaceChildren();
  for(const s of sortConversations(rooms.map(([key,c])=>({kind:'room',target:key,name:c.adv_name||key,unknown:!!c.unknown})))) {
    const b=node('button','nav-item'+(keyFor(selection)===keyFor(s)?' active':''));b.title=s.target;
    b.append(node('span','','▤'),node('span','nav-name',s.name));appendUnread(b,s);b.onclick=()=>openChat(s);$('rooms').append(b);
  }
  if(!rooms.length) $('rooms').append(node('p','nav-empty','Keine Roomserver im Companion gespeichert.'));

}
function applyState(data) {
  Object.assign(state,data); backendOnline=true;
  if(selection && ['dm','room'].includes(selection.kind) && !state.contacts[selection.target]) {
    rememberDraft();selection=null;historyVersion++;messageRows=[];
    showTab(activeTab);
  }
  if(selection?.kind==='channel'&&!state.channels.some(c=>String(c.index)===selection.target&&c.name===selection.name)) {
    drafts.delete(keyFor(selection));selection=null;historyVersion++;messageRows=[];
    showTab(activeTab);
  }
  $('endpoint').textContent=`${state.host}:${state.port}`;
  $('device-name').textContent=state.info.name||'Dein Companion';
  const info=[['Adresse',`${state.host}:${state.port}`],['Gerät',state.info.name||state.device.model||'—'],['Firmware',state.device.ver||'—'],['Frequenz',state.info.radio_freq ? `${state.info.radio_freq} MHz`:'—'],['Rauschpegel',state.stats.STATS_RADIO?.noise_floor!==undefined?`${state.stats.STATS_RADIO.noise_floor} dBm`:'—']];
  $('radio-info').replaceChildren(...info.map(([key,value])=>{const row=node('div');row.append(node('dt','',key),node('dd','',value));return row;}));
  updateConnection(); renderNav(); if(!paused) renderEvents();
  renderLastHops();
  renderScopes();
  renderPathHash();
  renderMultiAcks();
  renderChannelManager();
  if(state.error) {connectionError=true;error(`Verbindung: ${state.error} · Erneuter Versuch erfolgt automatisch.`);}
  else if(connectionError) {connectionError=false;error(null);}
}
function updateConnection() {
  const connected=backendOnline&&state.status==='connected';
  $('connection-status').textContent=backendOnline?statusNames[state.status]:'Server nicht erreichbar';
  $('device-dot').classList.toggle('online',connected);
  $('connect').hidden=connected;
  $('connect').disabled=!backendOnline||state.status!=='offline';
  $('connect').textContent=connected?'Verbunden':state.status==='offline'?'Verbinden':'Verbinde …';
  $('refresh').disabled=!connected;
  updateComposer();
  renderScopes();
  renderPathHash();
  renderMultiAcks();
  renderChannelManager();
  renderRoom();
  renderDiscoveries();
  renderTrace();
  renderContactManager();
  renderRepeater();
}
function renderDiscoveries() {
  const connected=backendOnline&&state.status==='connected';
  $('discover').disabled=!connected||discoveryBusy;
  $('discover').textContent=discoveryBusy?'Bitte warten …':'DISCOVER aussenden';
  const entries=Object.entries(state.discovered||{}).sort((a,b)=>b[1].last_seen-a[1].last_seen);
  $('discovered-count').textContent=entries.length;
  const search=$('discovered-search').value.toLowerCase();
  const list=$('discovered-list');
  const expanded=new Set([...list.querySelectorAll('details[open]')].map(el=>el.dataset.key));
  list.replaceChildren();
  for(const [key,entry] of entries) {
    const contact=entry.contact||{}, name=contact.adv_name||key.slice(0,16);
    if(!`${name} ${key}`.toLowerCase().includes(search))continue;
    const row=node('tr'), identity=node('td','device-identity');
    const keyText=node('small','device-key',key);
    keyText.title=key;
    const details=node('details');details.dataset.key=key;details.open=expanded.has(key);
    details.append(node('summary','',name),keyText);
    identity.append(details);
    const type={1:'Chat',2:'Repeater',3:'Roomserver',4:'Sensor'}[contact.type]||'Unbekannt';
    const seen=new Date(entry.last_seen*1000);
    const timestamp=node('td','device-seen',seen.toLocaleString('de-DE',{day:'2-digit',month:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit'}));
    timestamp.title=seen.toLocaleString('de-DE');
    row.append(identity,node('td','device-type',type),node('td','device-source',(entry.sources||[]).join(' + ')||'—'),timestamp);
    const discovery=entry.observations?.DISCOVER_RESPONSE;
    for(const [field,label] of [['SNR','Antwort bei dir'],['SNR_in','Anfrage beim Gerät']]) {
      const value=discovery?.payload?.[field];
      const signal=node('td','numeric',Number.isFinite(value)?value.toLocaleString('de-DE'):'—');
      const description=`${label}: ${Number.isFinite(value)?value.toLocaleString('de-DE')+' dB':'nicht verfügbar'}`;
      signal.setAttribute('aria-label',description);
      signal.title=description+(discovery?` · DISCOVER: ${new Date(discovery.time*1000).toLocaleString('de-DE')}`:'');
      row.append(signal);
    }
    const saved=state.contacts[key]&&!state.contacts[key].unknown;
    const complete=key.length===64&&[1,2,3,4].includes(contact.type);
    const action=node('td','device-action');
    if(saved||!complete) {
      action.append(node('span','contact-state',saved?'Gespeichert':'Unvollständig'));
      action.title=saved?'Im Kontaktbuch':'Warte auf vollständige Daten';
    } else {
      const button=node('button','table-action','Speichern');
      button.setAttribute('aria-label','Als Kontakt speichern');
      button.title=`${name} als Kontakt speichern`;
      button.disabled=!connected||discoveryBusy;
      button.onclick=()=>discoveryAction('/api/discovered/save',{target:key},'Kontakt im Companion gespeichert und bestätigt.');
      action.append(button);
    }
    row.append(action);list.append(row);
  }
  if(!list.children.length) {
    const row=node('tr'), cell=node('td','discovery-empty',entries.length?'Keine passenden Geräte.':'Noch keine Geräte empfangen. DISCOVER senden oder ADVERTs abwarten.');
    cell.colSpan=7;row.append(cell);list.append(row);
  }
}
async function discoveryAction(path, body, success) {
  if(discoveryBusy)return;
  discoveryBusy=true;renderDiscoveries();error(null);$('discovery-feedback').textContent='';
  try {applyState(await api(path,body));$('discovery-feedback').textContent=success;}
  catch(e){error(e.message);}
  finally{discoveryBusy=false;renderDiscoveries();}
}
$('discover').onclick=()=>discoveryAction('/api/discover',{},'DISCOVER gesendet. Empfangene Antworten erscheinen automatisch in der Liste.');
$('discovered-search').oninput=renderDiscoveries;

function renderRoom() {
  const visible=selection?.kind==='room';
  $('room-login-form').hidden=!visible;
  if(!visible)return;
  const room=state.rooms?.[selection.target]||{}, online=backendOnline&&state.status==='connected';
  const busy=roomSaving||room.status==='logging_in';
  $('room-status').textContent={logging_in:'Anmeldung läuft …',logged_in:room.can_post?'Anmeldung bestätigt · Schreiben erlaubt':'Anmeldung bestätigt · Nur lesen',failed:'Anmeldung fehlgeschlagen',disconnected:'Nicht angemeldet'}[room.status]||'Nicht angemeldet';
  $('room-password').disabled=!online||busy||selection.unknown;
  $('room-login').disabled=!online||busy||room.pending_send||selection.unknown;
  $('room-logout').disabled=!online||roomSaving||selection.unknown||!['logged_in','logging_in'].includes(room.status);
  $('room-feedback').textContent=room.error||'';
}
$('room-login-form').onsubmit=async e=>{
  e.preventDefault();if($('room-login').disabled)return;
  const target=selection.target,password=$('room-password').value;
  $('room-password').value='';roomSaving=true;renderRoom();error(null);
  try{applyState(await api('/api/rooms/login',{target,password}));}
  catch(e){error(e.message);}finally{roomSaving=false;renderRoom();updateComposer();}
};
$('room-logout').onclick=async()=>{
  if($('room-logout').disabled)return;
  const target=selection.target;roomSaving=true;renderRoom();error(null);
  try{applyState(await api('/api/rooms/logout',{target}));}
  catch(e){error(e.message);}finally{roomSaving=false;renderRoom();updateComposer();}
};
function renderContactManager() {
  const online=backendOnline&&state.status==='connected';
  for(const id of ['new-contact-name','new-contact-key','new-contact-type','add-contact']) $(id).disabled=!online||contactSaving;
  const contacts=Object.entries(state.contacts).filter(([key,c])=>key.length===64&&!c.unknown)
    .sort((a,b)=>(a[1].adv_name||a[0]).localeCompare(b[1].adv_name||b[0]));
  $('managed-contact-count').textContent=contacts.length;
  const search=$('managed-contact-search').value.toLowerCase(), list=$('managed-contacts');
  list.replaceChildren();
  for(const [key,c] of contacts) {
    const name=c.adv_name||key.slice(0,16);
    if(!`${name} ${key}`.toLowerCase().includes(search))continue;
    const row=node('div','managed-channel'), details=node('span','discovery-details');
    details.append(node('strong','',name),node('small','',key),node('small','',{1:'Chat',2:'Repeater',3:'Roomserver',4:'Sensor'}[c.type]||'Typ unbekannt'));
    const remove=node('button','button compact','Löschen');
    remove.type='button';remove.disabled=!online||contactSaving;
    remove.setAttribute('aria-label',`${name} vom Companion löschen`);
    remove.onclick=()=>changeContact('/api/contacts/remove',{target:key});
    row.append(details,remove);list.append(row);
  }
  if(!list.children.length)list.append(node('p','nav-empty',contacts.length?'Keine passenden Kontakte.':'Keine gespeicherten Kontakte vorhanden.'));
}
async function changeContact(path, body) {
  if(contactSaving)return;
  contactSaving=true;renderContactManager();$('contact-feedback').textContent='Companion wird aktualisiert …';
  try {
    applyState(await api(path,body));
    if(path==='/api/contacts')$('add-contact-form').reset();
    $('contact-feedback').textContent=path==='/api/contacts'?'Kontakt im Companion gespeichert und bestätigt.':'Kontakt gelöscht und bestätigt. Der Nachrichtenverlauf bleibt erhalten.';
  } catch(e) {$('contact-feedback').textContent=e.message;}
  finally {contactSaving=false;renderContactManager();}
}
$('managed-contact-search').oninput=renderContactManager;
$('add-contact-form').onsubmit=e=>{
  e.preventDefault();if($('add-contact').disabled)return;
  const name=$('new-contact-name').value.trim();
  if(!name||encoder.encode(name).length>31){$('contact-feedback').textContent='Der Name muss 1–31 UTF-8-Bytes lang sein.';return;}
  changeContact('/api/contacts',{target:$('new-contact-key').value.trim(),name,type:Number($('new-contact-type').value)});
};

function renderChannelManager() {
  const online=backendOnline&&state.status==='connected';
  $('channel-capacity').textContent=`${state.channels.length} / ${state.device.max_channels??'—'} Plätze belegt`;
  $('new-channel-name').disabled=!online||channelSaving;
  $('add-channel').disabled=!online||channelSaving;
  $('managed-channels').replaceChildren(...state.channels.filter(c=>c.name.startsWith('#')).map(c=>{
    const row=node('div','managed-channel');
    const remove=node('button','button compact','Vom Companion entfernen');
    remove.type='button';remove.disabled=!online||channelSaving;
    remove.setAttribute('aria-label',`${c.name} vom Companion entfernen`);
    remove.onclick=()=>changeChannel('/api/channels/remove',{index:c.index,name:c.name});
    row.append(node('span','',c.name),remove);return row;
  }));
}
async function changeChannel(path, body) {
  channelSaving=true;renderChannelManager();updateComposer();$('channel-feedback').textContent='Companion wird aktualisiert …';
  try{
    applyState(await api(path,body));
    if(path==='/api/channels')$('new-channel-name').value='';
    $('channel-feedback').textContent=path==='/api/channels'?'Channel im Companion gespeichert.':'Channel vom Companion entfernt. Der bisherige Verlauf bleibt lokal archiviert.';
  }catch(e){$('channel-feedback').textContent=e.message;}
  finally{channelSaving=false;renderChannelManager();updateComposer();}
}
$('add-channel-form').onsubmit=e=>{e.preventDefault();if(!channelSaving)changeChannel('/api/channels',{name:$('new-channel-name').value});};
function renderMultiAcks() {
  const value=state.info.multi_acks;
  const supported=(state.device['fw ver']||0)>=7&&(value===0||value===1);
  $('multi-acks-current').textContent=supported?(value===1?'Aktiv':'Aus'):'Nicht verfügbar';
  if(!multiAcksDirty) $('multi-acks-enabled').checked=supported&&value===1;
  for(const id of ['multi-acks-enabled','multi-acks-save']) $(id).disabled=!backendOnline||state.status!=='connected'||!supported||multiAcksSaving;
}
$('multi-acks-enabled').onchange=()=>{multiAcksDirty=true;$('multi-acks-feedback').textContent='';};
$('multi-acks-form').onsubmit=async e=>{
  e.preventDefault();if($('multi-acks-save').disabled)return;
  multiAcksSaving=true;renderMultiAcks();$('multi-acks-feedback').textContent='Wird gespeichert …';
  try{
    const result=await api('/api/multi-acks',{enabled:$('multi-acks-enabled').checked});
    multiAcksDirty=false;applyState(result);$('multi-acks-feedback').textContent='Im Companion gespeichert und bestätigt.';
  }catch(e){$('multi-acks-feedback').textContent=e.message;}
  finally{multiAcksSaving=false;renderMultiAcks();}
};
function renderPathHash() {
  const mode=state.device.path_hash_mode, supported=Number.isInteger(mode)&&mode>=0&&mode<=2;
  $('path-hash-current').textContent=supported?`${mode+1} Byte`:'Nicht verfügbar';
  if(!pathHashDirty) $('path-hash-bytes').value=supported?String(mode+1):'';
  for(const id of ['path-hash-bytes','path-hash-save']) $(id).disabled=!backendOnline||state.status!=='connected'||!supported||pathHashSaving;
}
$('path-hash-bytes').onchange=()=>{pathHashDirty=true;$('path-hash-feedback').textContent='';};
$('path-hash-form').onsubmit=async e=>{
  e.preventDefault();
  pathHashSaving=true;$('path-hash-feedback').textContent='Wird gespeichert …';renderPathHash();
  try {
    const result=await api('/api/path-hash',{bytes:Number($('path-hash-bytes').value)});
    pathHashDirty=false;applyState(result);$('path-hash-feedback').textContent='Im Companion gespeichert und bestätigt.';
  } catch(e) {$('path-hash-feedback').textContent=e.message;}
  finally {pathHashSaving=false;renderPathHash();}
};

function scopeLabel(scope) {return scope === '*' ? 'Ohne Scope' : scope || 'Companion-Standard';}
function renderScopes() {
  const scopes=state.scopes||{}, online=backendOnline&&state.status==='connected';
  $('default-scope-current').textContent=scopes.supported?(scopes.default||'Ohne Scope'):'Nicht verfügbar';
  if(!defaultScopeDirty) $('default-scope-input').value=scopes.default||'';
  for(const id of ['default-scope-input','default-scope-save','default-scope-clear']) $(id).disabled=!online||!scopes.supported||scopeSaving;
  const nextChannel=selection?.kind==='channel'?selection.target:'';
  if(scopeChannel!==nextChannel) {scopeChannel=nextChannel;channelScopeDirty=false;}
  const channel=scopeChannel!=='';
  $('channel-scope-form').hidden=!channel;
  if(channel){
    const scope=scopes.channels?.[scopeChannel]||'';
    $('channel-scope-current').textContent=`Aktiv: ${scopeLabel(scope)}`;
    if(!channelScopeDirty){$('channel-scope-mode').value=scope==='*'?'unscoped':scope?'region':'default';$('channel-scope-input').value=scope==='*'?'':scope;}
  }
  $('channel-scope-input').hidden=$('channel-scope-mode').value!=='region';
  $('channel-scope-input').required=$('channel-scope-mode').value==='region';
  for(const id of ['channel-scope-mode','channel-scope-input','channel-scope-save']) $(id).disabled=!online||!channel||scopeSaving||(state.device['fw ver']||0)<8;
  $('channel-scope-mode').querySelector('[value=unscoped]').disabled=(state.device['fw ver']||0)<12;
  updateComposer();
}
async function saveScope(channel, scope) {
  const feedback=channel===null?'default-scope-feedback':'channel-scope-feedback';
  scopeSaving=true;$(feedback).textContent='Wird gespeichert …';renderScopes();
  try{
    const result=await api('/api/scopes',{channel,scope});
    if(channel===null)defaultScopeDirty=false;
    else if(scopeChannel===channel)channelScopeDirty=false;
    applyState(result);if(channel===null||scopeChannel===channel)$(feedback).textContent='Gespeichert.';
  }catch(e){if(channel===null||scopeChannel===channel)$(feedback).textContent=e.message;}
  finally{scopeSaving=false;renderScopes();}
}
$('default-scope-input').oninput=()=>{defaultScopeDirty=true;$('default-scope-feedback').textContent='';};
$('default-scope-form').onsubmit=e=>{e.preventDefault();saveScope(null,$('default-scope-input').value);};
$('default-scope-clear').onclick=()=>saveScope(null,'');
$('channel-scope-mode').onchange=()=>{channelScopeDirty=true;$('channel-scope-feedback').textContent='';renderScopes();};
$('channel-scope-input').oninput=()=>{channelScopeDirty=true;$('channel-scope-feedback').textContent='';};
$('channel-scope-form').onsubmit=e=>{e.preventDefault();if(scopeChannel==='')return;const mode=$('channel-scope-mode').value;saveScope(scopeChannel,mode==='unscoped'?'*':mode==='region'?$('channel-scope-input').value:'');};
function renderLastHops() {
  const now=Date.now()/1000;
  const entries=Object.entries(state.last_hops).filter(([,entry])=>entry.time>now-300&&entry.time<=now);
  state.last_hops=Object.fromEntries(entries);
  entries.sort(([a,x],[b,y])=>{
    const xValid=Number.isFinite(x.snr), yValid=Number.isFinite(y.snr);
    return (yValid-xValid)||(xValid&&yValid?y.snr-x.snr:0)||a.localeCompare(b);
  });
  $('last-hops').replaceChildren(...entries.map(([hash,entry])=>{
    const row=node('tr');
    row.append(node('td','',hash),node('td','',Number.isFinite(entry.snr)?entry.snr:'—'),node('td','',new Date(entry.time*1000).toLocaleTimeString('de-DE')));
    return row;
  }));
  $('last-hops-empty').hidden=entries.length>0;
}
setInterval(renderLastHops,1000);

function renderEvents() {
  const filter=$('event-filter').value;
  const events=(paused?pausedEvents:state.events).filter(e=>e.type==='RX_LOG_DATA'&&(filter==='all'||String(e.payload?.payload_type)===filter));
  $('event-count').textContent=events.length;
  $('events-empty').hidden=events.length>0;
  $('events').replaceChildren(...events.slice().reverse().map(e=>{
    const p=e.payload||{}, row=node('tr');
    const description=describePacket(e);
    const values=[new Date(e.time*1000).toLocaleTimeString('de-DE'), description.type, packetPreview(e), packetLastHop(e), p.rssi??p.RSSI??'—', p.snr??p.SNR??'—'];
    values.forEach((value,index)=>{
      const cell=node('td',index===2?'event-description':'',index===0?undefined:value);
      if(index===0){
        const button=node('button','packet-open',value);
        button.setAttribute('aria-label',`${value} · ${description.type} · Details ansehen`);
        button.type='button';button.onclick=()=>showPacket(e);cell.append(button);
      }else cell.title=index===3?(value==='—'?'Last Hop nicht bestimmbar':value==='Direkt'?'Direkt empfangen · kein Repeater im Pfad':`Letzter Hop im Empfangspfad: ${value}`):String(value);
      row.append(cell);
    });
    return row;
  }));
}

function rememberDraft() {if(selection) drafts.set(keyFor(selection),$('message-text').value);}
async function openChat(s) {
  rememberDraft();selection=s;historyVersion++;messageRows=[];olderAvailable=false;
  navigation.opened[navigationKey(s)]=Date.now();saveNavigation();
  $('channel-scope-feedback').textContent='';renderScopes();
  $('room-password').value='';renderRoom();
  showTab('terminal');
  $('chat-title').textContent=s.name;
  $('chat-icon').textContent=s.kind==='channel'?'#':s.name.slice(0,2).toUpperCase();
  $('chat-subtitle').textContent=s.kind==='channel'?'Channel · Nachrichten über dein Mesh':s.unknown?'Unbekannter Absender · zum Antworten zuerst im Companion speichern':(s.kind==='room'?'Roomserver':'Direktnachricht');
  $('message-text').value=drafts.get(keyFor(s))||'';
  $('messages').replaceChildren(node('p','nav-empty','Nachrichten werden geladen …'));
  renderNav();updateComposer();await loadMessages();
}
function messagePathLabel(message) {
  if(message.direction!=='in')return 'Pfad: nicht übermittelt';
  const reception=message.reception;
  if(reception?.routing==='direct')return 'Pfad: nicht übermittelt (Direct-Routing)';
  if(reception?.routing!=='flood'||!Number.isInteger(reception.hops)||reception.hops<0||reception.hops>63)return 'Pfad: nicht verfügbar';
  if(reception.hops===0)return 'Pfad: direkt empfangen';
  const path=reception.path;
  if(Array.isArray(path)&&path.length===reception.hops&&path.every(hash=>typeof hash==='string'&&/^(?:[0-9a-f]{2}){1,3}$/i.test(hash))) {
    return `Pfad: ${path.map(hash=>hopName(hash.toLowerCase())).join(' → ')} → Du`;
  }
  return `Pfad: ${reception.hops} ${reception.hops===1?'Hop':'Hops'} · Knotenfolge nicht übermittelt`;
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
    if(!messageRows.length){const empty=node('div','empty-state');empty.append(node('p','','Noch keine Nachrichten.'));fragment.append(empty);}
    for(const message of messageRows) {
      const wrap=node('article',`message ${message.direction}`);
      const labels={received:'Empfangen',sent:selection.kind==='dm'?'An Companion übergeben · unbestätigt':'An Companion übergeben',delivered:'Zugestellt ✓',sending:'Warte auf Bestätigung',unconfirmed:'Keine Zustellbestätigung · Versuche ausgeschöpft',interrupted:'Unbestätigt · Sendeversuche abgebrochen'};
      if(selection.kind==='room')labels.delivered='Vom Roomserver angenommen ✓';
      if(message.direction==='out'&&selection.kind==='channel'&&message.repeater_count>0) labels.sent=`von ${message.repeater_count} ${message.repeater_count===1?'Repeater':'Repeatern'} empfangen`;
      const sender=message.direction==='out'?'Du':selection.kind==='room'?roomAuthor(message.sender_key):selection.kind==='dm'?contactName(message.target):selection.name;
      wrap.append(node('div','bubble',message.text));
      const meta=node('div','message-meta',`${sender} · ${new Date(message.timestamp*1000).toLocaleString('de-DE',{dateStyle:'short',timeStyle:'short'})} · ${labels[message.status]||message.status}`);
      if(message.repeater_count>0) meta.title='Anhand zurückgehörter Weiterleitungen: unterschiedliche letzte Hop-Hashes, keine vollständige Empfangsbestätigung. Hash-Kollisionen können die Anzahl verringern.';
      wrap.append(meta,node('div','message-path',messagePathLabel(message)));fragment.append(wrap);
    }
    list.append(fragment);
    if(before) list.scrollTop=oldTop+list.scrollHeight-oldHeight;
    else list.scrollTop=nearBottom||firstLoad?list.scrollHeight:oldTop;
    markVisibleRead();
  } catch(e){error(e.message);}
}
function updateComposer() {
  const length=encoder.encode($('message-text').value.trim()).length;
  const limit=selection?.kind==='room'?150:selection?.kind==='channel'&&state.info.name?Math.max(0,160-encoder.encode(`${state.info.name}: `).length):160;
  $('message-counter').textContent=`${length} / ${limit} Bytes`;
  $('message-counter').classList.toggle('danger',length>limit);
  const roomBlocked=selection?.kind==='room'&&(!state.rooms?.[selection.target]?.can_post||state.rooms?.[selection.target]?.pending_send||roomSaving);
  $('send').disabled=sending||scopeSaving||channelSaving||roomBlocked||!backendOnline||state.status!=='connected'||!selection||selection.unknown||length===0||length>limit;
  $('send').textContent=sending?'Wird gesendet …':'Nachricht senden ↗';
}
$('contact-search').oninput=renderNav;
$('event-filter').onchange=renderEvents;
$('pause').onclick=()=>{paused=!paused;if(paused)pausedEvents=state.events.slice();$('pause').textContent=paused?'Fortsetzen':'Pausieren';if(!paused)renderEvents();};
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
stream.addEventListener('radio',e=>{
  const event=JSON.parse(e.data), hash=packetLastHop(event), p=event.payload||{};
  if(/^(?:[0-9a-f]{2}){1,3}$/.test(hash)&&Number.isFinite(event.time)&&(!state.last_hops[hash]||event.time>=state.last_hops[hash].time)) {
    state.last_hops[hash]={time:event.time,snr:p.snr??p.SNR};
  }
  renderLastHops();
  state.events.push(event);state.events=state.events.slice(-300);if(!paused)renderEvents();
});
stream.addEventListener('message',e=>{
  const data=JSON.parse(e.data||'{}');
  if(data.conversations)state.conversations=data.conversations;
  renderNav();if(selection)loadMessages();
});
stream.onerror=()=>{backendOnline=false;connectionError=true;updateConnection();error('Der Webserver ist nicht erreichbar. Die Verbindung wird automatisch wiederhergestellt.');};
showTab('terminal');renderNav();updateConnection();

function renderCliReference() {
  if(!$('cli-reference').open)return;
  const role=state.contacts[$('repeater-target').value]?.type;
  const query=$('cli-command-search').value.trim().toLocaleLowerCase('de');
  const list=$('cli-command-list'), filter=JSON.stringify([role,query]);
  if(list.dataset.filter===filter)return;
  list.dataset.filter=filter;
  const entries=meshcoreCliCommands.filter(entry=>
    (!role||!entry.role||entry.role===role)&&
    `${entry.commands.map(command=>command.syntax).join(' ')} ${entry.description}`.toLocaleLowerCase('de').includes(query));
  list.replaceChildren(...entries.map(entry=>{
    const row=node('tr'), syntax=node('td'), description=node('td','',entry.description);
    for(const command of entry.commands) {
      const line=node('div');
      line.append(node('code','',command.syntax));
      if(command.serial)line.append(node('span','cli-command-note','Nur Serial'));
      syntax.append(line);
    }
    if(entry.role)description.append(node('span','cli-command-note',entry.role===2?'Nur Repeater':'Nur Roomserver'));
    row.append(syntax,description);return row;
  }));
  $('cli-command-empty').hidden=entries.length>0;
}
$('cli-reference').ontoggle=renderCliReference;
$('cli-command-search').oninput=renderCliReference;

function renderRepeater() {
  const select=$('repeater-target'), previous=select.value;
  const entries=Object.entries(state.contacts).filter(([,c])=>[2,3].includes(c.type)&&!c.unknown)
    .sort(([keyA,a],[keyB,b])=>(a.adv_name||keyA).localeCompare(b.adv_name||keyB,'de',{sensitivity:'base'})||keyA.localeCompare(keyB));
  const choices=JSON.stringify(entries.map(([key,c])=>[key,c.adv_name||key]));
  // Replacing native options dismisses an open dropdown, even with identical data.
  if(select.dataset.choices!==choices) {
    select.replaceChildren(...entries.map(([key,c])=>{const option=node('option','',c.adv_name||key);option.value=key;return option;}));
    if(!entries.length)select.append(node('option','','Keine gespeicherten Repeater / Roomserver'));
    if(entries.some(([key])=>key===previous))select.value=previous;
    select.dataset.choices=choices;
    if(previous!==select.value) {$('repeater-password').value='';$('repeater-command').value='';$('repeater-feedback').textContent='';}
  }
  renderCliReference();
  const session=state.repeaters?.[select.value], status=session?.status||'disconnected';
  const blocked=!backendOnline||state.status!=='connected'||!entries.length||repeaterBusy||status==='logging_in';
  select.disabled=repeaterBusy;
  $('repeater-password').disabled=blocked;
  $('repeater-login').disabled=blocked;
  $('repeater-logout').disabled=blocked||status!=='logged_in';
  $('repeater-command').disabled=blocked||status!=='logged_in';
  $('repeater-send').disabled=blocked||status!=='logged_in'||!$('repeater-command').value.trim()||encoder.encode($('repeater-command').value).length>160;
  $('repeater-status').textContent={disconnected:'Nicht angemeldet',logging_in:'Anmeldung läuft …',logged_in:'Angemeldet',failed:'Anmeldung fehlgeschlagen',timeout:'Keine Anmeldebestätigung'}[status]||status;
  const transcript=(session?.replies||[]).map(reply=>`${new Date(reply.time*1000).toLocaleTimeString('de-DE')}  ${reply.text}`).join('\n');
  const output=$('repeater-replies');
  if(output.textContent!==(transcript||'Noch keine CLI-Antwort empfangen.')) {output.textContent=transcript||'Noch keine CLI-Antwort empfangen.';output.scrollTop=output.scrollHeight;}
}
async function repeaterAction(action) {
  const target=$('repeater-target').value;
  const value=action==='login'?$('repeater-password').value:action==='command'?$('repeater-command').value:'';
  $('repeater-password').value='';repeaterBusy=true;renderRepeater();$('repeater-feedback').textContent='';
  try {
    applyState(await api('/api/repeaters',{target,action,value}));
    if(action==='command') {$('repeater-command').value='';$('repeater-feedback').textContent='An Companion übergeben · Ausführung noch nicht bestätigt. Antworten erscheinen unten.';}
  } catch(e) {$('repeater-feedback').textContent=e.message;}
  finally {repeaterBusy=false;renderRepeater();}
}
$('repeater-target').onchange=()=>{$('repeater-password').value='';$('repeater-command').value='';$('repeater-feedback').textContent='';renderRepeater();};
$('repeater-command').oninput=renderRepeater;
$('repeater-login-form').onsubmit=e=>{e.preventDefault();if(!$('repeater-login').disabled)repeaterAction('login');};
$('repeater-command-form').onsubmit=e=>{e.preventDefault();if(!$('repeater-send').disabled)repeaterAction('command');};
$('repeater-logout').onclick=()=>repeaterAction('logout');

function showTab(tab) {
  activeTab=tab;
  for(const name of ['terminal','monitor','device','mesh']) {
    const selected=name===tab, button=$(name+'-nav');
    $(name).hidden=!selected;
    button.setAttribute('aria-selected',String(selected));
    button.tabIndex=selected?0:-1;
  }
  $('chat').hidden=!selection;
  $('terminal-empty').hidden=!!selection;
  if(tab==='terminal')requestAnimationFrame(markVisibleRead);
}
for(const [index,tab] of ['terminal','monitor','device','mesh'].entries()) {
  const button=$(tab+'-nav');
  button.onclick=()=>showTab(tab);
  button.onkeydown=e=>{
    const tabs=['terminal','monitor','device','mesh'];
    let next;
    if(e.key==='ArrowRight')next=(index+1)%4;
    if(e.key==='ArrowLeft')next=(index+3)%4;
    if(e.key==='Home')next=0;
    if(e.key==='End')next=3;
    if(next!==undefined){e.preventDefault();showTab(tabs[next]);$(tabs[next]+'-nav').focus();}
  };
}

const deviceTabs=['device-cli','channel-manager','contact-manager','device-companion','companion-settings'];
function showDeviceTab(tab) {
  for(const name of deviceTabs) {
    const selected=name===tab, button=$(name+'-nav');
    $(name).hidden=!selected;
    button.setAttribute('aria-selected',String(selected));
    button.tabIndex=selected?0:-1;
  }
}
for(const [index,tab] of deviceTabs.entries()) {
  const button=$(tab+'-nav');
  button.onclick=()=>showDeviceTab(tab);
  button.onkeydown=e=>{
    let next;
    if(e.key==='ArrowRight')next=(index+1)%deviceTabs.length;
    if(e.key==='ArrowLeft')next=(index+deviceTabs.length-1)%deviceTabs.length;
    if(e.key==='Home')next=0;
    if(e.key==='End')next=deviceTabs.length-1;
    if(next!==undefined){e.preventDefault();showDeviceTab(deviceTabs[next]);$(deviceTabs[next]+'-nav').focus();}
  };
}

const monitorTabs=['monitor-live','monitor-hops'];
function showMonitorTab(tab) {
  for(const name of monitorTabs) {
    const selected=name===tab, button=$(name+'-nav');
    $(name).hidden=!selected;
    button.setAttribute('aria-selected',String(selected));
    button.tabIndex=selected?0:-1;
  }
  renderLastHops();
}
for(const [index,tab] of monitorTabs.entries()) {
  const button=$(tab+'-nav');
  button.onclick=()=>showMonitorTab(tab);
  button.onkeydown=e=>{
    let next;
    if(e.key==='ArrowRight'||e.key==='ArrowLeft')next=1-index;
    if(e.key==='Home')next=0;
    if(e.key==='End')next=1;
    if(next!==undefined){e.preventDefault();showMonitorTab(monitorTabs[next]);$(monitorTabs[next]+'-nav').focus();}
  };
}

// Editing never transmits. Only the form submit makes one TRACE request.

function tracePath() {
  const size=Number($('trace-size').value);
  let path=traceDraft.map(value=>{
    value=value.trim().toLowerCase();
    return value.length===64&&state.contacts[value]?.type===2?value.slice(0,size*2):value;
  });
  if($('trace-return').checked)path=path.concat(path.slice(0,-1).reverse());
  return path;
}
function renderTraceEditor() {
  const list=$('trace-hops');list.replaceChildren();
  traceDraft.forEach((value,index)=>{
    const row=node('li'), input=node('input');
    input.value=value;input.setAttribute('list','trace-contacts');input.setAttribute('aria-label',`Hop ${index+1}: Repeater-ID oder Kontakt`);
    input.placeholder='Repeater-ID oder Kontakt';input.autocomplete='off';input.maxLength=64;
    input.oninput=()=>{traceDraft[index]=input.value;renderTraceValidation();};
    row.append(input);
    for(const [label,delta] of [['↑',-1],['↓',1],['Entfernen',0]]) {
      const button=node('button','button compact',label);button.type='button';
      button.setAttribute('aria-label',delta?`Hop ${index+1} nach ${delta<0?'oben':'unten'}`:`Hop ${index+1} entfernen`);
      button.disabled=delta!==0&&(index+delta<0||index+delta>=traceDraft.length);
      button.onclick=()=>{
        if(delta)[traceDraft[index],traceDraft[index+delta]]=[traceDraft[index+delta],traceDraft[index]];
        else traceDraft.splice(index,1);
        renderTraceEditor();
      };
      row.append(button);
    }
    list.append(row);
  });
  renderTraceValidation();
}
function renderTraceValidation() {
  const size=Number($('trace-size').value), path=tracePath();
  const max=state.trace_limits?.[size]||({1:63,2:53,4:31,8:17})[size];
  const valid=path.length>0&&path.length<=max&&path.every(id=>new RegExp(`^[0-9a-f]{${size*2}}$`).test(id));
  $('trace-validation').textContent=valid?`${path.length} / ${max} Hops`:`1–${max} Hops; jede ID benötigt ${size*2} Hexzeichen.`;
  $('trace-preview').textContent=['Companion',...path.map(id=>id||'?'),'Companion'].join(' → ');
  $('trace-send').disabled=traceBusy||!backendOnline||state.status!=='connected'||!valid;
  $('trace-add').disabled=traceDraft.length>=63;
}
function renderTrace() {
  const options=$('trace-contacts');
  const entries=Object.entries(state.contacts||{}).filter(([,contact])=>contact.type===2&&!contact.unknown)
    .map(([key,contact])=>[key,contact.adv_name||key]);
  const choices=JSON.stringify(entries);
  if(options.dataset.choices!==choices) {
    options.replaceChildren(...entries.map(([key,label])=>{
      const option=node('option');option.value=key;option.label=label;return option;
    }));
    options.dataset.choices=choices;
  }
  renderTraceValidation();
  const results=$('trace-results');
  const opened=new Set([...results.querySelectorAll('details[open]')].map(el=>el.dataset.id));
  results.replaceChildren();
  for(const measurement of state.traces||[]) {
    const details=node('details');details.dataset.id=measurement.id;details.open=opened.has(measurement.id);
    details.append(node('summary','',`${new Date(measurement.timestamp*1000).toLocaleString('de-DE')} · ${traceStatuses[measurement.status]||measurement.status} · ${measurement.id.slice(0,8)}`));
    details.append(node('p','trace-path',['Companion',...measurement.path,'Companion'].join(' → ')));
    if(measurement.error)details.append(node('p','',measurement.error));
    const table=node('table'), head=node('thead'), header=node('tr'), body=node('tbody');
    for(const title of ['Hop','Empfänger','SNR (dB)'])header.append(node('th','',title));
    head.append(header);table.append(head,body);
    [...measurement.path,'Companion'].forEach((id,index)=>{
      const row=node('tr'), snr=measurement.snrs[index];
      row.append(node('td','',index+1),node('td','',measurement.names[index]?`${measurement.names[index]} (${id})`:id),node('td',snr==null?'trace-missing':'',snr==null?'Fehlt':snr.toLocaleString('de-DE')));
      body.append(row);
    });
    details.append(table);results.append(details);
  }
}
$('trace-add').onclick=()=>{traceDraft.push('');renderTraceEditor();document.querySelector('#trace-hops li:last-child input').focus();};
$('trace-size').onchange=renderTraceValidation;
$('trace-return').onchange=renderTraceValidation;
$('trace-form').onsubmit=async event=>{
  event.preventDefault();if($('trace-send').disabled||traceBusy)return;
  const body={path:[...traceDraft],hash_size:Number($('trace-size').value),return_path:$('trace-return').checked};
  traceBusy=true;renderTraceValidation();$('trace-feedback').textContent='';
  try {applyState(await api('/api/trace',body));$('trace-feedback').textContent='TRACE einmal gesendet.';}
  catch(exc){$('trace-feedback').textContent=exc.message;}
  finally{traceBusy=false;renderTraceValidation();}
};
renderTraceEditor();

const meshTabs=['mesh-discovery','manual-trace'];
function showMeshTab(tab) {
  for(const name of meshTabs) {
    const selected=name===tab, button=$(name+'-nav');
    $(name).hidden=!selected;
    button.setAttribute('aria-selected',String(selected));
    button.tabIndex=selected?0:-1;
  }
}
for(const [index,tab] of meshTabs.entries()) {
  const button=$(tab+'-nav');
  button.onclick=()=>showMeshTab(tab);
  button.onkeydown=event=>{
    let next;
    if(event.key==='ArrowRight'||event.key==='ArrowLeft')next=1-index;
    if(event.key==='Home')next=0;
    if(event.key==='End')next=1;
    if(next!==undefined){event.preventDefault();showMeshTab(meshTabs[next]);$(meshTabs[next]+'-nav').focus();}
  };
}
