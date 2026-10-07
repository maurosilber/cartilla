"""Serve dist/ and reload the browser when src/index.html changes.

Usage: python3 serve.py
"""

import shutil

from livereload import Server


def copy_index():
    shutil.copy("src/index.html", "dist/index.html")


server = Server()
server.watch("src/index.html", copy_index)
server.serve(root="dist", port=8000)
