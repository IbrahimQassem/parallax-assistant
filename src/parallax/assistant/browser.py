"""A single visible browser, observed elements only, no model-supplied scripts."""
from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import os
import re
import socket
import sys
from pathlib import Path
from urllib.parse import urlsplit

from playwright.async_api import async_playwright

from .actions import Action, web_url
from .file_transfer import retrieve_file


OBSERVE = r"""() => {
  const visible = el => el.getClientRects().length && getComputedStyle(el).visibility !== 'hidden';
  const labelText = label => {
    const walker=document.createTreeWalker(label,NodeFilter.SHOW_TEXT), parts=[];
    while(walker.nextNode()) {
      const parent=walker.currentNode.parentElement;
      if(parent && visible(parent) && !parent.closest('input,textarea,select,button,script,style,[contenteditable="true"]')) parts.push(walker.currentNode.textContent);
    }
    return parts.join(' ').replace(/\s+/g,' ').trim();
  };
  const sensitive = el => /password|hidden|file/.test(el.type || '') ||
    /password|one-time|cc-|credit|card.?number|cvv|cvc|\botp\b|secret|token|\bpin\b|pin[-_]?code/i.test(
      [el.name,el.id,el.autocomplete,el.getAttribute('aria-label')].join(' '));
  const allNodes = [...document.querySelectorAll('a[href],button,input,textarea,select,summary,[role="button"],[role="link"],[role="tab"],[role="menuitem"],[contenteditable="true"]')].filter(visible);
  const nodes = allNodes.slice(0, 200);
  const owner = node => node.form || node.closest('form');
  // Named form controls can shadow form.action, form.method and even methods.
  const formProperty = (form,key) => Object.getOwnPropertyDescriptor(HTMLFormElement.prototype,key).get.call(form);
  const formAttribute = (form,key) => Element.prototype.getAttribute.call(form,key) || '';
  const destinationFor = node => node.hasAttribute('formaction') ? node.formAction : owner(node) ? formProperty(owner(node),'action') : '';
  const forms = [...document.forms];
  const formInfo = node => {
    const form=owner(node);
    if (!form) return null;
    const destination=new URL(destinationFor(node));
    return {ref:String(forms.indexOf(form)), dom_id:formAttribute(form,'id'), name:formAttribute(form,'name'),
      label:formAttribute(form,'aria-label'), method:node.hasAttribute('formmethod') ? node.formMethod : formProperty(form,'method'),
      action:destination.origin+destination.pathname,
      credentialed:!!(destination.username || destination.password),
      controls:allNodes.filter(n=>owner(n)===form).length};
  };
  const blockSelector = 'p,tr,li,[role="status"],[role="alert"]';
  const blocks = [...document.querySelectorAll(blockSelector + ',div')]
    .filter(el => visible(el) && !el.querySelector(blockSelector) &&
      (el.matches(blockSelector) || !el.children.length) &&
      !el.closest('button,a,input,textarea,select,[contenteditable="true"]') &&
      !el.querySelector('input,textarea,select,[contenteditable="true"]'))
    .map(el => (el.innerText || '').trim()).filter(text => text && text.length <= 1200).slice(0, 80);
  const clean = text => (text || '').replace(/\s+/g, ' ').trim();
  const displayCell = el => visible(el) && !el.isContentEditable && !el.closest('form') &&
    !el.matches('input,textarea,select,button,a,[contenteditable="true"]') &&
    !el.querySelector('input,textarea,select,button,a,[contenteditable="true"],table,dl');
  const records = [];
  const addRecord = pairs => {
    if (pairs.length < 2 || pairs.length > 12 || new Set(pairs.map(p=>p[0])).size !== pairs.length) return;
    if (pairs.some(([label,value])=>!label || label.length>100 || value.length>600)) return;
    if (pairs.reduce((size,p)=>size+p[0].length+p[1].length,0)>2400) return;
    if (records.length<80) records.push({fields:Object.fromEntries(pairs)});
  };
  for (const table of [...document.querySelectorAll('table')].filter(visible).slice(0,20)) {
    const rows = [...table.rows].filter(row=>row.closest('table')===table);
    const header = rows.find(row=>row.cells.length>=2 && [...row.cells].every(cell=>cell.tagName==='TH'));
    if (!header || header.cells.length>12 || [...header.cells].some(cell=>!displayCell(cell) || cell.colSpan!==1 || cell.rowSpan!==1)) continue;
    const labels = [...header.cells].map(cell=>clean(cell.innerText));
    if (new Set(labels).size!==labels.length) continue;
    for (const row of rows.slice(rows.indexOf(header)+1,rows.indexOf(header)+101)) {
      const cells = [...row.cells];
      if (!visible(row) || cells.length!==labels.length || cells.some(cell=>cell.colSpan!==1 || cell.rowSpan!==1)) continue;
      addRecord(cells.flatMap((cell,i)=>displayCell(cell) ? [[labels[i],clean(cell.innerText)]] : []));
    }
  }
  for (const list of [...document.querySelectorAll('dl')].filter(visible).slice(0,80)) {
    if (list.querySelector('dl')) continue;
    const terms = [...list.querySelectorAll('dt')];
    const pairs = [];
    let valid = true;
    for (const term of terms) {
      const value = term.nextElementSibling;
      if (!value || value.tagName!=='DD' || value.nextElementSibling?.tagName==='DD') { valid=false;break; }
      if (displayCell(term) && displayCell(value)) pairs.push([clean(term.innerText),clean(value.innerText)]);
    }
    if (valid) addRecord(pairs);
  }
  return {
    text: (document.body?.innerText || '').slice(0, 16000),
    blocks,
    records,
    formState: nodes.filter(n=>!sensitive(n)).map(n=>[n.value || '', !!n.checked]),
    items: nodes.map(node => ({node,
      privateState: sensitive(node) ? null : [node.value || '', !!node.checked],
      privateFiles: node.type === 'file' ? [...node.files].map(f=>[f.name,f.size,f.type,f.lastModified]) : null,
      privateDestination: owner(node) ? destinationFor(node) : '', info: {
      tag: node.tagName.toLowerCase(), type: node.type || node.getAttribute('role') || '',
      role: node.getAttribute('role') || '',
      name: node.getAttribute('name') || '', multiple: !!node.multiple,
      accept: node.getAttribute('accept') || '', directory: node.hasAttribute('webkitdirectory'),
      selected_files: node.type === 'file' ? node.files.length : 0,
      form: formInfo(node),
      has_popup: node.getAttribute('aria-haspopup') || '',
      expanded: node.getAttribute('aria-expanded') || '',
      controls_role: document.getElementById(node.getAttribute('aria-controls'))?.getAttribute('role') || '',
      in_form: !!(node.form || node.closest('form')),
      editable: node.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(node.tagName),
      download: node.hasAttribute('download'),
      label: (node.getAttribute('aria-label') || [...(node.labels || [])].map(labelText).join(' ') ||
        node.innerText || node.placeholder || node.getAttribute('title') || '').slice(0, 500),
      href: node.tagName === 'A' ? node.href : '',
      sensitive: sensitive(node), disabled: !!node.disabled,
      options: node.tagName === 'SELECT' ? [...node.options].map(o=>({value:o.value,label:o.text})).slice(0,100) : []
    }}))
  };
}"""


