# -*- coding: utf-8 -*-
"""Local Beat Studio.

Serves the prompt desk and proxies the ACE-Step API at 127.0.0.1:8001.
Generated audio is saved under studio/library. Existing tracks in
acestep_output are exposed as samples for instant preview.
"""
from __future__ import annotations

import json
import mimetypes
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HOST = "127.0.0.1"
PORT = 8765
API = "http://127.0.0.1:8001"
HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
SAMPLES = ROOT / "acestep_output"
LIBRARY = HERE / "library"
PRESETS = HERE / "presets.json"
INDEX = HERE / "index.html"

AUDIO_EXT = {".mp3", ".wav", ".flac", ".ogg", ".m4a"}
SAVED: dict[str, dict] = {}
SAVE_LOCK = threading.Lock()

LIBRARY.mkdir(exist_ok=True)


def clamp_int(value, lo, hi, default):
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, n))


def slugify(text: str) -> str:
    text = (text or "track").strip().lower()
    text = re.sub(r"\s+", "-", text)
    text = re.sub(r"[^a-z0-9\u4e00-\u9fff_-]", "", text)
    return (text[:32] or "track")


def inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def resolve_media(bucket: str, rel: str) -> Path | None:
    root = SAMPLES if bucket == "samples" else LIBRARY if bucket == "library" else None
    if root is None:
        return None
    rel = urllib.parse.unquote(rel).replace("\\", "/").lstrip("/")
    parts = [p for p in rel.split("/") if p and p != "."]
    if not parts or any(p == ".." for p in parts):
        return None
    path = (root.joinpath(*parts)).resolve()
    if not inside(path, root.resolve()) or not path.is_file():
        return None
    if path.suffix.lower() not in AUDIO_EXT:
        return None
    return path


TITLES = {
    "hard_drops/01_bigroom_edm_128bpm.mp3": "主舞台电音",
    "hard_drops/02_techno_rave_140bpm.mp3": "仓库科技舞曲",
    "hard_drops/03_uk_drill_142bpm.mp3": "英式钻头 · 器乐",
    "hard_drops/04_psytrance_145bpm.mp3": "迷幻舞曲",
    "hard_drops/05_dubstep_140bpm.mp3": "回响贝斯",
    "hard_drops/06_hardstyle_150bpm.mp3": "硬派舞曲",
    "drill_vocal/01b_drill_rap_chorusstart_v1.mp3": "钻头说唱 · 副歌起 · 一",
    "drill_vocal/01_drill_rap_full_v1.mp3": "钻头说唱 · 全曲 · 一",
    "drill_vocal/02b_drill_rap_chorusstart_v2.mp3": "钻头说唱 · 副歌起 · 二",
    "drill_vocal/02_drill_rap_full_v2.mp3": "钻头说唱 · 全曲 · 二",
    "big_room_dj_drop_128.mp3": "主舞台电音 · 原稿",
    "big_room_dj_drop_128_instadrop.mp3": "主舞台电音 · 即时重拍",
    "techno_rave_kick_140.mp3": "仓库科技舞曲 · 原稿",
    "techno_rave_kick_140_instadrop.mp3": "仓库科技舞曲 · 即时重拍",
    "uk_drill_hard_142.mp3": "英式钻头 · 原稿",
    "uk_drill_hard_142_instadrop.mp3": "英式钻头 · 即时重拍",
    "uk_drill_142_instrumental.mp3": "英式钻头器乐",
    "uk_drill_142_instrumental_shortintro.mp3": "英式钻头器乐 · 短前奏",
    "uk_drill_rap_vocal_1.mp3": "钻头说唱人声 · 一",
    "uk_drill_rap_vocal_2.mp3": "钻头说唱人声 · 二",
    "uk_drill_rap_vocal_1_instant.mp3": "钻头说唱人声 · 一 · 即时",
    "uk_drill_rap_vocal_2_instant.mp3": "钻头说唱人声 · 二 · 即时",
    "psy_trance_rush_145.mp3": "迷幻舞曲 · 原稿",
    "psy_trance_rush_145_instadrop.mp3": "迷幻舞曲 · 即时重拍",
    "dubstep_wobble_drop_140.mp3": "回响贝斯 · 原稿",
    "dubstep_wobble_drop_140_instadrop.mp3": "回响贝斯 · 即时重拍",
    "bounce_hardstyle_kick_150.mp3": "硬派舞曲 · 原稿",
    "bounce_hardstyle_kick_150_instadrop.mp3": "硬派舞曲 · 即时重拍",
    "american_mahjong_social_bgm_96bpm.mp3": "轻社交背景乐",
    "chatcmpl-7060a4a3214ac8c4_1.mp3": "早期试听",
}


def pretty_name(filename: str, rel: str = "") -> str:
    if rel in TITLES:
        return TITLES[rel]
    stem = Path(filename).stem.replace("_", " ").replace("-", " ")
    return re.sub(r"\s+", " ", stem).strip()


