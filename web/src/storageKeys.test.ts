import { afterEach, describe, expect, it } from "vitest";

import { moveFormerStorageKeys } from "./storageKeys";

describe("moveFormerStorageKeys", () => {
  afterEach(() => window.localStorage.clear());

  it("moves a key saved under the former name", () => {
    window.localStorage.setItem("tailorcv.theme", "dark");

    moveFormerStorageKeys(window.localStorage);

    expect(window.localStorage.getItem("clearscout.theme")).toBe("dark");
    expect(window.localStorage.getItem("tailorcv.theme")).toBeNull();
  });

  it("keeps a value already saved under the new name", () => {
    window.localStorage.setItem("tailorcv.nav", "collapsed");
    window.localStorage.setItem("clearscout.nav", "expanded");

    moveFormerStorageKeys(window.localStorage);

    expect(window.localStorage.getItem("clearscout.nav")).toBe("expanded");
    expect(window.localStorage.getItem("tailorcv.nav")).toBeNull();
  });

  it("leaves keys from other applications alone", () => {
    window.localStorage.setItem("other.theme", "light");

    moveFormerStorageKeys(window.localStorage);

    expect(window.localStorage.getItem("other.theme")).toBe("light");
  });
});
