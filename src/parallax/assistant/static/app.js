const $ = (id) => document.getElementById(id);
const auth = document.querySelector('meta[name="assistant-token"]').content;
$("provider").value = document.querySelector('meta[name="assistant-provider"]').content;
let pending = null;
let submitting = false;
let lastEventCount = -1;
let currentState = null;
let resultKey = null;
let layoutKey = null;
let planKey = null;
let revising = false;
let attemptsKey = null;
let authorityKey = null;
let artifactsKey = null;
let fileAdding = false;
let checkpointBusy = false;
let checkpointTaskId = null;
let checkpointReviewStale = false;
const names = {idle:"جاهز",running:"قيد التنفيذ",waiting:"بانتظارك",verified:"متحقق منه",partial:"مكتمل جزئيًا",unverified:"غير متحقق منه",cancelled:"متوقفة",failed:"تعذّرت",limited:"وصلت إلى الحد"};
const verbs = {click:"النقر على العنصر",fill:"تعبئة الحقل",select:"اختيار القيمة",press:"الضغط على المفتاح",download:"تنزيل الملف",upload:"رفع الملف المحدد"};
const terminalResults = new Set(["verified","partial","unverified"]);
async function api(path, data) {
  const response = await fetch(path, {method:data ? "POST" : "GET", headers:{"X-Parallax-Token":auth,"Content-Type":"application/json"}, ...(data ? {body:JSON.stringify(data)} : {})});
  const body = await response.json();
  if (!response.ok) {
    const failure=new Error(body.error || "تعذّر الاتصال.");failure.code=body.code;
    throw failure;
  }
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
function completionName(status) { return names[status] || "غير متحقق منه"; }
function conditionText(value) {
  return typeof value==="string" ? value : Object.entries(value || {}).map(([label,text])=>`${label}: ${text}`).join("؛ ");
}
function changeValue(change) {
  return change.choice_label && change.choice_label!==change.value ? `${change.choice_label} (${change.value})` : change.value;
}
function renderCompletion(completion, sources) {
  const section=node("section",undefined,"completion");
  section.append(node("h4","حالة الإنجاز والتحقق"));
  section.append(node("p",completionName(completion?.status),"completion-status "+(completion?.status || "unverified")));
  section.append(readableText(completion?.reason || "لا يوجد دليل تحقق قابل للمراجعة."));
  for (const [title, values, empty] of [["ما أُنجز",completion?.done,"لم يثبت إنجاز محدد."],["ما بقي",completion?.remaining,"لم يذكر المحرك ما بقي؛ لا يعني ذلك اكتمال المهمة."]]) {
    const block=node("div",undefined,"completion-list");block.append(node("h5",title));
    const list=node("ul");for (const value of values?.length ? values : [empty]) list.append(node("li",value));
    block.append(list);section.append(block);
  }
  const evidence=node("div",undefined,"completion-evidence");evidence.append(node("h5","أدلة التحقق"));
  if (completion?.evidence?.length) {
    const list=node("ul");
    for (const item of completion.evidence) {
      const source=sources.find(s=>s.id===item.source_id);const row=node("li");
      row.append(readableText(item.claim));
      if (source) row.append(sourceLink(source,"فتح المصدر "+item.source_id));
      row.append(node("span",item.observed_after_action ? "قُرئ بعد آخر خطوة تفاعلية." : "قُرئ قبل أي خطوة تفاعلية.","hint"));list.append(row);
    }
    evidence.append(list);
  } else evidence.append(node("p","لا توجد أدلة صفحة قابلة للتحقق لهذه النتيجة.","hint"));
  section.append(evidence);
  if (completion?.effect_checks?.length) {
    const checks=node("div",undefined,"completion-evidence");checks.append(node("h5","التحقق من آثار العمليات"));
    const list=node("ul");
    for (const [index,check] of completion.effect_checks.entries()) {
      const row=node("li",`العملية ${index+1}: ${check.status==="matched" ? "طابقت الصفحة شرط النتيجة المحدد قبل التنفيذ" : check.status==="no_condition" ? "لم يُحدد شرط تحقق قبل التنفيذ" : "لم تثبت الحالة المتوقعة"}`);
      const source=sources.find(s=>s.id===check.source_id);
      if (source) row.append(sourceLink(source,"فتح دليل العملية"));
      list.append(row);
    }
    checks.append(list,node("p","المطابقة تخص الحالة المعروضة في الموقع، ولا تثبت وحدها اكتمال معالجة الخدمة خارج الصفحة.","hint"));section.append(checks);
  }
  if (completion?.operation_checks?.length) {
    const checks=node("div",undefined,"completion-evidence");checks.append(node("h5","نتيجة تغييرات النماذج المقترحة"));
    const list=node("ul");
    for (const [index,check] of completion.operation_checks.entries()) {
      const row=node("li",`المجموعة ${index+1}: ${check.status==="matched" ? "ظهرت جميع القيم المطلوبة للسجل المحدد" : "لم تثبت جميع القيم المطلوبة، حتى لو لم تبدأ عملية الحفظ"}`);
      const source=sources.find(s=>s.id===check.source_id);
      if (source) row.append(sourceLink(source,"فتح دليل القيم"));
      list.append(row);
    }
    checks.append(list);section.append(checks);
  }
  return section;
}
function renderResult(state, saved=false) {
  const key=JSON.stringify([state.id,state.result,state.report,state.completion,state.sources,state.finished_at]);
  if (!saved && resultKey===key) return;
  if (!saved) resultKey=key;
  const container=$(saved ? "history-result" : "result");container.replaceChildren();
  container.hidden=!state.result;
  if (!saved) $("suggestions").replaceChildren();
  if (!state.result) return;
  const report=state.report,sources=state.sources || [];
  container.append(node("h3","النتيجة"));
  if (saved) container.append(node("p",`نتيجة محفوظة · ${new Date((state.finished_at || state.recorded_at)*1000).toLocaleString("ar")} · ${completionName(state.status)}. لم يُعد التحقق منها الآن.`,"hint"));
  else if (state.restored) container.append(node("p","نتيجة محفوظة من التشغيل السابق؛ لم تُحدّث بياناتها بعد إعادة تشغيل الخدمة.","hint"));
  if (!saved && state.finished_at) {
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
  container.append(renderCompletion(state.completion || report?.completion, sources));
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
  if (saved) return;
  const suggestions=report?.followups?.length ? report.followups : ["تعمق في النتيجة الأولى وتحقق من أدلتها", "وضح حدود النتيجة وما يحتاج إلى تحقق"];
  for (const suggestion of suggestions) {
    const button=node("button",suggestion);button.type="button";
    button.onclick=()=>{$("followup-task").value=suggestion;$("followup-task").focus();};
    $("suggestions").append(button);
  }
}
function renderArtifacts(state) {
  const active=["running","waiting"].includes(state.status);
  $("file-form").hidden=!active;
  $("file-add").disabled=fileAdding || !active || (state.artifacts || []).length>=5;
  const key=JSON.stringify([state.id,state.artifacts,state.artifacts_error,state.file_context_warning,active]);
  if (artifactsKey===key) return;
  artifactsKey=key;
  $("artifacts").hidden=!active && !state.artifacts?.length && !state.artifacts_error && !state.file_context_warning;
  $("artifact-list").replaceChildren();
  $("artifact-status").textContent=state.artifacts_error || state.file_context_warning || "";
  renderFileList($("artifact-list"),state.artifacts || [],$("artifact-status"));
}
function renderFileList(container, files, notice, afterRemove=async()=>{}) {
  container.replaceChildren();
  for (const file of files) {
    const item=node("div",undefined,"completion-list");
    item.append(node("p",`${file.name} — ${file.size} بايت`));
    item.append(node("p",file.source_kind==="inherited" ? "مرجع لنسخة محددة من مهمة سابقة؛ إزالته هنا لا تحذف الأصل" : file.source_kind==="user_selected" ? "اخترته من جهازك لهذه المهمة" : "نُزّل ضمن هذه المهمة","hint"));
    const get=node("button",file.available ? "الحصول على نسخة" : "الملف غير متاح");get.disabled=!file.available;
    get.onclick=async()=>{
      get.disabled=true;
      try {
        const response=await fetch(`/api/files/${file.task_id}/${file.id}`,{headers:{"X-Parallax-Token":auth}});
        if (!response.ok) throw new Error("الملف غير متاح أو تغير محتواه.");
        const url=URL.createObjectURL(await response.blob());
        const link=node("a");link.href=url;link.download=file.name;link.click();
        setTimeout(()=>URL.revokeObjectURL(url),1000);
        notice.textContent="تم تجهيز نسخة الملف.";
      } catch(e) {notice.textContent=e.message;}
      finally {get.disabled=false;}
    };
    const remove=node("button",file.source_kind==="inherited" ? "إزالة من المتابعة" : "حذف النسخة المحلية");
    remove.onclick=async()=>{
      remove.disabled=true;
      try {render(await api("/api/files/delete",{task_id:file.task_id,id:file.id}));await afterRemove();}
      catch(e) {notice.textContent=e.message;remove.disabled=false;}
    };
    item.append(get,remove);container.append(item);
  }
}
function renderPlan(state) {
  const key=JSON.stringify([state.id,state.task_plan,state.plan_revision,state.plan_stale]);
  if (planKey===key) return;
  planKey=key;
  const plan=state.task_plan;
  $("task-plan").hidden=!plan;
  $("plan-content").replaceChildren();
  if (!plan) return;
  $("plan-notice").textContent=state.plan_stale
    ? "وصل توجيه جديد؛ هذه الخطة السابقة بانتظار إعادة التقييم."
    : "هذا فهم المساعد للمهمة وتقدمها المقترح؛ التحقق من الإنجاز يظهر في النتيجة.";
  const content=$("plan-content");
  content.append(node("h4","الهدف"),readableText(plan.goal));
  for (const [title,values] of [["القيود والافتراضات",plan.constraints],["معايير الإنجاز",plan.success_criteria]]) {
    if (!values.length) continue;
    content.append(node("h4",title));
    const list=node("ul");
    for (const value of values) list.append(node("li",value));
    content.append(list);
  }
  const labels={pending:"لاحقًا",in_progress:"جارٍ العمل عليها",done:"أبلغ المساعد بإتمامها",blocked:"بانتظار متطلب"};
  content.append(node("h4","خطوات العمل"));
  const steps=node("ol",undefined,"plan-steps");
  for (const step of plan.steps) {
    const row=node("li");
    row.append(node("span",step.title),node("span",labels[step.status],"hint plan-step-status"));
    if (step.depends_on.length) {
      const titles=step.depends_on.map(id=>plan.steps.find(item=>item.id===id)?.title).filter(Boolean);
      row.append(node("p","تحتاج أولًا: "+titles.join("، "),"hint"));
    }
    steps.append(row);
  }
  content.append(steps);
}
function renderAttempts(state) {
  const rows=state.uncertain_actions || [];
  const key=JSON.stringify([rows,state.journal_error,state.id,state.status]);
  if (attemptsKey===key) return;
  attemptsKey=key;
  $("uncertain-actions").hidden=!rows.length && !state.journal_error;
  $("journal-error").hidden=!state.journal_error;
  $("journal-error").textContent=state.journal_error || "";
  $("uncertain-list").replaceChildren();
  for (const receipt of rows) {
    const row=node("article",undefined,"attempt-row");
    row.append(node("h4",verbs[receipt.kind] || "إجراء تفاعلي"),sourceLink({url:receipt.origin},"فتح الوجهة للفحص"),
      node("p",new Date(receipt.created_at*1000).toLocaleString("ar"),"hint"));
    const active=receipt.status==="attempting" && receipt.task_id===state.id && ["running","waiting"].includes(state.status);
    row.append(node("p",active ? "قد يكون الإجراء قيد التنفيذ؛ انتظر النتيجة أو أوقف المهمة قبل الفحص." : "لم يتأكد أثر هذا الإجراء. اختيارك أدناه يسجل فحصًا يدويًا، ولا يعد تحققًا آليًا.","hint"));
    const buttons=node("div",undefined,"toolbar");
    for (const [occurred,label] of [[true,"تحققت أن الإجراء تم"],[false,"تحققت أنه لم يتم"]]) {
      const button=node("button",label);button.type="button";button.disabled=active;
      button.onclick=async()=>{
        for (const control of buttons.children) control.disabled=true;
        $("error").hidden=true;
        try {render(await api("/api/control",{command:occurred ? "effect_occurred" : "effect_absent",token:receipt.id}));}
        catch(e) {error(e.message);for (const control of buttons.children) control.disabled=false;}
      };
      buttons.append(button);
    }
    row.append(buttons);$("uncertain-list").append(row);
  }
}
function renderAuthority(state) {
  const authority=state.operation_authority;
  $("operation-authority").hidden=!authority;
  if (!authority) return;
  const key=JSON.stringify([authority.id,authority.description,authority.page,authority.identity,authority.changes]);
  if (key!==authorityKey) {
    authorityKey=key;
    const details=$("authority-details");details.replaceChildren(node("p",authority.description),node("p",conditionText(authority.identity)),sourceLink({url:authority.page},"فتح صفحة العملية"));
    const list=node("ol");
    for (const change of authority.changes) list.append(node("li",`${verbs[change.kind]}: ${change.label}${change.value ? " — "+changeValue(change) : ""}`));
    details.append(list);
  }
  const status={active:"نشط",revoked:"أُلغي اعتماد البقية",expired:"انتهت الصلاحية",consumed:"انتهى تنفيذ الخطوات؛ التحقق من النتيجة مستقل"}[authority.status] || "غير نشط";
  $("authority-status").textContent=`${status} · تقدم التنفيذ: ${authority.executed} / ${authority.steps} خطوات`;
  const basis=authority.basis==="user_explicit_request" ? "تفويض من طلبك الصريح" : "اعتماد من المعاينة";
  $("authority-expiry").textContent=`${basis} لهذه القيم فقط، ينتهي ${new Date(authority.expires_at*1000).toLocaleTimeString("ar")}. الإلغاء لا يتراجع عن خطوة بدأت بالفعل.`;
  $("revoke-operation").disabled=authority.status!=="active";
}
function render(state) {
  currentState=state;
  expireHistoryView();
  updateDeleteControls();
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
  $("followup").hidden=!terminalResults.has(state.status);
  $("result-tools").hidden=!state.result;
  $("followup-submit").disabled=submitting;
  $("task-revision").hidden=!active;
  $("revision-submit").disabled=!active || revising;
  renderPlan(state);
  renderAttempts(state);
  renderAuthority(state);
  renderResult(state);
  renderArtifacts(state);
  const nextLayout=JSON.stringify([state.id,state.status]);
  if (layoutKey!==nextLayout) {
    if (terminalResults.has(state.status)) {$("composer").open=false;$("event-log").open=false;$("followup-mode").value=state.consent_mode || "browse";}
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
    $("reason").textContent=p.message || p.operation?.description || p.action.reason;
    $("details").replaceChildren();
    if (p.type==="approval") {
      const rows=p.operation ? [["الصفحة",p.page],["الهوية",conditionText(p.operation.identity)],["حدود الاعتماد","هذه القيم فقط، مرة واحدة، خلال خمس دقائق من الموافقة"]] : [["الصفحة",p.page],["الإجراء",verbs[p.action.kind]],["العنصر",p.target?.label || p.target?.tag],["القيمة",p.action.value || "—"]];
      for (const [label,value] of rows) {
        const dt=document.createElement("dt"),dd=document.createElement("dd");
        dt.textContent=label;dd.textContent=value;$("details").append(dt,dd);
      }
      for (const [index,change] of (p.operation?.changes || []).entries()) {
        $("details").append(node("dt",`${index+1}. ${verbs[change.kind]} — ${change.label}`),node("dd",changeValue(change) || "تنفيذ زر الحفظ المعروض"));
      }
      if (p.download) {
        for (const [label,value] of [["رابط الملف",p.download.url],["مجلد الحفظ",p.download.directory],["الحد الأقصى","20 ميغابايت؛ خمسة ملفات للمهمة؛ الاحتفاظ حتى الحذف"]]) {
          $("details").append(node("dt",label),node("dd",value));
        }
      }
      if (p.upload) {
        for (const [label,value] of [["الملف",p.upload.name],["الحجم",`${p.upload.size} بايت`],["بصمة النسخة",p.upload.sha256],["النوع",p.upload.mime],["وجهة النموذج",p.target?.form?.action || p.page],["الملفات المحددة في الحقل",`${p.target?.selected_files || 0} — سيستبدلها هذا الاختيار`]]) {
          $("details").append(node("dt",label),node("dd",value));
        }
      }
      if (p.effect_check) {
        for (const [label,value] of [["العملية المطلوبة",p.effect_check.description],["العنصر المراد التحقق منه",conditionText(p.effect_check.subject)],["النتيجة المتوقعة",conditionText(p.effect_check.outcome)],["صفحة التحقق",p.effect_check.url]]) {
          $("details").append(node("dt",label),node("dd",value));
        }
        for (const [label,aliases] of Object.entries(p.effect_check.label_aliases || {})) {
          $("details").append(node("dt",`تسميات بديلة لحقل ${label}`),node("dd",aliases.join("، ")));
        }
        if (p.effect_check.url_scope === "origin") {
          $("details").append(node("dt","نطاق دليل النتيجة"),node("dd","صفحة لاحقة على أصل الموقع نفسه؛ مع تطابق هوية السجل وقيمه المطلوبة."));
        }
        if (p.effect_check.casefold_outcome?.length) {
          $("details").append(node("dt","حقول لا تؤثر حالة الأحرف في قيمها"),node("dd",p.effect_check.casefold_outcome.join("، ")));
        }
      } else if (Object.hasOwn(p,"effect_check")) {
        $("details").append(node("dt","التحقق من النتيجة"),node("dd","لم يُحدد شرط قبل هذه الخطوة؛ لن تُعد نتيجتها متحققًا منها تلقائيًا."));
      }
    }
    $("note").value="";
    $("note").disabled=p.type!=="handoff";
    $("handoff-note").hidden=p.type!=="handoff";
    $("approve").textContent=p.type==="handoff" ? "انتهيت، استأنف المهمة" : p.operation ? "اعتماد التغييرات وحفظها مرة واحدة" : `اعتماد ${verbs[p.action.kind]}`;
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
$("revoke-operation").addEventListener("click",async()=>{
  const authority=currentState?.operation_authority;
  if (!authority || authority.status!=="active") return;
  $("revoke-operation").disabled=true;
  try {render(await api("/api/control",{command:"revoke_operation",token:authority.id}));}
  catch(e) {error(e.message);render(await api("/api/state"));}
});
$("pending").addEventListener("keydown",e=>{
  if (e.key==="Escape" && pending?.type==="approval") {e.preventDefault();control("reject");}
});
$("revision-form").addEventListener("submit",async e=>{
  e.preventDefault();
  if (revising || !["running","waiting"].includes(currentState?.status)) return;
  revising=true;$("revision-submit").disabled=true;$("error").hidden=true;
  try {
    render(await api("/api/control",{command:"revise",token:currentState.id,note:$("revision-note").value}));
    $("revision-note").value="";
    $("revision-status").textContent="وصل التوجيه. لن يستأنف التسلم اليدوي إلا بعد اختيارك الاستئناف.";
  } catch(e) {error(e.message);}
  finally {revising=false;$("revision-submit").disabled=!["running","waiting"].includes(currentState?.status);}
});
$("task-form").addEventListener("submit",async e=>{
  e.preventDefault();if(submitting)return;submitting=true;$("start").disabled=true;$("error").hidden=true;lastEventCount=-1;
  try {$("export-status").textContent="";$("revision-status").textContent="";render(await api("/api/task",{task:$("task").value,provider:$("provider").value,consent_mode:$("consent-mode").value}));}
  catch(e) {error(e.message);}
  finally {submitting=false;}
});
$("followup-form").addEventListener("submit",async e=>{
  e.preventDefault();if(submitting || !terminalResults.has(currentState?.status))return;
  submitting=true;$("followup-submit").disabled=true;$("error").hidden=true;
  try {
    $("export-status").textContent="";
    $("revision-status").textContent="";
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
$("file-form").onsubmit=async(event)=>{
  event.preventDefault();
  if (fileAdding) return;
  const file=$("task-file").files[0];
  if (!file || file.size>20*1024*1024) {$("artifact-status").textContent="اختر ملفًا لا يتجاوز 20 ميغابايت.";return;}
  fileAdding=true;$("file-add").disabled=true;
  try {
    const response=await fetch("/api/files/add",{method:"POST",headers:{"X-Parallax-Token":auth,
      "X-Parallax-Task":currentState.id,"X-Parallax-Filename":encodeURIComponent(file.name),"Content-Type":"application/octet-stream"},body:file});
    const state=await response.json();
    if (!response.ok) throw new Error(state.error || "تعذرت إضافة الملف.");
    render(state);$("task-file").value="";
    $("artifact-status").textContent="أضيفت نسخة محلية للمهمة. إذا كنت تتسلم المتصفح فاضغط استئناف بعد الانتهاء.";
  } catch(e) {$("artifact-status").textContent=e.message;}
  finally {fileAdding=false;$("file-add").disabled=!["running","waiting"].includes(currentState?.status) || (currentState?.artifacts || []).length>=5;}
};
$("reject").onclick=()=>control("reject");
$("approve").onclick=()=>control(pending?.type==="handoff" ? "resume" : "approve");
$("consent-mode").onchange=()=>{
  $("consent-help").textContent=$("consent-mode").value==="review"
    ? "يطلب موافقتك قبل كل نقرة أو تعبئة أو اختيار. التنقل المباشر والقراءة والتمرير تلقائية."
    : "يفتح القوائم والتبويبات المعروفة تلقائيًا. الحفظ والإرسال والتعبئة والعناصر غير الواضحة تحتاج موافقتك. يمكنك إيقاف المهمة في أي وقت.";
};
function exportText(state=currentState) {
  const report=state?.report;
  const lines=report ? [report.summary,"", "نطاق الفحص",report.scope,report.work_done] : [state?.result || ""];
  for (const finding of report?.findings || []) {
    lines.push("",finding.title,finding.detail,...finding.metrics.map(m=>`${m.label}: ${m.value}`));
  }
  const completion=state?.completion || report?.completion;
  if (completion) lines.push("","حالة الإنجاز والتحقق",completionName(completion.status),completion.reason || "", "", "ما أُنجز",...(completion.done || []).map(v=>"- "+v), "", "ما بقي",...(completion.remaining || []).map(v=>"- "+v));
  if (report?.limitations?.length) lines.push("","حدود النتيجة",...report.limitations.map(v=>`- ${v}`));
  if (state?.sources?.length) lines.push("","المصادر المقروءة",...state.sources.map(s=>`${s.id} — ${s.title}\n${s.url}`));
  return lines.join("\n");
}
let historyLoaded=false, historyBusy=false, historyCursor="", historyNext=null, historyPast=[];
let savedState=null, historyRequest=0;
let deleteTarget=null, historyDeleting=false, deleteReturnFocus=null;
let retentionSaving=false, historyExpiries=new Map();
function retentionDescription(policy) {
  return policy?.expires_at ? `الحذف التلقائي في ${new Date(policy.expires_at*1000).toLocaleString("ar")}. التنظيف يعمل أثناء تشغيل الخدمة أو عند فتحها مجددًا.` : "لا يوجد حذف تلقائي لهذه النتيجة؛ تبقى حتى تحذفها.";
}
function expireHistoryView() {
  const now=Date.now()/1000;
  const expired=id=>historyExpiries.has(id) && historyExpiries.get(id)<=now;
  const selectedExpired=savedState?.retention?.expires_at && savedState.retention.expires_at<=now;
  const targetExpired=deleteTarget?.retention?.expires_at && deleteTarget.retention.expires_at<=now;
  if (!selectedExpired && !targetExpired && ![...historyExpiries.values()].some(deadline=>deadline<=now)) return;
  if (deleteTarget && (targetExpired || expired(deleteTarget.id))) {deleteTarget=null;$("history-delete-preview").hidden=true;}
  if (selectedExpired) {
    savedState=null;historyRequest++;$("history-detail").hidden=true;
    $("history-result").replaceChildren();$("history-files").replaceChildren();
    clearCheckpointReview();
  }
  historyExpiries.clear();historyLoaded=false;$("history-list").replaceChildren();
  $("history-status").textContent="انتهت مدة الاحتفاظ بإحدى النتائج؛ جارٍ تحديث السجل.";
  if ($("history").open) loadHistory();
}
function clearCheckpointReview() {
  $("checkpoint-text").textContent="";$("checkpoint-review").hidden=true;
  $("checkpoint-edit-brief").value="";$("checkpoint-edit-status").textContent="";
  $("checkpoint-editor").open=false;checkpointReviewStale=false;
  $("checkpoint-recovery").hidden=true;$("checkpoint-recovery-reason").textContent="";
}
function updateDeleteControls() {
  const active=["running","waiting"].includes(currentState?.status);
  if (checkpointTaskId!==currentState?.id) {
    checkpointTaskId=currentState?.id;
    $("checkpoint-form").reset();$("checkpoint-status").textContent="";$("checkpoint-save").open=false;
  }
  $("checkpoint-save").hidden=!currentState?.id;
  $("checkpoint-submit").disabled=checkpointBusy || currentState?.status==="running";
  const checkpointUnavailable=checkpointBusy || active || !savedState?.checkpoint || savedState.checkpoint.status!=="ready" || checkpointReviewStale;
  const checkpointDirty=$("checkpoint-edit-brief").value!==(savedState?.checkpoint?.brief || "");
  $("checkpoint-resume").disabled=checkpointUnavailable || $("checkpoint-editor").open || checkpointDirty;
  $("checkpoint-edit-submit").disabled=checkpointUnavailable;
  $("checkpoint-edit-brief").disabled=checkpointBusy || active;
  $("checkpoint-reload").disabled=checkpointBusy;
  $("checkpoint-prepare").disabled=checkpointBusy || active || checkpointReviewStale || !savedState?.checkpoint?.recovery?.can_prepare;
  $("checkpoint-attempt-report").disabled=checkpointBusy || !savedState?.checkpoint?.recovery?.report_available;
  $("history-delete-confirm").disabled=historyDeleting || historyBusy || retentionSaving || checkpointBusy || active || !deleteTarget;
  $("history-delete-cancel").disabled=historyDeleting;
  $("history-delete-wait").textContent=active ? "أوقف المهمة الجارية أو انتظر انتهاءها قبل حذف تقرير وملفاته." : "";
  $("retention-save").disabled=active || historyDeleting || retentionSaving || checkpointBusy || !savedState;
  $("retention-days").disabled=active || historyDeleting || retentionSaving;
}
function cancelReportDeletion() {
  if (historyDeleting) return;
  deleteTarget=null;$("history-delete-preview").hidden=true;
  if (deleteReturnFocus?.isConnected) deleteReturnFocus.focus();
  else $("history").querySelector("summary").focus();
}
async function loadHistory(cursor="", past=[], afterDeletion=false) {
  if (historyBusy || retentionSaving || (historyDeleting && !afterDeletion)) return;
  historyBusy=true;
  updateDeleteControls();
  for (const id of ["history-refresh","history-next","history-previous"]) $(id).disabled=true;
  $("history-status").textContent="جارٍ قراءة النتائج المحفوظة…";
  try {
    const page=await api("/api/history?cursor="+encodeURIComponent(cursor));
    historyCursor=cursor;historyPast=past;historyNext=page.next_cursor;historyLoaded=true;
    historyExpiries=new Map(page.items.filter(item=>item.retention?.expires_at).map(item=>[item.id,item.retention.expires_at]));
    $("history-list").replaceChildren();
    for (const item of page.items) {
      const row=node("li");
      const open=node("button",item.summary || "نتيجة بلا ملخص");open.disabled=!item.available;
      open.onclick=()=>openSavedReport(item.id);
      const deletionPending=item.status==="deletion_pending";
      row.append(open,node("p",deletionPending ? "الحذف المحلي لم يكتمل؛ المحتوى محجوب عن الاستخدام." : `${new Date(item.recorded_at*1000).toLocaleString("ar")} · ${item.available ? completionName(item.status) : "غير متاح"}`,"hint"));
      const remove=node("button",deletionPending ? "استكمال الحذف" : "حذف هذه النتيجة","danger");
      remove.onclick=()=>{
        if (historyDeleting) return;
        deleteTarget=item;deleteReturnFocus=remove;
        $("history-delete-target").textContent=item.summary || "نتيجة بلا ملخص";
        $("history-delete-preview").hidden=false;updateDeleteControls();
        $("history-delete-cancel").focus();
      };
      row.append(remove);
      $("history-list").append(row);
    }
    $("history-status").textContent=page.items.length ? "النتائج مرتبة من الأحدث؛ فتحها لا يستأنف التنفيذ." : "لا توجد نتائج محفوظة في هذه الصفحة.";
  } catch(e) {$("history-status").textContent=e.message;}
  finally {
    historyBusy=false;$("history-refresh").disabled=false;
    $("history-next").disabled=!historyNext;$("history-previous").disabled=!historyPast.length;
    updateDeleteControls();
  }
}
async function openSavedReport(identifier, afterCheckpoint=false) {
  if (historyDeleting || retentionSaving || (checkpointBusy && !afterCheckpoint)) return;
  const request=++historyRequest;
  savedState=null;$("history-detail").hidden=true;
  $("history-result").replaceChildren();$("history-files").replaceChildren();
  clearCheckpointReview();
  $("history-status").textContent="جارٍ فتح التقرير المحفوظ…";
  try {
    const state=await api("/api/history/"+identifier);
    if (request!==historyRequest) return;
    if (state.retention?.expires_at && state.retention.expires_at<=Date.now()/1000) throw new Error("انتهت مدة الاحتفاظ بهذه النتيجة.");
    savedState=state;renderResult(state,true);
    $("checkpoint-review").hidden=!state.checkpoint;
    $("checkpoint-text").textContent=state.checkpoint?.brief || "";
    checkpointReviewStale=false;
    $("checkpoint-editor").hidden=state.checkpoint?.status!=="ready";
    $("checkpoint-editor").open=false;
    $("checkpoint-edit-brief").value=state.checkpoint?.brief || "";
    $("checkpoint-edit-status").textContent="";
    $("checkpoint-resume-status").textContent=state.checkpoint?.status==="claimed" ? "سبق بدء متابعة هذه النقطة. راجع المهمة وسجل الأثر؛ لن يُعاد تشغيلها من هذا الزر." : "";
    $("checkpoint-recovery").hidden=!state.checkpoint?.continuation;
    $("checkpoint-recovery-reason").textContent=state.checkpoint?.recovery?.reason || "";
    $("checkpoint-recovery-uncertain").hidden=!state.checkpoint?.recovery?.uncertain;
    $("retention-days").value=state.retention?.days?.toString() || "";
    $("retention-status").textContent=retentionDescription(state.retention);
    $("history-file-status").textContent=state.artifacts_error || "";
    renderFileList($("history-files"),state.artifacts,$("history-file-status"),async()=>{
      if (savedState?.id===identifier) await openSavedReport(identifier);
    });
    $("history-detail").hidden=false;
    updateDeleteControls();
    $("history-status").textContent="التقرير معروض للمراجعة؛ المهمة الحالية لم تتغير.";
  } catch(e) {if(request===historyRequest) $("history-status").textContent=e.message;}
}
$("history").addEventListener("toggle",()=>{if($("history").open && !historyLoaded) loadHistory();});
$("history").addEventListener("keydown",event=>{
  if (event.key!=="Escape") return;
  event.preventDefault();event.stopPropagation();
  cancelReportDeletion();
  $("history").open=false;$("history").querySelector("summary").focus();
});
$("history-delete-cancel").onclick=cancelReportDeletion;
$("checkpoint-form").onsubmit=async event=>{
  event.preventDefault();
  if (checkpointBusy || $("checkpoint-submit").disabled || !currentState?.id) return;
  checkpointBusy=true;updateDeleteControls();
  try {
    const result=await api("/api/checkpoints/save",{id:currentState.id,brief:$("checkpoint-brief").value,days:Number($("checkpoint-days").value)});
    render(result.state);$("checkpoint-brief").value="";
    $("checkpoint-status").textContent="حُفظت نقطة المتابعة. افتحها من النتائج السابقة للاستئناف أو حذف التقرير والنقطة وملفاته.";
    historyLoaded=false;
    if ($("history").open) await loadHistory();
  } catch(error) {$("checkpoint-status").textContent=error.message;}
  finally {checkpointBusy=false;updateDeleteControls();}
};
$("checkpoint-resume").onclick=async()=>{
  if ($("checkpoint-resume").disabled || !savedState?.checkpoint) return;
  checkpointBusy=true;updateDeleteControls();
  const identifier=savedState.id;
  try {
    const state=await api("/api/checkpoints/resume",{id:identifier,provider:$("provider").value,revision:savedState.checkpoint.revision});
    render(state);
    await openSavedReport(identifier,true);
  } catch(error) {
    if (error.code==="checkpoint_conflict") checkpointReviewStale=true;
    $("checkpoint-resume-status").textContent=error.message;
  }
  finally {checkpointBusy=false;updateDeleteControls();}
};
$("checkpoint-editor").addEventListener("toggle",updateDeleteControls);
$("checkpoint-edit-brief").addEventListener("input",updateDeleteControls);
$("checkpoint-reload").onclick=()=>{if(savedState && !checkpointBusy) openSavedReport(savedState.id);};
$("checkpoint-attempt-report").onclick=()=>{
  const identifier=savedState?.checkpoint?.recovery?.task_id;
  if (identifier && !$("checkpoint-attempt-report").disabled) openSavedReport(identifier);
};
$("checkpoint-prepare").onclick=async()=>{
  if ($("checkpoint-prepare").disabled || !savedState?.checkpoint) return;
  const identifier=savedState.id, revision=savedState.checkpoint.revision;
  checkpointBusy=true;updateDeleteControls();
  try {
    await api("/api/checkpoints/prepare",{id:identifier,revision});
    await openSavedReport(identifier,true);
    $("checkpoint-resume-status").textContent="أُعيد تجهيز النقطة؛ راجع ما بقي وعدّل النص قبل اختيار الاستئناف. لم يبدأ المتصفح.";
  } catch(error) {
    if (error.code==="checkpoint_conflict") checkpointReviewStale=true;
    $("checkpoint-recovery-reason").textContent=error.message;
  }
  finally {checkpointBusy=false;updateDeleteControls();}
};
$("checkpoint-edit-form").onsubmit=async event=>{
  event.preventDefault();
  if ($("checkpoint-edit-submit").disabled || !savedState?.checkpoint) return;
  const identifier=savedState.id, revision=savedState.checkpoint.revision;
  checkpointBusy=true;updateDeleteControls();
  try {
    await api("/api/checkpoints/update",{id:identifier,brief:$("checkpoint-edit-brief").value,revision});
    await openSavedReport(identifier,true);
    $("checkpoint-resume-status").textContent="حُفظ النص المعدّل؛ راجعه قبل الاستئناف.";
    historyLoaded=false;
    if ($("history").open) await loadHistory();
  } catch(error) {
    if (error.code==="checkpoint_conflict") checkpointReviewStale=true;
    $("checkpoint-edit-status").textContent=error.message;
  }
  finally {checkpointBusy=false;updateDeleteControls();}
};
$("retention-form").onsubmit=async event=>{
  event.preventDefault();
  if (!savedState || $("retention-save").disabled) return;
  const identifier=savedState.id, days=$("retention-days").value;
  retentionSaving=true;updateDeleteControls();
  try {
    const policy=await api("/api/history/retention",{id:identifier,days:days ? Number(days) : null});
    if (savedState?.id===identifier) {
      savedState.retention=policy;
      if (deleteTarget?.id===identifier) deleteTarget.retention=policy;
      if (policy.expires_at) historyExpiries.set(identifier,policy.expires_at);
      else historyExpiries.delete(identifier);
      $("retention-status").textContent=retentionDescription(policy);
    }
  } catch(error) {$("retention-status").textContent=error.message;}
  finally {retentionSaving=false;updateDeleteControls();}
};
$("history-delete-confirm").onclick=async()=>{
  if (!deleteTarget || $("history-delete-confirm").disabled) return;
  const identifier=deleteTarget.id;
  historyDeleting=true;historyRequest++;updateDeleteControls();
  try {
    const result=await api("/api/history/delete",{id:identifier});
    render(result.state);
    if (savedState?.id===identifier) {
      savedState=null;historyRequest++;$("history-detail").hidden=true;
      $("history-result").replaceChildren();$("history-files").replaceChildren();
      clearCheckpointReview();
    }
    deleteTarget=null;$("history-delete-preview").hidden=true;
    await loadHistory("",[],true);
    $("history-status").textContent=result.cleanup_pending
      ? "حُجب التقرير وملفاته عن الاستخدام، لكن الحذف من القرص لم يكتمل. أعد المحاولة من استكمال الحذف."
      : "حُذف التقرير وملفاته المحلية. بقي سجل منع تكرار العمليات.";
    $("history-refresh").focus();
  } catch(e) {$("history-status").textContent=e.message;}
  finally {historyDeleting=false;updateDeleteControls();}
};
$("history-refresh").onclick=()=>loadHistory();
$("history-next").onclick=()=>{if(historyNext) loadHistory(historyNext,[...historyPast,historyCursor]);};
$("history-previous").onclick=()=>{if(historyPast.length) loadHistory(historyPast.at(-1),historyPast.slice(0,-1));};
$("history-download").onclick=()=>{
  if (!savedState) return;
  const heading=`نتيجة محفوظة · ${new Date((savedState.finished_at || savedState.recorded_at)*1000).toLocaleString("ar")} · ${completionName(savedState.status)}\nلم يُعد التحقق منها الآن.\n\n`;
  const url=URL.createObjectURL(new Blob([heading+exportText(savedState)],{type:"text/plain;charset=utf-8"}));
  const link=node("a");link.href=url;link.download=`parallax-report-${savedState.id}.txt`;link.click();
  setTimeout(()=>URL.revokeObjectURL(url),1000);
};
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
