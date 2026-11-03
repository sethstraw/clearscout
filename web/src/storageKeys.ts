const FORMER_PREFIX = "tailorcv.";
const PREFIX = "clearscout.";

/** Move keys saved under the project's former name; a key already moved is kept. */
export function moveFormerStorageKeys(storage: Storage): void {
  const former: string[] = [];
  for (let index = 0; index < storage.length; index += 1) {
    const key = storage.key(index);
    if (key?.startsWith(FORMER_PREFIX)) former.push(key);
  }
  for (const key of former) {
    const target = PREFIX + key.slice(FORMER_PREFIX.length);
    const value = storage.getItem(key);
    if (value !== null && storage.getItem(target) === null) storage.setItem(target, value);
    storage.removeItem(key);
  }
}
