"""Local HTTP interface for the macOS project storage manager."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import unicodedata
from urllib.parse import urlsplit
import webbrowser

from linked_projects import linked_roots
from storage import Root, open_scanned_file, scan, trash_files


APP_DIR = Path(__file__).resolve().parent
HOME = Path.home().resolve()
CONFIG = HOME / "Library/Application Support/DiskLedger/folders.json"
CODEX_HOME = HOME / ".codex"
CODEX_EXPORTS = HOME / "Documents/Codex"
FILE_LIMIT = 5000
BATCH_LIMIT = 200
CUSTOM_LIMIT = 24
PREVIEW_TYPES = {
    ".avif": "image/avif", ".bmp": "image/bmp", ".gif": "image/gif", ".heic": "image/heic",
    ".heif": "image/heif", ".jpeg": "image/jpeg", ".jpg": "image/jpeg", ".png": "image/png",
    ".svg": "image/svg+xml", ".tif": "image/tiff", ".tiff": "image/tiff", ".webp": "image/webp",
    ".avi": "video/x-msvideo", ".m4v": "video/mp4", ".mkv": "video/x-matroska",
    ".mov": "video/quicktime", ".mp4": "video/mp4", ".ogv": "video/ogg", ".webm": "video/webm",
}
PREVIEW_CHUNK_SIZE = 256 * 1024
ARTIFACT_EXTENSIONS = frozenset("""
    7z aac aep aif aiff avif blend csv doc docx epub fig flac gif gz heic html jpeg jpg jsonl
    key m4a m4v mkv mov mp3 mp4 numbers odp ods odt pages pdf png ppt pptx psd rar rtf srt
    svg tar tif tiff tsv txt vtt wav webm webp xls xlsx zip
