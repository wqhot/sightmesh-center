from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from urllib.parse import urlsplit

class CesiumHandler(SimpleHTTPRequestHandler):
    def guess_type(self, path):
        if urlsplit(path).path.endswith(".terrain"):
            return "application/vnd.quantized-mesh"
        return super().guess_type(path)

    def end_headers(self):
        if urlsplit(self.path).path.endswith(".terrain"):
            self.send_header("Content-Encoding", "gzip")
        self.send_header("Access-Control-Allow-Origin", "*")
        super().end_headers()

ThreadingHTTPServer(("0.0.0.0", 8000), CesiumHandler).serve_forever()
