// Regression guards for Windows `hermes` resolution in main.ts.
//
// main.ts has no module.exports, so these follow the repo's source-assertion
// test pattern (see windows-child-process.test.ts). They pin the two Windows
// resolution bugs that caused desktop reinstall loops:
//   1. findOnPath() tried the empty extension FIRST, so an extensionless
//      Git-Bash `hermes` shim shadowed the real hermes.cmd/hermes.exe; the
//      shim then failed the --version probe and the desktop fell through to a
//      spurious bootstrap/repair.
//   2. handOffWindowsBootstrapRecovery() chose --update vs the destructive
//      --repair by checking ONLY venv\Scripts\hermes.exe (the console-script
//      shim, written at the END of venv setup and absent in interrupted
//      states), so it escalated to a full venv recreate even on healthy
//      installs.
//   3. unwrapWindowsVenvHermesCommand() returned the venv python with NO
//      runtime probe (bypassing the caller's --version check too), so a venv
//      broken mid-update (e.g. missing python-dotenv) was re-selected forever:
//      Retry / "Repair install" resolved the same dead interpreter instead of
//      falling through to the bootstrap installer.

import assert from 'node:assert/strict'
import fs from 'node:fs'
import path from 'node:path'
import test from 'node:test'
import { fileURLToPath } from 'node:url'

const __dirname = path.dirname(fileURLToPath(import.meta.url))

function readMain() {
  return fs.readFileSync(path.join(__dirname, 'main.ts'), 'utf8').replace(/\r\n/g, '\n')
}

test('findOnPath tries PATHEXT extensions before the bare (empty) name on Windows', () => {
  const source = readMain()
  // Fixed order: PATHEXT first, empty string LAST.
  assert.match(
    source,
    /\(process\.env\.PATHEXT \|\| '\.COM;\.EXE;\.BAT;\.CMD'\)\.split\(';'\)\.filter\(Boolean\), ''\]/,
    'extensions array must end with the empty string, not start with it'
  )
  // The buggy empty-first order must not return.
  assert.doesNotMatch(
    source,
    /\['', \.\.\.\(process\.env\.PATHEXT/,
    'empty-extension-first order regressed: an extensionless shim can shadow hermes.cmd/.exe'
  )
})

test('packaged DFM client pins its analysis home before generic HERMES_HOME resolution', () => {
  const source = readMain()
  const resolverStart = source.indexOf('function resolveHermesHome()')
  const resolverEnd = source.indexOf('\nfunction ', resolverStart + 1)
  const body = source.slice(resolverStart, resolverEnd === -1 ? undefined : resolverEnd)
  const dfmHome = body.indexOf('IS_PACKAGED && IS_DFM_PRODUCT')
  const genericHome = body.indexOf('if (process.env.HERMES_HOME)')

  assert.notEqual(dfmHome, -1, 'dedicated DFM home guard must exist')
  assert.notEqual(genericHome, -1, 'generic HERMES_HOME fallback must remain available')
  assert.ok(dfmHome < genericHome, 'DFM home guard must run before a machine-wide HERMES_HOME can redirect the client')
  assert.match(body, /return path\.join\(process\.env\.LOCALAPPDATA, 'hermes'\)/)
})

test('packaged DFM client prefers its stamped local source and rejects external runtimes', () => {
  const source = readMain()
  const resolverStart = source.indexOf('function resolveHermesBackend(backendArgs)')
  const resolverEnd = source.indexOf('\nasync function ensureRuntime(', resolverStart + 1)
  const body = source.slice(resolverStart, resolverEnd === -1 ? undefined : resolverEnd)
  const stampedSource = body.indexOf('const stampedLocalRoot =')
  const pathLookup = body.indexOf("findOnPath('hermes')")

  assert.notEqual(stampedSource, -1, 'local DFM build must resolve its stamped source checkout')
  assert.notEqual(pathLookup, -1, 'generic products must retain external CLI compatibility')
  assert.ok(stampedSource < pathLookup, 'stamped DFM source must win before any external CLI lookup')
  assert.match(body, /!\(IS_PACKAGED && IS_DFM_PRODUCT\).*HERMES_DESKTOP_IGNORE_EXISTING/)
  assert.match(body, /const python = IS_PACKAGED && IS_DFM_PRODUCT \? null : findSystemPython\(\)/)
})

test('Windows bootstrap recovery chooses --update when any real-install signal is present', () => {
  const source = readMain()
  assert.match(source, /const haveRealInstall =/, 'recovery must compute haveRealInstall')
  assert.match(source, /fileExists\(venvPython\)/, 'recovery must accept the venv interpreter as a real-install signal')
  assert.match(
    source,
    /\.hermes-bootstrap-complete/,
    'recovery must accept the bootstrap-complete marker as a real-install signal'
  )
  assert.match(source, /updaterArgs = haveRealInstall \? \['--update'/, 'updaterArgs must gate on haveRealInstall')
  // The old too-narrow check (only venv\Scripts\hermes.exe) must not return.
  assert.doesNotMatch(
    source,
    /updaterArgs = fileExists\(venvHermes\) \?/,
    'recovery regressed to gating only on the hermes.exe shim, which forces destructive --repair'
  )
})

test('unwrapWindowsVenvHermesCommand smoke-tests the venv python before trusting it', () => {
  const source = readMain()
  const fnStart = source.indexOf('function unwrapWindowsVenvHermesCommand(')
  assert.notEqual(fnStart, -1, 'unwrapWindowsVenvHermesCommand must exist in main.ts')
  // Slice out just the function body (up to the next top-level function decl)
  const fnEnd = source.indexOf('\nfunction ', fnStart + 1)
  const body = source.slice(fnStart, fnEnd === -1 ? undefined : fnEnd)
  assert.match(
    body,
    /canImportHermesCli\(python/,
    'unwrap must probe the venv interpreter; returning it unprobed re-selects a broken venv ' +
      'forever (Retry/Repair loop on a mid-update venv missing e.g. python-dotenv)'
  )
  assert.match(
    body,
    /return null\s*\n\s*\}\s*\n\s*return \{/,
    'a failed probe must fall through (return null) so the resolver reaches the bootstrap rung'
  )
})
