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
      menu?: {
        onAction: (cb: (action: string) => void) => () => void
      }
      backend?: {
        /** Backend-process lifecycle from the Electron main process (crash/restart/give-up). */
        onStatus: (cb: (payload: { state: 'restarting' | 'ok' | 'failed'; attempt?: number; max?: number }) => void) => () => void
      }
    }
  }
}

export {}
