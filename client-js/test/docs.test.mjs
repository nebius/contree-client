/** Runs the annotated `js` code blocks in docs/**\/*.md as real tests.
 *
 * An HTML comment right above a fence carries `key: value` pairs,
 * exactly like the Python side's markdown-pytest convention:
 *   <!-- name: some_name; fixtures: client, operationId -->
 *   ```js
 *   ...
 *   ```
 * Blocks sharing a `name` (within one file) concatenate, in document
 * order, into one test body - so a multi-block example can build on
 * variables an earlier block in the same section declared. `fixtures`
 * is a comma list resolved against doc-fixtures.mjs and bound as that
 * many named parameters; `exported as local` binds a differently-named
 * fixture under the identifier the visible code actually uses (e.g. a
 * client pre-armed to throw, bound as `client`). An unannotated ```js
 * fence is left alone: purely illustrative, not executed.
 */

import { readFileSync, readdirSync } from "node:fs";
import { dirname, join, relative } from "node:path";
import { test } from "node:test";
import { fileURLToPath } from "node:url";

import { fixtures } from "./doc-fixtures.mjs";

const HERE = dirname(fileURLToPath(import.meta.url));
const DOCS_DIR = join(HERE, "..", "..", "docs");
const SKIP_DIRS = new Set(["_build", "node_modules"]);

/** Every `.md` file under `docs/`, recursively (`docs/tutorial.md`,
 * `docs/js/*.md`, ... - `fs.readdirSync`'s `recursive` option needs
 * Node 20.1+, and this package supports Node >=18.17, so this walks
 * by hand instead). */
function* walkMarkdownFiles(dir) {
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    if (entry.isDirectory()) {
      if (!SKIP_DIRS.has(entry.name)) {
        yield* walkMarkdownFiles(join(dir, entry.name));
      }
    } else if (entry.name.endsWith(".md")) {
      yield join(dir, entry.name);
    }
  }
}

const ANNOTATION_RE = /^<!--\s*(.+?)\s*-->\s*$/;

function parseArguments(commentBody) {
  const result = {};
  for (const part of commentBody.split(";")) {
    const colon = part.indexOf(":");
    if (colon === -1) continue;
    const key = part.slice(0, colon).trim();
    const value = part.slice(colon + 1).trim();
    if (key) result[key] = value;
  }
  return result;
}

/** Parse a `fixtures:` value into `{ exported, local }` pairs; a bare
 * name binds under its own name, `exported as local` renames it. */
function parseFixtureList(value) {
  return (value ?? "")
    .split(",")
    .map((entry) => entry.trim())
    .filter(Boolean)
    .map((entry) => {
      const [, exported, local] = entry.match(/^(\S+)(?:\s+as\s+(\S+))?$/);
      return { exported, local: local ?? exported };
    });
}

/** Scan one markdown file for annotated ```js fences. Returns
 * `{ name, fixtureRefs, startLine, source }[]`, one entry per fence
 * (not yet grouped by name). */
function scanFile(path) {
  const lines = readFileSync(path, "utf8").split("\n");
  const blocks = [];
  for (let i = 0; i < lines.length; i++) {
    const match = lines[i].match(ANNOTATION_RE);
    if (!match) continue;
    let fenceLine = i + 1;
    while (fenceLine < lines.length && lines[fenceLine].trim() === "") {
      fenceLine++;
    }
    if (lines[fenceLine]?.trim() !== "```js") continue;
    const args = parseArguments(match[1]);
    if (!args.name) continue;
    const codeLines = [];
    let k = fenceLine + 1;
    while (k < lines.length && lines[k].trim() !== "```") {
      codeLines.push(lines[k]);
      k++;
    }
    blocks.push({
      name: args.name,
      fixtureRefs: parseFixtureList(args.fixtures),
      startLine: fenceLine + 2, // 1-based line number of the first code line
      source: codeLines.join("\n"),
    });
    i = k;
  }
  return blocks;
}

/** Group same-named blocks (within one file) into one compiled test:
 * concatenated source, the union of every block's requested fixtures,
 * tagged with the file and the first block's starting line. */
function groupBlocks(path, blocks) {
  const groups = new Map();
  for (const block of blocks) {
    const existing = groups.get(block.name);
    if (existing === undefined) {
      groups.set(block.name, {
        path,
        name: block.name,
        startLine: block.startLine,
        fixtureRefs: [...block.fixtureRefs],
        source: block.source,
      });
      continue;
    }
    existing.source += "\n" + block.source;
    for (const ref of block.fixtureRefs) {
      if (!existing.fixtureRefs.some((r) => r.local === ref.local)) {
        existing.fixtureRefs.push(ref);
      }
    }
  }
  return [...groups.values()];
}

// a plain Function body cannot contain a static `import` statement,
// so each one becomes an awaited dynamic import bound the same way -
// `{ A, B }` / `* as ns` / a bare default name all destructure the
// same from a namespace object.
const IMPORT_RE = /import\s+([\s\S]+?)\s+from\s+["']([^"']+)["'];?/g;

function rewriteImports(source) {
  return source.replace(IMPORT_RE, (_whole, clause, specifier) => {
    const trimmed = clause.trim();
    const binding = trimmed.startsWith("*")
      ? trimmed.replace(/^\*\s*as\s*/, "").trim()
      : trimmed.startsWith("{")
        ? trimmed
        : `{ default: ${trimmed} }`;
    return `const ${binding} = await import(${JSON.stringify(specifier)});`;
  });
}

function compileGroup(group) {
  for (const { exported } of group.fixtureRefs) {
    if (!(exported in fixtures)) {
      throw new Error(
        `${group.path}:${group.startLine}: unknown fixture ${JSON.stringify(exported)}`,
      );
    }
  }
  const sourceUrl = `${group.path}:${group.startLine}`;
  const body = `${rewriteImports(group.source)}\n//# sourceURL=${sourceUrl}`;
  // eslint-disable-next-line no-new-func
  const fn = new Function(
    ...group.fixtureRefs.map((ref) => ref.local),
    `return (async () => {\n${body}\n})();`,
  );
  return async () => {
    await fn(...group.fixtureRefs.map((ref) => fixtures[ref.exported]()));
  };
}

for (const path of [...walkMarkdownFiles(DOCS_DIR)].sort()) {
  const groups = groupBlocks(path, scanFile(path));
  for (const group of groups) {
    const run = compileGroup(group);
    const label = relative(DOCS_DIR, path);
    test(`docs/${label} :: ${group.name}`, run);
  }
}
