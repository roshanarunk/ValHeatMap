import { useRef, useState } from 'react'
import { Empty, Field, Panel } from './Controls'
import { api } from '../lib/api'

type Status = { kind: 'idle' | 'busy' | 'ok' | 'error'; message?: string }

const REGIONS = ['na', 'eu', 'ap', 'kr', 'latam', 'br']

/**
 * Bringing matches in: by Riot ID via HenrikDev, by match id from either
 * API, or by dropping a raw match JSON file.
 */
export function ImportPanel({
  liveSources,
  onImported,
}: {
  liveSources: Record<string, boolean>
  onImported: () => Promise<void> | void
}) {
  const [open, setOpen] = useState(false)
  const [status, setStatus] = useState<Status>({ kind: 'idle' })
  const [riotId, setRiotId] = useState('')
  const [matchId, setMatchId] = useState('')
  const [region, setRegion] = useState('na')
  const fileRef = useRef<HTMLInputElement | null>(null)

  const henrikReady = !!liveSources.henrik
  const riotReady = !!liveSources.riot

  const run = async (task: () => Promise<string>) => {
    setStatus({ kind: 'busy' })
    try {
      const message = await task()
      await onImported()
      setStatus({ kind: 'ok', message })
    } catch (e) {
      setStatus({ kind: 'error', message: e instanceof Error ? e.message : String(e) })
    }
  }

  const importPlayer = () => {
    const trimmed = riotId.trim()
    const [name, tag] = trimmed.split('#')
    if (!name || !tag) {
      setStatus({ kind: 'error', message: 'Enter a Riot ID as Name#TAG.' })
      return
    }
    void run(async () => {
      const res = await api.importHenrikPlayer(name, tag, region)
      return `Imported ${res.count} match${res.count === 1 ? '' : 'es'} for ${trimmed}.`
    })
  }

  const importMatch = () => {
    const id = matchId.trim()
    if (!id) return
    void run(async () => {
      // Prefer HenrikDev, fall back to the official API if only it is keyed.
      const res = henrikReady
        ? await api.importHenrik(id, region)
        : await api.importRiot(id, region)
      return `Imported ${res.match.map_name} (${res.match.kills} kills).`
    })
  }

  const importFile = (file: File) => {
    void run(async () => {
      const text = await file.text()
      let payload: unknown
      try {
        payload = JSON.parse(text)
      } catch {
        throw new Error('That file is not valid JSON.')
      }
      const res = await api.importUpload(payload)
      return `Imported ${res.match.map_name} (${res.match.kills} kills).`
    })
  }

  return (
    <Panel
      title="Add matches"
      subtitle={henrikReady ? 'HenrikDev connected' : 'Upload JSON, or add an API key'}
      actions={
        <button type="button" className="linkbtn" onClick={() => setOpen((v) => !v)}>
          {open ? 'Hide' : 'Open'}
        </button>
      }
    >
      {open && (
        <div className="import">
          {(henrikReady || riotReady) && (
            <Field label="Region">
              <select value={region} onChange={(e) => setRegion(e.target.value)}>
                {REGIONS.map((r) => (
                  <option key={r} value={r}>
                    {r.toUpperCase()}
                  </option>
                ))}
              </select>
            </Field>
          )}

          {henrikReady && (
            <Field label="Riot ID" hint="Pulls that player's recent matches">
              <div className="inputrow">
                <input
                  value={riotId}
                  placeholder="Name#TAG"
                  onChange={(e) => setRiotId(e.target.value)}
                  onKeyDown={(e) => e.key === 'Enter' && importPlayer()}
                />
                <button type="button" onClick={importPlayer} disabled={status.kind === 'busy'}>
                  Fetch
                </button>
              </div>
            </Field>
          )}

          {(henrikReady || riotReady) && (
            <Field label="Match ID">
              <div className="inputrow">
                <input
                  value={matchId}
                  placeholder="UUID"
                  onChange={(e) => setMatchId(e.target.value)}
                  onKeyDown={(e) => e.key === 'Enter' && importMatch()}
                />
                <button type="button" onClick={importMatch} disabled={status.kind === 'busy'}>
                  Fetch
                </button>
              </div>
            </Field>
          )}

          <Field label="Match JSON" hint="Riot match-v1 or HenrikDev v4 payload">
            <div className="inputrow">
              <input
                ref={fileRef}
                type="file"
                accept="application/json,.json"
                onChange={(e) => {
                  const file = e.target.files?.[0]
                  if (file) importFile(file)
                  e.target.value = ''
                }}
              />
            </div>
          </Field>

          {!henrikReady && !riotReady && (
            <Empty>
              Set <code>HENRIK_API_KEY</code> (or <code>RIOT_API_KEY</code>) on the server to pull
              matches live. Uploading a JSON file works without any key.
            </Empty>
          )}

          {status.kind !== 'idle' && status.message && (
            <p className={`import__status import__status--${status.kind}`}>{status.message}</p>
          )}
          {status.kind === 'busy' && <p className="import__status">Working…</p>}
        </div>
      )}
    </Panel>
  )
}
