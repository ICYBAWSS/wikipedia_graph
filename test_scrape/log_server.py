
import http.server
import socketserver
import os
import re
import json

PORT = 8000

class RangeRequestHandler(http.server.SimpleHTTPRequestHandler):
    def do_POST(self):
        if self.path == '/log':
            content_length = int(self.headers['Content-Length'])
            post_data = self.rfile.read(content_length)
            try:
                log_data = json.loads(post_data)
                level = log_data.get('level', 'INFO')
                message = log_data.get('message', '')
                print(f"[BROWSER {level}] {message}")
                self.send_response(200)
                self.end_headers()
            except Exception as e:
                print(f"Error parsing log: {e}")
                self.send_error(400)
        else:
            self.send_error(404)

    def translate_path(self, path):
        if path.startswith('/test_scrape/'):
            path = path[len('/test_scrape/'):]
        path = path.lstrip('/')
        if not path:
            path = 'index.html'
        return os.path.abspath(os.path.join(os.getcwd(), path))

    def end_headers(self):
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Range, Content-Type')
        super().end_headers()

    def do_OPTIONS(self):
        self.send_response(200)
        self.end_headers()

    def guess_type(self, path):
        if path.endswith(".wasm"):
            return "application/wasm"
        if path.endswith(".js"):
            return "application/javascript"
        return super().guess_type(path)

    def send_head(self):
        path = self.translate_path(self.path)
        if os.path.isdir(path):
            if not self.path.endswith('/'):
                self.send_response(http.HTTPStatus.MOVED_PERMANENTLY)
                self.send_header("Location", self.path + '/')
                self.end_headers()
                return None
            for index in "index.html", "index.htm":
                index = os.path.join(path, index)
                if os.path.exists(index):
                    path = index
                    break
            else:
                return self.list_directory(path)
        
        if not os.path.exists(path):
            self.send_error(http.HTTPStatus.NOT_FOUND, f"File not found: {path}")
            return None

        ctype = self.guess_type(path)
        f = None
        try:
            f = open(path, 'rb')
        except OSError:
            self.send_error(http.HTTPStatus.NOT_FOUND, "File not found")
            return None

        file_size = os.path.getsize(path)
        if "Range" in self.headers:
            range_header = self.headers.get("Range")
            match = re.match(r'bytes=(\d+)-(\d*)', range_header)
            if match:
                start = int(match.group(1))
                end = match.group(2)
                if end:
                    end = int(end)
                else:
                    end = file_size - 1
                end = min(end, file_size - 1)
                if start > end:
                    self.send_error(http.HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                    f.close()
                    return None
                self.send_response(http.HTTPStatus.PARTIAL_CONTENT)
                self.send_header("Content-Range", f"bytes {start}-{end}/{file_size}")
                self.send_header("Content-Length", str(end - start + 1))
                self.send_header("Last-Modified", self.date_time_string(os.stat(path).st_mtime))
                self.send_header("Content-type", ctype)
                self.send_header("Accept-Ranges", "bytes")
                self.end_headers()
                f.seek(start)
                return f

        self.send_response(http.HTTPStatus.OK)
        self.send_header("Content-type", ctype)
        self.send_header("Content-Length", str(file_size))
        self.send_header("Last-Modified", self.date_time_string(os.stat(path).st_mtime))
        self.send_header("Accept-Ranges", "bytes")
        self.end_headers()
        return f

if __name__ == "__main__":
    handler = RangeRequestHandler
    socketserver.TCPServer.allow_reuse_address = True
    with socketserver.TCPServer(("", PORT), handler) as httpd:
        print(f"Serving at port {PORT}")
        httpd.serve_forever()
