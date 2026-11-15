const HOP_BY_HOP = new Set([
  "connection",
  "content-length",
  "content-encoding",
  "host",
  "keep-alive",
  "proxy-authenticate",
  "proxy-authorization",
  "te",
  "trailer",
  "transfer-encoding",
  "upgrade",
]);

function backendOrigin() {
  const raw = String(process.env.MECHUS_API_ORIGIN || "").trim().replace(/\/$/, "");
  if (!raw) return "";
  const parsed = new URL(raw);
  if (!/^https?:$/.test(parsed.protocol)) throw new Error("MECHUS_API_ORIGIN must use http(s)");
  return parsed.origin + parsed.pathname.replace(/\/$/, "");
}

function forwardedPath(request) {
  const value = request.query && request.query.path;
  const raw = Array.isArray(value) ? value.join("/") : String(value || "");
  const path = raw.startsWith("/") ? raw : `/${raw}`;
  if (!/^\/(?:api|callback|ready|share)(?:\/|$)/.test(path)) {
    throw new Error("Unsupported backend path");
  }
  return path;
}

function appendQuery(target, query) {
  for (const [key, value] of Object.entries(query || {})) {
    if (key === "path" || value == null) continue;
    for (const item of Array.isArray(value) ? value : [value]) {
      target.searchParams.append(key, String(item));
    }
  }
}

async function requestBody(request) {
  if (request.method === "GET" || request.method === "HEAD") return undefined;
  if (Buffer.isBuffer(request.body)) return request.body;
  if (typeof request.body === "string") return Buffer.from(request.body);
  if (request.body && typeof request.body === "object") {
    return Buffer.from(JSON.stringify(request.body));
  }
  const chunks = [];
  for await (const chunk of request) chunks.push(Buffer.from(chunk));
  return chunks.length ? Buffer.concat(chunks) : undefined;
}

module.exports = async function proxy(request, response) {
  let origin;
  let path;
  try {
    origin = backendOrigin();
    path = forwardedPath(request);
  } catch (error) {
    return response.status(400).json({ success: false, error: error.message });
  }

  if (!origin) {
    return response.status(503).json({
      success: false,
      code: "backend_not_configured",
      error: "Set MECHUS_API_ORIGIN in Vercel to connect the live Mechus backend.",
      demo: "/dashboard?demo=1",
    });
  }

  const target = new URL(`${origin}${path}`);
  appendQuery(target, request.query);
  const headers = {};
  for (const [key, value] of Object.entries(request.headers || {})) {
    if (!HOP_BY_HOP.has(key.toLowerCase()) && value != null) headers[key] = value;
  }
  headers["x-forwarded-host"] = request.headers.host || "";
  headers["x-mechus-vercel-bridge"] = "1";

  try {
    const upstream = await fetch(target, {
      method: request.method,
      headers,
      body: await requestBody(request),
      redirect: "manual",
    });
    response.statusCode = upstream.status;
    for (const [key, value] of upstream.headers.entries()) {
      if (!HOP_BY_HOP.has(key.toLowerCase())) response.setHeader(key, value);
    }
    response.setHeader("cache-control", upstream.headers.get("cache-control") || "no-store");
    return response.end(Buffer.from(await upstream.arrayBuffer()));
  } catch (error) {
    return response.status(502).json({
      success: false,
      code: "backend_unreachable",
      error: "The Mechus backend could not be reached.",
      detail: error.message,
    });
  }
};