class PersonalBrowser:
    def __init__(self, profile: Path, *, headless: bool = False, allow_local: bool = False,
                 existing_chrome: bool = False, chrome_data_dir: Path | None = None):
        self.profile = profile
        self.headless = headless
        self.allow_local = allow_local
        self.existing_chrome = existing_chrome
        self.chrome_data_dir = chrome_data_dir
        self.connection = None
        self.managed_pages = []
        self.context = None
        self.playwright = None
        self.page = None
        self.elements = {}
        self.tab_ids = {}
        self.blocked_origin = ""
        self._hosts = {}

    def _chrome_endpoint(self):
        directory = self.chrome_data_dir
        if directory is None:
            if sys.platform == "darwin":
                directory = Path.home() / "Library/Application Support/Google/Chrome"
            elif sys.platform == "win32":
                directory = Path(os.environ.get("LOCALAPPDATA", "")) / "Google/Chrome/User Data"
            else:
                directory = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))) / "google-chrome"
        try:
            port, path = (directory / "DevToolsActivePort").read_text().strip().splitlines()
            if not port.isdecimal() or not 1 <= int(port) <= 65535:
                raise ValueError()
            if not re.fullmatch(r"/devtools/browser/[A-Za-z0-9-]+", path):
                raise ValueError()
        except (OSError, ValueError):
            raise RuntimeError("افتح Google Chrome 144 أو أحدث، ثم chrome://inspect/#remote-debugging وفعّل Allow remote debugging. بعدها أعد بدء المهمة.") from None
        return f"ws://127.0.0.1:{port}{path}"

    async def start(self):
        if self.context:
            return
        if self.playwright:
            await self.playwright.stop()
        self.managed_pages = []
        endpoint = self._chrome_endpoint() if self.existing_chrome else None
        if not self.existing_chrome:
            self.profile.mkdir(parents=True, exist_ok=True, mode=0o700)
            self.profile.chmod(0o700)
        self.playwright = await async_playwright().start()
        try:
            if self.existing_chrome:
                try:
                    self.connection = await self.playwright.chromium.connect_over_cdp(
                        endpoint, no_defaults=True, timeout=60000,
                    )
                except Exception:
                    raise RuntimeError("تعذّر الاتصال بـ Chrome. فعّل Remote debugging واسمح بطلب الاتصال داخل Chrome، ثم أعد بدء المهمة.") from None
                if not self.connection.contexts:
                    raise RuntimeError("لم تتوفر جلسة Chrome. افتح نافذة عادية ثم أعد المحاولة.")
                self.context = self.connection.contexts[0]
                self.context.on("close", self._context_closed)
                # A new tab shares the signed-in profile without observing unrelated tabs.
                self.page = await self.context.new_page()
                await self._manage_page(self.page)
                await self.page.bring_to_front()
                return
            self.context = await self.playwright.chromium.launch_persistent_context(
                str(self.profile), headless=self.headless, accept_downloads=False,
                viewport={"width": 1280, "height": 850}, service_workers="block",
                chromium_sandbox=True,
            )
            await self.context.route("**/*", self._route)
            self.context.set_default_timeout(8000)
            self.context.on("close", self._context_closed)
            self.context.on("page", self._new_page)
            for page in self.context.pages:
                self._new_page(page)
            self.page = self.context.pages[0] if self.context.pages else await self.context.new_page()
        except BaseException:
            await self.close()
            raise

    def _context_closed(self, context):
        if self.context is context:
            self.context = None

    def _new_page(self, page):
        self.page = page
        page.on("dialog", lambda dialog: dialog.dismiss())
        page.on("download", lambda download: download.cancel())

    async def _manage_page(self, page):
        if page in self.managed_pages:
            return
        self.managed_pages.append(page)
        page.set_default_timeout(8000)
        await page.route("**/*", self._route)
        page.on("popup", self._manage_page)
        self._new_page(page)

    async def _allowed(self, url):
        try:
            parsed = urlsplit(web_url(url))
            if self.blocked_origin and f"{parsed.scheme}://{parsed.netloc}" == self.blocked_origin:
                return False
            host = parsed.hostname
            if self.allow_local:
                return True
            if host not in self._hosts:
                answers = await asyncio.get_running_loop().getaddrinfo(host, parsed.port or 443, type=socket.SOCK_STREAM)
                self._hosts[host] = bool(answers) and all(ipaddress.ip_address(a[4][0]).is_global for a in answers)
            return self._hosts[host]
        except (ValueError, OSError):
            return False

    async def _route(self, route):
        if await self._allowed(route.request.url):
            await route.continue_()
        else:
            await route.abort("blockedbyclient")

    async def observe(self) -> dict:
        if not self.context:
            await self.start()
        if not self.page or self.page.is_closed():
            pages = self.managed_pages if self.existing_chrome else self.context.pages
            self.page = next((p for p in pages if not p.is_closed()), None) or await self.context.new_page()
            if self.existing_chrome:
                await self._manage_page(self.page)
        for handle in self.elements.values():
            await handle.dispose()
        self.elements = {}
        pages = self.managed_pages if self.existing_chrome else self.context.pages
        self.tab_ids = {str(i): p for i, p in enumerate(pages) if not p.is_closed()}
        if self.page.url != "about:blank" and not await self._allowed(self.page.url):
            raise ValueError("الصفحة الحالية غير مسموحة للمساعد. افتح موقعًا عامًا في تبويب المساعد.")
        items, texts, form_hashes, evidence_blocks, evidence_records = [], [], [], [], []
        for frame in self.page.frames[:10]:
            try:
                observation = await frame.evaluate_handle(OBSERVE)
                text_handle = await observation.get_property("text")
                texts.append(await text_handle.json_value())
                blocks_handle = await observation.get_property("blocks")
                evidence_blocks.extend({"frame": frame.url, "main": frame == self.page.main_frame, "text": text}
                                       for text in await blocks_handle.json_value())
                await blocks_handle.dispose()
                records_handle = await observation.get_property("records")
                evidence_records.extend({**record, "frame": frame.url, "main": frame == self.page.main_frame}
                                        for record in await records_handle.json_value())
                await records_handle.dispose()
                form_handle = await observation.get_property("formState")
                form_hashes.append(hashlib.sha256(json.dumps(await form_handle.json_value()).encode()).hexdigest())
                await form_handle.dispose()
                entries = await observation.get_property("items")
                for entry in (await entries.get_properties()).values():
                    node = (await entry.get_property("node")).as_element()
                    info_handle = await entry.get_property("info")
                    info = await info_handle.json_value()
                    state_handle = await entry.get_property("privateState")
                    value = await state_handle.json_value()
                    info["state_hash"] = hashlib.sha256(json.dumps(value).encode()).hexdigest() if value is not None else None
                    await state_handle.dispose()
                    files_handle = await entry.get_property("privateFiles")
                    files = await files_handle.json_value()
                    info["file_state_hash"] = hashlib.sha256(json.dumps(files).encode()).hexdigest() if files is not None else None
                    await files_handle.dispose()
                    destination_handle = await entry.get_property("privateDestination")
                    destination = await destination_handle.json_value()
                    if info.get("form"):
                        info["form"]["action_hash"] = hashlib.sha256(destination.encode()).hexdigest()
                    await destination_handle.dispose()
                    identifier = str(len(items))
                    self.elements[identifier] = node
                    items.append({"id": identifier, "frame": frame.url, **info})
                    await info_handle.dispose()
                    await entry.dispose()
                await text_handle.dispose()
                await entries.dispose()
                await observation.dispose()
            except Exception:
                # Navigating/cross-origin frames may disappear while observing.
                continue
        bounded_blocks, remaining = [], 16000
        for block in evidence_blocks:
            if len(bounded_blocks) >= 200:
                break
            if len(block["text"]) <= remaining:
                bounded_blocks.append(block)
                remaining -= len(block["text"])
        bounded_records, remaining = [], 12000
        for record in evidence_records:
            size = sum(len(key) + len(value) for key, value in record["fields"].items())
            if len(bounded_records) < 80 and size <= remaining:
                bounded_records.append(record)
                remaining -= size
        snapshot = {
            "url": self.page.url,
            "title": await self.page.title(),
            "text": "\n".join(texts)[:24000],
            "elements": items,
            "form_hashes": form_hashes,
            "evidence_blocks": bounded_blocks,
            "evidence_records": bounded_records,
            "tabs": [{"id": i, "url": p.url} for i, p in self.tab_ids.items()],
        }
        snapshot["fingerprint"] = hashlib.sha256(json.dumps(snapshot, sort_keys=True).encode()).hexdigest()
        return snapshot

    def preview(self, action: Action, snapshot: dict) -> dict:
        target = next((e for e in snapshot["elements"] if e["id"] == action.target), None)
        if action.kind in {"click", "fill", "select", "press", "download", "upload"}:
            if target is None or target["disabled"]:
                raise ValueError("العنصر غير متاح. يلزم قراءة الصفحة مجددًا.")
            if action.kind == "upload":
                if target.get("tag") != "input" or target.get("type") != "file" or target.get("directory"):
                    raise ValueError("يلزم حقل ملف مرصود؛ رفع المجلدات أو حقل غير ملف غير مسموح.")
            elif target["sensitive"]:
                raise ValueError("هذا حقل حساس: استخدم تسليم المتصفح لإكماله يدويًا.")
            if action.kind == "select" and action.value not in {x["value"] for x in target["options"]}:
                raise ValueError("الخيار غير موجود في الصفحة.")
            if action.kind == "download":
                if getattr(self, "existing_chrome", False):
                    raise ValueError("تنزيل الملفات المُدار متاح في جلسة المساعد المستقلة؛ استعمل التسلم اليدوي في Chrome المفتوح.")
                if target.get("tag") != "a" or action.value:
                    raise ValueError("يلزم رابط ملف HTTP/HTTPS مرصود؛ الأزرار والملفات المولدة داخل الصفحة تحتاج تسلمًا يدويًا حاليًا.")
                web_url(target.get("href", ""))
        return {"action": action.to_dict(), "page": snapshot["url"], "target": target,
                "fingerprint": snapshot["fingerprint"]}

    async def upload_file(self, action, name, mime, content):
        element = self.elements[action.target]
        if not await element.evaluate("el => el.isConnected && el.tagName === 'INPUT' && el.type === 'file' && !el.disabled && !el.webkitdirectory"):
            raise ValueError("تغير حقل الرفع قبل التنفيذ.")
        await element.set_input_files({"name": name, "mimeType": mime, "buffer": content})
        await asyncio.sleep(0.35)

    async def download_to(self, action, destination, max_bytes, expected_url):
        if self.existing_chrome or not self.context:
            raise ValueError("التنزيل غير متاح في حالة المتصفح الحالية.")
        element = self.elements[action.target]
        url = await element.evaluate("el => el.isConnected && el.tagName === 'A' ? el.href : ''")
        web_url(url)
        if url != expected_url:
            raise ValueError("تغير رابط التنزيل بعد المعاينة.")
        return await retrieve_file(url, destination, max_bytes, self._allowed, self.context.cookies)

    async def execute(self, action: Action):
        if action.kind == "navigate":
            if not await self._allowed(action.value):
                raise ValueError("الرابط غير مسموح: لا يمكن الوصول إلى الشبكات المحلية أو صفحة التحكم.")
            await self.page.goto(action.value, wait_until="domcontentloaded", timeout=30000)
        elif action.kind == "switch_tab":
            if action.target not in self.tab_ids:
                raise ValueError("علامة التبويب لم تعد متاحة.")
            self.page = self.tab_ids[action.target]
            await self.page.bring_to_front()
        elif action.kind == "scroll":
            await self.page.mouse.wheel(0, 650 if action.value == "down" else -650)
        elif action.kind == "wait":
            await asyncio.sleep(1)
        elif action.kind in {"click", "fill", "select", "press"}:
            element = self.elements[action.target]
            if not await element.evaluate("el => el.isConnected"):
                raise ValueError("تغيّر العنصر قبل تنفيذ الخطوة.")
            if action.kind == "click":
                await element.click()
            elif action.kind == "fill":
                await element.fill(action.value)
            elif action.kind == "select":
                await element.select_option(value=action.value)
            else:
                await element.press(action.value)
        # A short settling interval is bounded; never retry a submitted action automatically.
        await asyncio.sleep(0.35)

    async def close(self):
        context, playwright = self.context, self.playwright
        self.context = self.playwright = None
        try:
            if context and self.existing_chrome:
                for page in self.managed_pages:
                    if not page.is_closed():
                        await page.unroute("**/*", self._route)
                # Stopping the driver disconnects CDP; never close the user's context.
            elif context:
                await context.close()
        except Exception as error:
            # Ctrl+C can reach Chromium/the driver before this cleanup coroutine.
            if not any(text in str(error).lower() for text in ("connection closed", "has been closed", "target closed")):
                raise
        finally:
            if playwright:
                await playwright.stop()
            self.connection = None
            self.managed_pages = []
            self.elements = {}
            self.tab_ids = {}
            self.page = None
