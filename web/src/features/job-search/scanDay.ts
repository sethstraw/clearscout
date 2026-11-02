/** Show a scan's calendar day in the viewer's locale without a timezone shift. */
export function scanDay(isoDate: string): string {
  return new Date(`${isoDate}T00:00:00`).toLocaleDateString();
}
