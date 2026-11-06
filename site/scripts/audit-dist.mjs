// Fails when a built page loads anything from outside /agentprint/ or has no <title>.
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join, relative } from "node:path";

const dist = new URL("../dist", import.meta.url).pathname;
const BASE = "/agentprint/";

function* htmlFiles(dir) {
  for (const name of readdirSync(dir)) {
    const p = join(dir, name);
    if (statSync(p).isDirectory()) yield* htmlFiles(p);
    else if (name.endsWith(".html")) yield p;
  }
}

const isExternal = (u) => /^(?:[a-z][a-z0-9+.-]*:)?\/\//i.test(u);
const attr = (tag, name) => tag.match(new RegExp(`\\s${name}\\s*=\\s*(?:"([^"]*)"|'([^']*)')`, "i"));
const LINK_RELS = /stylesheet|preload|icon|modulepreload|prefetch|preconnect|dns-prefetch/i;

const violations = [];
let pages = 0;
for (const file of htmlFiles(dist)) {
  pages += 1;
  const html = readFileSync(file, "utf8");
  const rel = relative(dist, file);
  const check = (kind, url) => {
    if (isExternal(url) || (url.startsWith("/") && !url.startsWith(BASE))) {
      violations.push(`${rel}: ${kind} points outside ${BASE}: ${url}`);
    }
  };
  for (const [tag] of html.matchAll(/<script\b[^>]*>/gi)) {
    const m = attr(tag, "src");
    if (m) check("<script src>", m[1] ?? m[2]);
  }
  for (const [tag] of html.matchAll(/<link\b[^>]*>/gi)) {
    const rl = attr(tag, "rel");
    const m = attr(tag, "href");
    if (m && rl && LINK_RELS.test(rl[1] ?? rl[2])) check("<link href>", m[1] ?? m[2]);
  }
  for (const [tag] of html.matchAll(/<img\b[^>]*>/gi)) {
    const m = attr(tag, "src");
    if (m && !m[1]?.startsWith("data:")) check("<img src>", m[1] ?? m[2]);
  }
  const title = html.match(/<title[^>]*>([\s\S]*?)<\/title>/i);
  if (!title || !title[1].trim()) violations.push(`${rel}: missing or empty <title>`);
}

if (pages === 0) violations.push("no HTML pages found in dist/ (run the build first)");
if (violations.length) {
  console.error(violations.join("\n"));
  process.exit(1);
}
console.log(`audit-dist: ${pages} pages, no violations`);
