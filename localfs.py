"""Локальная «Firestore» на ПК: тот же REST, что у Google, но данные лежат в db/localfs.json.

Мини-апп и бот ходят сюда вместо firestore.googleapis.com, когда в консоли выбран режим «ПК».
Хранится сырой Firestore-JSON (stringValue / integerValue / ...), поэтому приложению ничего переписывать не нужно.
Поддержано ровно то, чем пользуются приложение и бот: get / patch (updateMask, currentDocument) / create / delete /
list / runQuery (фильтры, orderBy, limit) / runAggregationQuery (count) / commit (транзакция).
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from aiohttp import web

_PATH_RE = re.compile(r"^/v1/projects/([^/]+)/databases/([^/]+)/documents(.*)$")


def _now_ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def pyval(v: dict) -> Any:
    """Firestore-значение -> python."""
    if not isinstance(v, dict):
        return None
    if "integerValue" in v:
        return int(v["integerValue"])
    if "doubleValue" in v:
        return float(v["doubleValue"])
    if "stringValue" in v:
        return v["stringValue"]
    if "booleanValue" in v:
        return bool(v["booleanValue"])
    if "timestampValue" in v:
        return v["timestampValue"]
    if "arrayValue" in v:
        return [pyval(x) for x in (v["arrayValue"].get("values") or [])]
    if "mapValue" in v:
        return {k: pyval(x) for k, x in (v["mapValue"].get("fields") or {}).items()}
    return None


def _err(code: int, status: str, msg: str) -> web.Response:
    return web.json_response({"error": {"code": code, "message": msg, "status": status}}, status=code)


class LocalFS:
    def __init__(self, path: Path):
        self.path = path
        self.data: dict[str, dict[str, dict]] = {}
        self._dirty = False
        self._flush_task: asyncio.Task | None = None
        self.load()

    # ---------- storage ----------
    def load(self) -> None:
        try:
            self.data = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {}
        except Exception:
            bak = self.path.with_suffix(".broken")
            try:
                self.path.replace(bak)
            except Exception:
                pass
            self.data = {}

    def flush(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.path)
        self._dirty = False

    def touch(self) -> None:
        self._dirty = True
        if self._flush_task is None or self._flush_task.done():
            try:
                self._flush_task = asyncio.get_running_loop().create_task(self._flush_later())
            except RuntimeError:
                self.flush()

    async def _flush_later(self) -> None:
        await asyncio.sleep(1.5)
        if self._dirty:
            try:
                self.flush()
            except Exception:
                pass

    def counts(self) -> dict[str, int]:
        return {k: len(v) for k, v in self.data.items()}

    def put_raw(self, coll: str, doc_id: str, fields: dict) -> None:
        now = _now_ts()
        cur = self.data.setdefault(coll, {}).get(doc_id)
        self.data[coll][doc_id] = {"fields": fields, "createTime": (cur or {}).get("createTime", now), "updateTime": now}
        self.touch()

    # ---------- helpers ----------
    @staticmethod
    def _doc_json(p: str, d: str, coll: str, doc_id: str, doc: dict) -> dict:
        return {"name": f"projects/{p}/databases/{d}/documents/{coll}/{doc_id}", "fields": doc["fields"],
                "createTime": doc["createTime"], "updateTime": doc["updateTime"]}

    def _check_precond(self, doc: dict | None, cond: dict | None) -> web.Response | None:
        if not cond:
            return None
        if "exists" in cond:
            want = cond["exists"] in (True, "true", "True")
            if want and doc is None:
                return _err(404, "NOT_FOUND", "Document to update not found")
            if not want and doc is not None:
                return _err(409, "ALREADY_EXISTS", "Document already exists")
        if cond.get("updateTime") and (doc is None or doc["updateTime"] != cond["updateTime"]):
            return _err(400, "FAILED_PRECONDITION", "Precondition failed: updateTime mismatch")
        return None

    def _apply_update(self, coll: str, doc_id: str, fields: dict, mask: list[str] | None) -> dict:
        cur = self.data.setdefault(coll, {}).get(doc_id)
        now = _now_ts()
        if mask:
            base = dict(cur["fields"]) if cur else {}
            for m in mask:
                top = m.split(".")[0].strip("`")
                if top in fields:
                    base[top] = fields[top]
                else:
                    base.pop(top, None)
            new_fields = base
        else:
            new_fields = fields
        doc = {"fields": new_fields, "createTime": cur["createTime"] if cur else now, "updateTime": now}
        self.data[coll][doc_id] = doc
        self.touch()
        return doc

    # ---------- queries ----------
    @staticmethod
    def _match(doc: dict, flt: dict | None) -> bool:
        if not flt:
            return True
        if "compositeFilter" in flt:
            cf = flt["compositeFilter"]
            res = [LocalFS._match(doc, f) for f in cf.get("filters", [])]
            return any(res) if cf.get("op") == "OR" else all(res)
        if "fieldFilter" in flt:
            ff = flt["fieldFilter"]
            field = ff["field"]["fieldPath"]
            have = pyval(doc["fields"].get(field)) if field in doc["fields"] else None
            want = pyval(ff.get("value", {}))
            op = ff.get("op")
            if have is None:
                return False
            try:
                return {"EQUAL": have == want, "NOT_EQUAL": have != want, "LESS_THAN": have < want,
                        "LESS_THAN_OR_EQUAL": have <= want, "GREATER_THAN": have > want,
                        "GREATER_THAN_OR_EQUAL": have >= want}.get(op, False)
            except TypeError:
                return False
        return True

    def run_query(self, sq: dict) -> list[tuple[str, str, dict]]:
        coll = (sq.get("from") or [{}])[0].get("collectionId", "")
        rows = [(coll, i, d) for i, d in self.data.get(coll, {}).items() if self._match(d, sq.get("where"))]
        for ob in reversed(sq.get("orderBy") or []):
            fp = ob["field"]["fieldPath"]
            rev = ob.get("direction") == "DESCENDING"

            def keyf(r, fp=fp):
                v = pyval(r[2]["fields"].get(fp)) if fp in r[2]["fields"] else None
                return (v is not None, v if v is not None else 0)
            rows.sort(key=keyf, reverse=rev)
        if sq.get("limit"):
            rows = rows[: int(sq["limit"])]
        return rows

    # ---------- HTTP ----------
    async def handle(self, request: web.Request) -> web.Response:
        m = _PATH_RE.match(request.path)
        if not m:
            return _err(404, "NOT_FOUND", "Not found")
        p, d, rest = m.groups()
        q = request.rel_url.query
        method = request.method

        if rest.startswith(":"):                       # :runQuery / :runAggregationQuery / :commit
            body = await _json(request)
            if rest == ":runQuery":
                rows = self.run_query(body.get("structuredQuery") or {})
                rt = _now_ts()
                if not rows:
                    return web.json_response([{"readTime": rt}])
                return web.json_response([{"document": self._doc_json(p, d, c, i, doc), "readTime": rt} for c, i, doc in rows])
            if rest == ":runAggregationQuery":
                sq = (body.get("structuredAggregationQuery") or {}).get("structuredQuery") or {}
                n = len(self.run_query(sq))
                return web.json_response([{"result": {"aggregateFields": {"n": {"integerValue": str(n)}}}, "readTime": _now_ts()}])
            if rest == ":commit":
                return self._commit(p, d, body)
            return _err(400, "INVALID_ARGUMENT", "Unsupported method")

        parts = [x for x in rest.split("/") if x]
        if not parts:
            return _err(400, "INVALID_ARGUMENT", "Collection required")
        coll = parts[0]
        doc_id = "/".join(parts[1:]) if len(parts) > 1 else ""

        if not doc_id:                                  # collection level
            if method == "GET":                         # list
                size = int(q.get("pageSize", "300"))
                ids = sorted(self.data.get(coll, {}))
                start = int(q.get("pageToken", "0") or 0)
                chunk = ids[start:start + size]
                out: dict[str, Any] = {}
                if chunk:
                    out["documents"] = [self._doc_json(p, d, coll, i, self.data[coll][i]) for i in chunk]
                if start + size < len(ids):
                    out["nextPageToken"] = str(start + size)
                return web.json_response(out)
            if method == "POST":                        # create
                body = await _json(request)
                new_id = q.get("documentId") or secrets.token_hex(10)
                if new_id in self.data.get(coll, {}):
                    return _err(409, "ALREADY_EXISTS", "Document already exists")
                doc = self._apply_update(coll, new_id, body.get("fields") or {}, None)
                return web.json_response(self._doc_json(p, d, coll, new_id, doc))
            return _err(405, "INVALID_ARGUMENT", "Method not allowed")

        cur = self.data.get(coll, {}).get(doc_id)
        if method == "GET":
            if cur is None:
                return _err(404, "NOT_FOUND", "Document not found")
            return web.json_response(self._doc_json(p, d, coll, doc_id, cur))
        if method == "DELETE":
            if cur is not None:
                del self.data[coll][doc_id]
                self.touch()
            return web.json_response({})
        if method == "PATCH":
            cond = {}
            if "currentDocument.exists" in q:
                cond["exists"] = q["currentDocument.exists"]
            if "currentDocument.updateTime" in q:
                cond["updateTime"] = q["currentDocument.updateTime"]
            bad = self._check_precond(cur, cond)
            if bad is not None:
                return bad
            body = await _json(request)
            mask = q.getall("updateMask.fieldPaths", []) or None
            doc = self._apply_update(coll, doc_id, body.get("fields") or {}, mask)
            return web.json_response(self._doc_json(p, d, coll, doc_id, doc))
        return _err(405, "INVALID_ARGUMENT", "Method not allowed")

    def _commit(self, p: str, d: str, body: dict) -> web.Response:
        writes = body.get("writes") or []
        pending = []
        # 1) проверяем все условия до применения — транзакция атомарна
        for w in writes:
            if "update" in w:
                name = w["update"]["name"]
            elif "delete" in w:
                name = w["delete"]
            else:
                return _err(400, "INVALID_ARGUMENT", "Unsupported write")
            tail = name.split("/documents/", 1)[-1]
            coll, _, doc_id = tail.partition("/")
            cur = self.data.get(coll, {}).get(doc_id)
            bad = self._check_precond(cur, w.get("currentDocument"))
            if bad is not None:
                return bad
            pending.append((w, coll, doc_id))
        # 2) применяем
        results = []
        for w, coll, doc_id in pending:
            if "update" in w:
                mask = (w.get("updateMask") or {}).get("fieldPaths")
                doc = self._apply_update(coll, doc_id, w["update"].get("fields") or {}, mask)
                results.append({"updateTime": doc["updateTime"]})
            else:
                self.data.get(coll, {}).pop(doc_id, None)
                self.touch()
                results.append({})
        return web.json_response({"writeResults": results, "commitTime": _now_ts()})


async def _json(request: web.Request) -> dict:
    try:
        return await request.json()
    except Exception:
        return {}


# ====================== сервер + консоль ======================
CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, POST, PATCH, DELETE, OPTIONS",
    "Access-Control-Max-Age": "86400",
}


@web.middleware
async def cors_mw(request: web.Request, handler):
    if request.method == "OPTIONS":
        resp = web.Response(status=204)
    else:
        try:
            resp = await handler(request)
        except web.HTTPException as e:
            resp = e
    resp.headers.update(CORS_HEADERS)
    resp.headers["Access-Control-Allow-Headers"] = request.headers.get("Access-Control-Request-Headers", "*")
    return resp


def _is_local(request: web.Request) -> bool:
    """Консоль — только с этого же ПК: не через туннель (туннель добавляет X-Forwarded-*)."""
    if request.remote not in ("127.0.0.1", "::1"):
        return False
    for h in ("X-Forwarded-For", "X-Forwarded-Host", "Cf-Connecting-Ip", "Forwarded", "X-Real-Ip"):
        if h in request.headers:
            return False
    origin = request.headers.get("Origin")
    if origin and not re.match(r"^https?://(127\.0\.0\.1|localhost)(:\d+)?$", origin):
        return False
    return True


def build_app(store: LocalFS, ctx: dict[str, Callable]) -> web.Application:
    """ctx: get_mode(), set_mode(m), status() -> dict, fb_test() -> dict, fb_import() -> dict, ext_test() -> dict."""
    app = web.Application(middlewares=[cors_mw], client_max_size=4 * 1024 * 1024)

    async def mode(request):
        return web.json_response({"mode": ctx["get_mode"](), "ts": int(time.time() * 1000)})

    async def ping(request):
        return web.json_response({"ok": True})

    def guard(fn):
        async def inner(request):
            if not _is_local(request):
                return web.json_response({"error": "console is local only"}, status=403)
            return await fn(request)
        return inner

    async def console_page(request):
        html_path = Path(__file__).with_name("console.html")
        return web.Response(text=html_path.read_text(encoding="utf-8"), content_type="text/html")

    async def status(request):
        return web.json_response(await ctx["status"]())

    async def set_mode(request):
        body = await _json(request)
        m = body.get("mode")
        if m not in ("pc", "firebase"):
            return web.json_response({"error": "bad mode"}, status=400)
        ctx["set_mode"](m)
        return web.json_response(await ctx["status"]())

    async def fb_test(request):
        return web.json_response(await ctx["fb_test"]())

    async def fb_import(request):
        return web.json_response(await ctx["fb_import"]())

    async def ext_test(request):
        return web.json_response(await ctx["ext_test"]())

    app.router.add_get("/mode", mode)
    app.router.add_get("/ping", ping)
    app.router.add_get("/console", guard(console_page))
    app.router.add_get("/admin/status", guard(status))
    app.router.add_post("/admin/mode", guard(set_mode))
    app.router.add_post("/admin/fbtest", guard(fb_test))
    app.router.add_post("/admin/import", guard(fb_import))
    app.router.add_post("/admin/exttest", guard(ext_test))
    app.router.add_route("*", "/v1/{tail:.*}", store.handle)
    return app


async def start_server(app: web.Application, host: str, port: int) -> web.AppRunner:
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    await web.TCPSite(runner, host, port).start()
    return runner
