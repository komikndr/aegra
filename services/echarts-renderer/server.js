const http = require("node:http");
const { createCanvas } = require("@napi-rs/canvas");
const echarts = require("echarts");

const PORT = Number(process.env.PORT || 8787);
const MAX_BODY_BYTES = 256 * 1024;
const MAX_OPTION_DEPTH = 20;
const MAX_OPTION_ITEMS = 10_000;
const WIDTH = 960;
const HEIGHT = 540;

echarts.setPlatformAPI({ createCanvas: () => createCanvas(1, 1) });

function isExternalImageReference(value, key) {
  const normalized = value.trim().toLowerCase();
  if (normalized.startsWith("image://")) return true;
  if (!/(image|background)/i.test(key)) return false;
  return /^(https?:|\/\/|data:|file:|ftp:|image:|url\()/i.test(normalized);
}

function validateOption(value, depth = 0, seen = { count: 0 }, key = "") {
  if (depth > MAX_OPTION_DEPTH) throw new Error("Chart option is too deeply nested");
  seen.count += 1;
  if (seen.count > MAX_OPTION_ITEMS) throw new Error("Chart option has too many values");
  if (typeof value === "number" && !Number.isFinite(value)) throw new Error("Chart option contains a non-finite number");
  if (typeof value === "string") {
    if (isExternalImageReference(value, key)) {
      throw new Error("Chart option cannot reference external images");
    }
    return;
  }
  if (Array.isArray(value)) {
    value.forEach((item) => validateOption(item, depth + 1, seen, key));
  } else if (value && typeof value === "object") {
    Object.entries(value).forEach(([entryKey, item]) => validateOption(item, depth + 1, seen, entryKey));
  }
}

function render(option) {
  if (!option || Array.isArray(option) || typeof option !== "object") throw new Error("Chart option must be an object");
  validateOption(option);
  const canvas = createCanvas(WIDTH, HEIGHT);
  const chart = echarts.init(canvas, null, { renderer: "canvas", ssr: true, width: WIDTH, height: HEIGHT });
  try {
    chart.setOption({ ...option, animation: false }, { notMerge: true, lazyUpdate: false });
    return canvas.toBuffer("image/png");
  } finally {
    chart.dispose();
  }
}

function respond(response, status, body, contentType = "application/json") {
  response.writeHead(status, { "Content-Type": contentType, "Cache-Control": "no-store" });
  response.end(body);
}

function createServer() {
  return http.createServer((request, response) => {
    if (request.method === "GET" && request.url === "/health") return respond(response, 200, JSON.stringify({ status: "healthy" }));
    if (request.method !== "POST" || request.url !== "/render") return respond(response, 404, JSON.stringify({ error: "not found" }));
    let bytes = 0;
    let tooLarge = false;
    const chunks = [];
    request.on("data", (chunk) => {
      bytes += chunk.length;
      if (bytes > MAX_BODY_BYTES) {
        tooLarge = true;
        return;
      }
      chunks.push(chunk);
    });
    request.on("end", () => {
      if (tooLarge) return respond(response, 413, JSON.stringify({ error: "request body is too large" }));
      try {
        const payload = JSON.parse(Buffer.concat(chunks).toString("utf8"));
        const image = render(payload.option);
        return respond(response, 200, image, "image/png");
      } catch (error) {
        return respond(response, 422, JSON.stringify({ error: "invalid chart option" }));
      }
    });
    request.on("error", () => {
      if (!response.writableEnded) respond(response, 400, JSON.stringify({ error: "invalid request" }));
    });
  });
}

if (require.main === module) {
  createServer().listen(PORT, "0.0.0.0", () => console.log(`ECharts renderer listening on ${PORT}`));
}

module.exports = { MAX_BODY_BYTES, createServer, render, validateOption };
