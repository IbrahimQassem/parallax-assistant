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


OBSERVE = r"""() => {
  const visible = el => el.getClientRects().length && getComputedStyle(el).visibility !== 'hidden';
  const sensitive = el => /password|hidden|file/.test(el.type || '') ||
    /password|one-time|cc-|credit|card.?number|cvv|cvc|\botp\b|secret|token|\bpin\b|pin[-_]?code/i.test(
      [el.name,el.id,el.autocomplete,el.getAttribute('aria-label')].join(' '));
  const nodes = [...document.querySelectorAll('a[href],button,input,textarea,select,summary,[role="button"],[role="link"],[role="tab"],[role="menuitem"],[contenteditable="true"]')]
    .filter(visible).slice(0, 200);
  return {
    text: (document.body?.innerText || '').slice(0, 16000),
    formState: nodes.filter(n=>!sensitive(n)).map(n=>[n.value || '', !!n.checked]),
    items: nodes.map(node => ({node, info: {
      tag: node.tagName.toLowerCase(), type: node.type || node.getAttribute('role') || '',
      role: node.getAttribute('role') || '',
      has_popup: node.getAttribute('aria-haspopup') || '',
      expanded: node.getAttribute('aria-expanded') || '',
      controls_role: document.getElementById(node.getAttribute('aria-controls'))?.getAttribute('role') || '',
      in_form: !!(node.form || node.closest('form')),
      editable: node.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(node.tagName),
      download: node.hasAttribute('download'),
      label: (node.getAttribute('aria-label') || [...(node.labels || [])].map(x=>x.innerText).join(' ') ||
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
        items, texts, form_hashes = [], [], []
        for frame in self.page.frames[:10]:
            try:
                observation = await frame.evaluate_handle(OBSERVE)
                text_handle = await observation.get_property("text")
                texts.append(await text_handle.json_value())
                form_handle = await observation.get_property("formState")
                form_hashes.append(hashlib.sha256(json.dumps(await form_handle.json_value()).encode()).hexdigest())
                await form_handle.dispose()
                entries = await observation.get_property("items")
                for entry in (await entries.get_properties()).values():
                    node = (await entry.get_property("node")).as_element()
                    info_handle = await entry.get_property("info")
                    info = await info_handle.json_value()
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
        snapshot = {
            "url": self.page.url,
            "title": await self.page.title(),
            "text": "\n".join(texts)[:24000],
            "elements": items,
            "form_hashes": form_hashes,
            "tabs": [{"id": i, "url": p.url} for i, p in self.tab_ids.items()],
        }
        snapshot["fingerprint"] = hashlib.sha256(json.dumps(snapshot, sort_keys=True).encode()).hexdigest()
        return snapshot

    def preview(self, action: Action, snapshot: dict) -> dict:
        target = next((e for e in snapshot["elements"] if e["id"] == action.target), None)
        if action.kind in {"click", "fill", "select", "press"}:
            if target is None or target["disabled"]:
                raise ValueError("العنصر غير متاح. يلزم قراءة الصفحة مجددًا.")
            if target["sensitive"]:
                raise ValueError("هذا حقل حساس: استخدم تسليم المتصفح لإكماله يدويًا.")
            if action.kind == "select" and action.value not in {x["value"] for x in target["options"]}:
                raise ValueError("الخيار غير موجود في الصفحة.")
        return {"action": action.to_dict(), "page": snapshot["url"], "target": target,
                "fingerprint": snapshot["fingerprint"]}

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
