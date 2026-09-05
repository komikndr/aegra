const assert = require("node:assert/strict");
const test = require("node:test");
const { MAX_BODY_BYTES, createServer } = require("../server");

async function withServer(run) {
  const server = createServer();
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  const address = server.address();
  try {
    await run(`http://127.0.0.1:${address.port}`);
  } finally {
    await new Promise((resolve, reject) => server.close((error) => (error ? reject(error) : resolve())));
  }
}

function readPngDimension(image, offset) {
  return image.readUInt32BE(offset);
}

test("health endpoint and render endpoint return a PNG", async () => {
  await withServer(async (baseUrl) => {
    const health = await fetch(`${baseUrl}/health`);
    assert.equal(health.status, 200);
    assert.deepEqual(await health.json(), { status: "healthy" });

    const response = await fetch(`${baseUrl}/render`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ option: { xAxis: { type: "category", data: ["A"] }, yAxis: {}, series: [{ type: "bar", data: [1] }] } }),
    });
    const image = Buffer.from(await response.arrayBuffer());
    assert.equal(response.status, 200);
    assert.equal(response.headers.get("content-type"), "image/png");
    assert.deepEqual(image.subarray(0, 8), Buffer.from([137, 80, 78, 71, 13, 10, 26, 10]));
    assert.equal(readPngDimension(image, 16), 960);
    assert.equal(readPngDimension(image, 20), 540);
  });
});

test("renderer rejects invalid options, external image references, and oversized bodies", async () => {
  await withServer(async (baseUrl) => {
    const invalid = await fetch(`${baseUrl}/render`, { method: "POST", body: JSON.stringify({ option: [] }) });
    assert.equal(invalid.status, 422);

    const externalImage = await fetch(`${baseUrl}/render`, {
      method: "POST",
      body: JSON.stringify({ option: { graphic: { style: { image: "https://example.test/chart.png" } } } }),
    });
    assert.equal(externalImage.status, 422);

    const oversized = await fetch(`${baseUrl}/render`, {
      method: "POST",
      body: "x".repeat(MAX_BODY_BYTES + 1),
    });
    assert.equal(oversized.status, 413);
  });
});
