"use strict";
const fs = require("node:fs");
const path = require("node:path");
const root = path.resolve(__dirname, "..");
const jobs = [
  ["src/event_logger/web/frontend/parts", "src/event_logger/web/static/app.js"],
];
for (const [src, target] of jobs) {
  const dir = path.join(root, src);
  const parts = fs.readdirSync(dir).filter((x) => x.endsWith(".jsfrag")).sort();
  if (parts.length < 2) throw new Error("frontend parts missing");
  const output = parts.map((p) => fs.readFileSync(path.join(dir, p), "utf8")).join("");
  const targetPath = path.join(root, target);
  if (process.argv.includes("--check")) {
    if (output !== fs.readFileSync(targetPath, "utf8")) throw new Error("frontend bundle out of date: " + target);
  } else {
    fs.writeFileSync(targetPath, output);
  }
  process.stdout.write("verified or built: " + target + "\n");
}
