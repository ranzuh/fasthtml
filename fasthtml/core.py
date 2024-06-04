import json,dateutil,uuid,inspect

from fastcore.utils import *
from fastcore.xml import *

from types import UnionType, SimpleNamespace as ns
from typing import Optional, get_type_hints, get_args, get_origin, Union, Mapping, TypedDict
from datetime import datetime
from dataclasses import dataclass,fields,is_dataclass,MISSING,asdict
from collections import namedtuple
from inspect import isfunction,ismethod,signature,Parameter,get_annotations
from functools import wraps, partialmethod

from .starlette import *


empty = Parameter.empty

def is_typeddict(cls:type)->bool:
    attrs = 'annotations', 'required_keys', 'optional_keys'
    return isinstance(cls, type) and all(hasattr(cls, f'__{attr}__') for attr in attrs)

def is_namedtuple(cls):
    "`True` is `cls` is a namedtuple type"
    return issubclass(cls, tuple) and hasattr(cls, '_fields')

def date(s:str):
    "Convert `s` to a datetime"
    return dateutil.parser.parse(s)

def snake2hyphens(s:str):
    "Convert `s` from snake case to hyphenated and capitalised"
    s = snake2camel(s)
    return camel2words(s, '-')

htmx_hdrs = dict(
    boosted="HX-Boosted",
    current_url="HX-Current-URL",
    history_restore_request="HX-History-Restore-Request",
    prompt="HX-Prompt",
    request="HX-Request",
    target="HX-Target",
    trigger_name="HX-Trigger-Name",
    trigger="HX-Trigger")

@dataclass
class HtmxHeaders:
    boosted:str|None=None; current_url:str|None=None; history_restore_request:str|None=None; prompt:str|None=None
    request:str|None=None; target:str|None=None; trigger_name:str|None=None; trigger:str|None=None
    def __bool__(self): return any(hasattr(self,o) for o in htmx_hdrs)

def _get_htmx(req):
    res = {k:req.headers.get(v.lower(), None) for k,v in htmx_hdrs.items()}
    return HtmxHeaders(**res)

def str2int(s)->int:
    "Convert `s` to an `int`"
    s = s.lower()
    if s=='on': return 1
    if s=='none': return 0
    return 0 if not s else int(s)

def _fix_anno(t):
    "Create appropriate callable type for casting a `str` to type `t` (or first type in `t` if union)"
    origin = get_origin(t)
    if origin is Union or origin is UnionType:
        t = first(o for o in get_args(t) if o!=type(None))
    d = {bool: str2bool, int: str2int}
    return d.get(t, t)

def _form_arg(k, v, d):
    "Get type by accessing key `k` from `d`, and use to cast `v`"
    if v is None: return
    anno = d.get(k, None)
    if not anno: return v
    return _fix_anno(anno)(v)

def _is_body(anno):
    return issubclass(anno, (dict,ns)) or is_dataclass(anno) or is_namedtuple(anno) or \
        get_annotations(anno) or is_typeddict(anno)

def _anno2flds(anno):
    if is_dataclass(anno): return {o.name:o.type for o in fields(anno)}
    if is_namedtuple(anno): return {o:str for o in anno._fields}
    annoanno = get_annotations(anno)
    if annoanno: return annoanno
    return {}

async def _from_body(req, arg, p):
    body = await req.form()
    anno = p.annotation
    d = _anno2flds(anno)
    cargs = {k:_form_arg(k, v, d) for k,v in body.items()}
    return anno(**cargs)

async def _find_p(req, arg:str, p):
    anno = p.annotation
    if arg.lower()=='auth': return req.scope['auth']
    if isinstance(anno, type):
        if issubclass(anno, Request): return req
        if issubclass(anno, HtmxHeaders): return _get_htmx(req)
        if issubclass(anno, Starlette): return req.scope['app']
        if _is_body(anno): return await _from_body(req, arg, p)
    if anno is empty:
        if 'request'.startswith(arg.lower()): return req
        if 'session'.startswith(arg.lower()): return req.scope.get('session', {})
        if arg.lower()=='htmx': return _get_htmx(req)
        if arg.lower()=='app': return req.scope['app']
        return None
    res = req.path_params.get(arg, None)
    if not res: res = req.query_params.get(arg, None)
    if not res: res = req.cookies.get(arg, None)
    if not res: res = req.headers.get(snake2hyphens(arg), None)
    if not res: res = nested_idx(req.scope, 'session', arg) or None
    if res is empty or res is None:
        body = await req.form()
        res = body.get(arg, None)
    if not res: res = p.default
    if not isinstance(res, str) or anno is empty: return res
    return _fix_anno(anno)(res)

async def _wrap_req(req, params):
    return [await _find_p(req, arg, p) for arg,p in params.items()]

@dataclass
class HttpHeader: k:str;v:str

def _xt_resp(req, resp, hdrs, **bodykw):
    http_hdrs,resp = partition(resp, risinstance(HttpHeader))
    http_hdrs = {o.k:str(o.v) for o in http_hdrs}
    titles,bdy = partition(resp, lambda o: getattr(o, 'tag', '')=='title')
    if resp and 'hx-request' not in req.headers and isinstance(resp,tuple) and titles:
        resp = Html(Header(titles[0], *hdrs), Body(bdy, **bodykw))
    return HTMLResponse(to_xml(resp), headers=http_hdrs)

