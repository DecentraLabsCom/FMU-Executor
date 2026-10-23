"""Serve the local Three.js scene exporter and save its generated GLB."""

from __future__ import annotations

import argparse
import json
import os
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


EXAMPLE_ROOT = Path(__file__).resolve().parents[1]
OUTPUT_PATH = EXAMPLE_ROOT / "smart-warehouse-cell.glb"
MAX_UPLOAD_BYTES = 50 * 1024 * 1024


class Handler(SimpleHTTPRequestHandler):
    def do_POST(self) -> None:
        if self.path != "/__export":
            self.send_error(404)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self.send_error(400, "Invalid content length")
            return
        if length < 20 or length > MAX_UPLOAD_BYTES:
            self.send_error(413, "GLB size is invalid")
            return

        payload = self.rfile.read(length)
        if len(payload) != length or payload[:4] != b"glTF":
            self.send_error(400, "The submitted file is not a complete GLB")
            return

        OUTPUT_PATH.write_bytes(payload)
        binding_count = payload.count(b"fmiBindings")
        body = json.dumps({
            "file": OUTPUT_PATH.name,
            "bytes": len(payload),
            "bindings": binding_count,
        }).encode("utf-8")
        self.send_response(201)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            # A headless browser may close after saving the GLB, before reading
            # the small JSON acknowledgement.
            pass


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--three-root", type=Path, required=True, help="Installed Marketplace Three.js package directory")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()

    three_root = args.three_root.resolve(strict=True)
    exporter_source = three_root / "examples" / "jsm" / "exporters" / "GLTFExporter.js"
    loader_source = three_root / "examples" / "jsm" / "loaders" / "GLTFLoader.js"
    utils_source = three_root / "examples" / "jsm" / "utils"
    if not exporter_source.is_file():
        raise FileNotFoundError(f"Three.js GLTFExporter is missing: {exporter_source}")
    if not loader_source.is_file():
        raise FileNotFoundError(f"Three.js GLTFLoader is missing: {loader_source}")
    vendor = EXAMPLE_ROOT / "webapp" / "vendor"
    vendor.mkdir(parents=True, exist_ok=True)
    exporter = exporter_source.read_text(encoding="utf-8")
    exporter = exporter.replace("from 'three'", "from './three.module.js'")
    exporter = exporter.replace('from "three"', 'from "./three.module.js"')
    (vendor / "GLTFExporter.js").write_text(exporter, encoding="utf-8", newline="\n")
    loader = loader_source.read_text(encoding="utf-8")
    loader = loader.replace("from 'three'", "from './three.module.js'")
    loader = loader.replace('from "three"', 'from "./three.module.js"')
    loader = loader.replace("../utils/", "./utils/")
    (vendor / "GLTFLoader.js").write_text(loader, encoding="utf-8", newline="\n")
    utilities = vendor / "utils"
    utilities.mkdir(parents=True, exist_ok=True)
    for utility_name in ("BufferGeometryUtils.js", "SkeletonUtils.js"):
        utility = (utils_source / utility_name).read_text(encoding="utf-8")
        utility = utility.replace("from 'three'", "from '../three.module.js'")
        utility = utility.replace('from "three"', 'from "../three.module.js"')
        (utilities / utility_name).write_text(utility, encoding="utf-8", newline="\n")

    os.chdir(EXAMPLE_ROOT)
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"Open http://127.0.0.1:{args.port}/tools/export-scene.html")
    print(f"The generated GLB will be saved to {OUTPUT_PATH}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
