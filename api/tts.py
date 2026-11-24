import asyncio
import json
import os
import re
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.request import Request, urlopen

import edge_tts

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

VOICES = [
    {
        "id": "vi-VN-HoaiMyNeural",
        "name": "Hoài My (Nữ • Tiếng Việt chuẩn)",
        "badge": "Chuẩn truyền cảm",
        "gender": "female",
        "locale": "vi-VN",
        "description": "Giọng nữ miền Bắc tự nhiên, rõ chữ, đọc đề thi & văn bản dài cực mượt",
    },
    {
        "id": "vi-VN-NamMinhNeural",
        "name": "Nam Minh (Nam • Tiếng Việt chuẩn)",
        "badge": "Trầm ấm rõ nét",
        "gender": "male",
        "locale": "vi-VN",
        "description": "Giọng nam trầm ấm, dứt khoát, phù hợp đọc giảng bài & thuyết minh",
    },
    {
        "id": "en-US-JennyNeural",
        "name": "Jenny (Nữ • Tiếng Anh Mỹ)",
        "badge": "Tiếng Anh Mỹ",
        "gender": "female",
        "locale": "en-US",
        "description": "Giọng nữ bản xứ Mỹ chuẩn cho đề thi Tiếng Anh",
    },
    {
        "id": "en-US-GuyNeural",
        "name": "Guy (Nam • Tiếng Anh Mỹ)",
        "badge": "Tiếng Anh Mỹ",
        "gender": "male",
        "locale": "en-US",
        "description": "Giọng nam bản xứ Mỹ tự nhiên",
    },
    {
        "id": "en-GB-SoniaNeural",
        "name": "Sonia (Nữ • Tiếng Anh Anh)",
        "badge": "Tiếng Anh Anh",
        "gender": "female",
        "locale": "en-GB",
        "description": "Giọng nữ chuẩn Anh-Anh (British English)",
    },
]

MALE_KEYWORDS = (
    "nam",
    "male",
    "hải đăng",
    "hai dang",
    "minh đức",
    "minh duc",
    "thiện minh",
    "thien minh",
    "quang sơn",
    "quang son",
    "thiền tâm",
    "adam",
    "namminh",
)


def resolve_voice(voice_input: str) -> str:
    if not voice_input:
        return "vi-VN-HoaiMyNeural"
    v = str(voice_input).strip()
    if v.endswith("Neural"):
        return v
    low = v.lower()
    if any(k in low for k in MALE_KEYWORDS):
        return "vi-VN-NamMinhNeural"
    return "vi-VN-HoaiMyNeural"


def format_rate(speed) -> str:
    try:
        val = float(speed)
    except (TypeError, ValueError):
        val = 1.0
    pct = int(round((val - 1.0) * 100))
    pct = max(-50, min(100, pct))
    return f"{pct:+d}%"


def clean_text(text: str) -> str:
    text = re.sub(r"\[(cười|hắng giọng|thở dài|nghi[^\]]*|khoảng nghỉ[^\]]*)\]", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def split_chunks(text: str, max_len: int = 1600):
    if len(text) <= max_len:
        return [text]
    sentences = re.split(r"(?<=[.!?\n;:])\s+", text)
    chunks = []
    current = ""
    for sent in sentences:
        sent = sent.strip()
        if not sent:
            continue
        if len(sent) > max_len:
            if current:
                chunks.append(current)
                current = ""
            words = sent.split(" ")
            part = ""
            for w in words:
                if len(part) + len(w) + 1 <= max_len:
                    part = f"{part} {w}".strip()
                else:
                    if part:
                        chunks.append(part)
                    part = w
            if part:
                chunks.append(part)
        elif len(current) + len(sent) + 1 <= max_len:
            current = f"{current} {sent}".strip()
        else:
            if current:
                chunks.append(current)
            current = sent
    if current:
        chunks.append(current)
    return chunks


async def synth_one_chunk(chunk_text: str, voice: str, rate: str, sem: asyncio.Semaphore) -> bytes:
    last_err = None
    async with sem:
        for attempt in range(3):
            try:
                communicate = edge_tts.Communicate(text=chunk_text, voice=voice, rate=rate)
                buf = bytearray()
                async for msg in communicate.stream():
                    if msg["type"] == "audio":
                        buf.extend(msg["data"])
                if buf:
                    return bytes(buf)
                last_err = RuntimeError("Empty audio received")
            except Exception as exc:
                last_err = exc
                await asyncio.sleep(0.4 * (attempt + 1))
    raise RuntimeError(f"TTS chunk failed: {last_err}")


async def synth_full_text(text: str, voice: str, rate: str) -> bytes:
    chunks = split_chunks(text, max_len=1600)
    sem = asyncio.Semaphore(4)
    tasks = [synth_one_chunk(c, voice, rate, sem) for c in chunks]
    parts = await asyncio.gather(*tasks)
    return b"".join(parts)


def load_index_html():
    for candidate in (
        os.path.join(BASE_DIR, "index.html"),
        os.path.join(os.getcwd(), "index.html"),
    ):
        if os.path.isfile(candidate):
            with open(candidate, "rb") as f:
                return f.read()
    return None


def shorten_long_url(target_url: str) -> str:
    url_str = str(target_url or "").strip()
    if not re.match(r"^https?://", url_str, flags=re.IGNORECASE):
        raise ValueError("Đường link cần rút gọn phải bắt đầu bằng http:// hoặc https://")

    ua_headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36",
        "Content-Type": "application/x-www-form-urlencoded; charset=utf-8",
    }

    providers = [
        ("https://clck.ru/--", urlencode({"url": url_str}).encode("utf-8"), "text"),
        ("https://tinyurl.com/api-create.php", urlencode({"url": url_str}).encode("utf-8"), "text"),
        ("https://is.gd/create.php", urlencode({"format": "simple", "url": url_str}).encode("utf-8"), "text"),
        ("https://v.gd/create.php", urlencode({"format": "simple", "url": url_str}).encode("utf-8"), "text"),
        ("https://da.gd/s", urlencode({"url": url_str}).encode("utf-8"), "text"),
        ("https://spoo.me/", urlencode({"url": url_str}).encode("utf-8"), "spoo_json"),
    ]

    for endpoint, form_bytes, mode in providers:
        try:
            headers = dict(ua_headers)
            if mode == "spoo_json":
                headers["Accept"] = "application/json"
            req = Request(endpoint, data=form_bytes, headers=headers, method="POST")
            with urlopen(req, timeout=8) as resp:
                raw_txt = resp.read().decode("utf-8", errors="ignore").strip()
                if mode == "spoo_json":
                    parsed = json.loads(raw_txt)
                    candidate = str(parsed.get("short_url") or "").strip()
                else:
                    candidate = raw_txt.splitlines()[0].strip() if raw_txt else ""
                if re.match(r"^https?://\S+$", candidate, flags=re.IGNORECASE) and len(candidate) < len(url_str):
                    return candidate
        except Exception:
            continue

    if len(url_str) <= 2400:
        try:
            get_ep = "https://clck.ru/--?" + urlencode({"url": url_str})
            req = Request(get_ep, headers={"User-Agent": ua_headers["User-Agent"]}, method="GET")
            with urlopen(req, timeout=7) as resp:
                candidate = resp.read().decode("utf-8", errors="ignore").strip()
                if re.match(r"^https?://\S+$", candidate, flags=re.IGNORECASE) and len(candidate) < len(url_str):
                    return candidate
        except Exception:
            pass

    raise RuntimeError("Các máy chủ rút gọn link đang tạm bận, vui lòng thử lại sau giây lát.")


