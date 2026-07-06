/** Load every scenario file (except _helpers/index) into one flat registry. */
const fs = require('fs')
const path = require('path')

const registry = []
const seen = new Set()
for (const f of fs.readdirSync(__dirname).sort()) {
  if (!f.endsWith('.js') || f.startsWith('_') || f === 'index.js') continue
  const mod = require(path.join(__dirname, f))
  const arr = Array.isArray(mod) ? mod : mod.scenarios || []
  for (const s of arr) {
    if (!s || !s.id || typeof s.run !== 'function') throw new Error(`bad scenario in ${f}: ${JSON.stringify(s && s.id)}`)
    if (seen.has(s.id)) throw new Error(`duplicate scenario id: ${s.id} (in ${f})`)
    seen.add(s.id)
    registry.push({ file: f, ...s })
  }
}

module.exports = registry
