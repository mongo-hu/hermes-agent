import { readdirSync, rmSync } from 'fs'
import { relative, resolve } from 'path'

const repoRoot = resolve(import.meta.dirname, '..', '..', '..')
const sourceRoots = [resolve(repoRoot, 'apps', 'desktop', 'src'), resolve(repoRoot, 'apps', 'shared', 'src')]

function sourceJavaScriptFiles(directory) {
  return readdirSync(directory, { withFileTypes: true }).flatMap(entry => {
    const path = resolve(directory, entry.name)

    if (entry.isDirectory()) {
      return sourceJavaScriptFiles(path)
    }

    return entry.isFile() && entry.name.endsWith('.js') ? [path] : []
  })
}

const files = sourceRoots.flatMap(sourceJavaScriptFiles)

if (process.argv.includes('--clean')) {
  for (const file of files) {
    rmSync(file)
  }

  console.log(
    `Removed ${files.length} generated JavaScript file${files.length === 1 ? '' : 's'} from source directories.`
  )
  process.exit(0)
}

if (files.length > 0) {
  const sample = files
    .slice(0, 12)
    .map(file => `  - ${relative(repoRoot, file)}`)
    .join('\n')
  const remainder = files.length > 12 ? `\n  ... and ${files.length - 12} more` : ''

  console.error(
    [
      'Generated JavaScript is shadowing TypeScript source files.',
      sample + remainder,
      'Run `npm run clean:source-js` from apps/desktop, then retry.'
    ].join('\n')
  )
  process.exit(1)
}
