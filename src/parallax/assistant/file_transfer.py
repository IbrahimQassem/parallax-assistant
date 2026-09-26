"""Stream an approved HTTP download without following an unchecked redirect."""
import asyncio
from email.message import Message
from email.utils import collapse_rfc2231_value
import http.client
import threading
from urllib.parse import unquote, urljoin, urlsplit

from .actions import web_url


async def retrieve_file(url, destination, max_bytes, allowed, cookies):
    initial = urlsplit(web_url(url))
    origin = (initial.scheme, initial.netloc)
    cancelled = threading.Event()
    connection = [None]

    def transfer(current, cookie_header):
        parsed = urlsplit(current)
        client = http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
        conn = client(parsed.hostname, parsed.port, timeout=5)
        connection[0] = conn
        try:
            if cancelled.is_set(): raise ValueError("أُلغي تنزيل الملف.")
            path = parsed.path or "/"
            if parsed.query: path += "?" + parsed.query
            conn.request("GET", path, headers={"Cookie": cookie_header, "Accept-Encoding": "identity"})
            response = conn.getresponse()
            if response.status in {301, 302, 303, 307, 308}:
                location = response.getheader("Location")
                if not location: raise ValueError("إعادة توجيه التنزيل غير صالحة.")
                return {"redirect": urljoin(current, location)}
            if response.status != 200:
                raise ValueError("لم يرجع الموقع ملفًا ناجحًا؛ قد يلزم تسجيل الدخول أو مسار آخر.")
            if response.getheader("Content-Encoding", "identity").lower() != "identity":
                raise ValueError("ترميز استجابة التنزيل غير مدعوم حاليًا.")
            length = response.getheader("Content-Length")
            if length is not None and (not length.isdecimal() or int(length) > max_bytes):
                raise ValueError("حجم الملف غير صالح أو يتجاوز الحد المسموح.")
            count = 0
            with destination.open("wb") as stream:
                while True:
                    if cancelled.is_set(): raise ValueError("أُلغي تنزيل الملف.")
                    block = response.read1(min(65536, max_bytes - count + 1))
                    if not block: break
                    count += len(block)
                    if count > max_bytes: raise ValueError("تجاوز الملف حد الحجم المسموح.")
                    stream.write(block)
            if length is not None and count != int(length):
                raise ValueError("انقطع التنزيل قبل وصول الملف كاملًا.")
            header = Message()
            header["Content-Disposition"] = response.getheader("Content-Disposition", "")
            name = header.get_param("filename", header="Content-Disposition")
            if isinstance(name, tuple): name = collapse_rfc2231_value(name)
            return {"name": name or unquote(parsed.path.rsplit("/", 1)[-1]) or "download.bin"}
        finally:
            conn.close()
            connection[0] = None

    for _ in range(6):
        parsed = urlsplit(web_url(url))
        if (parsed.scheme, parsed.netloc) != origin or not await allowed(url):
            raise ValueError("وجهة التنزيل أو إعادة توجيهها خارج النطاق المعتمد.")
        selected = await cookies([url])
        cookie_header = "; ".join(f"{item['name']}={item['value']}" for item in selected)
        work = asyncio.create_task(asyncio.to_thread(transfer, url, cookie_header))
        try:
            result = await asyncio.shield(work)
        except asyncio.CancelledError:
            cancelled.set()
            if connection[0]: connection[0].close()
            await asyncio.gather(work, return_exceptions=True)
            raise
        except Exception:
            raise ValueError("تعذر إكمال تنزيل الملف ضمن حدود الحجم والوجهة؛ لم تُحفظ نسخة مكتملة.") from None
        if "name" in result: return result["name"]
        url = result["redirect"]
    raise ValueError("تجاوز التنزيل الحد المسموح لإعادة التوجيه.")
