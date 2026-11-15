import {api, downloadPackage} from '../api/client.js';
import {handover} from '../api/entities.js';
import {workspace} from '../stores/workspace.js';
import {session} from '../stores/session.js';
import {heading} from '../components/common/shell.js';
import {esc,date,notify} from '../utils/dom.js';
import {submit} from '../hooks/forms.js';

const KIND_LABELS={dataset:'原始日记',job:'已批准简报',report:'分发计划',delivery:'投递归档'};
const PHASE_LABELS={dataset:'原始日记',job:'简报与参数',report:'分发计划',delivery:'投递归档'};

function checksTable(verification){
 if(!verification?.checks?.length)return '<p class="empty">尚未生成校验结果。</p>';
 return '<div class="checks">'+verification.checks.map(c=>'<article class="check '+esc(c.status)+'"><div><strong>'+esc(c.label)+'</strong><span class="check-state">'+(c.status==='passed'?'通过':c.status==='warning'?'提示':'未通过')+'</span></div><p>'+esc(c.detail)+'</p></article>').join('')+'</div>';
}

function progressBar(progress,total,processed){
 const keys=Object.keys(PHASE_LABELS);
 const done=keys.reduce((sum,k)=>sum+Number(progress?.[k]?.done||0),0);
 const all=keys.reduce((sum,k)=>sum+Number(progress?.[k]?.total||0),0)||total||0;
 const pct=all?Math.min(100,Math.round(done/all*100)):0;
 return '<div class="progress"><div class="progress-track"><span style="width:'+pct+'%"></span></div><div class="progress-legend">'+
 keys.map(k=>{const part=progress?.[k]||{};const partTotal=Number(part.total||0);const partDone=Number(part.done||0);return '<span>'+esc(PHASE_LABELS[k])+' '+(partDone)+'/'+(partTotal)+'</span>';}).join('')+
 '<strong>'+done+' / '+all+' 项 · '+pct+'%</strong></div></div>';
}

function mappingsTable(detail){
 const rows=detail.mappings||[];
 if(!rows.length)return '<p class="empty">接收完成后，此处列出原站编号与本地的对应关系。</p>';
 return '<div class="table-wrap"><table><thead><tr><th>类型</th><th>原站编号</th><th>原站名称</th><th>本地编号</th><th>本地名称</th></tr></thead><tbody>'+
 rows.map(m=>'<tr><td>'+esc(KIND_LABELS[m.entity_type]||m.entity_type)+'</td><td>#'+esc(m.origin_id)+'</td><td>'+esc(m.origin_name)+'</td><td>#'+esc(m.local_id)+'</td><td>'+esc(m.local_name)+'</td></tr>').join('')+
 '</tbody></table></div>';
}

function itemsTable(detail){
 const items=(detail.items||[]).map(i=>'<tr><td>'+esc(KIND_LABELS[i.kind]||i.kind)+'</td><td>#'+esc(i.origin_id)+'</td><td>'+esc(i.name)+'</td><td>'+esc(i.format||'摘要')+'</td><td>'+Number(i.size||0).toLocaleString()+' B</td><td><code>'+esc(String(i.sha256||'').slice(0,16))+'…</code></td></tr>').join('');
 return '<div class="table-wrap"><table><thead><tr><th>类型</th><th>原站编号</th><th>名称</th><th>文件</th><th>大小</th><th>SHA256</th></tr></thead><tbody>'+items+'</tbody></table></div>';
}

export async function handoverPage(){
 const [imports,overview]=await Promise.all([handover.list(),api('/overview')]);
 const latest=imports[0]?await handover.detail(imports[0].id):null;
 const canWrite=session.user.role!=='viewer';
 const approved=overview.approved||0;
 const cards=imports.map(row=>{
  const pct=row.total_items?Math.round(row.processed_items/row.total_items*100):0;
  return '<button class="handover-card'+(latest&&latest.id===row.id?' active':'')+'" data-import="'+row.id+'"><div class="handover-card-head"><strong>'+esc(row.filename)+'</strong><span class="pill '+esc(row.status)+'">'+esc(row.status_label)+'</span></div><span>来源 '+esc(row.origin_site||'未知站点')+' · '+esc(row.status_label)+'</span><small>SHA256 '+esc(row.package_checksum.slice(0,18))+'… · '+row.processed_items+'/'+row.total_items+' 项'+(row.status==='rejected'?(' · '+row.problem_count+' 项问题'):'')+'</small><div class="mini-track"><span style="width:'+(row.status==='received'?100:pct)+'%"></span></div></button>';
 }).join('')||'<p class="empty">尚未上传交接包。</p>';

 return heading('06 / HANDOVER','交接包导出与接收','备用站接班时带走已批准简报及其原始日记、参数和已投递文件。校验不过或内容被改动时整包拒绝；接收完成前不会出现在业务列表。',
  '<button class="button" id="refresh-handover">刷新 ↻</button>')+
 '<div class="handover-grid"><section class="panel handover-left"><div class="section-title"><h2>导出交接包</h2><span>主站 → 备用站</span></div>'+
 '<p class="hint handover-note">打包范围：全部已批准且已完成的分析简报，附带其原始日记、分析参数、分发计划和已投递的 CSV/JSON 归档字节。未批准内容不会进入交接包。</p>'+
 '<div class="handover-export"><div><span>待交接已批准简报</span><strong>'+approved+'</strong></div><button class="primary" id="export-package" '+(approved?'':'disabled')+'>下载交接包 ↗</button></div>'+
 '<p class="hint">导出是可追溯的读取操作，观察员也可下载；上传、校验与接收仅限负责人和分析员。</p>'+
 '<div class="section-title spaced"><h2>接收交接包</h2><span>待校验 → 已接收</span></div>'+
 '<form id="handover-form"><label>本地交接包文件<input type="file" id="handover-file" accept=".zip" '+(canWrite?'':'disabled')+'></label>'+
 '<button type="submit" class="primary" '+(canWrite?'':'disabled')+'>上传并校验 ↗</button>'+
 (canWrite?'':'<p class="hint">观察员不可导入交接包。</p>')+'</form>'+
 '<p class="hint">上传后先进入“待校验”：文件齐全、SHA256、包内关联和归档字节全部核对通过才会开放接收；任何缺件或改动都会整包拒绝。接收的计划默认停发，归档下载字节与原站一致。</p></section>'+
 '<section class="panel handover-right"><div class="section-title"><h2>接收记录</h2><span>'+imports.length+' 个交接包</span></div>'+
 '<div class="handover-cards">'+cards+'</div></section></div>'+
 (latest?await handoverDetail(latest):'');
}

