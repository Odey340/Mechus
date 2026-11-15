import { cp, mkdir, rm, writeFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import path from "node:path";

const repoRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const sourceRoot = path.join(repoRoot, "koov", "static");
const outputRoot = path.resolve(
  process.env.MECHUS_VERCEL_OUTPUT_DIR || path.join(repoRoot, "public"),
);

const configuredOrigin = String(process.env.MECHUS_API_ORIGIN || "").trim();
let apiOrigin = "";
if (configuredOrigin) {
  const parsed = new URL(configuredOrigin);
  if (!/^https?:$/.test(parsed.protocol)) {
    throw new Error("MECHUS_API_ORIGIN must use http:// or https://");
  }
  parsed.pathname = parsed.pathname.replace(/\/$/, "");
  apiOrigin = parsed.toString().replace(/\/$/, "");
}

await rm(outputRoot, { recursive: true, force: true });
await mkdir(path.join(outputRoot, "static"), { recursive: true });

for (const directory of ["css", "js", "vendor", "img"]) {
  await cp(
    path.join(sourceRoot, directory),
    path.join(outputRoot, "static", directory),
    { recursive: true },
  );
}

await Promise.all([
  cp(path.join(sourceRoot, "properties.html"), path.join(outputRoot, "dashboard.html")),
  cp(path.join(sourceRoot, "properties.html"), path.join(outputRoot, "properties.html")),
  cp(path.join(sourceRoot, "welcome.html"), path.join(outputRoot, "welcome.html")),
  cp(path.join(sourceRoot, "report.html"), path.join(outputRoot, "report.html")),
]);

const runtimeConfig =
  `window.MECHUS_RUNTIME = Object.freeze(${JSON.stringify({ apiOrigin })});\n`;
await writeFile(
  path.join(outputRoot, "static", "js", "runtime-config.js"),
  runtimeConfig,
  "utf8",
);

await writeFile(
  path.join(outputRoot, "meetings.html"),
  `<!doctype html><meta charset="utf-8"><script>location.replace("/dashboard" + location.search + "#/meetings")</script>`,
  "utf8",
);

await writeFile(
  path.join(outputRoot, "health.json"),
  `${JSON.stringify({ status: "ok", service: "mechus-vercel-ui" })}\n`,
  "utf8",
);

console.log(
  `Built Mechus Vercel assets in ${outputRoot} (${apiOrigin ? `backend: ${apiOrigin}` : "same-origin API bridge"}).`,
);
