// The refresh task accepts at most this many pending discovery ids per request.
export const READ_BATCH_SIZE = 250;

/** Split items into consecutive batches of at most `size`, keeping every item in order. */
export function readBatches<T>(items: readonly T[], size: number = READ_BATCH_SIZE): T[][] {
  const batches: T[][] = [];
  for (let offset = 0; offset < items.length; offset += size) {
    batches.push(items.slice(offset, offset + size));
  }
  return batches;
}
