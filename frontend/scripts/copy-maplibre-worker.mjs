// Copy MapLibre's Web Worker into public/ so the browser can load it.
//
// MapLibre parses map tiles in a Web Worker that lives in a separate file.
// It finds that file at runtime relative to its own module URL, which
// bundlers (Turbopack, webpack) can't follow, so the worker is never emitted
// and the map stays blank. Serving our own copy and calling
// maplibregl.setWorkerUrl("/vendor/maplibre-gl-worker.mjs") fixes that.
// Running this before every dev/build keeps the copy in step with the
// installed version. public/vendor/ is gitignored.
import { copyFileSync, existsSync, mkdirSync, readFileSync, rmSync } from "node:fs";
import { dirname } from "node:path";
import { fileURLToPath } from "node:url";

const source = fileURLToPath(import.meta.resolve("maplibre-gl/dist/maplibre-gl-worker.mjs"));
const target = fileURLToPath(new URL("../public/vendor/maplibre-gl-worker.mjs", import.meta.url));

// Nothing to do if the copy is already current.
if (existsSync(target) && readFileSync(target).equals(readFileSync(source))) process.exit(0);

mkdirSync(dirname(target), { recursive: true });
// Remove before copying: in Docker the dev server runs as root and the folder
// is bind-mounted, so an old copy may be owned by root. Deleting it only
// needs write access to the folder; overwriting it would need ownership.
rmSync(target, { force: true });
copyFileSync(source, target);
