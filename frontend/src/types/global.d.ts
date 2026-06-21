declare global {
  interface Window {
    opentrace?: {
      backendUrl: string
      terminal?: {
        start: (opts?: { cols?: number; rows?: number }) => Promise<{
          shell: string
          shellName: string
          cwd: string
          pid: number
          tracing: boolean
        }>
        write: (data: string) => void
        resize: (cols: number, rows: number) => void
        onData: (cb: (data: string) => void) => () => void
        onExit: (cb: (payload: { exitCode: number; signal?: number }) => void) => () => void
      }
      tracing?: {
        set: (enabled: boolean) => Promise<boolean>
        get: () => Promise<boolean>
      }
      session?: {
        set: (id: string) => Promise<boolean>
      }
    }
  }
}

export {}