def list_audio(root: Path, bucket: str, limit: int = 60) -> list[dict]:
    if not root.is_dir():
        return []
    items = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not d.startswith(".") and d not in {"__pycache__"}]
        for name in filenames:
            if Path(name).suffix.lower() not in AUDIO_EXT:
                continue
            full = Path(dirpath) / name
            rel = full.relative_to(root).as_posix()
            st = full.stat()
            items.append(
                {
                    "title": pretty_name(name, rel),
                    "url": f"/media/{bucket}/{rel}",
                    "bytes": st.st_size,
                    "mtime": int(st.st_mtime),
                    "bucket": bucket,
                }
            )
    items.sort(key=lambda item: item["mtime"], reverse=True)
    return items[:limit]


def api_json(path: str, payload: dict | None = None, timeout: float = 30):
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        API + path,
        data=data,
        headers={"Content-Type": "application/json; charset=utf-8"} if data else {},
        method="POST" if data is not None else "GET",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read()
    return json.loads(body.decode("utf-8"))


def build_payload(body: dict) -> dict:
    mode = body.get("mode") or "caption"
    duration = clamp_int(body.get("duration"), 10, 240, 45)
    bpm = clamp_int(body.get("bpm"), 40, 220, 120)
    language = body.get("language") or "zh"
    if language not in {"zh", "en", "ja", "ko", "yue"}:
        language = "en"
    thinking = bool(body.get("thinking"))
    instrumental = bool(body.get("instrumental"))
    lyrics = "" if instrumental else str(body.get("lyrics") or "")
    common = {
        "vocal_language": language,
        "audio_format": "mp3",
        "use_random_seed": True,
        "audio_duration": duration,
        "bpm": bpm,
        "batch_size": 1,
        "use_format": False,
    }
    if mode == "simple":
        query = str(body.get("description") or body.get("prompt") or "").strip()
        if len(query) < 2:
            raise ValueError("先写一句想听的描述")
        common.update(
            {
                "prompt": "",
                "lyrics": lyrics,
                "sample_mode": True,
                "sample_query": query,
                "thinking": True,
                "use_cot_caption": True,
                "use_cot_language": True,
            }
        )
        return common
    prompt = str(body.get("prompt") or "").strip()
    if len(prompt) < 2:
        raise ValueError("先写风格提示词，或点一个预制风格")
    common.update(
        {
            "prompt": prompt,
            "lyrics": lyrics,
            "sample_mode": False,
            "sample_query": "",
            "thinking": thinking,
            "use_cot_caption": False,
            "use_cot_language": False,
        }
    )
    return common


def audio_remote_url(file_field: str) -> str:
    if file_field.startswith("http://") or file_field.startswith("https://"):
        return file_field
    if file_field.startswith("/"):
        return API + file_field
    return API + "/" + file_field


