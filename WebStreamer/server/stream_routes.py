#!/usr/bin/env python3
import time
import math
import logging
import mimetypes
import traceback
import subprocess
from aiohttp import web
from aiohttp.http_exceptions import BadStatusLine
from WebStreamer.bot import multi_clients, work_loads, StreamBot
from WebStreamer.vars import Var
from WebStreamer.server.exceptions import FIleNotFound, InvalidHash
from WebStreamer import utils, StartTime, __version__
from WebStreamer.utils.render_template import render_page
import static_ffmpeg  # Pastikan modul ini tersedia

routes = web.RouteTableDef()

@routes.get("/status", allow_head=True)
async def root_route_handler(_):
    return web.json_response(
        {
            "server_status": "running",
            "uptime": utils.get_readable_time(time.time() - StartTime),
            "telegram_bot": "@" + StreamBot.username,
            "connected_bots": len(multi_clients),
            "loads": dict(
                ("bot" + str(c + 1), l)
                for c, (_, l) in enumerate(
                    sorted(work_loads.items(), key=lambda x: x[1], reverse=True)
                )
            ),
            "version": __version__,
        }
    )

@routes.get("/watch/{path}", allow_head=True)
async def stream_handler(request: web.Request):
    try:
        path = request.match_info["path"]
        return web.Response(text=await render_page(path), content_type='text/html')
    except InvalidHash as e:
        raise web.HTTPForbidden(text=e.message)
    except FIleNotFound as e:
        raise web.HTTPNotFound(text=e.message)
    except (AttributeError, BadStatusLine, ConnectionResetError):
        pass
    except Exception as e:
        logging.critical(e.with_traceback(None))
        logging.debug(traceback.format_exc())
        raise web.HTTPInternalServerError(text=str(e))

@routes.get("/dl/{path}", allow_head=True)
async def download_handler(request: web.Request):
    try:
        path = request.match_info["path"]
        return await media_streamer(request, path)
    except InvalidHash as e:
        raise web.HTTPForbidden(text=e.message)
    except FIleNotFound as e:
        raise web.HTTPNotFound(text=e.message)
    except (AttributeError, BadStatusLine, ConnectionResetError):
        pass
    except Exception as e:
        logging.critical(e.with_traceback(None))
        logging.debug(traceback.format_exc())
        raise web.HTTPInternalServerError(text=str(e))

class_cache = {}

@routes.get("/video/{path}", allow_head=True)
async def video_stream_handler(request: web.Request):
    try:
        path = request.match_info["path"]
        return await media_streamer(request, path, is_video=True)
    except InvalidHash as e:
        raise web.HTTPForbidden(text=e.message)
    except FIleNotFound as e:
        raise web.HTTPNotFound(text=e.message)
    except (AttributeError, BadStatusLine, ConnectionResetError):
        pass
    except Exception as e:
        logging.critical(e.with_traceback(None))
        logging.debug(traceback.format_exc())
        raise web.HTTPInternalServerError(text=str(e))

@routes.get("/hls/{path}", allow_head=True)
async def hls_handler(request: web.Request):
    path = request.match_info["path"]
    video_url = f"https://example.com/video{path}"  # Sesuaikan URL sumber video

    cmd = [
        "static_ffmpeg", "-i", video_url, "-c:v", "copy", "-c:a", "aac",
        "-f", "hls", "-hls_time", "4", "-hls_list_size", "0", "pipe:1"
    ]

    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return web.Response(body=proc.stdout, content_type="application/vnd.apple.mpegurl")

# Route baru untuk halaman player yang menggunakan custom PlayerJs
@routes.get("/player/{path}", allow_head=True)
async def player_handler(request: web.Request):
    path = request.match_info["path"]
    # Halaman HTML yang memuat custom PlayerJs dan mengarahkan file video ke route /video/{path}
    html = f'''<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Custom Video Player</title>
  <script src="https://cdn.jsdelivr.net/gh/Hanifalm/PlayerJs-Custom@refs/heads/main/playerjs%20(1).js"></script>
</head>
<body style="margin:0; background: #000;">
  <div id="player"></div>
  <script>
    new Playerjs({{
      id: "player",
      file: "/video/{path}"
    }});
  </script>
</body>
</html>'''
    return web.Response(text=html, content_type="text/html")

async def media_streamer(request: web.Request, db_id: str, is_video: bool = False):
    range_header = request.headers.get("Range", 0)

    index = min(work_loads, key=work_loads.get)
    faster_client = multi_clients[index]

    if Var.MULTI_CLIENT:
        logging.info(f"Client {index} sedang serving {request.headers.get('X-FORWARDED-FOR', request.remote)}")

    if faster_client in class_cache:
        tg_connect = class_cache[faster_client]
        logging.debug(f"Menggunakan objek ByteStreamer yang sudah di-cache untuk client {index}")
    else:
        logging.debug(f"Membuat objek ByteStreamer baru untuk client {index}")
        tg_connect = utils.ByteStreamer(faster_client)
        class_cache[faster_client] = tg_connect

    file_id = await tg_connect.get_file_properties(db_id, multi_clients)
    file_size = file_id.file_size

    if range_header:
        from_bytes, until_bytes = range_header.replace("bytes=", "").split("-")
        from_bytes = int(from_bytes)
        until_bytes = int(until_bytes) if until_bytes else file_size - 1
    else:
        from_bytes = request.http_range.start or 0
        until_bytes = (request.http_range.stop or file_size) - 1

    if (until_bytes > file_size) or (from_bytes < 0) or (until_bytes < from_bytes):
        return web.Response(
            status=416,
            body="416: Range not satisfiable",
            headers={"Content-Range": f"bytes */{file_size}"},
        )

    chunk_size = 1024 * 1024
    until_bytes = min(until_bytes, file_size - 1)

    offset = from_bytes - (from_bytes % chunk_size)
    first_part_cut = from_bytes - offset
    last_part_cut = until_bytes % chunk_size + 1

    req_length = until_bytes - from_bytes + 1
    part_count = math.ceil(until_bytes / chunk_size) - math.floor(offset / chunk_size)
    body = tg_connect.yield_file(
        file_id, index, offset, first_part_cut, last_part_cut, part_count, chunk_size
    )

    mime_type = file_id.mime_type or mimetypes.guess_type(utils.get_name(file_id))[0] or "application/octet-stream"

    disposition = "inline" if is_video else "attachment"

    headers = {
        "Content-Type": mime_type,
        "Content-Range": f"bytes {from_bytes}-{until_bytes}/{file_size}",
        "Content-Length": str(req_length),
        "Content-Disposition": f'{disposition}; filename="{utils.get_name(file_id)}"',
        "Accept-Ranges": "bytes",
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "GET, HEAD, OPTIONS",
    }

    if is_video:
        headers["X-Frame-Options"] = "ALLOWALL"
        # Jika ingin menggunakan CSP, bisa menambahkan:
        # headers["Content-Security-Policy"] = "frame-ancestors *;"

    return web.Response(
        status=206 if range_header else 200,
        body=body,
        headers=headers,
    )

def init_app():
    app = web.Application()
    app.add_routes(routes)
    return app

if __name__ == "__main__":
    app = init_app()
    web.run_app(app)
