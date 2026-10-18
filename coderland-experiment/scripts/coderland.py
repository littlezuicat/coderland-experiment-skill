#!/usr/bin/env python3
"""Standard-library helper bundled with the CoderLand Experiment Skill."""

from __future__ import annotations

import argparse
import http.client
import getpass
import hashlib
import json
import math
import os
import re
import secrets
import shutil
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile
from pathlib import Path, PurePosixPath


API_BASE = "https://code.zuicat.cn"
BUU_BASE = "https://buu-download.code.zuicat.cn"
API_PREFIX = "/api/v1/experiments"
MAX_CHUNK_BYTES = 8 * 1024 * 1024
MAX_ARCHIVE_UNPACKED = 3 * 1024 * 1024 * 1024
MAX_PAPER_PDF_BYTES = 50 * 1024 * 1024
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff"}
MASK_EXTENSIONS = {".png", ".bmp", ".tif", ".tiff"}


class CoderLandError(Exception):
    pass


class _TrustedRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, file_pointer, code, message, headers, new_url):
        old = urllib.parse.urlsplit(request.full_url)
        new = urllib.parse.urlsplit(new_url)
        if new.scheme != "https" or new.hostname != old.hostname or new.port != old.port:
            raise urllib.error.URLError("拒绝把 API 请求或凭据重定向到其他主机。")
        return super().redirect_request(request, file_pointer, code, message, headers, new_url)


class _TrustedDownloadRedirectHandler(_TrustedRedirectHandler):
    """Allow CoderLand's documented Drive redirects without forwarding API credentials."""

    _GOOGLE_DOWNLOAD_HOSTS = {"drive.google.com", "drive.usercontent.google.com"}
    _GOOGLE_DOWNLOAD_PATHS = {"/uc", "/download"}

    def redirect_request(self, request, file_pointer, code, message, headers, new_url):
        old = urllib.parse.urlsplit(request.full_url)
        new = urllib.parse.urlsplit(new_url)
        if new.scheme == "https" and new.hostname == old.hostname and new.port == old.port:
            return super().redirect_request(request, file_pointer, code, message, headers, new_url)
        if (request.get_method().upper() != "GET" or new.scheme != "https"
                or (new.hostname or "").lower() not in self._GOOGLE_DOWNLOAD_HOSTS
                or new.path not in self._GOOGLE_DOWNLOAD_PATHS or new.port not in (None, 443)):
            raise urllib.error.URLError("拒绝跟随非预期的外部下载重定向。")
        redirected = urllib.request.HTTPRedirectHandler.redirect_request(
            self, request, file_pointer, code, message, headers, new_url,
        )
        if redirected is not None:
            for header_name in (*redirected.headers, *redirected.unredirected_hdrs):
                if header_name.casefold() == "x-api-key":
                    redirected.remove_header(header_name)
        return redirected


def credentials_path() -> Path:
    override = os.environ.get("CODERLAND_CREDENTIALS_FILE")
    if override:
        return Path(override).expanduser()
    if os.name == "nt":
        appdata = os.environ.get("APPDATA")
        if not appdata:
            raise CoderLandError("找不到 APPDATA；请设置 CODERLAND_CREDENTIALS_FILE。")
        return Path(appdata) / "CoderLand" / "credentials.json"
    return Path.home() / ".config" / "coderland" / "credentials.json"


