import { describe, expect, it } from "vitest";

import { tagColorClass } from "./tagColor";

describe("tagColorClass", () => {
  it("maps persisted colours to one finite stylesheet palette", () => {
    expect(tagColorClass("#4f46e5")).toBe("tag-color--accent");
    expect(tagColorClass("#0d9488")).toBe("tag-color--teal");
    expect(tagColorClass("not-a-colour")).toBe("tag-color--accent");
  });
});
