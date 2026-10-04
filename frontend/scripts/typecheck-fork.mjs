#!/usr/bin/env node
/**
 * Type checks the app with vue-tsc (templates included) and fails only on errors in this fork's own files: the AI
 * provider, recipe card and MCP frontend (docs/ai/PHASE2.md §18). Upstream Mealie has type errors of its own, so a
 * plain `vue-tsc` run can't gate CI; this one gates the fork's code and reports the rest as a count.
 *
 * Run `pnpm nuxt prepare` first (it writes `.nuxt/tsconfig.app.json`). Exits 0 when no fork file has an error, 1 when
 * one does, 2 when vue-tsc couldn't run. Fork-owned.
 */
import { spawnSync } from "node:child_process";
import { existsSync } from "node:fs";
import { dirname, join, relative, resolve, sep } from "node:path";
import { fileURLToPath } from "node:url";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const project = ".nuxt/tsconfig.app.json";

/** The fork's files, relative to `frontend/` (upstream files with a fork hook keep upstream's errors, so they're out) */
export const FORK_PATHS = [
  /^app\/components\/Domain\/Ingest\//,
  /^app\/components\/Domain\/Group\/(GroupAIProvider|GroupMcp|GroupRecipeCardSettings)/,
  /^app\/components\/Domain\/Household\/HouseholdNotifierAIEvents/,
  /^app\/components\/Domain\/User\/UserMcp/,
  /^app\/components\/Layout\/(DefaultLayout|LayoutParts\/AppHeader)\.test\.ts$/,
  /^app\/composables\/(__tests__\/)?use-(recipe-ingest|ai-provider-routing|mcp)[\w.-]*$/,
  /^app\/composables\/__tests__\/use-auth-backend-restore-pause\.test\.ts$/,
  /^app\/lib\/api\/user\/(recipe-ingest|mcp|group-ai-providers)\.ts$/,
  /^app\/lib\/api\/admin\/admin-ai-providers\.ts$/,
  /^app\/pages\/admin\/backups\.test\.ts$/,
  /^app\/plugins\/__tests__\/axios-restore-pause\.test\.ts$/,
  /^app\/pages\/g\/\[groupSlug\]\/recipes\/cards\//,
  /^app\/pages\/g\/\[groupSlug\]\/r\/create\/ai\.test\.ts$/,
  /^app\/pages\/group\/index\.test\.ts$/,
  /^app\/pages\/oauth\/consent\./,
  /^app\/pages\/user\/profile\/connected-apps\.vue$/,
];

export function isForkFile(file) {
  const path = relative(root, resolve(root, file)).split(sep).join("/");
  return FORK_PATHS.some(pattern => pattern.test(path));
}

/** vue-tsc's errors: `file(line,col): error TSnnnn: message`, with indented continuation lines */
export function parseErrors(output) {
  const errors = [];
  for (const line of output.split(/\r?\n/)) {
    const match = /^(.+?)\((\d+),(\d+)\): error (TS\d+): /.exec(line);
    if (match) {
      errors.push({ file: match[1], text: line });
    }
    else if (errors.length && /^\s+\S/.test(line)) {
      errors[errors.length - 1].text += `\n${line}`;
    }
  }
  return errors;
}

function main() {
  if (!existsSync(join(root, project))) {
    console.error(`${project} is missing: run \`pnpm nuxt prepare\` first.`);
    return 2;
  }
  const bin = join(root, "node_modules", ".bin", process.platform === "win32" ? "vue-tsc.cmd" : "vue-tsc");
  const result = spawnSync(bin, ["--noEmit", "--pretty", "false", "-p", project], {
    cwd: root,
    encoding: "utf8",
    maxBuffer: 256 * 1024 * 1024,
    shell: process.platform === "win32",
  });
  if (result.error) {
    console.error(`vue-tsc couldn't run: ${result.error.message}`);
    return 2;
  }
  const output = `${result.stdout ?? ""}${result.stderr ?? ""}`;
  const errors = parseErrors(output);
  if (result.status !== 0 && !errors.length) {
    // not type errors: vue-tsc itself failed
    console.error(output);
    return 2;
  }

  const fork = errors.filter(error => isForkFile(error.file));
  for (const error of fork) {
    console.error(error.text);
  }
  console.log(`vue-tsc: ${errors.length} error(s), ${fork.length} in the fork's files (${errors.length - fork.length} upstream).`);
  return fork.length ? 1 : 0;
}

if (resolve(process.argv[1] ?? "") === fileURLToPath(import.meta.url)) {
  process.exit(main());
}
