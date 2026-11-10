"""Tıklama takibi yönlendirme sunucusu: /r/<link>/<bot>/<user>/<imza> → hedef link."""
import hmac
import logging

from aiohttp import web

from .config import WEB_PORT
from .content import track_sig
from .db import Click, Link, Session

log = logging.getLogger(__name__)


async def redirect(request: web.Request):
    try:
        lid, bid, uid = (int(request.match_info[k]) for k in ("lid", "bid", "uid"))
    except ValueError:
        raise web.HTTPNotFound()
    async with Session() as s:
        link = await s.get(Link, lid)
        if not link:
            raise web.HTTPNotFound()
        if hmac.compare_digest(request.match_info["sig"], track_sig(lid, bid, uid)):
            s.add(Click(link_id=lid, bot_id=bid, user_id=uid))
            await s.commit()
    raise web.HTTPFound(link.url)


async def health(_):
    return web.Response(text="ok")


async def start_web() -> web.AppRunner:
    app = web.Application()
    app.router.add_get("/r/{lid}/{bid}/{uid}/{sig}", redirect)
    app.router.add_get("/health", health)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", WEB_PORT).start()
    log.info("Takip sunucusu port %s üzerinde", WEB_PORT)
    return runner
