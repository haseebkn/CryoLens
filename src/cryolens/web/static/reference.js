'use strict';
document.addEventListener('DOMContentLoaded', async () => {
  const $ = id => document.getElementById(id);
  let packet;
  try { const response = await fetch('tasks.json'); if (!response.ok) throw new Error('Packet unavailable'); packet = await response.json(); }
  catch { $('progress').textContent = 'Review packet unavailable. Open the verified local review server.'; return; }
  const key = `cryolens-reference:${packet.workspace_id}:${packet.phase}:${packet.queue}`;
  const draftKey = `${key}:drafts:v2`, unreadable = new Set();
  function readStore(name) {
    try {
      const value = JSON.parse(localStorage.getItem(name) || '{}');
      if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error();
      return value;
    } catch {
      unreadable.add(name);
      $('message').textContent = 'Existing browser storage could not be read and will not be overwritten. Export this session before leaving.';
      return {};
    }
  }
  function writeStore(name, value) {
    if (unreadable.has(name)) throw new Error('Existing storage is unreadable');
    localStorage.setItem(name, JSON.stringify(value));
  }
  let saved = readStore(key), drafts = readStore(draftKey), restoring = false, storageFailed = false;
  let savedBaseline = structuredClone(saved);
  let index = 0, marks = [], regions = [], position = null, regionStart = null, image = null, imageRequest = 0, elapsed = 0, lastTick = performance.now(), viewedChannels = new Set(), active = !document.hidden;
  const tick = () => { const now = performance.now(); if (active) elapsed += (now - lastTick) / 1000; lastTick = now; };
  document.addEventListener('visibilitychange', () => { tick(); active = !document.hidden; if (document.hidden) persistDraft(); });
  const task = () => packet.tasks[index];
  const fields = ['reviewer', 'label', 'identity', 'rationale', 'support', 'status', 'notes'];
  function persistDraft() {
    if (restoring || !task()) return true;
    tick();
    const draft = {
      marks: structuredClone(marks), excluded_regions: structuredClone(regions),
      position: position ? [...position] : null, elapsed,
      viewed_channels: [...viewedChannels],
      fields: Object.fromEntries(fields.map(id => [id, $(id).value])),
    };
    drafts[task().task_id] = draft;
    try {
      drafts = { ...readStore(draftKey), [task().task_id]: draft };
      writeStore(draftKey, drafts); storageFailed = false; return true;
    }
    catch {
      storageFailed = true;
      $('message').textContent = 'Browser storage unavailable. Stay on this task; Save and export this session before leaving. Unsaved drafts are not submissions.';
      return false;
    }
  }
  $('review-form').addEventListener('input', event => {
    if (task().mode === 'candidate' && ['label', 'identity', 'rationale', 'support'].includes(event.target.id)) {
      position = [...task().centre]; $('position').value = position.join(', ');
    }
    persistDraft();
  });
  window.addEventListener('beforeunload', event => {
    if (!persistDraft() || storageFailed) { event.preventDefault(); event.returnValue = ''; }
  });
  $('queue').textContent = packet.queue;
  $('boundary').textContent = packet.identity_claim_boundary;
  if (packet.assignment) { $('reviewer').value = packet.assignment; $('reviewer').readOnly = true; }
  function inside(row, col) { const [r0,c0,r1,c1] = task().core; return row >= r0 && row < r1 && col >= c0 && col < c1; }
  function point(event) { const rect = $('image').getBoundingClientRect(); return [Math.floor((event.clientY - rect.top) * $('image').height / rect.height), Math.floor((event.clientX - rect.left) * $('image').width / rect.width)]; }
  function draw() {
    if (!image) return;
    const canvas = $('image'), context = canvas.getContext('2d');
    canvas.width = task().shape[1]; canvas.height = task().shape[0];
    canvas.style.width = `${canvas.width * Number($('zoom').value)}px`; canvas.style.height = `${canvas.height * Number($('zoom').value)}px`;
    context.drawImage(image, 0, 0);
    const [r0,c0,r1,c1] = task().core;
    context.strokeStyle = '#62c8ff'; context.lineWidth = 1; context.strokeRect(c0+.5,r0+.5,c1-c0-1,r1-r0-1);
    for (const [a,b,z,d] of regions) { context.fillStyle = 'rgba(255,174,74,.22)'; context.fillRect(b,a,d-b,z-a); context.strokeStyle = '#ffae4a'; context.strokeRect(b,a,d-b,z-a); }
    const dots = [...marks]; if (position) dots.push({row:position[0],col:position[1],label:'pending'});
    for (const mark of dots) { context.strokeStyle = mark.label === 'target' ? '#72ed99' : mark.label === 'uncertain' ? '#ffae4a' : '#ff6971'; context.beginPath(); context.moveTo(mark.col-3,mark.row); context.lineTo(mark.col+3,mark.row); context.moveTo(mark.col,mark.row-3); context.lineTo(mark.col,mark.row+3); context.stroke(); }
    if (task().mode === 'candidate') { const [row,col] = task().centre; context.strokeStyle = '#ffffff'; context.strokeRect(col-5,row-5,10,10); }
  }
  function list() {
    $('marks').replaceChildren(); $('regions').replaceChildren();
    for (const [i,mark] of marks.entries()) { const li = document.createElement('li'); li.textContent = `${mark.label}; identity ${mark.identity}; sample ${mark.row}, ${mark.col} — ${mark.rationale} `; const remove = document.createElement('button'); remove.type='button'; remove.textContent='Remove draft'; remove.onclick=()=>{marks.splice(i,1);list();draw();persistDraft();}; li.append(remove); $('marks').append(li); }
    for (const [i,region] of regions.entries()) { const li=document.createElement('li'); li.textContent=`Samples ${region.join(', ')} `;const remove=document.createElement('button');remove.type='button';remove.textContent='Remove draft';remove.onclick=()=>{regions.splice(i,1);list();draw();persistDraft();};li.append(remove);$('regions').append(li); }
  }
  async function load() {
    if (!task()) { $('progress').textContent = 'No tasks in this packet.'; return; }
    restoring = true;
    lastTick = performance.now(); elapsed = 0; image = null; $('image').getContext('2d').clearRect(0,0,$('image').width,$('image').height); viewedChannels = new Set(); position = task().mode === 'candidate' ? task().centre : null;
    const existing = saved[task().task_id]; marks = existing ? structuredClone(existing.marks) : []; regions = existing ? structuredClone(existing.excluded_regions) : [];
    $('status').value = existing?.status || 'partial'; $('notes').value=existing?.notes || ''; $('attest').checked=false;
    $('position').value=position ? position.join(', ') : ''; $('rationale').value='';$('identity').value='unknown';$('support').value='';$('label').value='uncertain';
    if (task().mode === 'candidate' && marks.length === 1) {
      const mark = marks[0];
      $('label').value = mark.label; $('identity').value = mark.identity;
      $('rationale').value = mark.rationale; $('support').value = JSON.stringify(mark.supporting_observations);
    }
    const draft = drafts[task().task_id];
    if (draft) {
      marks = structuredClone(draft.marks); regions = structuredClone(draft.excluded_regions);
      position = draft.position; elapsed = draft.elapsed; viewedChannels = new Set(draft.viewed_channels);
      for (const id of fields) if (id !== 'reviewer' || !packet.assignment) $(id).value = draft.fields[id];
      $('position').value = position ? position.join(', ') : '';
      $('message').textContent = 'Recovered a local draft. Check the assessment and attest again before saving; drafts are not submissions.';
    }
    $('label').querySelector('option[value="non_target"]').disabled = task().mode === 'survey';
    $('instructions').textContent=task().mode === 'survey' ? 'Click a valid core pixel, then record a target or uncertainty. Shift-drag to exclude an ambiguous region. Do not apply the detector’s size threshold.' : 'Assess the white centre square. Retained/rejected status and previous answers are hidden. Record exactly one assessment for this candidate.';
    $('metadata').textContent=`Acquired ${task().acquired_utc}; ${task().spacing_m} m native samples; HH fixed −30 to −5 dB / HV −40 to −10 dB display. Zoom creates no finer measurements.`;
    $('progress').textContent=`Task ${index+1} of ${packet.tasks.length}; ${Object.keys(saved).length} responses saved locally. ID ${task().task_id.slice(0,8)}`;
    $('previous').disabled=index===0;$('next').disabled=index===packet.tasks.length-1;
    $('optical').replaceChildren();
    for(const evidence of task().optical){const p=document.createElement('p');p.textContent=`Optical context: ${evidence.provider}; ${evidence.acquired_utc}; separation ${(evidence.signed_time_separation_seconds/3600).toFixed(2)} h; ${evidence.visibility.status}. ${evidence.full_movement_envelope_in_chip?'Assumed envelope shown.':'Movement envelope exceeds chip.'} Missing counterpart is not a negative label.`;const link=document.createElement('a');link.href=evidence.image;link.target='_blank';link.rel='noopener';link.textContent='Inspect supporting radar/optical pair';$('optical').append(p,link);}
    restoring = false;
    list(); const request=++imageRequest; const selectedBand=$('band').value; const nextImage=new Image(); nextImage.onload=()=>{if(request===imageRequest){image=nextImage;viewedChannels.add(selectedBand);draw();persistDraft();}};nextImage.onerror=()=>{$('message').textContent='Image unavailable. Do not mark this task complete.';};nextImage.src=task()[selectedBand];
  }
  $('band').onchange=()=>{tick();const request=++imageRequest;const selectedBand=$('band').value;image=null;const nextImage=new Image();nextImage.onload=()=>{if(request===imageRequest){image=nextImage;viewedChannels.add(selectedBand);draw();persistDraft();}};nextImage.onerror=()=>{$('message').textContent='Image unavailable. Do not complete this task.';};nextImage.src=task()[selectedBand];};$('zoom').onchange=draw;
  $('previous').onclick=()=>{if(persistDraft()){index--;load();}};$('next').onclick=()=>{if(persistDraft()){index++;load();}};
  $('image').onpointerdown=event=>{if(event.shiftKey){regionStart=point(event);$('image').setPointerCapture(event.pointerId);}};
  $('image').onpointerup=event=>{if(regionStart){const end=point(event);const start=regionStart;regionStart=null;if(!inside(...start)||!inside(...end)){$('message').textContent='Uncertain region must stay inside the core.';return;}regions.push([Math.min(start[0],end[0]),Math.min(start[1],end[1]),Math.max(start[0],end[0])+1,Math.max(start[1],end[1])+1]);list();draw();persistDraft();}else if(task().mode==='survey'){const selected=point(event);if(!inside(...selected)){$('message').textContent='Context outside the core cannot be annotated.';return;}if(position&&position.some((v,i)=>v!==selected[i])){$('message').textContent='Record the selected object or Clear selected point before selecting another.';return;}position=selected;$('position').value=selected.join(', ');draw();persistDraft();}};
  function clearPoint() {
    position = null; $('position').value = ''; $('rationale').value = '';
    $('support').value = ''; $('identity').value = 'unknown'; $('label').value = 'uncertain';
    draw(); persistDraft();
  }
  $('clear-point').onclick = clearPoint;
  function recordPoint() {
    $('message').textContent='';if(!image||!position){$('message').textContent='Load the image and select a core pixel first.';return false;}const rationale=$('rationale').value.trim();if(rationale.length<3){$('message').textContent='Record an object rationale, or Clear selected point if no object is intended.';return false;}
    let observations=[];try{observations=$('support').value.trim()?JSON.parse($('support').value):[];if(!Array.isArray(observations))throw new Error();}catch{$('message').textContent='Supporting observations must be a JSON array. Put interpretation in Object rationale; leave this evidence field empty when identity is unknown.';return false;}
    if($('identity').value!=='unknown'&&($('label').value!=='target'||!observations.length)){$('message').textContent='A resolved identity requires a target and independent positive evidence.';return false;}
    const mark={row:position[0],col:position[1],label:$('label').value,identity:$('identity').value,rationale,supporting_observations:observations};if(task().mode==='candidate')marks=[mark];else{marks=marks.filter(m=>m.row!==mark.row||m.col!==mark.col);marks.push(mark);}
    position = null; $('position').value = '';
    if (task().mode === 'survey') {
      $('rationale').value = ''; $('support').value = '';
      $('identity').value = 'unknown'; $('label').value = 'uncertain';
    }
    list(); draw(); persistDraft(); return true;
  }
  $('add-mark').onclick=()=>{recordPoint();};
  $('review-form').onsubmit=event=>{
    event.preventDefault();tick();const reviewer=$('reviewer').value.trim();if(!image||!reviewer||!$('attest').checked||$('notes').value.trim().length<3){$('message').textContent='Load the image, enter your ID, inspection notes and personal-inspection attestation.';return;}
    if($('status').value==='complete'&&viewedChannels.size!==2){$('message').textContent='Inspect both HH and HV before declaring complete coverage.';return;}
    // A selected point is an explicit assessment, never an implied empty survey.
    // Validate and record it atomically with Save, or leave the response unsaved.
    if(position&&!recordPoint())return;
    if(task().mode==='candidate'&&$('status').value==='complete'&&marks.length!==1){$('message').textContent='Record one explicit candidate assessment before completing.';return;}
    const latest = readStore(key);
    if (!unreadable.has(key) && JSON.stringify(latest[task().task_id]) !== JSON.stringify(savedBaseline[task().task_id])) {
      $('message').textContent = 'This response changed in another tab. Your draft is retained; export saved responses there and resolve the conflicting review before saving here.';
      return;
    }
    // Other tasks saved in another tab must not be replaced by our older snapshot.
    saved = { ...saved, ...latest };
    saved[task().task_id]={workspace_id:packet.workspace_id,task_id:task().task_id,phase:packet.phase,reviewer_id:reviewer,reviewed_utc:new Date().toISOString(),status:$('status').value,active_seconds:Math.min(86400,Math.max(1,elapsed+(saved[task().task_id]?.active_seconds||0))),personally_inspected:true,viewed_channels:[...viewedChannels],notes:$('notes').value.trim(),marks:structuredClone(marks),excluded_regions:structuredClone(regions)};
    let persisted=false;
    try{writeStore(key,saved);savedBaseline=structuredClone(saved);persisted=true;$('message').textContent=`Response saved locally with ${marks.length} recorded object(s). Export and import to validate it; no server write occurred.`;}catch{storageFailed=true;$('message').textContent='Browser storage unavailable; response is only in memory. Export this session before leaving.';}
    elapsed=0;
    // Update the draft after saving so restored drafts never reintroduce a pending
    // point or double count elapsed time. This does not alter the submission schema.
    if(persisted)persistDraft();
    $('progress').textContent=`Task ${index+1} of ${packet.tasks.length}; ${Object.keys(saved).length} responses in this session.`;
  };
  $('export').onclick=()=>{const rows=Object.values(saved);if(!rows.length){$('message').textContent='No personally inspected responses have been saved.';return;}const blob=new Blob([JSON.stringify(rows,null,2)],{type:'application/json'});const url=URL.createObjectURL(blob);const link=document.createElement('a');link.href=url;link.download=`reference-${packet.workspace_id.slice(0,12)}-${packet.phase}.json`;link.click();URL.revokeObjectURL(url);};
  await load();
});