def load_credentials() -> dict:
    path = credentials_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise CoderLandError("尚未配置 API Key。先运行 `python scripts/coderland.py login`。") from exc
    except (OSError, ValueError) as exc:
        raise CoderLandError(f"无法读取凭据文件 {path}: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("api_key"), str):
        raise CoderLandError(f"凭据文件格式无效：{path}")
    api_key = data["api_key"]
    if not re.fullmatch(r"cl_exp_[A-Za-z0-9_-]{32,64}", api_key):
        raise CoderLandError("凭据文件中的 API Key 格式无效。")
    return data


def save_credentials(data: dict) -> Path:
    path = credentials_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    payload = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    try:
        with temporary.open("w", encoding="utf-8") as stream:
            if os.name != "nt":
                os.chmod(temporary, 0o600)
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        if os.name != "nt":
            os.chmod(path, 0o600)
    except OSError:
        temporary.unlink(missing_ok=True)
        raise
    return path


def _json_bytes(data: object) -> bytes:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _request(method: str, path: str, *, base: str | None = None, api_key: str | None = None,
             body: object | None = None, raw: bytes | None = None,
             headers: dict[str, str] | None = None, timeout: int = 120) -> tuple[bytes, dict[str, str]]:
    base_url = (base or API_BASE).rstrip("/")
    if base_url not in {API_BASE, BUU_BASE} and not re.match(r"^http://(127\.0\.0\.1|localhost)(:\d+)?$", base_url):
        raise CoderLandError("Skill 只连接固定的码境 API 或 BUU 上传主机。")
    if not path.startswith("/"):
        raise CoderLandError("API 路径必须以 / 开头。")
    request_headers = {"Accept": "application/json", "User-Agent": "CoderLand-Experiment-Skill/1.0.0"}
    if api_key:
        request_headers["X-API-Key"] = api_key
    data = raw
    if body is not None:
        data = _json_bytes(body)
        request_headers["Content-Type"] = "application/json; charset=utf-8"
    if headers:
        request_headers.update(headers)
    request = urllib.request.Request(base_url + path, data=data, headers=request_headers, method=method.upper())
    try:
        opener = urllib.request.build_opener(_TrustedRedirectHandler())
        with opener.open(request, timeout=timeout) as response:
            return response.read(), dict(response.headers.items())
    except urllib.error.HTTPError as exc:
        response_body = exc.read()
        try:
            detail = json.loads(response_body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            detail = {"error": response_body[:500].decode("utf-8", "replace") or str(exc)}
        code = str(detail.get("code") or "") if isinstance(detail, dict) else ""
        message = str(detail.get("error") or f"HTTP {exc.code}") if isinstance(detail, dict) else f"HTTP {exc.code}"
        if code == "BUU_NETWORK_REQUIRED":
            message = "请连接 BUU 后重试；未完成本次文件传输。"
        raise CoderLandError(f"{message} [{code or exc.code}]") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise CoderLandError(f"连接码境 API 失败：{exc}") from exc


def _request_json(method: str, path: str, *, base: str | None = None,
                  api_key: str | None = None, body: object | None = None,
                  raw: bytes | None = None, headers: dict[str, str] | None = None,
                  timeout: int = 120) -> dict:
    response_body, _ = _request(
        method, path, base=base, api_key=api_key, body=body, raw=raw, headers=headers, timeout=timeout
    )
    try:
        result = json.loads(response_body.decode("utf-8")) if response_body else {}
    except (ValueError, UnicodeDecodeError) as exc:
        raise CoderLandError("服务器返回的 JSON 无效。") from exc
    if not isinstance(result, dict):
        raise CoderLandError("服务器返回了非对象 JSON。")
    return result


def _request_to_file(method: str, path: str, destination: Path, *, base: str | None = None,
                     api_key: str | None = None, body: object | None = None,
                     headers: dict[str, str] | None = None, timeout: int = 3600) -> tuple[int, dict[str, str]]:
    base_url = (base or API_BASE).rstrip("/")
    if base_url not in {API_BASE, BUU_BASE} and not re.match(r"^http://(127\.0\.0\.1|localhost)(:\d+)?$", base_url):
        raise CoderLandError("Skill 只连接固定的码境 API 或 BUU 上传主机。")
    if not path.startswith("/"):
        raise CoderLandError("API 路径必须以 / 开头。")
    request_headers = {"Accept": "application/json", "User-Agent": "CoderLand-Experiment-Skill/1.0.0"}
    if api_key:
        request_headers["X-API-Key"] = api_key
    data = None
    if body is not None:
        data = _json_bytes(body)
        request_headers["Content-Type"] = "application/json; charset=utf-8"
    if headers:
        request_headers.update(headers)
    request = urllib.request.Request(base_url + path, data=data, headers=request_headers, method=method.upper())
    destination = destination.expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.part")
    written = 0
    try:
        opener = urllib.request.build_opener(_TrustedDownloadRedirectHandler())
        with opener.open(request, timeout=timeout) as response:
            response_headers = dict(response.headers.items())
            raw_length = response.headers.get("Content-Length", "")
            try:
                expected = int(raw_length)
                if expected < 0:
                    expected = None
            except (TypeError, ValueError):
                expected = None
            with temporary.open("xb") as output:
                if os.name != "nt":
                    os.chmod(temporary, 0o600)
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    output.write(chunk)
                    written += len(chunk)
                    if expected:
                        print(f"下載中：{written * 100 // expected}% ({written}/{expected} bytes)", file=sys.stderr, flush=True)
                    else:
                        print(f"下載中：{written} bytes（服务器未提供文件总大小）", file=sys.stderr, flush=True)
                output.flush()
                os.fsync(output.fileno())
            if expected is not None and written != expected:
                raise CoderLandError(f"下载未完成：收到 {written} / {expected} bytes，原文件保持不变。")
        os.replace(temporary, destination)
        return written, response_headers
    except urllib.error.HTTPError as exc:
        response_body = exc.read(64 * 1024)
        try:
            detail = json.loads(response_body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            detail = {"error": response_body[:500].decode("utf-8", "replace") or str(exc)}
        code = str(detail.get("code") or "") if isinstance(detail, dict) else ""
        message = str(detail.get("error") or f"HTTP {exc.code}") if isinstance(detail, dict) else f"HTTP {exc.code}"
        if code == "BUU_NETWORK_REQUIRED":
            message = "请连接 BUU 后重试；未完成本次文件传输。"
        raise CoderLandError(f"{message} [{code or exc.code}]") from exc
    except (urllib.error.URLError, TimeoutError, http.client.HTTPException) as exc:
        raise CoderLandError(f"连接码境 API 失败：{exc}") from exc
    except OSError as exc:
        raise CoderLandError(f"无法保存下载文件：{exc}") from exc
    finally:
        temporary.unlink(missing_ok=True)


def _request_file(method: str, path: str, file_path: Path, *, base: str,
                  api_key: str, content_type: str, timeout: int = 3600) -> tuple[bytes, dict[str, str]]:
    base_url = base.rstrip("/")
    if base_url not in {API_BASE, BUU_BASE}:
        raise CoderLandError("Skill 只连接固定的码境 API 或 BUU 上传主机。")
    if not path.startswith(API_PREFIX + "/") or any(ord(char) < 32 or ord(char) == 127 for char in path):
        raise CoderLandError("上传路径无效。")
    parsed_base = urllib.parse.urlsplit(base_url)
    headers = {
        "Accept": "application/json",
        "Content-Type": content_type,
        "Content-Length": str(file_path.stat().st_size),
        "User-Agent": "CoderLand-Experiment-Skill/1.0.0",
        "X-API-Key": api_key,
    }
    connection = http.client.HTTPSConnection(parsed_base.hostname, parsed_base.port or 443, timeout=timeout)
    try:
        connection.putrequest(method.upper(), path, skip_accept_encoding=True)
        for name, value in headers.items():
            connection.putheader(name, value)
        connection.endheaders()
        sent = 0
        total = file_path.stat().st_size
        with file_path.open("rb") as stream:
            while True:
                chunk = stream.read(8 * 1024 * 1024)
                if not chunk:
                    break
                connection.send(chunk)
                sent += len(chunk)
                if total:
                    print(f"上传附件: {sent * 100 // total}% ({sent}/{total} bytes)", flush=True)
        response = connection.getresponse()
        response_body = response.read()
        response_headers = {name: value for name, value in response.getheaders()}
        if response.status < 200 or response.status >= 300:
            try:
                detail = json.loads(response_body.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                detail = {"error": response_body[:500].decode("utf-8", "replace") or f"HTTP {response.status}"}
            code = str(detail.get("code") or "") if isinstance(detail, dict) else ""
            message = str(detail.get("error") or f"HTTP {response.status}") if isinstance(detail, dict) else f"HTTP {response.status}"
            if code == "BUU_NETWORK_REQUIRED":
                message = "请连接 BUU 后重试；未完成本次文件传输。"
            raise CoderLandError(f"{message} [{code or response.status}]")
        return response_body, response_headers
    except (http.client.HTTPException, TimeoutError, OSError) as exc:
        raise CoderLandError(f"文件上传连接中断：{exc}") from exc
    finally:
        connection.close()


def load_api_key() -> str:
    return str(load_credentials()["api_key"])


def command_login(args) -> None:
    if getattr(args, "stdin", False):
        api_key = sys.stdin.readline().strip()
        if not api_key:
            raise CoderLandError("没有从标准输入读取到 API Key；请从账号设置重新复制完整登录命令。")
    else:
        print("如果还没有 API Key，请先打开码境账号设置生成：")
        print("https://code.zuicat.cn/profile/?tab=settings")
        api_key = getpass.getpass("请在此处粘贴 API Key（输入不回显；不要发到聊天中）：").strip()
    if not re.fullmatch(r"cl_exp_[A-Za-z0-9_-]{32,64}", api_key):
        raise CoderLandError("API Key 格式无效；请在码境个人中心重新复制完整密钥。")
    path = save_credentials({"api_base_url": API_BASE, "api_key": api_key, "created_at": time.strftime("%Y-%m-%d %H:%M:%S")})
    print(f"API Key 已保存到：{path}")
    print("此命令只在本机保存密钥；如需停用，请在码境个人中心撤销。")


# Keep the old command as a compatibility alias for existing local instructions.
command_configure = command_login


def command_forget(_args) -> None:
    path = credentials_path()
    try:
        path.unlink()
    except FileNotFoundError:
        print("本机没有已保存的 API Key。")
        return
    except OSError as exc:
        raise CoderLandError(f"无法清除本机凭据 {path}: {exc}") from exc
    print("本机 API Key 凭据已清除；如需让密钥失效，请在码境个人中心撤销。")


def command_contract(_args) -> None:
    api_key = load_api_key()
    contract = _request_json("GET", API_PREFIX + "/contract", api_key=api_key)
    profiles = _request_json("GET", API_PREFIX + "/upload-profiles", api_key=api_key)
    documents = {}
    for label, key in (("guide", "guide_url"), ("openapi", "openapi_url")):
        path = str(contract.get(key) or "")
        parsed = urllib.parse.urlsplit(path)
        if (parsed.scheme or parsed.netloc or parsed.query or parsed.fragment
                or not parsed.path.startswith("/docs/") or ".." in PurePosixPath(parsed.path).parts):
            raise CoderLandError(f"服务端返回了不安全的 {label} 文档位置；停止读取。")
        raw, _headers = _request("GET", parsed.path, base=API_BASE, timeout=60)
        try:
            documents[label] = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise CoderLandError(f"服务端 {label} 文档不是 UTF-8 文本。") from exc
    print(json.dumps({"contract": contract, "upload_profiles": profiles, "documents": documents}, ensure_ascii=False, indent=2))


def api_path(path: str) -> str:
    parsed = urllib.parse.urlsplit(path)
    if parsed.scheme or parsed.netloc or parsed.fragment:
        raise CoderLandError("只允许 CoderLand 实验 API 相对路径。")
    if parsed.path.startswith("/api/experiments/"):
        path = API_PREFIX + parsed.path.removeprefix("/api/experiments")
        if parsed.query:
            path += "?" + parsed.query
    parsed_path = urllib.parse.urlsplit(path).path
    decoded_path = parsed_path
    for _ in range(8):
        if "\\" in decoded_path or any(segment in {".", ".."} for segment in decoded_path.split("/")):
            raise CoderLandError("API 路径包含不安全的路径片段。")
        if re.search(r"%(?:2f|5c)", decoded_path, re.IGNORECASE):
            raise CoderLandError("API 路径不能包含编码后的路径分隔符。")
        unquoted = urllib.parse.unquote(decoded_path)
        if unquoted == decoded_path:
            break
        decoded_path = unquoted
    else:
        raise CoderLandError("API 路径包含过深的百分号编码。")
    if "\\" in decoded_path or any(segment in {".", ".."} for segment in decoded_path.split("/")):
        raise CoderLandError("API 路径包含不安全的路径片段。")
    if parsed_path != API_PREFIX and not parsed_path.startswith(API_PREFIX + "/"):
        raise CoderLandError("Skill 只能访问 /api/v1/experiments 下的成员接口。")
    return path


def command_api(args) -> None:
    api_key = load_api_key()
    path = api_path(args.path)
    method = args.method.upper()
    if method not in {"GET", "POST", "DELETE"}:
        raise CoderLandError("仅支持 GET、POST、DELETE。")
    if method == "DELETE" and not args.confirmed_delete:
        raise CoderLandError("删除前需取得用户明确确认；确认后再加 --confirmed-delete。")
    body = None
    if args.json:
        try:
            body = json.loads(args.json)
        except ValueError as exc:
            raise CoderLandError("--json 内容必须是有效 JSON。") from exc
    headers = {}
    if method == "DELETE":
        headers["X-CoderLand-Confirm-Delete"] = "confirmed"
    if args.output:
        output = Path(args.output).expanduser()
        size, response_headers = _request_to_file(method, path, output, api_key=api_key, body=body, headers=headers)
        print(json.dumps({"saved_to": str(output.resolve()), "bytes": size,
                          "content_type": response_headers.get("Content-Type", "")}, ensure_ascii=False))
        return
    raw, _ = _request(method, path, api_key=api_key, body=body, headers=headers)
    try:
        parsed = json.loads(raw.decode("utf-8")) if raw else {}
    except (ValueError, UnicodeDecodeError):
        parsed = {"response_bytes": len(raw)}
    print(json.dumps(parsed, ensure_ascii=False, indent=2))


def _project_output_dir() -> Path:
    output_dir = Path.cwd() / "coderland-output"
    output_dir.mkdir(parents=True, exist_ok=True)
    return output_dir


def _safe_member_name(name: str) -> str:
    normalized = name.replace("\\", "/")
    path = PurePosixPath(normalized)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise CoderLandError(f"压缩包包含不安全路径：{name}")
    return path.as_posix()


def _collect_entries(source: Path, max_unpacked: int = MAX_ARCHIVE_UNPACKED) -> list[dict]:
    source = source.expanduser().resolve()
    entries = []
    total = 0
    if source.is_file() and source.suffix.lower() == ".zip":
        try:
            with zipfile.ZipFile(source) as archive:
                seen = set()
                for info in archive.infolist():
                    if info.is_dir():
                        continue
                    name = _safe_member_name(info.filename)
                    mode = (info.external_attr >> 16) & 0xFFFF
                    if (mode & 0o170000) == 0o120000:
                        raise CoderLandError(f"ZIP 不接受符号链接：{name}")
                    if info.flag_bits & 0x1:
                        raise CoderLandError(f"ZIP 加密条目无法校验：{name}")
                    if info.file_size and info.compress_size and info.file_size / info.compress_size > 1000:
                        raise CoderLandError(f"ZIP 含有异常压缩比例的文件：{name}")
                    if name in seen:
                        raise CoderLandError(f"ZIP 含有重复路径：{name}")
                    seen.add(name)
                    total += int(info.file_size)
                    if total > max_unpacked:
                        raise CoderLandError("解压后文件总量超过服务端限制。")
                    entries.append({"name": name, "size": info.file_size, "zip_path": source, "zip_name": info.filename})
        except (OSError, zipfile.BadZipFile, zipfile.LargeZipFile) as exc:
            raise CoderLandError(f"无法读取 ZIP：{exc}") from exc
    elif source.is_dir():
        for path in sorted(source.rglob("*")):
            if path.is_symlink():
                raise CoderLandError(f"目录包含符号链接，停止打包：{path}")
            if not path.is_file():
                continue
            name = _safe_member_name(path.relative_to(source).as_posix())
            total += path.stat().st_size
            if total > max_unpacked:
                raise CoderLandError("文件总量超过服务端限制。")
            entries.append({"name": name, "size": path.stat().st_size, "file_path": path})
    else:
        raise CoderLandError("输入必须是 ZIP 文件或用户指定的目录。")
    if not entries:
        raise CoderLandError("输入内容为空。")
    root_names = {entry["name"].split("/", 1)[0] for entry in entries if "/" in entry["name"]}
    has_root_files = any("/" not in entry["name"] for entry in entries)
    single_root = next(iter(root_names), "")
    # Keep semantic profile directories such as input/ and annotations/ intact.
    # In particular, a no-reference archive commonly contains only input/*;
    # stripping that sole root makes an otherwise valid archive fail validation.
    if (len(root_names) == 1 and single_root.casefold() not in {"input", "gt", "annotations"}
            and not has_root_files and all("/" in entry["name"] for entry in entries)):
        root = single_root + "/"
        if all(entry["name"].startswith(root) for entry in entries):
            for entry in entries:
                entry["name"] = entry["name"][len(root):]
    return entries


def _read_entry(entry: dict) -> bytes:
    if "file_path" in entry:
        return entry["file_path"].read_bytes()
    with zipfile.ZipFile(entry["zip_path"]) as archive:
        return archive.read(entry["zip_name"])


def _profile(profile_id: str) -> dict:
    profiles = _upload_profiles()
    for item in profiles.get("profiles", []):
        if item.get("id") == profile_id:
            result = dict(item)
            result["_limits"] = profiles.get("limits") or {}
            return result
    raise CoderLandError(f"服务端当前没有这个上传 profile：{profile_id}")


def _upload_profiles() -> dict:
    profiles = _request_json("GET", API_PREFIX + "/upload-profiles", api_key=load_api_key())
    if not isinstance(profiles.get("profiles"), list):
        raise CoderLandError("服务端返回的上传 profiles 格式无效。")
    return profiles


def _profile_limits(profile: dict) -> dict:
    limits = profile.get("_limits") or profile.get("limits") or {}
    if not isinstance(limits, dict):
        raise CoderLandError("服务端上传 profile 的 limits 格式无效。")
    return limits


def _image_files(entries: list[dict], prefix: str | None = None) -> list[dict]:
    result = []
    for entry in entries:
        path = PurePosixPath(entry["name"])
        if prefix and not entry["name"].startswith(prefix):
            continue
        if path.suffix.lower() in IMAGE_EXTENSIONS:
            result.append(entry)
    return result


def _finite_number(value: object) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(float(value))
    except (OverflowError, ValueError):
        return False


def _paired_check(entries: list[dict], masks: bool = False) -> dict:
    source_images = _image_files(entries, "input/")
    target_images = [entry for entry in entries if entry["name"].startswith("gt/") and
                     PurePosixPath(entry["name"]).suffix.lower() in (MASK_EXTENSIONS if masks else IMAGE_EXTENSIONS)]
    if not source_images or not target_images:
        raise CoderLandError("缺少 input/ 或 gt/ 中的图片/掩码。")
    if not masks:
        inputs = {entry["name"][len("input/"):] for entry in source_images}
        targets = {entry["name"][len("gt/"):] for entry in target_images}
        if inputs != targets:
            missing_gt = sorted(inputs - targets)[:5]
            missing_input = sorted(targets - inputs)[:5]
            raise CoderLandError(f"input/ 与 gt/ 图片未一一对应。缺少 GT: {missing_gt}; 缺少输入: {missing_input}")
        return {"image_count": len(inputs), "files": len(entries)}
    def by_stem(files: list[dict], prefix: str) -> dict[str, str]:
        result = {}
        for entry in files:
            relative = entry["name"][len(prefix):]
            path = PurePosixPath(relative)
            key = (path.parent.as_posix() + "/" if path.parent.as_posix() != "." else "") + path.stem
            if key in result:
                raise CoderLandError(f"同一掩码名对应多个文件，无法自动匹配：{key}")
            result[key] = relative
        return result
    inputs = by_stem(source_images, "input/")
    targets = by_stem(target_images, "gt/")
    if inputs.keys() != targets.keys():
        raise CoderLandError("input/ 图片与 gt/ 掩码未按相对路径和文件名一一对应。")
    return {"image_count": len(inputs), "files": len(entries)}


def _coco_check(entries: list[dict], profile_id: str, max_json_bytes: int = 256 * 1024 * 1024) -> tuple[dict, dict[str, str], str, bytes]:
    if profile_id == "coco-detection":
        annotation_candidates = [entry for entry in entries
                                 if entry["name"] == "annotations/instances.json"
                                 or PurePosixPath(entry["name"]).match("annotations/instances_*.json")]
        output_annotation_name = "annotations/instances.json"
    else:
        annotation_candidates = [entry for entry in entries
                                 if PurePosixPath(entry["name"]).name == "person_keypoints.json"
                                 or PurePosixPath(entry["name"]).match("annotations/person_keypoints_*.json")]
        output_annotation_name = "annotations/person_keypoints.json"
    if len(annotation_candidates) != 1:
        raise CoderLandError(f"需要且只能有一个匹配的 COCO 标注 JSON；当前找到 {len(annotation_candidates)} 个。")
    annotation = annotation_candidates[0]
    if int(annotation["size"]) > max_json_bytes:
        raise CoderLandError(f"COCO 标注 JSON 超过服务器当前限制 {max_json_bytes} 字节。")
    try:
        document = json.loads(_read_entry(annotation).decode("utf-8-sig"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise CoderLandError(f"COCO 标注 JSON 无法解析：{exc}") from exc
    if not isinstance(document, dict) or not all(isinstance(document.get(key), list) for key in ("images", "categories", "annotations")):
        raise CoderLandError("COCO JSON 必须包含 images、categories 和 annotations 数组。")
    image_entries = [entry for entry in entries if entry["name"].startswith("input/")
                     and PurePosixPath(entry["name"]).suffix.lower() in {".jpg", ".jpeg", ".png"}]
    if not image_entries:
        raise CoderLandError("COCO 图片必须是 JPG/PNG，并直接放在 input/ 下。")
    allowed_names = {entry["name"] for entry in image_entries} | {annotation["name"]}
    unexpected = sorted(entry["name"] for entry in entries if entry["name"] not in allowed_names
                        and entry["name"] not in {"input", "annotations"})
    if unexpected:
        raise CoderLandError(f"COCO ZIP 含有不支持的额外文件或目录：{unexpected[:5]}")
    if any(entry not in image_entries and entry is not annotation for entry in entries
           if entry["name"] not in {"input/", "annotations/"}):
        raise CoderLandError("COCO ZIP 只允许 input/ 图片和一个标注 JSON。")
    basename_index: dict[str, list[dict]] = {}
    path_index: dict[str, dict] = {}
    for entry in image_entries:
        basename_index.setdefault(PurePosixPath(entry["name"]).name.casefold(), []).append(entry)
        path_index[entry["name"].casefold()] = entry
    id_to_path = {}
    used_ids = set()
    renames = {}
    used_destinations = set()
    image_records = document["images"]
    if not image_records:
        raise CoderLandError("COCO images 列表为空。")
    image_names = set()
    for image in image_records:
        if not isinstance(image, dict) or "id" not in image or not isinstance(image.get("file_name"), str):
            raise CoderLandError("COCO images 中存在缺少 id/file_name 的记录。")
        if isinstance(image["id"], bool) or not isinstance(image["id"], int):
            raise CoderLandError("COCO image id 必须是整数。")
        image_id = str(image["id"])
        if image_id in used_ids:
            raise CoderLandError(f"COCO image id 重复：{image_id}")
        used_ids.add(image_id)
        file_name = image["file_name"].replace("\\", "/").lstrip("./")
        candidate = path_index.get(file_name.casefold())
        if candidate is None and file_name.startswith("input/"):
            candidate = path_index.get(file_name.casefold())
        if candidate is None:
            matches = basename_index.get(PurePosixPath(file_name).name.casefold(), [])
            if len(matches) != 1:
                raise CoderLandError(f"COCO file_name 无法唯一对应 ZIP 图片：{image['file_name']}")
            candidate = matches[0]
        basename = PurePosixPath(candidate["name"]).name
        if basename in image_names:
            raise CoderLandError(f"COCO JSON 中图片文件名重复：{basename}")
        image_names.add(basename)
        width, height = image.get("width"), image.get("height")
        if any(isinstance(value, bool) or not isinstance(value, int) or value <= 0 for value in (width, height)):
            raise CoderLandError(f"COCO images 的宽高必须是正整数：{basename}")
        destination = "input/" + basename
        if destination.casefold() in used_destinations and renames.get(candidate["name"]) != destination:
            raise CoderLandError(f"多个图片会重名，不能安全整理到 input/：{basename}")
        used_destinations.add(destination.casefold())
        renames[candidate["name"]] = destination
        image["file_name"] = basename
        id_to_path[image_id] = destination
    if image_names != {PurePosixPath(entry["name"]).name for entry in image_entries}:
        raise CoderLandError("COCO JSON 的 images 必须完整覆盖 input/ 中的图片。")
    category_records = document["categories"]
    if not category_records or any(not isinstance(item, dict) or "id" not in item for item in category_records):
        raise CoderLandError("COCO categories 中存在无效或缺失 id 的记录。")
    if any(isinstance(item["id"], bool) or not isinstance(item["id"], int) for item in category_records):
        raise CoderLandError("COCO category id 必须是整数。")
    if any(not isinstance(item.get("name"), str) or not item["name"].strip() for item in category_records):
        raise CoderLandError("COCO categories 中每个类别都必须有名称。")
    category_values = [str(item["id"]) for item in category_records]
    if len(set(category_values)) != len(category_records):
        raise CoderLandError("COCO categories 中存在重复 id。")
    category_ids = set(category_values)
    if not category_ids:
        raise CoderLandError("COCO categories 为空。")
    category_keypoints = {
        str(item["id"]): item.get("keypoints")
        for item in category_records if isinstance(item, dict)
    }
    annotation_ids = set()
    for item in document["annotations"]:
        if (not isinstance(item, dict) or isinstance(item.get("image_id"), bool)
                or not isinstance(item.get("image_id"), int) or str(item.get("image_id")) not in id_to_path):
            raise CoderLandError("COCO annotations 中有 image_id 不属于 images。")
        if (isinstance(item.get("category_id"), bool) or not isinstance(item.get("category_id"), int)
                or str(item.get("category_id")) not in category_ids):
            raise CoderLandError("COCO annotations 中有 category_id 不属于 categories。")
        if profile_id == "coco-detection":
            annotation_id = str(item.get("id", ""))
            bbox = item.get("bbox")
            if (isinstance(item.get("id"), bool) or not isinstance(item.get("id"), int)
                    or not annotation_id or annotation_id in annotation_ids):
                raise CoderLandError("COCO annotations 的 id 必须存在且唯一。")
            if (not isinstance(bbox, list) or len(bbox) != 4
                    or any(not _finite_number(value) for value in bbox)
                    or bbox[2] <= 0 or bbox[3] <= 0):
                raise CoderLandError("COCO bbox 必须包含有效的 [x, y, width, height]。")
            annotation_ids.add(annotation_id)
            if "area" in item and (
                not _finite_number(item["area"]) or item["area"] <= 0
            ):
                raise CoderLandError("COCO annotations 中的 area 必须是正有限数值。")
        if profile_id == "coco-keypoints":
            annotation_id = item.get("id")
            if isinstance(annotation_id, bool) or not isinstance(annotation_id, int) or str(annotation_id) in annotation_ids:
                raise CoderLandError("COCO 关键点 annotations 的 id 必须是唯一整数。")
            annotation_ids.add(str(annotation_id))
            keypoints = item.get("keypoints")
            category_id = str(item.get("category_id"))
            if not isinstance(keypoints, list) or len(keypoints) != 51:
                raise CoderLandError("COCO 关键点标注必须包含 17 组 x、y、v 数据。")
            if not isinstance(category_keypoints.get(category_id), list) or len(category_keypoints[category_id]) != 17:
                raise CoderLandError("COCO categories 缺少 17 个关键点定义。")
            if (not isinstance(item.get("bbox"), list) or len(item["bbox"]) != 4
                    or any(not _finite_number(value) for value in item["bbox"])
                    or item["bbox"][2] <= 0 or item["bbox"][3] <= 0):
                raise CoderLandError("COCO 关键点标注缺少 bbox。")
            area = item.get("area")
            if not _finite_number(area) or area <= 0:
                raise CoderLandError("COCO 关键点标注必须包含正有限 area。")
    if profile_id == "coco-keypoints" and not document["annotations"]:
        raise CoderLandError("COCO 关键点测试集至少需要一条人体标注。")
    if profile_id == "coco-keypoints":
        names = category_records[0].get("keypoints") if len(category_records) == 1 else None
        standard_keypoints = [
            "nose", "left_eye", "right_eye", "left_ear", "right_ear", "left_shoulder", "right_shoulder",
            "left_elbow", "right_elbow", "left_wrist", "right_wrist", "left_hip", "right_hip",
            "left_knee", "right_knee", "left_ankle", "right_ankle",
        ]
        if names != standard_keypoints:
            raise CoderLandError("姿态估计类别必须按 COCO 顺序定义 17 个关键点。")
        for item in document["annotations"]:
            keypoints = item.get("keypoints")
            if any(not _finite_number(value) for value in keypoints):
                raise CoderLandError("COCO 关键点坐标必须是有限数值。")
            if any(value not in (0, 1, 2) for value in keypoints[2::3]):
                raise CoderLandError("COCO 关键点可见性标记只能是 0、1 或 2。")
            if isinstance(item.get("num_keypoints"), bool) or not isinstance(item.get("num_keypoints"), int):
                raise CoderLandError("COCO 关键点标注缺少有效 num_keypoints。")
            if item["num_keypoints"] != sum(1 for value in keypoints[2::3] if value > 0):
                raise CoderLandError("COCO num_keypoints 与可见性标记不一致。")
            if len(item.get("bbox", [])) != 4:
                raise CoderLandError("COCO 关键点标注缺少 bbox。")
    updated = json.dumps(document, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if annotation["name"] != output_annotation_name:
        renames[annotation["name"]] = output_annotation_name
    return {"image_count": len(image_records), "annotation_count": len(document["annotations"]), "files": len(entries)}, renames, annotation["name"], updated


def _validate_profile_layout(entries: list[dict], profile_id: str) -> None:
    allowed_images = {".jpg", ".jpeg", ".png"}
    if profile_id in {"paired-restoration", "segmentation"}:
        allowed = {"input/", "gt/"}
        for entry in entries:
            name = entry["name"]
            parts = PurePosixPath(name).parts
            if len(parts) != 2 or parts[0] not in {"input", "gt"}:
                raise CoderLandError(f"测试 ZIP 只允许 input/ 和 gt/ 中的直接文件：{name}")
            suffix = PurePosixPath(name).suffix.lower()
            valid = suffix in allowed_images if parts[0] == "input" or profile_id == "paired-restoration" else suffix in MASK_EXTENSIONS
            if not valid:
                raise CoderLandError(f"服务端不接受此任务中的文件类型：{name}")
    elif profile_id == "no-reference-restoration":
        for entry in entries:
            name = entry["name"]
            if len(PurePosixPath(name).parts) != 2 or not name.startswith("input/") or PurePosixPath(name).suffix.lower() not in allowed_images:
                raise CoderLandError(f"无参考复原 ZIP 只允许 input/ 下的 JPG/PNG 图片：{name}")
    elif profile_id in {"coco-detection", "coco-keypoints"}:
        allowed_images = {".jpg", ".jpeg", ".png"}
        for entry in entries:
            name = entry["name"]
            path = PurePosixPath(name)
            if name in {"input/", "annotations/"}:
                continue
            if len(path.parts) >= 2 and path.parts[0] == "input" and path.suffix.lower() in allowed_images:
                continue
            annotation_pattern = "instances_*.json" if profile_id == "coco-detection" else "person_keypoints_*.json"
            if name == "annotations/instances.json" and profile_id == "coco-detection":
                continue
            if name == "annotations/person_keypoints.json" and profile_id == "coco-keypoints":
                continue
            if len(path.parts) == 2 and path.parts[0] == "annotations" and path.match(f"annotations/{annotation_pattern}"):
                continue
            raise CoderLandError(f"COCO ZIP 只允许 input/ 图片和 annotations/ 标注 JSON：{name}")


def validate_entries(entries: list[dict], profile_id: str, profile: dict | None = None) -> tuple[dict, dict[str, str], dict[str, bytes]]:
    profile = profile or _profile(profile_id)
    if profile.get("id") != profile_id:
        raise CoderLandError("服务端上传 profile 标识与所选 profile 不一致。")
    limits = _profile_limits(profile)
    single_image_limit = int(limits.get("single_image_bytes", 0) or 0)
    if single_image_limit:
        for entry in entries:
            suffix = PurePosixPath(entry["name"]).suffix.lower()
            if suffix in IMAGE_EXTENSIONS | MASK_EXTENSIONS and int(entry["size"]) > single_image_limit:
                raise CoderLandError(f"单个图片或掩码超过服务端当前限制：{entry['name']}")
    def checked(summary: dict) -> dict:
        max_images = int(limits.get("image_count", 0) or 0)
        if max_images and int(summary.get("image_count") or 0) > max_images:
            raise CoderLandError(f"图片数量超过服务端当前上限 {max_images}。")
        return summary
    _validate_profile_layout(entries, profile_id)
    names = {entry["name"] for entry in entries}
    if profile_id == "paired-restoration":
        if not any(name.startswith("input/") for name in names) or not any(name.startswith("gt/") for name in names):
            raise CoderLandError("所选配对复原 profile 要求 input/ 和 gt/ 目录。")
        summary = _paired_check(entries)
        return checked(summary), {}, {}
    if profile_id == "segmentation":
        if not any(name.startswith("input/") for name in names) or not any(name.startswith("gt/") for name in names):
            raise CoderLandError("分割 profile 要求 input/ 和 gt/ 目录。")
        return checked(_paired_check(entries, masks=True)), {}, {}
    if profile_id == "no-reference-restoration":
        images = _image_files(entries, "input/")
        if not images:
            raise CoderLandError("无参考复原 ZIP 的 input/ 中至少需要一张 JPG/PNG 图片。")
        return checked({"image_count": len(images), "files": len(entries)}), {}, {}
    if profile_id in {"coco-detection", "coco-keypoints"}:
        summary, renames, json_name, rewritten = _coco_check(
            entries, profile_id, int(limits.get("prediction_json_bytes", 256 * 1024 * 1024))
        )
        return checked(summary), renames, {json_name: rewritten}
    raise CoderLandError(f"本 Skill 版本不支持 profile：{profile_id}")


def command_inspect(args) -> None:
    profile = _profile(args.profile)
    limits = _profile_limits(profile)
    entries = _collect_entries(Path(args.source), int(limits.get("test_archive_unpacked_bytes", MAX_ARCHIVE_UNPACKED)))
    summary, _, _ = validate_entries(entries, args.profile, profile)
    print(json.dumps({"valid": True, "profile": args.profile, **summary}, ensure_ascii=False, indent=2))


def command_package(args) -> None:
    profile = _profile(args.profile)
    limits = _profile_limits(profile)
    entries = _collect_entries(Path(args.source), int(limits.get("test_archive_unpacked_bytes", MAX_ARCHIVE_UNPACKED)))
    summary, renames, rewritten_json = validate_entries(entries, args.profile, profile)
    output_dir = _project_output_dir()
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(args.source).stem or Path(args.source).name).strip("._-") or "dataset"
    output = output_dir / f"{stem}-{args.profile}.zip"
    temp = output.with_suffix(".zip.tmp")
    total_output = 0
    used_names = set()
    try:
        with zipfile.ZipFile(temp, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=1, allowZip64=True) as archive:
            for entry in entries:
                old_name = entry["name"]
                new_name = renames.get(old_name, old_name)
                if new_name.casefold() in used_names:
                    raise CoderLandError(f"打包后路径重复：{new_name}")
                used_names.add(new_name.casefold())
                if old_name in rewritten_json:
                    data = rewritten_json[old_name]
                    archive.writestr(new_name, data)
                    total_output += len(data)
                    continue
                with archive.open(new_name, "w") as destination:
                    if "file_path" in entry:
                        with entry["file_path"].open("rb") as source_stream:
                            shutil.copyfileobj(source_stream, destination, 1024 * 1024)
                    else:
                        with zipfile.ZipFile(entry["zip_path"]) as source_archive:
                            with source_archive.open(entry["zip_name"], "r") as source_stream:
                                shutil.copyfileobj(source_stream, destination, 1024 * 1024)
                total_output += int(entry["size"])
        os.replace(temp, output)
    except Exception:
        temp.unlink(missing_ok=True)
        raise
    archive_limit = int(limits.get("test_archive_bytes", 0) or 0)
    if archive_limit and output.stat().st_size > archive_limit:
        output.unlink(missing_ok=True)
        raise CoderLandError(f"生成 ZIP 超过服务端当前大小限制 {archive_limit} 字节，已删除生成包，源文件未改动。")
    print(json.dumps({"valid": True, "profile": args.profile, "output": str(output.resolve()),
                      "archive_bytes": output.stat().st_size, **summary}, ensure_ascii=False, indent=2))


def _require_buu(api_key: str) -> None:
    result = _request_json("GET", API_PREFIX + "/upload-network", base=BUU_BASE, api_key=api_key, timeout=20)
    if not result.get("available"):
        raise CoderLandError("请连接 BUU 后重试；尚未创建上传会话，也没有传输文件。")


def _state_file(label: str, details: dict) -> tuple[str, Path]:
    output_dir = _project_output_dir()
    signature = hashlib.sha256(_json_bytes(details)).hexdigest()[:24]
    path = output_dir / f".upload-{label}-{signature}.json"
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
        request_id = str(state.get("request_id") or "")
        if re.fullmatch(r"[0-9a-f]{32}", request_id):
            return request_id, path
    except (FileNotFoundError, OSError, ValueError):
        pass
    request_id = uuid.uuid4().hex
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps({"request_id": request_id}, separators=(",", ":")), encoding="utf-8")
    if os.name != "nt":
        os.chmod(temporary, 0o600)
    os.replace(temporary, path)
    return request_id, path


def _create_dataset_session(api_key: str, category: str, name: str, description: str,
                            test: Path, train: Path | None, request_id: str) -> dict:
    payload = {
        "category": category,
        "name": name,
        "description": description,
        "test_file": {"name": test.name, "size": test.stat().st_size},
        "request_id": request_id,
    }
    if train:
        payload["train_file"] = {"name": train.name, "size": train.stat().st_size}
    return _request_json("POST", API_PREFIX + "/datasets/upload-sessions", api_key=api_key, body=payload)


def _upload_dataset_file(api_key: str, session: dict, kind: str, path: Path, chunk_bytes: int) -> None:
    upload_id = str(session.get("upload_id") or "")
    if not re.fullmatch(r"[0-9a-f]{32}", upload_id):
        raise CoderLandError("服务器未返回有效的 BUU 上传会话。")
    status_path = f"{API_PREFIX}/datasets/upload-sessions/{upload_id}/status?kind={kind}"
    status = _request_json("GET", status_path, api_key=api_key)
    received = int(status.get("received") or 0)
    total = path.stat().st_size
    if received < 0 or received > total:
        raise CoderLandError("服务器记录的上传位置与本地文件大小不一致。")
    if received >= total:
        print(f"{kind}: 服务器已收到完整文件。")
        return
    _require_buu(api_key)
    ticket_data = _request_json(
        "POST", f"{API_PREFIX}/datasets/upload-sessions/{upload_id}/bridge-ticket",
        api_key=api_key, body={"kind": kind}, timeout=30,
    )
    ticket = str(ticket_data.get("ticket") or "")
    if not ticket:
        raise CoderLandError("服务器未能为当前文件签发新的 BUU 上传票据。")
    grant_data = _request_json(
        "POST", "/api/experiments/datasets/upload-bridge/redeem", base=BUU_BASE,
        api_key=api_key, body={"upload_id": upload_id, "kind": kind, "ticket": ticket, "size": total}, timeout=30,
    )
    grant = str(grant_data.get("upload_grant") or "")
    if not re.fullmatch(r"[A-Za-z0-9_-]{40,64}", grant):
        raise CoderLandError("BUU 没有返回有效的文件上传授权。")
    chunk_url = "/api/experiments/datasets/upload-chunk?" + urllib.parse.urlencode({"upload_id": upload_id, "kind": kind})
    sent = received
    with path.open("rb") as stream:
        stream.seek(received)
        while sent < total:
            chunk = stream.read(min(chunk_bytes, total - sent))
            if not chunk:
                raise CoderLandError("本地文件读取提前结束。")
            end = sent + len(chunk) - 1
            raw = grant.encode("ascii") + b"\n" + chunk
            ack = _request_json(
                "POST", chunk_url, base=BUU_BASE, api_key=api_key, raw=raw,
                headers={"Content-Type": "application/octet-stream", "Content-Range": f"bytes {sent}-{end}/{total}"},
                timeout=300,
            )
            sent = int(ack.get("received") or 0)
            if sent != end + 1:
                raise CoderLandError("服务器确认的分块位置不匹配，已停止传输。")
            print(f"{kind}: {sent * 100 // total}% ({sent}/{total} bytes)", flush=True)


def command_upload_dataset(args) -> None:
    api_key = load_api_key()
    test = Path(args.test).expanduser().resolve()
    train = Path(args.train).expanduser().resolve() if args.train else None
    output_dir = (Path.cwd() / "coderland-output").resolve()
    for archive in (test, train):
        if archive is None:
            continue
        try:
            archive.relative_to(output_dir)
        except ValueError as exc:
            raise CoderLandError("上传前先使用 package 生成包；只接受项目 coderland-output/ 中的新 ZIP。") from exc
        if not archive.is_file() or archive.suffix.lower() != ".zip":
            raise CoderLandError(f"无效 ZIP 文件：{archive}")
    profile = _profile(args.profile)
    if args.category not in profile.get("categories", []):
        raise CoderLandError(f"profile {args.profile} 不适用于类别 {args.category}。")
    limits = _profile_limits(profile)
    chunk_bytes = min(MAX_CHUNK_BYTES, int(limits.get("upload_chunk_bytes", MAX_CHUNK_BYTES) or MAX_CHUNK_BYTES))
    if chunk_bytes < 1:
        raise CoderLandError("服务端当前分块上传大小无效。")
    test_size_limit = int(limits.get("test_archive_bytes", 0) or 0)
    train_size_limit = int(limits.get("train_archive_bytes", 0) or 0)
    if test_size_limit and test.stat().st_size > test_size_limit:
        raise CoderLandError("测试集 ZIP 超过服务端当前大小限制。")
    if train and train_size_limit and train.stat().st_size > train_size_limit:
        raise CoderLandError("训练集 ZIP 超过服务端当前大小限制。")
    entries = _collect_entries(test, int(limits.get("test_archive_unpacked_bytes", MAX_ARCHIVE_UNPACKED)))
    summary, _renames, _rewritten = validate_entries(entries, args.profile, profile)
    if not args.name or len(args.name) > int(limits.get("dataset_name_characters", 18)):
        raise CoderLandError("数据集名称为空或超过服务端当前长度限制。")
    if len(args.description) > int(limits.get("dataset_description_characters", 20)):
        raise CoderLandError("数据集简介超过服务端当前长度限制。")
    if train:
        try:
            with zipfile.ZipFile(train) as train_archive:
                if not any(not item.is_dir() for item in train_archive.infolist()):
                    raise CoderLandError("训练集 ZIP 内容为空。")
        except (OSError, zipfile.BadZipFile, zipfile.LargeZipFile) as exc:
            raise CoderLandError(f"训练集不是可读取的 ZIP：{exc}") from exc
    count = int(summary.get("image_count") or 0)
    if count < 1:
        raise CoderLandError("测试集 ZIP 中没有可计入的受支持图片。")
    max_images = int(limits.get("image_count", 5000))
    if count > max_images:
        raise CoderLandError(f"测试集图片数超过服务端当前上限 {max_images}。")
    _require_buu(api_key)
    details = {
        "category": args.category, "name": args.name, "description": args.description,
        "test": [test.name, test.stat().st_size, test.stat().st_mtime_ns],
        "train": [train.name, train.stat().st_size, train.stat().st_mtime_ns] if train else None,
    }
    request_id, _state = _state_file("dataset", details)
    session = _create_dataset_session(api_key, args.category, args.name, args.description, test, train, request_id)
    for kind, archive in (("test", test), ("train", train)):
        if archive:
            _upload_dataset_file(api_key, session, kind, archive, chunk_bytes)
    result = _request_json("POST", API_PREFIX + "/datasets/upload-complete", api_key=api_key, body={
        "upload_id": session["upload_id"],
        "metadata": {"category": args.category, "name": args.name, "description": args.description},
        "test_image_count": count,
    }, timeout=300)
    dataset_id = str(result.get("dataset_id") or "")
    if not dataset_id:
        raise CoderLandError("服务端没有返回数据集 ID，无法确认登记状态。")
    baseline_status = str(result.get("baseline_status") or "unknown")
    deadline = time.monotonic() + 600
    while baseline_status == "pulling" and time.monotonic() < deadline:
        time.sleep(3)
        current = _request_json("GET", f"{API_PREFIX}/datasets/{urllib.parse.quote(dataset_id, safe='-')}", api_key=api_key)
        dataset = current.get("dataset") or {}
        baseline_status = str(dataset.get("baseline_status") or "unknown")
        print(f"服务端校验数据集：{baseline_status}", flush=True)
    if baseline_status == "failed":
        raise CoderLandError(f"数据集已登记但服务器基准校验失败；记录 ID：{dataset_id}。请检查 ZIP 后联系管理员处理。")
    print(json.dumps({"created": True, "test_image_count": count, "baseline_status": baseline_status,
                      "dataset_id": dataset_id, "dataset": result}, ensure_ascii=False, indent=2))


def command_upload_file(args) -> None:
    api_key = load_api_key()
    profile_data = _upload_profiles()
    profile_limits = profile_data.get("limits") or {}
    chunk_bytes = min(MAX_CHUNK_BYTES, int(profile_limits.get("upload_chunk_bytes", MAX_CHUNK_BYTES) or MAX_CHUNK_BYTES))
    if chunk_bytes < 1:
        raise CoderLandError("服务端当前分块上传大小无效。")
    path = Path(args.file).expanduser().resolve()
    if not path.is_file():
        raise CoderLandError("上传目标必须是一个普通文件。")
    target = args.target.strip()
    parsed_target = urllib.parse.urlsplit(target)
    if parsed_target.scheme or parsed_target.netloc or not parsed_target.path.startswith("/api/experiments/"):
        raise CoderLandError("目标必须是文档中的 /api/experiments/... 文件上传路径。")
    query = urllib.parse.parse_qs(parsed_target.query, keep_blank_values=True)
    query["filename"] = [path.name]
    target = parsed_target.path + "?" + urllib.parse.urlencode(query, doseq=True)
    api_target = api_path(target)
    _require_buu(api_key)
    suffix = path.suffix.lower()
    content_type = "application/zip" if suffix == ".zip" else "application/octet-stream"
    raw, _response_headers = _request_file(
        "POST", api_target, path, base=BUU_BASE, api_key=api_key,
        content_type=content_type,
    )
    try:
        result = json.loads(raw.decode("utf-8")) if raw else {}
    except (ValueError, UnicodeDecodeError):
        result = {"response_bytes": len(raw)}
    print(json.dumps({"uploaded": True, "result": result}, ensure_ascii=False, indent=2))


def _paper_pdf_bytes(path_value: str | Path) -> bytes:
    path = Path(path_value).expanduser().resolve()
    if not path.is_file() or path.suffix.lower() != ".pdf":
        raise CoderLandError("论文附件必须是 PDF 文件。")
    size = path.stat().st_size
    if not 0 < size <= MAX_PAPER_PDF_BYTES:
        raise CoderLandError("PDF 不能为空且不能超过 50 MiB。")
    with path.open("rb") as stream:
        signature = stream.read(5)
        stream.seek(max(0, size - 4096))
        ending = stream.read(4096)
        stream.seek(0)
        data = stream.read(MAX_PAPER_PDF_BYTES + 1)
    if len(data) != size:
        raise CoderLandError("读取 PDF 时文件大小发生变化，请重新选择文件。")
    if signature != b"%PDF-" or b"%%EOF" not in ending:
        raise CoderLandError("文件内容不像有效的 PDF，请检查后重新选择。")
    return data


def _paper_multipart(fields: dict[str, str], pdf_bytes: bytes | None = None) -> tuple[bytes, str]:
    allowed_fields = {"candidate_token", "dataset_id", "network_name"}
    if not fields or any(name not in allowed_fields for name in fields):
        raise CoderLandError("论文表单字段无效。")
    boundary = "----CoderLandSkill" + secrets.token_hex(16)
    parts: list[bytes] = []
    for name, value in fields.items():
        if not isinstance(value, str) or any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise CoderLandError("论文表单字段包含无效字符。")
        parts.extend((
            f"--{boundary}\r\n".encode("ascii"),
            f'Content-Disposition: form-data; name="{name}"\r\n'.encode("ascii"),
            b"Content-Type: text/plain; charset=utf-8\r\n\r\n",
            value.encode("utf-8"),
            b"\r\n",
        ))
    if pdf_bytes is not None:
        parts.extend((
            f"--{boundary}\r\n".encode("ascii"),
            b'Content-Disposition: form-data; name="pdf"; filename="paper.pdf"\r\n',
            b"Content-Type: application/pdf\r\n\r\n",
            pdf_bytes,
            b"\r\n",
        ))
    parts.append(f"--{boundary}--\r\n".encode("ascii"))
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def _paper_fields(dataset_id: str) -> dict[str, str]:
    value = dataset_id.strip()
    if not re.fullmatch(r"[a-z0-9-]{1,64}", value):
        raise CoderLandError("数据集标识无效。")
    return {"dataset_id": value}


def command_add_paper(args) -> None:
    api_key = load_api_key()
    fields = _paper_fields(args.dataset_id)
    query = args.query.strip()
    network_name = args.network_name.strip()
    if not 2 <= len(query) <= 512 or any(ord(char) < 32 or ord(char) == 127 for char in query):
        raise CoderLandError("论文搜索词长度应为 2–512 个字符。")
    if not 1 <= len(network_name) <= 255 or any(ord(char) < 32 or ord(char) == 127 for char in network_name):
        raise CoderLandError("网络简称长度应为 1–255 个字符，且不能包含控制字符。")
    pdf_bytes = _paper_pdf_bytes(args.pdf) if args.pdf else None
    search = _request_json(
        "POST", API_PREFIX + "/papers/search", api_key=api_key,
        body={"dataset_id": fields["dataset_id"], "query": query},
    )
    candidates = search.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        raise CoderLandError("没有找到可添加的论文候选项。")
    print("论文候选（选择序号以继续；取消或输入无效内容不会创建论文）：")
    for index, candidate in enumerate(candidates, start=1):
        if not isinstance(candidate, dict):
            continue
        title = str(candidate.get("title") or "（无标题）")
        authors = str(candidate.get("authors") or "")
        year = str(candidate.get("year") or "")
        venue = str(candidate.get("venue") or "")
        doi = str(candidate.get("doi") or "")
        details = " · ".join(value for value in (authors, year, venue, doi) if value)
        print(f"{index}. {title}" + (f"\n   {details}" if details else ""))
    try:
        selection = input("请选择论文序号：").strip()
        selected_index = int(selection) - 1
    except (EOFError, ValueError) as exc:
        raise CoderLandError("没有取得有效的论文选择，未添加论文。") from exc
    if not 0 <= selected_index < len(candidates) or not isinstance(candidates[selected_index], dict):
        raise CoderLandError("论文序号无效，未添加论文。")
    candidate_token = str(candidates[selected_index].get("candidate_token") or "")
    if not candidate_token:
        raise CoderLandError("候选论文缺少服务端确认令牌，请重新搜索。")
    fields["candidate_token"] = candidate_token
    fields["network_name"] = network_name
    request_body, content_type = _paper_multipart(fields, pdf_bytes)
    result = _request_json(
        "POST", API_PREFIX + "/papers", api_key=api_key, raw=request_body,
        headers={"Content-Type": content_type}, timeout=3600,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


def command_upload_paper_pdf(args) -> None:
    api_key = load_api_key()
    fields = _paper_fields(args.dataset_id)
    paper_id = args.paper_id.strip().lower()
    if not re.fullmatch(r"[0-9a-f]{32}", paper_id):
        raise CoderLandError("论文记录 ID 无效。")
    pdf_bytes = _paper_pdf_bytes(args.pdf)
    request_body, content_type = _paper_multipart(fields, pdf_bytes)
    result = _request_json(
        "POST", f"{API_PREFIX}/papers/{paper_id}/pdf", api_key=api_key, raw=request_body,
        headers={"Content-Type": content_type}, timeout=3600,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


def _http_error_status(exc: CoderLandError) -> int | None:
    cause = exc.__cause__
    return int(cause.code) if isinstance(cause, urllib.error.HTTPError) else None


def _find_submission_in_dataset(api_key: str, dataset_id: str, submission_id: str) -> dict | None:
    path = f"{API_PREFIX}/datasets/{urllib.parse.quote(dataset_id, safe='-')}"
    response = _request_json("GET", path, api_key=api_key)
    dataset = response.get("dataset") or {}
    for collection_name in ("submissions", "pending_submissions"):
        records = dataset.get(collection_name) or []
        if not isinstance(records, list):
            continue
        for record in records:
            if isinstance(record, dict) and str(record.get("id") or "") == submission_id:
                return {"collection": collection_name, "record": record}
    return None


def _report_catalog_finalize_result(api_key: str, dataset_id: str, staging_id: str) -> bool:
    found = _find_submission_in_dataset(api_key, dataset_id, staging_id)
    if not found:
        return False
    record = found["record"]
    if found["collection"] == "submissions" and str(record.get("workflow_status") or "") == "completed":
        print(json.dumps({"finalized": True, "confirmed_by": "dataset_catalog", "submission": record},
                         ensure_ascii=False, indent=2))
        return True
    status = str(record.get("status") or record.get("workflow_status") or "pending")
    print(f"提交记录仍在目录的待处理列表中：{status}。不会重复调用 finalize。", flush=True)
    return False


def _wait_after_finalize_uncertainty(api_key: str, dataset_id: str, staging_id: str, wait_timeout: int) -> bool:
    deadline = time.monotonic() + max(0, wait_timeout)
    while True:
        try:
            stage = _request_json("GET", f"{API_PREFIX}/staging/{staging_id}", api_key=api_key)
            if str(stage.get("dataset_id") or "") != dataset_id:
                raise CoderLandError("暂存记录属于另一个数据集；停止确认并且没有重试 finalize。")
            status = str(stage.get("status") or "unknown")
            message = str(stage.get("status_message") or status)
            print(f"正在等待评测：{message}", flush=True)
            if status == "failed":
                raise CoderLandError(f"服务端报告评测失败：{message}")
        except CoderLandError as exc:
            if _http_error_status(exc) != 404:
                raise
            if _report_catalog_finalize_result(api_key, dataset_id, staging_id):
                return True
            if time.monotonic() >= deadline:
                raise CoderLandError(
                    "暂存记录已不存在，数据集目录也暂未找到对应完成记录；结果无法确认。为避免重复提交，没有再次调用 finalize。"
                ) from exc
            print("暂存记录返回 404，正在数据集目录中确认同 ID 的复现记录；不会重发提交。", flush=True)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise CoderLandError("评测仍未进入可确认状态；暂存 ID 已保留，未再次调用 finalize。")
        time.sleep(min(3, remaining))


def command_finalize(args) -> None:
    api_key = load_api_key()
    dataset_id = _paper_fields(args.dataset_id)["dataset_id"]
    staging_id = args.staging_id.strip().lower()
    if not re.fullmatch(r"[0-9a-f]{32}", staging_id):
        raise CoderLandError("暂存记录 ID 无效。")
    staging_path = f"{API_PREFIX}/staging/{staging_id}"
    try:
        stage = _request_json("GET", staging_path, api_key=api_key)
    except CoderLandError as exc:
        if _http_error_status(exc) == 404:
            if _report_catalog_finalize_result(api_key, dataset_id, staging_id):
                return
            raise CoderLandError("暂存记录不存在，且数据集目录没有同 ID 的已完成复现；未调用 finalize。") from exc
        raise
    if str(stage.get("dataset_id") or "") != dataset_id:
        raise CoderLandError("暂存记录属于另一个数据集；未调用 finalize。")
    status = str(stage.get("status") or "")
    if status == "evaluating":
        print("正在等待评测；暂存记录已处于评测中，不会重复调用 finalize。", flush=True)
        _wait_after_finalize_uncertainty(api_key, dataset_id, staging_id, args.wait_timeout)
        return
    if status != "uploading":
        raise CoderLandError(f"暂存记录状态为 {status or 'unknown'}，未调用 finalize。")
    print("正在等待评测（服务端完成后会返回指标）…", flush=True)
    try:
        result = _request_json(
            "POST", staging_path + "/finalize", api_key=api_key, body={}, timeout=args.request_timeout,
        )
    except CoderLandError as exc:
        status_code = _http_error_status(exc)
        transport_uncertain = status_code is None
        response_uncertain = status_code in {404, 502, 503}
        if not (transport_uncertain or response_uncertain):
            raise
        print("finalize 请求超时或响应丢失；先只读核对状态，不会重复提交。", flush=True)
        if _wait_after_finalize_uncertainty(api_key, dataset_id, staging_id, args.wait_timeout):
            return
        raise CoderLandError("无法确认最终评测结果；没有再次调用 finalize。") from exc
    print(json.dumps(result, ensure_ascii=False, indent=2))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="码境实验 Agent Skill 内置辅助脚本（Python 标准库）")
    commands = parser.add_subparsers(dest="command", required=True)
    login = commands.add_parser("login", help="安全配置码境 API Key（本机隐藏输入）")
    login.add_argument("--stdin", action="store_true", help="从标准输入读取 Key，供账号设置页生成的 CMD 命令使用")
    login.set_defaults(func=command_login)
    configure = commands.add_parser("configure", help="旧命令兼容别名：保存 API Key")
    configure.add_argument("--stdin", action="store_true", help=argparse.SUPPRESS)
    configure.set_defaults(func=command_login)
    forget = commands.add_parser("forget", help="清除本机保存的 API Key")
    forget.set_defaults(func=command_forget)
    contract = commands.add_parser("contract", help="读取服务端最新 API 与上传规则")
    contract.set_defaults(func=command_contract)
    inspect = commands.add_parser("inspect", help="按服务器 profile 只读检查 ZIP 或目录")
    inspect.add_argument("--profile", required=True)
    inspect.add_argument("--source", required=True)
    inspect.set_defaults(func=command_inspect)
    package = commands.add_parser("package", help="验证并在 coderland-output/ 生成新 ZIP")
    package.add_argument("--profile", required=True)
    package.add_argument("--source", required=True)
    package.set_defaults(func=command_package)
    dataset = commands.add_parser("upload-dataset", help="通过 BUU 创建并上传用户数据集")
    dataset.add_argument("--profile", required=True)
    dataset.add_argument("--category", required=True)
    dataset.add_argument("--name", required=True)
    dataset.add_argument("--description", required=True)
    dataset.add_argument("--test", required=True)
    dataset.add_argument("--train")
    dataset.set_defaults(func=command_upload_dataset)
    upload_file = commands.add_parser("upload-file", help="通过 BUU 上传一个复现附件")
    upload_file.add_argument("--target", required=True)
    upload_file.add_argument("--file", required=True)
    upload_file.set_defaults(func=command_upload_file)
    add_paper = commands.add_parser("add-paper", help="搜索候选论文并将用户选定的论文添加到数据集")
    add_paper.add_argument("--dataset-id", required=True)
    add_paper.add_argument("--query", required=True)
    add_paper.add_argument("--network-name", required=True)
    add_paper.add_argument("--pdf", help="可选的论文 PDF，最大 50 MiB")
    add_paper.set_defaults(func=command_add_paper)
    upload_paper_pdf = commands.add_parser("upload-paper-pdf", help="向已有论文记录上传 PDF")
    upload_paper_pdf.add_argument("--paper-id", required=True)
    upload_paper_pdf.add_argument("--dataset-id", required=True)
    upload_paper_pdf.add_argument("--pdf", required=True)
    upload_paper_pdf.set_defaults(func=command_upload_paper_pdf)
    finalize = commands.add_parser("finalize", help="完成评测并在超时后只读确认最终提交状态")
    finalize.add_argument("--staging-id", required=True)
    finalize.add_argument("--dataset-id", required=True)
    finalize.add_argument("--request-timeout", type=int, default=3600, help="finalize 请求超时秒数")
    finalize.add_argument("--wait-timeout", type=int, default=600, help="超时后只读核对状态的等待秒数")
    finalize.set_defaults(func=command_finalize)
    api = commands.add_parser("api", help="调用 /api/v1/experiments 成员 API")
    api.add_argument("method", choices=("GET", "POST", "DELETE"))
    api.add_argument("path", help="/api/v1/experiments/... 或服务器返回的对应资源路径")
    api.add_argument("--json", help="JSON 请求体")
    api.add_argument("--output", help="将二进制响应保存到这个路径")
    api.add_argument("--confirmed-delete", action="store_true", help="已取得用户对永久删除的明确确认")
    api.set_defaults(func=command_api)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        args.func(args)
        return 0
    except CoderLandError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("已中断。", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
