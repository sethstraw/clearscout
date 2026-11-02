const PALETTE = [
  [79, 70, 229],
  [13, 148, 136],
  [37, 99, 235],
  [180, 83, 9],
  [185, 28, 28],
] as const;

export function tagColorClass(color: string): string {
  const value = Number.parseInt(color.slice(1), 16);
  if (!Number.isFinite(value)) return "tag-color--accent";
  const rgb = [(value >> 16) & 255, (value >> 8) & 255, value & 255];
  const closest = PALETTE.reduce(
    (best, candidate, index) => {
      const distance = candidate.reduce(
        (total, channel, channelIndex) => total + (channel - rgb[channelIndex]!) ** 2,
        0,
      );
      return distance < best.distance ? { distance, index } : best;
    },
    { distance: Number.POSITIVE_INFINITY, index: 0 },
  );
  return [
    "tag-color--accent",
    "tag-color--teal",
    "tag-color--blue",
    "tag-color--amber",
    "tag-color--danger",
  ][closest.index]!;
}
