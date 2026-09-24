// The SDK decodes RX_LOG_DATA on the server. RAW_DATA is opaque application data.
const packetTypes = ['Anfrage', 'Antwort', 'Direktnachricht', 'Bestätigung', 'Knoten-Ankündigung', 'Channel-Nachricht', 'Channel-Daten', 'Anonyme Anfrage', 'Pfad-Antwort', 'Routenverfolgung', 'Mehrteiliges Paket', 'Steuerpaket'];
const packetRoutes = ['Flood mit Transportcode', 'Flood · Verteilung im Mesh', 'Direct · vorgegebene Route', 'Direct mit Transportcode'];

function hopName(hash) {
  const matches=Object.entries(state.contacts).filter(([key])=>key.toLowerCase().startsWith(hash.toLowerCase()));
  return matches.length===1 && matches[0][1].adv_name ? `${matches[0][1].adv_name} (${hash})` : hash;
}

function receivedScopeLabel(p, radio) {
  if(!radio)return 'Nicht übermittelt · nur im Funkpaket verfügbar';
  const scope=p.received_scope;
  if(scope?.status==='unscoped'||[1,2].includes(p.route_type))return 'Ohne Scope';
  if(scope?.status==='scoped'){
    const names=scope.candidates||[];
    if(names.length===1)return `${names[0]} (Code stimmt überein)`;
    if(names.length>1)return `Mehrdeutig: ${names.join(', ')}`;
    return 'Scope vorhanden · Name unbekannt';
  }
  return 'Nicht bestimmbar';
}

function describePacket(event) {
  const p=event.payload||{}, fields=[];
  const add=(label,value)=>{if(value!==undefined&&value!==null&&value!=='')fields.push([label,String(value)]);};
  const radio=event.type==='RX_LOG_DATA';
  const type=radio ? packetTypes[p.payload_type]||'Unbekanntes Funkpaket' : eventNames[event.type]||event.type;
  const message=typeof p.message==='string'?p.message:typeof p.text==='string'?p.text:null;
  add('Empfangen',new Date(event.time*1000).toLocaleString('de-DE'));
  add('Pakettyp',type);
  if(radio)add('Routing',packetRoutes[p.route_type]||'Nicht verfügbar');
  const channelMessage=(radio&&p.payload_type===5)||event.type==='CHANNEL_MSG_RECV';
  const scopeLabel=channelMessage?receivedScopeLabel(p,radio):null;
  if(channelMessage){
    add('Scope des Absenders',scopeLabel);
    add('Scope-Transportcode',p.received_scope?.code);
    if(p.received_scope?.candidates?.length)add('Scope-Zuordnung','Abgleich mit bekannten Scope-Namen; der 16-Bit-Code kann mehrdeutig sein.');
  }
  const name=p.adv_name||p.chan_name;
  add(p.adv_name?'Knotenname':'Channel',name);
  add('Channel-Kennung',p.chan_hash);
  const key=p.adv_key||p.public_key||p.pubkey_prefix;
  if(typeof key==='string'&&key)add('Knoten',hopName(key));
  if(p.channel_idx!==undefined)add('Channel',state.channels.find(c=>c.index===p.channel_idx)?.name||`Slot ${p.channel_idx}`);
  if(p.adv_type!==undefined)add('Gerätetyp',['Unbekannt','Companion / Chat','Repeater','Room-Server','Sensor'][p.adv_type]||`Typ ${p.adv_type}`);
  if(Number.isFinite(p.adv_lat)&&Number.isFinite(p.adv_lon))add('Position',`${p.adv_lat}, ${p.adv_lon}`);
  if(radio&&Number.isInteger(p.path_len)&&p.path_len>=0&&p.path_len<=63){
    const flood=p.route_type===0||p.route_type===1;
    add(flood?'Bisherige Hops':'Pfadeinträge',p.path_len);
    if(p.path_len===0)add('Pfad',flood?'Direkt empfangen · kein Repeater im Pfad':'Keine Pfadeinträge übermittelt');
    else if(p.payload_type===9)add('Pfad','Trace-Pfad · siehe technische Rohdaten');
    else if([1,2,3].includes(p.path_hash_size)&&typeof p.path==='string'&&p.path.length===p.path_len*p.path_hash_size*2&&/^[0-9a-f]+$/i.test(p.path)){
      const hops=p.path.match(new RegExp(`.{${p.path_hash_size*2}}`,'g')).map(hopName);
      add(flood?'Empfangspfad':'Routing-Pfad',hops.join(' → '));
    }else add('Pfad','Knotenfolge nicht verfügbar');
  }
  const rssi=p.rssi??p.RSSI, snr=p.snr??p.SNR;
  if(rssi!==undefined)add('Signalstärke (RSSI)',`${rssi} dBm`);
  if(snr!==undefined)add('Signal / Rauschen (SNR)',`${snr} dB`);
  if(p.payload_length!==undefined)add('Paketgröße',`${p.payload_length} Bytes`);
  if(p.sender_timestamp!==undefined)add('Sendezeit',new Date(p.sender_timestamp*1000).toLocaleString('de-DE'));
  add('Bestätigungscode',p.code);
  let content;
  if(message!==null)content=message;
  else if(radio&&[0,1,2,5,6,7,8].includes(p.payload_type))content='Inhalt verschlüsselt · kein Klartext verfügbar';
  else if(radio&&p.payload_type===4)content=p.adv_name?`${p.adv_name} meldet sich im Mesh`:'Knoten meldet sich im Mesh';
  else if(radio&&p.payload_type===3)content='Empfangsbestätigung für eine Nachricht';
  else if(event.type==='RAW_DATA')content='Binäre Anwendungsdaten · kein Klartext verfügbar';
  else content=p.address||name||key||'Details und Rohdaten verfügbar';
  const parts=[name,radio?packetRoutes[p.route_type]:null,scopeLabel?`Scope: ${scopeLabel}`:null,content].filter(Boolean);
  return {type,summary:parts.join(' · '),fields,message};
}

function showPacket(event) {
  const description=describePacket(event);
  $('packet-title').textContent=description.type;
  $('packet-summary').textContent=description.message===null?description.summary:'Nachrichteninhalt als Klartext verfügbar.';
  $('packet-fields').replaceChildren(...description.fields.map(([label,value])=>{
    const row=node('div');row.append(node('dt','',label),node('dd','',value));return row;
  }));
  $('packet-message').hidden=description.message===null;
  $('packet-message').textContent=description.message||'';
  $('packet-raw').textContent=JSON.stringify(event.payload,null,2);
  $('packet-dialog').querySelector('details').open=false;
  $('packet-dialog').showModal();
}
