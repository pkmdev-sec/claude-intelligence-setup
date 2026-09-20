#!/usr/bin/env node

import { execFileSync } from "node:child_process";
import fs from "node:fs/promises";
import path from "node:path";
import process from "node:process";
import remend from "remend";

const DOC_EXTENSIONS = new Set([".md", ".markdown", ".mdown", ".mdx", ".mkd"]);
const DEFAULT_MAX_BYTES = 600_000;
const TRAILING_WINDOW = 500;

function parseArgs(argv) {
  const opts = {
    check: false,
    changed: false,
    staged: false,
    verbose: false,
    maxBytes: DEFAULT_MAX_BYTES,
    files: [],
  };

  for (let i = 0; i < argv.length; i += 1) {
    const arg = argv[i];
    if (arg === "--check") opts.check = true;
    else if (arg === "--changed") opts.changed = true;
    else if (arg === "--staged") opts.staged = true;
    else if (arg === "--verbose") opts.verbose = true;
    else if (arg === "--max-bytes") {
      const raw = argv[i + 1];
      const parsed = Number(raw);
      if (!Number.isFinite(parsed) || parsed <= 0) {
        throw new Error(`Invalid --max-bytes value: ${raw}`);
      }
      opts.maxBytes = parsed;
      i += 1;
    } else if (arg.startsWith("-")) {
      throw new Error(`Unknown option: ${arg}`);
    } else {
      opts.files.push(arg);
    }
  }

  if (opts.staged && opts.changed) {
    throw new Error("Use only one of --staged or --changed");
  }

  return opts;
}

function isDocFile(filePath) {
  return DOC_EXTENSIONS.has(path.extname(filePath).toLowerCase());
}

function looksLikeTrailingTruncation(content) {
  const tail = content.slice(-TRAILING_WINDOW).trimEnd();
  const patterns = [
    /\[[^\]\n]{1,200}\]\([^)\n]{0,320}$/,
    /\[[^\]\n]{1,200}$/,
    /\*\*[^*\n]{1,220}$/,
    /~~[^~\n]{1,220}$/,
    /`[^`\n]{1,220}$/,
    /\$\$[^$]{1,280}$/,
    /(^|\n)```[^\n]*\n[\s\S]{0,320}$/,
  ];
  return patterns.some((re) => re.test(tail));
}

function parsePorcelainPath(line) {
  const raw = line.slice(3).trim();
  if (raw.includes(" -> ")) return raw.split(" -> ").at(-1) ?? "";
  return raw;
}

function listGitChangedFiles(mode) {
  try {
    execFileSync("git", ["rev-parse", "--is-inside-work-tree"], { stdio: "ignore" });
  } catch {
    return [];
  }

  if (mode === "staged") {
    const out = execFileSync(
      "git",
      ["diff", "--cached", "--name-only", "--diff-filter=ACMR"],
      { encoding: "utf8" },
    );
    return out
      .split("\n")
      .map((line) => line.trim())
      .filter(Boolean);
  }

  const out = execFileSync("git", ["status", "--porcelain"], { encoding: "utf8" });
  return out
    .split("\n")
    .filter(Boolean)
    .map(parsePorcelainPath)
    .map((line) => line.trim())
    .filter(Boolean);
}

async function loadUtf8(filePath) {
  const buf = await fs.readFile(filePath);
  if (buf.includes(0)) {
    return null;
  }
  return buf.toString("utf8");
}

async function normalizeOne(inputPath, opts) {
  const filePath = path.resolve(inputPath);

  if (!isDocFile(filePath)) {
    return { status: "skip", filePath, reason: "unsupported extension" };
  }

  let stat;
  try {
    stat = await fs.stat(filePath);
  } catch (err) {
    return { status: "skip", filePath, reason: "missing file" };
  }

  if (!stat.isFile()) {
    return { status: "skip", filePath, reason: "not a regular file" };
  }

  if (stat.size > opts.maxBytes) {
    return { status: "skip", filePath, reason: `over max size (${stat.size} bytes)` };
  }

  const original = await loadUtf8(filePath);
  if (original == null) {
    return { status: "skip", filePath, reason: "binary file" };
  }

  if (original.includes("streamdown-doc-gate: ignore")) {
    return { status: "skip", filePath, reason: "file-level ignore marker set" };
  }

  const repaired = remend(original, { linkMode: "protocol" });
  if (repaired === original) {
    return { status: "clean", filePath };
  }

  if (!looksLikeTrailingTruncation(original)) {
    return {
      status: "manual",
      filePath,
      reason: "repair needed but no trailing truncation signature",
    };
  }

  if (opts.check) {
    return { status: "needs-fix", filePath };
  }

  await fs.writeFile(filePath, repaired, "utf8");
  return { status: "fixed", filePath };
}

function printResult(result, verbose) {
  if (result.status === "clean" && !verbose) return;
  if (result.status === "clean") console.log(`CLEAN  ${result.filePath}`);
  else if (result.status === "fixed") console.log(`FIXED  ${result.filePath}`);
  else if (result.status === "needs-fix") console.log(`CHECK  ${result.filePath}`);
  else if (result.status === "manual") console.log(`MANUAL ${result.filePath} (${result.reason})`);
  else if (result.status === "skip" && verbose) console.log(`SKIP   ${result.filePath} (${result.reason})`);
  else if (result.status === "error") console.log(`ERROR  ${result.filePath} (${result.reason})`);
}

async function main() {
  const opts = parseArgs(process.argv.slice(2));

  const sourceFiles =
    opts.files.length > 0
      ? opts.files
      : listGitChangedFiles(opts.staged ? "staged" : "changed");

  const uniqueDocs = [...new Set(sourceFiles)].filter((file) => isDocFile(file));
  if (uniqueDocs.length === 0) {
    console.log("No documentation files found for Streamdown normalization.");
    return 0;
  }

  const results = [];
  for (const file of uniqueDocs) {
    try {
      const result = await normalizeOne(file, opts);
      results.push(result);
      printResult(result, opts.verbose);
    } catch (err) {
      const reason = err instanceof Error ? err.message : String(err);
      const result = { status: "error", filePath: path.resolve(file), reason };
      results.push(result);
      printResult(result, true);
    }
  }

  const fixed = results.filter((r) => r.status === "fixed").length;
  const needsFix = results.filter((r) => r.status === "needs-fix").length;
  const manual = results.filter((r) => r.status === "manual").length;
  const errors = results.filter((r) => r.status === "error").length;
  const clean = results.filter((r) => r.status === "clean").length;

  console.log(
    `Summary: clean=${clean} fixed=${fixed} needs_fix=${needsFix} manual=${manual} errors=${errors}`,
  );

  if (opts.check) {
    return needsFix + manual + errors > 0 ? 1 : 0;
  }

  return manual + errors > 0 ? 2 : 0;
}

main()
  .then((code) => {
    process.exitCode = code;
  })
  .catch((err) => {
    const msg = err instanceof Error ? err.message : String(err);
    console.error(`streamdown-doc-gate failed: ${msg}`);
    process.exitCode = 1;
  });