class handler(BaseHTTPRequestHandler):
    def _cors_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-API-Key")

    def _send_json(self, status_code: int, payload: dict):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status_code)
        self._cors_headers()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors_headers()
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        action = parse_qs(parsed.query).get("action", [""])[0]

        if action == "shorten" or path in ("/shorten", "/api/tts/shorten") or path.endswith("/shorten"):
            target_url = parse_qs(parsed.query).get("url", [""])[0]
            if not target_url:
                self._send_json(400, {"error": "Thiếu tham số url cần rút gọn."})
                return
            try:
                short_url = shorten_long_url(target_url)
                self._send_json(200, {"short_url": short_url})
            except Exception as exc:
                self._send_json(502, {"error": str(exc)})
            return

        if action == "voices" or path in ("/voices", "/api/tts/voices") or path.endswith("/voices"):
            self._send_json(200, {"voices": VOICES})
            return

        if action == "health" or path in ("/health", "/api/tts"):
            self._send_json(
                200,
                {
                    "status": "ok",
                    "model": "Microsoft Neural TTS (Vercel)",
                    "engine": "edge-tts",
                    "voices_count": len(VOICES),
                },
            )
            return

        if path in ("/", "/index.html"):
            html = load_index_html()
            if html is not None:
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(html)))
                self.end_headers()
                self.wfile.write(html)
                return

        self._send_json(404, {"detail": "Not Found"})

    def do_POST(self):
        try:
            parsed = urlparse(self.path)
            path = parsed.path.rstrip("/") or "/"
            query_action = parse_qs(parsed.query).get("action", [""])[0]

            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length) if length > 0 else b"{}"
            data = json.loads(raw.decode("utf-8"))

            action = str(data.get("action") or query_action or "").strip().lower()
            if action == "shorten" or path in ("/shorten", "/api/tts/shorten") or path.endswith("/shorten"):
                target_url = str(data.get("url") or "").strip()
                if not target_url:
                    self._send_json(400, {"error": "Thiếu đường link cần rút gọn."})
                    return
                short_url = shorten_long_url(target_url)
                self._send_json(200, {"short_url": short_url})
                return

            text = clean_text(str(data.get("text") or ""))
            if not text:
                self._send_json(400, {"error": "Văn bản trống, vui lòng nhập nội dung."})
                return

            voice = resolve_voice(data.get("voice") or data.get("voice_id") or "")
            rate = format_rate(data.get("speed", 1.0))

            loop = asyncio.new_event_loop()
            try:
                asyncio.set_event_loop(loop)
                audio_bytes = loop.run_until_complete(synth_full_text(text, voice, rate))
            finally:
                loop.close()

            self.send_response(200)
            self._cors_headers()
            self.send_header("Content-Type", "audio/mpeg")
            self.send_header("Content-Disposition", 'inline; filename="voice.mp3"')
            self.send_header("Content-Length", str(len(audio_bytes)))
            self.end_headers()
            self.wfile.write(audio_bytes)
        except Exception as exc:
            self._send_json(500, {"error": f"Lỗi tạo âm thanh: {exc}"})
