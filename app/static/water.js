const waterState={busy:false};const feedback=document.querySelector('#message');const form=document.querySelector('#water-form');form?.addEventListener('submit',async e=>{e.preventDefault();if(waterState.busy)return;waterState.busy=true;const b=form.querySelector('button');b.disabled=true;b.textContent='核算中';try{const params={max_gap_hours:form.elements.max_gap_hours.value,tolerance_m3:form.elements.tolerance_m3.value};let options;if(form.elements.file.files[0]){const f=new FormData();f.append('file',form.elements.file.files[0]);f.append('params',JSON.stringify(params));options={method:'POST',body:f};}else options={method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({text:form.elements.text.value,params})};const res=await fetch('/api/runs',options);const result=await res.json();if(!res.ok)throw Error(result.message);location.assign('/m/5?run='+result.manifest.run_id);}catch(err){feedback.textContent=err.message;}finally{waterState.busy=false;b.disabled=false;b.textContent='重算水账';}});const review=document.querySelector('#review-form');review?.addEventListener('submit',async e=>{e.preventDefault();try{const res=await fetch('/api/review/'+review.dataset.run,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({version:Number(review.dataset.version),decision:review.elements.decision.value})});const result=await res.json();if(!res.ok)throw Error(result.message);location.reload();}catch(err){feedback.textContent=err.message;}});

const revForm=document.querySelector('#revision-form');
const revMsg=document.querySelector('#revision-message');
revForm?.addEventListener('submit',async e=>{e.preventDefault();const btn=revForm.querySelector('button');btn.disabled=true;revMsg.textContent='办理中…';
  try{const res=await fetch('/api/revision/'+revForm.dataset.base,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({text:revForm.elements.text.value})});
    const job=await res.json();if(!res.ok)throw Error(job.message);revMsg.textContent='补传任务 '+job.id.slice(0,12)+'：'+job.status+'（检查点 '+job.checkpoint+'/'+job.steps.length+'）';location.reload();
  }catch(err){revMsg.textContent=err.message;}finally{btn.disabled=false;}});

function fmt(v){return v===null||v===undefined?'—':String(v);}
let currentJobId=null;
async function showJob(jobId){
  currentJobId=jobId;
  const res=await fetch('/api/revision/job/'+jobId);const job=await res.json();if(!res.ok){feedback.textContent=job.message;return;}
  const panel=document.querySelector('#job-detail');panel.hidden=false;document.querySelector('#job-id').textContent=jobId.slice(0,12);
  document.querySelector('#job-days').textContent=(job.affected_days||[]).map(x=>x[0]+' '+x[1]).join('　')||'无';
  const tb=document.querySelector('#job-changes tbody');tb.innerHTML='';
  for(const c of (job.day_changes||[])){
    const fields=c.fields?Object.entries(c.fields).map(([k,v])=>k+': '+fmt(v.old)+' → '+fmt(v.new)).join('；'):(c.change==='新增'?'整日新增':'整日消除');
    tb.insertAdjacentHTML('beforeend',`<tr><td>${c.station}</td><td>${c.date}</td><td>${c.change}</td><td>${c.fields?Object.keys(c.fields).join(','):'—'}</td><td>${fields}</td></tr>`);}
  if(!(job.day_changes||[]).length)tb.innerHTML='<tr><td colspan="5">按日无变化或尚在待裁决。</td></tr>';
  const box=document.querySelector('#job-conflicts');box.innerHTML='';
  for(const c of (job.open_conflicts||[])){
    const name=c.station+'|'+c.time;
    const opts=c.options.map(o=>`<label class="conflict-opt"><input type="radio" name="${name}" value="${o.ref}"> ${o.ref} ${fmt(o.raw.id)} 水位${fmt(o.raw.level)} 入${fmt(o.raw.inflow)} 出${fmt(o.raw.outflow)} v${fmt(o.raw.version)}</label>`).join('');
    box.insertAdjacentHTML('beforeend',`<div class="quality-block"><b>${c.station} ${c.time}</b>${opts}<button class="btn js-adj" data-station="${c.station}" data-time="${c.time}" data-name="${name}">提交裁决</button></div>`);}
  if(!(job.open_conflicts||[]).length)box.textContent='无待裁决冲突。';
  const ev=[];const m=job.merge||{};
  for(const it of [...(m.stale||[]),...(m.superseded||[]),...(m.duplicates||[])]){
    ev.push(it.code+' 行'+fmt(it.line)+' '+fmt(it.key));}
  for(const r of (job.resolved||[]))ev.push('已裁决 '+r.station+' '+r.time+' 保留行'+r.keep_line);
  document.querySelector('#job-evidence').textContent=ev.join('　')||'无';
  const cf=document.querySelector('#candidate-form');
  if(job.candidate_run_id){cf.hidden=false;cf.dataset.job=jobId;cf.dataset.version=job.candidate_version||1;}else cf.hidden=true;
  const sw=document.querySelector('#switch-btn');sw.dataset.job=jobId;
  sw.hidden=!(job.candidate_run_id&&!(job.open_conflicts||[]).length);
}
document.querySelectorAll('.js-job').forEach(b=>b.addEventListener('click',()=>showJob(b.dataset.job)));
document.addEventListener('click',async e=>{
  const adj=e.target.closest('.js-adj');if(!adj)return;
  const picked=document.querySelector(`input[name="${adj.dataset.name}"]:checked`);
  if(!picked){feedback.textContent='请先选择保留行';return;}
  const res=await fetch('/api/revision/job/'+currentJobId+'/adjudicate',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({station:adj.dataset.station,time:adj.dataset.time,ref:picked.value})});
  const job=await res.json();if(!res.ok){feedback.textContent=job.message;return;}location.reload();});
const candidateForm=document.querySelector('#candidate-form');
candidateForm?.addEventListener('submit',async e=>{e.preventDefault();const jobId=candidateForm.dataset.job;
  const res=await fetch('/api/revision/job/'+jobId+'/review',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({version:Number(candidateForm.dataset.version),decision:candidateForm.elements.decision.value})});
  const data=await res.json();if(!res.ok){feedback.textContent=data.message;return;}location.reload();});
document.querySelector('#switch-btn')?.addEventListener('click',async e=>{
  const jobId=e.target.dataset.job;const res=await fetch('/api/revision/job/'+jobId+'/switch',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({})});
  const data=await res.json();if(!res.ok){feedback.textContent=data.message;return;}location.reload();});