def _wrap_resp(req, resp, cls, hdrs, **bodykw):
    if isinstance(resp, Response): return resp
    if cls is not empty: return cls(resp)
    if isinstance(resp, (list,tuple)): return _xt_resp(req, resp, hdrs, **bodykw)
    if isinstance(resp, str): cls = HTMLResponse
    elif isinstance(resp, Mapping): cls = JSONResponse
    else:
        resp = str(resp)
        cls = HTMLResponse
    return cls(resp)

def _wrap_ep(f, hdrs, before, **bodykw):
    if not (isfunction(f) or ismethod(f)): return f
    sig = signature(f)
    params = sig.parameters
    cls = sig.return_annotation

    async def _f(req):
        resp = None
        for b in before:
            if not resp:
                wreq = await _wrap_req(req, signature(b).parameters)
                resp = b(*wreq)
                if is_async_callable(b): resp = await resp
        if not resp:
            wreq = await _wrap_req(req, params)
            resp = f(*wreq)
            if is_async_callable(f): resp = await resp
        return _wrap_resp(req, resp, cls, hdrs, **bodykw)
    return _f

class RouteX(Route):
    def __init__(self, path:str, endpoint, *, methods=None, name=None, include_in_schema=True, middleware=None,
                hdrs=None, before=None, **bodykw):
        super().__init__(path, _wrap_ep(endpoint, hdrs, before, **bodykw), methods=methods, name=name,
                         include_in_schema=include_in_schema, middleware=middleware)

class RouterX(Router):
    def __init__(self, routes=None, redirect_slashes=True, default=None, on_startup=None, on_shutdown=None,
                 lifespan=None, *, middleware=None, hdrs=None, before=None, **bodykw):
        super().__init__(routes, redirect_slashes, default, on_startup, on_shutdown,
                 lifespan=lifespan, middleware=middleware)
        self.hdrs,self.bodykw,self.before = hdrs or (),bodykw,before or ()

    def add_route( self, path: str, endpoint: callable, methods=None, name=None, include_in_schema=True):
        route = RouteX(path, endpoint=endpoint, methods=methods, name=name, include_in_schema=include_in_schema,
                      hdrs=self.hdrs, before=self.before, **self.bodykw)
        self.routes = [o for o in self.routes if o.methods!=methods or o.path!=path]
        self.routes.append(route)

htmxscr = Script(
    src="https://unpkg.com/htmx.org@1.9.12", crossorigin="anonymous",
    integrity="sha384-ujb1lZYygJmzgSwoxRggbCHcjc0rB2XoQrxeTUQyRjrOnlCoYta87iKBWq3EsdM2")

def get_key(key=None, fname='.sesskey'):
    if key: return key
    fname = Path(fname)
    if fname.exists(): return fname.read_text()
    key = str(uuid.uuid4())
    fname.write_text(key)
    return key

def _list(o): return [] if not o else o if isinstance(o, (tuple,list)) else [o]

class FastHTML(Starlette):
    def __init__(self, debug=False, routes=None, middleware=None, exception_handlers=None,
                 on_startup=None, on_shutdown=None, lifespan=None, hdrs=None, before=None,
                 secret_key=None, session_cookie='session_', max_age=365*24*3600, sess_path='/',
                 same_site='lax', sess_https_only=False, sess_domain=None, key_fname='.sesskey', **bodykw):
        middleware,before = _list(middleware),_list(before)
        secret_key = get_key(secret_key, key_fname)
        sess = Middleware(SessionMiddleware, secret_key=secret_key, session_cookie=session_cookie,
                          max_age=max_age, path=sess_path, same_site=same_site,
                          https_only=sess_https_only, domain=sess_domain)
        middleware.append(sess)
        super().__init__(debug, routes, middleware, exception_handlers, on_startup, on_shutdown, lifespan=lifespan)
        hdrs = list([] if hdrs is None else hdrs) + [htmxscr]
        self.router = RouterX(routes, on_startup=on_startup, on_shutdown=on_shutdown, lifespan=lifespan, hdrs=hdrs,
                              before=before, **bodykw)

    def route(self, path:str, methods=None, name=None, include_in_schema=True):
        def f(func):
            m = [methods] if isinstance(methods,str) else [func.__name__] if not methods else methods
            self.router.add_route(path, func, methods=m, name=name, include_in_schema=include_in_schema)
            return func
        return f

all_meths = 'get post put delete patch head trace options'.split()
for o in all_meths: setattr(FastHTML, o, partialmethod(FastHTML.route, methods=o))

def reg_re_param(m, s):
    cls = get_class(f'{m}Conv', sup=StringConvertor, regex=s)
    register_url_convertor(m, cls())

# Starlette doesn't have the '?', so it chomps the whole remaining URL
reg_re_param("path", ".*?")
reg_re_param("static", "ico|gif|jpg|jpeg|webm|css|js|woff|png|svg|mp4|webp|ttf|otf|eot|woff2|txt|xml")

class MiddlewareBase:
    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] not in ["http", "websocket"]:
            await self.app(scope, receive, send)
            return
        return HTTPConnection(scope)
