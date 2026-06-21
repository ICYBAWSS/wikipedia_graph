import http.server
import socketserver
import json
import webbrowser
import os
import sys
import re

PORT = 8000
LOG_FILE = "console_logs.txt"
COORD_LOG_FILE = "coordinates_log.txt"

# Clear/Initialize the log files
with open(LOG_FILE, "w", encoding="utf-8") as f:
    f.write("=== BROWSER CONSOLE LOGS ===\n")

with open(COORD_LOG_FILE, "w", encoding="utf-8") as f:
    f.write("=== NODE COORDINATES LOG ===\n")

class LogRequestHandler(http.server.SimpleHTTPRequestHandler):
    def send_head(self):
        # Check for Range header
        range_header = self.headers.get('Range')
        if not range_header or not range_header.startswith('bytes='):
            return super().send_head()

        path = self.translate_path(self.path)
        if os.path.isdir(path):
            return super().send_head()
        
        try:
            file = open(path, 'rb')
            file_size = os.fstat(file.fileno()).st_size
            
            # Parse 'bytes=start-end'
            range_match = re.match(r'bytes=(\d*)-(\d*)', range_header)
            if not range_match:
                file.close()
                return super().send_head()
                
            start_str, end_str = range_match.groups()
            
            if start_str:
                start = int(start_str)
                if end_str:
                    end = int(end_str)
                else:
                    end = file_size - 1
            else:
                end = file_size - 1
                start = file_size - int(end_str)
                
            # Clamp ranges
            start = max(0, min(start, file_size - 1))
            end = max(0, min(end, file_size - 1))
            
            if start > end:
                self.send_error(416, "Requested Range Not Satisfiable")
                file.close()
                return None
                
            self.send_response(206)
            self.send_header('Content-type', self.guess_type(path))
            self.send_header('Content-Range', f'bytes {start}-{end}/{file_size}')
            self.send_header('Content-Length', str(end - start + 1))
            self.send_header('Accept-Ranges', 'bytes')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.send_header('Access-Control-Allow-Methods', 'POST, GET, OPTIONS')
            self.send_header('Access-Control-Allow-Headers', 'Content-Type, Range')
            self.end_headers()
            
            if self.command == 'GET':
                file.seek(start)
                bytes_to_send = end - start + 1
                chunk_size = 64 * 1024
                while bytes_to_send > 0:
                    read_size = min(bytes_to_send, chunk_size)
                    data = file.read(read_size)
                    if not data:
                        break
                    self.wfile.write(data)
                    bytes_to_send -= len(data)
                    
            file.close()
            return None
        except Exception as e:
            print(f"Error serving range request: {e}")
            return super().send_head()

    def end_headers(self):
        self.send_header('Accept-Ranges', 'bytes')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'POST, GET, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type, Range')
        self.send_header('Access-Control-Expose-Headers', 'Accept-Ranges, Content-Range, Content-Length')
        super().end_headers()

    def do_OPTIONS(self):
        self.send_response(200)
        self.end_headers()

    def do_POST(self):
        if self.path == '/log':
            content_length = int(self.headers['Content-Length'])
            post_data = self.rfile.read(content_length)
            try:
                log_entry = json.loads(post_data.decode('utf-8'))
                log_type = log_entry.get('type', 'log').upper()
                message = log_entry.get('message', '')
                
                log_line = f"[{log_type}] {message}\n"
                with open(LOG_FILE, "a", encoding="utf-8") as f:
                    f.write(log_line)
                
                # Print to terminal
                sys.stdout.write(log_line)
                sys.stdout.flush()
                
                self.send_response(200)
                self.send_header('Content-type', 'application/json')
                self.end_headers()
                self.wfile.write(b'{"status":"ok"}')
            except Exception as e:
                self.send_response(400)
                self.end_headers()
                self.wfile.write(str(e).encode('utf-8'))
        elif self.path == '/save_coordinates':
            content_length = int(self.headers['Content-Length'])
            post_data = self.rfile.read(content_length)
            try:
                positions = json.loads(post_data.decode('utf-8'))
                import sqlite3
                conn = sqlite3.connect('results/wiki_graph.db')
                cursor = conn.cursor()
                cursor.executemany(
                    "UPDATE nodes SET x = ?, y = ? WHERE id = ?",
                    [(node['x'], node['y'], node['id']) for node in positions]
                )
                conn.commit()
                conn.close()
                print(f"Saved {len(positions)} node positions to database successfully.")
                self.send_response(200)
                self.send_header('Content-type', 'application/json')
                self.end_headers()
                self.wfile.write(b'{"status":"ok"}')
            except Exception as e:
                print(f"Error saving coordinates: {e}")
                self.send_response(500)
                self.end_headers()
                self.wfile.write(str(e).encode('utf-8'))
        elif self.path == '/log_coordinates':
            content_length = int(self.headers['Content-Length'])
            post_data = self.rfile.read(content_length)
            try:
                data = json.loads(post_data.decode('utf-8'))
                tick = data.get('tick', 0)
                nodes = data.get('nodes', [])
                
                log_lines = [f"--- Tick {tick} ---\n"]
                for node in nodes:
                    log_lines.append(f"  Node '{node['id']}': x={node['x']:.4f}, y={node['y']:.4f}\n")
                
                with open(COORD_LOG_FILE, "a", encoding="utf-8") as f:
                    f.writelines(log_lines)
                
                self.send_response(200)
                self.send_header('Content-type', 'application/json')
                self.end_headers()
                self.wfile.write(b'{"status":"ok"}')
            except Exception as e:
                print(f"Error logging coordinates: {e}")
                self.send_response(500)
                self.end_headers()
                self.wfile.write(str(e).encode('utf-8'))
        else:
            self.send_response(404)
            self.end_headers()

def main():
    # Set the directory to the project root
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    
    # Allow port reuse to avoid 'Address already in use' errors
    socketserver.TCPServer.allow_reuse_address = True
    
    with socketserver.TCPServer(("", PORT), LogRequestHandler) as httpd:
        print(f"Server started at http://localhost:{PORT}")
        print(f"Saving browser console logs to {os.path.abspath(LOG_FILE)}")
        print("Press Ctrl+C to stop the server.")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nServer stopped.")

if __name__ == "__main__":
    main()