async function handoverDetail(detail){
 const canAccept=session.user.role!=='viewer'&&detail.status!=='rejected'&&detail.status!=='received';
 const received=detail.status==='received';
 return '<section class="panel handover-detail" id="handover-detail">'+
 '<div class="section-title"><h2>'+esc(detail.filename)+'</h2><span class="pill '+esc(detail.status)+'">'+esc(detail.status_label)+'</span></div>'+
 '<div class="handover-meta"><span>来源站点 <strong>'+esc(detail.origin_site||'未知')+'</strong></span><span>包指纹 <code>'+esc(detail.package_checksum.slice(0,24))+'…</code></span><span>上传 '+date(detail.created_at)+'</span>'+(received?'<span>接收 '+date(detail.accepted_at)+'</span>':'')+'</div>'+
 '<p class="handover-summary '+(detail.verification?.ok?'ok':'bad')+'">'+esc(detail.verification?.summary||detail.summary||'')+'</p>'+
 (detail.verification?.problems?.length?'<ul class="problems">'+detail.verification.problems.slice(0,12).map(p=>'<li>'+esc(p)+'</li>').join('')+'</ul>':'')+
 '<div class="section-title spaced"><h2>校验结果</h2><span>共 '+(detail.verification?.checks?.length||0)+' 项</span></div>'+checksTable(detail.verification)+
 '<div class="section-title spaced"><h2>接收进度</h2><span>'+(received?'已完成':(detail.status==='failed'?'中断，可再次发起继续':'等待接收'))+'</span></div>'+progressBar(detail.progress,detail.total_items,detail.processed_items)+
 (detail.last_error?'<p class="warning">'+esc(detail.last_error)+'</p>':'')+
 (canAccept?'<button class="primary" id="accept-import" data-import="'+detail.id+'">开始接收并完成校验 ↗</button>':(received?'<p class="hint">接收完成。所接收的分发计划默认停发，归档字节已按原站校验保留。</p>':''))+
 '<div class="section-title spaced"><h2>来源对应关系</h2><span>原站编号 → 本地编号</span></div>'+mappingsTable(detail)+
 '<div class="section-title spaced"><h2>包内明细</h2><span>manifest 清单</span></div>'+itemsTable(detail)+
 '</section>';
}

export async function bindHandover(refresh){
 document.querySelector('#refresh-handover').onclick=refresh;
 document.querySelector('#export-package')?.addEventListener('click',async()=>{
  try{await downloadPackage();notify('交接包已开始下载');}
  catch(error){notify(error.message,true);}
 });
 const form=document.querySelector('#handover-form');
 submit(form,async f=>{
  const file=document.querySelector('#handover-file').files[0];
  if(!file)throw new Error('请选择交接包 ZIP 文件');
  const row=await handover.upload(file);
  notify(row.status==='rejected'?('校验未通过，整包拒绝：'+row.problem_count+' 项问题'):(row.status==='received'?'该交接包此前已接收':row.status_label));
  await refresh();
 });
 document.querySelector('#accept-import')?.addEventListener('click',async event=>{
  const button=event.currentTarget;button.disabled=true;
  try{const row=await handover.accept(Number(button.dataset.import));notify(row.status==='received'?'交接包已接收，内容进入业务列表':'接收未完成：'+row.status_label);await refresh();}
  catch(error){notify(error.message,true);await refresh();}
  finally{button.disabled=false;}
 });
 document.querySelectorAll('.handover-card').forEach(button=>button.onclick=async()=>{
  const anchor=document.querySelector('#handover-detail');
  if(!anchor)return;
  try{const detail=await handover.detail(Number(button.dataset.import));anchor.outerHTML=await handoverDetail(detail);bindHandover(refresh);}catch(error){notify(error.message,true);}
 });
}
