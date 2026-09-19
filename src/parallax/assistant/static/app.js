const $ = (id) => document.getElementById(id);
const auth = document.querySelector('meta[name="assistant-token"]').content;
$("provider").value = document.querySelector('meta[name="assistant-provider"]').content;
let pending = null;
let submitting = false;
let lastEventCount = -1;
let currentState = null;
let resultKey = null;
let layoutKey = null;
const names = {idle:"جاهز",running:"قيد التنفيذ",waiting:"بانتظارك",completed:"النتيجة جاهزة",cancelled:"متوقفة",failed:"تعذّرت",limited:"وصلت إلى الحد"};
const verbs = {click:"النقر على العنصر",fill:"تعبئة الحقل",select:"اختيار القيمة",press:"الضغط على المفتاح"};
async function api(path, data) {
  const response = await fetch(path, {method:data ? "POST" : "GET", headers:{"X-Parallax-Token":auth,"Content-Type":"application/json"}, ...(data ? {body:JSON.stringify(data)} : {})});
  const body = await response.json();
  if (!response.ok) throw new Error(body.error || "تعذّر الاتصال.");
  return body;
}
function error(message) { $("error").textContent=message; $("error").hidden=false; }
function node(tag, text, className) {
  const element=document.createElement(tag);
  if (text !== undefined) element.textContent=text;
  if (className) element.className=className;
  return element;
}
function sourceLink(source, label) {
  try {
    const url=new URL(source.url);
    if (!["http:","https:"].includes(url.protocol) || url.username || url.password) throw new Error();
    const a=node("a",label || source.title || url.hostname);
    a.href=url.href;a.target="_blank";a.rel="noopener noreferrer";
    a.title=url.hostname;
    return a;
  } catch {return node("span","رابط المصدر غير صالح");}
}
function readableText(text) {
  const block=node("div",undefined,"answer-text");
  // Only backtick code spans are formatted. Never interpret model HTML.
  text.split(/(`[^`\n]+`)/g).forEach(part=>{
    if (part.startsWith("`") && part.endsWith("`")) {
      const code=node("code",part.slice(1,-1));code.dir="ltr";block.append(code);
    } else block.append(document.createTextNode(part));
  });
  return block;
}
function renderResult(state) {
  const key=JSON.stringify([state.id,state.result,state.report,state.sources,state.finished_at]);
  if (resultKey===key) return;
  resultKey=key;
  const container=$("result");container.replaceChildren();
  container.hidden=!state.result;
  $("suggestions").replaceChildren();
  if (!state.result) return;
  const report=state.report,sources=state.sources || [];
  container.append(node("h3","النتيجة"));
  if (state.restored) container.append(node("p","نتيجة محفوظة من التشغيل السابق؛ لم تُحدّث بياناتها بعد إعادة تشغيل الخدمة.","hint"));
  if (state.finished_at) {
    const seconds=Math.max(0,Math.round(state.finished_at-state.started_at));
    container.append(node("p",`أُعدّت ${new Date(state.finished_at*1000).toLocaleString("ar")} · مدة المهمة ${seconds} ثانية`,"hint"));
  }
  if (report) {
    container.append(readableText(report.summary));
    const scope=node("div",undefined,"scope");
    scope.append(node("h4","نطاق النتيجة والعمل المنفذ"),readableText(report.scope || "لم يوضح المحرك الفلاتر أو نطاق المقارنة."),
      readableText(report.work_done || "لم يوضح المحرك مستوى الفحص."),
      node("p","هذا وصف المحرك لعمله؛ سجل المصادر أدناه يوضح الصفحات التي قُرئت فعلًا.","hint"));
    container.append(scope);
    for (const [index,finding] of report.findings.entries()) {
      const card=node("article",undefined,"finding");
      card.append(node("h4",`${index+1}. ${finding.title.replace(/^\s*[0-9٠-٩]+[.)、-]\s*/,"")}`));
      const metrics=node("dl",undefined,"metrics");
      for (const metric of finding.metrics) {
        const pair=node("div");pair.append(node("dt",metric.label),node("dd",metric.value));metrics.append(pair);
      }
      if (finding.metrics.length) card.append(metrics);
      card.append(readableText(finding.detail));
      const refs=node("div",undefined,"source-links");
      for (const id of finding.source_ids) {
        const source=sources.find(s=>s.id===id);
        if (source) refs.append(sourceLink(source,`فتح المصدر ${id}`));
      }
      if (!refs.childElementCount) refs.append(node("span","لا يوجد مصدر مقروء مرتبط بهذه النقطة.","hint"));
      card.append(refs);container.append(card);
    }
    const limits=node("div",undefined,"limitations");limits.append(node("h4","حدود النتيجة وما لم يُتحقق منه"));
    const list=node("ul");
    for (const item of report.limitations.length ? report.limitations : ["لم يذكر المحرك قيودًا إضافية؛ هذا لا يعني أن كل استنتاج تحقق منه مستقلًا."]) list.append(node("li",item));
    limits.append(list);container.append(limits);
  } else container.append(readableText(state.result));
  if (sources.length) {
    const evidence=node("details");
    evidence.append(node("summary",`صفحات قرأها المساعد (${sources.length})`),
      node("p","قراءة الصفحة لا تثبت وحدها صحة كل استنتاج. الروابط التي لم تُفتح لا تُدرج هنا.","hint"));
    const list=node("ul",undefined,"evidence");
    for (const source of sources) {
      const item=node("li");item.append(sourceLink(source,`${source.id} — ${source.title}`),
        node("span",`آخر قراءة: ${new Date(source.observed_at*1000).toLocaleString("ar")}`,"hint"));list.append(item);
    }
    evidence.append(list);container.append(evidence);
  } else container.append(node("p","لا توجد صفحات ويب مسجلة كأدلة لهذه النتيجة.","hint"));
  const suggestions=report?.followups?.length ? report.followups : ["تعمق في النتيجة الأولى وتحقق من أدلتها", "وضح حدود النتيجة وما يحتاج إلى تحقق"];
  for (const suggestion of suggestions) {
    const button=node("button",suggestion);button.type="button";
    button.onclick=()=>{$("followup-task").value=suggestion;$("followup-task").focus();};
    $("suggestions").append(button);
  }
}
function render(state) {
  currentState=state;
  document.body.dataset.status=state.status;
  const active = ["running","waiting"].includes(state.status);
  const mode=state.browser?.mode === "chrome" ? "Chrome" : "المتصفح المستقل";
  $("browser-status").textContent=state.browser?.connected ? `${mode} متصل` : `${mode} · غير متصل بعد`;
  $("browser-status").classList.toggle("connected",!!state.browser?.connected);
  $("status").textContent = names[state.status] || state.status;
  $("start").disabled = active || submitting;
  $("consent-mode").disabled=active;
  $("empty-state").hidden=!!state.id;
  $("run-summary").hidden=!state.id;
  $("run-summary").textContent=state.consent_mode
    ? `${state.consent_mode==="review" ? "مراجعة كل خطوة" : "تصفح تلقائي"} · ${state.automatic_steps || 0} خطوات تلقائية · ${state.approval_requests || 0} طلبات موافقة`
    : "نتيجة محفوظة من تشغيل سابق";
  $("pause").disabled = !active || state.pending?.type === "handoff";
  $("stop").disabled = !active;
  $("current-task").textContent=(state.task || "").replace(/https?:\/\/\S+/g,"[رابط المهمة]");
  $("page").replaceChildren();
  $("page").hidden=!state.page;
  if (state.page) $("page").append(sourceLink({url:state.page,title:"فتح الصفحة الحالية"}));
  $("approval-hint").hidden=!active;
  $("approval-hint").textContent=state.consent_mode==="review"
    ? "أنت تراجع كل خطوة تفاعلية. القراءة والتنقل المباشر تلقائيان."
    : "يتابع التصفح تلقائيًا، ويطلب قرارك عند تغيير البيانات أو غموض أثر الخطوة.";
  $("output-status").hidden=!active || !!state.result;
  $("output-status").textContent=state.status==="waiting"
    ? (state.pending?.type==="approval" ? "لم تصدر النتيجة بعد. المهمة تنتظر اعتماد الخطوة أدناه." : "لم تصدر النتيجة بعد. المهمة متوقفة؛ سبب التوقف والخطوة المطلوبة موضحان أدناه.")
    : `${state.phase || "جارٍ معالجة المهمة"} — ستظهر المخرجات هنا عند اكتمال التحليل.`;
  $("followup").hidden=state.status!=="completed";
  $("result-tools").hidden=!state.result;
  $("followup-submit").disabled=submitting;
  renderResult(state);
  const nextLayout=JSON.stringify([state.id,state.status]);
  if (layoutKey!==nextLayout) {
    if (state.status==="completed") {$("composer").open=false;$("event-log").open=false;$("followup-mode").value=state.consent_mode || "browse";}
    if (active) $("composer").open=false;
    if (currentState.id !== JSON.parse(layoutKey || "[null]")[0]) lastEventCount=-1;
    layoutKey=nextLayout;
  }
  $("pending").hidden=!state.pending;
  if (state.pending && pending?.token !== state.pending.token) {
    const p=state.pending;
    $("pending-title").textContent=p.failure ? "نحتاج مساعدتك لاستكمال المهمة" : p.type==="handoff" ? "خطوة تحتاج تدخلك" : "قرار واحد قبل المتابعة";
    $("approval-why").textContent=p.approval_reason || "";
    $("approval-why").hidden=!p.approval_reason;
    $("reason").textContent=p.message || p.action.reason;
    $("details").replaceChildren();
    if (p.type==="approval") {
      for (const [label,value] of [["الصفحة",p.page],["الإجراء",verbs[p.action.kind]],["العنصر",p.target?.label || p.target?.tag],["القيمة",p.action.value || "—"]]) {
        const dt=document.createElement("dt"),dd=document.createElement("dd");
        dt.textContent=label;dd.textContent=value;$("details").append(dt,dd);
      }
    }
    $("note").value="";
    $("note").disabled=p.type!=="handoff";
    $("handoff-note").hidden=p.type!=="handoff";
    $("approve").textContent=p.type==="handoff" ? "انتهيت، استأنف المهمة" : `اعتماد ${verbs[p.action.kind]}`;
  }
  pending=state.pending;
  const events=state.events || [];
  const eventKey=JSON.stringify(events);
  if (lastEventCount!==eventKey) {
    $("events").replaceChildren(...events.map(e=>{const li=document.createElement("li");li.textContent=`${new Date(e.time*1000).toLocaleTimeString("ar")} — ${e.message}`;return li;}));
    lastEventCount=eventKey;
  }
}
async function control(command) {
  $("error").hidden=true;
  const token=pending?.token || "";
  try { await api("/api/control",{command,token,note:$("note").value}); render(await api("/api/state")); }
  catch(e) {error(e.message);}
}
$("task-form").addEventListener("submit",async e=>{
  e.preventDefault();if(submitting)return;submitting=true;$("start").disabled=true;$("error").hidden=true;lastEventCount=-1;
  try {$("export-status").textContent="";render(await api("/api/task",{task:$("task").value,provider:$("provider").value,consent_mode:$("consent-mode").value}));}
  catch(e) {error(e.message);}
  finally {submitting=false;}
});
$("followup-form").addEventListener("submit",async e=>{
  e.preventDefault();if(submitting || currentState?.status!=="completed")return;
  submitting=true;$("followup-submit").disabled=true;$("error").hidden=true;
  try {
    $("export-status").textContent="";
    render(await api("/api/task",{task:$("followup-task").value,provider:currentState.provider,parent_id:currentState.id,consent_mode:$("followup-mode").value}));
    $("followup-task").value="";
  } catch(e) {error(e.message);}
  finally {submitting=false;$("followup-submit").disabled=false;}
});
for (const [field,button] of [["task","start"],["followup-task","followup-submit"]]) {
  $(field).addEventListener("keydown",e=>{
    if (e.key!=="Enter" || e.shiftKey || e.ctrlKey || e.altKey || e.metaKey || e.isComposing || e.keyCode===229) return;
    e.preventDefault();
    if (!e.repeat && !submitting && !$(button).disabled) $(field).form.requestSubmit($(button));
  });
}
$("stop").onclick=()=>control("stop");$("pause").onclick=()=>control("pause");
$("reject").onclick=()=>control("reject");
$("approve").onclick=()=>control(pending?.type==="handoff" ? "resume" : "approve");
$("consent-mode").onchange=()=>{
  $("consent-help").textContent=$("consent-mode").value==="review"
    ? "يطلب موافقتك قبل كل نقرة أو تعبئة أو اختيار. التنقل المباشر والقراءة والتمرير تلقائية."
    : "يفتح القوائم والتبويبات المعروفة تلقائيًا. الحفظ والإرسال والتعبئة والعناصر غير الواضحة تحتاج موافقتك. يمكنك إيقاف المهمة في أي وقت.";
};
function exportText() {
  const report=currentState?.report;
  const lines=report ? [report.summary,"", "نطاق الفحص",report.scope,report.work_done] : [currentState?.result || ""];
  for (const finding of report?.findings || []) {
    lines.push("",finding.title,finding.detail,...finding.metrics.map(m=>`${m.label}: ${m.value}`));
  }
  if (report?.limitations?.length) lines.push("","حدود النتيجة",...report.limitations.map(v=>`- ${v}`));
  if (currentState?.sources?.length) lines.push("","المصادر المقروءة",...currentState.sources.map(s=>`${s.id} — ${s.title}\n${s.url}`));
  return lines.join("\n");
}
$("copy-result").onclick=async()=>{
  try {await navigator.clipboard.writeText(exportText());$("export-status").textContent="نُسخت النتيجة";}
  catch {$("export-status").textContent="تعذّر النسخ؛ استخدم تنزيل التقرير.";}
};
$("download-result").onclick=()=>{
  const url=URL.createObjectURL(new Blob([exportText()],{type:"text/plain;charset=utf-8"}));
  const link=node("a");link.href=url;link.download="parallax-report.txt";link.click();
  setTimeout(()=>URL.revokeObjectURL(url),1000);$("export-status").textContent="تم تجهيز تنزيل التقرير";
};
document.addEventListener("keydown",e=>{if(e.key==="Escape" && pending){e.preventDefault();control("reject");}});
async function poll(){try{render(await api("/api/state"));}catch(e){error(e.message);$("browser-status").textContent="تعذّر التحقق من الاتصال";$("browser-status").classList.remove("connected");}setTimeout(poll,1000);}
poll();
