import { describe, expect, it } from "vitest";
import { describeRule, formatDay } from "./dates";

describe("describeRule monthly forms", () => {
  it("describes numbered weekdays", () => {
    expect(describeRule("FREQ=MONTHLY;BYDAY=2TU")).toBe("monthly on the 2nd Tuesday");
    expect(describeRule("FREQ=MONTHLY;BYDAY=-1FR")).toBe("monthly on the last Friday");
  });

  it("describes days of the month", () => {
    expect(describeRule("FREQ=MONTHLY;BYMONTHDAY=15")).toBe("monthly on the 15th");
    expect(describeRule("FREQ=MONTHLY;BYMONTHDAY=2,3,11,12,13,21,22,23,31")).toBe(
      "monthly on the 2nd, 3rd, 11th, 12th, 13th, 21st, 22nd, 23rd, 31st",
    );
  });

  it("joins lists with a single leading 'the'", () => {
    expect(describeRule("FREQ=MONTHLY;BYMONTHDAY=1,15")).toBe("monthly on the 1st, 15th");
    expect(describeRule("FREQ=MONTHLY;BYDAY=1MO,3MO")).toBe("monthly on the 1st Monday, 3rd Monday");
  });

  it("combines with INTERVAL, UNTIL and COUNT", () => {
    expect(describeRule("FREQ=MONTHLY;INTERVAL=2;BYMONTHDAY=1;COUNT=6")).toBe("every 2 months on the 1st, 6 times");
    expect(describeRule("FREQ=MONTHLY;INTERVAL=3;BYDAY=-1FR")).toBe("every 3 months on the last Friday");
    expect(describeRule("FREQ=MONTHLY;BYDAY=2TU;UNTIL=20261231T235959Z")).toBe(
      "monthly on the 2nd Tuesday until " + formatDay("2026-12-31"),
    );
    expect(describeRule("FREQ=MONTHLY;BYDAY=5SU;COUNT=4")).toBe("monthly on the 5th Sunday, 4 times");
  });
});

describe("describeRule existing forms", () => {
  it("keeps its current output", () => {
    expect(describeRule("FREQ=DAILY")).toBe("daily");
    expect(describeRule("FREQ=WEEKLY;BYDAY=TU")).toBe("weekly on Tue");
    expect(describeRule("FREQ=WEEKLY;INTERVAL=2;BYDAY=MO,WE")).toBe("every 2 weeks on Mon, Wed");
    expect(describeRule("FREQ=MONTHLY")).toBe("monthly");
    expect(describeRule("FREQ=MONTHLY;BYDAY=TU")).toBe("monthly on Tue");
    expect(describeRule("FREQ=YEARLY")).toBe("yearly");
    expect(describeRule("FREQ=YEARLY;COUNT=3")).toBe("yearly, 3 times");
    expect(describeRule("FREQ=WEEKLY;UNTIL=20261231T000000Z")).toBe("weekly until " + formatDay("2026-12-31"));
  });
});

describe("describeRule fallbacks", () => {
  it.each([
    "FREQ=WEEKLY;BYDAY=2TU",
    "FREQ=YEARLY;BYMONTHDAY=15",
    "FREQ=MONTHLY;BYDAY=-2MO",
    "FREQ=MONTHLY;BYDAY=0MO",
    "FREQ=MONTHLY;BYDAY=6MO",
    "FREQ=MONTHLY;BYDAY=+2TU",
    "FREQ=MONTHLY;BYDAY=1MO,TU",
    "FREQ=MONTHLY;BYDAY=2XX",
    "FREQ=MONTHLY;BYMONTHDAY=-1",
    "FREQ=MONTHLY;BYMONTHDAY=0",
    "FREQ=MONTHLY;BYMONTHDAY=abc",
    "FREQ=MONTHLY;BYMONTHDAY=32",
    "FREQ=MONTHLY;BYMONTHDAY=",
    "FREQ=MONTHLY;BYMONTHDAY",
    "FREQ=MONTHLY;BYDAY=2TU;BYMONTHDAY=",
    "FREQ=MONTHLY;BYDAY=2TU;BYMONTHDAY=15",
    "FREQ=MONTHLY;BYSETPOS=1;BYDAY=MO",
  ])("returns %s unchanged", (rule) => {
    expect(describeRule(rule)).toBe(rule);
  });
});
