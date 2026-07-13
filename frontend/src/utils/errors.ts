/** Extract a message from a caught value, falling back for non-Error throws. */
export function errorMessage(err: unknown, fallback: string): string {
  return err instanceof Error ? err.message : fallback
}
