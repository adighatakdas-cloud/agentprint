// Fails when a built page loads anything from outside /agentprint/, links inside the site
// without the /agentprint/ base, has no <title>, or when the landing page lost its static
// terminal (a reader without JavaScript must still see the whole run, ending on BLOCK).
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join, relative } from "node:path";
import { fileURLToPath } from "node:url";

const dist = fileURLToPath(new URL("../dist", import.meta.url));
const BASE = "/agentprint/";
const ORIGIN = "https://adighatakdas-cloud.github.io";
const LANDING_TEXT = [
  "The security check your AI coding agent runs for you.",
  "BLOCK",
  "agentprint: BLOCK",
];

function* files(dir, suffix) {
  for (const name of readdirSync(dir)) {
    const p = join(dir, name);
    if (statSync(p).isDirectory()) yield* files(p, suffix);
    else if (name.endsWith(suffix)) yield p;
  }
}

const isExternal = (u) => /^(?:[a-z][a-z0-9+.-]*:)?\/\//i.test(u);
const isFirstParty = (u) =>
  u.startsWith("data:") ||
  u.startsWith(`${ORIGIN}${BASE}`) ||
  (!isExternal(u) && (!u.startsWith("/") || u.startsWith(BASE)));
const attr = (tag, name) => {
  const m = tag.match(new RegExp(`\\s${name}\\s*=\\s*(?:"([^"]*)"|'([^']*)')`, "i"));
  return m ? (m[1] ?? m[2]) : null;
};
// "a.png 1x, /b.png 2x" -> ["a.png", "/b.png"]
const srcsetUrls = (v) => v.split(",").map((c) => c.trim().split(/\s+/)[0]).filter(Boolean);
const cssUrls = (css) =>
  [...css.matchAll(/url\(\s*(?:"([^"]*)"|'([^']*)'|([^)'"\s]*))\s*\)/gi)].map((m) => m[1] ?? m[2] ?? m[3]);
const LINK_RELS = /stylesheet|preload|icon|modulepreload|prefetch|preconnect|dns-prefetch|manifest/i;

const violations = [];
const outside = (where, kind, url) => {
  if (!isFirstParty(url)) violations.push(`${where}: ${kind} points outside ${BASE}: ${url}`);
};

let pages = 0;
for (const file of files(dist, ".html")) {
  pages += 1;
  const html = readFileSync(file, "utf8");
  const rel = relative(dist, file);
  const check = (kind, url) => url != null && outside(rel, kind, url);
  for (const [tag] of html.matchAll(/<script\b[^>]*>/gi)) check("<script src>", attr(tag, "src"));
  for (const [tag] of html.matchAll(/<link\b[^>]*>/gi)) {
    const rl = attr(tag, "rel");
    if (rl && LINK_RELS.test(rl)) check("<link href>", attr(tag, "href"));
  }
  for (const [tag] of html.matchAll(/<(?:img|source|video|audio|iframe|embed)\b[^>]*>/gi)) {
    check("src", attr(tag, "src"));
    for (const u of srcsetUrls(attr(tag, "srcset") ?? "")) check("srcset", u);
  }
  for (const [tag] of html.matchAll(/<meta\b[^>]*>/gi)) {
    const key = attr(tag, "property") ?? attr(tag, "name") ?? "";
    if (/^(?:og|twitter):image/i.test(key)) check(`<meta ${key}>`, attr(tag, "content"));
  }
  for (const [, css] of html.matchAll(/<style\b[^>]*>([\s\S]*?)<\/style>/gi)) {
    for (const u of cssUrls(css)) check("inline style url()", u);
  }
  for (const [, css] of html.matchAll(/\sstyle\s*=\s*"([^"]*)"/gi)) {
    for (const u of cssUrls(css)) check("style attribute url()", u);
  }
  for (const [tag] of html.matchAll(/<a\b[^>]*>/gi)) {
    const href = attr(tag, "href");
    if (href?.startsWith("/") && !href.startsWith("//") && !href.startsWith(BASE)) {
      violations.push(`${rel}: link without the ${BASE} base: ${href}`);
    }
  }
  const title = html.match(/<title[^>]*>([\s\S]*?)<\/title>/i);
  if (!title || !title[1].trim()) violations.push(`${rel}: missing or empty <title>`);
}

for (const file of files(dist, ".css")) {
  for (const u of cssUrls(readFileSync(file, "utf8"))) outside(relative(dist, file), "css url()", u);
}

let landing = "";
try {
  landing = readFileSync(join(dist, "index.html"), "utf8");
} catch {
  violations.push("index.html: missing");
}
for (const text of landing ? LANDING_TEXT : []) {
  if (!landing.includes(text)) violations.push(`index.html: static page lacks ${JSON.stringify(text)}`);
}

if (pages === 0) violations.push("no HTML pages found in dist/ (run the build first)");
if (violations.length) {
  console.error(violations.join("\n"));
  process.exit(1);
}
console.log(`audit-dist: ${pages} pages, no violations`);
