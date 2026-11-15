import {api} from './client.js';
import {session} from '../stores/session.js';
export const datasets={list:()=>api('/datasets'),get:id=>api('/datasets/'+id),create:data=>api('/datasets',{method:'POST',body:JSON.stringify(data)})};
export const jobs={list:()=>api('/jobs'),get:id=>api('/jobs/'+id),create:data=>api('/jobs',{method:'POST',body:JSON.stringify(data)}),approve:id=>api('/jobs/'+id+'/approve',{method:'POST'})};
export const reports={list:()=>api('/reports'),create:data=>api('/reports',{method:'POST',body:JSON.stringify(data)}),toggle:id=>api('/reports/'+id+'/toggle',{method:'POST'}),deliveries:()=>api('/deliveries')};

export const handover={
 list:()=>api('/handover/imports'),
 detail:id=>api('/handover/imports/'+id),
 upload:async file=>{
  const r=await fetch('/api/handover/imports?filename='+encodeURIComponent(file.name),{method:'POST',headers:{'Content-Type':'application/zip',...(session.token?{Authorization:'Bearer '+session.token}:{})},body:file});
  if(!r.ok){let d=await r.json().catch(()=>({detail:'上传失败，请重试'}));throw new Error(typeof d.detail==='string'?d.detail:JSON.stringify(d.detail));}
  return r.json();
 },
 accept:id=>api('/handover/imports/'+id+'/accept',{method:'POST'}),
};
