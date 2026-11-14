"""Serve build/ as static files and accept POST /api/export to save CSV/GeoJSON into exports/.
Usage: python scripts/serve.py [port]   (binds 127.0.0.1)"""
import http.server, json, pathlib, re, sys, functools, datetime as dt
ROOT = pathlib.Path(__file__).resolve().parents[1]; EXP = ROOT/'exports'; EXP.mkdir(exist_ok=True)
class H(http.server.SimpleHTTPRequestHandler):
    def do_POST(self):
        if self.path != '/api/export': return self.send_error(404)
        n = int(self.headers.get('Content-Length', 0))
        if n > 200_000_000: return self.send_error(413)
        body = json.loads(self.rfile.read(n))
        name = re.sub(r'[^A-Za-z0-9_.-]', '_', body['filename'])[:150]
        if not name.endswith(('.csv', '.geojson')): return self.send_error(400)
        (EXP/name).write_text(body['content'])
        out = json.dumps({'saved': f'exports/{name}', 'bytes': len(body['content'])}).encode()
        self.send_response(200); self.send_header('Content-Type', 'application/json'); self.send_header('Content-Length', str(len(out))); self.end_headers(); self.wfile.write(out)
port = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
http.server.ThreadingHTTPServer(('127.0.0.1', port), functools.partial(H, directory=str(ROOT/'build'))).serve_forever()