def save_result(task_id: str, item: dict, title: str) -> dict:
    with SAVE_LOCK:
        if task_id in SAVED:
            return SAVED[task_id]
        file_field = item.get("file") or ""
        if not file_field:
            raise RuntimeError("生成完成，但没有返回音频地址")
        stamp = time.strftime("%Y%m%d_%H%M%S")
        dest = LIBRARY / f"{stamp}_{slugify(title)}.mp3"
        urllib.request.urlretrieve(audio_remote_url(file_field), dest)
        meta = {
            "title": title or dest.stem,
            "url": f"/media/library/{dest.name}",
            "prompt": item.get("prompt") or "",
            "lyrics": item.get("lyrics") or "",
            "metas": item.get("metas") or {},
            "bytes": dest.stat().st_size,
            "mtime": int(dest.stat().st_mtime),
        }
        dest.with_suffix(".json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        SAVED[task_id] = meta
        return meta


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        print("[studio]", fmt % args, flush=True)

    def send_json(self, code: int, payload: dict):
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def read_json(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if n > 2_000_000:
            raise ValueError("请求太大")
        raw = self.rfile.read(n) if n else b"{}"
        data = json.loads(raw.decode("utf-8") or "{}")
        if not isinstance(data, dict):
            raise ValueError("请求格式不对")
        return data

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        try:
            if path in {"/", "/index.html"}:
                self.serve_index()
            elif path == "/api/health":
                self.handle_health()
            elif path == "/api/presets":
                raw = PRESETS.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(raw)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(raw)
            elif path == "/api/library":
                generated = list_audio(LIBRARY, "library", 40)
                samples = list_audio(SAMPLES, "samples", 80)
                self.send_json(200, {"ok": True, "generated": generated, "samples": samples})
            elif path.startswith("/media/"):
                self.serve_media(path[len("/media/") :])
            else:
                self.send_json(404, {"ok": False, "error": "没有这个页面"})
        except Exception as exc:  # noqa: BLE001 - last-resort HTTP error
            self.send_json(500, {"ok": False, "error": str(exc)})

    def do_HEAD(self):
        self.do_GET()

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        try:
            body = self.read_json()
            if path == "/api/generate":
                self.handle_generate(body)
            elif path == "/api/status":
                self.handle_status(body)
            else:
                self.send_json(404, {"ok": False, "error": "没有这个接口"})
        except ValueError as exc:
            self.send_json(400, {"ok": False, "error": str(exc)})
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:400]
            self.send_json(502, {"ok": False, "error": detail or "ACE-Step 拒绝了这次生成"})
        except urllib.error.URLError:
            self.send_json(503, {"ok": False, "error": "连不上 ACE-Step（127.0.0.1:8001）。先运行 ACE-Step-1.5\\start_api_server.bat"})
        except Exception as exc:  # noqa: BLE001
            self.send_json(500, {"ok": False, "error": str(exc)})

    def serve_index(self):
        raw = INDEX.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(raw)

    def handle_health(self):
        try:
            api_json("/health", timeout=3)
            self.send_json(200, {"ok": True, "api": API})
        except Exception:
            self.send_json(200, {"ok": False, "api": API})

    def handle_generate(self, body: dict):
        payload = build_payload(body)
        resp = api_json("/release_task", payload, timeout=60)
        data = resp.get("data") if isinstance(resp, dict) else None
        task_id = ""
        if isinstance(data, dict):
            task_id = data.get("task_id") or ""
        task_id = task_id or (resp.get("task_id") if isinstance(resp, dict) else "") or ""
        if not task_id:
            raise RuntimeError("ACE-Step 没有返回任务编号")
        title = str(body.get("title") or "未命名").strip() or "未命名"
        SAVED.setdefault(task_id + ":title", {"title": title})
        self.send_json(200, {"ok": True, "task_id": task_id, "title": title})

    def handle_status(self, body: dict):
        task_id = str(body.get("task_id") or "")
        if not task_id:
            raise ValueError("缺少任务编号")
        resp = api_json("/query_result", {"task_id_list": [task_id]}, timeout=30)
        entries = resp.get("data") if isinstance(resp, dict) else resp
        entry = (entries[0] if isinstance(entries, list) and entries else {}) or {}
        status = entry.get("status")
        if status == 0 or status is None:
            self.send_json(200, {"ok": True, "status": 0, "task_id": task_id})
            return
        if status == 2:
            detail = entry.get("result") or "生成失败"
            self.send_json(200, {"ok": True, "status": 2, "task_id": task_id, "error": str(detail)[:500]})
            return
        result = entry.get("result") or "[]"
        if isinstance(result, str):
            result = json.loads(result or "[]")
        item = result[0] if isinstance(result, list) and result else {}
        title_holder = SAVED.get(task_id + ":title") or {}
        title = str(body.get("title") or title_holder.get("title") or "未命名")
        meta = save_result(task_id, item, title)
        self.send_json(
            200,
            {
                "ok": True,
                "status": 1,
                "task_id": task_id,
                "title": meta["title"],
                "url": meta["url"],
                "prompt": meta["prompt"],
                "lyrics": meta["lyrics"],
                "metas": meta["metas"],
            },
        )

    def serve_media(self, spec: str):
        spec = urllib.parse.unquote(spec)
        if "/" not in spec:
            self.send_json(404, {"ok": False, "error": "找不到音频"})
            return
        bucket, rel = spec.split("/", 1)
        path = resolve_media(bucket, rel)
        if path is None:
            self.send_json(404, {"ok": False, "error": "找不到音频"})
            return
        self.send_file(path)

    def send_file(self, path: Path):
        size = path.stat().st_size
        ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        start, end = 0, size - 1
        status = 200
        range_header = self.headers.get("Range")
        if range_header and range_header.startswith("bytes="):
            spec = range_header.split("=", 1)[1].split(",")[0].strip()
            left, _, right = spec.partition("-")
            if left == "":
                length = int(right or 0)
                start = max(0, size - length)
            else:
                start = int(left)
                end = int(right) if right else size - 1
            end = min(end, size - 1)
            if start > end or start >= size:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            status = 206
        length = end - start + 1
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-cache")
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        if self.command == "HEAD":
            return
        with path.open("rb") as handle:
            handle.seek(start)
            remaining = length
            while remaining > 0:
                chunk = handle.read(min(64 * 1024, remaining))
                if not chunk:
                    break
                try:
                    self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    return
                remaining -= len(chunk)


def main():
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    url = f"http://{HOST}:{PORT}"
    print(f"节拍台已打开  {url}", flush=True)
    print("生成音乐需要 ACE-Step API：ACE-Step-1.5\\start_api_server.bat", flush=True)
    if os.environ.get("BEAT_NO_BROWSER") != "1":
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("已停止", flush=True)


if __name__ == "__main__":
    main()
