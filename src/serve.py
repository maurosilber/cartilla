"""Serve dist/ and reload the browser when src/index.html changes.

Usage: python3 serve.py
"""

from livereload import Server

from build import build

server = Server()
server.watch("src/index.html", build)
server.serve(root="dist", port=8000)