""".split())
STATIC_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/styles.css": ("styles.css", "text/css; charset=utf-8"),
    "/projects.css": ("projects.css", "text/css; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
}


class RequestError(Exception):
    def __init__(self, message, status=HTTPStatus.BAD_REQUEST):
        super().__init__(message)
        self.status = status


def is_artifact(path):
    suffix = path.suffix.lower()
    return suffix[1:] in ARTIFACT_EXTENSIONS or suffix in PREVIEW_TYPES


def preview_range(value, size):
    """Parse a single HTTP byte range into inclusive bounds."""
    match = re.fullmatch(r"bytes=([0-9]*)-([0-9]*)", value.strip()) if len(value) <= 100 else None
    if not match or size <= 0:
        return None
    first, last = match.groups()
    if first:
        start = int(first)
        end = min(int(last), size - 1) if last else size - 1
        return (start, end) if start <= end else None
    if not last or int(last) == 0:
        return None
    return max(0, size - int(last)), size - 1


def is_codex_output(path):
    parts = path.parts
    return (len(parts) >= 4 and parts[2] == "outputs"
            and re.fullmatch(r"\d{4}-\d{2}-\d{2}", parts[0]) is not None and is_artifact(path))


def first_folder(path):
    if len(path.parts) >= 2:
        return path.parts[0], path.parts[0]
    return None


def export_project(path):
    date, folder = path.parts[:2]
    label = folder.replace("-", " ")
    return f"{date}/{folder}", label


def visualization_project(path):
    if len(path.parts) >= 5:
        folder = Path(*path.parts[:4])
        return folder.as_posix(), f"可视化 {folder.name[:8]}"
    return None


def object_fields(value):
    return value if isinstance(value, dict) else {}


def display_name(value):
    return value.strip()[:200] if isinstance(value, str) else ""


def read_codex_state():
    try:
        return object_fields(json.loads((CODEX_HOME / ".codex-global-state.json").read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return {}


def attached_roots(state, roots):
    folders = {}
    excluded = [APP_DIR, *(root.path for root in roots)]
    for project_id, label, output in linked_roots(state, HOME, excluded):
        folders.setdefault((project_id, label, output.parent), set()).add(output.name)
    for (project_id, label, path), exports in folders.items():
        key = hashlib.sha256(project_id.encode() + b"\0" + os.fsencode(path)).hexdigest()[:24]

        def within_exports(relative, names=frozenset(exports)):
            return relative.parts[0] in names

        def include(relative, within_exports=within_exports):
            return len(relative.parts) > 1 and within_exports(relative) and is_artifact(relative)

        yield project_id, Root(f"linked-{key}", label, path, include=include, group=first_folder, descend=within_exports)


def add_project_titles(roots, state):
    """Resolve scanned folder names from local project and thread title indexes."""
    titles = {}
    try:
        with (CODEX_HOME / "session_index.jsonl").open(encoding="utf-8", errors="replace") as handle:
            for line in handle:
                try:
                    item = object_fields(json.loads(line))
                except ValueError:
                    continue
                thread_id, title = item.get("id"), display_name(item.get("thread_name"))
                if isinstance(thread_id, str) and title:
                    titles[thread_id] = title
    except OSError:
        pass
    names = {key: display_name(object_fields(item).get("name"))
             for key, item in object_fields(state.get("local-projects")).items()}
    assignments = object_fields(state.get("thread-project-assignments"))
    output_threads = {output: key for key, output in
                      object_fields(state.get("thread-projectless-output-directories")).items()
                      if isinstance(output, str)}
    for root in roots:
        for project in root.get("projects", []):
            key = project["key"]
            if root["id"] == "projects":
                project["label"] = names.get(key) or f"本地项目 {key.removeprefix('g-p-')[:8]}"
                continue
            if root["id"] == "codex-output":
                thread_id = output_threads.get(str(Path(project["path"]) / "outputs"))
                project["detail"] = key.split("/", 1)[0]
            elif root["id"] in ("visualizations", "images"):
                thread_id = Path(key).name
                if root["id"] == "images":
                    project["label"] = f"图片批次 {thread_id[:8]}"
            else:
                continue
            project["label"] = titles.get(thread_id, project["label"])
            parent_id = object_fields(assignments.get(thread_id)).get("projectId")
            if isinstance(parent_id, str) and names.get(parent_id):
                project["parentLabel"] = names[parent_id]


def default_roots():
    candidates = (
        Root("codex-output", "Codex 任务导出", CODEX_EXPORTS, include=is_codex_output, group=export_project),
        Root("images", "生成图片", CODEX_HOME / "generated_images", include=is_artifact, group=first_folder),
        Root("projects", "ChatGPT 项目", CODEX_HOME / ".chatgpt-projects", include=is_artifact, group=first_folder),
        Root("visualizations", "图表与可视化", CODEX_HOME / "visualizations", include=is_artifact, group=visualization_project),
    )
    return [root for root in candidates if root.path.is_dir()]


def is_inside(path, directory):
    prefix = path.parts[:len(directory.parts)]
    return (unicodedata.normalize("NFD", str(Path(*prefix))).casefold()
            == unicodedata.normalize("NFD", str(directory)).casefold())


def custom_root(path):
    key = hashlib.sha256(os.fsencode(path)).hexdigest()[:24]
    return Root(f"custom-{key}", path.name, path, group=first_folder)


def validate_custom_path(value, require_available=True):
    if not isinstance(value, str) or not value.strip() or len(value) > 4096 or "\x00" in value:
        raise RequestError("请输入有效的文件夹路径。")
    try:
        path = Path(value.strip()).expanduser()
    except RuntimeError as exc:
        raise RequestError("无法展开这个主目录路径。") from exc
    if not path.is_absolute() or ".." in path.parts:
        raise RequestError("请输入绝对路径，例如 ~/Documents/我的项目。")
    if is_inside(path, HOME):
        relative = path.parts[len(HOME.parts):]
        permitted = (bool(relative) and relative[0].casefold() != "library"
                     and not any(part.startswith(".") for part in relative))
    else:
        permitted = len(path.parts) >= 4 and path.parts[1] == "Volumes" and not any(
            part.startswith(".") for part in path.parts[2:]
        )
    if not permitted or is_inside(path, APP_DIR):
        raise RequestError("请选择个人主目录或外置磁盘内的项目文件夹。")
    try:
        if require_available and path.resolve() != path:
            raise RequestError("请选择文件夹的真实路径。")
        if require_available and not path.is_dir():
            raise RequestError("找不到这个文件夹，请检查路径。")
    except (OSError, RuntimeError) as exc:
        raise RequestError("无法读取这个文件夹路径。") from exc
    return path


class StorageManager:
    def __init__(self, config, defaults):
        self.config = config
        self.defaults = defaults
        self.custom = self.load_config()
        self.lock = threading.Lock()
        self.snapshot = {}

    def load_config(self):
        try:
            values = json.loads(self.config.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return []
        if not isinstance(values, list) or len(values) > CUSTOM_LIMIT:
            raise ValueError("目录设置格式有误。")
        folders = []
        for value in values:
            # Missing folders remain visible, for example when an external drive is unplugged.
            path = validate_custom_path(value, require_available=False)
            root = custom_root(path)
            if not self.overlaps(path, self.defaults + folders):
                folders.append(root)
        return folders

    @staticmethod
    def overlaps(path, roots):
        return any(is_inside(path, root.path) or is_inside(root.path, path) for root in roots)

    def save_config(self, roots):
        self.config.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=self.config.parent, delete=False
            ) as handle:
                temporary_path = Path(handle.name)
                json.dump([str(root.path) for root in roots], handle, ensure_ascii=False)
                handle.write("\n")
            os.replace(temporary_path, self.config)
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
        self.custom = roots
        self.snapshot.clear()

    def add_folder(self, value):
        path = validate_custom_path(value)
        if not os.access(path, os.R_OK | os.X_OK):
            raise RequestError("当前进程没有读取这个文件夹的权限。")
        with self.lock:
            if self.overlaps(path, self.defaults + self.custom):
                raise RequestError("这个路径与现有扫描目录重叠。")
            if len(self.custom) >= CUSTOM_LIMIT:
                raise RequestError(f"最多可以添加 {CUSTOM_LIMIT} 个自定义目录。")
            self.save_config(self.custom + [custom_root(path)])

    def remove_folder(self, key):
        with self.lock:
            roots = [root for root in self.custom if root.id != key]
            if len(roots) == len(self.custom):
                raise RequestError("找不到这个自定义目录。", HTTPStatus.NOT_FOUND)
            self.save_config(roots)

    def scan(self):
        with self.lock:
            state = read_codex_state()
            configured = self.defaults + self.custom
            linked_records = list(attached_roots(state, configured))
            linked = [root for _, root in linked_records]
            files, roots, snapshot = scan(configured + linked, excluded_paths=(APP_DIR,))
            add_project_titles(roots, state)
            covered = {project_id for project_id, _ in linked_records}
            covered.update(project["key"] for root in roots if root["id"] == "projects" for project in root["projects"])
            other_projects = [label for key, item in object_fields(state.get("local-projects")).items()
                              if key not in covered and (label := display_name(object_fields(item).get("name")))]
            custom_ids = {root.id for root in self.custom}
            linked_ids = {root.id for root in linked}
            for root in roots:
                root["custom"] = root["id"] in custom_ids
                if root["id"] in linked_ids:
                    root["kind"] = "linked"
            files.sort(key=lambda row: (-row["allocated"], -row["size"], row["path"]))
            files = files[:FILE_LIMIT]
            for row in files:
                mime = PREVIEW_TYPES.get(Path(row["name"]).suffix.lower())
                if mime:
                    row["previewKind"] = mime.split("/", 1)[0]
            self.snapshot = {row["id"]: snapshot[row["id"]] for row in files}
            totals = {key: sum(root[key] for root in roots) for key in ("size", "allocated", "count")}
            disk = shutil.disk_usage(HOME)
            return {
                "roots": roots,
                "files": files,
                "totals": totals,
                "disk": {"total": disk.total, "free": disk.free},
                "otherProjects": sorted(other_projects),
                "scannedAt": time.time(),
                "listedCount": len(files),
                "omittedCount": max(0, totals["count"] - len(files)),
            }

    def trash(self, ids):
        if (not isinstance(ids, list) or not 1 <= len(ids) <= BATCH_LIMIT
                or any(not isinstance(key, str) or not 1 <= len(key) <= 128 for key in ids)):
            raise RequestError(f"请选择 1 至 {BATCH_LIMIT} 个文件。")
        with self.lock:
            results = trash_files(list(dict.fromkeys(ids)), self.snapshot)
            for result in results:
                if result["ok"]:
                    self.snapshot.pop(result["id"], None)
            return results

    def _preview_source(self, key):
        entry = self.snapshot.get(key) if isinstance(key, str) and len(key) == 36 else None
        if entry is None:
            raise RequestError("文件记录已失效，请重新扫描。", HTTPStatus.NOT_FOUND)
        path = entry.root.joinpath(*entry.relative)
        mime = PREVIEW_TYPES.get(path.suffix.lower())
        if mime is None:
            raise RequestError("此文件类型不支持预览。", HTTPStatus.UNSUPPORTED_MEDIA_TYPE)
        return entry, path, mime

    def preview(self, key):
        with self.lock:
            entry, _, mime = self._preview_source(key)
            try:
                handle = open_scanned_file(entry)
            except (ValueError, OSError) as exc:
                raise RequestError("文件已变化或无法读取，请重新扫描。", HTTPStatus.CONFLICT) from exc
            return handle, mime

    def open_preview(self, key):
        with self.lock:
            entry, path, mime = self._preview_source(key)
            if mime == "image/svg+xml":
                raise RequestError("SVG 支持页面内预览。", HTTPStatus.UNSUPPORTED_MEDIA_TYPE)
            try:
                with open_scanned_file(entry):
                    command = ["/usr/bin/open"]
                    if path.suffix.lower() in {".mp4", ".m4v", ".mov"}:
                        command.extend(("-a", "QuickTime Player"))
                    elif mime.startswith("image/"):
                        command.extend(("-a", "Preview"))
                    subprocess.run([*command, path], capture_output=True, check=True, timeout=8)
            except (ValueError, OSError, subprocess.SubprocessError) as exc:
                raise RequestError("无法用系统应用打开该文件，请重新扫描或检查文件权限。",
                                   HTTPStatus.CONFLICT) from exc


class LocalServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, port, manager):
        super().__init__(("127.0.0.1", port), Handler)
        self.manager = manager
        self.host = f"127.0.0.1:{self.server_port}"
        self.origin = f"http://{self.host}"


class Handler(BaseHTTPRequestHandler):
    timeout = 15

    def respond(self, status, content, mime):
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self' 'unsafe-inline'; "
                         "object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(content)

    def json_response(self, payload, status=HTTPStatus.OK):
        self.respond(status, json.dumps(payload, ensure_ascii=True).encode(), "application/json; charset=utf-8")

    def check_request(self, path):
        if self.headers.get("Host") != self.server.host:
            raise RequestError("请从本机地址打开这个工具。", HTTPStatus.FORBIDDEN)
        if path.startswith("/api/") and self.headers.get("Sec-Fetch-Site") == "cross-site":
            raise RequestError("此请求必须来自工具界面。", HTTPStatus.FORBIDDEN)
        if path.startswith("/api/preview/") and (self.headers.get("Origin", self.server.origin) != self.server.origin
                or self.headers.get("Sec-Fetch-Site") not in (None, "none", "same-origin")):
            raise RequestError("预览只能从工具界面打开。", HTTPStatus.FORBIDDEN)
        if self.command in ("POST", "DELETE"):
            if (self.headers.get("Origin", self.server.origin) != self.server.origin
                    or self.headers.get("X-Storage-Manager") != "1"):
                raise RequestError("此操作必须来自工具界面。", HTTPStatus.FORBIDDEN)

    def read_json(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 32768 or self.headers.get_content_type() != "application/json":
                raise ValueError
            body = json.loads(self.rfile.read(length))
            if not isinstance(body, dict):
                raise ValueError
            return body
        except (ValueError, UnicodeError) as exc:
            raise RequestError("请求格式有误。") from exc

    def stream_preview(self, key):
        handle, mime = self.server.manager.preview(key)
        with handle:
            size = os.fstat(handle.fileno()).st_size
            status = HTTPStatus.OK
            start, length = 0, size
            content_range = None
            requested_range = self.headers.get("Range") if self.command == "GET" else None
            if requested_range is not None:
                bounds = preview_range(requested_range, size)
                if bounds is None:
                    status = HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE
                    length, content_range = 0, f"bytes */{size}"
                else:
                    start, end = bounds
                    status = HTTPStatus.PARTIAL_CONTENT
                    length, content_range = end - start + 1, f"bytes {start}-{end}/{size}"
            handle.seek(start)
            self.send_response(status)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(length))
            self.send_header("Accept-Ranges", "bytes")
            if content_range is not None:
                self.send_header("Content-Range", content_range)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Security-Policy", "sandbox; default-src 'none'; style-src 'unsafe-inline'; img-src data:")
            self.send_header("Cross-Origin-Resource-Policy", "same-origin")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            if self.command == "HEAD":
                return
            try:
                while length:
                    chunk = handle.read(min(length, PREVIEW_CHUNK_SIZE))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    length -= len(chunk)
            except OSError:
                self.close_connection = True

    def handle_request(self):
        try:
            path = urlsplit(self.path).path
            self.check_request(path)
            manager = self.server.manager
            if self.command == "GET" and path in STATIC_FILES:
                name, mime = STATIC_FILES[path]
                self.respond(HTTPStatus.OK, (APP_DIR / "web" / name).read_bytes(), mime)
            elif self.command == "GET" and path == "/api/scan":
                self.json_response(manager.scan())
            elif self.command in ("GET", "HEAD") and path.startswith("/api/preview/"):
                self.stream_preview(path.removeprefix("/api/preview/"))
            elif self.command == "POST" and path == "/api/open-preview":
                manager.open_preview(self.read_json().get("id"))
                self.json_response({"ok": True})
            elif self.command == "POST" and path == "/api/roots":
                manager.add_folder(self.read_json().get("path"))
                self.json_response({"ok": True})
            elif self.command == "POST" and path == "/api/trash":
                self.json_response({"results": manager.trash(self.read_json().get("ids"))})
            elif self.command == "DELETE" and path.startswith("/api/roots/"):
                manager.remove_folder(path.rsplit("/", 1)[-1])
                self.json_response({"ok": True})
            else:
                self.json_response({"error": "找不到这个页面。"}, HTTPStatus.NOT_FOUND)
        except RequestError as exc:
            self.json_response({"error": str(exc)}, exc.status)
        except (BrokenPipeError, ConnectionResetError):
            return
        except OSError as exc:
            self.log_error("%s", exc)
            self.json_response({"error": "本地文件操作失败，请查看启动窗口。"}, HTTPStatus.INTERNAL_SERVER_ERROR)

    do_GET = do_HEAD = do_POST = do_DELETE = handle_request


def main():
    parser = argparse.ArgumentParser(description="DiskLedger · Local storage dashboard for macOS")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-open", action="store_true")
    parser.add_argument("--config", type=Path, default=CONFIG, help=argparse.SUPPRESS)
    args = parser.parse_args()
    try:
        with LocalServer(args.port, StorageManager(args.config, default_roots())) as server:
            print(f"DiskLedger 已启动：{server.origin}/", flush=True)
            print("关闭此窗口或按 Ctrl+C 停止。", flush=True)
            if not args.no_open:
                webbrowser.open(server.origin + "/")
            server.serve_forever()
    except KeyboardInterrupt:
        pass
    except (OSError, ValueError, RequestError) as exc:
        parser.exit(1, f"启动失败：{exc}\n")


if __name__ == "__main__":
    main()
