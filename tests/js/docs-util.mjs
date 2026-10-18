// The one place the JS tests read the documentation from (issue #244, slice 0): a test that pins a sentence of the docs goes through readDoc(),
// so moving a section to another page changes a path here and not in each test.
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

export const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..', '..');

// Where the sections of the former README live (issue #244, slice 1); the Python twin is tests/docs_util.py.
export const MCP_PAGE = 'docs/guide/mcp.md';

export function readDoc(rel) {
  return fs.readFileSync(path.join(ROOT, rel), 'utf8');
}
